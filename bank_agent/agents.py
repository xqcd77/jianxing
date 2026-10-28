"""Role-isolated planning agents. They propose tasks; only the gateway can execute."""

from datetime import datetime, timedelta
from decimal import Decimal
import json
import re

import httpx
from pydantic import ValidationError

from .errors import BankError, ClarificationNeeded
from .dag import precedes, references, topological
from .operations import DEFINITIONS, PARAM_MODELS
from .prompts import BANKING_SYSTEM_PROMPT
from .dialogue import is_followup, reconcile_missing_fields, transfer_grounding, period_slot
from .resources import subscription_grounding, card_unlock_grounding
from .schemas import AgentProposal, DomainTask, Intent, NodeSpec
from .storage import audit, canonical
from .memory import SENSITIVE, SharedMemory
from .conversation import conversation_intent
from .information import scoped_information_intent


ROLES = {
    "planner": "理解用户、补全上下文、选择领域、拆解任务；不能确认或执行业务",
    "funds": "余额与预留、收款人核实、即时/定时转账、AA创建与支付；维护可用资金约束",
    "bills": "消费分类、月报年报、疑似重复扣款、已知现金流压力测试；所有数字来自账本",
    "wealth": "适当性测评、产品对比、申购、赎回及结算；未到账赎回不计入现金",
    "cards": "虚拟/实体卡申请、额度、类别与金额限制、解锁、挂失/解挂；银行规则独立审批",
    "subscriptions": "识别扣费候选、查询合同、续费提醒、撤销代扣与取消商户合同；分别报告结果",
    "life": "确认生日日期与配送信息、生成目标、核对报价、采购订单与退款；请求Funds锁定预算",
    "reviewer": "检验操作归属、参数、DAG和授权入口；记录解释与证据，不能替用户授权",
}

SCENARIOS = {
    "salary": {
        "label": "工资分配与生日预算",
        "message": "预留3000元生活费，为10月20日爱人生日锁定1000元并提前两天买鲜花蛋糕，给妈妈转800元，申购模拟现金产品500元，分析本月账单",
        "tasks": [
            {"domain": "funds", "action": "funds.reserve", "params": {"amount_fen": 300000, "purpose": "生活费"}},
            {"domain": "life", "action": "life.goal", "params": {"birthday_date": "2026-10-20", "budget_fen": 100000}},
            {"domain": "funds", "action": "funds.transfer", "params": {"recipient_hint": "妈妈", "amount_fen": 80000}},
            {"domain": "wealth", "action": "wealth.purchase", "params": {"product_id": "P_CASH", "amount_fen": 50000}},
            {"domain": "bills", "action": "bills.report", "params": {"period": "2026-10"}},
            {"domain": "bills", "action": "cashflow.project", "params": {"days": 30, "expense_increase_pct": 20}},
        ],
    },
    "six": {
        "label": "六领域联合办理",
        "message": "查询余额和年度账单，对比理财，查看卡片，识别订阅，为爱人10月20日生日预留1000元采购鲜花蛋糕",
        "tasks": [
            {"domain": "funds", "action": "balance.query", "params": {}},
            {"domain": "bills", "action": "bills.report", "params": {"period": "2026", "annual": True}},
            {"domain": "wealth", "action": "wealth.compare", "params": {}},
            {"domain": "cards", "action": "cards.query", "params": {}},
            {"domain": "subscriptions", "action": "subscriptions.detect", "params": {}},
            {"domain": "life", "action": "life.goal", "params": {"birthday_date": "2026-10-20", "budget_fen": 100000}},
        ],
    },
    "aa": {
        "label": "三人AA收款",
        "message": "聚餐300.01元，我和小王、小李AA",
        "tasks": [
            {
                "domain": "funds",
                "action": "aa.create",
                "params": {"amount_fen": 30001, "participants": ["u_demo", "u_wang", "u_li"], "subject": "聚餐AA"},
            },
        ],
    },
    "subscriptions": {
        "label": "识别并取消视频订阅",
        "message": "识别订阅，查询合同，取消视频会员并设置音乐会员提前两天提醒",
        "tasks": [
            {"domain": "subscriptions", "action": "subscriptions.detect", "params": {}},
            {"domain": "subscriptions", "action": "subscriptions.query", "params": {}},
            {
                "domain": "subscriptions",
                "action": "subscriptions.cancel",
                "params": {"subscription_id": "S_VIDEO", "scope": "both"},
            },
            {
                "domain": "subscriptions",
                "action": "subscriptions.remind",
                "params": {"subscription_id": "S_MUSIC", "days_before": 2},
            },
        ],
    },
    "card": {
        "label": "申请虚拟卡并设置交易限制",
        "message": "申请日常虚拟卡，将主卡单笔限额设为500元，日限额1000元，禁止游戏交易",
        "tasks": [
            {"domain": "cards", "action": "cards.apply", "params": {"kind": "virtual", "name": "日常虚拟卡"}},
            {
                "domain": "cards",
                "action": "cards.restrict",
                "params": {"single_limit_fen": 50000, "daily_limit_fen": 100000, "blocked_categories": ["gaming"]},
            },
        ],
    },
}


def money(text):
    match = re.search(r"(\d+(?:\.\d+)?)\s*(万|千)?\s*(?:元|块)", text)
    match = match or re.search(r"(\d+(?:\.\d+)?)\s*(万|千)?", text)
    if not match:
        raise ClarificationNeeded("请明确金额，例如800元或300.01元")
    value = Decimal(match[1]) * {None: 1, "千": 1000, "万": 10000}[match[2]] * 100
    if value != value.to_integral_value():
        raise ClarificationNeeded("金额最多保留两位小数")
    return int(value)


