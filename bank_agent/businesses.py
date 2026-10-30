"""Six customer-facing business categories, bound to existing gateway operations."""

from copy import deepcopy
from datetime import date, datetime, timedelta
from typing import Literal

from pydantic import Field, ValidationError
from sqlalchemy import select

from .errors import BankError
from .operations import DEFINITIONS, Money
from .presentation import yuan
from .schemas import Intent, StrictModel
from .storage import Node, Plan, User, audit, documents


def field(name, label, kind="text", **options):
    return {"name": name, "label": label, "kind": kind, "required": True, **options}


def action(key, label, description, operation, fields=(), **options):
    return {
        "id": key,
        "label": label,
        "description": description,
        "operation": operation,
        "fields": list(fields),
        **options,
    }


AMOUNT = field("amount_fen", "金额（元）", "money")
RECIPIENT = field(
    "recipient_hint", "收款人", "recipient", source="contacts", help="可输入已验证姓名、手机号或唯一备注。"
)
CARD = field("card_id", "本人卡片", "select", source="active_cards")
PRODUCT = field("product_id", "理财产品", "select", source="products")
SUBSCRIPTION = field("subscription_id", "订阅合同", "select", source="subscriptions")
BIRTHDAY = field(
    "birthday_date",
    "生日日期",
    "date",
    default_key="birthday",
    help="当月预留预算，生日前两天采购；月初生日需另行调整安排。",
)
BUDGET = field("budget_fen", "生日预算（元）", "money", default=100000)
ADDRESS = field("address", "配送地址", default_key="address", help="请核对演示配送地址；不会向真实商户下单。")

