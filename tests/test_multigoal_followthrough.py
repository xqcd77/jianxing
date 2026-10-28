"""Mixed-task acceptance: obligations survive missing fields and product replies."""
import time

import pytest

from bank_agent.schemas import Intent
from bank_agent.storage import Document, User
from conftest import approve, finish, payroll
from test_public_information import ask


def add_wang_alias(client):
    with client.app.state.db.write() as session:
        contact=session.get(Document,'contact_wang')
        contact.data={**contact.data,'aliases':[*contact.data['aliases'],'老王']}


@pytest.mark.parametrize('message',[
    '给妈妈转400元  给老王转1500元  剩下的钱的20%用于理财  并给出推荐',
    '给妈妈转400元，给老王转1500元，剩余资金20%用于理财，并给出推荐',
    '给妈妈转400元给老王转1500元剩下的钱的20%用于理财',
    '先向我母亲汇款四百元，接着给王同学转一千五百元，剩余资金的百分之二十用于理财，并帮我推荐。',
    '先处理两笔转账：妈妈400元、小王1500元；剩余可用资金的五分之一用于理财，帮我推荐。',
])
def test_two_transfers_plus_fractional_wealth_keep_all_goals(client,headers,message):
    add_wang_alias(client)
    payroll(client,headers,450000)
    result=ask(client,headers,message)
    assert 'nodes' in result,result
    transfers=[n for n in result['nodes'] if n['operation']=='funds.transfer']
    assert [(n['params']['recipient_hint'],n['params']['amount_fen']) for n in transfers]==[('妈妈',40000),('小王',150000)],result
    assert transfers[0]['status']=='WAIT_CONFIRM' and transfers[1]['status']=='READY'
    assert any(n['operation']=='wealth.compare' for n in result['nodes']),result
    pending=next(g for g in result['request_goals'] if g['kind']=='bank_task')
    assert pending['pending_task']['params']=={'amount_fen':52000},pending
    assert not any('ETF' in g['title'] for g in result['request_goals'])
    assert client.app.state.bank.facts('u_demo')['account']['balance_fen']==450000
    result=finish(client,headers,result)
    assert any(n['operation']=='wealth.compare' and n['status']=='SUCCEEDED' for n in result['nodes'])
    assert result['status']=='WAIT_INPUT'
    assert client.app.state.bank.facts('u_demo')['account']['balance_fen']==260000
    selected=ask(client,headers,'我选择模拟30天产品',result['conversation_id'])
    assert selected['id']==result['id']
    assert [n['result']['receipt_id'] for n in selected['nodes'] if n['operation']=='funds.transfer']==[n['result']['receipt_id'] for n in result['nodes'] if n['operation']=='funds.transfer']
    purchase=next(n for n in selected['nodes'] if n['operation']=='wealth.purchase')
    assert purchase['params']=={'product_id':'P_LOCKED','amount_fen':52000}
    assert purchase['error'] and 'assess' in purchase['risk']['permitted_next_actions']
    assert client.app.state.bank.facts('u_demo')['account']['balance_fen']==260000
    with client.app.state.db.write() as session:
        session.get(User,'u_demo').risk_level=3
    response=client.post('/api/plans/'+selected['id']+'/resume',headers=headers,json={})
    assert response.status_code==200,response.text
    resumed=response.json()
    assert next(n for n in resumed['nodes'] if n['operation']=='wealth.purchase')['status']=='WAIT_MFA'
    assert client.app.state.bank.facts('u_demo')['account']['balance_fen']==260000


def test_incomplete_task_does_not_block_independent_known_transfer(client,headers):
    payroll(client,headers)
    intent=Intent(summary='两笔转账与待选理财',clarification='选哪款理财',clarification_kind='product',tasks=[
        {'domain':'funds','action':'funds.transfer','params':{'recipient_hint':'妈妈','amount_fen':40000}},
        {'domain':'wealth','action':'wealth.purchase','params':{'amount_fen':52000}}])
    result=client.app.state.graph.start('u_demo','两项独立办理',intent=intent)
    assert result['nodes'][0]['operation']=='funds.transfer' and result['nodes'][0]['status']=='WAIT_CONFIRM'
    assert next(g for g in result['request_goals'] if g['kind']=='bank_task')['pending_task']['params']['amount_fen']==52000


def test_top_level_question_for_existing_deferred_goal_does_not_halt_ready_tasks(client,headers):
    payroll(client,headers)
    intent=Intent(summary='办理与选品',clarification='你想选哪款产品？',clarification_kind='product',tasks=[
        {'domain':'funds','action':'funds.transfer','params':{'recipient_hint':'妈妈','amount_fen':40000}},
        {'domain':'wealth','action':'wealth.compare','params':{}}],additional_goals=[{
            'kind':'bank_task','title':'理财待选产品','pending_task':{'domain':'wealth','action':'wealth.purchase','params':{'amount_fen':52000}}}])
    result=client.app.state.graph.start('u_demo','转账与理财选品',intent=intent)
    assert result['nodes'][0]['status']=='WAIT_CONFIRM'
    assert any(n['operation']=='wealth.compare' for n in result['nodes'])


