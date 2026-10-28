"""Shared deterministic wording for the local synthetic product disclosures."""

from decimal import Decimal


def yuan(value):
    return f"{Decimal(value) / 100:,.2f}元"


def disclosure_clauses(p):
    return [
        {"key": "risk_level", "text": f"模拟产品风险等级R{p['risk_level']}；不承诺本金和收益。"},
        {"key": "minimum_fen", "text": f"起购金额{yuan(p['minimum_fen'])}。"},
        {"key": "lock_days", "text": f"申购后锁定{p['lock_days']}个自然日；锁定期内不能赎回。"},
        {"key": "redeem_business_days", "text": f"赎回预计T+{p['redeem_business_days']}个工作日到账；T+0按当前模拟规则当日结算。T+n在15:00及以后提交增加一个工作日。"},
        {"key": "fee_bps", "text": f"赎回费率为{Decimal(p['fee_bps']) / 100}%（按整数分向下取整）。"},
        {"key": "nav_1e4", "text": f"当前模拟定点净值{Decimal(p['nav_1e4']) / 10000}；不据此预测未来收益。"},
        {"key": "calendar", "text": "演示工作日为周一至周五，未接入法定节假日与真实交易日历。"},
        {"key": "liquidity", "text": "赎回申请、持仓市值和预计收入均不属于已到账可用资金。"},
    ]


