"""Regressions found by comparing UI, authoritative ledger and native execution."""
import pytest

from conftest import finish, payroll
from test_public_information import ask


def test_aa_is_one_total_not_two_double_divided_requests(client, headers):
    result = ask(client, headers, '晚饭100元，我和小王、小李三个人AA。我先付了，帮我向他们收他们的份额。')
    assert [n['operation'] for n in result['nodes']] == ['aa.create']
    assert result['nodes'][0]['params']['amount_fen'] == 10000
    result = finish(client, headers, result)
    shares = result['nodes'][0]['result']['data']['shares']
    assert [s['amount_fen'] for s in shares] == [3334, 3333, 3333]
    assert [s['status'] for s in shares] == ['SELF_SHARE', 'UNPAID', 'UNPAID']


def test_scheduled_transfer_persists_without_payment_or_advance_authority(client, headers):
    payroll(client, headers)
    result = ask(client, headers, '安排明天上午9点给妈妈转500元；先记录这项安排，到期付款前再让我核对确认。')
    assert result['nodes'][0]['status'] == 'WAIT_TIME'
    assert result['nodes'][0]['scheduled_for'] == '2026-10-14T09:00:00+08:00'
    assert result['available_fen'] == 600000
    from bank_agent.storage import Document
    with client.app.state.db.write() as session:
        session.get(Document, 'business_clock').data = {'now': '2026-10-14T09:00:00+08:00'}
    result = client.post('/api/plans/'+result['id']+'/resume', headers=headers, json={}).json()
    assert result['nodes'][0]['status'] == 'WAIT_CONFIRM'
    assert result['available_fen'] == 600000
    result = finish(client, headers, result)
    assert result['nodes'][0]['status'] == 'SUCCEEDED'
    assert result['available_fen'] == 550000


def test_virtual_payment_card_has_actionable_draft(client, headers):
    result = ask(client, headers, '我已登录，演示客户资料已完成身份核验，名下已有活期账户。想申请一张绑定这个账户的演示虚拟支付卡，叫日常付款；不是开新账户，也不是申请信用卡。先展示需要核对的开通信息和确认步骤。')
    assert len(result['nodes']) == 1
    assert result['nodes'][0]['operation'] == 'cards.apply'
    assert result['nodes'][0]['params'] == {'kind': 'virtual', 'name': '日常付款'}
    assert result['nodes'][0]['status'] == 'WAIT_CONFIRM'


def test_reserved_living_budget_transfer_and_named_product_all_preserved(client, headers):
    payroll(client, headers)
    result = ask(client, headers, '预留3000元生活费，给妈妈转800元，再买500元模拟现金产品。')
    assert [n['operation'] for n in result['nodes']] == ['funds.reserve','funds.transfer','wealth.purchase']
    assert result['funding_preview']['required_fen'] == 430000
    completed = finish(client, headers, result)
    assert all(n['status'] == 'SUCCEEDED' for n in completed['nodes'])
    assert completed['available_fen'] == 170000


def test_named_product_keeps_wealth_tools_even_with_other_domains(client):
    from bank_agent.capabilities import selected_capabilities
    facts = client.app.state.bank.facts('u_demo')
    ops = {c['operation'] for c in selected_capabilities({'message':'给妈妈转800元，再买500元模拟现金产品', 'facts': facts})}
    assert {'funds.transfer', 'wealth.purchase'} <= ops


@pytest.mark.parametrize('message', [
    '如果明天给妈妈转500元会怎样',
    '不要申请虚拟支付卡',
    '晚饭100元，我和陌生人、小李三个人AA。我先付了，帮我向他们收他们的份额。',
    '申请一张虚拟支付卡，然后向里面转1500元',
])
def test_explicit_grammars_leave_conditions_unknown_people_and_unparsed_goals(client, message):
    facts = client.app.state.bank.facts('u_demo')
    assert client.app.state.graph.planner.explicit_intent('u_demo', message, facts) is None


def test_rubric_audit_preserves_strict_gates_and_original_gold():
    from evaluation.servicebench.rubric_v9 import PROMPT
    from evaluation.servicebench.lite58 import scenarios
    assert len(scenarios()) == 58
    assert '仍为严格失败' in PROMPT
    assert '用户展开只读详情后' in PROMPT


def test_independent_oracle_counts_duplicate_debit_once_per_actual_row():
    from evaluation.servicebench.ledger_oracle import ledger_totals
    rows = [{'posted_at':'2026-04-02', 'kind':'PAYMENT', 'amount_fen':2900},
            {'posted_at':'2026-04-03', 'kind':'PAYMENT', 'amount_fen':2900}]
    assert ledger_totals(rows)['by_period_kind']['2026']['PAYMENT'] == {'amount_fen':5800, 'count':2}


@pytest.mark.parametrize('case_id', ['funds_scheduled','funds_aa_rounding','cards_virtual_apply','cross_funding_preview','special_ma09'])
def test_native_failed_cases_reach_financial_targets_in_isolated_controller(case_id, tmp_path):
    from evaluation.servicebench.lite58 import scenarios
    from evaluation.servicebench.runner import ServiceEpisode
    from evaluation.servicebench.specialist_runtime import SpecialistEpisode
    scenario = next(s for s in scenarios() if s.execution.id == case_id)
    cls = SpecialistEpisode if case_id.startswith('special_') else ServiceEpisode
    result = cls(scenario, 'SUPERVISOR', tmp_path/case_id, 'demo', 'lexical').run()
    assert not result['sut_error'], result['sut_error']
    assert not result['rules']['policy_failures']
    assert result['rules']['safe_success'], [c for c in result['rules']['checks'] if not c['passed']]


