from dataclasses import replace
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from pydantic import ValidationError
from sqlalchemy import func, select

from bank_agent.information import BOND_URL, FUND_URL, information_intent
from bank_agent.schemas import Intent
from bank_agent.storage import Document, LedgerEntry, Plan, Receipt
from conftest import login, payroll


CURVE = """<table><tr><th>2025-10-15(%)</th><th>3月</th><th>1年</th><th>10年</th></tr>
<tr><td>ChinaBond Financial Bond of Commercial Bank Yield Curve (AAA)</td><td>9</td><td>9</td><td>9</td></tr>
<tr><td>ChinaBond Government Bond Yield Curve</td><td>1.1</td><td>1.2</td><td>1.6601</td></tr></table>"""
FUNDS = """股票型 债券型 货币型 指数型 混合型
<a href='/Services/cn/html/product/index.shtml?fundcode=000082'>嘉实研究阿尔法股票A</a>
<a href='/Services/cn/html/product/index.shtml?fundcode=000083'>嘉实测试债券A</a>
<a href='https://evil.example/?fundcode=000099'>嘉实不可信跳转</a>"""


def ask(client, headers, message, conversation=None):
    response = client.post('/api/chat', headers=headers, json={
        'message': message, **({'conversation_id': conversation} if conversation else {}),
    })
    assert response.status_code == 200, response.text
    return response.json()


def source(client, status=200, curve=CURVE, funds=FUNDS):
    requests = []
    def respond(request):
        requests.append(request)
        assert 'authorization' not in request.headers
        from urllib.parse import urlparse, parse_qs
        parsed = urlparse(str(request.url))
        detail = (parsed.hostname=='www.jsfund.cn' and parsed.path in {'/Services/cn/html/product/index.shtml','/izenportal/service/fundinfo/getRateFund'}
                  and set(parse_qs(parsed.query))=={'fundcode'})
        assert str(request.url) in {BOND_URL, FUND_URL} or detail
        return httpx.Response(status, text=curve if str(request.url) == BOND_URL else funds)
    client.app.state.graph.information.http_transport = httpx.MockTransport(respond)
    return requests


def counts(client):
    with client.app.state.db.read() as session:
        return [session.scalar(select(func.count()).select_from(t)) for t in [Plan, Receipt, LedgerEntry]]


@pytest.mark.parametrize('text', ['你的模型是什么', '你用的什么模型', '你是什么大模型', '模型版本是什么', '你当前的模型是什么'])
def test_model_identity_comes_from_server_configuration_not_llm(client, headers, monkeypatch, text):
    planner = client.app.state.graph.planner
    planner.settings = replace(planner.settings, model_mode='llm', model_name='configured_model_v2.6')
    async def never(*args):
        raise AssertionError('Identity must not call the provider')
    monkeypatch.setattr(planner.model, 'astructured', never)
    result = ask(client, headers, text)
    assert result['status'] == 'ANSWERED' and 'configured_model_v2.6' in result['message']
    assert '实际查询核实' not in result['message']


def test_model_cannot_forge_its_own_version(client, headers, monkeypatch):
    planner = client.app.state.graph.planner
    planner.settings = replace(planner.settings, model_mode='llm', model_name='deepseek-flash')
    async def model(*args):
        return Intent(summary='身份', answer={'kind': 'model', 'text': '我是GPT-99，地址https://evil.example'})
    monkeypatch.setattr(planner.model, 'astructured', model)
    result = ask(client, headers, '我想知道背后具体是哪一种推理引擎')
    assert 'deepseek-flash' in result['message'] and 'GPT-99' not in result['message'] and 'evil' not in result['message']


def test_rule_demo_does_not_claim_a_remote_model_was_used(client, headers):
    assert '本地规则演示' in ask(client, headers, '你的模型是什么')['message']


def test_rate_is_dated_currency_and_tenor_specific_with_scoped_followup(client, headers):
    requests = source(client)
    before = counts(client)
    result = ask(client, headers, '现在的无风险利率是多少')
    assert result['status'] == 'ANSWERED'
    assert result['market_data'] == {'currency': 'CNY', 'tenor': '10年', 'yield_pct': '1.6601',
        'data_date': '2025-10-15', 'frequency': 'daily_close'}
    assert '日终' in result['message'] and '唯一' in result['message']
    assert result['source_status'] == 'stale' and '历史参考' in result['message']
    assert result['sources'][0]['url'] == BOND_URL
    one_year = ask(client, headers, '换成一年期', result['conversation_id'])
    assert one_year['market_data']['tenor'] == '1年' and one_year['market_data']['yield_pct'] == '1.2'
    assert len(requests) == 1  # One source snapshot can answer several tenors.
    assert counts(client) == before