class ModelAdapter:
    def __init__(self, settings):
        self._settings = settings
        from .harness import BankAgentHarness
        self.harness = BankAgentHarness(settings)

    @property
    def settings(self):
        registry = self.harness.model_registry
        return registry.effective(self._settings) if registry else self._settings

    @settings.setter
    def settings(self, value):
        self._settings = value

    def _headers(self):
        registry = self.harness.model_registry
        return registry.headers(self.settings) if registry else {'Authorization': 'Bearer ' + self.settings.model_api_key}

    def _request(self, role, payload, schema):
        if not self.settings.model_api_key:
            raise BankError("MODEL_NOT_CONFIGURED", "请在本地环境变量中配置模型密钥", 503)
        repair = bool(payload.get("_protocol_repair"))
        payload = {k: v for k, v in payload.items() if k != "_protocol_repair"}
        from .capabilities import selected_capabilities
        operations = selected_capabilities(payload,role)
        system = self.system_prompt + (
            "当前角色：" + role + "。职责：" + ROLES[role] + "。"
            "AA使用已知参与者u_demo、u_wang、u_li。"
            "下列是唯一业务工具目录，operation对应action，owner对应domain：" + canonical(operations)
        )
        request = {
            "model": self.settings.model_name,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": canonical(payload)}],
            "tools": [
                {
                    "type": "function",
                    "function": {
                        "name": "submit_plan",
                        "description": "提交完整需求草案：可执行银行任务和additional_goals逐项对应原始请求；不是执行交易或授予确认权限",
                        "parameters": schema.model_json_schema(),
                    },
                }
            ],
            "tool_choice": {"type": "function", "function": {"name": "submit_plan"}},
            "max_tokens": self.settings.model_max_tokens if self.settings.model_thinking and not repair else 4096,
            "response_format": {"type": "json_object"},
        }
        if "api.deepseek.com" in self.settings.model_base_url:
            request["thinking"] = {"type": "enabled" if self.settings.model_thinking and not repair else "disabled"}
            if self.settings.model_thinking and not repair:
                if self.settings.model_reasoning_effort not in ["low", "high", "max"]:
                    raise BankError("MODEL_CONFIG_INVALID", "推理强度配置无效，请检查本地模型设置", 503)
                request["reasoning_effort"] = self.settings.model_reasoning_effort
                # Thinking + forced tools is unsupported; mixing optional tool
                # calls and JSON output also leaves two possible response paths.
                # The planner needs a typed proposal, not an API tool execution.
                request.pop("tools")
                request.pop("tool_choice")
                request["messages"][0]["content"] += (
                    "\n本次协议要求：仅返回下列schema对应的JSON对象，不调用函数、不包装成name/arguments，不输出代码块。"
                    "tasks是拟办理的业务，不代表调用工具或获得授权。JSON Schema："
                    + canonical(schema.model_json_schema())
                )
            else:
                request["temperature"] = 0
        if repair:
            request.pop("tools", None)
            request.pop("tool_choice", None)
            request.pop("reasoning_effort", None)
            request["messages"][0]["content"] += (
                "\n这是一次有界结构恢复。根据原始用户需求重新提取，不续写截断内容，不猜测缺失金额、对象或授权。"
                "只输出符合下列schema的JSON；summary简短，不能输出分析过程、字段说明或外层包装。JSON Schema："
                + canonical(schema.model_json_schema())
            )
        return request

    @property
    def system_prompt(self):
        return BANKING_SYSTEM_PROMPT

    def structured(self, role, payload, schema):
        payload = self.harness.prepare_payload(role, payload)
        request = self._contextual_request(role, payload, schema)
        import asyncio

        def post():
            try:
                with httpx.Client(timeout=self.settings.model_timeout, transport=getattr(self, "http_transport", None)) as client:
                    return client.post(
                        self.settings.model_base_url + "/chat/completions", json=request,
                        headers=self._headers(),
                    )
            except httpx.HTTPError as error:
                raise BankError("MODEL_RESPONSE_INVALID", "模型超时；没有执行业务", 502) from error

        async def invoke():
            return await asyncio.to_thread(post)
        return self.harness.run_sync(role, request, schema, invoke, self._catalog_decoder(role,payload))

    def _catalog_decoder(self, role, payload):
        from .capabilities import selected_capabilities
        allowed = {c['operation'] for c in selected_capabilities(payload,role)}
        def decode(response, schema):
            result = self._decode(response,schema)
            if isinstance(result,Intent) and any(t.action not in allowed for t in result.tasks):
                raise BankError('MODEL_RESPONSE_INVALID','本次草案使用了需求范围之外的业务，请重新核对；没有执行业务',502)
            return result
        return decode

    def _contextual_request(self, role, payload, schema):
        request = self._request(role, payload, schema)
        if self.harness.context_builder is not None and payload.get('task_brief'):
            from .context_builder import CONTEXT_POLICY
            request['messages'][0]['content'] += '\n' + CONTEXT_POLICY
        if self.harness.model_registry:
            request = self.harness.model_registry.adapt(request, role)
        if self.harness.stage_packer is not None:
            from .stage_context import POLICY
            # Pack the final payload, after tool selection and protocol repair.
            # Never resurrect fields that a role-specific request removed.
            message = request['messages'][-1]
            packed, metadata = self.harness.stage_packer.prepare(role, json.loads(message['content']))
            if metadata['reference_count']:
                metadata['estimated_before'] = self.harness.budget.estimate(request)
                message['content'] = canonical(packed)
                request['messages'][0]['content'] += '\n' + POLICY
                metadata['estimated_input_tokens'] = self.harness.budget.estimate(request)
                self.harness.telemetry.emit('context.compacted', role=role, details=metadata)
        return request

    @staticmethod
    def _decode(response, schema):
        try:
            if response.status_code != 200:
                failure = BankError("MODEL_HTTP_ERROR", f"模型接口返回{response.status_code}；没有执行业务", 502)
                failure.provider_status = response.status_code
                raise failure
            choice = response.json()["choices"][0]
            if choice.get("finish_reason") == "length":
                raise BankError("MODEL_OUTPUT_TRUNCATED", "本次安排未完整生成，请重试；没有执行业务", 502)
            message = choice["message"]
            calls = message.get("tool_calls") or []
            if calls:
                if len(calls) != 1 or calls[0]["function"]["name"] != "submit_plan":
                    raise BankError("MODEL_RESPONSE_INVALID", "本次安排未通过校验；没有执行业务", 502)
                content = calls[0]["function"]["arguments"]
            else:
                content = message.get("content", "")
            # Never persist or expose reasoning_content. Business explanations
            # are produced from validated slots, gateway states and receipts.
            # Some OpenAI-compatible models prefix final content with a think
            # block. Discard it locally; never persist or display that material.
            if isinstance(content, str):
                content = re.sub(r'^\s*<think>.*?</think>\s*', '', content, count=1, flags=re.S)
            return schema.model_validate(json.loads(content))
        except BankError:
            raise
        except ValidationError as error:
            # Persist error categories and declared field paths, never input
            # values, arbitrary extra keys, provider bodies, or private reasoning.
            names = set()
            def declared(value):
                if isinstance(value, dict):
                    names.update(value.get("properties", {}))
                    for child in value.values():
                        declared(child)
                elif isinstance(value, list):
                    for child in value:
                        declared(child)
            declared(schema.model_json_schema())
            errors = [{"type": e["type"], "path": [p if (type(p) is int and 0 <= p < 1000) or p in names
                        else "<undeclared_field>" for p in e["loc"]]}
                      for e in error.errors(include_input=False, include_context=False, include_url=False)[:8]]
            failure = BankError("MODEL_RESPONSE_INVALID", "本次办理安排未通过核验，请重试或使用业务表单；没有执行业务", 502)
            failure.diagnostic = {"kind": "schema_validation", "schema": schema.__name__, "fields": errors}
            raise failure from None
        except json.JSONDecodeError:
            failure = BankError("MODEL_RESPONSE_INVALID", "本次办理安排未完整返回，请重试；没有执行业务", 502)
            failure.diagnostic = {"kind": "invalid_json", "schema": schema.__name__}
            raise failure from None
        except (httpx.HTTPError, ValueError, KeyError, IndexError, TypeError) as error:
            raise BankError("MODEL_RESPONSE_INVALID", "模型超时或未返回合规结构；没有执行业务", 502) from error

    async def astructured(self, role, payload, schema):
        import asyncio

        if "structured" in self.__dict__:
            return await asyncio.to_thread(self.structured, role, payload, schema)
        payload = await asyncio.to_thread(self.harness.prepare_payload, role, payload)
        request = self._contextual_request(role, payload, schema)

        async def invoke():
            try:
                async with httpx.AsyncClient(timeout=self.settings.model_timeout, transport=getattr(self, "http_transport", None)) as client:
                    return await client.post(
                        self.settings.model_base_url + "/chat/completions",
                        json=request,
                        headers=self._headers(),
                    )
            except httpx.HTTPError as error:
                raise BankError("MODEL_RESPONSE_INVALID", "模型超时；没有执行业务", 502) from error
        return await self.harness.run(role, request, schema, invoke, self._catalog_decoder(role,payload))


