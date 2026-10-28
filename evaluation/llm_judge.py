"""MiMo JSON-mode client; no shared model history, banking tools or unsafe fallback."""
import asyncio
from dataclasses import dataclass
import hashlib
import json
import time

import httpx
from pydantic import ValidationError

from .config import JudgeSettings
from .judge_schemas import JudgePacket, JudgeVerdict
from .rubrics import JUDGE_PROMPT, RUBRIC_VERSION
from .trace import scrub


class JudgeError(Exception):
    def __init__(self, code, attempts=None):
        self.code, self.attempts = code, attempts or []
        super().__init__(code)


@dataclass
class Judgement:
    verdict: JudgeVerdict
    attempts: list[dict]
    rubric_version: str
    packet_hash: str
    prompt_hash: str


def judge_system_prompt():
    return JUDGE_PROMPT + "\nJSON Schema：" + json.dumps(JudgeVerdict.model_json_schema(), ensure_ascii=False)


def packet_hash(packet):
    data = scrub(packet.model_dump(mode="json"))
    return hashlib.sha256(json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def validate_cached_result(cached, packet, manifest_hash, packet_version):
    if (cached.get("manifest_hash") != manifest_hash or cached.get("packet_hash") != packet_hash(packet)
            or cached.get("prompt_hash") != hashlib.sha256(judge_system_prompt().encode()).hexdigest()
            or cached.get("rubric_version") != RUBRIC_VERSION
            or cached.get("packet_version", "api-v1") != packet_version):
        raise ValueError("Cached judge result is incompatible; use a new judge revision")
    if cached.get("status") == "ok":
        JudgeVerdict.model_validate(cached["verdict"]).validate_against(packet)
    elif cached.get("status") not in {"judge_error", "packet_error"}:
        raise ValueError("Unknown cached judge result status")


class MiMoJudge:
    def __init__(self, settings: JudgeSettings, transport=None):
        self.settings, self.transport = settings, transport
        self._semaphore = asyncio.Semaphore(settings.concurrency)
        self._rate_lock = asyncio.Lock()
        self._next_start = 0.0

    async def _rate_slot(self):
        async with self._rate_lock:
            now = time.monotonic()
            delay = max(0, self._next_start - now)
            self._next_start = max(now, self._next_start) + 60 / self.settings.rpm
        if delay:
            await asyncio.sleep(delay)

    async def grade(self, packet: JudgePacket):
        if not self.settings.api_key:
            raise JudgeError("JUDGE_NOT_CONFIGURED")
        # Redact before sending or hashing; the same public packet is used by all variants.
        safe = scrub(packet.model_dump(mode="json"))
        clean_packet = JudgePacket.model_validate(safe)
        packet_json = json.dumps(safe, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        system = judge_system_prompt()
        attempts, format_notice = [], ""
        async with self._semaphore:
            async with httpx.AsyncClient(timeout=self.settings.timeout, trust_env=False,
                                         follow_redirects=False, transport=self.transport) as client:
                for attempt in range(self.settings.format_retries + 1):
                    await self._rate_slot()
                    body = {
                        "model": self.settings.model,
                        "messages": [{"role": "system", "content": system + format_notice},
                                     {"role": "user", "content": packet_json}],
                        "response_format": {"type": "json_object"},
                        "thinking": {"type": "enabled" if self.settings.thinking else "disabled"},
                        "max_completion_tokens": self.settings.max_completion_tokens,
                        "stream": False,
                    }
                    # MiMo thinking mode does not expose DeepSeek's max reasoning_effort.
                    # Temperature is deliberately omitted; it is overridden in thinking mode.
                    started = time.perf_counter()
                    meta = {"attempt": attempt + 1, "requested_model": self.settings.model, "usage": None}
                    try:
                        response = await client.post(self.settings.base_url + "/chat/completions", json=body,
                            headers={"api-key": self.settings.api_key, "Content-Type": "application/json"})
                    except httpx.HTTPError:
                        meta.update(status="transport_error", duration_ms=round((time.perf_counter() - started) * 1000, 2))
                        attempts.append(meta)
                        raise JudgeError("JUDGE_TRANSPORT_ERROR", attempts) from None
                    meta.update(http_status=response.status_code,
                                duration_ms=round((time.perf_counter() - started) * 1000, 2))
                    if response.status_code != 200:
                        meta["status"] = "http_error"
                        attempts.append(meta)
                        raise JudgeError(f"JUDGE_HTTP_{response.status_code}", attempts)
                    try:
                        data = response.json()
                        meta.update(provider_model=data.get("model"), usage=scrub(data.get("usage")))
                        choice = data["choices"][0]
                        reason = choice.get("finish_reason")
                        if reason != "stop":
                            raise ValueError("incomplete_response")
                        message = choice["message"]
                        if message.get("tool_calls"):
                            raise ValueError("unexpected_tool_call")
                        verdict = JudgeVerdict.model_validate_json(message["content"]).validate_against(clean_packet)
                    except (ValueError, ValidationError, KeyError, IndexError, TypeError):
                        meta["status"] = "format_error"
                        attempts.append(meta)
                        if attempt < self.settings.format_retries:
                            format_notice = "\n上次输出未通过结构校验。重新独立评分，严格覆盖所有子项ID，分数为1到5整数，仅引用已有来源ID。"
                            continue
                        raise JudgeError("JUDGE_OUTPUT_INVALID", attempts) from None
                    meta["status"] = "ok"
                    attempts.append(meta)
                    # reasoning_content is never retained, returned or passed into a repair.
                    return Judgement(verdict, attempts, RUBRIC_VERSION,
                        hashlib.sha256(packet_json.encode()).hexdigest(), hashlib.sha256(system.encode()).hexdigest())
