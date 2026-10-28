"""Business wording at the presentation boundary; never a source of bank facts."""

from decimal import Decimal
import re

from .funding import incoming_intent as incoming_intent


def yuan(amount_fen):
    value = Decimal(amount_fen) / 100
    return f"{value:,.2f}".removesuffix(".00") + "元"


def aa_collection_notice(node):
    """Describe receipt-backed collection progress, separately from task success."""
    if node.get('status') != 'SUCCEEDED' or node.get('operation') not in {'aa.create', 'aa.status'}:
        return ''
    shares = (node.get('result') or {}).get('data', {}).get('shares', [])
    if not shares:
        return 'AA收款状态尚未核实，请查看最新收款记录。'
    others = [s for s in shares if s['status'] != 'SELF_SHARE']
    pending = [s for s in others if s['status'] == 'UNPAID']
    paid = sum(s['amount_fen'] for s in others if s['status'] == 'PAID')
    if pending:
        prefix = 'AA收款请求已发出；发起时' if node['operation'] == 'aa.create' else '本次查询显示'
        return (f"{prefix}已收{yuan(paid)}，还有{len(pending)}人合计{yuan(sum(s['amount_fen'] for s in pending))}未支付，尚未收齐。"
                '发起人自担份额不收款；发起请求不会再次扣除垫付消费。实际到账以最新收款记录为准。')
    return f"本次查询显示其他参与者应付份额已收齐，共{yuan(paid)}；发起人自担份额不计入收款。"


def revised_plan_title(specs, product_names):
    labels = {
        "funds": "资金办理",
        "bills": "账单分析",
        "wealth": "理财办理",
        "cards": "卡片管理",
        "subscriptions": "订阅管理",
        "life": "生日安排",
    }
    descriptions = []
    for spec in specs:
        label = labels[spec.agent]
        if spec.operation == "funds.transfer":
            label = "转账给" + spec.params.get("recipient_hint", "待确认收款人")
        elif spec.operation == "funds.reserve":
            label = "预留预算"
        elif spec.operation == 'cards.apply':
            label = '申请虚拟卡' if spec.params.get('kind') == 'virtual' else '申请实体卡'
        elif spec.operation in {"wealth.purchase", "wealth.redeem"}:
            label = ("申购" if spec.operation.endswith("purchase") else "赎回") + product_names.get(
                spec.params.get("product_id"), "待选产品"
            )
        amount = spec.params.get("amount_fen", spec.params.get("budget_fen"))
        if isinstance(amount, int):
            label += " " + yuan(amount)
        descriptions.append(label)
    return "办理计划：" + "；".join(descriptions)


QUESTIONS = {
    "recipient": "请告诉我收款人的姓名、手机号或备注。",
    "amount": "你希望办理的金额是多少？",
    "product": "你想选择哪款理财产品？也可以先让我比较可选产品。",
    "date": "你希望安排在哪一天？",
    "risk_answers": "请先完成风险测评，再选择与你风险承受能力相符的产品。",
    "business": "你想办理什么业务？例如转账、查账单、管理卡片或订阅。",
}


def clarification_reply(intent):
    """Model questions may ask for inputs, but may not assert financial results."""
    if intent.reject_reason:
        return (
            "涉及资金或账户安全的操作，需要你本人确认并完成必要验证。密码和验证码请在安全中心填写。"
            if intent.rejection_kind == "security"
            else "这项业务目前还不能直接办理。你可以换一种办理方式，或联系银行工作人员。"
        )
    if intent.clarification_kind in QUESTIONS:
        return QUESTIONS[intent.clarification_kind]
    text = intent.clarification or QUESTIONS["business"]
    # A malformed model response falls back to a focused business question;
    # internal identifiers, units and alleged completions never reach the chat.
    technical = re.search(
        r"[a-z]+\.[a-z_]+|\b\w+_\w+\b|\d+\s*分|工具|接口|沙箱|DAG|JSON|已到账|已转账|已扣款|办理成功", text
    )
    if technical:
        if "收款人" in text:
            return QUESTIONS["recipient"]
        if "产品" in text:
            return QUESTIONS["product"]
        if "日期" in text or "哪天" in text:
            return QUESTIONS["date"]
        if "金额" in text:
            return QUESTIONS["amount"]
        return QUESTIONS["business"]
    return text[:300]