class PlannerAgent:
    def __init__(self, db, bank, settings):
        self.db, self.bank, self.settings = db, bank, settings
        self.model = ModelAdapter(settings)
        self.memory = SharedMemory(db, bank, settings)
        from .context_builder import ContextBuilder
        self.context_builder = ContextBuilder(db, bank, self.memory)

    @staticmethod
    def _transient(error):
        return (error.code == "MODEL_HTTP_ERROR" and getattr(error, "provider_status", None) in {429, 500, 502, 503, 504}) or (
            error.code == "MODEL_RESPONSE_INVALID" and isinstance(error.__cause__, httpx.TransportError))

    @staticmethod
    def _protocol_failure(error):
        return error.code in {"MODEL_OUTPUT_TRUNCATED","MODEL_DEADLINE_EXCEEDED"} or (
            error.code == "MODEL_RESPONSE_INVALID" and getattr(error, "diagnostic", {}).get("kind") in {"invalid_json", "schema_validation"})

    def _grounded_fallback(self,user_id,conversation_id,grounded,error):
        with self.db.write() as session:
            audit(session,user_id,"MODEL_GROUNDED_FALLBACK",{"error_code":error.code,
                "source":"explicit_user_slots_and_verified_resources","authorizes_business":False})
        return self._reconcile(user_id,conversation_id,grounded,Intent(summary="恢复明确业务字段"))

    def _record_retry(self, user_id, error):
        with self.db.write() as session:
            audit(session, user_id, "MODEL_REQUEST_RETRY", {"role": "planner", "attempt": 1,
                "error_code": error.code, "provider_status": getattr(error, "provider_status", None)})

    def explicit_intent(self, user_id, message, facts):
        from .source_intents import explicit_intent
        from .storage import Account
        with self.db.read() as session:
            participants = {c['name']: account.user_id for c in facts['contacts'] if c.get('verified')
                and (account := session.get(Account, c['account_id']))}
        return explicit_intent(message, facts, user_id, participants)

    def _inputs(self, user_id, message, conversation_id):
        if SENSITIVE.search(message):
            return Intent(
                summary="敏感凭证不可进入对话",
                reject_reason="请在安全中心输入凭证；聊天不会办理凭证验证",
                rejection_kind="security",
            )
        if any(text in message.lower() for text in ["忽略权限", "跳过验证", "跳过确认", "ignore previous", "伪造回执"]):
            return Intent(
                summary="权限绕过请求",
                reject_reason="操作权限只能由银行网关判定，不能通过对话跳过",
                rejection_kind="security",
            )
        from .intake import request_manifest
        compound = len({g['kind'] for g in request_manifest(message)})>1
        answer = None if compound else conversation_intent(message, self.settings) or scoped_information_intent(
            self.db, user_id, message, conversation_id,
        )
        if answer:
            return answer
        facts = self.bank.facts(user_id)
        explicit = self.explicit_intent(user_id, message, facts)
        if explicit:
            return explicit
        card_request = card_unlock_grounding(message, facts)
        if card_request:
            return card_request  # Explicit operation and verified card; the gateway still enforces state and MFA.
        context = self.context_builder.read(user_id, message, conversation_id, facts=facts, include_memory=True)
        grounded = transfer_grounding(message, facts, context) or subscription_grounding(message)
        from .intake import request_manifest
        context["request_manifest"] = request_manifest(message)
        return facts, context, grounded

    def plan(self, user_id, message, conversation_id=None):
        inputs = self._inputs(user_id, message, conversation_id)
        if isinstance(inputs, Intent):
            return inputs
        facts, context, grounded = inputs
        if grounded and context.get("pending_draft") and is_followup(message, facts, context["pending_draft"]):
            return grounded
        if self.settings.model_mode == "llm":
            payload = {"message": message, "facts": facts, "context": context}
            for attempt in range(2):
                try:
                    intent = self.model.structured("planner", payload, Intent)
                    break
                except BankError as error:
                    if grounded is not None and self._protocol_failure(error):
                        return self._grounded_fallback(user_id,conversation_id,grounded,error)
                    if attempt or not (self._transient(error) or self._protocol_failure(error)):
                        raise
                    self._record_retry(user_id, error)
                    if self._protocol_failure(error):
                        payload = {**payload, "_protocol_repair": True}
            return self._reconcile(user_id, conversation_id, grounded, intent, message)
        return self._local(user_id, message, facts, context, grounded)

    async def aplan(self, user_id, message, conversation_id=None):
        import asyncio

        if "plan" in self.__dict__:
            return await asyncio.to_thread(self.plan, user_id, message, conversation_id)
        inputs = await asyncio.to_thread(self._inputs, user_id, message, conversation_id)
        if isinstance(inputs, Intent):
            return inputs
        facts, context, grounded = inputs
        if grounded and context.get("pending_draft") and is_followup(message, facts, context["pending_draft"]):
            return grounded
        if self.settings.model_mode == "llm":
            payload = {"message": message, "facts": facts, "context": context}
            for attempt in range(2):
                try:
                    intent = await self.model.astructured(
                        "planner", payload, Intent
                    )
                    break
                except BankError as error:
                    if grounded is not None and self._protocol_failure(error):
                        return await asyncio.to_thread(self._grounded_fallback,user_id,conversation_id,grounded,error)
                    if attempt or not (self._transient(error) or self._protocol_failure(error)):
                        raise
                    await asyncio.to_thread(self._record_retry, user_id, error)
                    if self._protocol_failure(error):
                        payload = {**payload, "_protocol_repair": True}
            return await asyncio.to_thread(self._reconcile, user_id, conversation_id, grounded, intent, message)
        return self._local(user_id, message, facts, context, grounded)

    def _reconcile(self, user_id, conversation_id, grounded, intent, message=""):
        from .dialogue import explicit_transfers
        facts = self.bank.facts(user_id) if message else None
        bindings = explicit_transfers(message, facts) if message else []
        if bindings and message and not (intent.reject_reason and intent.rejection_kind=='security'):
            from .intake import request_manifest, ensure_goal_coverage
            if len({e['kind'] for e in request_manifest(message)})>1 and (intent.answer or intent.information_query):
                intent=ensure_goal_coverage(intent,message)
        # Explicit, timeless source bindings can restore an omitted banking
        # goal. They still create drafts, never authorization or execution.
        conditional = re.search(r'如果|假如|要是|是否|明天|后天|预约|每月|下周|\d{4}-\d{2}-\d{2}|'
            r'只分析|先分析|只算|假设|不操作|不要操作|不要办理|不办理|能不能|能否|可不可以|可以吗|好不好|怎么|如何|解释|了解|只看|先算',message)
        if bindings and not conditional and not intent.answer and not intent.information_query and not (intent.reject_reason and intent.rejection_kind=='security'):
            for binding in bindings:
                aliases = {label for c in facts['contacts'] if c.get('verified') and
                    binding['recipient_hint'] in [c.get('id'),c['name'],c.get('phone'),*c.get('aliases',[]),*c.get('notes',[])]
                    for label in [c.get('id'),c['name'],c.get('phone'),*c.get('aliases',[]),*c.get('notes',[])]}
                if not any(t.action=='funds.transfer' and t.params.get('recipient_hint') in aliases for t in intent.tasks):
                    intent.tasks.append(DomainTask(domain='funds',action='funds.transfer',params={
                        'recipient_hint':binding['recipient_hint'],'amount_fen':binding['amount_fen']}))
        for task in intent.tasks:
            if task.action != "funds.transfer":
                continue
            matched_contacts = [c for c in facts['contacts'] if c.get('verified') and
                task.params.get('recipient_hint') in [c.get('id'),c['name'],c.get('phone'),*c.get('aliases',[]),*c.get('notes',[])]]
            matches = [b for b in bindings if any(b['recipient_hint'] in [c['name'],c.get('phone'),*c.get('aliases',[]),*c.get('notes',[])] for c in matched_contacts)]
            if len(matches)==1:
                task.params={**task.params,'recipient_hint':matches[0]['recipient_hint']}
            elif len(matched_contacts)==1 and sum(c.get('verified') and c['name']==matched_contacts[0]['name'] for c in facts['contacts'])==1:
                task.params={**task.params,'recipient_hint':matched_contacts[0]['name']}
            if len(matches) == 1 and task.params.get("amount_fen") != matches[0]["amount_fen"]:
                task.params = {**task.params, "amount_fen": matches[0]["amount_fen"]}
                with self.db.write() as session:
                    audit(session, user_id, "INTENT_SLOTS_GROUNDED", {
                        "conversation_id": conversation_id, "fields": ["amount_fen"],
                        "source": "recipient_bound_transfer_clause",
                    })
        if message:
            # A plain budget reserve has no linked life goal. Do not let an
            # invented optional ID turn it into an unrelated goal operation.
            from .storage import Document
            for task in intent.tasks:
                goal_id = task.params.get('goal_id') if task.action == 'funds.reserve' else None
                if (not isinstance(goal_id, str) or not goal_id or goal_id.startswith('$')
                        or goal_id in message or re.search(r'目标|生日|鲜花|蛋糕|礼物', message)
                        or not re.search(r'预留|留出|留好|锁定|冻结', message)):
                    continue
                with self.db.write() as session:
                    if session.get(Document, goal_id) is None:
                        task.params = {**task.params, 'goal_id': ''}
                        audit(session, user_id, 'INTENT_SLOTS_GROUNDED', {
                            'conversation_id': conversation_id, 'fields': ['goal_id'],
                            'source': 'ordinary_reserve_without_linked_goal',
                        })
            from .request_understanding import remaining_wealth_budget
            purchases=[t for t in intent.tasks if t.action=='wealth.purchase']+[
                g.pending_task for g in intent.additional_goals if g.pending_task and g.pending_task.action=='wealth.purchase']
            budget=remaining_wealth_budget(message,facts,intent.tasks)
            if budget and not purchases and re.search(r'用于|用来|划出|拿|投入',message) and not re.search(r'只分析|只推荐|不申购|不买',message):
                purchases=[DomainTask(domain='wealth',action='wealth.purchase',params={'amount_fen':budget})]
                intent.tasks.extend(purchases)
            if len(purchases)==1:
                purchase=purchases[0]
                purchase.params={key:value for key,value in purchase.params.items() if value is not None and value!=''}
                if budget:
                    purchase.params={**purchase.params,'amount_fen':budget}
                selected=next((p for p in facts['products'] if p['id']==purchase.params.get('product_id')),None)
                if selected and selected['name'] not in message and selected['id'] not in message:
                    purchase.params.pop('product_id',None)
            if purchases and re.search(r'推荐|比较|对比|建议',message) and not re.search(r'ETF',message,re.I) and not any(t.action=='wealth.compare' for t in intent.tasks):
                intent.tasks.append(DomainTask(domain='wealth',action='wealth.compare'))
            from .information import ground_investment_query
            total = sum(t.params.get('amount_fen',0) for t in intent.tasks if t.action=='funds.transfer')
            for goal in intent.additional_goals:
                if goal.information_query:
                    goal.information_query = ground_investment_query(goal.information_query,message,total,facts['account']['available_fen'])
            if intent.information_query:
                intent.information_query = ground_investment_query(intent.information_query,message,total,facts['account']['available_fen'])
        for task in intent.tasks:
            if task.action != "aa.create" or not isinstance(task.params.get("participants"), list) or not all(isinstance(v,str) for v in task.params["participants"]):
                continue
            if re.search(r"不包括我|不算我|我不(?:参与|参加|分摊)", message):
                return Intent(summary="核对AA参与范围", clarification="当前AA收款需要发起人参与分摊。你明确不参与，请先核对由谁发起，或改为明确金额的转账收款。")
            contacts = self.bank.facts(user_id)["contacts"]
            from .storage import Account, User
            participants = []
            with self.db.read() as session:
                owner = session.get(User,user_id)
                for value in task.params["participants"]:
                    resolved = value
                    if value == "我" or (owner and value == owner.name and "我" in message):
                        resolved = user_id
                    elif isinstance(value,str) and not value.startswith("u_"):
                        matches = [c for c in contacts if c.get("verified") and value in [c["name"],c["phone"],*c.get("aliases",[])]
                            and any(label in message for label in [c["name"],c["phone"],*c.get("aliases",[])])]
                        if len(matches)==1:
                            account=session.get(Account,matches[0]["account_id"])
                            if account and session.get(User,account.user_id):
                                resolved=account.user_id
                    participants.append(resolved)
            if "我" in message and user_id not in participants:
                participants.insert(0,user_id)
            if participants != task.params["participants"]:
                task.params = {**task.params, "participants": list(dict.fromkeys(participants))}
                with self.db.write() as session:
                    audit(session,user_id,"INTENT_SLOTS_GROUNDED",{"conversation_id":conversation_id,
                        "fields":["participants"],"source":"authenticated_actor_and_explicit_verified_contacts"})
        if grounded and not (intent.reject_reason and intent.rejection_kind == "security"):
            if grounded.model_dump(mode="json") != intent.model_dump(mode="json"):
                with self.db.write() as session:
                    audit(
                        session,
                        user_id,
                        "INTENT_SLOTS_GROUNDED",
                        {
                            "conversation_id": conversation_id,
                            "fields": list(grounded.tasks[0].params) if grounded.tasks else [],
                            "source": "explicit_user_message_and_verified_contacts",
                        },
                    )
            return grounded
        from .intake import ensure_goal_coverage
        intent = reconcile_missing_fields(intent)
        if message:
            from .dialogue import reconcile_bill_period
            intent = reconcile_bill_period(intent,message,facts['business_now'])
        if intent.clarification and intent.tasks and all(t.action in {'funds.transfer','balance.query'} for t in intent.tasks) and re.search(r'ETF|会员|投资期限|风险承受|证券平台|基金平台',intent.clarification,re.I):
            try:
                for task in intent.tasks:
                    PARAM_MODELS[task.action].model_validate(task.params)
            except ValidationError:
                pass
            else:
                note = next((g for g in intent.additional_goals if g.information_query or g.kind=='capability_gap'),None)
                if note:
                    note.message = (note.message+' '+intent.clarification).strip()[:600]
                    intent = intent.model_copy(update={'clarification':None,'clarification_kind':'other'})
        return ensure_goal_coverage(intent, message)

    def _local(self, user_id, message, facts, context, grounded):
        if grounded:
            return grounded
        if context.get("pending_request"):
            original = context["pending_request"]
            named = [
                c["name"]
                for c in facts["contacts"]
                if message.strip() in [c["name"], c["phone"], *c["aliases"], *c["notes"]]
            ]
            if "转" in original and len(named) == 1:
                original = re.sub(r"给.*?(?=转|\d|$)", "给" + named[0], original)
                message = original if "给" in original else "给" + named[0] + original
            else:
                message = original + " " + message
        return self._demo(user_id, message, facts, context)

    def _demo(self, user_id, message, facts, context):
        tasks = []

        def add(op, params=None, when=None):
            tasks.append(DomainTask(domain=DEFINITIONS[op][0], action=op, params=params or {}, scheduled_for=when))

        text = message.strip()
        if (re.match(r"(?:请|帮我)?(?:查询|查看|统计|分析)", text) and "账单" in text
                and not re.search(r"给.+?转|转账给|帮我申购|我要申购", text)):
            period = period_slot(text, facts["business_now"]) or facts["business_now"][:7]
            return Intent(summary="查询指定期间账单", tasks=[DomainTask(domain="bills", action="bills.report",
                params={"period": period, "annual": len(period) == 4})])
        if text == "运行工资分配示例":
            return Intent(summary=SCENARIOS["salary"]["message"], tasks=SCENARIOS["salary"]["tasks"])
        for clause in re.split(r"[，,；;。\n]|然后|并且", text):
            if not clause.strip():
                continue
            if re.match(r"\s*(?:不要|别|不)(?:再|替我)?(?:转|申购|购买|解挂|取消|付款|办理)", clause):
                continue
            if re.search(r"账单|月报|年报|消费分类", clause) and re.search(r"查|统计|分析|报告", clause) and not re.search(r"给.+?转", clause):
                period = period_slot(clause, facts["business_now"]) or facts["business_now"][:7]
                add("bills.report", {"period": period, "annual": len(period) == 4})
                continue
            if re.search(r"比较|对比|推荐", clause) and re.search(r"理财|产品|申购条件", clause) and not re.search(r"帮我申购|我要申购|直接买", clause):
                add("wealth.compare")
                continue
            if any(word in clause for word in ["转给", "转账", "再转"]) or ("给" in clause and "转" in clause):
                if "取消" in clause:
                    raise ClarificationNeeded("取消已有定时转账请在任务列表取消对应计划")
                amount_text = re.search(r"(?:转(?:账|给)?|再转).*?(\d+(?:\.\d{1,2})?\s*[千万元块]?)", clause)
                matches = [
                    c["name"]
                    for c in facts["contacts"]
                    if any(a in clause for a in [c["name"], c["phone"], *c["aliases"], *c["notes"]])
                ]
                from .dialogue import explicit_transfers
                source_bindings = explicit_transfers(clause, facts)
                if len(source_bindings)==1:
                    matches=[source_bindings[0]['recipient_hint']]
                if len(matches) > 1:
                    raise ClarificationNeeded("此句包含多个收款人，请明确每个人的金额或选择一个收款人")
                hint = matches[0] if matches else None
                if not hint and any(p in clause for p in ["他", "她", "再转"]):
                    hint = context.get("last_recipient")
                if not hint:
                    raise ClarificationNeeded("请指定已验证收款人的姓名、手机号或备注")
                currency = re.search(r"\d+(?:\.\d+)?\s*[千万元块]*[元块]", clause)
                from .dialogue import explicit_transfers
                bindings = [b for b in explicit_transfers(clause, facts) if b["recipient_hint"] == hint]
                amount = bindings[0]["amount_fen"] if len(bindings) == 1 else money(
                    currency[0] if currency else amount_text[1] if amount_text else clause)
                when = None
                clock = datetime.fromisoformat(facts["business_now"])
                if "明天" in clause:
                    when = (clock + timedelta(days=1)).isoformat()
                elif "后天" in clause:
                    when = (clock + timedelta(days=2)).isoformat()
                elif re.search(r"\d{4}-\d{2}-\d{2}", clause):
                    when = re.search(r"\d{4}-\d{2}-\d{2}", clause)[0] + "T09:00:00+08:00"
                add("funds.transfer", {"recipient_hint": hint, "amount_fen": amount}, when)
            elif "AA" in clause.upper():
                request = re.search(r"aa_[a-f0-9]+", clause)
                if request:
                    add(
                        "aa.pay" if any(w in clause for w in ["支付", "付", "结清"]) else "aa.status",
                        {"request_id": request[0]},
                    )
                else:
                    participants = [user_id] + [
                        value for name, value in [("小王", "u_wang"), ("小李", "u_li")] if name in text
                    ]
                    add(
                        "aa.create",
                        {
                            "amount_fen": money(clause),
                            "participants": list(dict.fromkeys(participants)),
                            "subject": "聚餐AA",
                        },
                    )
            elif "生日" in clause:
                match = re.search(r"(\d{1,2})月(\d{1,2})[日号]?", clause)
                event = facts["life_events"][0] if facts["life_events"] else None
                birthday = (
                    f"{facts['business_now'][:4]}-{int(match[1]):02}-{int(match[2]):02}"
                    if match
                    else (event["date"] if event else None)
                )
                if not birthday:
                    raise ClarificationNeeded("请明确生日日期")
                budget = re.search(r"(\d+(?:\.\d{1,2})?)\s*元", clause)
                add("life.goal", {"birthday_date": birthday, "budget_fen": money(budget[0]) if budget else 100000})
            elif any(w in clause for w in ["预留", "锁定"]):
                add("funds.reserve", {"amount_fen": money(clause), "purpose": clause})
            elif "余额" in clause:
                add("balance.query")
            elif any(w in clause for w in ["账单", "消费分类", "年报", "月报"]):
                explicit_period = period_slot(clause, facts["business_now"])
                annual = bool(explicit_period and len(explicit_period) == 4)
                period = facts["business_now"][: 4 if annual else 7]
                match = re.search(r"(\d{4})年", clause)
                if match:
                    period = match[1] if annual else period
                month = re.search(r"(\d{1,2})月", clause)
                if month and not annual:
                    period = facts["business_now"][:4] + f"-{int(month[1]):02}"
                period = explicit_period or period
                add("bills.report", {"period": period, "annual": annual})
            elif "异常" in clause or "重复扣" in clause:
                add("bills.anomalies")
            elif "压力" in clause or "现金流" in clause:
                pct = re.search(r"(\d+)%", clause)
                add("cashflow.project", {"expense_increase_pct": int(pct[1]) if pct else 20})
            elif "风险测评" in clause:
                answers = re.findall(r"[1-5]", clause)
                if len(answers) != 5:
                    raise ClarificationNeeded("模拟测评需给出5个1..5的真实回答，例如风险测评2 2 2 2 2")
                add("wealth.assess", {"answers": list(map(int, answers))})
            elif any(w in clause for w in ["申购", "购买理财", "赎回"]):
                product = re.search(r"P_(?:CASH|STEADY|LOCKED)", clause.upper())
                if not product:
                    named = [p for p in facts["products"] if p["name"] in clause]
                    if len(named) == 1:
                        add(
                            "wealth.redeem" if "赎回" in clause else "wealth.purchase",
                            {"product_id": named[0]["id"], "amount_fen": money(clause)},
                        )
                        continue
                if not product:
                    raise ClarificationNeeded("你想选择哪款理财产品？可以先让我比较可选产品。")
                add(
                    "wealth.redeem" if "赎回" in clause else "wealth.purchase",
                    {"product_id": product[0], "amount_fen": money(clause)},
                )
            elif any(w in clause for w in ["对比", "推荐", "理财产品"]) or ("比较" in clause and "产品" in clause):
                add("wealth.compare")
            elif "持仓" in clause or "收益" in clause:
                add("wealth.holdings")
            elif "申请" in clause and "卡" in clause:
                add("cards.apply", {"kind": "physical" if "实体" in clause else "virtual"})
            elif "解挂" in clause:
                add("cards.restore")
            elif "挂失" in clause:
                add("cards.lost")
            elif "解锁" in clause or ("解除" in clause and "交易限制" in clause):
                add("cards.unlock")
            elif "额度" in clause and "卡" in clause:
                add("cards.limit", {"amount_fen": money(clause)})
            elif "限制" in clause and "卡" in clause:
                add(
                    "cards.restrict",
                    {
                        "single_limit_fen": money(clause),
                        "daily_limit_fen": 100000,
                        "blocked_categories": ["gaming"] if "游戏" in clause else [],
                    },
                )
            elif "卡" in clause:
                add("cards.query")
            elif any(w in clause for w in ["订阅", "代扣", "会员"]):
                contract = "S_VIDEO" if "视频" in clause else "S_MUSIC" if "音乐" in clause else None
                if "取消" in clause:
                    if not contract:
                        raise ClarificationNeeded("请指定视频会员或音乐会员，也可以先查询订阅合同")
                    add("subscriptions.cancel", {"subscription_id": contract})
                elif "提醒" in clause:
                    if not contract:
                        raise ClarificationNeeded("请指定需要提醒的订阅")
                    add("subscriptions.remind", {"subscription_id": contract, "days_before": 2})
                else:
                    add("subscriptions.detect" if "识别" in clause else "subscriptions.query")
            elif "订单" in clause:
                order = re.search(r"order_[a-f0-9]+", clause)
                add("life.cancel_order", {"order_id": order[0]}) if "取消" in clause and order else add("life.orders")
        if not tasks:
            return Intent(summary=text, clarification="请说明业务、金额和业务对象；可使用演示场景卡片")
        return Intent(summary=text, tasks=tasks)