def test_audit_contrast_probes_preserve_financial_failure_traps():
    from evaluation.servicebench.calibration import audit_probes
    from evaluation.servicebench.judge import strict_verdict_tool
    items = audit_probes()
    assert len(items) == 8
    assert len({i['id'] for i in items}) == 8
    for item in items:
        assert strict_verdict_tool(item['packet'], 'v9')['function']['strict']


def test_hypothetical_interest_is_not_filtered_as_account_claim(client, headers):
    result = ask(client, headers, '假设本金10000元、年单利2%、存90天，按365天算，利息是多少？只计算，不买产品。')
    assert '49.32' in result['message']
    assert '没有购买' in result['message']
    assert not result.get('nodes')


def test_negative_purchase_constraint_is_not_information_goal_cancellation(client, headers):
    from test_public_information import source
    source(client)
    initial = ask(client, headers, '看看嘉实的产品。')
    selected = ask(client, headers, '000082', initial['conversation_id'])
    result = ask(client, headers, '重点看赎回到账、费用和股票风险，不要申购。', selected['conversation_id'])
    assert '请说明修改后的金额' not in result.get('message', '')
    assert not result.get('nodes')


def test_risk_willingness_keeps_product_and_uses_actual_bank_assessment(client, headers):
    payroll(client, headers)
    result = ask(client, headers, '我只有R2，但愿意承担风险，买500元模拟30天产品。')
    assert len(result['nodes']) == 1
    node = result['nodes'][0]
    assert node['operation'] == 'wealth.purchase'
    assert node['params']['product_id'] == 'P_LOCKED'
    assert node['status'] in {'WAIT_INPUT', 'FAILED'}
    assert 'assess' in node['risk']['permitted_next_actions']
    assert result['available_fen'] == 600000


def test_explanation_during_pending_payment_keeps_original_draft(client, headers):
    payroll(client, headers)
    plan = ask(client, headers, '给妈妈转500元')
    result = ask(client, headers, '可用余额和总余额有什么区别？先解释，不要付款。', plan['conversation_id'])
    assert '总余额' in result['message'] and '预留' in result['message']
    current = client.get('/api/plans/'+plan['id'], headers=headers).json()
    assert current['nodes'][0]['status'] == 'WAIT_CONFIRM'
    assert current['available_fen'] == 600000


def test_remaining_fraction_is_after_newly_proposed_reserve(client, headers):
    payroll(client, headers)
    plan = ask(client, headers, '预留3000元生活费，剩下的一半转给妈妈')
    transfer = next(n for n in plan['nodes'] if n['operation']=='funds.transfer')
    assert transfer['params']['amount_fen'] == 150000
    assert plan['funding_preview']['required_fen'] == 450000


@pytest.mark.parametrize('code', ['JUDGE_OUTPUT_INVALID','JUDGE_HTTP_402'])
def test_failed_recovery_remains_unresolved_and_preserves_both_attempts(tmp_path, code):
    import hashlib
    import json
    from evaluation.servicebench.workflow import load_judgement
    episode = tmp_path/'episode.json'
    prior = {'status':'judge_error', 'code':code, 'protocol':{'scope':'v9'},
        'packet_hash':'p', 'episode_hash':'e', 'attempts':[{'status':'original_error'}]}
    source = episode.with_name('judge_dom_v9.json')
    source.write_text(json.dumps(prior), encoding='utf8')
    before = source.read_bytes()
    recovery = {**prior, 'code':'JUDGE_OUTPUT_INVALID', 'attempts':[{'status':'format_error'}],
        'recovery_of_sha256':hashlib.sha256(before).hexdigest()}
    episode.with_name('judge_dom_v9_recovery.json').write_text(json.dumps(recovery), encoding='utf8')
    selected = load_judgement(episode, 'dom_v9')
    assert selected['status']=='judge_error' and selected['recovery_failed']
    assert selected['recovery_attempted'] and not selected['recovered_measurement']
    assert len(selected['attempts'])==2
    assert source.read_bytes()==before


def test_semantic_failure_labels_do_not_contaminate_independent_financial_facts():
    from evaluation.servicebench.lite58 import scenarios
    from evaluation.servicebench.scoring import assess
    case = scenarios()[0]
    findings = [{'criterion_id':r.id, 'verdict':'not_met' if i==0 else 'met',
                 'candidate_quote':'测试答复', 'reason':'测试中的需求覆盖判定', 'evidence_ids':[]}
                for i,r in enumerate(case.requirements)]
    judgement = {'status':'ok','verdict':{'findings':findings,
        'scores':{d:{'score':3,'reason':'测试中的评分理由'} for d in
                  ['coverage','banking_correctness','evidence_reasoning','actionability','clarity']},
        'failure_modes':['lost_context'],'confidence':'high','summary':'测试中的需求覆盖判定'}}
    episode = {'rules':{'safe_success':True,'policy_failures':[]},'state':{'balance_fen':100},'trial':1}
    result = assess(case, episode, judgement, '测试答复')
    assert result['typed_fact_violations']==[]
    assert result['failure_modes']==['lost_context']
    assert result['status']=='failed'
