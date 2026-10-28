from datetime import datetime
from typing import Annotated, Literal

from pydantic import Field, StrictInt, create_model
from sqlalchemy import func, select

from .errors import BankError, ClarificationNeeded
from .schemas import StrictModel
from .storage import (
    Account,
    Document,
    Transaction,
    documents,
)

Money = Annotated[StrictInt, Field(gt=0, le=100_000_000)]
Positive = Annotated[StrictInt, Field(gt=0)]

# Each operation has one owner and an explicit input schema. Neither model output
# nor API callers can add confirmed/admin/verified flags to obtain authority.
DEFINITIONS = {
    "balance.query": ("funds", {}, "GREEN"),
    "funds.reserve": ("funds", {"amount_fen": (Money, ...), "purpose": (str, ...), "goal_id": (str, "")}, "YELLOW"),
    "funds.release": ("funds", {"hold_id": (str, ...)}, "YELLOW"),
    "funds.transfer": (
        "funds",
        {"recipient_hint": (str, ...), "amount_fen": (Money, ...), "subject": (str, "转账")},
        "DYNAMIC",
    ),
    "aa.create": (
        "funds",
        {"amount_fen": (Money, ...), "participants": (list[str], ...), "subject": (str, "AA收款")},
        "YELLOW",
    ),
    "aa.status": ("funds", {"request_id": (str, ...)}, "GREEN"),
    "aa.pay": ("funds", {"request_id": (str, ...)}, "DYNAMIC"),
    "bills.report": ("bills", {"period": (str, "2026-10"), "annual": (bool, False)}, "GREEN"),
    "bills.anomalies": ("bills", {}, "GREEN"),
    "cashflow.income_plan": (
        "bills",
        {"date": (str, ...), "amount_fen": (Money, ...), "label": (str, "预计工资")},
        "YELLOW",
    ),
    "cashflow.project": (
        "bills",
        {
            "days": (Annotated[StrictInt, Field(ge=1, le=366)], 30),
            "income_delay_days": (Annotated[StrictInt, Field(ge=0, le=60)], 0),
            "expense_increase_pct": (Annotated[StrictInt, Field(ge=0, le=100)], 0),
        },
        "GREEN",
    ),
    "wealth.compare": ("wealth", {}, "GREEN"),
    "wealth.assess": ("wealth", {"answers": (list[Annotated[StrictInt, Field(ge=1, le=5)]], ...)}, "YELLOW"),
    "wealth.purchase": ("wealth", {"product_id": (str, ...), "amount_fen": (Money, ...)}, "RED"),
    "wealth.redeem": ("wealth", {"product_id": (str, ...), "amount_fen": (Money, ...)}, "RED"),
    "wealth.holdings": ("wealth", {}, "GREEN"),
    "cards.query": ("cards", {}, "GREEN"),
    "cards.apply": ("cards", {"kind": (str, "virtual"), "name": (str, "日常虚拟卡")}, "YELLOW"),
    "cards.limit": ("cards", {"card_id": (str, "C_MAIN"), "amount_fen": (Money, ...)}, "RED"),
    "cards.restrict": (
        "cards",
        {
            "card_id": (str, "C_MAIN"),
            "single_limit_fen": (Money, 100000),
            "daily_limit_fen": (Money, 100000),
            "blocked_categories": (list[Literal["gifts", "gaming"]], []),
        },
        "YELLOW",
    ),
    "cards.unlock": ("cards", {"card_id": (str, "C_MAIN")}, "RED"),
    "cards.lost": ("cards", {"card_id": (str, "C_MAIN")}, "RED"),
    "cards.restore": ("cards", {"card_id": (str, "C_MAIN")}, "RED"),
    "subscriptions.detect": ("subscriptions", {}, "GREEN"),
    "subscriptions.query": ("subscriptions", {"name_hint": (str, "")}, "YELLOW"),
    "subscriptions.cancel": ("subscriptions", {"subscription_id": (str, ...), "scope": (str, "both"), "contract_version": (str, ""),
        "assistance_case_id": (str, ""), "assistance_version": (StrictInt, 0)}, "YELLOW"),
    "subscriptions.remind": (
        "subscriptions",
        {"subscription_id": (str, ...), "days_before": (Annotated[StrictInt, Field(ge=0, le=30)], 2), "contract_version": (str, "")},
        "YELLOW",
    ),
    "life.catalog": ("life", {}, "GREEN"),
    "life.goal": (
        "life",
        {
            "birthday_date": (str, ...),
            "budget_fen": (Money, 100000),
            "address": (str, "沙箱配送地址"),
            "card_id": (str, "C_MAIN"),
        },
        "YELLOW",
    ),
    "life.purchase": ("life", {"goal_id": (str, ...), "card_id": (str, "C_MAIN"), "quote": (dict, {})}, "DYNAMIC"),
    "life.orders": ("life", {}, "GREEN"),
    "life.cancel_order": ("life", {"order_id": (str, ...)}, "YELLOW"),
}
PARAM_MODELS = {
    name: create_model(name.replace(".", "_") + "Params", __base__=StrictModel, **fields)
    for name, (_, fields, _) in DEFINITIONS.items()
}

