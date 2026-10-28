import json

import httpx
import pytest

from bank_agent.intent_constraints import constrain_intent, forbidden_operations
from bank_agent.resources import subscription_grounding
from bank_agent.research import ResearchService
from bank_agent.research_schemas import ResearchBrief
from bank_agent.schemas import Intent
from conftest import finish


@pytest.mark.parametrize("text", [
    "哪些扣款可能是订阅？先查一下，别取消。", "识别订阅候选，不要取消", "看看哪些是订阅但不要取消",
])
def test_subscription_discovery_never_creates_cancellation(client, headers, text):
    response = client.post('/api/chat', headers=headers, json={'message': text})
    assert response.status_code == 200, response.text
    plan = response.json()
    assert [n['operation'] for n in plan['nodes']] == ['subscriptions.detect']
    assert plan['status'] == 'COMPLETED'
    assert 'subscriptions.cancel' in plan['review']['protected']['forbidden_operations']


def test_prohibition_gate_removes_model_write_before_dag():
    intent = Intent(summary='错误草案', tasks=[{'domain':'subscriptions','action':'subscriptions.detect'},
        {'domain':'subscriptions','action':'subscriptions.cancel','params':{'subscription_id':'S_VIDEO'}}])
    safe = constrain_intent(intent, '先查询订阅，别取消')
    assert [t.action for t in safe.tasks] == ['subscriptions.detect']
    assert forbidden_operations('不要赎回，也别取消订阅') == ['subscriptions.cancel','wealth.redeem']


def test_named_negative_clause_does_not_cancel_other_resources():
    intent = subscription_grounding('取消视频会员，别取消音乐会员')
    assert intent.tasks[0].params['subscription_id'] == '视频会员'
    assert 'subscriptions.cancel' not in forbidden_operations('取消视频会员，别取消音乐会员')


@pytest.mark.parametrize('query,scope', [
    ('帮我把视频会员取消掉，银行代扣和商户自动续费都停掉。','both'),
    ('只撤销视频会员的银行代扣，保留商户合同','bank'),
    ('只取消视频会员的商户合同，保留银行代扣','merchant'),
    ('视频会员只停银行卡自动代扣，商户会员合同别动。','bank'),
    ('视频会员不支持银行这边退订的话，先停这张卡的代扣，告诉我接下来去哪取消会员。','bank'),
])
def test_subscription_name_and_cancellation_scope_close_through_gateway(client, headers, query, scope):
    response = client.post('/api/chat',headers=headers,json={'message':query})
    assert response.status_code == 200, response.text
    plan = finish(client,headers,response.json())
    assert plan['status'] == 'COMPLETED'
    data = next(n['result']['data'] for n in plan['nodes'] if n['operation']=='subscriptions.cancel')
    assert (data['bank_mandate']=='REVOKED') == (scope in {'bank','both'})
    assert (data['merchant_contract']=='CANCELLED') == (scope in {'merchant','both'})


def test_reminder_preserves_amount_contract_and_negative_instruction(client, headers):
    plan = client.post('/api/chat',headers=headers,json={'message':'音乐会员提前3天提醒就行，不要取消也别改扣款金额。'}).json()
    assert not any(n['operation']=='subscriptions.cancel' for n in plan['nodes'])
    done = finish(client,headers,plan)
    result = next(n['result']['data'] for n in done['nodes'] if n['operation']=='subscriptions.remind')
    assert result['days_before']==3 and result['amount_fen']==1900
    assert result['merchant_contract']==result['bank_mandate']=='ACTIVE'


@pytest.mark.parametrize('query', [
    '深入比较产品，拟投500元，不要直接购买。',
    '我有500元闲钱，2026年10月14日必须用到，深入比较产品，不要替我买。',
    '计划投资五百元，比较理财产品',
])
def test_explicit_research_budget_survives_missing_model_slot(query):
    brief = ResearchBrief(topic='combined')
    ResearchService._ground(brief,query,'2026-10-13T09:00:00+08:00')
    assert brief.amount_fen==50000


def test_account_balance_cannot_be_invented_as_purchase_budget():
    brief = ResearchBrief(amount_fen=50000)
    from bank_agent.errors import BankError
    with pytest.raises(BankError):
        ResearchService._ground(brief,'我余额500元，想比较产品','2026-10-13T09:00:00+08:00')


