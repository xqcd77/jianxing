"""One request intake shared by HTTP and direct Supervisor callers.

Manifest entries are coverage hints, never permissions or executable tools.
Unrecognized combinations reach the full planner with the original message.
"""
import asyncio
import re

from .conversation import conversation_intent
from .errors import ClarificationNeeded
from .harness import task_context
from .memory import SENSITIVE
from .request_goals import continue_goal_agenda, conversation_task_status
from .request_understanding import understand_request
from .research import UNSAFE
from .research_schemas import ResearchRequest
from .schemas import AdditionalGoal, Intent
from .storage import uid


GOAL_PATTERNS = {
    'transfer': r'(?:给|向).{0,16}(?:转|汇|打款)|转给|转账|汇给|打给',
    'etf': r'ETF|交易型开放式指数基金',
    'wealth': r'理财|模拟.{0,8}产品',
    'research': r'(?:分析|研究|核查|解释).{0,12}(?:账单|收支|流水|消费|现金流)|(?:账单|收支|现金流).{0,12}(?:分析|研究)',
    'membership': r'(?:订|定|开通|购买|买|开).{0,10}会员',
    'card_apply': r'(?:申请|办|开通|订阅).{0,30}(?:卡片|张.{0,24}卡|银行卡|虚拟(?:支付|付款|借记)?卡)',
    'incoming': r'(?:向|往|给).{0,6}(?:我的|本人|自己).{0,8}(?:转入|转账|充)|模拟转入',
}


def request_manifest(message):
    """Keep source clauses before choosing a route; negative requests stay data."""
    clauses = [c.strip() for c in re.split(r'[，,；;。\n]|然后|另外|并且|同时', message) if c.strip()]
    entries = []
    for clause in clauses:
        if re.match(r'^(?:不要|别|不用|不许)', clause):
            continue
        for kind, pattern in GOAL_PATTERNS.items():
            if kind=='wealth' and re.search(r'ETF',clause,re.I) and not re.search(r'模拟.{0,8}产品',clause):
                continue
            matches = list(re.finditer(pattern, clause, re.I))
            if kind == 'transfer':
                matches = [m for m in matches if not re.match(r'(?:向|往|给)(?:我|本人|自己)',m[0])]
            if matches:
                entries.append({'kind': kind, 'source_text': clause[:500]})
    return entries


def ensure_goal_coverage(intent, message):
    """An omitted goal must be visible; this never invents an operation."""
    if intent.reject_reason and intent.rejection_kind=='security':
        return intent
    manifest = request_manifest(message)
    if any(e['kind']=='wealth' for e in manifest) and not re.search(r'ETF|证券|定投',message,re.I):
        intent=intent.model_copy(update={'additional_goals':[g for g in intent.additional_goals
            if not (g.information_query and g.information_query.asset_type=='etf')
            and not (g.kind=='capability_gap' and re.search(r'ETF|证券|自动定投',g.title+' '+g.message,re.I))]})
    if len({entry['kind'] for entry in manifest})<=1 and (intent.reject_reason or intent.answer or intent.information_query):
        return intent
    if intent.reject_reason or intent.answer or intent.information_query:
        extras = list(intent.additional_goals)
        if intent.information_query:
            extras.append(AdditionalGoal(kind='public_information',title='公开资料查询',
                source_text=next((e['source_text'] for e in manifest if e['kind']=='etf'),''),
                information_query=intent.information_query))
        intent = intent.model_copy(update={'reject_reason':None,'answer':None,'information_query':None,'additional_goals':extras})
    actions = {t.action for t in intent.tasks}
    covered = {
        'transfer': 'funds.transfer' in actions,
        'card_apply': 'cards.apply' in actions,
        'etf': any(g.information_query and g.information_query.asset_type == 'etf' for g in intent.additional_goals),
        'wealth': any(a.startswith('wealth.') for a in actions) or any(g.pending_task and g.pending_task.domain=='wealth' for g in intent.additional_goals),
        'research': any(g.kind=='research' for g in intent.additional_goals) or any(a in {'bills.report','bills.anomalies','cashflow.project'} for a in actions),
        'membership': any('会员' in g.source_text or '会员' in g.title for g in intent.additional_goals),
        'incoming': False,
    }
    extras = list(intent.additional_goals)
    for entry in manifest:
        kind, source = entry['kind'], entry['source_text']
        if covered[kind] or any(g.source_text == source for g in extras):
            continue
        # Cancelling an existing subscription is not opening a new membership.
        if kind == 'membership' and any(a.startswith('subscriptions.') for a in actions):
            continue
        if kind=='etf':
            from .information import ground_investment_query
            from .schemas import PublicInformationQuery
            query = ground_investment_query(PublicInformationQuery(topic='fund_products',asset_type='etf'),message,
                sum(t.params.get('amount_fen',0) for t in intent.tasks if t.action=='funds.transfer'))
            extras.append(AdditionalGoal(kind='public_information',title='ETF资料与投资条件',source_text=source,
                message='ETF需求已保留为资料核查；本次未购买或开通定投。',information_query=query))
            covered[kind]=True
            continue
        if kind=='membership':
            from .request_understanding import MEMBERSHIP_BOUNDARY
            extras.append(AdditionalGoal(kind='capability_gap',title='开通会员',source_text=source,message=MEMBERSHIP_BOUNDARY))
            covered[kind]=True
            continue
        if kind=='research':
            extras.append(AdditionalGoal(kind='research',title='收支研究',source_text=source,
                research_query=source,message='研究独立进行，不产生资金授权。'))
            covered[kind]=True
            continue
        extras.append(AdditionalGoal(kind='clarification', title='待核对的需求', source_text=source,
            message='这项需求已单独保留，尚未执行；请补充具体对象或办理条件。'))
        covered[kind] = True
    etf_public = [g for g in extras if g.information_query and g.information_query.asset_type=='etf']
    # A service boundary is a note on a recommendation goal, not an invented
    # extra request to buy. Preserve a separate gap when purchase was requested.
    requested_etf_trade = re.search(r'(?:购买|买|申购|下单)[^，,；;。]{0,12}ETF|(?:开通|设置)[^，,；;。]{0,8}定投',message,re.I)
    if len(etf_public)==1 and not requested_etf_trade:
        redundant = [g for g in extras if g.kind=='capability_gap' and re.search(r'ETF|证券|自动定投',g.title+' '+g.source_text,re.I)]
        for gap in redundant:
            etf_public[0].message = (etf_public[0].message+' '+gap.message).strip()[:600]
        extras = [g for g in extras if g not in redundant]
    return intent.model_copy(update={'additional_goals': extras[:12]})


