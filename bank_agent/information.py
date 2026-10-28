"""Allowlisted public information, isolated from synthetic bank products and writes."""

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from html.parser import HTMLParser
import re
import time
import json
from urllib.parse import parse_qs, urljoin, urlparse

import httpx
import asyncio

from .errors import ClarificationNeeded
from .schemas import Intent, PublicInformationQuery
from .storage import Document


BOND_URL = "https://yield.chinabond.com.cn/cbweb-cbrc-web/cbrc/showCbrc"
FUND_URL = "https://www.jsfund.cn/Services/cn/jsp/product/index.jsp?SiteID=1"
FUND_RATE_URL = "https://www.jsfund.cn/izenportal/service/fundinfo/getRateFund"
ALLOWED_HOSTS = {"yield.chinabond.com.cn", "www.jsfund.cn"}
TENORS = {
    "3月": r"(?<!\d)(?:3|三)(?:个)?月", "6月": r"(?<!\d)(?:6|六)(?:个)?月",
    "1年": r"(?<!\d)(?:1|一)年", "2年": r"(?<!\d)(?:2|二|两)年",
    "3年": r"(?<!\d)(?:3|三)年", "5年": r"(?<!\d)(?:5|五)年",
    "7年": r"(?<!\d)(?:7|七)年", "10年": r"(?<!\d)(?:10|十)年", "30年": r"(?<!\d)(?:30|三十)年",
}
COMPANIES = re.compile(r"嘉实|易方达|华夏|南方|广发|富国|中欧|招商|博时|鹏华|银华|汇添富")
ACTION = re.compile(
    r"(?:给|向).{0,15}(?:转|汇|打)(?:账|款|钱)?|(?:帮我|我要|直接)(?:买|申购|赎回)|"
    r"转给|汇给|打给|(?:申请|办理|办|开通|订阅).{0,12}(?:卡片|银行卡|虚拟卡|实体卡)|"
    r"(?:订|定|开通|购买|办理).{0,8}会员|查.{0,5}(?:余额|账单)|预留|冻结|解锁|"
    r"挂失|解挂|取消(?:订阅|会员|代扣)|调整额度|忽略权限|跳过验证|跳过确认|ignore previous|伪造回执", re.I,
)
ETF = re.compile(r"(?<![a-z])ETF(?![a-z])|交易型开放式指数基金", re.I)


def investment_amounts(message):
    """Keep stated capital and each contribution in different semantic slots."""
    from .dialogue import money_slot
    amount = r"[-−－]?(?:\d{1,3}(?:[,，]\d{3})+|\d+)(?:\.\d+)?\s*[万千]?\s*[元块]?(?![\d.])"
    capital = re.search(rf"(?:我(?:的|有)|本金|预算|总资金|资金总额|总共)\s*(?:是|有|为)?\s*({amount})", message)
    periodic = re.search(rf"(?:每日|每天|每个交易日|每交易日)\s*(?:定投|投入|投)?\s*({amount})", message)
    periodic = periodic or re.search(rf"定投\s*({amount})\s*(?:每天|每日|每个交易日)", message)
    def parse(match):
        return money_slot(match[1].strip().rstrip("元块").strip() + "元") if match else None
    return parse(capital), parse(periodic)


def investment_preferences(message):
    result = {}
    risk = re.search(r'(?:承受|风险(?:承受)?(?:等级|级别)?|最多接受)\s*R?([1-5])',message,re.I)
    if risk:
        result['risk_level']=int(risk[1])
    horizon = re.search(r'(?:投资|持有|定投)(?:期限)?\s*(?:计划|打算|为|是)?\s*(\d+|[一二两三四五六七八九十])\s*(年|个月)',message)
    if horizon:
        value = int(horizon[1]) if horizon[1].isdigit() else {'一':1,'二':2,'两':2,'三':3,'四':4,'五':5,'六':6,'七':7,'八':8,'九':9,'十':10}[horizon[1]]
        result['investment_horizon_months']=value*(12 if horizon[2]=='年' else 1)
    if re.search(r'证券账户|券商|场内',message):
        result['channel']='exchange'
    elif re.search(r'基金平台|场外|支付宝|天天基金',message):
        result['channel']='fund_platform'
    return result