BUSINESSES = [
    {
        "id": "funds",
        "label": "智能转账",
        "icon": "流",
        "description": "转账与预约 · AA收款 · 预算安排",
        "actions": [
            action("aa", "AA收款", "发起收款，或处理自己参与的AA。", None,
                   mode="group", children=["aa_create", "aa_manage"], tier="DYNAMIC"),
            action("aa_manage", "我的AA", "查看参与的收款，待付款时只办理本人份额。", None,
                   mode="aa_records", tier="GREEN"),
            action("budget", "预算管理", "预留或释放本人资金，不改变账户总余额。", None,
                   mode="group", children=["reserve", "release"], tier="YELLOW"),
            action("balance", "余额与可用资金", "查看已入账资金、可用余额和预留预算。", "balance.query"),
            action(
                "transfer",
                "转账给联系人",
                "核对收款人和金额，按日累计金额确认或强验证。",
                "funds.transfer",
                [RECIPIENT, AMOUNT, field("subject", "转账备注", required=False, default="转账")],
            ),
            action(
                "scheduled_transfer",
                "预约转账",
                "到预约时间重查余额和权限，必要时再请求确认。",
                "funds.transfer",
                [
                    RECIPIENT,
                    AMOUNT,
                    field(
                        "scheduled_for", "预约时间（北京时间）", "datetime", help="以页面业务日期为基准，选择未来时间。"
                    ),
                    field("subject", "转账备注", required=False, default="预约转账"),
                ],
            ),
            action(
                "incoming",
                "向本人账户转入",
                "演示账户可确认模拟到账；未接入真实外部银行卡扣款。",
                None,
                [AMOUNT],
                mode="incoming",
                tier="YELLOW",
            ),
            action(
                "aa_create",
                "发起AA收款",
                "按总额分摊并分配零头，发起人计入人数，其他人各自确认支付。",
                "aa.create",
                [
                    AMOUNT,
                    field(
                        "participants",
                        "一起分摊的演示用户",
                        "multi",
                        source="aa_users",
                        help="你会自动作为发起人加入；至少再选择一人。",
                    ),
                    field("subject", "收款事项", default="聚餐AA"),
                ],
            ),
            action(
                "aa_status",
                "查看AA进度",
                "查看本人参与的收款请求及各自份额。",
                "aa.status",
                [field("request_id", "AA收款请求", "select", source="aa_requests")],
            ),
            action(
                "aa_pay",
                "支付本人AA份额",
                "仅支付自己的未付份额，不能代替其他人授权。",
                "aa.pay",
                [field("request_id", "待支付AA请求", "select", source="aa_payable")],
            ),
            action(
                "reserve",
                "预留一笔预算",
                "降低可用余额保护预算，账户总余额保持不变。",
                "funds.reserve",
                [AMOUNT, field("purpose", "预算用途", placeholder="例如：生活费")],
            ),
            action(
                "release",
                "释放预留预算",
                "仅释放本人仍有效的预留，不会撤销已完成付款。",
                "funds.release",
                [field("hold_id", "要释放的预算", "select", source="holds")],
            ),
        ],
    },
    {
        "id": "bills",
        "label": "账单分析",
        "icon": "览",
        "description": "月度与年度账单 · 异常核查 · 现金流",
        "actions": [
            action(
                "monthly",
                "月度消费账单",
                "按账本已有类别汇总，区分工资、转入与消费。",
                "bills.report",
                [field("period", "账单月份", "month", default_key="month")],
                fixed={"annual": False},
            ),
            action(
                "annual",
                "年度消费账单",
                "查看全年收入、资金转入、消费及分类统计。",
                "bills.report",
                [field("period", "账单年份", "year", default_key="year")],
                fixed={"annual": True},
            ),
            action(
                "anomalies", "疑似重复扣款", "核查同商户、同日、同金额重复交易；结果需要人工核实。", "bills.anomalies"
            ),
            action(
                "cashflow",
                "未来现金流与压力测试",
                "仅覆盖登记订阅和已确认预计收入，不将预计收入计入当前余额。",
                "cashflow.project",
                [
                    field("days", "预测天数", "integer", default=30, minimum=1, maximum=366),
                    field("income_delay_days", "收入延迟天数", "integer", default=0, minimum=0, maximum=60),
                    field("expense_increase_pct", "支出增加比例（%）", "integer", default=0, minimum=0, maximum=100),
                ],
            ),
            action(
                "income_plan",
                "登记预计收入",
                "用于未来预测，登记后不会增加已入账或可用余额。",
                "cashflow.income_plan",
                [
                    AMOUNT,
                    field("date", "预计到账日期", "date"),
                    field("label", "收入说明", placeholder="例如：预计工资"),
                ],
            ),
        ],
    },
    {
        "id": "wealth",
        "label": "理财操作",
        "icon": "盈",
        "description": "风险测评 · 产品对比 · 申购与赎回",
        "actions": [
            action("compare", "产品推荐与对比", "按测评、资金和流动性条件筛选模拟产品，不承诺收益。", "wealth.compare"),
            action(
                "assess",
                "完成模拟风险测评",
                "五题均需本人作答，测评结果用于产品适当性校验。",
                "wealth.assess",
                [field("answers", "模拟风险问卷", "risk")],
            ),
            action(
                "purchase",
                "申购理财产品",
                "核验测评、风险、起购金额和余额，申购需要强验证。",
                "wealth.purchase",
                [PRODUCT, AMOUNT],
            ),
            action(
                "redeem",
                "赎回持有产品",
                "核验份额与锁定期；赎回申请成功不等于现金已到账。",
                "wealth.redeem",
                [field("product_id", "持有产品", "select", source="holdings"), AMOUNT],
            ),
            action("holdings", "持仓、收益与结算", "查看模拟净值、持仓收益和赎回到账状态。", "wealth.holdings"),
        ],
    },
    {
        "id": "cards",
        "label": "卡片管理",
        "icon": "守",
        "description": "申请与额度 · 交易限制 · 挂失与解挂",
        "actions": [
            action("query", "查看本人卡片", "查看卡片状态、银行批准额度及当前交易限制。", "cards.query"),
            action(
                "apply",
                "申请新卡",
                "虚拟卡需确认；实体卡需强验证，完成后停在待寄送状态。",
                "cards.apply",
                [
                    field(
                        "kind",
                        "卡片类型",
                        "select",
                        choices=[
                            {"value": "virtual", "label": "虚拟卡"},
                            {"value": "physical", "label": "实体卡（待寄送）"},
                        ],
                        default="virtual",
                    ),
                    field("name", "卡片用途名称", placeholder="例如：日常支付卡"),
                ],
                tier="DYNAMIC",
            ),
            action(
                "limit", "申请调整交易额度", "由模拟银行规则审批，不代表真实信用卡授信。", "cards.limit", [CARD, AMOUNT]
            ),
            action(
                "restrict",
                "设置交易限制",
                "单笔和日限额不能超过银行批准额度；可限制礼品或游戏交易。",
                "cards.restrict",
                [
                    CARD,
                    field("single_limit_fen", "单笔限额（元）", "money", default=50000),
                    field("daily_limit_fen", "日限额（元）", "money", default=100000),
                    field(
                        "blocked_categories",
                        "禁止的交易类别",
                        "multi",
                        required=False,
                        choices=[{"value": "gifts", "label": "礼品"}, {"value": "gaming", "label": "游戏"}],
                    ),
                ],
            ),
            action("unlock", "解除交易限制", "恢复至银行批准额度；不能替代挂失卡的解挂。", "cards.unlock", [CARD]),
            action("lost", "办理卡片挂失", "对本人有效卡片挂失，需要强验证。", "cards.lost", [CARD]),
            action(
                "restore",
                "办理卡片解挂",
                "仅对本人已挂失卡片办理，需要强验证。",
                "cards.restore",
                [field("card_id", "已挂失卡片", "select", source="lost_cards")],
            ),
        ],
    },
    {
        "id": "subscriptions",
        "label": "订阅代扣",
        "icon": "续",
        "description": "识别周期扣费 · 合同管理 · 续费提醒",
        "actions": [
            action(
                "detect",
                "识别订阅扣费候选",
                "从已有账单识别周期消费，候选不等于确认了订阅合同。",
                "subscriptions.detect",
            ),
            action(
                "query", "查看订阅与代扣合同", "核对本人合同详情；此类信息查询先确认，再按权限读取。", "subscriptions.query"
            ),
            action(
                "cancel",
                "撤销代扣或取消订阅",
                "银行代扣与商户合同分别处理，取消不等于退款。",
                "subscriptions.cancel",
                [
                    SUBSCRIPTION,
                    field(
                        "scope",
                        "本次取消范围",
                        "select",
                        choices=[
                            {"value": "bank", "label": "仅撤销银行代扣"},
                            {"value": "merchant", "label": "仅取消商户合同"},
                            {"value": "both", "label": "代扣与商户合同都取消"},
                        ],
                    ),
                ],
            ),
            action(
                "remind",
                "设置续费提醒",
                "在已登记续费日前提醒，不代表暂停扣费或取消合同。",
                "subscriptions.remind",
                [SUBSCRIPTION, field("days_before", "提前提醒天数", "integer", default=2, minimum=0, maximum=30)],
            ),
        ],
    },
    {
        "id": "life",
        "label": "跨场景联动",
        "icon": "序",
        "description": "资金分配 · 预算与现金流 · 计划管理",
        "actions": [
            action(
                "allocation", "资金分配", "用已到账资金组合预算、转账和理财，按选定项目分别确认。", None,
                [
                    field("allocation_kind", "本次安排", "select", default="budget_transfer", choices=[
                        {"value": "budget", "label": "预算与资金分析"},
                        {"value": "budget_transfer", "label": "预算 + 联系人转账"},
                        {"value": "budget_investment", "label": "预算 + 理财申购"},
                        {"value": "budget_transfer_investment", "label": "预算 + 转账 + 理财"},
                    ]),
                    field("budget_fen", "预留预算（元）", "money", default=300000),
                    field("purpose", "预算用途", default="生活备用金"),
                    {**RECIPIENT, "show_when": {"allocation_kind": ["budget_transfer", "budget_transfer_investment"]}},
                    field("transfer_fen", "转账金额（元）", "money", show_when={"allocation_kind": ["budget_transfer", "budget_transfer_investment"]}),
                    {**PRODUCT, "show_when": {"allocation_kind": ["budget_investment", "budget_transfer_investment"]}},
                    field("investment_fen", "申购金额（元）", "money", show_when={"allocation_kind": ["budget_investment", "budget_transfer_investment"]}),
                ], mode="allocation", tier="DYNAMIC",
            ),
            action(
                "budget_cashflow", "预算与现金流", "预留一笔用途预算，核对预留后的可用资金与未来收支。", None,
                [AMOUNT, field("purpose", "预算用途", placeholder="例如：房租、出行、生活备用金"),
                 field("days", "预测天数", "integer", default=30, minimum=1, maximum=366)],
                mode="budget_cashflow", tier="YELLOW",
            ),
            action("plans", "计划管理", "查看办理进度，继续待办或取消未完成安排。", None,
                   mode="plans", tier="GREEN"),
            action(
                "birthday",
                "生日预算与提前采购",
                "当月预留预算，生日前两天核对鲜花、蛋糕与报价后独立确认付款。",
                "life.goal",
                [BIRTHDAY, BUDGET, ADDRESS, CARD],
            ),
            action(
                "birthday_checkout",
                "继续生日计划",
                "恢复已有计划的剩余安排，重查时间、预算、报价及权限，已完成步骤不会重复办理。",
                "life.purchase",
                [field("plan_id", "已有生日采购计划", "select", source="purchase_plans")],
                mode="resume",
            ),
            action("catalog", "查看鲜花与蛋糕", "查看模拟商品、库存和配送费用，查询不会产生订单。", "life.catalog"),
            action("orders", "查看目标与订单", "核对生日安排、订单、配送日期和付款状态。", "life.orders"),
            action(
                "cancel_order",
                "取消订单并退款",
                "仅取消本人可取消的模拟订单，按实际订单金额退款。",
                "life.cancel_order",
                [field("order_id", "可取消订单", "select", source="orders")],
            ),
            action(
                "salary",
                "工资分配与生日预算",
                "使用已入账资金安排生活费、转账、理财及生日预算，逐项授权。",
                None,
                [
                    field("living_fen", "生活费预算（元）", "money", default=300000),
                    RECIPIENT,
                    field("transfer_fen", "转账金额（元）", "money", default=80000),
                    PRODUCT,
                    field("investment_fen", "申购金额（元）", "money", default=50000),
                    BIRTHDAY,
                    BUDGET,
                    ADDRESS,
                    CARD,
                ],
                mode="salary",
                tier="DYNAMIC",
            ),
            action(
                "overview",
                "账户综合分析",
                "汇总资金、账单、理财、卡片、周期扣费和已有安排。",
                None,
                mode="overview",
                tier="GREEN",
            ),
        ],
    },
]