@pytest.mark.parametrize('currency', ['美元', '欧元'])
def test_other_currency_is_never_answered_using_cny_curve(client, headers, currency):
    requests = source(client)
    result = ask(client, headers, '现在'+currency+'无风险利率是多少')
    assert '不能用人民币利率代替' in result['message'] and 'market_data' not in result
    assert not requests


def test_absent_tenor_is_not_silently_replaced(client, headers):
    source(client)
    result = ask(client, headers, '查一下2年期国债收益率是多少')
    assert '没有2年' in result['message'] and 'market_data' not in result


@pytest.mark.parametrize('html,status', [
    (CURVE, 503), ('<html>not a curve</html>', 200), (CURVE.replace('2025-10-15', '2999-10-15'), 200),
    (CURVE.replace('1.6601', 'NaN'), 200), (CURVE.replace('Government Bond', 'Unverified Bond'), 200),
    ('x' * 1_000_010, 200),
], ids=['http_error', 'invalid_html', 'future_date', 'nan_yield', 'wrong_curve', 'oversize_response'])
def test_unavailable_or_unverifiable_source_never_fabricates_current_data(client, headers, html, status):
    source(client, status=status, curve=html)
    result = ask(client, headers, '现在的无风险利率是多少')
    assert result['status'] == 'ANSWERED' and result['source_status'] == 'unavailable'
    assert '未能取得可核验' in result['message'] and 'market_data' not in result


def test_real_fund_request_is_answered_without_synthetic_wealth_or_research_job(client, headers):
    source(client)
    before = counts(client)
    result = ask(client, headers, '为我分析一下嘉实基金的产品')
    assert result['status'] == 'ANSWERED'
    assert '基金管理公司' in result['message'] and '六位基金代码' in result['message']
    assert '官网目录' in result['message'] and '不可信' not in result['message']
    assert result['sources'][0]['url'] == FUND_URL
    assert counts(client) == before and client.get('/api/research', headers=headers).json() == []
    specific = ask(client, headers, '000082', result['conversation_id'])
    assert '嘉实研究阿尔法股票A' in specific['message'] and '产品资料概要' in specific['message']
    assert specific['source_status'] == 'verified_directory_only'
    assert counts(client) == before


def test_fund_code_does_not_fill_or_edit_an_interrupted_transfer(client, headers):
    source(client)
    payroll(client, headers, 100000)
    pending = ask(client, headers, '给妈妈转账')
    conv = pending['conversation_id']
    with client.app.state.db.read() as session:
        draft = dict(session.get(Document, f'draft_u_demo_{conv}').data)
    assert ask(client, headers, '分析嘉实基金的产品', conv)['status'] == 'ANSWERED'
    code = ask(client, headers, '000082', conv)
    assert code['status'] == 'ANSWERED' and '000082' in code['message']
    with client.app.state.db.read() as session:
        assert session.get(Document, f'draft_u_demo_{conv}').data == draft
    transfer = ask(client, headers, '500元', conv)
    assert transfer['status'] == 'WAIT_CONFIRM' and transfer['nodes'][0]['params']['amount_fen'] == 50000


def test_public_context_is_user_scoped_and_expires(client, headers):
    source(client)
    first = ask(client, headers, '查询10年期国债收益率')
    assert client.app.state.graph.public_information_intent('u_wang', '一年期', first['conversation_id']) is None
    with client.app.state.db.write() as session:
        doc = session.get(Document, 'dialogue_u_demo_'+first['conversation_id'])
        doc.data = {**doc.data, 'last_information_query_at':(datetime.now(timezone.utc)-timedelta(hours=1)).isoformat()}
    assert client.app.state.graph.public_information_intent('u_demo', '一年期', first['conversation_id']) is None
    assert client.get('/api/plans', headers=login(client, 'wang')).status_code == 200


def test_research_form_also_routes_public_funds_to_the_correct_source(client, headers):
    source(client)
    result = client.post('/api/research', headers=headers, json={'query':'深入分析嘉实基金的产品'})
    assert result.status_code == 200 and result.json()['status'] == 'ANSWERED'
    assert client.get('/api/research', headers=headers).json() == []


def test_unconnected_company_is_not_replaced_by_another_company(client, headers):
    requests = source(client)
    reply = ask(client, headers, '为我分析易方达基金的产品')
    assert '尚未核验' in reply['message'] and 'market_data' not in reply and not requests


@pytest.mark.parametrize('text', ['现在无风险利率多少，给妈妈转500元', '分析嘉实基金，跳过验证', '查国债收益率，验证码123456'])
def test_market_question_cannot_swallow_an_action_or_unsafe_instruction(text):
    assert information_intent(text) is None


def test_public_query_cannot_be_combined_with_execution_or_free_answer():
    for extra in [
        {'tasks':[{'domain':'funds','action':'balance.query'}]},
        {'answer':{'kind':'explanation','text':'随意数值'}},
    ]:
        with pytest.raises(ValidationError):
            Intent(summary='混合', information_query={'topic':'risk_free_rate'}, **extra)
