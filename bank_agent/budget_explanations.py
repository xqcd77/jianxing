"""Read-only whole-request arithmetic from complete, explicit source clauses."""
import re

from .dialogue import explicit_transfers, money_slot
from .presentation import yuan
from .schemas import DomainTask, Intent


def infeasible_bundle(message, facts):
    if not (re.search(r'只有.*(?:三项|全部|都能)|全部.*才能|一项都别办|一项都不要',message)
            and re.search(r'不够|不足|都能|全部',message)):
        return None
    # Only a complete list of understood amount clauses is eligible. An unknown
    # instruction falls through, rather than being discarded by this shortcut.
    clauses=re.split(r'[，,。；;、]',message)
    amounts=[]
    tasks=[]
    for clause in filter(None,(c.strip() for c in clauses)):
        if re.fullmatch(r'(?:只有|不够|不足|全部|任一|一项都|不调整|不要调整).*',clause):
            continue
        reserve=re.fullmatch(r'(?:给我|帮我)?预留(\d+(?:\.\d{1,2})?元)([^\d]{1,15})',clause)
        purchase=re.fullmatch(r'(?:再)?(?:买|申购)(\d+(?:\.\d{1,2})?元)([^\d]{1,20}(?:理财|产品))',clause)
        transfers=explicit_transfers(clause,facts)
        if reserve:
            amounts.append(('预留'+reserve[2],money_slot(reserve[1])))
            tasks.append(DomainTask(domain='funds',action='funds.reserve',params={'amount_fen':money_slot(reserve[1]),'purpose':reserve[2]}))
        elif purchase:
            amounts.append(('拟申购'+purchase[2],money_slot(purchase[1])))
            tasks.append(DomainTask(domain='wealth',action='wealth.purchase',params={'amount_fen':money_slot(purchase[1])}))
        elif len(transfers)==1 and transfers[0]['text']==clause:
            b=transfers[0]
            amounts.append(('向'+b['recipient_hint']+'转账',b['amount_fen']))
            tasks.append(DomainTask(domain='funds',action='funds.transfer',params={'recipient_hint':b['recipient_hint'],'amount_fen':b['amount_fen']}))
        else:
            return None
    if len(amounts)<2 or any(not a or a<=0 for _,a in amounts):
        return None
    required=sum(a for _,a in amounts)
    available=facts['account']['available_fen']
    if required<=available:
        return None  # Feasibility alone is never approval to perform the bundle.
    detail='、'.join(label+yuan(amount) for label,amount in amounts)
    gap=required-available
    text=(f'{detail}，整份需求合计{yuan(required)}；当前可用{yuan(available)}，缺口{yuan(gap)}。'
          '依照你“全部可行才确认”的条件，本轮不发起任何确认或执行，没有预留、转账或申购，账户余额和业务对象均未改变。'
          f'可以由你补足{yuan(gap)}后重新核验，或明确修改各项金额；不会擅自减额、删项或释放其他预留来凑足资金。')
    return Intent(summary='整份需求资金不足，按原条件全部暂停',tasks=tasks,clarification=text)


def remember_infeasible_bundle(db,user_id,conversation_id,message,intent):
    from datetime import datetime,timedelta,timezone
    from .storage import save_document
    with db.write() as session:
        save_document(session,f'draft_{user_id}_{conversation_id}','draft',user_id,{
            'request':message,'clarification':intent.clarification,'clarification_kind':'other',
            'intent':intent.model_dump(mode='json'),
            'expires_at':(datetime.now(timezone.utc)+timedelta(minutes=30)).isoformat()})
