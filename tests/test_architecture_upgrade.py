"""Cross-component acceptance checks for the architecture consolidation."""
from dataclasses import replace

from bank_agent.capabilities import REGISTRY, selected_capabilities
from bank_agent.intake import request_manifest
from bank_agent.schemas import Intent
from bank_agent.supervisor_agents import BusinessModel
from conftest import finish, login, payroll
from test_public_information import ask, source
from test_request_goal_repairs import ETF_DIRECTORY


def compound(client, headers):
    source(client, funds=ETF_DIRECTORY)
    payroll(client, headers)
    return ask(client, headers, '给妈妈转500元，拿1000元看看ETF，再开个视频会员')


def test_manifest_and_intake_preserve_three_goals(client, headers):
    result = compound(client, headers)
    assert {g['kind'] for g in request_manifest('给妈妈转500元，拿1000元看看ETF，再开个视频会员')} == {'transfer','etf','membership'}
    assert len(result['request_goals']) == 3
    assert result['nodes'][0]['params']['amount_fen'] == 50000
    assert client.get('/api/overview',headers=headers).json()['account']['balance_fen'] == 600000


def test_goal_relative_patch_keeps_identity_and_completed_receipt(client, headers):
    result = compound(client, headers)
    finished = finish(client, headers, result)
    before = [g['id'] for g in finished['request_goals']]
    changed = ask(client, headers, '第二个少花两百', result['conversation_id'])
    assert [g['id'] for g in changed['request_goals']] == before
    assert changed['request_goals'][1]['query']['budget_fen'] == 80000
    assert changed['request_goals'][0]['status'] == 'SUCCEEDED'
    assert client.get('/api/overview',headers=headers).json()['account']['balance_fen'] == 550000
    cancelled = ask(client, headers, '第三个先别办', result['conversation_id'])
    assert cancelled['request_goals'][2]['status'] == 'CANCELLED'
    assert cancelled['request_goals'][1]['query']['budget_fen'] == 80000


def test_goal_api_rejects_stale_version_and_other_owner(client, headers):
    result = compound(client, headers)
    path = '/api/conversations/' + result['conversation_id'] + '/goals'
    agenda = client.get(path,headers=headers).json()
    patch = {'target_id':agenda['goals'][1]['id'],'expected_revision':agenda['revision'], 'fields':{'budget_fen':80000}}
    assert client.post(path,headers=headers,json=patch).status_code == 200
    assert client.post(path,headers=headers,json=patch).status_code == 409
    assert client.get(path,headers=login(client,'wang')).status_code == 404
    assert client.post(path,headers=headers,json={**patch,'expected_revision':agenda['revision']+1,'fields':{'budget_fen':-1}}).status_code == 400


def test_pure_information_goal_can_be_changed_without_bank_plan(client, headers):
    source(client, funds=ETF_DIRECTORY)
    result = ask(client, headers, '我的6000元，推荐ETF，每天定投200元')
    changed = ask(client, headers, 'ETF预算改成800元', result['conversation_id'])
    assert not changed.get('nodes')
    assert changed['request_goals'][0]['query']['budget_fen'] == 80000


def test_bank_goal_edit_invalidates_old_approval_and_cancel_is_local(client, headers):
    result = compound(client, headers)
    old = result['nodes'][0]
    changed = ask(client, headers, '第一个改成400元', result['conversation_id'])
    assert changed['nodes'][0]['params']['amount_fen'] == 40000
    stale = client.post('/api/plans/'+result['id']+'/approve',headers=headers,
        json={'node_id':old['id'],'fingerprint':old['fingerprint']})
    assert stale.status_code == 409
    cancelled = ask(client, headers, '第一个先别办', result['conversation_id'])
    assert cancelled['nodes'][0]['status'] == 'CANCELLED'
    assert cancelled['request_goals'][1]['status'] != 'CANCELLED'
    assert client.get('/api/overview',headers=headers).json()['account']['balance_fen'] == 600000


def test_ambiguous_goal_reference_does_not_guess(client, headers):
    result = compound(client, headers)
    reply = ask(client, headers, '那个先别办', result['conversation_id'])
    assert reply['status'] == 'NEEDS_CLARIFICATION'
    assert client.get('/api/plans/'+result['id'],headers=headers).json()['nodes'][0]['status'] == 'WAIT_CONFIRM'


def test_catalog_covers_all_requested_domains_and_reduces_prompt(client):
    settings = replace(client.app.state.settings,model_api_key='test-only')
    model = BusinessModel(settings)
    short = model._request('planner',{'message':'给妈妈转500元'},Intent)
    mixed = selected_capabilities({'message':'给妈妈转500元，取消视频会员'})
    assert {c['owner'] for c in mixed} == {'funds','subscriptions'}
    assert len(short['messages'][0]['content']) < 9000
    assert 'wealth.purchase' not in short['messages'][0]['content']
    for op,cap in REGISTRY.items():
        metadata = cap.describe()
        assert metadata['enabled'] and metadata['schema']['type']=='object'
        assert metadata['preconditions'] and metadata['failure_policy']


