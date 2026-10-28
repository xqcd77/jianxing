"""Evidence-backed ETF profiles and deterministic condition checks.

Unknown values remain null. Directory order, training knowledge and synthetic
bank products cannot establish product suitability or current trading prices.
"""
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import hashlib
import json
import re
from types import SimpleNamespace

from .storage import Document, save_document


def profile_from_page(product, page, fetched_at):
    text = ' '.join(page.text)
    def unique(pattern, convert=int):
        matches = set(re.findall(pattern,text,re.I))
        return convert(next(iter(matches))) if len(matches)==1 else None
    profile = {**product, 'risk_level':unique(r'(?:风险等级|风险级别)\s*[:：]?\s*R?([1-5])'),
        'management_fee_pct':unique(r'管理费率\s*[:：]?\s*(\d+(?:\.\d+)?)\s*%',str),
        'custody_fee_pct':unique(r'托管费率\s*[:：]?\s*(\d+(?:\.\d+)?)\s*%',str),
        'minimum_fen':unique(r'(?:最低申购金额|起投金额|最低定投金额)\s*[:：]?\s*(\d+(?:\.\d+)?)\s*元',lambda v:int(Decimal(v)*100)),
        'lot_size':unique(r'(?:最小交易单位|交易单位)\s*[:：]?\s*(\d+)\s*份'),
        'price_fen':unique(r'最新成交价\s*[:：]?\s*(\d+(?:\.\d+)?)\s*元',lambda v:int(Decimal(v)*100)),
        'price_as_of':unique(r'行情日期\s*[:：]?\s*(20\d{2}-\d{2}-\d{2})',str),
        'minimum_horizon_months':unique(r'建议持有期限\s*[:：]?\s*(\d+)\s*个月'),
        'supports_amount_dca':True if '支持按金额定投' in text and '不支持按金额定投' not in text else
                              False if '不支持按金额定投' in text else None,
        'channels':['fund_platform'] if '购买渠道：基金平台' in text else ['exchange'] if '购买渠道：证券账户' in text else None,
        'fetched_at':fetched_at,'source_url':product['url'],
        'source_sha256':hashlib.sha256(text.encode()).hexdigest()}
    for field in ('management_fee_pct','custody_fee_pct'):
        if profile[field] is not None and not 0 <= Decimal(profile[field]) <= 10:
            profile[field] = None
    if profile['lot_size'] is not None and not 1<=profile['lot_size']<=100000:
        profile['lot_size']=None
    return profile


def profile_from_rate_data(product, data, fetched_at, source_url):
    """Parse only fund-bound annual fees, not subscription tiers or NAV prices."""
    if not isinstance(data,dict) or type(data.get('result')) is not int or data['result'] != 0 or not isinstance(data.get('data'),dict):
        raise ValueError('Official fee response is not verified')
    rows = data['data'].get('result')
    if not isinstance(rows,list) or not rows or any(not isinstance(row,dict)
            or row.get('fundcode') != product['code'] for row in rows):
        raise ValueError('Official fee rows do not match the requested fund')
    profile = profile_from_page(product,SimpleNamespace(text=[]),fetched_at)
    for kind,field in [(11,'management_fee_pct'),(12,'custody_fee_pct')]:
        values = {str(row.get('chargeratedesc','')).strip() for row in rows if row.get('chargeratetype')==kind}
        if len(values)==1:
            match = re.fullmatch(r'(\d+(?:\.\d+)?)\s*%',next(iter(values)))
            if match and 0 <= Decimal(match[1]) <= 10:
                profile[field]=match[1]
    raw = json.dumps(data,ensure_ascii=False,sort_keys=True,separators=(',',':'))
    profile.update(source_url=source_url,source_sha256=hashlib.sha256(raw.encode()).hexdigest(),
                   source_evidence=raw,source_format='official_fund_fees_v1')
    return profile


class ETFProductCatalog:
    def __init__(self, db=None):
        self.db,self._profiles = db,{}

    def get(self, code):
        if self.db:
            with self.db.read() as session:
                doc = session.get(Document,'etf_profile_'+code)
                profile = dict(doc.data) if doc and doc.kind=='etf_profile' and doc.owner_id=='system' else None
        else:
            profile = self._profiles.get(code)
        if not profile:
            return None
        try:
            if datetime.fromisoformat(profile['fetched_at']) < datetime.now(timezone.utc)-timedelta(hours=24):
                return None
        except (KeyError,ValueError):
            return None
        return profile

    def save(self, profile):
        if self.db:
            with self.db.read() as session:
                doc = session.get(Document,'etf_profile_'+profile['code'])
                previous = dict(doc.data) if doc else None
        else:
            previous = self._profiles.get(profile['code'])
        value = {**profile,'version':(previous.get('version',1) if previous else 0)
            + int(not previous or previous.get('source_sha256')!=profile['source_sha256'])}
        if self.db:
            with self.db.write() as session:
                save_document(session,'etf_profile_'+profile['code'],'etf_profile','system',value)
        else:
            self._profiles[profile['code']]=value
        return value


def check_conditions(profile, query):
    unknown,excluded,checks = [],[],[]
    if query.risk_level is None:
        unknown.append('用户风险承受等级')
    elif profile.get('risk_level') is None:
        unknown.append('产品风险等级')
    elif profile['risk_level']>query.risk_level:
        excluded.append('产品风险等级超过用户明确的承受等级')
    else:
        checks.append('风险等级条件符合')
    if query.investment_horizon_months is None:
        unknown.append('计划投资期限')
    elif profile.get('minimum_horizon_months') is None:
        unknown.append('官方投资期限条件')
    elif query.investment_horizon_months<profile['minimum_horizon_months']:
        excluded.append('计划投资期限短于已披露建议期限')
    else:
        checks.append('期限条件符合')
    if query.channel is None:
        unknown.append('购买渠道')
    elif profile.get('channels') is None:
        unknown.append('产品购买渠道')
    elif query.channel not in profile['channels']:
        excluded.append('产品与指定购买渠道不匹配')
    else:
        checks.append('购买渠道符合')
    contribution = query.periodic_amount_fen
    if contribution:
        minimum = profile.get('minimum_fen')
        if minimum is None:
            unknown.append('最低定投金额')
        elif contribution < minimum:
            excluded.append('每次投入低于已披露最低金额')
        else:
            checks.append('最低投入金额符合')
        supported = profile.get('supports_amount_dca')
        if supported is None:
            unknown.append('渠道是否支持按金额定投')
        elif not supported:
            excluded.append('当前资料不支持按金额自动定投')
        if query.channel == 'exchange':
            price_date = profile.get('price_as_of')
            fresh_price = price_date == datetime.now(timezone(timedelta(hours=8))).date().isoformat()
            if not profile.get('lot_size') or not profile.get('price_fen') or not fresh_price:
                unknown.append('当期成交价格与最小交易单位')
            elif profile['lot_size']*profile['price_fen'] > contribution:
                excluded.append('每次投入不足一个最小交易单位，且尚未计费用')
    if query.budget_fen is not None and contribution and query.budget_fen < contribution:
        excluded.append('总预算不足一次完整定投')
    for field,label in [('management_fee_pct','管理费率'),('custody_fee_pct','托管费率')]:
        if profile.get(field) is None:
            unknown.append(label)
    return {'status':'excluded' if excluded else 'needs_verification' if unknown else 'conditions_matched',
            'passed_checks':checks,'missing':unknown,'exclusion_reasons':excluded,
            'financial_operations_executed':False}