def expand_tasks(intent, business_time=None):
    specs = []
    for index, task in enumerate(intent.tasks):
        if task.action not in DEFINITIONS or DEFINITIONS[task.action][0] != task.domain.value:
            raise BankError("AGENT_OWNERSHIP", "规划器分配的工具与领域不匹配")
        node_id = task.id or f"t{index}"
        spec = NodeSpec(
            id=node_id,
            agent=task.domain.value,
            operation=task.action,
            params=task.params,
            depends_on=task.depends_on,
            scheduled_for=task.scheduled_for,
            reason="用户明确提出的业务意图",
        )
        specs.append(spec)
        if task.action in {"subscriptions.cancel", "subscriptions.remind"}:
            target = spec.params.get("subscription_id", "")
            if target and not target.startswith("$") and not re.fullmatch(r"S_[A-Z0-9_]+", target):
                query_id = f"{node_id}_contract"
                specs.insert(len(specs) - 1, NodeSpec(id=query_id, agent="subscriptions", operation="subscriptions.query",
                    params={"name_hint": target}, depends_on=list(spec.depends_on), reason="经本人确认查询合同后匹配名称"))
                spec.params = {**spec.params, "subscription_id": f"${query_id}.selected.subscription_id",
                               "contract_version": f"${query_id}.selected.contract_version"}
                spec.depends_on = list(dict.fromkeys([*spec.depends_on, query_id]))
        if task.action == "life.goal":
            try:
                day = datetime.fromisoformat(task.params["birthday_date"]).date()
            except (KeyError, ValueError) as error:
                raise ClarificationNeeded("请明确有效的生日日期，例如2026-10-20") from error
            month_start = day.replace(day=1).isoformat() + "T09:00:00+08:00"
            reserve_time = (
                month_start
                if business_time and datetime.fromisoformat(month_start) > datetime.fromisoformat(business_time)
                else None
            )
            specs.extend(
                [
                    NodeSpec(
                        id=f"{node_id}_reserve",
                        agent="funds",
                        operation="funds.reserve",
                        params={
                            "amount_fen": task.params.get("budget_fen", 100000),
                            "purpose": "生日鲜花蛋糕预算",
                            "goal_id": f"${node_id}.goal_id",
                        },
                        depends_on=[node_id],
                        scheduled_for=reserve_time,
                        reason="Life请求Funds在生日当月预留预算",
                    ),
                    NodeSpec(
                        id=f"{node_id}_purchase",
                        agent="life",
                        operation="life.purchase",
                        params={"goal_id": f"${node_id}.goal_id", "card_id": task.params.get("card_id", "C_MAIN")},
                        depends_on=[node_id, f"{node_id}_reserve"],
                        scheduled_for=(day - timedelta(days=2)).isoformat() + "T09:00:00+08:00",
                        reason="生日前两天重新报价并独立确认付款",
                    ),
                ]
            )
    return infer_ordering(specs)


