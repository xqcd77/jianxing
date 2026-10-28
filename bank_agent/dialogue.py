"""Ground explicit transfer slots and keep business context across turns.

This module has no execution or authorization capability. Model plans still go
through the domain agents, reviewer and the independent bank gateway.
"""

from datetime import datetime, timedelta, timezone
from contextlib import nullcontext
from decimal import Decimal
import re

from .errors import ClarificationNeeded
from .schemas import DomainTask, Intent
from .storage import Document, Node, Plan, audit, save_document
from sqlalchemy import select
from .operations import DEFINITIONS


def reconcile_missing_fields(intent):
    """Missing information is derived from schemas, never solely model wording."""
    if intent.reject_reason or not intent.tasks:
        return intent
    kinds = {
        "recipient_hint": "recipient",
        "product_id": "product",
        "amount_fen": "amount",
        "budget_fen": "amount",
        "date": "date",
        "birthday_date": "date",
        "answers": "risk_answers",
    }
    for task in intent.tasks:
        if task.action not in DEFINITIONS:
            return intent
        for key, (_, default) in DEFINITIONS[task.action][1].items():
            if default is ... and (key not in task.params or task.params[key] in [None, ""]):
                return intent.model_copy(
                    update={"clarification": "请补充办理所需的信息。", "clarification_kind": kinds.get(key, "other")}
                )
    if intent.clarification_kind in ["recipient", "amount", "product"]:
        field = {"recipient": "recipient_hint", "amount": "amount_fen", "product": "product_id"}[
            intent.clarification_kind
        ]
        if any(field in task.params for task in intent.tasks):
            return intent.model_copy(update={"clarification": None, "clarification_kind": "other"})
    return intent


def reconcile_bill_period(intent, message, now):
    """A source-confirmed report period is not a missing appointment date.

    Only clear a contradictory date flag for one complete, immediate read.
    Ambiguous periods, scheduled requests and other goals retain clarification.
    """
    if (not intent.clarification or intent.clarification_kind!='date'
            or intent.reject_reason or intent.additional_goals or len(intent.tasks)!=1):
        return intent
    task=intent.tasks[0]
    if (task.action!='bills.report' or task.scheduled_for or task.depends_on
            or re.search(r'如果|假如|还是|或者|预约|定时|明天|后天|下周|每月|到时候|分别|[和及与、]\s*\d{1,2}月',message)
            or len(set(re.findall(r'20\d{2}(?:年\s*\d{1,2}月|-\d{2}|年)?',message)))!=1
            or len(set(re.findall(r'20\d{2}',message)))!=1):
        return intent
    from .operations import PARAM_MODELS
    from pydantic import ValidationError
    try:
        period=period_slot(message,now)
        params=PARAM_MODELS[task.action].model_validate(task.params)
    except (ValidationError,ClarificationNeeded):
        return intent
    if not period or params.period!=period or params.annual!=(len(period)==4):
        return intent
    return intent.model_copy(update={'clarification':None,'clarification_kind':'other'})


