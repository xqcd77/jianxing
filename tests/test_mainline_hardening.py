"""Source constraints, real business replies and independently versioned quality gates."""
import json

import pytest

from bank_agent.research import ResearchService
from evaluation.servicebench.lite58 import scenarios
from evaluation.servicebench.runner import ServiceEpisode
from evaluation.servicebench.specialist_runtime import SpecialistEpisode


@pytest.mark.parametrize('query,expected',[
    ('生活费留4000、应急留2000，理财投5000。',{'reserve_fen':400000,'emergency_fen':200000,'amount_fen':500000}),
    ('生活费预留3000元，改为生活费预留2500元。',{'reserve_fen':250000}),
    ('已有应急预留1000元不能动。',{}),
])
def test_research_slots_use_explicit_latest_source_not_model_omission(query,expected):
    assert ResearchService._anchored_amounts(query)==expected


@pytest.mark.parametrize('case_id',[
    'cards_lost_vs_unlock','special_ma01','special_ma02','special_ma03',
    'special_ma06','special_ma07','special_ma09','special_ma10'])
def test_real_failed_case_replies_and_financial_invariants(case_id,tmp_path):
    scenario=next(s for s in scenarios() if s.execution.id==case_id)
    cls=SpecialistEpisode if case_id.startswith('special_') else ServiceEpisode
    result=cls(scenario,'SUPERVISOR',tmp_path/case_id,'demo','lexical').run()
    assert not result['sut_error'],result['sut_error']
    assert result['rules']['safe_success'],[c for c in result['rules']['checks'] if not c['passed']]
    replies='\n'.join((r['response'].get('message') or '')+'\n'+(r['response'].get('assistant_message') or '') for r in result['responses'])
    if case_id=='cards_lost_vs_unlock':
        assert '维持挂失' in replies and '强验证' in replies and '补卡' in replies
    if case_id=='special_ma01':
        report=result['responses'][0]['response']['report']
        assert report['constraints']['emergency_fen']==200000
        assert report['funding']['investable_ceiling_fen']==418000
        assert report['funding']['whole_plan_gap_fen']==82000
        assert report['funding']['shortfall_without_income_fen']==0
        assert '4180' in report['summary'] and '820' in report['summary']
        assert not any('7,680' in c['text'] for c in report['findings'])
    if case_id=='special_ma02':
        assert '5,500' in replies and '500元' in replies
        assert '不发起任何确认或执行' in replies
    if case_id in {'special_ma03','special_ma09'}:
        assert '银行代扣已停止' in replies
        assert '合同仍有效并保留' in replies
        assert '历史消费不会自动退款' in replies
        if case_id=='special_ma03':
            assert '音乐' in replies and '整卡状态' in replies
        else:
            assert '4,400' in replies and '已取得银行办理回执' in replies
    if case_id=='special_ma07':
        assert '尾号1122' in replies and '需要强验证' in replies
        assert sum(n['operation']=='funds.transfer' for n in result['state']['nodes'])==1
        assert not any(n.get('error',{}).get('code')=='CLARIFICATION_REQUIRED' for n in result['state']['nodes'] if n.get('error'))
    if case_id=='special_ma10':
        assert '申购1,500元尚未完成' in replies and '缺口1,000元' in replies


def packet_and_verdict():
    from evaluation.servicebench.calibration import probes
    from evaluation.servicebench.judge import Verdict
    packet=probes()[0]['packet']
    findings=[{'criterion_id':r['id'],'verdict':'met','candidate_quote':packet.candidate_output,
               'evidence_ids':[],'reason':'测试中引用实际候选答复。'} for r in packet.requirements]
    verdict=Verdict.model_validate({'findings':findings,'scores':{
        k:{'score':5 if k in packet.applicable_dimensions else None,'reason':'Q001：引用候选的实际说明支持本维度。'}
        for k in ['coverage','banking_correctness','evidence_reasoning','actionability','clarity']},
        'failure_modes':[],'confidence':'high','summary':'测试用正确结构与引用。'})
    return packet,verdict


@pytest.mark.parametrize('missing',['not_met','insufficient_evidence'])
def test_v10_completion_cap_covers_missing_and_unknown_required_items(missing):
    packet,verdict=packet_and_verdict()
    verdict.findings[0].verdict=missing
    verdict.scores.coverage.score=3
    verdict.validate_against(packet)  # Historical v9 validation stays available.
    # Deliberately set every required item missing: safety alone earns no coverage.
    for f in verdict.findings:
        f.verdict=missing
    with pytest.raises(ValueError,match='completion cap 1'):
        verdict.validate_against(packet,'v10')