def infer_ordering(specs, immutable=frozenset()):
    # Apply the same business ordering to initial plans and dynamic revisions.
    reserve_ids = [n.id for n in specs if n.operation == "funds.reserve" and n.scheduled_for is None]
    topological(specs)  # Validate user-declared ordering before adding inferred edges.
    nodes = {node.id: node for node in specs}
    for spec in specs:
        if spec.id not in immutable and spec.operation in ["funds.transfer", "wealth.purchase"]:
            for reserve_id in reserve_ids:
                if not precedes(nodes, spec.id, reserve_id):
                    spec.after.append(reserve_id)
    immediate_writes = [n.id for n in specs if DEFINITIONS[n.operation][2] != "GREEN" and n.scheduled_for is None]
    for spec in specs:
        if spec.id not in immutable and spec.operation in ["bills.report", "cashflow.project"]:
            for dependency in immediate_writes:
                if not precedes(nodes, spec.id, dependency):
                    spec.after.append(dependency)
    # Sharing an agent does not make one task's success a prerequisite for another.
    # Serialize same-domain mutations as ordering edges; rejection does not cancel siblings.
    previous_writes = {}
    for spec in topological(specs):
        if DEFINITIONS[spec.operation][2] != "GREEN" and spec.scheduled_for is None:
            previous = previous_writes.get(spec.agent)
            if previous and spec.id not in immutable:
                spec.after.append(previous)
            previous_writes[spec.agent] = spec.id
        spec.after = list(dict.fromkeys(spec.after))
    return topological(specs)