def ground_investment_query(query, message, transfer_total=0, available_fen=None):
    """Bind money and suitability inputs to source text, never inferred model facts."""
    if ETF.search(message) and not re.search(r'货币基金|债券基金|混合基金|股票基金',message):
        query = query.model_copy(update={'asset_type':'etf'})
    if query.asset_type!='etf':
        return query
    budget, contribution = investment_amounts(message) if query.currency=='CNY' else (None,None)
    if query.currency=='CNY':
        from .dialogue import money_slot
        amount = r'(?:\d+(?:\.\d+)?\s*[万千]?|[零〇一二两三四五六七八九十百千万]+)\s*[元块]'
        explicit = re.search(rf'(?:拿|用|花|投入?|预算(?:是|为)?)\s*({amount})[^，,。；;转汇]{{0,14}}ETF',message,re.I)
        explicit = explicit or re.search(rf'({amount})\s*(?:用于|用来|拿来)?\s*(?:投资|投|买|申购|推荐|选|看看|了解|研究|的)?\s*ETF',message,re.I)
        explicit = explicit or re.search(rf'ETF预算\s*(?:是|为)?\s*({amount})',message,re.I)
        if explicit:
            budget=money_slot(explicit[1])
        elif re.search(r'(?:剩下|剩余|余下|其余)[^。；;]{0,35}ETF',message,re.I):
            from .advisory_budget import bind_budget_rule, calculate_budget
            rule = bind_budget_rule({'source_text': message, 'query': query.model_dump(mode='json')})
            # An explicit user's capital is an advisory assumption, never cash
            # credited to the account. Execution still uses bank availability.
            basis = budget if budget is not None else available_fen
            budget = calculate_budget(rule, basis, transfer_total)[0] if rule and basis is not None else None
    preferences = investment_preferences(message)
    return query.model_copy(update={'budget_fen':budget,'periodic_amount_fen':contribution,
        'risk_level':preferences.get('risk_level'),'channel':preferences.get('channel'),
        'investment_horizon_months':preferences.get('investment_horizon_months')})


def information_intent(message, context=None):
    from .memory import SENSITIVE

    if SENSITIVE.search(message) or ACTION.search(message):
        return None
    requested = re.split(r"(?:不要|别|禁止|不许).*(?:代替|替代|顶替)", message)[0]
    matches = [(m.start(),label) for label,pattern in TENORS.items() for m in re.finditer(pattern,requested)]
    tenor = min(matches)[1] if matches else None
    context = context or {}
    previous = context.get("last_information_query", {})
    try:
        fresh = datetime.fromisoformat(context.get("last_information_query_at", "")) > datetime.now(timezone.utc) - timedelta(minutes=30)
    except (ValueError, TypeError):
        fresh = False
    if not fresh:
        previous = {}
    rate_followup = previous.get("topic") == "risk_free_rate" and re.fullmatch(
        r"\s*(?:那|改成|换成|看看|查一下)?\s*[\d一二两三五六七十]+(?:个)?(?:月|年)(?:期)?(?:的)?(?:呢|吧)?[？?。\s]*", message,
    )
    if rate_followup or (
        ("无风险利率" in message or ("国债" in message and "收益率" in message))
        and not re.search(r"什么意思|是什么概念|如何计算|怎么计算|假设|怎么算|计算收益", message)
    ):
        currency = "USD" if re.search(r"美元|美国|美债|\bUSD\b", message, re.I) else (
            "EUR" if re.search(r"欧元|\bEUR\b", message, re.I) else previous.get("currency", "CNY") if rate_followup else "CNY"
        )
        return Intent(summary="查询公开国债收益率参考", information_query=PublicInformationQuery(
            topic="risk_free_rate", currency=currency, tenor=tenor,
        ))
    company = COMPANIES.search(message)
    code = re.search(r"(?<!\d)\d{6}(?!\d)", message)
    if ETF.search(message) and re.search(r"推荐|适合|定投|分析|研究|比较|对比|介绍|查询|查一下|看看|了解|产品", message):
        currency = "USD" if re.search(r"美元|美金|\bUSD\b", message, re.I) else "EUR" if re.search(r"欧元|\bEUR\b", message, re.I) else "CNY"
        try:
            budget, contribution = investment_amounts(message) if currency == "CNY" else (None, None)
        except ClarificationNeeded as error:
            return Intent(summary="核对投资预算", clarification=error.message, clarification_kind="amount")
        return Intent(summary="核查ETF公开产品与定投条件", information_query=PublicInformationQuery(
            topic="fund_products", asset_type="etf", currency=currency, organization=company[0] if company else None,
            product_hint=message[:120], budget_fen=budget, periodic_amount_fen=contribution,
            **investment_preferences(message),
        ))
    fund_followup = previous.get("topic") == "fund_products" and (
        re.fullmatch(r"\s*\d{6}[。\s]*", message) or re.fullmatch(r"(?:看|分析|看看)?(?:货币|债券|股票|混合|指数)型?(?:基金)?(?:的|吧|呢)?[？?。\s]*", message)
    )
    details_followup = (previous.get('topic') == 'fund_products'
        and re.search(r'赎回|到账|费用|风险|条款|费率|流动性', message)
        and not re.search(r'(?:给|向).{0,12}转|帮我申购|我要申购|立即购买|取消代扣|换成|改成', message))
    if details_followup:
        return Intent(summary='继续核查同一产品的公开条款，尚未申购',
            information_query=PublicInformationQuery.model_validate({**previous,
                'product_hint': code[0] if code else previous.get('product_hint', '')}))
    if fund_followup or (
        ("基金" in message or company) and re.search(r"分析|研究|比较|对比|介绍|有哪些|看看|了解|产品", message)
    ):
        organization = company[0] if company else previous.get("organization") if fund_followup else None
        return Intent(summary="了解公募基金产品资料", information_query=PublicInformationQuery(
            topic="fund_products", organization=organization,
            product_hint=code[0] if code else message[:120],
        ))
    return None


