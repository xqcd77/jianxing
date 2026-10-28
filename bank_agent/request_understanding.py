"""Conservative, source-bound shortcuts for explicit multi-goal requests.

Unknown clauses fall through to the business model with the full original text.
This module creates proposals only: it has no execution or authorization tools.
"""
import re
from decimal import Decimal

from .dialogue import explicit_transfers, money_slot, transfer_grounding
from .errors import ClarificationNeeded
from .information import ETF, investment_amounts, investment_preferences
from .presentation import yuan
from .schemas import AdditionalGoal, DomainTask, Intent, PublicInformationQuery

AMOUNT = r"\d+(?:\.\d+)?\s*[万千]?\s*[元块]"
NEW_CARD = re.compile(r"(?:申请|开通|开|办|订阅).*(?:卡片|张.*卡)")
MEMBERSHIP = re.compile(r"(?:订|定|开通|开|购买|买).{0,12}会员")
BOUNDARIES = (
    "当前新卡共用本人账户资金，没有独立的充值余额；不能把新卡当作收款联系人。"
    "如需限制消费，可另行设置交易限额；如需隔离预算，可另行确认资金预留。"
)
MEMBERSHIP_BOUNDARY = "会员开通需求已保留。当前支持查询、取消已有订阅和续费提醒，尚未接入商户的新会员开通接口；请在对应商户核对套餐与价格后开通，本次没有扣费。"


def remaining_wealth_budget(message,facts,tasks):
    """A single explicit remaining-wealth fraction, bound to all proposed transfers."""
    if ETF.search(message):
        return None
    match=re.search(r'(?:剩下|剩余|余下|其余)[^。；;]{0,30}?(\d+(?:\.\d+)?[%％]|百分之[一二两三四五六七八九十百]+|[二三四五十]分之[一二三四五六七八九])[^。；;]{0,18}?(?:理财|投资)',message)
    if not match:
        return None
    ratio=match[1]
    numbers={'一':1,'二':2,'两':2,'三':3,'四':4,'五':5,'六':6,'七':7,'八':8,'九':9,'十':10}
    if ratio.startswith('百分之'):
        fraction=Decimal(money_slot(ratio[3:]+'元'))/10000
    elif '分之' in ratio:
        denominator,numerator=ratio.split('分之')
        fraction=Decimal(numbers[numerator])/numbers[denominator]
    else:
        fraction=Decimal(ratio[:-1])/100
    if not 0<fraction<=1:
        return None
    outgoing=sum(t.params.get('amount_fen',0) for t in tasks if t.action=='funds.transfer')
    return int(Decimal(max(0,facts['account']['available_fen']-outgoing))*fraction)


def _recipient(text, facts, context):
    matches = []
    for c in facts['contacts']:
        if c.get('verified') and any(x and x in text for x in [c['name'], c.get('phone',''), *c.get('aliases', []), *c.get('notes', [])]):
            matches.append(c['name'])
    if not matches and re.search(r"给她|给他|向她|向他", text) and context.get('last_recipient'):
        matches = [context['last_recipient']]
    return matches[0] if len(set(matches)) == 1 else None