class DomainAgent:
    def __init__(self, role, bank):
        self.role, self.validate_params = role, bank.validate_params

    checks = ("严格参数校验", "本领域工具归属")
    context_fields = ()

    def propose(self, specs, user_id=None):
        owned = []
        for node in specs:
            if node.agent != self.role:
                continue
            if DEFINITIONS[node.operation][0] != self.role:
                raise BankError("AGENT_OWNERSHIP", "领域Agent无权提出其他领域操作")
            # References are resolved and strictly validated again at execution.
            if set(node.params) - PARAM_MODELS[node.operation].model_fields.keys():
                raise BankError("INVALID_TOOL_ARGUMENTS", "业务提议包含工具定义之外的字段")
            if not list(references(node.params)):
                node.params = self.validate_params(node.operation, node.params)
            owned.append(node)
        facts = {
            "responsibility": ROLES[self.role],
            "source": "domain_rule_engine",
            "checks": self.checks,
            "operations": [node.operation for node in owned],
            "context_scope": self.context_fields,
        }
        return AgentProposal(agent=self.role, nodes=owned, facts=[facts])


class FundsAgent(DomainAgent):
    checks = ("唯一已验证收款人", "余额扣除预留", "当日累计限额", "AA整数分守恒")
    context_fields = ("本人账户", "已验证联系人", "本人预算", "AA份额")