def scoped_information_intent(db, user_id, message, conversation_id):
    direct = information_intent(message)
    if direct or not conversation_id:
        return direct
    with db.read() as session:
        doc = session.get(Document, f"dialogue_{user_id}_{conversation_id}")
        if doc and doc.owner_id == user_id and doc.kind == "dialogue":
            return information_intent(message, doc.data)
    return None


class PublicPage(HTMLParser):
    def __init__(self):
        super().__init__()
        self.rows, self.links, self.text = [], [], []
        self.cells, self.buffer, self.link_buffer = [], [], []
        self.in_cell, self.href = False, None

    def handle_starttag(self, tag, attrs):
        if tag == "tr":
            self.cells = []
        if tag in {"th", "td"}:
            self.in_cell, self.buffer = True, []
        if tag == "a":
            self.href, self.link_buffer = dict(attrs).get("href", ""), []

    def handle_data(self, data):
        self.text.append(data)
        if self.in_cell:
            self.buffer.append(data)
        if self.href is not None:
            self.link_buffer.append(data)

    def handle_endtag(self, tag):
        if tag in {"th", "td"} and self.in_cell:
            self.cells.append(" ".join("".join(self.buffer).split()))
            self.in_cell = False
        if tag == "tr" and self.cells:
            self.rows.append(self.cells)
        if tag == "a" and self.href is not None:
            self.links.append((" ".join("".join(self.link_buffer).split()), re.sub(r"\s+", "", self.href)))
            self.href = None


