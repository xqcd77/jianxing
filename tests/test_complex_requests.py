"""Expert acceptance scenarios: only observable goals, ledger and dependencies score."""
import json
from pathlib import Path

import pytest
from sqlalchemy import select

from bank_agent.storage import Transaction, documents
from conftest import finish, payroll, start
from test_public_information import ask, source
from test_request_goal_repairs import ETF_DIRECTORY

CASES = json.loads((Path(__file__).parents[1] / 'evaluation/cases/complex_requests_10.json').read_text(encoding='utf-8'))


def transfers(plan):
    return [(n['params']['recipient_hint'], n['params']['amount_fen']) for n in plan.get('nodes', [])
            if n['operation'] == 'funds.transfer']


def goals(result):
    return result.get('request_goals', [])


def advisory(result):
    return next(g for g in goals(result) if g['kind'] == 'public_information')


@pytest.mark.parametrize('case', CASES, ids=lambda c: c['id'])
def test_complex_request_acceptance(client, headers, case):
    source(client, funds=ETF_DIRECTORY)
    cid = case['id']
    payroll(client, headers, 450000 if cid == 'C06' else 600000)
    if cid == 'C04':
        finish(client, headers, start(client, 'funds.reserve', {'amount_fen':100000, 'purpose':'不可挪用的应急金'}))
    with client.app.state.db.read() as session:
        before_transfers = len(list(session.scalars(select(Transaction).where(Transaction.kind=='TRANSFER'))))
    result = ask(client, headers, case['message'])
    assert not result['status'].startswith('RESEARCH_'), result
    assert result['status'] != 'REJECTED', result
    with client.app.state.db.read() as session:
        assert len(list(session.scalars(select(Transaction).where(Transaction.kind == 'TRANSFER')))) == before_transfers
        assert not documents(session, 'research', 'u_demo')
    if cid in {'C01','C07','C08'}:
        assert transfers(result) == [('妈妈',50000)], result
        assert len(goals(result)) == 3
        assert advisory(result)['query']['budget_fen'] == 100000
        assert any('会员' in g['title'] for g in goals(result))
    elif cid == 'C02':
        text = result.get('message', result.get('assistant_message',''))
        assert not transfers(result), result
        assert '1500' in text.replace(',','') and '共用' in text and '虚拟' in text and '实体' in text
        result = ask(client, headers, '虚拟卡', result['conversation_id'])
        assert [n['operation'] for n in result['nodes']] == ['cards.apply'], result
        assert result['nodes'][0]['params']['kind'] == 'virtual'
        assert any('1500' in (g.get('notice','')+g['title']).replace(',','') for g in goals(result))
    elif cid in {'C03','C04'}:
        assert transfers(result) == [('妈妈',300000 if cid == 'C03' else 125000)], result
        assert result['status'] == 'WAIT_MFA'
        assert all(n['operation'] != 'funds.release' for n in result['nodes'])
    elif cid == 'C05':
        assert transfers(result) == [('妈妈',50000),('小王',275000)], result
    elif cid == 'C06':
        assert transfers(result) == [('妈妈',120000)], result
        budget = advisory(result)['result']['investment_budget']
        assert (budget['budget_fen'],budget['periodic_amount_fen'],budget['full_contributions']) == (330000,20000,16)
    elif cid == 'C09':
        assert [n['operation'] for n in result['nodes']] == ['cards.apply','cards.restrict'], result
        result = finish(client, headers, result)
        created = result['nodes'][0]['result']['data']['card_id']
        assert result['nodes'][1]['params']['card_id'] == created != 'C_MAIN'
        assert result['nodes'][1]['result']['data']['daily_limit_fen'] == 150000
    elif cid == 'C10':
        assert transfers(result) == [('小王',30000)], result
        assert any(n['operation'] == 'balance.query' for n in result['nodes'])
        assert advisory(result)['query']['asset_type'] == 'etf'
    if cid in {'C07','C08'}:
        original_id = result['id']
        if cid == 'C08':
            result = finish(client, headers, result)
        if cid == 'C07':
            for _ in range(7):
                ask(client,headers,'谢谢',result['conversation_id'])
        result = ask(client, headers, case['followup'], result['conversation_id'])
        assert result['id'] == original_id, result
        assert transfers(result) == [('妈妈',50000)]
        assert advisory(result)['query']['budget_fen'] == (80000 if cid == 'C07' else 100000)
        assert any('会员' in g['title'] for g in goals(result))
        refreshed = client.get('/api/plans/'+original_id, headers=headers).json()
        from bank_agent.contracts import RequestGoal
        assert refreshed['request_goals'] == [RequestGoal.model_validate(g).model_dump() for g in result['request_goals']]
        with client.app.state.db.read() as session:
            tx = list(session.scalars(select(Transaction).where(Transaction.kind=='TRANSFER')))
            assert len(tx) == before_transfers + (1 if cid=='C08' else 0)
    assert all(n['operation'] != 'wealth.purchase' for n in result.get('nodes',[]))