class BillsAgent(DomainAgent):
    checks = ("报告覆盖期", "分类与退款口径", "重复扣款需人工核实", "预测收入不计可用现金")
    context_fields = ("本人账本", "登记的未来义务", "用户确认预计收入")


class WealthAgent(DomainAgent):
    checks = ("风险等级和测评有效期", "最低金额", "锁定期", "份额净值", "结算前不计现金")
    context_fields = ("模拟产品披露", "本人测评", "本人持仓", "结算记录")


class CardsAgent(DomainAgent):
    checks = ("卡片归属与状态", "银行额度审批", "类别与交易限额", "挂失和交易解锁区分")
    context_fields = ("本人卡片", "银行审批规则")


class SubscriptionsAgent(DomainAgent):
    checks = ("候选与合同区分", "代扣与商户合同分别处理", "取消不等于退款", "续费提醒去重")
    context_fields = ("历史周期扣费", "授权后查询合同", "取消结果和人工待办")


class LifeAgent(DomainAgent):
    checks = ("用户确认日期地址", "预算交Funds管理", "当期服务端报价", "订单退款证据")
    context_fields = ("用户确认生活事件", "模拟商品目录", "目标和商户订单")


DOMAIN_AGENTS = {
    "funds": FundsAgent,
    "bills": BillsAgent,
    "wealth": WealthAgent,
    "cards": CardsAgent,
    "subscriptions": SubscriptionsAgent,
    "life": LifeAgent,
}