# Navigation expresses customer tasks; the flat registry retains compatible sample APIs.
MENU = {
    "funds": ["balance", "transfer", "scheduled_transfer", "incoming", "aa", "budget"],
    "bills": ["monthly", "annual", "anomalies", "cashflow", "income_plan"],
    "wealth": ["compare", "assess", "purchase", "redeem", "holdings"],
    "cards": ["query", "apply", "limit", "restrict", "unlock", "lost", "restore"],
    "subscriptions": ["detect", "query", "cancel", "remind"],
    "life": ["allocation", "budget_cashflow", "overview", "plans"],
}


class BusinessRequest(StrictModel):
    params: dict = Field(default_factory=dict)
    conversation_id: str | None = None


class IncomingParams(StrictModel):
    amount_fen: Money | None = None


class ResumeParams(StrictModel):
    plan_id: str = Field(min_length=1)


class SalaryParams(StrictModel):
    living_fen: Money
    recipient_hint: str = Field(min_length=1)
    transfer_fen: Money
    product_id: str = Field(min_length=1)
    investment_fen: Money
    birthday_date: str
    budget_fen: Money
    address: str = Field(min_length=1)
    card_id: str = Field(min_length=1)


class AllocationParams(StrictModel):
    allocation_kind: Literal["budget", "budget_transfer", "budget_investment", "budget_transfer_investment"]
    budget_fen: Money
    purpose: str = Field(min_length=1)
    recipient_hint: str | None = Field(default=None, min_length=1)
    transfer_fen: Money | None = None
    product_id: str | None = Field(default=None, min_length=1)
    investment_fen: Money | None = None