@pytest.mark.parametrize('message',[
    '给妈妈转500元并取消视频会员',
    '把一半的钱转给妈妈并取消视频会员',
    '如果工资到账就把一半的钱转给妈妈',
    '只分析把一半的钱转给妈妈会剩多少，不要操作',
    '给妈妈转500元还要向同学收AA款',
])
def test_shortcut_never_swallows_unparsed_conditions_or_goals(client,message):
    from bank_agent.request_understanding import understand_request
    assert understand_request(message,client.app.state.bank.facts('u_demo')) is None


def test_goal_followup_is_scoped_to_owner_and_current_plan_state(client,headers):
    from bank_agent.dialogue import read_context
    from conftest import login
    source(client,funds=ETF_DIRECTORY)
    payroll(client,headers)
    result = ask(client,headers,CASES[7]['message'])
    completed = finish(client,headers,result)
    context = read_context(client.app.state.db,'u_demo',result['conversation_id'],'会员选视频的',client.app.state.bank.facts('u_demo'))
    assert next(g for g in context['request_goals'] if g['kind']=='bank_operation')['status']=='SUCCEEDED'
    other = ask(client,login(client,'wang'),'ETF预算改成800元',result['conversation_id'])
    assert other.get('id') != result['id']
    current = client.get('/api/plans/'+completed['id'],headers=headers).json()
    assert advisory(current)['query']['budget_fen']==100000


def test_new_card_with_put_money_paraphrase_preserves_both_goals(client,headers):
    payroll(client,headers)
    result=ask(client,headers,'麻烦办张虚拟银行卡，并把1500块放到这张新卡里面')
    assert [n['operation'] for n in result['nodes']]==['cards.apply']
    assert result['nodes'][0]['params']['kind']=='virtual'
    assert len(goals(result))==2
    assert '1500' in goals(result)[1]['title'].replace(',','')


def test_legacy_two_goal_parser_does_not_drop_third_unpunctuated_goal(client):
    from bank_agent.request_goals import split_transfer_information
    assert split_transfer_information('我的4500元给妈转1200元剩下推荐ETF再开通视频会员',client.app.state.bank.facts('u_demo')) is None


def test_membership_followup_cannot_revalidate_stale_investment_budget(client,headers):
    source(client,funds=ETF_DIRECTORY)
    payroll(client,headers)
    result=ask(client,headers,CASES[0]['message'])
    node=result['nodes'][0]
    edited=client.post('/api/plans/'+result['id']+'/edit',headers=headers,json={
        'node_id':node['id'],'params':{**node['params'],'amount_fen':60000},'expected_version':result['version']})
    assert edited.status_code==200,edited.text
    assert advisory(edited.json())['status']=='NEEDS_RECHECK'
    followed=ask(client,headers,'会员选视频的，按月付',result['conversation_id'])
    assert transfers(followed)==[('妈妈',60000)]
    assert advisory(followed)['status']=='NEEDS_RECHECK'
    refreshed=ask(client,headers,'ETF预算改成800元',result['conversation_id'])
    assert advisory(refreshed)['query']['budget_fen']==80000
    assert advisory(refreshed)['status']!='NEEDS_RECHECK'
    assert '视频' in next(g['title'] for g in goals(refreshed) if g['kind']=='capability_gap')


@pytest.mark.parametrize('value',[
    {'kind':'public_information','title':'ETF'},
    {'kind':'capability_gap','title':'会员','information_query':{'topic':'fund_products'}},
])
def test_goal_kind_and_query_cannot_disagree(value):
    from pydantic import ValidationError
    from bank_agent.schemas import AdditionalGoal
    with pytest.raises(ValidationError):
        AdditionalGoal.model_validate(value)


@pytest.mark.parametrize('message,operation,kind',[
    ('申请一张虚拟卡，再推荐ETF','cards.apply','bank_operation'),
    ('推荐ETF，再订一个视频会员',None,'capability_gap'),
])
def test_public_information_route_cannot_swallow_another_goal(client,headers,message,operation,kind):
    source(client,funds=ETF_DIRECTORY)
    result=ask(client,headers,message)
    assert len(goals(result))==2,result
    assert any(g['kind']==kind for g in goals(result))
    assert advisory(result)['query']['asset_type']=='etf'
    if operation:
        assert [n['operation'] for n in result['nodes']]==[operation]


def test_original_two_goal_request_uses_same_persistent_followup(client,headers):
    source(client,funds=ETF_DIRECTORY)
    payroll(client,headers,450000)
    result=ask(client,headers,'我的4500元 给妈转1200 剩下的给我推荐一款ETF产品')
    assert advisory(result)['query']['budget_fen']==330000
    changed=ask(client,headers,'ETF预算改成2000元',result['conversation_id'])
    assert changed['id']==result['id']
    assert transfers(changed)==[('妈妈',120000)]
    assert advisory(changed)['query']['budget_fen']==200000
