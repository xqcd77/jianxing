from sqlalchemy import select

from .security import password_hash
from .storage import Account, Document, LedgerEntry, Transaction, User, save_document, uid

DEMO_PASSWORD = "DemoBank!2026"


def post_money(session, sender, recipient, amount, receipt_id):
    if amount <= 0:
        raise ValueError("positive integer cents required")
    source, target = session.get(Account, sender), session.get(Account, recipient)
    if source is None or target is None:
        raise ValueError("unknown account")
    if not source.system and source.balance_fen < amount:
        raise ValueError("insufficient balance")
    source.balance_fen -= amount
    target.balance_fen += amount
    session.add_all(
        [
            LedgerEntry(id=uid("entry"), receipt_id=receipt_id, account_id=sender, delta_fen=-amount),
            LedgerEntry(id=uid("entry"), receipt_id=receipt_id, account_id=recipient, delta_fen=amount),
        ]
    )


def seed_database(db, auth):
    with db.write() as session:
        if session.scalar(select(User).limit(1)):
            return
        for user_id, username, name in [
            ("u_demo", "demo", "演示用户"),
            ("u_wang", "wang", "小王"),
            ("u_li", "li", "小李"),
        ]:
            session.add(
                User(
                    id=user_id,
                    username=username,
                    name=name,
                    password_hash=password_hash(DEMO_PASSWORD),
                    totp_encrypted=auth.new_totp(),
                    risk_level=2,
                    risk_valid_until="2027-10-13",
                    is_demo_admin=user_id == "u_demo",
                )
            )
            session.add(Account(id=f"a_{username}", user_id=user_id, name=f"{name}活期", balance_fen=0, system=False))
        for account_id in ["a_mom", "a_landlord", "sys_payroll", "sys_merchants", "sys_wealth"]:
            session.add(Account(id=account_id, user_id="system", name=account_id, balance_fen=0, system=True))
        session.flush()
        post_money(session, "sys_payroll", "a_wang", 200000, "opening_wang")
        post_money(session, "sys_payroll", "a_li", 200000, "opening_li")
        save_document(session, "business_clock", "clock", "system", {"now": "2026-10-13T09:00:00+08:00"})
        contacts = [
            ("mom", "妈妈", "13800000001", "a_mom", ["妈", "母亲", "妈妈"], ["生活费"]),
            ("wang", "小王", "13800000002", "a_wang", ["小王", "王同学"], ["AA", "聚餐"]),
            ("li", "小李", "13800000003", "a_li", ["小李", "李同学"], ["AA"]),
            ("landlord", "房东", "13800000004", "a_landlord", ["房东", "赵先生"], ["房租", "租金"]),
        ]
        for contact_id, name, phone, account, aliases, notes in contacts:
            save_document(
                session,
                f"contact_{contact_id}",
                "contact",
                "u_demo",
                {
                    "id": contact_id,
                    "name": name,
                    "phone": phone,
                    "account_id": account,
                    "aliases": aliases,
                    "notes": notes,
                    "verified": True,
                    "source": "用户确认的沙箱联系人",
                },
            )
        for product in [
            {
                "id": "P_CASH",
                "name": "模拟现金产品",
                "risk_level": 1,
                "minimum_fen": 10000,
                "lock_days": 0,
                "redeem_business_days": 0,
                "fee_bps": 0,
                "nav_1e4": 10000,
            },
            {
                "id": "P_STEADY",
                "name": "模拟稳健产品",
                "risk_level": 2,
                "minimum_fen": 10000,
                "lock_days": 0,
                "redeem_business_days": 1,
                "fee_bps": 0,
                "nav_1e4": 10000,
            },
            {
                "id": "P_LOCKED",
                "name": "模拟30天产品",
                "risk_level": 3,
                "minimum_fen": 50000,
                "lock_days": 30,
                "redeem_business_days": 1,
                "fee_bps": 10,
                "nav_1e4": 10000,
            },
        ]:
            save_document(session, product["id"], "product", "system", {**product, "execution_mode": "sandbox"})
        save_document(
            session,
            "C_MAIN",
            "card",
            "u_demo",
            {
                "id": "C_MAIN",
                "name": "默认演示卡",
                "last4": "2026",
                "kind": "virtual",
                "status": "ACTIVE",
                "credit_limit_fen": 1000000,
                "single_limit_fen": 1000000,
                "daily_limit_fen": 1000000,
                "blocked_categories": [],
            },
        )
        for contract_id, name, amount, merchant in [
            ("S_VIDEO", "视频会员", 2900, "video"),
            ("S_MUSIC", "音乐会员", 1900, "music"),
        ]:
            save_document(
                session,
                contract_id,
                "subscription",
                "u_demo",
                {
                    "id": contract_id,
                    "name": name,
                    "amount_fen": amount,
                    "merchant_id": merchant,
                    "next_debit": "2026-10-20",
                    "bank_mandate": "ACTIVE",
                    "merchant_contract": "ACTIVE",
                    "cancellation_supported": True,
                    "reminder_days": 2,
                },
            )
        save_document(
            session,
            "partner_birthday",
            "life_event",
            "u_demo",
            {
                "name": "爱人生日",
                "date": "2026-10-20",
                "source": "用户确认的演示资料",
                "address": "沙箱配送地址，不涉及真实收件人",
            },
        )
        for item in [
            {
                "id": "FLOWERS",
                "name": "演示鲜花",
                "merchant_id": "flower_shop",
                "category": "flowers",
                "price_fen": 20000,
                "delivery_fen": 0,
                "stock": 100,
            },
            {
                "id": "CAKE",
                "name": "演示蛋糕",
                "merchant_id": "cake_shop",
                "category": "cake",
                "price_fen": 30000,
                "delivery_fen": 0,
                "stock": 100,
            },
        ]:
            save_document(session, item["id"], "catalog", "system", item)
        # Every history row has balancing entries. Month-end cash returns to zero;
        # these are explicitly synthetic data, not a fabricated user bank account.
        for month in range(1, 10):
            rows = [
                ("PAYROLL", 600000, "sys_payroll", "a_demo", "工资", "income", ""),
                ("PAYMENT", 300000, "a_demo", "sys_merchants", "房租", "housing", "rent"),
                ("PAYMENT", 90000, "a_demo", "sys_merchants", "超市", "groceries", "market"),
                ("PAYMENT", 110000, "a_demo", "sys_merchants", "餐饮", "food", "food"),
                ("PAYMENT", 30000, "a_demo", "sys_merchants", "交通", "transport", "metro"),
                (
                    "PAYMENT",
                    47100 - (2900 if month == 4 else 0),
                    "a_demo",
                    "sys_merchants",
                    "水电",
                    "utilities",
                    "utility",
                ),
                ("PAYMENT", 2900, "a_demo", "sys_merchants", "视频会员", "subscription", "video"),
                ("TRANSFER", 20000, "a_demo", "a_wang", "聚餐AA", "transfer", ""),
            ]
            if month == 4:
                rows.append(("PAYMENT", 2900, "a_demo", "sys_merchants", "视频会员重复扣款", "subscription", "video"))
            for index, (kind, amount, source, target, subject, category, merchant) in enumerate(rows):
                receipt = f"seed_{month}_{index}"
                post_money(session, source, target, amount, receipt)
                session.add(
                    Transaction(
                        id=uid("txn"),
                        owner_id="u_demo",
                        kind=kind,
                        amount_fen=amount,
                        sender=source,
                        recipient=target,
                        subject=subject,
                        category=category,
                        merchant_id=merchant,
                        posted_at=f"2026-{month:02d}-01T09:{index:02d}:00+08:00",
                        receipt_id=receipt,
                    )
                )
        assert session.get(Account, "a_demo").balance_fen == 0
        session.add(Document(id="seed_version", kind="meta", owner_id="system", data={"version": 1}))
