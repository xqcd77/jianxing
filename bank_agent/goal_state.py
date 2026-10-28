"""Versioned, owner-scoped goal changes. Semantic recall is never task state."""
import asyncio
from copy import deepcopy
import re
from typing import Literal

from pydantic import Field
from pydantic import ValidationError

from .dialogue import money_slot, period_slot
from .errors import BankError, ClarificationNeeded
from .schemas import PlanEdit, PublicInformationQuery, StrictModel
from .storage import Document


class GoalPatch(StrictModel):
    target_id: str = Field(min_length=1, max_length=180)
    action: Literal['update', 'cancel'] = 'update'
    fields: dict = Field(default_factory=dict)
    expected_revision: int = Field(ge=1)


def read_agenda(banking, user_id, conversation_id):
    with banking.planner.db.read() as session:
        doc = session.get(Document, f'goal_agenda_{user_id}_{conversation_id}')
        if not doc or doc.kind != 'goal_agenda' or doc.owner_id != user_id:
            return None
        data = deepcopy(doc.data)
    if data.get('plan_id'):
        plan = banking.plans.snapshot(user_id, data['plan_id'])
        data['goals'] = plan.get('request_goals', data['goals'])
        data['plan_version'] = plan['version']
    return data


def _reply(banking, user_id, data, notice=''):
    if data.get('plan_id'):
        result = banking.plans.snapshot(user_id, data['plan_id'])
    else:
        result = {'status': 'ANSWERED', 'conversation_id': data['conversation_id'], 'execution_mode': 'sandbox',
                  'request_goals': data['goals'], 'message': notice or '已更新待处理需求，没有办理资金操作。'}
    result['goal_revision'] = data.get('revision', 1)
    if notice and result.get('nodes'):
        result['assistant_message'] += '\n' + notice
    return result


