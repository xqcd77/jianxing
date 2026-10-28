"""Preflight the complete provider request, preserving mandatory banking data.

This is a conservative UTF-8 estimate, not a provider tokenizer or billed usage.
No network token-counting request or model download is required.
"""

from copy import deepcopy
import hashlib
import json
import re

from .errors import BankError
from .storage import canonical


CRITICAL = re.compile(r"\d|不|勿|禁止|仅|只|必须|需要|别|预算|应急|生活费|金额|风险|授权|锁定|到账|赎回|"
                      r"\b(?:must|never|not|no|cannot|can't|don't|only|without|risk|lock|settle|redeem|transfer|"
                      r"purchase|sell|buy|balance|authoriz)\w*", re.I)


class RequestTokenBudget:
    def __init__(self, settings):
        self.limit = settings.model_context_tokens
        self.margin = settings.model_token_safety_margin
        if type(self.limit) is not int or self.limit <= 0 or type(self.margin) is not int or self.margin < 0:
            raise BankError("TOKEN_BUDGET_CONFIG_INVALID", "模型上下文预算配置无效", 503)

    @staticmethod
    def estimate(request):
        # Include ALL messages, tool schemas/choice, output schemas and extra provider
        # fields. Byte counting intentionally overestimates ordinary BPE text.
        return len(canonical(request).encode("utf-8")) + 128 + 32 * len(request.get("messages", []))

    def prepare(self, original):
        request = deepcopy(original)
        messages = request.get("messages", [])
        if messages and messages[0].get("role") == "system" and isinstance(messages[0].get("content"), str):
            request["messages"][0]["content"] += (
                "\n记忆冲突规则：余额、状态、风险和授权以本轮核验的银行事实为准；本轮明确要求优先于历史草案和偏好，"
                "修改办理条件仍须重新核对授权。历史回执只证明当时操作，不能证明当前余额或本次授权。"
                "同键偏好只使用有效版本；无明确键的文本不得按相似度相互覆盖。"
            )
        reserved = request.get("max_completion_tokens", request.get("max_tokens", 0))
        if type(reserved) is not int or reserved <= 0:
            raise BankError("TOKEN_BUDGET_CONFIG_INVALID", "模型输出预留配置无效", 503)
        before = self.estimate(request)
        metadata = {"counter": "utf8_conservative_estimate", "estimated_before": before,
                    "estimated_input_tokens": before, "output_reserved": reserved,
                    "safety_margin": self.margin, "context_limit": self.limit,
                    "removed_items": 0, "removed_turns": 0}

        def fits():
            metadata["estimated_input_tokens"] = self.estimate(request)
            return metadata["estimated_input_tokens"] + reserved + self.margin <= self.limit

        if fits():
            return request, metadata
        messages = request.get("messages", [])
        payload = None
        if messages and messages[-1].get("role") == "user" and isinstance(messages[-1].get("content"), str):
            try:
                value = json.loads(messages[-1]["content"])
                payload = value if isinstance(value, dict) else None
            except (ValueError, TypeError):
                pass

        # A lossless stage packet has a whole-input checksum and references.
        # Never partially trim it into a dangling or unauditable handoff. If it
        # still cannot fit after deduplication, fail before calling the provider.
        if payload is not None and 'stage_context' not in payload:
            context = payload.get("context", {})
            memories = [payload.get("history_context_only"), payload.get("shared_memory")]
            if isinstance(context, dict):
                memories.append(context.get("shared_memory"))
            # Only optional retrieved background may be evicted. Pinned original
            # clauses, explicit preferences, required rereads and fields stay intact.
            for memory in memories:
                if not isinstance(memory, dict) or not isinstance(memory.get("items"), list):
                    continue
                items = memory["items"]
                for index in range(len(items) - 1, -1, -1):
                    item = items[index]
                    if not isinstance(item, dict) or item.get("kind") == "preference" or item.get("critical_originals") \
                            or item.get("protected_fields") or CRITICAL.search(str(item.get("text", ""))):
                        continue
                    items.pop(index)
                    metadata["removed_items"] += 1
                    messages[-1]["content"] = canonical(payload)
                    if fits():
                        return request, metadata
            # Older transcript can be removed only when structured task state is
            # present, retaining the latest pair and all financial/negative turns.
            if isinstance(context, dict) and (context.get("active_plan") or context.get("pending_request")):
                turns = context.get("turns", [])
                if isinstance(turns, list):
                    for index in range(len(turns) - 3, -1, -1):
                        turn = turns[index]
                        if not isinstance(turn, dict) or CRITICAL.search(str(turn.get("content", ""))):
                            continue
                        turns.pop(index)
                        metadata["removed_turns"] += 1
                        messages[-1]["content"] = canonical(payload)
                        if fits():
                            return request, metadata
        fits()
        failure = BankError("MODEL_CONTEXT_BUDGET_EXCEEDED", "本次资料超过模型上下文预算，尚未执行业务；请缩小分析范围或分步办理。", 422)
        failure.budget = metadata
        raise failure