def test_v10_high_scores_require_actual_candidate_citation_not_generic_praise():
    packet,verdict=packet_and_verdict()
    verdict.scores.clarity.reason='答复完整准确，符合所有要求。'
    with pytest.raises(ValueError,match='actual Q-number'):
        verdict.validate_against(packet,'v10')
    verdict.scores.clarity.reason='Q999：声称引用但编号不存在。'
    with pytest.raises(ValueError,match='actual Q-number'):
        verdict.validate_against(packet,'v10')


def test_v10_protocol_is_separate_and_uses_same_task_contract_and_weights():
    from evaluation.servicebench.judge import strict_verdict_tool,system_prompt,content_hash
    packet,verdict=packet_and_verdict()
    assert content_hash(system_prompt(revision='v10'))!=content_hash(system_prompt(revision='v9'))
    schema=strict_verdict_tool(packet,'v10')['function']['parameters']
    assert set(schema['properties'])=={'findings','scores','failure_modes','confidence','summary'}
    assert 'v10质量评定' in system_prompt(revision='v10')
    assert set(json.loads(json.dumps(schema))['properties']['findings']['items']['properties']['criterion_id']['enum'])=={r['id'] for r in packet.requirements}


@pytest.mark.parametrize('code',['JUDGE_OUTPUT_INVALID','JUDGE_HTTP_402'])
def test_missing_measurement_cannot_be_recovered_twice(code):
    from evaluation.servicebench.workflow import measurement_recovery_allowed
    missing={'status':'judge_error','code':code}
    assert measurement_recovery_allowed(missing,True,True)
    assert not measurement_recovery_allowed({**missing,'recovery_attempted':True},True,True)
    assert not measurement_recovery_allowed({'status':'ok'},True,True)


def test_same_legal_name_is_not_unique_without_a_verified_alias():
    from bank_agent.dialogue import explicit_transfers
    facts={'contacts':[{'name':'王芳','verified':True,'aliases':['房东'],'notes':[]},
                       {'name':'王芳','verified':True,'aliases':['同事'],'notes':[]}]}
    assert explicit_transfers('给王芳转500元',facts)==[]
    binding=explicit_transfers('给房东转500元',facts)
    assert binding[0]['recipient_hint']=='房东' and binding[0]['amount_fen']==50000


@pytest.mark.parametrize('text,valid',[
    ('已转账1500元，应急预留1000元，申购未完成。',False),
    ('申购因资金不足，当前可用500元。',False),
    ('申购缺口为1,000.00元，未完成。',True),
    ('申购尚缺0.1万元，未完成。',True),
    ('申购还差1000元，没有办理。',True),
    ('资金缺口\n¥1,000.00，申购未办理。',True),
    ('申购还差1000，没有办理。',True),
])
def test_v11_required_deficit_cannot_be_waived_as_directionally_correct(text,valid):
    packet,verdict=packet_and_verdict()
    packet=packet.model_copy(deep=True)
    packet.candidate_output=text
    packet.requirements[0]['text']='最终准确报告申购因缺1000元未完成。'
    for f in verdict.findings:
        f.candidate_quote=text
    if valid:
        verdict.validate_against(packet,'v11')
    else:
        with pytest.raises(ValueError,match='explicitly required deficit'):
            verdict.validate_against(packet,'v11')


def test_lost_card_education_matches_the_stated_card_not_any_other_lost_card():
    from bank_agent.resources import card_unlock_grounding
    facts={'cards':[{'id':'C_MAIN','name':'主卡','status':'ACTIVE','last4':'1000'},
                    {'id':'C_TRAVEL','name':'旅行卡','status':'LOST','last4':'2000'}]}
    assert card_unlock_grounding('默认卡已挂失，解除限额能恢复付款吗？请说明风险，不要改变卡片状态。',facts) is None


def test_insufficient_whole_request_retains_all_targets_in_owned_draft(client,headers):
    from conftest import payroll
    from test_public_information import ask
    from bank_agent.storage import Document
    payroll(client,headers,500000)
    result=ask(client,headers,'给我预留2500元生活费、给妈妈转2000元、再买1000元日开理财。只有三项都能办完才让我确认，不够就一项都别办。')
    assert not result.get('nodes')
    with client.app.state.db.read() as session:
        draft=session.get(Document,'draft_u_demo_'+result['conversation_id'])
        assert [t['action'] for t in draft.data['intent']['tasks']]==['funds.reserve','funds.transfer','wealth.purchase']


def test_bank_only_summary_resolves_owned_query_receipt_reference(client,headers):
    from conftest import finish
    from bank_agent.schemas import Intent,DomainTask
    intent=Intent(summary='停止银行代扣并保留合同',tasks=[DomainTask(domain='subscriptions',action='subscriptions.cancel',
        params={'subscription_id':'视频会员','scope':'bank'})])
    plan=client.app.state.graph.start('u_demo','只停止视频会员银行代扣，保留会员合同',intent=intent)
    assert any(n['operation']=='subscriptions.query' for n in plan['nodes'])
    completed=finish(client,headers,plan)
    assert '银行代扣已停止' in completed['assistant_message']
    assert '会员合同仍有效并保留' in completed['assistant_message']