def test_financial_calculation_uses_supplied_inputs_not_benchmark_constants(client, headers):
    answer = client.post('/api/chat',headers=headers,json={'message':'账单20000元分10期，每月还本金2000元、手续费80元，真实年化是多少？不要替我办理分期。'}).json()
    assert answer['status']=='ANSWERED'
    assert '800元' in answer['message'] and '4.00%' in answer['message'] and 'IRR' in answer['message']
    assert '尾差' in answer['message'] and '没有办理' in answer['message']


def test_negated_alternative_tenor_cannot_replace_requested_tenor():
    from bank_agent.information import information_intent
    intent = information_intent('查人民币2年期国债收益率，不要拿1年或3年顶替')
    assert intent.information_query.tenor=='2年'


def test_new_judge_protocol_keeps_atomic_gates_and_distinguishes_regions():
    from evaluation.servicebench.judge import system_prompt
    from evaluation.servicebench.rubric import PROMPT
    prompt = system_prompt(revision='v3')
    assert PROMPT in prompt
    assert 'assistant_reply' in prompt and '不是助手重复追问两次' in prompt
    assert 'independent_ledger' in prompt
    v4 = system_prompt(revision='v4')
    assert '$ref' not in v4 and 'scores的五个维度每个都是' in v4
    v5 = system_prompt(revision='v5')
    assert '顶层且仅有五个字段' in v5 and 'failure_modes、confidence、summary都与scores同级' in v5
    assert len(v5) < len(v4)


def test_aa_initiator_is_grounded_only_when_user_explicitly_participates(client):
    planner=client.app.state.graph.planner
    intent=Intent(summary='AA',tasks=[{'domain':'funds','action':'aa.create','params':{
        'amount_fen':10000,'participants':['u_wang','u_li'],'subject':'晚餐'}}])
    corrected=planner._reconcile('u_demo',None,None,intent,'我和小王小李三人AA，共100元，由我垫付')
    assert corrected.tasks[0].params['participants']==['u_demo','u_wang','u_li']
    intent.tasks[0].params['participants']=['小王','小李']
    named=planner._reconcile('u_demo',None,None,intent,'我和小王小李三人AA，共100元，由我垫付')
    assert named.tasks[0].params['participants']==['u_demo','u_wang','u_li']
    intent.tasks[0].params['participants']=['u_wang','u_li']
    denied=planner._reconcile('u_demo',None,None,intent,'我不参加分摊，给小王小李发AA收款')
    assert denied.clarification and not denied.tasks


def test_truncated_plan_recovers_once_without_writing_before_confirmation(client, headers):
    from dataclasses import replace
    planner = client.app.state.graph.planner
    planner.settings = replace(planner.settings,model_mode='llm',model_api_key='synthetic-test-key',model_thinking=True)
    planner.model.settings = planner.settings
    seen = []
    def provider(request):
        seen.append(json.loads(request.content))
        if len(seen)==1:
            return httpx.Response(200,json={'choices':[{'finish_reason':'length','message':{'content':'{"summary":'}}]})
        return httpx.Response(200,json={'choices':[{'finish_reason':'stop','message':{'content':json.dumps({
            'summary':'查询余额','tasks':[{'domain':'funds','action':'balance.query'}]})}}]})
    planner.model.http_transport = httpx.MockTransport(provider)
    result = client.post('/api/chat',headers=headers,json={'message':'我的账户还有多少钱？'})
    assert result.status_code==200, result.text
    assert len(seen)==2
    assert seen[0]['thinking']['type']=='enabled' and seen[0]['reasoning_effort']=='max'
    assert seen[1]['thinking']['type']=='disabled' and 'tools' not in seen[1]
    assert result.json()['nodes'][0]['result']['data']['balance_fen']==0


def test_model_deadline_uses_only_explicit_grounded_subscription_proposal(client, headers):
    from dataclasses import replace
    from bank_agent.errors import BankError
    planner=client.app.state.graph.planner
    planner.settings=replace(planner.settings,model_mode='llm',model_api_key='synthetic')
    async def deadline(*args):
        raise BankError('MODEL_DEADLINE_EXCEEDED','synthetic deadline',502)
    planner.model.astructured=deadline
    plan=client.post('/api/chat',headers=headers,json={'message':'视频会员只停银行卡自动代扣，商户会员合同别动。'}).json()
    assert plan['status']=='WAIT_CONFIRM'
    assert not any(n.get('result') for n in plan['nodes'])
    done=finish(client,headers,plan)
    result=next(n['result']['data'] for n in done['nodes'] if n['operation']=='subscriptions.cancel')
    assert result['bank_mandate']=='REVOKED' and result['merchant_contract']=='ACTIVE'