def test_unknown_contact_pauses_only_its_transfer_and_can_be_filled(client,headers):
    payroll(client,headers,450000)
    result=ask(client,headers,'给妈妈转400元，给老王转1500元，剩下的钱20%用于理财并给出推荐')
    assert [n['params']['amount_fen'] for n in result['nodes'] if n['operation']=='funds.transfer']==[40000]
    pending=[g for g in result['request_goals'] if g['kind']=='bank_task']
    assert len(pending)==2
    transfer=next(g for g in pending if g['pending_task']['action']=='funds.transfer')
    assert transfer['pending_task']['params']['amount_fen']==150000
    investment=next(g for g in pending if g['pending_task']['action']=='wealth.purchase')
    assert investment['pending_task']['params']['amount_fen']==52000
    selected=ask(client,headers,'我选择模拟现金产品',result['conversation_id'])
    assert not any(n['operation']=='wealth.purchase' for n in selected['nodes'])
    filled=ask(client,headers,'收款人选小王',result['conversation_id'])
    assert filled['id']==result['id']
    assert len([n for n in filled['nodes'] if n['operation']=='funds.transfer'])==2
    chosen=ask(client,headers,'我选择模拟现金产品',result['conversation_id'])
    assert next(n for n in chosen['nodes'] if n['operation']=='wealth.purchase')['params']['amount_fen']==52000
    assert client.app.state.bank.facts('u_demo')['account']['balance_fen']==450000


def test_product_reply_before_transfer_confirmation_keeps_transfer_nodes(client,headers):
    payroll(client,headers,450000)
    initial=ask(client,headers,'给妈妈转400元，剩下的钱20%用于理财并给出推荐')
    selected=ask(client,headers,'我选择模拟稳健产品',initial['conversation_id'])
    assert selected['id']==initial['id']
    transfer=next(n for n in selected['nodes'] if n['operation']=='funds.transfer')
    purchase=next(n for n in selected['nodes'] if n['operation']=='wealth.purchase')
    assert transfer['status']=='WAIT_CONFIRM' and purchase['status']=='READY'
    assert purchase['depends_on']==[transfer['local_id']]
    assert purchase['params']['amount_fen']==82000
    result=approve(client,headers,selected,transfer)
    assert result.status_code==200,result.text
    assert next(n for n in result.json()['nodes'] if n['operation']=='wealth.purchase')['status']=='WAIT_MFA'


def test_pending_investment_budget_can_change_without_replanning_transfers(client,headers):
    payroll(client,headers,450000)
    initial=ask(client,headers,'给妈妈转400元，剩下的钱20%用于理财并推荐')
    changed=ask(client,headers,'理财预算改成600元',initial['conversation_id'])
    assert changed['id']==initial['id']
    pending=next(g for g in changed['request_goals'] if g['kind']=='bank_task')
    assert pending['pending_task']['params']['amount_fen']==60000
    selected=ask(client,headers,'我选择模拟现金产品',initial['conversation_id'])
    assert next(n for n in selected['nodes'] if n['operation']=='wealth.purchase')['params']['amount_fen']==60000
    assert client.app.state.bank.facts('u_demo')['account']['balance_fen']==450000


def test_alternative_product_after_risk_block_reuses_original_purchase(client,headers):
    payroll(client,headers,450000)
    initial=finish(client,headers,ask(client,headers,'给妈妈转400元，剩下的钱20%用于理财并推荐'))
    blocked=ask(client,headers,'我选择模拟30天产品',initial['conversation_id'])
    old=next(n for n in blocked['nodes'] if n['operation']=='wealth.purchase')
    assert old['error']['code']=='RISK_MISMATCH'
    changed=ask(client,headers,'我选择模拟现金产品',initial['conversation_id'])
    new=next(n for n in changed['nodes'] if n['operation']=='wealth.purchase')
    assert changed['id']==initial['id'] and old['id']==new['id']
    assert new['params']['product_id']=='P_CASH' and new['params']['amount_fen']==82000
    assert new['status']=='WAIT_MFA'
    assert client.app.state.bank.facts('u_demo')['account']['balance_fen']==410000


@pytest.mark.parametrize('message',[
    '给妈妈转400元，分析本月账单',
    '给妈妈转400元，给小王转1500元，研究本月收支与现金流',
    '给妈妈转400元，分析本月账单，再开个视频会员',
    '给妈妈转400元，剩下的钱20%用于理财，并分析本月账单',
    '给妈妈转400元，再研究一下理财产品的流动性',
])
def test_transfers_and_independent_research_have_separate_progress(client,headers,message):
    payroll(client,headers)
    result=ask(client,headers,message)
    assert any(n['operation']=='funds.transfer' and n['status']=='WAIT_CONFIRM' for n in result['nodes'])
    goal=next(g for g in result['request_goals'] if g['kind']=='research')
    assert goal['result']['id'].startswith('research_')
    deadline=time.monotonic()+95
    while time.monotonic()<deadline:
        response=client.get('/api/research/'+goal['result']['id'],headers=headers).json()
        if response['status']!='RESEARCH_RUNNING':
            break
        time.sleep(.03)
    assert response['status']=='RESEARCH_READY',response
    assert response['report']['constraints']['analysis_only'] and not response['report']['options']
    assert client.app.state.bank.facts('u_demo')['account']['balance_fen']==600000


@pytest.mark.parametrize('budget',['剩下的钱的120%用于理财','-500元用于理财'])
def test_invalid_investment_goal_does_not_swallow_valid_transfer(client,headers,budget):
    payroll(client,headers)
    result=ask(client,headers,'给妈妈转400元，'+budget)
    assert result['nodes'][0]['status']=='WAIT_CONFIRM'
    assert not any(n['operation']=='wealth.purchase' for n in result['nodes'])
    assert any(g['title']=='理财预算待核对' for g in result['request_goals'])


def test_local_wealth_does_not_gain_an_unasked_etf_boundary(client):
    from bank_agent.intake import ensure_goal_coverage
    intent=Intent(summary='理财',additional_goals=[{'kind':'capability_gap','title':'ETF资料可查询',
        'message':'证券自动定投未接入'}])
    result=ensure_goal_coverage(intent,'给妈妈转400元，剩下的钱20%用于理财')
    assert not any('ETF' in g.title for g in result.additional_goals)
