"""Narrow complete-message grammars for proposals, never execution or authority.

An unfamiliar clause returns None so the full planner still sees the entire text.
Names resolve from authenticated facts, amounts are integer fen, and all proposals
continue through the normal gateway, confirmation and scheduled-time checks.
"""
from datetime import datetime, timedelta
import re

from .dialogue import explicit_transfers, money_slot
from .schemas import DomainTask, Intent


def _complete_bill_question(message):
    remaining = re.sub(r'20\d{2}年\s*\d{1,2}月|20\d{2}-\d{2}|20\d{2}年?', '', message)
    remaining = re.sub(r'帮我|请|查询|查看|分析|列出|单独|分别|不算|不计入|不要算|计入|工资|收入|列|'
        r'消费|转账|账单|收支|月报|年报|合计|总共|一共|多少|了|我|的|是|有|与|和|及|并|'
        r'本月|上月|今年|去年|年度|全年|净额|退款|口径|金额|毛|净', '', remaining)
    return not re.sub(r'[，,；;。？！?!：:\s]', '', remaining)


def explicit_intent(message, facts, actor_id, participant_ids):
    if re.search(r'如果|假如|假设|是否|怎么|如何|不要(?:申请|安排|转|收)|不参与|不包括我|不算我', message):
        return None
    # Exact dated reports stay on a typed read-only path. No extra model fields.
    if (re.search(r'账单|月报|年报|消费', message)
            and _complete_bill_question(message)
            and not re.search(r'取消|撤销|申购|购买|转给|(?:给|向).{0,12}转|预留|理财|投资|ETF|会员|卡片|现金流|未来', message, re.I)):
        from .dialogue import period_slot
        period = period_slot(message, facts['business_now'])
        if period:
            return Intent(summary='按账本核对消费与转账口径', tasks=[DomainTask(
                domain='bills', action='bills.report', params={'period': period, 'annual': len(period) == 4})])
    purchase = re.fullmatch(r'(?:我(?:只有|目前是)R[1-5][，,]?(?:但我?(?:愿意|可以)承担风险[，,])?)?'
        r'(?:请|帮我)?(?:买|购买|申购)\s*(\d+(?:\.\d{1,2})?元)\s*([^，,。；;]+)[。\s]*', message, re.I)
    if purchase:
        product = next((p for p in facts['products'] if purchase[2] in {p['name'], p['id']}), None)
        if product and money_slot(purchase[1]) > 0:
            # Stated willingness is context, never a replacement risk assessment.
            return Intent(summary='核对所选产品申购条件，风险资格由银行现状判定', tasks=[DomainTask(
                domain='wealth', action='wealth.purchase', params={'amount_fen':money_slot(purchase[1]), 'product_id':product['id']})])
    # A single AA bill has one total. Creating a collection request cannot make
    # the other participants pay, and the creator's share is not collectible.
    aa = re.fullmatch(r'(?:晚饭|聚餐|饭钱|餐费|账单|总共|总计)?\s*(\d+(?:\.\d{1,2})?元)[，,\s]*我和(.{1,35}?)([二两三四五六七八九十\d]+)(?:个)?人AA[。。，,\s]*(?:我(?:先付了|已垫付|已经付了)[，,\s]*)?(?:帮我|请)?(?:向他们收他们的份额|收回他们的份额|发起收款|向他们发起AA收款)[。！!\s]*', message, re.I)
    if aa:
        names = [n.strip() for n in re.split(r'[、和与]', aa[2])]
        count = int(money_slot(aa[3]+'元')/100)
        if money_slot(aa[1]) <= 0 or len(names)+1 != count or len(set(names)) != len(names) or any(n not in participant_ids for n in names):
            return None
        return Intent(summary='按整笔餐费发起AA收款，其他参与者各自确认支付', tasks=[DomainTask(
            domain='funds', action='aa.create', params={'amount_fen': money_slot(aa[1]),
                'participants': [actor_id, *(participant_ids[n] for n in names)], 'subject': '聚餐AA'})])
    # Only the specified, already supported virtual debit-card product. No
    # account opening, credit application, transfer, limit change or bundled goal.
    card = re.fullmatch(r'(?:我已登录[，,]\s*(?:演示客户资料已完成身份核验[，,]\s*)?名下已有活期账户[。]\s*)?'
        r'(?:我想|想|请|帮我)?申请一张(?:绑定这个账户的)?(?:演示)?虚拟(?:支付|付款|借记)?卡'
        r'(?:[，,]\s*叫([^，,。；;]{1,20}))?(?:[；;，,]\s*不是开新账户[，,]\s*也不是申请信用卡)?'
        r'[。\s]*(?:先展示需要核对的开通信息和确认步骤[。\s]*)?', message)
    if card:
        return Intent(summary='核对本人账户绑定的虚拟支付卡开通草案', tasks=[DomainTask(
            domain='cards', action='cards.apply', params={'kind': 'virtual', 'name': card[1] or '日常虚拟卡'})])
    # Explicit date + wall clock + one source-bound transfer only. The following
    # wording is a request to keep a draft, not advance payment authorization.
    scheduled = re.fullmatch(r'(?:请|帮我)?(?:安排|预约)?(明天|后天|\d{4}-\d{2}-\d{2})(上午|下午|晚上)?'
        r'(\d{1,2})(?:点|时)(?:(\d{1,2})分?)?(.+?)'
        r'[；;。]\s*先记录这项安排[，,]\s*到期付款前再让我核对确认[。\s]*', message)
    if scheduled:
        bindings = explicit_transfers(scheduled[5], facts)
        if len(bindings) != 1:
            return None
        binding = bindings[0]
        if re.sub(r'给|向|\s', '', scheduled[5][:binding['start']]+scheduled[5][binding['end']:]):
            return None
        hour, minute = int(scheduled[3]), int(scheduled[4] or 0)
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            return None
        if scheduled[2] in {'下午', '晚上'} and hour < 12:
            hour += 12
        clock = datetime.fromisoformat(facts['business_now'])
        try:
            day = clock + timedelta(days=1 if scheduled[1] == '明天' else 2) if scheduled[1] in {'明天', '后天'} else datetime.fromisoformat(scheduled[1])
        except ValueError:
            return None
        when = clock.replace(year=day.year, month=day.month, day=day.day, hour=hour, minute=minute, second=0, microsecond=0)
        if when <= clock:
            return None
        return Intent(summary='预约草案已保留，到期仍需本人核对确认', tasks=[DomainTask(
            domain='funds', action='funds.transfer', params={'recipient_hint': binding['recipient_hint'],
                'amount_fen': binding['amount_fen']}, scheduled_for=when.isoformat())])
    return None