async def apply_goal_patch(banking, user_id, conversation_id, patch):
    from .request_goals import persist_goal_agenda
    async with banking.process_locks.ahold(f'goal:{user_id}:{conversation_id}'):
        data = await asyncio.to_thread(read_agenda, banking, user_id, conversation_id)
        if not data:
            raise BankError('GOAL_NOT_FOUND', '这项需求不存在或无权访问', 404)
        if data.get('revision', 1) != patch.expected_revision:
            raise BankError('GOAL_VERSION_CONFLICT', '需求已更新，请刷新后重新修改', 409)
        target = next((g for g in data['goals'] if g['id'] == patch.target_id), None)
        if not target:
            raise BankError('GOAL_NOT_FOUND', '这项需求不存在或无权访问', 404)
        from .bank_rules import DEFINITIONS
        repeat_read = (target['status'] == 'SUCCEEDED' and patch.action == 'update'
            and DEFINITIONS.get(target.get('operation'), (None, None, None))[2] == 'GREEN')
        if target['status'] in {'SUCCEEDED', 'CANCELLED', 'FAILED', 'COMPENSATED'} and not repeat_read:
            raise BankError('GOAL_NOT_EDITABLE', '这项需求已结束，不能修改或再次办理', 409)
        notice = ''
        if target['kind'] == 'bank_operation':
            plan = await asyncio.to_thread(banking.plans.snapshot, user_id, data['plan_id'])
            node = next(n for n in plan['nodes'] if n['id'] == target['node_id'])
            if patch.action == 'cancel':
                result = await banking.amutate(user_id, plan['id'], 'cancel-node',
                    {'node_id': node['id'], 'expected_version': plan['version']})
            else:
                from .bank_rules import PARAM_MODELS
                server_fields = {'quote','assistance_case_id','assistance_version'}
                allowed = set(PARAM_MODELS[node['operation']].model_fields)-server_fields
                if node['operation']=='funds.transfer':
                    allowed.add('scheduled_for')
                if not patch.fields or set(patch.fields) - allowed:
                    raise BankError('GOAL_PATCH_INVALID', '该需求暂不支持这些字段，请在办理卡片核对修改')
                fields = {k:v for k,v in patch.fields.items() if k!='scheduled_for'}
                schedule = {'scheduled_for':patch.fields['scheduled_for']} if 'scheduled_for' in patch.fields else {}
                if repeat_read:
                    return await _repeat_query(banking, user_id, conversation_id, plan, node, fields)
                result = await banking.amutate(user_id, plan['id'], 'edit', PlanEdit(node_id=node['id'],
                    params={**node['params'], **fields}, expected_version=plan['version'],**schedule))
            # The plan is already committed and its grants invalidated. Agenda
            # stores only links; it never re-submits the original banking tasks.
            with banking.planner.db.write() as session:
                for key in (f'goal_agenda_{user_id}_{conversation_id}', 'request_goals_' + plan['id']):
                    doc = session.get(Document, key)
                    if doc and doc.owner_id == user_id:
                        doc.data = {**doc.data, 'revision': patch.expected_revision + 1}
            result['goal_revision'] = patch.expected_revision + 1
            from .advisory_budget import refresh_derived_information
            return await refresh_derived_information(banking, user_id, conversation_id, result)
        if patch.action == 'cancel':
            target.update(status='CANCELLED', notice='已取消这项目标，没有办理或扣费。')
        elif target['kind']=='bank_task':
            from .bank_rules import PARAM_MODELS
            from .schemas import DomainTask
            task=DomainTask.model_validate(target['pending_task'])
            allowed=set(PARAM_MODELS[task.action].model_fields)-{'quote','confirmed','assistance_case_id','assistance_version'}
            if not patch.fields or set(patch.fields)-allowed:
                raise BankError('GOAL_PATCH_INVALID','该办理目标包含不支持的修改字段')
            task.params={**task.params,**patch.fields}
            try:
                PARAM_MODELS[task.action].model_validate(task.params)
            except ValidationError as error:
                if any(e['type']!='missing' for e in error.errors()):
                    raise BankError('GOAL_PATCH_INVALID','请核对填写的字段与金额') from error
            target.update(pending_task=task.model_dump(mode='json'),params=task.params,status='NEEDS_INPUT',
                          notice='已更新这项待办的字段，请补齐其余条件后继续；其他目标保持原安排。')
            if 'amount_fen' in patch.fields:
                target['slots']={**(target.get('slots') or {}),'amount_source':'explicit_update'}
        elif target['kind'] == 'public_information':
            allowed = {'budget_fen', 'periodic_amount_fen', 'investment_horizon_months', 'risk_level', 'channel'}
            if not patch.fields or set(patch.fields) - allowed:
                raise BankError('GOAL_PATCH_INVALID', '资料需求包含不支持的修改字段')
            try:
                query = PublicInformationQuery.model_validate({**target['query'], **patch.fields})
            except ValidationError as error:
                raise BankError('GOAL_PATCH_INVALID', '请核对预算、定投金额和投资条件的范围') from error
            target['query'] = query.model_dump(mode='json')
            if 'budget_fen' in patch.fields:
                target.pop('budget_rule', None)  # A new explicit amount replaces the former formula.
                target['notice'] = '已按本轮明确预算重新核查资料；尚未购买。'
            result = await banking.information.answer(query)
            target.update(result=result, status='UNAVAILABLE' if result.get('source_status') == 'unavailable'
                          else 'NEEDS_INPUT' if result.get('suitability') == 'needs_verification' else 'ANSWERED')
        else:
            if set(patch.fields) - {'membership_type', 'billing_period'} or not patch.fields:
                raise BankError('GOAL_PATCH_INVALID', '请补充会员类型或付费周期')
            slots = {**target.get('slots', {}), **patch.fields}
            if slots.get('membership_type') not in {'视频', '音乐'} or slots.get('billing_period', 'monthly') not in {'monthly', 'annual'}:
                raise BankError('GOAL_PATCH_INVALID', '请核对会员类型和付费周期')
            target.update(slots=slots, title=slots['membership_type'] + '会员开通',
                notice='已补充会员选择；当前尚未接入商户会员开通接口，本次没有开通或扣费。')
        target['revision'] = target.get('revision', 1) + 1
        response = (await asyncio.to_thread(banking.plans.snapshot, user_id, data['plan_id'])
                    if data.get('plan_id') else {'conversation_id': conversation_id})
        extras = [g for g in data['goals'] if g['kind'] != 'bank_operation']
        await banking._atomic(persist_goal_agenda, banking.planner.db, user_id, conversation_id,
            response, extras, [target['id']], patch.expected_revision)
        fresh = await asyncio.to_thread(read_agenda, banking, user_id, conversation_id)
        return await asyncio.to_thread(_reply, banking, user_id, fresh, notice)


