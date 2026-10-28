"""Explain actual owned resources and receipts; never infer a successful write."""
from .bank_rules import primary_account, resolve_contact
from .errors import BankError
from .presentation import yuan
from .storage import Document, documents


def explain_snapshot(session, user_id, snapshot):
    nodes=snapshot['nodes']
    notes=[]
    for node in nodes:
        op,status,params=node['operation'],node['status'],node['params']
        result=node.get('result') or {}
        data=result.get('data') or {}
        if op=='funds.transfer':
            if status=='SUCCEEDED' and result.get('receipt_id') and data.get('amount_fen'):
                notes.append(f"向{data['recipient']}转账{yuan(data['amount_fen'])}已完成，已取得银行办理回执。")
            elif status=='UNKNOWN':
                notes.append('这笔转账提交结果仍未知，银行可能已受理；请核实原请求回执，不要重复付款。')
            elif status in {'WAIT_CONFIRM','WAIT_MFA','READY'}:
                try:
                    contact=resolve_contact(session,user_id,params['recipient_hint'])
                except BankError:
                    continue
                tail=f"（尾号{contact['last4']}）" if contact.get('last4') else ''
                strong='按照当前银行规则，这笔转账需要强验证，' if node['tier']=='RED' else ''
                notes.append(f"收款人为{params['recipient_hint']}{tail}，金额{yuan(params['amount_fen'])}，付款账户为本人当前活期账户。"
                             +strong+'核对并完成对应确认后才会付款。')
        if op=='wealth.purchase' and status in {'FAILED','WAIT_INPUT','WAIT_CONFIRM','READY'}:
            amount=params.get('amount_fen')
            available=snapshot['available_fen']
            if amount and amount>available:
                notes.append(f'申购{yuan(amount)}尚未完成：提交前复核当前可用仅{yuan(available)}，缺口{yuan(amount-available)}。'
                             '不会自动重试、透支或释放已有预留。')
        if op!='subscriptions.cancel':
            continue
        key=params.get('subscription_id')
        if status=='SUCCEEDED' and result.get('receipt_id'):
            key=data.get('id',key)
        elif isinstance(key,str) and key.startswith('$'):
            from .preflight import resolve
            actual={n['local_id']:(n.get('result') or {}).get('data') for n in nodes
                    if n['status']=='SUCCEEDED' and n.get('result')}
            try:
                key=resolve(key,actual)
            except (BankError,KeyError):
                continue
        doc=session.get(Document,key) if isinstance(key,str) else None
        if not doc or doc.kind!='subscription' or doc.owner_id!=user_id:
            continue
        value=data if status=='SUCCEEDED' and result.get('receipt_id') else doc.data
        name=value['name']
        scope=params.get('scope','both')
        if scope=='bank':
            if status=='SUCCEEDED' and result.get('receipt_id'):
                stopped=value['bank_mandate'] in {'REVOKED','STOPPED'}
                notes.append(f"{name}银行代扣{'已停止' if stopped else '尚未停止'}，已取得办理回执。")
            else:
                notes.append(f'当前只是停止{name}银行代扣的待确认安排，尚未据此更改状态。')
            contract='仍有效并保留' if value['merchant_contract']=='ACTIVE' else '维持当前状态'
            notes.append(f'停止银行代扣与取消商户会员合同是两件事；该会员合同{contract}，本次不取消商户合同。'
                         '历史消费不会自动退款；后续合同费用或其他付款方式应向商户核实。')
            changed={n['params'].get('subscription_id') for n in nodes if n['operation']=='subscriptions.cancel'}
            others=[d.data['name'] for d in documents(session,'subscription',user_id) if d.id not in changed]
            if others:
                notes.append('本次不改变'+ '、'.join(others[:4])+'的合同与银行代扣。')
            if not any(n['operation'].startswith('cards.') and n['operation']!='cards.query' for n in nodes):
                cards=documents(session,'card',user_id)
                active=any(c.data.get('status')=='ACTIVE' for c in cards)
                notes.append('本次没有改变整卡状态、限额或普通支付设置；停代扣不等于冻结整卡。'
                             +('当前正常卡片的普通支付仍可按原状态与限额使用。' if active else '普通支付是否可用仍取决于当前卡片状态及限额。'))
    if not notes:
        return
    account=primary_account(session,user_id)
    notes.append(f"当前账户总余额{yuan(account.balance_fen)}，可用{yuan(snapshot['available_fen'])}。")
    if snapshot['status']=='COMPLETED':
        notes.append('可展开本次步骤的办理凭证，继续核对银行结果；已完成操作不会重复提交。')
    detail='\n'.join(dict.fromkeys(notes))
    explained_operations={'funds.transfer','wealth.purchase','subscriptions.cancel',
                          'funds.query','wealth.query','subscriptions.query'}
    preserves_other_results=any(n['operation'] not in explained_operations for n in nodes)
    if snapshot['status'] in {'COMPLETED','PARTIAL'} and not preserves_other_results:
        snapshot['assistant_message']=detail
    else:
        snapshot['assistant_message']+='\n'+detail