class PublicInformationService:
    def __init__(self, db=None):
        self._cache = {}
        from .etf_catalog import ETFProductCatalog
        self.catalog = ETFProductCatalog(db)

    @staticmethod
    def _validate_source(url):
        parsed = urlparse(url)
        params = parse_qs(parsed.query,keep_blank_values=True)
        trusted_detail = (parsed.scheme=='https' and parsed.hostname=='www.jsfund.cn' and not parsed.username
            and not parsed.password and parsed.port in {None,443} and not parsed.fragment
            and parsed.path in {'/Services/cn/html/product/index.shtml', '/izenportal/service/fundinfo/getRateFund'}
            and set(params)=={'fundcode'} and len(params['fundcode'])==1
            and re.fullmatch(r'\d{6}',params['fundcode'][0]))
        if url not in {BOND_URL,FUND_URL} and not trusted_detail:
            raise ValueError('Public URL is outside the connected official sources')

    async def _fetch_body(self, url):
        self._validate_source(url)
        async with httpx.AsyncClient(timeout=8, follow_redirects=False,
                transport=getattr(self, "http_transport", None)) as client:
            async with client.stream("GET", url) as response:
                response.raise_for_status()
                chunks, size = [], 0
                async for chunk in response.aiter_bytes():
                    size += len(chunk)
                    if size > 1_000_000:
                        raise ValueError("Public source exceeds the allowed size")
                    chunks.append(chunk)
                html = b"".join(chunks).decode(response.encoding or "utf-8")
        return html, datetime.now(timezone.utc).isoformat()

    async def _read(self, url):
        self._validate_source(url)
        cached = self._cache.get(url)
        if cached and time.monotonic() - cached[0] < 300:
            return cached[1], cached[2]
        html, fetched = await self._fetch_body(url)
        page = PublicPage()
        page.feed(html)
        self._cache[url] = (time.monotonic(), page, fetched)
        return page, fetched

    async def _load_etf_profile(self, product):
        from .etf_catalog import profile_from_page, profile_from_rate_data
        try:
            # Prefer the exact public fee endpoint used by the official product
            # page; legacy directory links currently redirect to a generic home.
            url = FUND_RATE_URL+'?fundcode='+product['code']
            raw, observed = await self._fetch_body(url)
            return profile_from_rate_data(product,json.loads(raw),observed,url)
        except (httpx.HTTPError,ValueError,UnicodeError):
            detail, observed = await self._read(product['url'])
            return profile_from_page(product,detail,observed)

    async def answer(self, query):
        from .progress import publish_progress
        publish_progress('information','正在查询官方资料与核对产品条件')
        try:
            result = await (self._rate(query) if query.topic == "risk_free_rate" else
                            self._etfs(query) if query.asset_type == "etf" else self._funds(query))
        except (httpx.HTTPError, ValueError, InvalidOperation, UnicodeError):
            url = BOND_URL if query.topic == "risk_free_rate" else FUND_URL
            self._cache.pop(url, None)
            result = self._response(
                "这次未能取得可核验的官网资料，所以暂不提供最新数值或具体产品结论。你可以打开下方官网核对，或稍后让我重试。",
                sources=[{"title": "中债国债收益率" if query.topic == "risk_free_rate" else "嘉实基金产品官网", "url": url}],
                source_status="unavailable",
            )
        if query.asset_type == "etf":
            from .presentation import yuan
            budget = {"budget_fen": query.budget_fen, "periodic_amount_fen": query.periodic_amount_fen,
                      "full_contributions": query.budget_fen // query.periodic_amount_fen
                      if query.budget_fen is not None and query.periodic_amount_fen else None,
                      "source": "user_statement", "financial_operations_executed": False}
            prefix = "你提出的ETF推荐需求已保留。"
            if query.budget_fen is not None:
                prefix += f"按你说明的资金，ETF研究预算为{yuan(query.budget_fen)}。"
            if query.periodic_amount_fen:
                prefix += f"每次定投{yuan(query.periodic_amount_fen)}。"
            if budget["full_contributions"] is not None:
                prefix += f"预算÷每次金额，可覆盖{budget['full_contributions']}次完整投入；不计费用和价格变化，也不把交易日数当成自然日数。"
            result.update(investment_budget=budget, message=prefix + "\n" + result["message"])
        return result

    async def _etfs(self, query):
        # The only connected public catalogue is stated explicitly. Directory
        # identity does not establish suitability, price, lot size or DCA support.
        if query.currency != "CNY":
            return self._response("你问的是外币ETF投资。当前只接入人民币口径的基金官网目录，不能把外币金额当成人民币预算，请补充对应市场的官方产品资料。", source_status="unavailable")
        if query.organization and query.organization != "嘉实":
            return self._response(f"ETF需求需要核查{query.organization}的官方资料；目前该来源尚未接入，请提供基金名称、代码或产品资料概要。", source_status="unavailable")
        page, fetched = await self._read(FUND_URL)
        products, _ = self._fund_directory(page)
        hint = query.product_hint or ""
        linked = "联接" in hint
        code = re.search(r"(?:代码|基金|ETF)\s*[:：]?\s*(\d{6})(?!\d)", hint, re.I)
        candidates = sorted([p for p in products.values() if ETF.search(p["name"])
                      and ("联接" in p["name"]) == linked and (not code or p["code"] == code[1])],key=lambda p:p['code'])[:3]
        from .etf_catalog import check_conditions
        async def enrich(product):
            profile = await asyncio.to_thread(self.catalog.get,product['code'])
            if profile is None:
                try:
                    value = await asyncio.wait_for(self._load_etf_profile(product),timeout=3)
                    profile = await asyncio.to_thread(self.catalog.save,value)
                except (httpx.HTTPError,ValueError,TimeoutError,UnicodeError):
                    profile = {**product,'fetched_at':None,'source_url':product['url']}
            return {**product,'profile':profile,'condition_check':check_conditions(profile,query)}
        inspected = await asyncio.gather(*(enrich(p) for p in candidates))
        candidates = [p for p in inspected if p['condition_check']['status']!='excluded']
        excluded = [p for p in inspected if p['condition_check']['status']=='excluded']
        sources = [{"title": "嘉实基金官方产品目录", "url": FUND_URL, "fetched_at": fetched}]
        if candidates:
            sources.extend({'title':p['name']+'官网资料','url':p['profile'].get('source_url') or p['url'],
                'fetched_at':p['profile'].get('fetched_at') or fetched} for p in candidates)
            text = ('当前已接入的嘉实官方资料中，可进一步核查的候选：'+
                    '、'.join(p['name']+'（'+p['code']+'）' for p in candidates)+'。'
                    '已逐项核对已知条件，未知信息保留为待核实；这不代表完成适合性判断。')
        else:
            text = "当前已接入的嘉实官网目录没有核验到符合这次条件的ETF候选；请提供目标ETF的名称、代码或产品资料概要。"
        text += ("\n场内ETF通常按交易份额与交易单位下单，需要继续核查交易单位、价格、费用及券商是否支持按金额定投，"
                 "不能仅凭每日投入金额就断言可成交。若考虑ETF联接基金，也需另行确认该产品的起投金额和定投规则。"
                 "请补充计划投资多久、能接受多大亏损，以及使用证券账户还是基金平台；资料核实后再判断哪款符合条件。"
                 "ETF存在市场波动和本金损失风险，本次没有购买或设置自动定投。")
        return self._response(text, candidates=candidates, sources=sources,
                              excluded_candidates=excluded, ranking_basis='先排除与已知条件冲突的产品，其余按代码展示；不按目录顺序声称适合',
                              source_status="verified_product_fields" if any(p['profile'].get('management_fee_pct') is not None
                                  or p['profile'].get('custody_fee_pct') is not None for p in inspected)
                                  else "verified_directory_only", suitability="needs_verification")

    @staticmethod
    def _fund_directory(page):
        products = {}
        for name, href in page.links:
            url = urljoin(FUND_URL, href)
            if urlparse(url).hostname != "www.jsfund.cn" or urlparse(url).scheme != "https":
                continue
            code = parse_qs(urlparse(url).query).get("fundcode", [""])[0]
            if name.startswith("嘉实") and re.fullmatch(r"\d{6}", code):
                products[code] = {"name": name, "code": code, "url": url}
        if not products:
            raise ValueError("Fund directory cannot be verified")
        body = "".join(page.text)
        return products, [c for c in ["货币型", "债券型", "股票型", "混合型", "指数型"] if c in body]

    @staticmethod
    def _response(message, **extra):
        return {"status": "ANSWERED", "message": message, "answer_kind": "public_information",
                "execution_mode": "sandbox", **extra}

    async def _rate(self, query):
        if query.currency != "CNY":
            return self._response(
                f"你问的是{query.currency}口径。当前公开数据查询只接入人民币国债收益率，不能用人民币利率代替。请说明需要的期限，或提供对应币种的官方数据，我可以继续解释。"
            )
        page, fetched = await self._read(BOND_URL)
        header = next((r for r in page.rows if r and re.search(r"20\d{2}-\d{2}-\d{2}", r[0]) and "10年" in r), None)
        values = next((r for r in page.rows if r and r[0] in {
            "ChinaBond Government Bond Yield Curve", "中债国债收益率曲线", "中债国债收益率曲线(到期)",
        }), None)
        if header is None or values is None or len(header) != len(values):
            raise ValueError("Source curve cannot be verified")
        data_date = re.search(r"20\d{2}-\d{2}-\d{2}", header[0])[0]
        today = datetime.now(timezone(timedelta(hours=8))).date()
        if date.fromisoformat(data_date) > today:
            raise ValueError("Source observation date is in the future")
        tenor = query.tenor or "10年"
        source = {"title": "中债国债收益率曲线", "url": BOND_URL, "data_date": data_date, "fetched_at": fetched}
        if tenor not in header:
            return self._response(
                f"这份官网表格没有{tenor}期限数据，我不会用其他期限替代。当前可查：{'、'.join(header[1:])}。请在中债官网核对所需期限，或明确选择另一期限后再查询。",
                sources=[source], source_status="verified",
            )
        rate = Decimal(values[header.index(tenor)])
        if not rate.is_finite() or not Decimal("-10") <= rate <= Decimal("30"):
            raise ValueError("Invalid source yield")
        stale = (today - date.fromisoformat(data_date)).days > 7
        prefix = "你没有指定币种和期限，这里先按人民币、10年期国债到期收益率作参考。\n" if query.tenor is None else "按人民币国债到期收益率口径，"
        return self._response(
            prefix + f"官网本次可核验的数据日期是{data_date}，{tenor}期收益率为{rate}%（年化）。"
            "这是日终曲线数据，不是盘中实时报价，也不是唯一的“无风险利率”。"
            "\n用于估值或资金安排时，应与币种、期限和用途匹配；你也可以让我查其他期限。"
            + ("\n这份数据距今已超过一周，只能作为历史参考，不能视为当前利率。请在中债官网核对最新公布日期，或稍后重新查询；尚未核实今日数值。" if stale else ""),
            sources=[source], source_status="stale" if stale else "verified",
            market_data={"currency": "CNY", "tenor": tenor, "yield_pct": str(rate), "data_date": data_date, "frequency": "daily_close"},
        )

    async def _funds(self, query):
        if not query.organization:
            return self._response("你想分析哪只公募基金？请提供基金全名、六位代码或基金公司名称。我会先核对资料，再从投资范围、风险、费用和赎回条件分析。")
        if "嘉实" not in query.organization:
            return self._response(
                f"你关注的是{query.organization}的公募基金。目前只读官网连接已接入嘉实基金，尚未核验这家公司的产品资料。请提供具体基金名称、代码及产品资料概要原文，我可以据此解释；不会用演示理财替代真实基金。现金管理用途尤其要核对风险、投资范围、费用、赎回受理时间与到账条件。"
            )
        page, fetched = await self._read(FUND_URL)
        products, categories = self._fund_directory(page)
        source = {"title": "嘉实基金官方产品目录", "url": FUND_URL, "fetched_at": fetched}
        hint = query.product_hint or ""
        code = re.search(r"(?<!\d)\d{6}(?!\d)", hint)
        named = [p for p in products.values() if p["name"] in hint]
        product = products.get(code[0]) if code else named[0] if len(named) == 1 else None
        if code:
            if not product:
                return self._response(f"这次官网目录没有核验到代码{code[0]}，请核对基金全名和份额类别。不能把相似名称当成同一只基金。", sources=[source])
        if product:
            return self._response(
                f"已在嘉实官网目录核验到：{product['name']}（{product['code']}）。"
                "当前目录只足以确认名称和代码，尚不足以判断最新持仓、费率、回撤及赎回到账时间。"
                "\n请提供这只基金的产品资料概要或相关条款原文，我可以逐项分析投资范围、主要风险、费用及流动性。它与账户里的演示理财产品分别管理。",
                sources=[source, {"title": product["name"] + "官网详情", "url": product["url"], "fetched_at": fetched}],
                source_status="verified_directory_only",
            )
        selected = next((c for c in categories if c[:2] in hint), None)
        candidates = [p for p in products.values() if not selected or selected[:2] in p["name"]][:3]
        examples = "、".join(p["name"] + "（" + p["code"] + "）" for p in candidates)
        return self._response(
            "嘉实基金是一家基金管理公司，旗下有多只不同产品，不能当作单只基金给出统一结论。"
            + (f"官网目录覆盖{'、'.join(categories)}等类型。" if categories else "")
            + (f"\n官网目录中的{'相关' if selected else '部分'}产品示例：{examples}。" if examples else "")
            + "\n分析时需要分别核查投资范围与风险、持仓集中度、回撤与波动、费率、申赎及到账条件；仅凭公司名称不能判断是否适合你。"
            + "当前目录尚不足以核验实时净值、费率和赎回条款，需继续查产品资料概要。"
            + "\n你想先看哪只？可以回复六位基金代码，或先选货币、债券、股票、混合、指数中的一类。这里查询公开资料，不会自动购买基金。",
            sources=[source], source_status="verified_directory_only",
        )