def partition_incomplete_tasks(intent,message=''):
    """A missing slot pauses its goal and dependants, never independent siblings."""
    from .schemas import AdditionalGoal
    if intent.reject_reason or not intent.tasks:
        return intent
    tasks=[t.model_copy(update={'id':t.id or f't{i}'},deep=True) for i,t in enumerate(intent.tasks)]
    task_ids={t.id for t in tasks}
    missing={t.id for t in tasks if t.action in DEFINITIONS and any(default is ...
        and (key not in t.params or t.params[key] in [None,'']) for key,(_,default) in DEFINITIONS[t.action][1].items())}
    missing|={t.id for t in tasks if set(t.depends_on)-task_ids}
    if not missing:
        if intent.clarification_kind in {'product','risk_answers','recipient','amount'} and any(g.kind=='bank_task' for g in intent.additional_goals):
            return intent.model_copy(update={'clarification':None,'clarification_kind':'other'})
        return intent
    blocked=set(missing)
    while True:
        enlarged=blocked|{t.id for t in tasks if set(t.depends_on)&blocked}
        if enlarged==blocked:
            break
        blocked=enlarged
    ready=[t for t in tasks if t.id not in blocked]
    if not ready:
        return intent
    extras=list(intent.additional_goals)
    for task in tasks:
        if task.id in blocked:
            title={'wealth.purchase':'理财申购待选产品','funds.transfer':'转账待补充信息'}.get(task.action,'待补充的办理需求')
            extras.append(AdditionalGoal(kind='bank_task',title=title,pending_task=task,
                source_text=(message or intent.summary)[:500],message='这项目标等待补充必填信息，其他独立步骤可先核对办理。'))
    if any(t.action=='wealth.purchase' and t.id in missing and not t.params.get('product_id') for t in tasks) and re.search(r'推荐|比较|对比|建议',message) and not any(t.action=='wealth.compare' for t in ready):
        ready.append(DomainTask(id='goal_compare',domain='wealth',action='wealth.compare'))
    return intent.model_copy(update={'tasks':ready,'additional_goals':extras,'clarification':None,'clarification_kind':'other'})


def period_slot(text, now):
    month = re.search(r"(20\d{2})年\s*(\d{1,2})月|(?<!\d)(20\d{2})-(\d{2})(?!-\d)", text)
    if month:
        year, number = (month[1], month[2]) if month[1] else (month[3], month[4])
        if not 1 <= int(number) <= 12:
            raise ClarificationNeeded("账单月份应为1至12月，请核对你要查询的月份。")
        return f"{year}-{int(number):02}"
    if any(word in text for word in ("年度", "全年", "年报")):
        year = re.search(r"20\d{2}", text)
        return year[0] if year else now[:4]
    return None


def money_slot(text):
    # Remove only valid thousands separators; commas between tasks remain.
    text = re.sub(r"(?<=\d)[,，](?=\d{3}(?:\D|$))", "", text)
    numeric = re.search(r"(?<![A-Za-z\d.])([-−－]?\d+(?:\.\d+)?)\s*(万|千)?\s*(?:元|块)", text)
    numeric = numeric or re.fullmatch(
        r"\s*(?:我说的是|金额是|就是|对[，,]?|改成|改为)?\s*(-?\d+(?:\.\d+)?)\s*(万|千)?\s*(?:元|块)?[啊呀吧呢！!。\s]*",
        text,
    )
    if numeric:
        value = (
            Decimal(numeric[1].replace("−", "-").replace("－", "-"))
            * {None: 1, "千": 1000, "万": 10000}[numeric[2]]
            * 100
        )
    else:
        if re.search(r"[零〇一二两三四五六七八九十百千万]+点[零〇一二两三四五六七八九]+", text):
            raise ClarificationNeeded("请用数字填写含小数的金额，例如3.5万元。")
        chinese = re.search(r"([零〇一二两三四五六七八九十百千万]+)\s*(?:元|块)", text)
        chinese = chinese or re.fullmatch(
            r"\s*(?:我说的是|金额是|就是|改成|改为)?\s*([零〇一二两三四五六七八九十百千万]+)\s*(?:元|块)?[啊呀吧呢！!。\s]*",
            text,
        )
        if not chinese:
            return None
        digits = {ch: n for n, ch in enumerate("零一二三四五六七八九")}
        digits.update({"〇": 0, "两": 2})
        total = section = digit = 0
        for ch in chinese[1]:
            if ch in digits:
                digit = digits[ch]
            elif ch == "万":
                total += (section + digit) * 10000
                section = digit = 0
            else:
                section += (digit or 1) * {"十": 10, "百": 100, "千": 1000}[ch]
                digit = 0
        value = Decimal(total + section + digit) * 100
    if value != value.to_integral_value():
        raise ClarificationNeeded("金额最多保留两位小数，请核对你要转账的金额。")
    if not 0 < value <= 100_000_000:
        raise ClarificationNeeded("转账金额需大于零；当前演示单笔上限为100万元。")
    return int(value)