def transfer_clause(text, facts, context, remaining):
    if not re.search(r"转|汇款|汇给|打款|打钱", text) or re.search(r"转入|转出.*卡", text):
        return None
    if re.search(r'并|以及|顺便|会员|ETF|申购|赎回|预留|冻结|解锁|撤销|取消|退款|理财',text,re.I):
        return None
    chinese_percentage = re.search(r'百分之([零〇一二两三四五六七八九十百]+)',text)
    if chinese_percentage:
        percent = Decimal(money_slot(chinese_percentage[1]+'元'))/100
        text = text[:chinese_percentage.start()] + str(percent) + '%' + text[chinese_percentage.end():]
    ratio = re.search(r"一半|半数|四分之一|三分之一|三分之二|全部|所有|全额|(\d+(?:\.\d+)?)\s*[%％]", text)
    if ratio:
        recipient = _recipient(text, facts, context)
        if not recipient:
            return Intent(summary='按比例转账', clarification='请明确这笔比例转账的收款人。', clarification_kind='recipient')
        fraction = Decimal(ratio[1])/100 if ratio[1] else {
            '一半':Decimal('0.5'), '半数':Decimal('0.5'), '四分之一':Decimal('0.25'),
            '三分之一':Decimal(1)/3, '三分之二':Decimal(2)/3,
            '全部':Decimal(1), '所有':Decimal(1), '全额':Decimal(1),
        }[ratio[0]]
        if not 0 < fraction <= 1:
            raise ClarificationNeeded('转账比例需大于0且不超过100%，请核对。')
        basis = facts['account']['balance_fen'] if '总余额' in text else remaining if re.search(r'剩余|剩下|余下', text) else facts['account']['available_fen']
        amount = int(Decimal(basis)*fraction)
        if amount <= 0:
            raise ClarificationNeeded('当前按该比例计算的可转金额不足0.01元，请先核对可用余额。')
        label = '总余额' if '总余额' in text else '剩余可用余额' if re.search(r'剩余|剩下|余下', text) else '可用余额'
        return Intent(summary=f'按当前{label}{yuan(basis)}的{ratio[0]}，向{recipient}转{yuan(amount)}（不足一分向下取整）',
            tasks=[DomainTask(domain='funds', action='funds.transfer', params={'recipient_hint':recipient,'amount_fen':amount,'subject':'按比例转账'})])
    simple=re.fullmatch(r'(?:请|帮我|我要)?(?:给|向)(.{1,24}?)(?:转账|转|汇款|汇|打款)\s*((?:\d+(?:\.\d+)?\s*[万千]?|[零〇一二两三四五六七八九十百千万]+)\s*[元块])',text.strip())
    if simple:
        target=re.sub(r'^我(?:的)?','',simple[1].strip())
        recipients=[c for c in facts['contacts'] if c.get('verified') and target in [c['name'],c.get('phone'),*c.get('aliases',[]),*c.get('notes',[])]]
        params={'amount_fen':money_slot(simple[2])}
        if len(recipients)==1:
            params['recipient_hint']=recipients[0]['name']
        return Intent(summary='核对转账',tasks=[DomainTask(domain='funds',action='funds.transfer',params=params)],
            clarification=None if len(recipients)==1 else '请明确已验证的收款人。',clarification_kind='recipient')
    bound = explicit_transfers(text, facts)
    if len(bound) == 1:
        b = bound[0]
        leftover = (text[:b['start']]+' '+text[b['end']:]).strip()
        leftover = re.sub(rf'(?:我的|我有|总共|总资金)\s*{AMOUNT}', '',leftover)
        if re.sub(r'(?:请|帮我|我要|我想|先|再|啊|吧|呢|\s)', '', leftover):
            return None
        return Intent(summary='核对转账',tasks=[DomainTask(domain='funds',action='funds.transfer',params={
            'recipient_hint':b['recipient_hint'],'amount_fen':b['amount_fen']})])
    # Unknown language remains a full-model request; do not use a broad amount
    # extractor to discard an unparsed tail after an otherwise valid transfer.
    if re.search(r'然后|还有|再', text):
        return None
    return transfer_grounding(text, facts, context)