def parse_goal_patch(message, data, facts=None):
    """Resolve a target conservatively, then parse only explicitly stated slots."""
    # A constraint on a consultation (e.g. "不要申购") is not cancellation
    # of the information goal. Explicit budget edits still work alongside it.
    # Locate the clause that actually changes a goal, rather than a preceding
    # "不是取消" / "已完成转账保持" clause or trailing unchanged sibling.
    clauses = re.split(r'[，,；;。\n]', message)
    edits = [c for c in clauses if re.search(r'改|换成|换给|调整|少花|少转|减少|降低|减去', c)
             and not re.search(r'不(?:要|用)?改|保持不变', c)]
    primary = edits[0] if len(edits) == 1 else clauses[0]
    if len(edits) > 1:
        raise ClarificationNeeded('请逐项修改，明确每一项目标与新的字段，避免混用金额或收款人。')
    change_text = re.sub(r'(?:不要|不用|别)(?:申购|购买|下单|转账|付款|办理|操作)', '', message)
    change = re.search(r'改|换成|换给|调整|少花|少转|减少|降低|减去|取消|先别|不要|不用|会员.*(?:选|要|是)', change_text)
    if not change and not re.fullmatch(r'\s*(?:视频|音乐)(?:的|会员)?(?:[，,\s]*(?:按月付|包月|按年付|包年))?[。\s]*', message):
        return None
    if re.match(r'\s*(?:取消|退订)(?:视频|音乐|已有|我的)', message) and not re.search(r'需求|目标|开通',message):
        return None  # An existing merchant contract is a different business.
    if len(re.findall(r'第[一二三四五六七八九十\d]+(?:个|项|件)',message)) > 1:
        raise ClarificationNeeded('请逐项说明修改，例如先说“第二个预算改成800元”，避免修改错目标。')
    goals = data['goals']
    ordinal = re.search(r'第([一二三四五六七八九十\d]+)(?:个|项|件)', message)
    if ordinal:
        token = ordinal[1]
        number = int(token) if token.isdigit() else '一二三四五六七八九十'.index(token) + 1 if token in '一二三四五六七八九十' else 0
        selected = goals[number - 1:number] if 1 <= number <= len(goals) else []
    else:
        # Ignore "转账不变" rather than treating it as the change target.
        primary = re.split(r'其他|其余', primary)[0]
        selected = [g for g in goals if
            (re.search(r'ETF|投资|基金', primary, re.I) and (g.get('query') or {}).get('asset_type') == 'etf') or
            ('会员' in primary and '会员' in g['title']) or
            (re.search(r'卡片|虚拟卡|实体卡|新卡|限额|额度',primary) and g.get('operation','').startswith('cards.')) or
            (re.search(r'预留|备用金',primary) and g.get('operation')=='funds.reserve') or
            (re.search(r'账单|月报|年报|月份',primary) and g.get('operation')=='bills.report') or
            (re.search(r'理财|申购|赎回',primary) and g.get('operation','').startswith('wealth.')) or
            (re.search(r'转账|妈妈|爸爸', primary) and g['kind'] == 'bank_operation' and re.search(r'转账|funds.transfer', g['title']))]
        # An explicit verified recipient identifies a goal more precisely than
        # the broad transfer domain. Never redirect a completed mother's task
        # to a different unfinished recipient merely because it is still open.
        subject = re.split(r'改|换成|换给|调整|少花|少转|减少|降低|减去|取消|先别', primary)[0]
        if not re.search(r'卡片|银行卡|理财|产品|ETF|会员|订阅|预留|收款人', subject, re.I):
            contacts = []
            for contact in (facts or {}).get('contacts', []):
                labels = [contact.get('name'), contact.get('phone'), *contact.get('aliases', []), *contact.get('notes', [])]
                if contact.get('verified') and any(label and label in subject for label in labels):
                    contacts.append(set(filter(None, labels)))
            if len(contacts) > 1:
                raise ClarificationNeeded('收款人对应多条联系人记录，请用唯一备注或明确第几项目标。')
            if contacts:
                named = [g for g in goals if g.get('operation') == 'funds.transfer'
                         and (g.get('params') or {}).get('recipient_hint') in contacts[0]]
                if named:
                    selected = named
        if not selected:
            if re.search(r'解锁|挂失|账单|卡片|基金|ETF|会员|订阅|代扣|转账',primary,re.I):
                return None  # A new business subject must not cancel an unrelated old goal.
            selected = [g for g in goals if g['status'] not in {'SUCCEEDED', 'CANCELLED', 'FAILED', 'COMPENSATED'}]
    if len(selected)>1:
        active=[g for g in selected if g['status'] not in {'SUCCEEDED','CANCELLED','SKIPPED','COMPENSATED'}]
        if active:
            selected=active
    if len(selected) != 1:
        raise ClarificationNeeded('请明确要修改或取消第几项目标，例如“第二个预算改成800元”。')
    target = selected[0]
    if re.search(r'取消|先别|不要办|不用办|不要开|不用开', primary) and not re.search(r'不变|不要改|不是取消|不取消', primary):
        return GoalPatch(target_id=target['id'], action='cancel', expected_revision=data.get('revision', 1))
    fields = {}
    # Strip ordinal numbers so "第二个" cannot become a monetary amount.
    clean = re.sub(r'第[一二三四五六七八九十\d]+(?:个|项|件)', '', primary)
    amount_source = re.split(r'改成|改为|换成|调整为|少花|少转|减少|降低|减去',clean)[-1]
    amount_match = re.search(r'([-−－]?\d+(?:\.\d+)?\s*[万千]?|[零〇一二两三四五六七八九十百千万]+)\s*(?:元|块)?', amount_source)
    if target.get('operation') == 'bills.report':
        period = period_slot(clean, (facts or {}).get('business_now', ''))
        if period:
            fields.update(period=period, annual=len(period) == 4)
        amount_match = None  # Years and months never become financial amounts.
    if target.get('operation') == 'cards.apply':
        amount_match = None
    if amount_match:
        amount = money_slot(amount_match[1] + '元')
        if target['kind'] in {'bank_operation','bank_task'}:
            operation=target.get('operation','')
            if operation=='cards.restrict':
                field = 'single_limit_fen' if '单笔' in clean else 'daily_limit_fen' if re.search(r'每日|日限额|每天',clean) else None
                if not field:
                    raise ClarificationNeeded('请说明要修改单笔限额还是每日限额。')
            else:
                field='budget_fen' if operation=='life.goal' else 'amount_fen'
        else:
            field = 'periodic_amount_fen' if re.search(r'每天|每日|每次|定投', clean) else 'budget_fen'
        if re.search(r'少花|少转|减少|降低|减去', clean):
            old = (target.get('query') or {}).get(field) or target.get('params', {}).get(field)
            if old is None:
                raise ClarificationNeeded('这项目标没有已确定的金额，请直接说明修改后的金额。')
            amount = old - amount
        fields[field] = amount
    member = re.search(r'视频|音乐', message)
    if member:
        fields['membership_type'] = member[0]
        fields['billing_period'] = 'annual' if re.search(r'按年|包年', message) else 'monthly'
    if target.get('operation')=='cards.apply' and re.search(r'虚拟卡|实体卡',message):
        fields['kind']='virtual' if '虚拟卡' in message else 'physical'
    recipient = re.search(r'(?:收款人\s*(?:改成|改为|换成)|改给|换给)\s*([^，,。；;\s]+)',clean)
    if recipient and target.get('operation')=='funds.transfer':
        fields['recipient_hint']=recipient[1]
    if not fields:
        raise ClarificationNeeded('请说明修改后的金额、会员类型或要取消的具体目标。')
    return GoalPatch(target_id=target['id'], fields=fields, expected_revision=data.get('revision', 1))