# Semantic contracts alongside input types: a valid shape alone does not tell
# a model which money flow or service boundary an operation represents.
OPERATION_DESCRIPTIONS = {
    'funds.transfer':'从本人当前可用余额转给已验证联系人；收款人不是新卡、银行卡别名或外部钱包。金额为该笔转账金额，不是总预算。',
    'balance.query':'读取已记账总余额、预留金额与当前可用余额；预计收入不计可用。',
    'funds.reserve':'冻结一笔本人可用资金以保护预算，不消费、不充值银行卡、不购买产品。普通生活费等预算预留的goal_id留空；仅关联真实已存在的目标ID或合法前置步骤返回引用，禁止把用途编造成目标ID。',
    'funds.release':'仅按用户明确授权释放指定预留，不能为了转账余额不足自动使用。',
    'aa.create':'发起一笔整单AA收款：amount_fen是全体总额，不是单个参与者应还额；participants包含发起人和全部已登记参与者。按分平均分配尾差，发起人自付不收款，其他人各自确认付款；创建请求不代表收齐或再次支付整单。',
    'wealth.compare':'只比较银行内合成演示产品；不是公开ETF行情或真实基金推荐。',
    'wealth.purchase':'申购用户明确选定的银行内模拟产品；不支持证券ETF或自动定投，不自动选择产品。',
    'wealth.redeem':'提交所选持仓赎回；申请成功不代表资金已到账。',
    'cards.apply':'申请虚拟或实体付款卡，返回card_id；共用本人借记账户，没有独立充值余额，不是开证券账户。',
    'cards.restrict':'设置已指定卡的单笔/每日消费上限和类别限制；没有资金划转。新卡须引用申请结果card_id。',
    'cards.limit':'调整卡片已审批交易额度，不增加余额，不代表充值或真实授信。',
    'subscriptions.query':'经本人确认查询已有商户订阅合同，不新开通会员。',
    'subscriptions.cancel':'按本人选择撤销银行代扣和/或取消已有商户合同，不开通、不退款。',
    'subscriptions.remind':'给已有订阅设置提醒，不购买、续费或变更套餐。',
}


def business_now(session):
    return datetime.fromisoformat(session.get(Document, "business_clock").data["now"])


def primary_account(session, user_id):
    account = session.scalar(select(Account).where(Account.user_id == user_id, Account.system.is_(False)))
    if not account:
        raise BankError("ACCOUNT_NOT_FOUND", "未找到本人账户", 404)
    return account


def held_total(session, user_id):
    return sum(
        doc.data["remaining_fen"] for doc in documents(session, "hold", user_id) if doc.data["status"] == "ACTIVE"
    )


def available(session, user_id):
    return primary_account(session, user_id).balance_fen - held_total(session, user_id)


def owned_document(session, doc_id, kind, user_id):
    doc = session.get(Document, doc_id)
    if doc is None or doc.kind != kind or doc.owner_id != user_id:
        raise BankError("RESOURCE_NOT_FOUND", "业务对象不存在或不属于当前用户", 404)
    return doc


def resolve_contact(session, user_id, hint):
    matches = []
    for doc in documents(session, "contact", user_id):
        value = doc.data
        if value["verified"] and hint in [
            value["id"],
            value["name"],
            value["phone"],
            *value["aliases"],
            *value["notes"],
        ]:
            matches.append(value)
    if len(matches) != 1:
        raise ClarificationNeeded("收款人不明确，请使用已验证联系人姓名、手机号或唯一备注")
    return matches[0]


def daily_outgoing(session, user_id):
    today = business_now(session).date().isoformat()
    return session.scalar(
        select(func.coalesce(func.sum(Transaction.amount_fen), 0)).where(
            Transaction.owner_id == user_id,
            Transaction.kind.in_(["TRANSFER", "PAYMENT", "AA_PAYMENT"]),
            Transaction.posted_at.startswith(today),
        )
    )


def risk_tier(session, user_id, operation, params):
    base = DEFINITIONS[operation][2]
    if operation == "cards.apply" and params["kind"] != "virtual":
        return "RED"
    if base != "DYNAMIC":
        return base
    amount = params.get("amount_fen", 0)
    if operation == "life.purchase":
        amount = params.get("quote", {}).get("total_fen", 0)
    if operation == "aa.pay":
        request = session.get(Document, params["request_id"])
        if not request or request.kind != "aa" or user_id not in [row["user_id"] for row in request.data["shares"]]:
            raise BankError("RESOURCE_NOT_FOUND", "收款请求不存在或无权访问", 404)
        amount = next(row["amount_fen"] for row in request.data["shares"] if row["user_id"] == user_id)
    return "RED" if daily_outgoing(session, user_id) + amount > 100000 else "YELLOW"


def card_daily_spent(session, user_id, card_id):
    today = business_now(session).date().isoformat()
    return sum(
        doc.data["amount_fen"]
        for doc in documents(session, "card_payment", user_id)
        if doc.data["card_id"] == card_id and doc.data["posted_at"].startswith(today)
    )