class TaskTokenBudget:
    """SQL-serialized reservations prevent parallel roles exceeding one task's cap."""

    def __init__(self, settings):
        self.limit = settings.task_token_budget
        if type(self.limit) is not int or self.limit <= 0:
            raise BankError("TOKEN_BUDGET_CONFIG_INVALID", "任务累计Token预算配置无效", 503)

    def reserve(self, session, record, budget):
        from .storage import Document
        from .runtime import append_event

        identity = canonical([record["owner_id"], record["task_id"] or record["id"]])
        key = "runtime_budget_" + hashlib.sha256(identity.encode()).hexdigest()
        document = session.get(Document, key)
        data = document.data if document else {"limit": self.limit, "allocated": 0, "charges": {}}
        limit = min(self.limit, data.get("limit", self.limit))
        reserved = budget["estimated_input_tokens"] + budget["output_reserved"]
        if data["allocated"] + reserved > limit:
            error = BankError("TASK_TOKEN_BUDGET_EXCEEDED", "本任务累计Token预算不足，已停止后续模型调用，请缩小任务范围。", 422)
            error.budget = {**budget, "task_limit": limit, "task_allocated": data["allocated"]}
            raise error
        data = {**data, "limit": limit, "allocated": data["allocated"] + reserved,
                "charges": {**data["charges"], record["id"]: {"tokens": reserved, "settled": False}}}
        if document:
            document.data = data
        else:
            session.add(Document(id=key, kind="runtime_budget", owner_id=record["owner_id"], data=data))
        record["_budget_key"] = key
        append_event(session, "budget.task_reserved", record["owner_id"], run_id=record["id"], role=record["role"],
                     task_id=record["task_id"],
                     details={"reserved_tokens": reserved, "task_allocated": data["allocated"], "task_limit": limit})

    @staticmethod
    def settle(session, record, reported_tokens):
        from .storage import Document
        from .runtime import append_event

        if not record.get("_budget_key"):
            return
        document = session.get(Document, record["_budget_key"])
        if document is None:
            return  # Reservation transaction did not commit.
        charge = document.data["charges"][record["id"]]
        if charge["settled"]:
            return
        # Unknown usage after dispatch retains the conservative reservation.
        # A pre-dispatch cancellation can release its reservation completely.
        charged = reported_tokens if reported_tokens is not None else charge["tokens"] if record.get("_provider_started") else 0
        data = document.data
        document.data = {**data, "allocated": data["allocated"] - charge["tokens"] + charged,
                         "charges": {**data["charges"], record["id"]: {"tokens": charged, "settled": True}}}
        append_event(session, "budget.task_settled", record["owner_id"], run_id=record["id"], role=record["role"],
                     task_id=record["task_id"],
                     details={"charged_tokens": charged, "task_allocated": document.data["allocated"]})