async def _repeat_query(banking, user_id, conversation_id, prior, node, fields):
    """A fresh read preserves terminal nodes and receipts; never replay writes."""
    from .schemas import DomainTask, Intent
    params = banking.planner.bank.validate_params(node['operation'], {**node['params'], **fields})
    response = await banking.astart(user_id, '按用户指定条件重新查询；保留原办理凭证。', conversation_id,
        intent=Intent(summary='按最新条件重新查询', tasks=[DomainTask(
            domain=node['agent'], action=node['operation'], params=params)]))
    transfers = [n for n in prior['nodes'] if n['status'] == 'SUCCEEDED' and n['operation'] == 'funds.transfer']
    if transfers:
        from .presentation import yuan
        history = {'plan_id': prior['id'], 'message': '此前已完成：' + '；'.join(
            '向' + str((n.get('result') or {}).get('data', {}).get('recipient', n['params'].get('recipient_hint', '收款人')))
            + '转账' + yuan(n['params']['amount_fen']) for n in transfers)
            + '。本次只重新查询，原转账凭证保留，不重复转账。'}
        with banking.planner.db.write() as session:
            for key in ('request_goals_' + response['id'], f'goal_agenda_{user_id}_{conversation_id}'):
                doc = session.get(Document, key)
                if doc and doc.owner_id == user_id:
                    doc.data = {**doc.data, 'completed_history': [*prior.get('completed_history', []), history]}
        response = await asyncio.to_thread(banking.plans.snapshot, user_id, response['id'])
    return response