def amount_answer(text):
    if re.fullmatch(r"\s*改(?:成|为)\s*[\d.零〇一二两三四五六七八九十百千万]+\s*[元块][，,\s]*不是\s*[\d.零〇一二两三四五六七八九十百千万]+\s*[元块]?[。！!\s]*", text):
        return True
    return bool(
        re.fullmatch(
            r"\s*(?:我说的是|金额是|就是|对[，,]?|改成|改为)?\s*[-\d.零〇一二两三四五六七八九十百千万,，]+\s*[万千]?\s*[元块]?[啊呀吧呢！!。\s]*",
            text,
        )
    )


def is_followup(text, facts, draft=None):
    text = text.strip()
    if amount_answer(text) or re.fullmatch(r"(?:改成|改为)?\d{4}-\d{2}-\d{2}", text):
        return True
    for contact in facts["contacts"]:
        if text in [contact["name"], contact["phone"], *contact["aliases"], *contact["notes"]]:
            return True
        if draft and draft.get('clarification_kind') == 'recipient' and contact.get('verified'):
            supplied = re.sub(r'^(?:给|向|收款人是)\s*', '', text).strip(' 。')
            if supplied in [contact['name'], contact['phone'], *contact['aliases'], *contact['notes']]:
                return True
    if any(text in [p["name"], p["id"], "就" + p["name"], "选" + p["name"]] for p in facts["products"]):
        return True
    return bool(draft and text in ["明天", "后天", "主卡", "虚拟卡", "实体卡", "银行代扣", "商户合同", "两者都取消"])


def explicit_transfers(message, facts):
    """Bind a verified recipient and the amount immediately following its action.

    Spans refer to the original text so a coordinator can retain other goals.
    A balance, total budget or contribution elsewhere is never a transfer slot.
    """
    if re.search(r"如果|假如|要是|是否|(?:不|别|无需|不要).{0,5}(?:转|汇|打)|"
                 r"上次|昨天|已经|刚刚|转了|转过|撤回|取消转", message):
        return []
    labels = {}
    for contact in facts["contacts"]:
        if contact.get("verified"):
            for label in dict.fromkeys([contact["name"], *contact.get("aliases", []), *contact.get("notes", [])]):
                labels.setdefault(label, []).append(contact)
    if not labels:
        return []
    recipients = "|".join(re.escape(label) for label in sorted(labels, key=len, reverse=True))
    numeric = r"[-−－]?(?:\d{1,3}(?:[,，]\d{3})+|\d+)(?:\.\d+)?\s*[万千]?"
    chinese = r"[零〇一二两三四五六七八九十百千万]+"
    pattern = re.compile(
        rf"(?:给|向)\s*(?:我的)?(?P<recipient>{recipients})\s*(?:再)?"
        rf"(?:转(?:账)?|汇(?:款)?|打(?:款|钱))\s*(?:人民币)?\s*"
        rf"(?P<amount>(?:{numeric}|{chinese})\s*[元块]?)(?![\d.万千零〇一二两三四五六七八九十百])"
    )
    result = []
    for match in pattern.finditer(message):
        contacts = labels[match["recipient"]]
        if len(contacts) != 1:
            continue
        amount = money_slot(match["amount"].strip().rstrip("元块").strip() + "元")
        name = contacts[0]['name']
        # Do not collapse a verified unique alias to an ambiguous legal name.
        hint = match['recipient'] if sum(c.get('verified') and c['name']==name for c in facts['contacts'])>1 else name
        result.append({"recipient_hint": hint, "amount_fen": amount,
                       "start": match.start(), "end": match.end(), "text": match[0].strip()})
    return result


