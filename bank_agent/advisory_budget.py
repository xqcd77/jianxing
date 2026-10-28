"""Recompute source-bound advisory formulas after changes to an owned plan.

This module only refreshes research inputs. It cannot submit or authorize money.
"""
import asyncio
from copy import deepcopy
from decimal import Decimal
import re

from .presentation import yuan
from .schemas import PublicInformationQuery


def bind_budget_rule(goal):
    query = goal.get('query') or {}
    source = goal.get('source_text', '')
    if query.get('currency', 'CNY') != 'CNY' or not re.search(r'剩下|剩余|余下|其余', source):
        return None
    remainder = re.split(r'剩下|剩余|余下|其余', source, maxsplit=1)[1]
    remainder = re.split(r'[，,；;。]', remainder)[0]
    for amount in re.finditer(r'(?:\d+(?:\.\d+)?|[一二两三四五六七八九十百千万]+)\s*[万千]?\s*[元块]', remainder):
        if not re.search(r'每(?:日|天|次)|定投', remainder[max(0, amount.start()-8):amount.start()]):
            return None  # "剩下的钱拿1000元" is fixed, not all remaining cash.
    ratios = re.findall(r'\d+(?:\.\d+)?\s*[%％]|百分之[零一二两三四五六七八九十百]+|[二三四五十]分之[一二两三四五六七八九]|一半', remainder)
    if len(ratios) > 1:
        return None
    ratio = ratios[0] if ratios else ''
    if ratio.startswith('百分之'):
        from .dialogue import money_slot
        fraction = Decimal(money_slot(ratio[3:] + '元')) / 10000
    elif '分之' in ratio:
        numbers = {'一': 1, '二': 2, '两': 2, '三': 3, '四': 4, '五': 5, '六': 6, '七': 7, '八': 8, '九': 9, '十': 10}
        denominator, numerator = ratio.split('分之')
        fraction = Decimal(numbers[numerator]) / numbers[denominator]
    else:
        fraction = Decimal('.5') if ratio == '一半' else Decimal(ratio[:-1].strip()) / 100 if ratio else Decimal(1)
    if not 0 < fraction <= 1:
        return None
    rule = {'basis': 'available_after_unfinished_plan_outflows', 'fraction': str(fraction)}
    capital = re.search(r'(?:我有|我的|总资金|总共|本金)\s*(\d+(?:\.\d+)?)\s*([万千]?)\s*元', source)
    if capital:
        rule['capital_fen'] = int(Decimal(capital[1]) * {'': 1, '千': 1000, '万': 10000}[capital[2]] * 100)
    return rule


def calculate_budget(rule, available, pending=0, spent=0):
    """The same source-bound arithmetic before review and after a task edit."""
    if rule.get('capital_fen') is not None:
        available = min(available, max(0, rule['capital_fen'] - spent))
    basis = max(0, available - pending)
    amount = int(Decimal(basis) * Decimal(rule['fraction']))
    notice = (f"按当前可用{yuan(available)}扣除本计划尚未执行的支出{yuan(pending)}，"
              f"剩余{yuan(basis)} × {Decimal(rule['fraction']) * 100}%＝{yuan(amount)}资料预算；"
              '已完成支出不重复扣减，尚未购买或授权。')
    return amount, notice


def derive_budget(rule, facts, nodes):
    spent, pending = 0, 0
    for node in nodes:
        if node['operation'] not in {'funds.transfer', 'funds.reserve', 'wealth.purchase', 'life.purchase'}:
            continue
        amount = node['params'].get('amount_fen', (node['params'].get('quote') or {}).get('total_fen', 0))
        if node['status'] == 'SUCCEEDED':
            spent += amount
        elif node['status'] not in {'CANCELLED', 'SKIPPED', 'COMPENSATED', 'PARTIAL'}:
            pending += amount
    return calculate_budget(rule, facts['account']['available_fen'], pending, spent)


async def refresh_derived_information(banking, user_id, conversation_id, plan):
    from .goal_state import read_agenda
    from .request_goals import persist_goal_agenda
    agenda = await asyncio.to_thread(read_agenda, banking, user_id, conversation_id)
    extras = deepcopy([g for g in agenda['goals'] if g['kind'] != 'bank_operation']) if agenda else []
    facts = await asyncio.to_thread(banking.planner.bank.facts, user_id)
    changed = []
    for goal in extras:
        if goal['kind'] != 'public_information' or goal['status'] == 'CANCELLED' or not goal.get('budget_rule'):
            continue
        amount, notice = derive_budget(goal['budget_rule'], facts, plan['nodes'])
        query = PublicInformationQuery.model_validate({**goal['query'], 'budget_fen': amount})
        goal.update(query=query.model_dump(mode='json'), notice=notice, result=None,
                    status='NEEDS_RETRY', revision=goal.get('revision', 1) + 1)
        changed.append(goal['id'])
    if not changed:
        return plan
    await banking._atomic(persist_goal_agenda, banking.planner.db, user_id, conversation_id,
                          plan, extras, changed, agenda['revision'])
    for goal in extras:
        if goal['id'] in changed:
            result = await banking.information.answer(PublicInformationQuery.model_validate(goal['query']))
            goal.update(result=result, status='UNAVAILABLE' if result.get('source_status') == 'unavailable'
                        else 'NEEDS_INPUT' if result.get('suitability') == 'needs_verification' else 'ANSWERED')
    # The existing result-only CAS checks goal revision and query. A concurrent
    # plan edit still marks this answer stale at snapshot time; it cannot grant.
    await banking._atomic(persist_goal_agenda, banking.planner.db, user_id, conversation_id,
                          plan, extras, None, None, True)
    return await asyncio.to_thread(banking.plans.snapshot, user_id, plan['id'])
