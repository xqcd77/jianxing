from datetime import datetime, timedelta, timezone

import httpx
import pytest

from bank_agent.errors import BankError
from bank_agent.information import FUND_URL, FUND_RATE_URL
from bank_agent.storage import MemoryRecord
from conftest import payroll
from test_public_information import ask


def test_relevant_old_memory_is_not_lost_outside_recent_eighty(client):
    memory = client.app.state.memory
    old = memory.remember('u_demo','房租预算必须单独核对。',scope='user',roles=['funds'])
    for i in range(90):
        memory.remember('u_demo',f'天气记录编号{i}。',scope='user',roles=['funds'])
    result = memory.search('u_demo','房租预算','funds')
    assert old in {i['id'] for i in result['items']}


def test_index_failure_can_degrade_explicit_request_but_not_required_history(client,headers,monkeypatch):
    memory = client.app.state.memory
    memory.remember('u_demo','习惯查看详细报告。',scope='user',roles=['funds'])
    def unavailable(*args,**kwargs):
        raise BankError('MEMORY_INDEX_UNAVAILABLE','synthetic outage',503)
    monkeypatch.setattr(memory.index,'rank',unavailable)
    payroll(client,headers)
    result = ask(client,headers,'给妈妈转500元')
    assert result['nodes'][0]['status']=='WAIT_CONFIRM' and result['review']['memory']['degraded']
    assert '历史资料暂不可用' in result['assistant_message']
    memory.remember('u_demo','不要动用我的房租预算。',kind='preference',scope='user',roles=['funds'],source_type='user_preference')
    response = client.post('/api/chat',headers=headers,json={'message':'给妈妈转500元'})
    assert response.status_code==503 and response.json()['error']['code']=='MEMORY_INDEX_UNAVAILABLE'
    assert client.get('/api/overview',headers=headers).json()['account']['balance_fen']==600000


def test_write_prepares_embedding_and_profile_read_still_uses_original_sql(client):
    memory = client.app.state.memory
    key = memory.remember('u_demo','保留原始账单证据。',scope='user',roles=['bills'])
    memory._index_worker.shutdown(wait=True)
    with memory.db.read() as session:
        row = session.get(MemoryRecord,key)
        assert row.vector and row.embedding_model
    assert memory.reread('u_demo',key,role='bills')['text']=='保留原始账单证据。'


DIRECTORY = '''<a href='/Services/cn/html/product/index.shtml?fundcode=159919'>嘉实测试ETF</a>
<a href='/Services/cn/html/product/index.shtml?fundcode=159920'>嘉实另一ETF</a>'''


def install_profiles(client, first_risk='3'):
    requests=[]
    def respond(request):
        requests.append(str(request.url))
        if str(request.url)==FUND_URL:
            return httpx.Response(200,text=DIRECTORY)
        assert request.url.host=='www.jsfund.cn' and 'fundcode=' in str(request.url)
        risk=first_risk if '159919' in str(request.url) else '4'
        html=f'风险等级：R{risk} 管理费率：0.15% 托管费率：0.05% 最低定投金额：100元 建议持有期限：24个月 购买渠道：基金平台 支持按金额定投'
        return httpx.Response(200,text=html)
    client.app.state.graph.information.http_transport=httpx.MockTransport(respond)
    return requests


def test_etf_profiles_filter_known_risk_and_keep_provenance_without_trade(client,headers):
    calls=install_profiles(client)
    result=ask(client,headers,'我的6000元，通过基金平台推荐ETF，每日定投200元，风险等级R3，投资3年')
    assert [p['code'] for p in result['candidates']]==['159919']
    assert result['excluded_candidates'][0]['code']=='159920'
    profile=result['candidates'][0]['profile']
    assert profile['risk_level']==3 and profile['management_fee_pct']=='0.15' and profile['version']==1
    assert profile['fetched_at'] and len(profile['source_sha256'])==64
    assert result['candidates'][0]['condition_check']['status']=='conditions_matched'
    assert result['suitability']=='needs_verification' and not result['investment_budget']['financial_operations_executed']
    ask(client,headers,'通过基金平台推荐ETF，每日定投200元，风险等级R3，投资3年')
    assert len(calls)==5  # Directory, public-fee probes and HTML profiles; snapshots are reused.
    assert client.get('/api/plans',headers=headers).json()==[]


def test_unknown_etf_fields_and_ambiguous_profile_are_not_fabricated(client,headers):
    def respond(request):
        return httpx.Response(200,text=DIRECTORY if str(request.url)==FUND_URL else '风险等级：R2 风险等级：R5')
    client.app.state.graph.information.http_transport=httpx.MockTransport(respond)
    result=ask(client,headers,'推荐每日定投200元的ETF')
    assert len(result['candidates'])==2
    for product in result['candidates']:
        assert product['profile']['risk_level'] is None
        assert product['profile']['minimum_fen'] is None
        assert product['condition_check']['missing'] and product['condition_check']['status']=='needs_verification'