class ReviewerAgent:
    def review(self, specs):
        ordered = topological(specs)
        nodes = {node.id: node for node in specs}
        if len(nodes) != len(specs) or len(nodes) > 64:
            raise BankError("INVALID_DAG", "节点编号重复或计划过大")
        visiting, visited = set(), set()

        def visit(key):
            if key in visiting:
                raise BankError("DAG_CYCLE", "计划依赖存在循环")
            if key not in nodes:
                raise BankError("MISSING_DEPENDENCY", "计划引用不存在的节点")
            if key in visited:
                return
            node = nodes[key]
            if node.operation not in DEFINITIONS or DEFINITIONS[node.operation][0] != node.agent:
                raise BankError("AGENT_OWNERSHIP", "节点工具归属错误")
            visiting.add(key)
            for dependency in [*node.depends_on, *node.after]:
                visit(dependency)
            for dependency, field in references(node.params):
                if dependency not in node.depends_on:
                    raise BankError("UNTRUSTED_REFERENCE", "结果引用必须声明为直接依赖")
            if node.scheduled_for:
                try:
                    when = datetime.fromisoformat(node.scheduled_for)
                except ValueError as error:
                    raise BankError("INVALID_SCHEDULE", "预约时间不是有效的ISO时间") from error
                if when.tzinfo is None:
                    raise BankError("INVALID_TIMEZONE", "预约时间必须包含时区")
            visiting.remove(key)
            visited.add(key)

        for key in nodes:
            visit(key)
        return {
            "passed": True,
            "method": "deterministic_dag_and_tool_validation",
            "checks": ["operation_owner", "acyclic", "references", "timezone", "gateway_rechecks_permission"],
            "agent_count": len({n.agent for n in specs}),
            "node_count": len(specs),
            "topological_order": [node.id for node in ordered],
            "ordering": {node.id: node.after for node in specs if node.after},
        }