class BudgetCashflowParams(StrictModel):
    amount_fen: Money
    purpose: str = Field(min_length=1)
    days: int = Field(default=30, strict=True, ge=1, le=366)


class BusinessService:
    def __init__(self, db, bank, plans, settings):
        self.db, self.bank, self.plans, self.settings = db, bank, plans, settings

    @staticmethod
    def lookup(category_id, action_id):
        for category in BUSINESSES:
            if category["id"] == category_id:
                for item in category["actions"]:
                    if item["id"] == action_id:
                        return item
        raise BankError("BUSINESS_NOT_FOUND", "没有找到这项业务", 404)

    def context(self, user_id):
        facts = self.bank.facts(user_id)
        clock = datetime.fromisoformat(facts["business_now"])
        products = {p["id"]: p for p in facts["products"]}
        resources = {
            "contacts": [
                {"value": c["name"], "label": c["name"] + " · " + c["phone"]}
                for c in facts["contacts"]
                if c.get("verified")
            ],
            "products": [
                {
                    "value": p["id"],
                    "label": p["name"],
                    "detail": f"风险{p['risk_level']}级 · 起购{yuan(p['minimum_fen'])} · 锁定{p['lock_days']}天 · 赎回T+{p['redeem_business_days']}工作日 · 不保证收益",
                }
                for p in products.values()
            ],
            "active_cards": [
                {"value": c["id"], "label": c["name"] + " · 尾号" + c["last4"]}
                for c in facts["cards"]
                if c["status"] == "ACTIVE"
            ],
            "lost_cards": [
                {"value": c["id"], "label": c["name"] + " · 尾号" + c["last4"]}
                for c in facts["cards"]
                if c["status"] == "LOST"
            ],
        }
        with self.db.read() as session:
            resources["subscriptions"] = [
                {"value": d.id, "label": d.data["name"]} for d in documents(session, "subscription", user_id)
            ]
            resources["holds"] = [
                {"value": d.id, "label": d.data["purpose"] + " · " + yuan(d.data["remaining_fen"])}
                for d in documents(session, "hold", user_id)
                if d.data["status"] == "ACTIVE"
            ]
            resources["holdings"] = [
                {
                    "value": d.data["product_id"],
                    "label": products[d.data["product_id"]]["name"],
                    "detail": "可赎回日期以锁定期和后端校验为准。",
                }
                for d in documents(session, "holding", user_id)
                if d.data["units_milli"] > 0
            ]
            resources["orders"] = [
                {
                    "value": d.id,
                    "label": ("鲜花" if d.data["item_id"] == "FLOWERS" else "蛋糕")
                    + " · "
                    + yuan(d.data["amount_fen"])
                    + " · "
                    + d.data["delivery_date"],
                }
                for d in documents(session, "order", user_id)
                if d.data["status"] == "CONFIRMED"
            ]
            resources["aa_users"] = [
                {"value": u.id, "label": u.name} for u in session.scalars(select(User)) if u.id != user_id
            ]
            aa = [d for d in documents(session, "aa") if user_id in [s["user_id"] for s in d.data["shares"]]]
            resources["aa_requests"] = [{"value": d.id, "label": d.data["subject"] + " · " + d.id} for d in aa]
            resources["aa_payable"] = [
                {
                    "value": d.id,
                    "label": d.data["subject"]
                    + " · 本人"
                    + yuan(next(s["amount_fen"] for s in d.data["shares"] if s["user_id"] == user_id)),
                }
                for d in aa
                if any(s["user_id"] == user_id and s["status"] == "UNPAID" for s in d.data["shares"])
            ]
            purchases = list(
                session.scalars(
                    select(Plan)
                    .join(Node, Node.plan_id == Plan.id)
                    .where(
                        Plan.user_id == user_id,
                        Node.operation == "life.purchase",
                        Node.status.not_in(["SUCCEEDED", "CANCELLED", "SKIPPED", "PARTIAL"]),
                        Plan.status.not_in(["COMPLETED", "CANCELLED", "PARTIAL"]),
                    )
                ).unique()
            )
            resources["purchase_plans"] = [{"value": p.id, "label": p.summary} for p in purchases]
        birthday = next(
            (
                e
                for e in facts["life_events"]
                if e.get("date", "") >= (clock.date() + timedelta(days=2)).isoformat()
                and e["date"][-2:] not in {"01", "02"}
            ),
            {},
        )
        suggested_birthday = clock.date() + timedelta(days=7)
        while suggested_birthday.day < 3:
            suggested_birthday += timedelta(days=1)
        return resources, {
            "month": clock.strftime("%Y-%m"),
            "year": clock.strftime("%Y"),
            "birthday": birthday.get("date", suggested_birthday.isoformat()),
            "address": birthday.get("address", ""),
        }

    def catalog(self, user_id, category_id=None, action_id=None):
        resources, defaults = self.context(user_id)
        with self.db.read() as session:
            can_simulate_incoming = self.settings.demo_enabled and session.get(User, user_id).is_demo_admin
        categories = deepcopy(BUSINESSES)
        from .capabilities import REGISTRY
        for category in categories:
            for item in category["actions"]:
                capability = REGISTRY.get(item['operation'])
                item["tier"] = item.get("tier", capability.tier if capability else 'DYNAMIC')
                if capability:
                    item['capability'] = capability.describe(include_schema=False)
                if item.get("mode") == "incoming" and not can_simulate_incoming:
                    item.update(
                        label="本人账户转入指引",
                        description="当前账户提供转出银行办理指引，不会直接增加余额。",
                        tier="GREEN",
                        fields=[],
                    )
                for spec in item["fields"]:
                    if "source" in spec:
                        spec["choices"] = resources[spec["source"]]
                        spec["empty_message"] = {
                            "active_cards": "暂无可用卡片",
                            "lost_cards": "暂无已挂失卡片",
                            "holdings": "暂无可赎回持仓",
                            "holds": "暂无有效预留预算",
                            "orders": "暂无可取消订单",
                            "purchase_plans": "暂无待办生日计划",
                            "aa_requests": "暂无本人参与的AA请求",
                            "aa_payable": "暂无本人待付AA份额",
                            "aa_users": "暂无其他已登记用户",
                            "subscriptions": "暂无已登记订阅合同",
                            "products": "暂无可选产品",
                        }.get(spec["source"], "当前没有可选择的对象")
                    if "default_key" in spec:
                        spec["default"] = defaults[spec["default_key"]]
                item["available"] = all(
                    not f["required"] or f["kind"] not in {"select", "multi"} or bool(f.get("choices"))
                    for f in item["fields"]
                )
        if action_id:
            self.lookup(category_id, action_id)
        for category in categories:
            by_id = {a["id"]: a for a in category["actions"]}
            for item in category["actions"]:
                if item.get("mode") == "group":
                    item["children"] = [by_id[key] for key in item["children"]]
            if action_id and category["id"] == category_id:
                return by_id[action_id]
            category["actions"] = [by_id[key] for key in MENU[category["id"]]]
        return categories

    def my_aa(self, user_id):
        with self.db.write() as session:
            rows = []
            for doc in documents(session, "aa"):
                own = next((s for s in doc.data["shares"] if s["user_id"] == user_id), None)
                if not own:
                    continue
                payable = [s for s in doc.data["shares"] if s["status"] != "SELF_SHARE"]
                rows.append({
                    "request_id": doc.id, "subject": doc.data["subject"],
                    "my_status": own["status"], "my_amount_fen": own["amount_fen"],
                    "collected_fen": sum(s["amount_fen"] for s in payable if s["status"] == "PAID"),
                    "receivable_fen": sum(s["amount_fen"] for s in payable),
                })
            audit(session, user_id, "AA_OVERVIEW_READ", {"count": len(rows)})
            return rows[-30:][::-1]

    def prepare(self, user_id, category_id, action_id, params):
        item = self.lookup(category_id, action_id)
        mode = item.get("mode", "operation")
        object_fields = [
            f for f in item["fields"] if (f.get("source") or f.get("choices")) and f["kind"] in {"select", "multi"}
        ]
        if object_fields:
            resources, _ = self.context(user_id)
            for spec in object_fields:
                if spec["name"] not in params:
                    continue
                choices = resources[spec["source"]] if spec.get("source") else spec["choices"]
                values = params[spec["name"]] if spec["kind"] == "multi" else [params[spec["name"]]]
                if not isinstance(values, list) or any(
                    not isinstance(v, str) or v not in {o["value"] for o in choices} for v in values
                ):
                    raise BankError(
                        "BUSINESS_OBJECT_INVALID",
                        "业务对象或选项不可用，请刷新后重新选择",
                        404 if spec.get("source") else 400,
                    )
        try:
            if mode in {"plans", "aa_records"}:
                StrictModel.model_validate(params)
                return {"mode": mode}
            if mode == "group":
                raise BankError("BUSINESS_GROUP", "请先选择这类业务中需要办理的事项")
            if mode == "incoming":
                return {"mode": mode, "amount_fen": IncomingParams.model_validate(params).amount_fen}
            if mode == "resume":
                data = ResumeParams.model_validate(params)
                snapshot = self.plans.snapshot(user_id, data.plan_id)
                if snapshot["status"] in {"COMPLETED", "CANCELLED", "PARTIAL"} or not any(
                    n["operation"] == "life.purchase"
                    and n["status"] not in {"SUCCEEDED", "CANCELLED", "SKIPPED", "PARTIAL"}
                    for n in snapshot["nodes"]
                ):
                    raise BankError("BUSINESS_NOT_AVAILABLE", "这份计划没有待办理的生日采购", 409)
                return {"mode": mode, "plan_id": data.plan_id}
            if mode == "allocation":
                data = AllocationParams.model_validate(params)
                tasks = [("funds.reserve", {"amount_fen": data.budget_fen, "purpose": data.purpose})]
                transfer = "transfer" in data.allocation_kind
                investment = "investment" in data.allocation_kind
                if transfer != bool(data.recipient_hint and data.transfer_fen) or investment != bool(data.product_id and data.investment_fen):
                    raise BankError("INVALID_BUSINESS_ARGUMENTS", "请完整填写所选业务：转账需要收款人与金额，理财需要产品与金额")
                if (not transfer and (data.recipient_hint or data.transfer_fen)) or (not investment and (data.product_id or data.investment_fen)):
                    raise BankError("INVALID_BUSINESS_ARGUMENTS", "未选择的业务不能附带办理参数")
                if transfer:
                    tasks.append(("funds.transfer", {"recipient_hint": data.recipient_hint, "amount_fen": data.transfer_fen}))
                if investment:
                    tasks.append(("wealth.purchase", {"product_id": data.product_id, "amount_fen": data.investment_fen}))
                tasks.extend([("bills.report", {"period": self.bank.facts(user_id)["business_now"][:7]}), ("cashflow.project", {"days": 30})])
            elif mode == "budget_cashflow":
                data = BudgetCashflowParams.model_validate(params)
                tasks = [("funds.reserve", {"amount_fen": data.amount_fen, "purpose": data.purpose}),
                         ("cashflow.project", {"days": data.days})]
            elif mode == "salary":
                data = SalaryParams.model_validate(params).model_dump()
                tasks = [
                    ("funds.reserve", {"amount_fen": data["living_fen"], "purpose": "生活费"}),
                    ("life.goal", {k: data[k] for k in ["birthday_date", "budget_fen", "address", "card_id"]}),
                    ("funds.transfer", {"recipient_hint": data["recipient_hint"], "amount_fen": data["transfer_fen"]}),
                    ("wealth.purchase", {"product_id": data["product_id"], "amount_fen": data["investment_fen"]}),
                    ("bills.report", {"period": self.bank.facts(user_id)["business_now"][:7], "annual": False}),
                    ("cashflow.project", {"days": 30}),
                ]
            elif mode == "overview":
                StrictModel.model_validate(params)
                tasks = [
                    (op, {"period": self.bank.facts(user_id)["business_now"][:7]} if op == "bills.report" else {})
                    for op in [
                        "balance.query",
                        "bills.report",
                        "wealth.compare",
                        "cards.query",
                        "subscriptions.detect",
                        "life.orders",
                    ]
                ]
            else:
                raw = dict(params)
                when = raw.pop("scheduled_for", None) if action_id == "scheduled_transfer" else None
                if action_id == "scheduled_transfer":
                    if (
                        not when
                        or datetime.fromisoformat(when).utcoffset() != timedelta(hours=8)
                        or datetime.fromisoformat(when)
                        <= datetime.fromisoformat(self.bank.facts(user_id)["business_now"])
                    ):
                        raise BankError("INVALID_SCHEDULE", "请选择晚于页面业务时间的北京时间预约时间")
                if item["operation"] == "aa.create":
                    members = raw.get("participants", [])
                    if not isinstance(members, list) or any(not isinstance(p, str) for p in members):
                        raise BankError("INVALID_BUSINESS_ARGUMENTS", "请选择已登记的AA参与用户")
                    raw["participants"] = list(dict.fromkeys([user_id, *members]))
                    if len(raw["participants"]) < 2:
                        raise BankError("INVALID_BUSINESS_ARGUMENTS", "AA至少两人，请再选择一位参与者")
                    with self.db.read() as session:
                        if any(session.get(User, p) is None for p in raw["participants"]):
                            raise BankError("INVALID_BUSINESS_ARGUMENTS", "AA参与者必须是已登记用户")
                if "annual" in raw and raw["annual"] != item.get("fixed", {}).get("annual"):
                    raise BankError("INVALID_BUSINESS_ARGUMENTS", "月度与年度账单类型不能通过参数改变")
                raw.update(item.get("fixed", {}))
                tasks = [(item["operation"], raw)]
        except (ValidationError, ValueError, TypeError) as error:
            raise BankError("INVALID_BUSINESS_ARGUMENTS", "请核对必填信息、金额、时间与业务对象") from error
        domain_tasks = []
        clock_day = datetime.fromisoformat(self.bank.facts(user_id)["business_now"]).date()
        for operation, raw in tasks:
            validated = self.bank.validate_params(operation, raw)
            if operation == "wealth.assess" and len(validated["answers"]) != 5:
                raise BankError("INVALID_RISK_ANSWERS", "五道测评题都需要本人作答，请完整填写")
            if (
                operation == "bills.report"
                and validated["annual"]
                and (len(validated["period"]) != 4 or not validated["period"].isdigit())
            ):
                raise BankError("INVALID_REPORT_YEAR", "年度账单请填写四位年份，例如2026")
            if operation == "life.goal":
                birthday = date.fromisoformat(validated["birthday_date"])
                if birthday - timedelta(days=2) < max(clock_day, birthday.replace(day=1)):
                    raise BankError("INVALID_BIRTHDAY_DATE", "采购安排需在当月且不早于当前业务日期，请调整生日日期")
            if operation == "cashflow.income_plan" and date.fromisoformat(validated["date"]) <= clock_day:
                raise BankError("INVALID_INCOME_DATE", "预计收入日期须晚于当前业务日期，不能将未到账资金记为当前余额")
            for name, value in validated.items():
                if name in {"purpose", "recipient_hint", "label", "name", "address"} and not value.strip():
                    raise BankError("INVALID_BUSINESS_ARGUMENTS", "请填写完整的业务信息")
            domain_tasks.append(
                {
                    "domain": DEFINITIONS[operation][0],
                    "action": operation,
                    "params": validated,
                    **({"scheduled_for": when} if mode == "operation" and when else {}),
                }
            )
        if mode in {"allocation", "budget_cashflow"}:
            for index, task in enumerate(domain_tasks):
                task["id"] = f"t{index}"
                if index:
                    task["depends_on"] = ["t0"]
        intent = Intent(summary=item["label"], tasks=domain_tasks)
        if mode in {"allocation", "budget_cashflow"}:
            from .agents import ReviewerAgent, expand_tasks
            specs = expand_tasks(intent, self.bank.facts(user_id)["business_now"])
            self.plans.simulator.ensure(user_id, specs, ReviewerAgent().review(specs))
        return {"mode": "plan", "intent": intent, "message": item["label"]}