def transfer_grounding(message, facts, context):
    """Single explicit transfers only; do not rewrite compound or conditional tasks."""
    draft = context.get("pending_draft")
    text = message.strip()
    original = draft.get("request", "") if draft and is_followup(text, facts, draft) else text
    normalized = re.sub(r"(?<=\d)[,，](?=\d{3}(?:\D|$))", "", original)
    if re.search(
        r"[，,；;。\n]|然后|并且|同时|另外|如果|假如|是否|怎么|如何|不想|不要|取消|撤回|跳过|忽略|"
        r"验证码|密码|理财|生日|AA|推荐|ETF|基金|定投|剩下|剩余|余下|其余|比较|对比|分析|预留",
        normalized,
        re.I,
    ):
        return None
    if re.search(
        r"下周|下个月|过几天|到时候|周[一二三四五六日天]|每月|之前|刚才|已经|刚刚|上次|昨天|前天|转了|转过|到账没|收到了吗",
        normalized,
    ):
        return None
    if len(re.findall(r"(?:\d+(?:\.\d+)?)\s*(?:万|千)?\s*(?:元|块)", normalized)) > 1:
        return None
    if not (
        re.search(r"给.+?转(?:账)?", normalized)
        or re.search(r"转(?:账)?给", normalized)
        or re.match(r"(?:明天|后天)?(?:请|帮我|我要|我想)?转账", normalized)
    ):
        return None
    if "转入" in normalized or re.search(r"(?:不|别|无需).{0,5}转", normalized):
        return None
    existing = draft.get("intent", {}).get("tasks", []) if draft else []
    params = dict(existing[0]["params"]) if len(existing) == 1 and existing[0]["action"] == "funds.transfer" else {}
    # Only user/fact-grounded recipient and amount are retained. No model flags
    # or fabricated params may enter the independent gateway.
    params = {key: value for key, value in params.items() if key in ["recipient_hint", "amount_fen", "subject"]}
    contact_text = text if draft and is_followup(text, facts, draft) and not amount_answer(text) else normalized
    recipient = contact_text.strip()
    if not (draft and is_followup(text, facts, draft) and not amount_answer(text)):
        target = re.search(r"给\s*(.+?)\s*转(?:账)?", normalized)
        target = target or re.search(r"转(?:账)?给\s*(.+?)\s*(?=\d+(?:\.\d+)?(?:万|千)?[元块]|$)", normalized)
        recipient = target[1].strip() if target else ""
    recipient = re.sub(r"^我(?:的)?", "", recipient).removesuffix("再").strip()
    matches = [
        c
        for c in facts["contacts"]
        if c.get("verified") and recipient in [c["name"], c["phone"], *c["aliases"], *c["notes"]]
    ]
    if len(matches) == 1:
        name=matches[0]['name']
        params["recipient_hint"] = recipient if sum(c.get('verified') and c['name']==name for c in facts['contacts'])>1 else name
    elif len(matches) > 1:
        params.pop("recipient_hint", None)
    elif recipient in ["他", "她"] and context.get("last_recipient"):
        params["recipient_hint"] = context["last_recipient"]
    elif recipient:
        params.pop("recipient_hint", None)
    bound = explicit_transfers(normalized, facts)
    explicit_amount = bound[0]["amount_fen"] if len(bound) == 1 else None
    amount = (
        (explicit_amount if explicit_amount is not None and not (draft and amount_answer(text))
         else money_slot(text if draft and amount_answer(text) else normalized))
        if not draft or amount_answer(text) or "amount_fen" not in params
        else params["amount_fen"]
    )
    if amount is None:
        bare = re.search(r"转(?:账)?\s*(\d+(?:\.\d+)?(?:万|千)?)(?=$|[元块啊吧呀\s])", normalized)
        bare = bare or re.search(r"转(?:账)?\s*([零〇一二两三四五六七八九十百千万]+)(?!点)", normalized)
        amount = money_slot(bare[1] + "元") if bare else None
    if amount is not None:
        params["amount_fen"] = amount
    params.setdefault("subject", "转账")
    missing = "recipient" if not params.get("recipient_hint") else "amount" if "amount_fen" not in params else None
    when = existing[0].get("scheduled_for") if len(existing) == 1 else None
    clock = datetime.fromisoformat(facts["business_now"])
    if "明天" in normalized:
        when = (clock + timedelta(days=1)).isoformat()
    elif "后天" in normalized:
        when = (clock + timedelta(days=2)).isoformat()
    elif date_match := re.search(r"\d{4}-\d{2}-\d{2}", normalized):
        when = date_match[0] + "T09:00:00+08:00"
    # Detailed time phrases are left to the planner, never silently defaulted.
    if re.search(r"上午|下午|晚上|\d+[点时]|\d{1,2}:\d{2}", normalized):
        return None
    return Intent(
        summary="本人账户转账安排",
        tasks=[DomainTask(domain="funds", action="funds.transfer", params=params, scheduled_for=when)],
        clarification="请补充收款人。" if missing == "recipient" else "请补充转账金额。" if missing else None,
        clarification_kind=missing or "other",
    )