def test_ready_is_published_after_role_memory_is_written(client,headers,monkeypatch):
    from conftest import payroll
    from test_research import research
    from bank_agent.storage import Document
    memory=client.app.state.memory
    remember=memory.remember
    observed=[]
    def checked(*args,**kwargs):
        if kwargs.get('source_type')=='research_report':
            with client.app.state.db.read() as session:
                source=session.get(Document,kwargs['source_id'])
                observed.append(source.data['status'])
        return remember(*args,**kwargs)
    monkeypatch.setattr(memory,'remember',checked)
    payroll(client,headers)
    result=research(client,headers,{'amount_fen':10000})
    assert result['status']=='RESEARCH_READY'
    assert len(observed)>=2 and set(observed)=={'RESEARCH_RUNNING'}


def test_receipt_explanation_preserves_other_business_results(client):
    from bank_agent.business_explanations import explain_snapshot
    snapshot={'status':'COMPLETED','available_fen':0,
              'assistant_message':'账单分析已完成，本月净消费300元。',
              'nodes':[{'operation':'funds.transfer','status':'SUCCEEDED','params':{},
                        'result':{'receipt_id':'fixture_receipt','data':{'recipient':'妈妈','amount_fen':10000}}},
                       {'operation':'bills.analyze','status':'SUCCEEDED','params':{},'result':{}}]}
    with client.app.state.db.read() as session:
        explain_snapshot(session,'u_demo',snapshot)
    assert '本月净消费300元' in snapshot['assistant_message']
    assert '向妈妈转账100元已完成' in snapshot['assistant_message']


@pytest.mark.parametrize('message,period,annual,cleared',[
    ('给我看2026年度账单，说明覆盖到哪个月。','2026',True,True),
    ('查看2026年9月账单，说明统计范围。','2026-09',False,True),
    ('查看2026年度账单。','2025',True,False),
    ('查看2026年度账单。','2026',False,False),
    ('查看2025还是2026年度账单。','2026',True,False),
    ('明天给我看2026年度账单。','2026',True,False),
    ('查看2026年9月和10月账单。','2026-09',False,False),
    ('分别查看2026年9月和2026年10月账单。','2026-09',False,False),
])
def test_complete_source_bill_period_does_not_ask_for_appointment_date(client,message,period,annual,cleared):
    from bank_agent.schemas import Intent,DomainTask
    raw=Intent(summary='生成账单',tasks=[DomainTask(domain='bills',action='bills.report',
        params={'period':period,'annual':annual})],
        clarification='报告生成后会说明实际覆盖月份。',clarification_kind='date')
    actual=client.app.state.graph.planner._reconcile('u_demo','bill-period',None,raw,message)
    assert (actual.clarification is None)==cleared
    if cleared:
        plan=client.app.state.graph.start('u_demo',message,intent=actual)
        assert plan['status']=='COMPLETED'
        report=next(n for n in plan['nodes'] if n['operation']=='bills.report')['result']['data']
        assert report['annual']==annual and report['period']==period


@pytest.mark.parametrize('hint',['房东','王芳'])
def test_source_alias_grounding_does_not_add_a_second_transfer(client,monkeypatch,hint):
    from bank_agent.schemas import Intent,DomainTask
    from bank_agent.dialogue import transfer_grounding
    planner=client.app.state.graph.planner
    facts=planner.bank.facts('u_demo')
    base=facts['contacts'][0]
    facts['contacts']=[{**base,'id':'landlord','name':'王芳','aliases':['房东'],'notes':[]},
                       {**base,'id':'coworker','name':'王芳','aliases':['同事'],'notes':[]}]
    monkeypatch.setattr(planner.bank,'facts',lambda _:facts)
    raw=Intent(summary='转账并查询余额',tasks=[DomainTask(domain='funds',action='funds.transfer',
        params={'recipient_hint':hint,'amount_fen':150000}),DomainTask(domain='funds',action='balance.query')])
    actual=planner._reconcile('u_demo','alias-grounding',None,raw,'给房东转1500元，并查询余额')
    transfers=[t for t in actual.tasks if t.action=='funds.transfer']
    assert len(transfers)==1
    assert transfers[0].params['recipient_hint']=='房东'
    assert transfers[0].params['amount_fen']==150000
    direct=transfer_grounding('给房东转1500元',facts,{})
    assert direct.tasks[0].params['recipient_hint']=='房东'
    ambiguous=transfer_grounding('给王芳转1500元',facts,{})
    assert ambiguous.clarification_kind=='recipient'