def test_remaining_etf_budget_and_preferences_are_grounded_from_user_source(client):
    from bank_agent.schemas import AdditionalGoal, PublicInformationQuery
    intent=Intent(summary='模型预算遗漏',tasks=[{'domain':'funds','action':'funds.transfer',
        'params':{'recipient_hint':'妈妈','amount_fen':120000}}],additional_goals=[AdditionalGoal(
            kind='public_information',title='ETF',information_query=PublicInformationQuery(
                topic='fund_products',asset_type='etf',risk_level=2,channel='exchange'))])
    result=client.app.state.graph.planner._reconcile('u_demo','grounding',None,intent,
        '我的4500元给妈转1200元剩下推荐ETF再开通视频会员')
    query=result.additional_goals[0].information_query
    assert query.budget_fen==330000 and query.risk_level is None and query.channel is None
    selected={c['owner'] for c in selected_capabilities({'message':'请把五百块汇给我妈，同时挑ETF和视频会员'})}
    assert selected >= {'funds','subscriptions'} and 'wealth' not in selected


def test_recommendation_boundary_does_not_invent_an_extra_purchase_goal():
    from bank_agent.intake import ensure_goal_coverage
    intent=Intent(summary='候选',tasks=[{'domain':'funds','action':'funds.transfer','params':{'recipient_hint':'妈妈','amount_fen':120000}}],
        additional_goals=[{'kind':'public_information','title':'ETF资料','information_query':{'topic':'fund_products','asset_type':'etf'}},
                          {'kind':'capability_gap','title':'视频会员开通','source_text':'开通视频会员'},
                          {'kind':'capability_gap','title':'ETF购买通道','source_text':'剩下推荐ETF','message':'尚未接入证券购买通道'}])
    result=ensure_goal_coverage(intent,'我的4500元给妈转1200元剩下推荐ETF再开通视频会员')
    assert len(result.additional_goals)==2 and '尚未接入' in result.additional_goals[0].message


def test_independent_member_clarification_cannot_block_explicit_transfer(client):
    from bank_agent.schemas import AdditionalGoal
    candidate=Intent(summary='会员缺项',clarification='请说明视频会员的平台。',clarification_kind='business',
        additional_goals=[AdditionalGoal(kind='capability_gap',title='视频会员开通',source_text='再开通视频会员')])
    result=client.app.state.graph.planner._reconcile('u_demo','source-coverage',None,candidate,
        '我的4500元给妈转1200元剩下推荐ETF再开通视频会员')
    assert result.clarification is None
    assert result.tasks[0].action=='funds.transfer' and result.tasks[0].params['amount_fen']==120000
    assert len(result.additional_goals)==2


def test_goal_patch_reuses_gateway_schema_for_card_fields_and_blocks_server_fields(client,headers):
    from conftest import start
    plan=start(client,'cards.restrict',{'single_limit_fen':50000,'daily_limit_fen':100000})
    # Direct graph callers and HTTP intake both create the same durable agenda.
    changed=ask(client,headers,'第一个每日限额改成800元',plan['conversation_id'])
    assert changed['nodes'][0]['params']['daily_limit_fen']==80000
    assert changed['nodes'][0]['params']['single_limit_fen']==50000
    path='/api/conversations/'+plan['conversation_id']+'/goals'
    agenda=client.get(path,headers=headers).json()
    bad=client.post(path,headers=headers,json={'target_id':agenda['goals'][0]['id'],
        'expected_revision':agenda['revision'],'fields':{'quote':{'total_fen':1}}})
    assert bad.status_code==400


def test_goal_migration_preserves_identity_and_is_idempotent(client):
    from bank_agent.migrations import migrate_goal_state
    from bank_agent.storage import Document,save_document
    db=client.app.state.db
    original={'conversation_id':'old-cid','plan_id':None,'goals':[{'id':'stable-old-id','kind':'capability_gap',
        'title':'开通会员','status':'UNSUPPORTED'}]}
    with db.write() as session:
        marker=session.get(Document,'migration_request_runtime_v1')
        session.delete(marker)
        save_document(session,'goal_agenda_u_demo_old-cid','goal_agenda','u_demo',original)
    migrate_goal_state(db)
    migrate_goal_state(db)
    with db.read() as session:
        agenda=session.get(Document,'goal_agenda_u_demo_old-cid').data
        assert agenda['revision']==1 and agenda['goals'][0]['revision']==1
        assert agenda['goals'][0]['id']=='stable-old-id' and agenda['goals'][0]['status']=='UNSUPPORTED'
    assert db.diagnostics()['writer_wait']['sample_count']>0