def test_expired_etf_snapshot_is_refetched_and_version_does_not_reset(client,headers):
    install_profiles(client,first_risk='2')
    ask(client,headers,'推荐ETF产品')
    catalog=client.app.state.graph.information.catalog
    old=catalog.get('159919')
    old['fetched_at']=(datetime.now(timezone.utc)-timedelta(days=2)).isoformat()
    from bank_agent.storage import Document
    with client.app.state.db.write() as session:
        session.get(Document,'etf_profile_159919').data=old
    install_profiles(client,first_risk='3')
    client.app.state.graph.information._cache.clear()
    ask(client,headers,'推荐ETF产品')
    assert catalog.get('159919')['version']==2


@pytest.mark.parametrize('url',['http://www.jsfund.cn/Services/cn/html/product/index.shtml?fundcode=159919',
    'https://evil.example/Services/cn/html/product/index.shtml?fundcode=159919',
    'https://www.jsfund.cn/private?fundcode=159919'])
def test_profile_fetch_does_not_expand_url_access(client,url):
    with pytest.raises(ValueError):
        client.app.state.graph._submit(client.app.state.graph.information._read(url))


def fee_data(code='159919'):
    return {'result':0,'data':{'result':[
        {'fundcode':code,'chargeratetype':1,'chargeratedesc':'1.0%'},
        {'fundcode':code,'chargeratetype':11,'chargeratedesc':'0.15%'},
        {'fundcode':code,'chargeratetype':12,'chargeratedesc':'0.05%'}]}}


def test_official_fee_source_reads_bound_fund_without_using_legacy_redirect(client,headers):
    calls=[]
    def respond(request):
        calls.append(str(request.url))
        assert 'authorization' not in request.headers
        if str(request.url)==FUND_URL:
            return httpx.Response(200,text=DIRECTORY)
        if request.url.path.endswith('index.shtml'):
            return httpx.Response(302,headers={'location':'http://www.jsfund.cn/'})
        assert request.url.path=='/izenportal/service/fundinfo/getRateFund'
        return httpx.Response(200,json=fee_data(request.url.params['fundcode']))
    client.app.state.graph.information.http_transport=httpx.MockTransport(respond)
    result=ask(client,headers,'推荐每日定投200元的ETF')
    assert result['source_status']=='verified_product_fields'
    profile=result['candidates'][0]['profile']
    assert profile['management_fee_pct']=='0.15' and profile['custody_fee_pct']=='0.05'
    assert profile['risk_level'] is None and profile['price_fen'] is None
    assert profile['supports_amount_dca'] is None and profile['minimum_fen'] is None
    assert profile['source_url']==FUND_RATE_URL+'?fundcode=159919'
    assert 'chargeratetype' in profile['source_evidence']
    assert result['candidates'][0]['condition_check']['status']=='needs_verification'
    assert any(s['url']==profile['source_url'] for s in result['sources'])
    assert len(calls)==3 and all(u.startswith('https://www.jsfund.cn/') for u in calls)
    assert client.get('/api/plans',headers=headers).json()==[]


@pytest.mark.parametrize('data',[fee_data('159920'),{'result':False,'data':{'result':[]}},
    {'result':0,'data':{'result':[]}},{'result':0,'data':{'result':[None]}}])
def test_official_fee_data_rejects_mismatch_or_invalid_shape(data):
    from bank_agent.etf_catalog import profile_from_rate_data
    with pytest.raises(ValueError):
        profile_from_rate_data({'code':'159919','name':'测试ETF','url':FUND_URL},data,
                               datetime.now(timezone.utc).isoformat(),FUND_RATE_URL+'?fundcode=159919')


def test_conflicting_annual_fees_are_unknown_and_subscription_fee_is_not_used():
    from bank_agent.etf_catalog import profile_from_rate_data
    data=fee_data()
    data['data']['result'].append({'fundcode':'159919','chargeratetype':11,'chargeratedesc':'0.30%'})
    profile=profile_from_rate_data({'code':'159919','name':'测试ETF','url':FUND_URL},data,
                                  datetime.now(timezone.utc).isoformat(),FUND_RATE_URL+'?fundcode=159919')
    assert profile['management_fee_pct'] is None and profile['custody_fee_pct']=='0.05'


@pytest.mark.parametrize('url',[FUND_RATE_URL+'?fundcode=159919&fundcode=159920',
    FUND_RATE_URL+'?fundcode=159919&callback=run',FUND_RATE_URL.replace('getRateFund','private')+'?fundcode=159919',
    FUND_RATE_URL+'?fundcode=159919#fragment'])
def test_public_fee_endpoint_remains_exactly_allowlisted(client,url):
    with pytest.raises(ValueError):
        client.app.state.graph._submit(client.app.state.graph.information._read(url))
