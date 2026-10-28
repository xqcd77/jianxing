"""Read-only financial education and calculations from explicitly supplied inputs.

No account facts, current quotes, approval or investment promises are produced.
Sources are recorded in docs/业务修复与同口径复测_2026-10-16.md.
"""
from decimal import Decimal, ROUND_HALF_UP
import re


def explanation(message):
    from .dialogue import money_slot
    from .presentation import yuan

    if re.search(r"(?:给|向).{0,15}转(?:账|款|钱)?|帮我申购|我要申购|帮我赎回", message):
        return None  # Never swallow an accompanying executable bank request.
    if re.search(r'可用余额.*(?:总余额|账户余额)|(?:总余额|账户余额).*可用余额', message) and re.search(r'区别|不同', message):
        return ('总余额是已经入账的账户资金；可用余额是总余额扣除预留等占用后当前能使用的钱。'
                '预计收入和尚未到账的赎回款不计入当前可用余额。本次只解释概念，不会付款或取消已有安排。')
    if '单利' in message and re.search(r'假设|只计算|仅计算', message):
        principal = re.search(r'本金\s*(\d+(?:\.\d{1,2})?\s*[万千]?元)', message)
        rate = re.search(r'年(?:单利|利率)\s*(\d+(?:\.\d+)?)\s*[%％]', message)
        days = re.search(r'(?:存|持有|期限)\s*(\d+)\s*天', message)
        basis = re.search(r'(?:按|以)\s*(360|365)\s*天', message)
        if principal and rate and days and basis:
            amount = money_slot(principal[1])
            interest = (Decimal(amount)*Decimal(rate[1])/100*int(days[1])/int(basis[1])).quantize(Decimal('1'), rounding=ROUND_HALF_UP)
            return (f"按你给定的假设单利：{principal[1]}×{rate[1]}%×{days[1]}÷{basis[1]}，利息四舍五入为{yuan(int(interest))}。"
                    "这只是按指定本金、年利率和计息天数的示意计算，不是当前银行报价；本次没有购买或办理产品。")
    if re.search(r"七日年化|7日年化", message) and re.search(r"是什么|是不是|一定|保证|保本|什么意思|收益", message):
        principal = re.search(r"(?:买|投入?|本金)\s*([\d.]+\s*[万千]?元)", message)
        rate = re.search(r"(?:七日年化|7日年化)(\d+(?:\.\d+)?)%", message)
        example = ""
        if principal and rate and (amount := money_slot(principal[1])):
            hypothetical = Decimal(amount)*Decimal(rate[1])/100
            example = f"例如你提供的本金与比率，只有全年维持同一水平等假设成立，示意收益才约为{yuan(int(hypothetical))}，不能说一定取得。"
        return ("七日年化反映最近七天收益按规则折算的历史表现，不是未来一年的固定利率，也不保证按这个水平取得收益。" + example +
                "只有全年恰好维持相同水平等假设成立，才能用本金乘该年化率作示意计算，不能把示意当承诺。"
                "货币基金也不是银行存款，不保证本金不亏或最低收益；请核对产品说明书、费用和赎回到账条件。")
    if "存款" in message and re.search(r"理财|基金", message) and re.search(r"一样|区别|一回事|赔|保本", message):
        return ("银行销售的理财或基金与银行存款不是一回事，销售渠道不能决定产品是否保本。低风险等级也不代表本金绝不会损失，或银行会兜底赔偿。"
                "应核对合同主体、产品法律性质、风险揭示和赎回到账条件，不能把存款的保障套到理财或基金。你可以提供产品名称或条款，我再逐项核查。")
    if re.search(r"分\s*\d+\s*期", message) and re.search(r"手续费|年费率|真实年化", message) and "每月" in message:
        principal = re.search(r"(?:账单|本金)\s*([\d.]+\s*[万千]?元)", message)
        periods = re.search(r"分\s*(\d+)\s*期", message)
        fee = re.search(r"手续费\s*(\d+(?:\.\d{1,2})?)\s*元", message)
        if principal and periods and fee and 1 <= int(periods[1]) <= 360:
            amount = money_slot(principal[1])
            if amount:
                total = Decimal(fee[1])*int(periods[1])
                ratio = total*100/(Decimal(amount)/100)
                return (f"按每期手续费相同、共{periods[1]}期计算，总手续费为{yuan(int(total*100))}，占最初本金的{ratio:.2f}%。"
                        "这只是总费用与初始本金的比例；本金逐月归还，实际占用资金减少，不能直接把它当实际年化成本。"
                        "应依据实际还款日期、每期本息费用和完整现金流计算IRR，并区分名义年化与考虑复利的有效年化。"
                        "等额本金按分取整可能有尾差，末期金额应以合同调整为准。本次仅解释和计算，没有办理分期。")
    if re.search(r"月息\s*三厘|月利率\s*三厘", message):
        principal = re.search(r"(?:借款?|贷款|本金)\s*([\d.]+\s*[万千]?元)", message)
        year = bool(re.search(r"一年|12个月", message))
        if principal and year and (amount := money_slot(principal[1])):
            interest = Decimal(amount)*Decimal('0.003')*12
            return ("月息三厘通常指月利率0.3%，仍应向银行核对合同报价口径。"
                    f"若本金全年不减少、按单利且无其他费用，利息为{yuan(int(interest))}，本息合计{yuan(amount+int(interest))}。"
                    "如果分期还本、按等额本金或等额本息还款，或另收服务费，结果会不同。请确认还款方式、计费本金和费用；这是假设计算，不是银行最终报价，也没有申请或放款。")
    if re.search(r"保本", message) and re.search(r"基金|理财", message) and re.search(r"随时取|随取|高收益|年化\s*\d", message):
        return ("不能凭空承诺存在同时保本、随时取用和固定高收益的基金。收益、风险和流动性条件必须以产品合同和可靠资料核查，不能省略可能影响本金与到账的风险。"
                "可以先确定：本金安全、随时用钱、较高收益，哪项最优先？再比较风险等级、费用、锁定期和赎回到账条件。本次没有推荐未经核验的产品，也不会直接购买。")
    if re.search(r"(?:申请|办理|想贷|帮我贷).{0,15}(?:贷款|消费贷)|(?:贷款|消费贷).{0,12}申请", message):
        stated = re.findall(r"[\d.]+\s*[万千]?元",message)
        amount_text = stated[0] if len(stated)==1 else ""
        return (f"你希望申请{amount_text}贷款。当前演示系统尚未接入贷款申请、授信审批与放款，无法核实或提供你的可贷额度和银行利率报价。"
                "请通过银行官方App的贷款专区或官方客服了解申请要求，可提前准备身份、收入和贷款用途信息，具体资料以银行要求为准。本次没有提交申请。")
    if re.search(r"换成?人民币|换汇|兑换", message) and re.search(r"美元|外币|外汇", message):
        return ("你想办理外汇兑换。当前仅有人民币演示账户，尚未核实美元余额，也未接入银行可成交汇率，不能给出当前可换金额或直接成交。"
                "美元兑换人民币需要核对银行的外币买入价、现汇/现钞口径、点差或费用，以及报价时间。请在银行官方App的外汇账户和结售汇页面核实；“合适就换”不等于对明确金额和汇率的授权。本次没有换汇。")
    if '验证码' in message and re.search(r'演示|测试|不用真实|不想.*短信',message) and re.search(r'怎么|如何|哪里|没有',message):
        return ('演示验证启用时，无需接收真实短信：打开安全中心的解锁表单，或对应业务卡片的验证表单，点击“获取演示验证码”，'
                '将页面显示的验证码填回同一表单再提交。验证码仅适用于该次解锁或对应交易，不能通用于其他业务；说明步骤不会自动解锁或执行付款。')
    return None