class RequestIntake:
    def __init__(self, banking, research, supervisor=None, events=None):
        self.banking, self.research, self.supervisor, self.events = banking, research, supervisor, events
        banking.research_service = research

    @task_context
    async def chat(self, user_id, message, conversation_id=None):
        conversation_id = conversation_id or uid('conversation')
        banking = self.banking
        with banking.planner.model.harness.scope(user_id, conversation_id):
            if SENSITIVE.search(message) or UNSAFE.search(message):
                return await banking.astart(user_id, message, conversation_id)
            reconciled = await self.reconcile_result(user_id, message, conversation_id)
            if reconciled:
                return reconciled
            continuation = await continue_goal_agenda(banking, user_id, conversation_id, message)
            if continuation:
                return continuation
            progress = await asyncio.to_thread(conversation_task_status, banking.planner.db, user_id,
                conversation_id, message, banking, self.research)
            if progress:
                await banking._atomic(banking._record_turn, user_id, conversation_id, message, progress)
                return progress
            facts = await asyncio.to_thread(banking.planner.bank.facts, user_id)
            from .budget_explanations import infeasible_bundle, remember_infeasible_bundle
            precheck_answer = infeasible_bundle(message, facts)
            if precheck_answer:
                # This text is computed here from owned current facts, not model
                # prose. Keep the generic model-claim filter intact elsewhere.
                await banking._atomic(remember_infeasible_bundle,banking.planner.db,user_id,conversation_id,message,precheck_answer)
                response={'status':'NEEDS_CLARIFICATION','message':precheck_answer.clarification,
                          'conversation_id':conversation_id,'execution_mode':'sandbox'}
                await banking._atomic(banking._record_turn,user_id,conversation_id,message,response,precheck_answer.model_dump(mode='json'))
                return response
            explicit = await asyncio.to_thread(banking.planner.explicit_intent, user_id, message, facts)
            if explicit:
                return await banking.astart(user_id, message, conversation_id, intent=explicit)
            context = await asyncio.to_thread(banking.planner.context_builder.read,
                user_id, message, conversation_id, facts=facts)
            manifest = request_manifest(message)
            kinds = {g['kind'] for g in manifest}
            if len(kinds) <= 1:
                public = await asyncio.to_thread(banking.public_information_intent, user_id, message, conversation_id)
                answer = conversation_intent(message, banking.planner.settings)
                if public or answer:
                    return await banking.astart(user_id, message, conversation_id, intent=public or answer)
                if self.events:
                    incoming = await self.incoming(user_id, message, conversation_id)
                    if incoming:
                        return incoming
            try:
                understood = understand_request(message, facts, context)
                if understood is None:
                    from .request_goals import split_transfer_information
                    bundle = split_transfer_information(message,facts)
                    if bundle:
                        understood, query, calculation = bundle
                        understood.additional_goals.append(AdditionalGoal(kind='public_information',
                            title='ETF推荐与条件核查' if query.asset_type=='etf' else '公开资料查询',
                            source_text=message[:500],message=calculation or '资料需求已保留，尚未购买。',
                            information_query=query))
            except ClarificationNeeded as error:
                understood = Intent(summary='核对原始金额', clarification=error.message, clarification_kind='amount')
            if understood:
                return await banking.astart(user_id, message, conversation_id,
                    intent=ensure_goal_coverage(understood, message))
            if self.supervisor:
                return await self.supervisor.dispatch(user_id, message, conversation_id)
            if self.research.should_research(message) and not kinds.intersection({'transfer', 'card_apply', 'membership'}):
                return await self.research.astart(user_id, ResearchRequest(query=message, conversation_id=conversation_id))
            return await banking.astart(user_id, message, conversation_id)

    async def reconcile_result(self, user_id, message, conversation_id):
        # Status lookup uses the original submission key, never a new transfer.
        if not re.fullmatch(r'(?:请|帮我|继续|先|再|一下|那笔|这笔|刚才的|原来的|转账|交易|银行|付款|结果|状态|核实|核对|查询|查|是否到账|到账没|了|吗|[，,。；;\s]|不要再转一次|别重复转账)+', message) or not re.search(r'核实|查询|到账没|是否到账', message):
            return None
        from .storage import Document
        with self.banking.planner.db.read() as session:
            dialogue = session.get(Document, f'dialogue_{user_id}_{conversation_id}')
            plan_id = dialogue.data.get('active_plan_id') if dialogue and dialogue.owner_id == user_id else None
        if not plan_id:
            return None
        current = await self.banking._atomic(self.banking.plans.snapshot, user_id, plan_id)
        if not any(n['status'] == 'UNKNOWN' for n in current['nodes']):
            return None
        response = await self.banking.amutate(user_id, plan_id, 'reconcile')
        await self.banking._atomic(self.banking._record_turn, user_id, conversation_id, message, response)
        return response

    async def incoming(self, user_id, message, conversation_id):
        incoming = await asyncio.to_thread(
            self.events.recognize_incoming, user_id, conversation_id, message
        )
        if incoming:
            if "outgoing_hint" in incoming:
                params = {}
                if incoming["outgoing_hint"]:
                    params["recipient_hint"] = incoming["outgoing_hint"]
                if incoming["amount_fen"] is not None:
                    params["amount_fen"] = incoming["amount_fen"]
                intent = Intent(
                    summary="转账对象澄清",
                    tasks=[{"domain": "funds", "action": "funds.transfer", "params": params}],
                )
                return await self.banking.astart(user_id, message, conversation_id, intent=intent)
            if incoming.get("guidance"):
                messages = {
                    "own_account_transfer": "这属于你本人不同账户之间的转账。当前版本尚未连接其他银行卡，请在转出银行核对转出账户和本人收款账户后办理；不会把这笔钱直接增加到当前余额。",
                    "cash_deposit": "你想办理的是现金存款。当前版本无法接收现金或连接存款设备，请通过银行支持的存款渠道办理；实际到账后再安排转账。",
                    "foreign_currency": "当前演示账户只支持人民币，不能把外币金额直接当成人民币入账。请通过支持该币种的银行账户办理，或先说明你要转入的人民币金额。",
                    "specified_account": "当前演示只连接默认本人账户，无法核实你指定尾号的银行卡。请先核对收款银行卡；若要向当前演示账户转入，可以明确说“向我的账户转入”。",
                    "incoming_howto": "向自己的账户转入资金，需要在转出银行核对本人收款账户后发起转账。当前版本未连接外部银行卡；在演示账户中，你可以说“向我的账户转入500元”，核对后确认模拟到账。",
                    "funds_already_sufficient": "当前可用余额已足够支付刚才的转账，无需再补差额。你可以点击继续办理，核对详情后确认转出。",
                }
                return {
                    "status": "INCOMING_GUIDANCE",
                    "message": messages[incoming["guidance"]],
                    "conversation_id": conversation_id,
                    "execution_mode": "sandbox",
                }
            if incoming.get("scheduled"):
                return {
                    "status": "INCOMING_GUIDANCE",
                    "message": "预约转入需要由转出银行支持。当前版本尚未连接外部银行卡，请在转出银行设置预约，到账后再安排这笔资金。",
                    "conversation_id": conversation_id,
                    "execution_mode": "sandbox",
                }
            if incoming.get("invalid_amount"):
                return {
                    "status": "NEEDS_CLARIFICATION",
                    "message": "请输入大于零的金额，最多保留两位小数；演示单次转入上限为100万元。",
                    "conversation_id": conversation_id,
                    "execution_mode": "sandbox",
                }
            response = await asyncio.to_thread(
                self.events.prepare_incoming,
                user_id,
                incoming["amount_fen"],
                conversation_id,
                clarify_destination=incoming.get("clarify_destination", False),
            )
            await asyncio.to_thread(
                self.banking._record_turn, user_id, response["conversation_id"], message, response
            )
            return response
        return None
