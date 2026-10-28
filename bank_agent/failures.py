"""Business conditions, security violations and channel faults have separate policies."""

SECURITY_CODES = {"IDEMPOTENCY_CONFLICT", "UNTRUSTED_QUOTE", "GRAPH_OWNER_MISMATCH", "SENSITIVE_BUSINESS_INPUT"}
TECHNICAL_CODES = {"LEDGER_INVARIANT", "BANK_UNAVAILABLE", "ADAPTER_RESPONSE_INVALID", "BANK_TECHNICAL_FAILURE"}
NEXT_STEPS = {
    "PRODUCT_TERMS_UNVERIFIED": "先核实产品条款；可更换资料完整且符合条件的产品，本次没有申购。",
    "INSUFFICIENT_AVAILABLE": "先补充资金或调低金额，再继续原计划；当前步骤尚未付款。",
    "PURCHASE_CONSTRAINT": "核对起购金额与可用资金，可调整金额后继续原计划。",
    "PRODUCT_LOCKED": "等待锁定期结束后再申请赎回；资金仍在原持仓中。",
    "RISK_MISMATCH": "选择风险等级符合当前测评的产品；测评请按真实情况作答。",
    "ASSESSMENT_EXPIRED": "先重新完成风险测评，再继续原计划。",
    "INSUFFICIENT_HOLDING": "核对当前可赎回持仓，调低赎回金额或选择其他持仓。",
    "LIMIT_DECLINED": "申请额度超过银行规则，请调整到允许范围。",
    "CARD_STATE_CONFLICT": "先查看卡片状态，再选择与当前状态对应的业务。",
    "OUT_OF_STOCK": "更换可用商品或等待补货后重新获取报价，再确认付款；无需默认转人工。",
    "MERCHANT_UNSUPPORTED": "本商户未接入自动取消通道。已登记协助工单，可查看核查结果后继续；银行代扣状态另行展示。",
    "SUPPORT_REVIEW_EXPIRED": "打开协助工单更新核查结果，然后重新确认；本步骤没有执行。",
    "RESEARCH_CONDITIONS_CHANGED": "当前条件已变化，补充条件并重新研究后再生成办理方案。",
}


def error_details(error):
    category = "security" if error.code in SECURITY_CODES else "technical" if error.code in TECHNICAL_CODES else "business"
    return {"code": error.code, "message": error.message, "category": category,
            "next_step": NEXT_STEPS.get(error.code, "核对办理条件，补齐或调整后继续原计划。" if category == "business"
                                         else "请在安全中心核验账户。" if category == "security"
                                         else "业务通道暂时受阻，请稍后核实结果或联系工作人员。")}