def read_context(db, user_id, conversation_id, message, facts):
    context = {}
    if not conversation_id:
        return context
    with db.read() as session:
        memory = session.get(Document, f"memory_{user_id}_{conversation_id}")
        if memory and memory.owner_id == user_id:
            context.update(memory.data)
        context.pop('active_plan', None)
        dialogue = session.get(Document, f"dialogue_{user_id}_{conversation_id}")
        if dialogue and dialogue.owner_id == user_id:
            context.update(dialogue.data)
            context.pop('active_plan', None)
            plan = session.get(Plan, context.get("active_plan_id", ""))
            if plan and plan.user_id == user_id and plan.conversation_id == conversation_id:
                context["active_plan"] = {
                    "id": plan.id,
                    "status": plan.status,
                    "version": plan.version,
                    "nodes": [
                        {
                            "id": node.id,
                            "operation": node.operation,
                            "params": node.params,
                            "status": node.status,
                            "depends_on": node.depends_on,
                            "scheduled_for": node.scheduled_for,
                            "result": node.result,
                        }
                        for node in session.scalars(select(Node).where(Node.plan_id == plan.id))
                    ],
                    "protected": (plan.review or {}).get('protected', {}),
                }
        for derived in ('request_goals', 'goal_revision', 'pending_request', 'pending_draft', 'task_brief'):
            context.pop(derived, None)
        agenda = session.get(Document, f'goal_agenda_{user_id}_{conversation_id}')
        if (agenda and agenda.owner_id == user_id and agenda.kind == 'goal_agenda'
                and agenda.data.get('conversation_id') == conversation_id):
            # The agenda can be newer than the dialogue pointer after an edit
            # or partial completion. Follow its owned plan before reading slots.
            if agenda.data.get('plan_id') and agenda.data['plan_id'] != context.get('active_plan', {}).get('id'):
                current = session.get(Plan, agenda.data['plan_id'])
                if not current or current.user_id != user_id or current.conversation_id != conversation_id:
                    from .errors import BankError
                    raise BankError('TASK_CONTEXT_SCOPE_INVALID', '任务来源不一致，请重新核对原办理计划', 409)
                context['active_plan'] = {
                    'id': current.id, 'status': current.status, 'version': current.version,
                    'protected': (current.review or {}).get('protected', {}),
                    'nodes': [{'id': n.id, 'operation': n.operation, 'params': n.params, 'status': n.status,
                               'depends_on': n.depends_on, 'scheduled_for': n.scheduled_for, 'result': n.result,
                               'reason': n.reason}
                              for n in session.scalars(select(Node).where(Node.plan_id == current.id))],
                }
            context['goal_revision'] = agenda.data.get('revision', 1)
            context['request_goals'] = [{k:g[k] for k in ('id','kind','title','status','node_id','query','notice','source_text',
                'operation','params','pending_task','revision','research_query','slots') if k in g}
                                        for g in agenda.data['goals']]
            for goal in context['request_goals']:
                if goal['kind'] == 'research':
                    original = next(g for g in agenda.data['goals'] if g['id'] == goal['id'])
                    research_id = (original.get('result') or {}).get('research_id') or (original.get('result') or {}).get('id')
                    research = session.get(Document, research_id) if research_id else None
                    if (research and research.owner_id == user_id and research.kind == 'research'
                            and research.data.get('conversation_id') == conversation_id and goal['status'] != 'CANCELLED'):
                        goal['research_id'] = research.id
                        goal['status'] = research.data['status']
                match = next((n for n in context.get('active_plan',{}).get('nodes',[]) if n['id']==goal.get('node_id')),None)
                if match:
                    goal['status'] = match['status']
                elif goal['kind'] in {'public_information','bank_task'} and goal['status']!='CANCELLED' and agenda.data.get('plan_id') and context.get('active_plan',{}).get('id') == agenda.data.get('plan_id'):
                    version = agenda.data.get('goal_plan_versions',{}).get(goal['id'],agenda.data.get('plan_version'))
                    if context['active_plan']['version'] != version:
                        goal['status'] = 'NEEDS_RECHECK'
        draft = session.get(Document, f"draft_{user_id}_{conversation_id}")
        if (
            draft
            and draft.owner_id == user_id
            and draft.data.get("expires_at", "9999") > datetime.now(timezone.utc).isoformat()
            and is_followup(message, facts, draft.data)
        ):
            context["pending_request"] = draft.data["request"]
            context["pending_draft"] = draft.data
    return context