def understand_request(message, facts, context=None):
    context = context or {}
    # Conditions, historical actions and scheduling need the full planner.
    if re.search(r'如果|假如|要是|是否|明天|后天|预约|每月|下周|昨天|上次|已转|已经|转过|到账没|撤回|'
                 r'只分析|只算|先分析|算一下|多少钱|能否|能不能|可不可以|不要操作|不转|别转|好不好|可以吗|应该|假设|怎么|如何', message):
        return None
    if context.get('active_plan_id') and re.match(r'\s*(?:在.*(?:计划|安排).*?)?(?:再加|追加|加上|再给|再帮我|再安排)',message):
        return None  # Existing-plan append must retain version and semantic review.
    pending = context.get('pending_draft', {})
    if pending and message.strip() in {'虚拟卡','实体卡'}:
        draft = Intent.model_validate(pending['intent'])
        if any(t.action=='cards.apply' for t in draft.tasks):
            for task in draft.tasks:
                if task.action=='cards.apply':
                    task.params['kind'] = 'virtual' if message.strip()=='虚拟卡' else 'physical'
            return draft.model_copy(update={'clarification':None})
    normalized = re.sub(r'(?<=\d)[,，](?=\d{3}(?:\D|$))', '', message)
    listed=re.match(r'\s*(?:先|请|帮我|现在)*(?:处理|安排|办理)?(?:[两二三四五六七八九十\d]+笔)?转账\s*[:：]([^；;。\n]+)',normalized)
    if listed:
        entries=[re.fullmatch(r'\s*([^、，,\d]{1,20}?)\s*((?:\d+(?:\.\d+)?|[零〇一二两三四五六七八九十百千万]+)\s*[元块])\s*',part) for part in re.split(r'[、，,]',listed[1])]
        if entries and all(entries):
            normalized='，'.join('给'+entry[1]+'转'+entry[2] for entry in entries)+normalized[listed.end():]
    normalized = re.sub(r'(?<=[元块])\s*(?=给|向|剩下|剩余|余下|其余|再给|再查|并给出|并分析)', '，', normalized)
    clauses = [c.strip(' ，,；;。') for c in re.split(r'[，,；;。\n]|然后|并且|同时|另外|(?=再(?:去|给|把|查|订|开))', normalized) if c.strip(' ，,；;。')]
    tasks, extras, descriptions = [], [], []
    remaining = facts['account']['available_fen']
    capital, contribution = investment_amounts(normalized)
    etf_goal = None
    clarification = None
    for clause in clauses:
        if re.fullmatch(r'(?:请)?(?:只(?:查|看)(?:公开)?资料|先(?:列出|列)?(?:计划)?(?:并)?等我确认|先核对并等我确认)', clause):
            continue  # Execution still requires the gateway's explicit approval.
        # Negative clauses cannot authorize a positive action. Explicit correction
        # is handled in its own source clause below, without inheriting old slots.
        if re.match(r'^(?:不要|别|不用|不许)', clause):
            if re.fullmatch(r'(?:不要|别|不用|不许).*(?:转.*|下单|购买|申购|操作|办理)[了啊吧]*', clause):
                continue
            return None
        clause = re.sub(r'^(?:改为|改成|接着|接下来|随后|先|再去|再|另外)\s*', '', clause)
        if re.fullmatch(r'(?:并|再|也)?(?:帮我|给我)?(?:给出|提供|做)?(?:推荐|建议|比较|对比)',clause) and any(t.action=='wealth.compare' for t in tasks):
            continue
        reserve = re.fullmatch(rf'(?:帮我)?预留\s*({AMOUNT})\s*(?:作为|用于|的)?\s*([^转买购申赎取消冻结释放]{{1,30}})', clause)
        if reserve:
            tasks.append(DomainTask(domain='funds', action='funds.reserve', params={
                'amount_fen': money_slot(reserve[1]), 'purpose': reserve[2].strip()}))
            remaining -= money_slot(reserve[1])
            continue
        if ETF.search(clause):
            if re.search(r'转|汇款|打款|打钱',clause) or MEMBERSHIP.search(clause):
                return None
            query = PublicInformationQuery(topic='fund_products',asset_type='etf',product_hint=clause[:120],periodic_amount_fen=contribution,
                **investment_preferences(message))
            explicit = re.search(rf'({AMOUNT})\s*(?:用于|用来|拿来)?\s*(?:投资|投|买|申购|推荐|选|看看|了解|研究|的?ETF)', clause, re.I)
            explicit = explicit or re.search(rf'ETF(?:预算)?\s*(?:是|为)?\s*({AMOUNT})',clause,re.I)
            if explicit:
                query.budget_fen = money_slot(explicit[1])
            elif re.search(r'剩余|剩下|余下|其余', clause):
                from .advisory_budget import bind_budget_rule, calculate_budget
                outgoing = sum(t.params['amount_fen'] for t in tasks if t.action=='funds.transfer')
                rule = bind_budget_rule({'source_text': normalized, 'query': query.model_dump(mode='json')})
                basis = capital if capital is not None else facts['account']['available_fen']
                query.budget_fen = calculate_budget(rule, basis, outgoing)[0] if rule else None
            else:
                query.budget_fen = capital
            currency = re.search(r'美元|美金|欧元', message)
            if currency:
                query.currency = 'EUR' if currency[0]=='欧元' else 'USD'
                query.budget_fen = query.periodic_amount_fen = None
            etf_goal = AdditionalGoal(kind='public_information',title='ETF资料与投资条件',source_text=clause,
                message='ETF需求已保留为资料核查；当前没有证券交易通道，未购买或开通定投。', information_query=query)
            extras.append(etf_goal)
            continue
        if re.search(r'每(?:个交易日|日|天).*定投|每个交易日.*投',clause) and ETF.search(message):
            continue  # contribution was bound separately over the full source
        if MEMBERSHIP.search(clause):
            if re.search(r'并|以及|顺便|取消|转账|卡片|新卡',clause):
                return None
            extras.append(AdditionalGoal(kind='capability_gap',title='开通会员',source_text=clause,message=MEMBERSHIP_BOUNDARY))
            continue
        if not ETF.search(clause) and re.search(r'理财|模拟.{0,8}产品',clause):
            if re.search(r'转账|转给|给.+转|赎回|取消|预约',clause):
                return None
            if re.search(r'研究|深度分析|流动性分析',clause) and not re.search(r'申购|购买|用于|用来',clause):
                extras.append(AdditionalGoal(kind='research',title='理财条件研究',source_text=clause,
                    research_query=clause,message='独立核查风险与流动性，研究不会产生扣款。'))
                continue
            ratio=re.search(r'(\d+(?:\.\d+)?)\s*[%％]',clause)
            if re.search(r'[-−－]\s*\d',clause) or ratio and not 0<Decimal(ratio[1])<=100:
                extras.append(AdditionalGoal(kind='clarification',title='理财预算待核对',source_text=clause,
                    message='理财金额必须大于零，比例需大于0且不超过100%；转账需求仍单独保留。'))
                continue
            fraction_budget=remaining_wealth_budget(clause,facts,tasks)
            if fraction_budget is not None and not ratio:
                amount=fraction_budget
                calculation=f'按扣除计划转账后的可用资金及指定比例，理财预算为{yuan(amount)}；尚未扣款。'
            elif ratio:
                fraction=Decimal(ratio[1])/100
                if not 0<fraction<=1:
                    raise ClarificationNeeded('理财比例需大于0且不超过100%，请核对。')
                outgoing=sum(t.params.get('amount_fen',0) for t in tasks if t.action=='funds.transfer')
                basis=max(0,facts['account']['available_fen']-outgoing) if re.search(r'剩下|剩余|余下|其余',clause) else facts['account']['available_fen']
                amount=int(Decimal(basis)*fraction)
                calculation=f'按可用资金扣除计划转账后的{yuan(basis)} × {ratio[1]}%，理财预算为{yuan(amount)}；尚未扣款。'
            else:
                amount_match=re.search(r'(\d+(?:\.\d+)?\s*[万千]?\s*[元块])',clause)
                amount=money_slot(amount_match[1]) if amount_match else None
                calculation=f'理财预算为{yuan(amount)}，尚未扣款。' if amount else '请先查看产品比较，再明确理财预算与产品。'
            product=next((p['id'] for p in facts.get('products',[]) if p['name'] in clause),None)
            if not product or re.search(r'推荐|比较|对比|建议',clause):
                tasks.append(DomainTask(id='wealth_compare',domain='wealth',action='wealth.compare'))
            if amount and re.search(r'用于|用来|申购|购买|买|拿|投',clause) and not re.search(r'只(?:看|推荐|分析)|先(?:看|推荐)',clause):
                if amount<=0:
                    raise ClarificationNeeded('按当前余额计算的理财预算不足0.01元，请调整金额。')
                transfer_ids=[]
                for i,task in enumerate(tasks):
                    if task.action=='funds.transfer':
                        task.id=task.id or f't{i}'
                        transfer_ids.append(task.id)
                pending=DomainTask(id='wealth_purchase',domain='wealth',action='wealth.purchase',
                    params={'amount_fen':amount,**({'product_id':product} if product else {})},depends_on=transfer_ids)
                if product:
                    tasks.append(pending)
                else:
                    extras.append(AdditionalGoal(kind='bank_task',title='理财申购待选产品',source_text=clause,
                        message=calculation+' 请选择具体模拟产品，转账可独立核对。',pending_task=pending))
            continue
        if re.search(r'(?:分析|研究|核查|解释).*(?:账单|收支|流水|消费|现金流)|(?:账单|收支|现金流).*(?:分析|研究)',clause):
            extras.append(AdditionalGoal(kind='research',title='收支与现金流研究',source_text=clause,
                research_query=clause,message='按账本独立研究；本轮研究不生成资金操作。'))
            continue
        if NEW_CARD.search(clause):
            if re.search(r'并|以及|顺便|转|放入|放到|充值|限额|预留|冻结|取消',clause):
                return None
            kind = 'virtual' if '虚拟' in clause else 'physical' if '实体' in clause else None
            tasks.append(DomainTask(id='new_card',domain='cards',action='cards.apply',params={'kind':kind} if kind else {}))
            if not kind:
                clarification = '新卡需求已保留，请选择虚拟卡还是实体卡？'
            continue
        if re.search(r'转入|充值|充入|存入|放到|放入',clause) and (any(t.action=='cards.apply' for t in tasks) or re.search(r'新卡|卡里|卡片',clause)):
            amount = money_slot(clause.split('转入')[-1]) if '转入' in clause else money_slot(clause)
            extras.append(AdditionalGoal(kind='capability_gap',title='新卡资金用途'+(yuan(amount) if amount else ''),source_text=clause,message=BOUNDARIES))
            continue
        if re.search(r'新卡',clause) and re.search(r'单笔',clause) and re.search(r'每日|每天|日',clause) and re.search(r'限额|限制',clause):
            if not any(t.id=='new_card' for t in tasks):
                return None
            amount = money_slot(clause)
            if amount is None:
                return None
            tasks.append(DomainTask(domain='cards',action='cards.restrict',params={'card_id':'$new_card.card_id','single_limit_fen':amount,'daily_limit_fen':amount},depends_on=['new_card']))
            continue
        if re.fullmatch(r'(?:帮我|给我)?查(?:询|一下)?(?:我的)?(?:可用)?余额',clause):
            tasks.append(DomainTask(domain='funds',action='balance.query'))
            continue
        transfer = transfer_clause(clause,facts,context,remaining)
        if transfer:
            for task in transfer.tasks:
                remaining -= task.params.get('amount_fen',0)
                tasks.append(task)
            if '按当前' in transfer.summary:
                descriptions.append(transfer.summary)
            continue
        if re.fullmatch(r'(?:我有|我的|本金|总资金|总共|预算).*?[元块]',clause):
            continue
        return None  # No silent consumption of unfamiliar clauses.
    if not tasks and not extras:
        return None
    public_goals = [g for g in extras if g.information_query and g.information_query.asset_type=='etf']
    if len(public_goals)==1:
        from .information import ground_investment_query
        public_goals[0].information_query = ground_investment_query(public_goals[0].information_query,message,
            sum(t.params.get('amount_fen',0) for t in tasks if t.action=='funds.transfer'),facts['account']['available_fen'])
    # Pure ETF requests retain the existing typed public-information path.
    if not tasks and len(extras)==1 and etf_goal:
        return None
    if not tasks and extras and all(g.kind=='research' for g in extras):
        return None
    from .dialogue import reconcile_missing_fields
    result=reconcile_missing_fields(Intent(summary='；'.join(descriptions) or '逐项核对本次办理与待处理需求',tasks=tasks,
                  clarification=clarification, additional_goals=extras))
    return None if len(tasks)==1 and not extras and result.clarification else result
