"""Real bank fixtures and controlled external events for specialist20.

Only setup data and fault timing are controlled. All planning, permission checks,
submissions, receipts, research and UI responses are those of the application.
"""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from threading import Event

from sqlalchemy import delete, select

from bank_agent.adapters import SandboxBankAdapter
from bank_agent.research_tools import PRODUCT_FIELDS
from bank_agent.product_disclosures import disclosure_clauses
from bank_agent.schemas import ApprovalRequest
from bank_agent.seed import post_money
from bank_agent.storage import Account, Document, LedgerEntry, Receipt, Transaction, save_document
from evaluation.runner import EpisodeError, financial_signature
from evaluation.schemas import Step
from .runner import ServiceEpisode


class SpecialistEpisode(ServiceEpisode):
    def __init__(self, scenario, *args, **kwargs):
        super().__init__(scenario, *args, **kwargs)
        self.fixture = next(p for p in self.case.patches if p["kind"] == "specialist_initial")
        self.key = self.fixture["id"]
        self.case = self.case.model_copy(update={"patches": [p for p in self.case.patches if p["kind"] != "specialist_initial"]})
        self._seeded = False
        self.overlapping_requests = False
        self.paired_plans = []

    def open(self):
        super().open()
        if self._seeded:
            return
        self._seeded = True
        with self.app.state.db.write() as s:
            # A new isolated account history, rather than unrelated seeded months.
            s.execute(delete(Transaction))
            s.execute(delete(LedgerEntry))
            s.execute(delete(Receipt))
            for account in s.scalars(select(Account)):
                account.balance_fen = 0
            for row in list(s.scalars(select(Document))):
                if row.kind in {"subscription", "product", "product_disclosure", "holding", "hold", "expected_income"}:
                    if row.kind == "subscription" and self.key in {"MA03", "MA09"}:
                        continue
                    s.delete(row)
            s.flush()
            save_document(s, "business_clock", "clock", "system", {"now": self.fixture["clock"]})
            for source in self.fixture["sources"]:
                if source.get("visibility") != "available_after_clock_and_controller_event":
                    save_document(s, source["source_id"], "financial_evidence", "u_demo", deepcopy(source))
            self.seed_products(s)
            self.seed_obligations(s)
            if self.key == "MA04":
                s.add(Account(id="a_father", user_id="external_father", name="爸爸", balance_fen=0, system=True))
                save_document(s, "contact_father", "contact", "u_demo", {"id": "U_FATHER", "name": "爸爸", "account_id": "a_father", "aliases": ["爸", "父亲"], "verified": True, "phone": "13800001111", "notes": [], "source": "synthetic_verified_contact"})
                self.transaction(s, "earlier_transfer", 20000, "TRANSFER", "2026-10-13T08:00:00+08:00", "当天此前转账", "transfer", recipient="a_mom")
            if self.key == "MA07":
                card = s.get(Document, "C_MAIN")
                card.data = {**card.data, "single_limit_fen": 150000}
                landlord = s.get(Document, "contact_landlord")
                landlord.data = {**landlord.data, "id": "U_LANDLORD", "name": "王芳", "last4": "1122", "aliases": ["房东", "房东王芳"]}
                save_document(s, "contact_colleague", "contact", "u_demo", {"id": "U_COLLEAGUE", "name": "王芳", "last4": "7788", "account_id": "a_wang", "aliases": ["同事"], "verified": True, "phone": "13800007788", "notes": [], "source": "synthetic_verified_contact"})
            if self.key in {"MA10", "DR08"}:
                amount = 100000 if self.key == "MA10" else 200000
                save_document(s, "existing_emergency", "hold", "u_demo", {"id": "existing_emergency", "amount_fen": amount, "remaining_fen": amount, "status": "ACTIVE", "purpose": "已有应急资金", "goal_id": ""})
            if self.key == "DR01":
                self.transaction(s, "SETTLE49", 4900, "PAYMENT", "2026-10-12T12:00:00+08:00", "视频订单ORDER49实际结算，关联AUTH49预授权", "subscription", "video")
                # Authorization/release are source records, never posted debits.
            if self.key == "DR02":
                self.transaction(s, "SEPBUY", 10000, "PAYMENT", "2026-09-28T09:00:00+08:00", "旅行订单SEP_ORDER", "travel", "travel")
                self.transaction(s, "OCTBUY", 60000, "PAYMENT", "2026-10-01T09:00:00+08:00", "旅行订单OCT_ORDER", "travel", "travel")
                self.transaction(s, "R200", 20000, "REFUND", "2026-10-10T09:00:00+08:00", "退款原订单OCT_ORDER", "travel", "travel")
                self.transaction(s, "R100", 10000, "REFUND", "2026-10-11T09:00:00+08:00", "退款原订单SEP_ORDER", "travel", "travel")
            if self.key == "DR09":
                for tx, order, ref in (("T129_A", "O101", "BR_A"), ("T129_B", "O102", "BR_B")):
                    self.transaction(s, tx, 12900, "PAYMENT", "2026-10-12T10:00:00+08:00", f"视频订单{order} 银行凭据{ref}", "subscription", "video")
        # Prepare restores the exact independently specified opening cash using
        # balanced ledger entries. History is not counted as measured writes.

    def transaction(self, s, key, amount, kind, at, subject, category, merchant="", recipient="sys_merchants"):
        incoming = kind == "REFUND"
        sender, target = ("sys_merchants", "a_demo") if incoming else ("a_demo", recipient)
        # Opening fixture payments may precede payroll; allow only this fixture
        # by providing a balanced opening credit first, without negative cash.
        if not incoming and s.get(Account, "a_demo").balance_fen < amount:
            post_money(s, "sys_payroll", "a_demo", amount, "opening_"+key)
        post_money(s, sender, target, amount, key)
        s.add(Transaction(id=key, owner_id="u_demo", kind=kind, amount_fen=amount,
            sender=sender, recipient=target, subject=subject, category=category,
            merchant_id=merchant, posted_at=at, receipt_id=key))

    def product(self, s, key, name, risk=1, minimum=10000, lock=0, days=0, fee=0, nav=10000, incomplete=False):
        value = {"id": key, "name": name, "risk_level": risk, "minimum_fen": minimum,
                 "lock_days": lock, "redeem_business_days": days, "fee_bps": fee, "nav_1e4": nav,
                 "execution_mode": "sandbox"}
        if incomplete:
            # Unknown remains unknown; do not invent T+0/zero fee for missing A.
            value["redeem_business_days"] = None
            value["fee_bps"] = None
        save_document(s, key, "product", "system", value)
        fields = {k: v for k, v in value.items() if k in PRODUCT_FIELDS and v is not None}
        save_document(s, "disclosure_"+key, "product_disclosure", "system", {
            "product_id": key, "name": name, "fields": fields,
            "clauses": [] if incomplete else disclosure_clauses(value),
            "supporting_sources": [deepcopy(x) for x in self.fixture["sources"] if any(w in x["title"] for w in ("条款", "合同", "营业日历", "份额", "净值"))],
            "source": "synthetic_bank_contract", "version": 4 if self.key == "DR05" else 1})

    def holding(self, s, key, value, unlock="2026-10-01", shares_milli=None):
        product = s.get(Document, key).data
        units = shares_milli if shares_milli is not None else value*100000//product["nav_1e4"]
        save_document(s, "holding_u_demo_"+key, "holding", "u_demo", {
            "product_id": key, "units_milli": units, "principal_fen": value, "available_after": unlock,
            "lots": [{"id": "opening_position_"+key, "units_milli": units, "principal_fen": value,
                      "available_after": unlock, "source": "synthetic_opening_position"}]})

    def seed_products(self, s):
        if self.key == "DR05":
            self.product(s, "P_NEW30", "新稳健30天", risk=2, lock=30, days=1)
        elif self.key == "DR06":
            self.product(s, "P_A", "安享A", incomplete=True)
            self.product(s, "P_B", "安享B")
        elif self.key == "DR07":
            self.product(s, "P_C", "稳盈C", risk=2, minimum=500000, days=1, fee=100, nav=10200)
            self.holding(s, "P_C", 100000, "2026-10-16", shares_milli=1000000)
        elif self.key == "DR10":
            self.product(s, "P_X", "日开X")
            self.product(s, "P_Y", "定开Y", risk=2, minimum=500000, lock=30, days=1)
        else:
            self.product(s, "P_CASH", "日开理财")
            if self.key in {"MA05", "DR04"}:
                self.product(s, "P_STEADY", "稳健产品", risk=2, days=1)
                self.holding(s, "P_STEADY", 300000 if self.key == "MA05" else 500000)
            if self.key == "DR04":
                self.holding(s, "P_CASH", 250000)

    def obligation(self, s, key, name, amount, date):
        save_document(s, key, "subscription", "u_demo", {"id": key, "name": name, "amount_fen": amount,
            "merchant_id": key, "next_debit": date[:10], "bank_mandate": "ACTIVE", "merchant_contract": "ACTIVE",
            "cancellation_supported": True, "reminder_days": 2})

    def income(self, s, amount, at):
        save_document(s, "expected_salary", "expected_income", "u_demo", {
            "id": "expected_salary", "amount_fen": amount, "date": at[:10], "at": at,
            "label": "预计工资", "source": "user_confirmed", "counted_as_spendable": False})

    def seed_obligations(self, s):
        if self.key == "MA01":
            self.obligation(s, "D_ELECTRIC", "电费", 30000, "2026-10-15")
            self.obligation(s, "S_MUSIC", "音乐", 2000, "2026-10-16")
        if self.key == "MA05":
            self.income(s, 500000, "2026-10-16T18:00:00+08:00")
        if self.key == "DR03":
            self.obligation(s, "S_STRESS", "订阅", 10000, "2026-10-20")
            self.income(s, 300000, "2026-10-18T18:00:00+08:00")
            # Rent/bill are one-off obligations in the original financial evidence,
            # not invented recurring subscriptions. Unsupported reading is a SUT gap.
        if self.key == "DR08":
            self.obligation(s, "D_REGISTERED", "已登记代扣", 120000, "2026-10-20")
        if self.key == "DR10":
            self.income(s, 400000, "2026-10-18T18:00:00+08:00")

    def prepare(self):
        super().prepare()
        if self.key in {"MA10", "DR08"}:
            self.setup_mode = True
            for category, action, params in [("funds", "transfer", {"recipient_hint": "妈妈", "amount_fen": 150000})] + (
                    [("wealth", "purchase", {"product_id": "P_CASH", "amount_fen": 150000})] if self.key == "MA10" else []):
                plan = self.request("POST", f"/api/businesses/{category}/{action}/prepare", {"params": params})
                if plan.get("http_status") != 200 or not plan.get("nodes"):
                    raise EpisodeError("SPECIALIST_PENDING_PLAN_FIXTURE_FAILED")
                if self.key == "MA10":
                    node = next(n for n in plan["nodes"] if n["operation"] in {"funds.transfer", "wealth.purchase"})
                    body = {"node_id": node["id"], "fingerprint": node["fingerprint"]}
                    if node["tier"] == "RED":
                        code = self.request("POST", "/api/security/demo-code", {"purpose": "approve", "plan_id": plan["id"],
                            "node_id": node["id"], "fingerprint": node["fingerprint"]})
                        body.update(otp=code["code"], demo_challenge_id=code["challenge_id"])
                    # Same real authorization service/factor; hold execution at the
                    # test's explicit submission barrier, never manufacture grants.
                    self.app.state.plans.authorize("u_demo", plan["id"], ApprovalRequest(**body))
                    self.authorized[node["id"]] = {"fingerprint": node["fingerprint"], "mfa": node["tier"] == "RED",
                        "tier": node["tier"], "user": "demo", "user_id": "u_demo"}
                    self.paired_plans.append(plan)
            self.setup_mode = False
            self.baseline_signature = financial_signature(self.app)

    def step(self, step):
        if step.kind == "fixture_event":
            event = step.params["event"]
            if event == "external_debit":
                with self.app.state.db.write() as s:
                    self.transaction(s, "external_debit", step.params["amount_fen"], "PAYMENT", step.params["at"], "外部消费入账", "other")
            if event == "restore_lookup":
                self.app.state.plans.adapter = SandboxBankAdapter(self.app.state.bank)
            with self.app.state.db.write() as s:
                save_document(s, "business_clock", "clock", "system", {"now": step.params["at"]})
                if event == "external_debit":
                    save_document(s, "MA08_ACCOUNT_V2", "financial_evidence", "u_demo", {
                        "source_id": "MA08_ACCOUNT_V2", "title": "外部消费后账户实况", "version": "2", "as_of": step.params["at"],
                        "authority": "synthetic_bank_record", "body": {"balance_fen": s.get(Account, "a_demo").balance_fen}})
                # MA09 success evidence is exclusively the actual bank receipt,
                # never the authored future placeholder BANK_RECEIPT_600.
            data = self.request("GET", f"/api/plans/{self.plan['id']}") if self.plan else self.request("GET", "/api/overview")
            return self.capture(step, data)
        if step.kind == "paired_submit":
            if len(self.paired_plans) != 2:
                raise EpisodeError("MISSING_TWO_AUTHORIZED_PLANS")
            first_in_bank, second_started = Event(), Event()
            episode = self
            class BarrierAdapter(SandboxBankAdapter):
                def submit(self, *args):
                    if args[2] == "funds.transfer":
                        first_in_bank.set()
                        if not second_started.wait(20):
                            raise EpisodeError("SECOND_REQUEST_BARRIER_TIMEOUT")
                        episode.overlapping_requests = True
                    return super().submit(*args)
            self.app.state.plans.adapter = BarrierAdapter(self.app.state.bank)
            first, second = self.paired_plans
            def transfer():
                return self.request("POST", f"/api/plans/{first['id']}/resume", {})
            def purchase():
                if not first_in_bank.wait(20):
                    raise EpisodeError("FIRST_REQUEST_BARRIER_TIMEOUT")
                second_started.set()
                return self.request("POST", f"/api/plans/{second['id']}/resume", {})
            with ThreadPoolExecutor(max_workers=2) as pool:
                a, b = pool.submit(transfer), pool.submit(purchase)
                a, b = a.result(timeout=45), b.result(timeout=45)
            self.capture(Step(kind="checkpoint", label="concurrent_purchase"), b)
            return self.capture(step, a)
        return super().step(step)

    def state(self):
        state = super().state()
        active = [n for n in state["nodes"] if n["status"] not in {"CANCELLED", "SUPERSEDED"}]
        state["specialist"] = {"purchase_receipts": state["receipts"].get("wealth.purchase", 0),
            "transfer_tiers": [n["tier"] for n in active if n["operation"] == "funds.transfer"],
            "live_purchase_count": sum(n["operation"] == "wealth.purchase" for n in active),
            "overlapping_requests": self.overlapping_requests}
        return state