def record_turn(db, user_id, conversation_id, message, response, intent=None, *, session=None):
    with (nullcontext(session) if session is not None else db.write()) as session:
        key = f"dialogue_{user_id}_{conversation_id}"
        previous = session.get(Document, key)
        data = previous.data if previous else {}
        background_research = response.get("research_id") and not response.get("id")
        if background_research and (data.get("active_request_kind") != "research" or
                                    data.get("active_research_id") != response["research_id"]):
            return  # Its persisted report remains available, without replacing a newer request's context.
        turns = data.get("turns", []) + [
            {"role": "user", "content": message},
            {"role": "assistant", "content": response.get("assistant_message", response.get("message", ""))},
        ]
        value = {**data, "turns": turns[-12:]}
        research_id = response.get("research_id") or (
            response.get("id") if response.get("status", "").startswith("RESEARCH_") else None
        )
        if research_id:
            # A research id is never a plan id. A late background completion
            # must not select itself over a newer research request.
            if response.get("status") == "RESEARCH_RUNNING" or not value.get("active_research_id"):
                value["active_research_id"] = research_id
            if response.get("id") == research_id:
                value["active_request_kind"] = "research"
        elif response.get("id") and response.get("nodes"):
            value["active_plan_id"] = response["id"]
            value["active_request_kind"] = "plan"
        if intent and intent.get("information_query"):
            value["last_information_query"] = intent["information_query"]
            value["last_information_query_at"] = datetime.now(timezone.utc).isoformat()
            value["last_information_response"] = response
            value["active_request_kind"] = "public_information"
        elif response.get("status") != "ANSWERED":
            value.pop("last_information_query", None)
            value.pop("last_information_query_at", None)
        if intent and not intent.get("answer") and not intent.get("information_query"):
            value["last_intent"] = intent
        save_document(session, key, "dialogue", user_id, value)
        audit(
            session,
            user_id,
            "DIALOGUE_UPDATED",
            {"conversation_id": conversation_id, "status": response["status"], "plan_id": response.get("id", "")},
        )