def plan_reply(snapshot):
    nodes = snapshot["nodes"]
    status = snapshot["status"]
    only = nodes[0] if len(nodes) == 1 else None
    if any((n.get("error") or {}).get("code") == "SECURITY_LOCKED" for n in nodes):
        if not snapshot["security_locked"]:
            return "安全锁定已解除。请点击“解锁后继续核对”，重新核对原业务后确认办理，无需转人工。"
        return "账户处于安全锁定状态，这项业务尚未执行。请点击下方“前往安全中心解锁”，验证后回到原业务重新核对即可，无需转人工。"
    if (
        only
        and only["operation"] == "funds.transfer"
        and (only.get("error") or {}).get("code") == "INSUFFICIENT_AVAILABLE"
    ):
        params = only["params"]
        funds = snapshot.get("available_fen")
        if funds is not None and funds >= params["amount_fen"]:
            next_step = (
                "请先在安全中心解除锁定。"
                if snapshot.get("security_locked")
                else "可以点击继续办理，重新核验后按权限确认。"
            )
            return (
                f"向{params['recipient_hint']}转账{yuan(params['amount_fen'])}的安排尚未执行。当前可用余额已足够。"
                + next_step
            )
        reason = f"当前可用余额为{yuan(funds)}，不足以支付这笔转账" if funds is not None else only["error"]["message"]
        return (
            f"你要向{params['recipient_hint']}转账{yuan(params['amount_fen'])}，但{reason}。这笔钱尚未转出。"
            "你可以先向自己的账户转入资金，到账后继续办理；这笔转账安排会保留。"
        )
    if only and only["operation"] == "funds.transfer" and status in ["WAIT_CONFIRM", "WAIT_MFA"]:
        params = only["params"]
        detail = f"向{params['recipient_hint']}转账{yuan(params['amount_fen'])}"
        before = snapshot.get('funding_preview', {}).get('before', {})
        basis = (f" 当前银行余额{yuan(before['balance_fen'])}，其中已预留{yuan(before['reserved_fen'])}，可用{yuan(before['available_fen'])}。"
                 '已有预留保持，工资流水不会再次加到可用余额；历史余额或同意不替代当前事实与本次授权。'
                 if before.get('reserved_fen') and {'balance_fen','available_fen'} <= before.keys() else '')
        if only.get("scheduled_for"):
            detail += "，预约时间为" + only["scheduled_for"][:16].replace("T", " ")
        if status == "WAIT_MFA":
            return detail + "。这笔业务尚未执行，需要本次验证；请核对下方详情，在办理卡片中获取或填写验证码后确认。" + basis
        return detail + "。尚未执行；请核对下方收款人、金额和时间，确认后办理。" + basis
    if status == "COMPLETED":
        aa_notices = [notice for n in nodes if (notice := aa_collection_notice(n))]
        if aa_notices:
            return '\n'.join(aa_notices) + ('\n其他步骤的办理结果与凭证见下方。' if len(nodes) > len(aa_notices) else '')
        if only and only["operation"] == "balance.query":
            data = (only.get("result") or {}).get("data", {})
            if "balance_fen" in data and "available_fen" in data:
                return f"这次查询的账户余额为{yuan(data['balance_fen'])}，其中可用余额为{yuan(data['available_fen'])}。"
        if only and only["operation"] == "bills.report":
            data = (only.get("result") or {}).get("data", {})
            if "consumption_fen" in data and "income_fen" in data:
                if not data.get("transaction_ids"):
                    return f"{data['period']}暂无入账流水记录，不能据此认定没有消费。请调整查询期间，或补充该期账单后再核对。"
                incoming = f"，本人账户资金转入{yuan(data['incoming_fen'])}" if data.get("incoming_fen") else ""
                coverage = (f" 已入账流水覆盖{data['covered_months'][0]}至{data['covered_months'][-1]}，统计截至{data['as_of'][:10]}，不是全年预测。"
                            if data.get('annual') and data.get('covered_months') else '')
                return (f"{data['period']}账单已整理：工资收入{yuan(data['income_fen'])}{incoming}，"
                        f"毛消费{yuan(data['gross_consumption_fen'])}，退款{yuan(data['refund_fen'])}，"
                        f"净消费{yuan(data['consumption_fen'])}（毛消费减退款），转账另列{yuan(data['transfer_fen'])}。{coverage}详细分类见下方。")
        financial = [n for n in nodes if n["status"] == "SUCCEEDED" and n["operation"] == "funds.transfer"]
        if len(nodes) == 1 and financial:
            n = financial[0]
            data = (n.get("result") or {}).get("data", {})
            amount = data.get("amount_fen")
            if amount is not None:
                return f"已在演示账户向{data['recipient']}转账{yuan(amount)}。办理凭证见下方。"
        return "本次办理已完成。查询结果和已完成操作的凭证见下方。"
    if status == "WAIT_MFA":
        funds = snapshot.get('funding_preview', {}).get('before', {})
        basis = (f"当前账户余额{yuan(funds['balance_fen'])}，已预留{yuan(funds['reserved_fen'])}，可用{yuan(funds['available_fen'])}。"
                 if {'balance_fen','reserved_fen','available_fen'} <= funds.keys() else '')
        return basis + "已核对办理安排。本次仍需重新授权，历史同意不能代替这次确认。请核对下方详情，在相应办理卡片中获取或填写验证码后确认。"
    if status == "WAIT_CONFIRM":
        return "已整理好办理安排。请核对下方的对象、金额和时间，确认后再执行涉及变更的步骤。"
    if status == "WAIT_TIME":
        return "当前步骤已处理，剩余安排将在预约时间到来后继续办理；需要确认时会提醒你。"
    if status == "WAIT_INPUT":
        return "还有信息需要补充。请查看下方待处理步骤，补齐后可以继续办理。"
    if status == "UNKNOWN":
        return "银行请求的结果还在核实，暂不能认定成功或失败。请点击“核实银行结果”，查询原提交回执；不要重复付款。"
    if status in ["FAILED", "PARTIAL"]:
        if snapshot.get("support_cases"):
            return "银行已完成部分与待协助部分已分别列出。请查看下方协助工单，核查后继续剩余业务；已完成操作不会重复执行。"
        return "本次安排有步骤未能完成。请查看下方各项结果；已完成的操作不会重复执行。"
    if status == "WAIT_HUMAN":
        return "这份安排已暂停，协助工单与下一步显示在下方。你可以补充信息、查看处理记录，并在核查后继续办理；演示环境可模拟客服核查。"
    if status == "CANCELLED":
        return "剩余安排已取消。此前已完成的转账或申购请以办理凭证为准。"
    return "正在按顺序办理，进度和结果会显示在下方。"
