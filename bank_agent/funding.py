"""Chinese funding vocabulary with explicit direction and contextual account binding.

This classifier only proposes a funding confirmation or asks for missing fields.
It cannot post a balance, grant authorization, or bind an unknown bank account.
"""

import re

from .errors import ClarificationNeeded

OWN_ACCOUNT = (
    r"(?:我(?:自己)?(?:的)?(?:这个|这张)?|本人(?:的|名下(?:的)?)?|自己(?:的)?)(?:银行)?(?:账户|账号|银行卡|卡)"
)
RELATIVE_ACCOUNT = (
    r"(?:里面|里边|这里|这儿|(?:这个|这张|当前|本)(?:银行)?(?:账户|账号|银行卡|卡)(?:里|上)?|"
    r"(?:账户|账号|银行卡|卡)(?:里面|里边|里|内|上)?|里)"
)
MOVEMENT = r"(?:转(?:账|钱)?|打(?:款|钱)?|汇(?:款|钱)?|存(?:款|钱)?|充(?:值|钱)?|补(?:充|上)?|放|划(?:拨|转)?)"
INBOUND_ACTION = rf"(?:{MOVEMENT}(?:入|进(?:来|去)?|到)?|入金)"
MONEY = r"(?:[-−－]?\d+(?:\.\d+)?\s*[万千]?|[零〇一二两三四五六七八九十百千万]+)\s*[元块]?"
MIXED_OR_UNSAFE = re.compile(
    r"[，,；;\n]|然后|并且|同时|再给|再转|顺便|忽略|跳过|验证码|密码|sk-|不要|不想|取消|撤回", re.I
)
FUTURE_TIME = re.compile(
    r"明天|后天|下周|下个月|下月|预约|定时|等.+到账|\d+月\d+[日号]|\d{4}-\d{2}-\d{2}|"
    r"(?:周|星期)[一二三四五六日天]|[一二两三四五六七八九十\d]+(?:分钟|小时|天)后"
)


def _toward_account(text, target):
    return bool(
        re.search(rf"(?:向|往|给){target}(?:里|内|上)?{INBOUND_ACTION}", text)
        or re.search(rf"{MOVEMENT}(?:入|进(?:来|去)?|到|至|给){target}", text)
        or re.search(rf"{MOVEMENT}{MONEY}(?:入|进(?:来|去)?|到|至|给){target}", text)
    )


def incoming_intent(text, *, own_account_context=False):
    from .memory import SENSITIVE

    if SENSITIVE.search(text):
        return None
    # Remove only valid thousands separators, not separators between tasks.
    text = re.sub(r"(?<=\d)[,，](?=\d{3}(?:\D|$))", "", text).strip().rstrip("。！？!? ")
    compact = re.sub(r"\s+", "", text)
    if MIXED_OR_UNSAFE.search(text) or re.search(r"(?:不|别|无需).{0,12}(?:转|存|充|打|汇|补)", text):
        return None
    if re.search(
        r"查询|查一下|查查|查账|查转|查入|查充|查到账|记录|历史|回执|凭证|进度|是否到账|有没有到账|到账了[吗没]|到账没",
        text,
    ):
        return None
    if re.search(r"转出|取出|提款|提现|取款|转走|划出|修改余额|改余额|余额(?:改成|改为|设为)|直接记账", text):
        return None
    if re.search(r"话费|手机费|游戏|公交|交通卡|饭卡|校园卡|会员", text):
        return None
    if re.search(r"现金.*(?:存|入账)|(?:存|入账).*现金|(?:ATM|柜台|存款机|存取款机).*(?:存款|存钱|存入)", text, re.I):
        return {"guidance": "cash_deposit"}
    if re.search(
        r"(?:给|向|往).*(?:微信|支付宝|钱包|零钱).*(?:充|存)|(?:充|转|汇|打|存|放).*(?:微信|支付宝|钱包|零钱)", text
    ):
        return None
    if re.search(
        r"(?:向|往|给)(?:我(?:的)?)?(?:尾号\d{4}(?:的)?(?:银行卡|卡|账户)|(?:银行卡|卡|账户)[（(]?尾号\d{4}[)）]?)",
        compact,
    ):
        return {"guidance": "specified_account"}
    # A named different destination is an account-to-account transfer, not new cash.
    if re.search(
        rf"{MOVEMENT}.*(?:到|给|入)(?:我(?:自己)?(?:的)?|本人(?:的)?|自己(?:的)?)(?:另(?:一|外)(?:个|张)?|其他|别的)(?:银行)?(?:账户|银行卡|卡)",
        compact,
    ):
        return {"guidance": "own_account_transfer"}
    explicit = _toward_account(compact, OWN_ACCOUNT)
    relative = _toward_account(compact, RELATIVE_ACCOUNT)
    # Customer language can describe the purpose rather than a bank transfer verb.
    balance_topup = bool(
        re.search(
            r"(?:补(?:足|充|上|够)?|充(?:值|钱)|存(?:款|钱)?)(?:一下|点|些)?(?:账户|银行卡|卡)?(?:余额|资金|钱)",
            compact,
        )
        or re.fullmatch(
            rf"(?:那|现在)?(?:我)?(?:先|想|要|准备)?(?:充(?:值|钱)?|补(?:充|上)?|存(?:入|钱)?)({MONEY})?(?:进去|进来|到账|上)?[吧啊呀]*",
            compact,
        )
        or re.fullmatch(rf"(?:那|现在)?(?:我)?(?:先|想|要|准备)?(?:转|打|汇)(?:进来|进去|入)({MONEY})?[吧啊呀]*", compact)
        or re.fullmatch(r"(?:那)?(?:我)?(?:先)?(?:把)?(?:这笔)?(?:钱|资金)(?:补上|补足|补够)[吧啊呀]*", compact)
        or re.search(r"补(?:足|够|齐)(?:这笔转账)?(?:的)?差额", compact)
    )
    if not explicit and not relative and not balance_topup:
        return None
    if re.search(r"怎么|如何|操作方法|操作步骤", text):
        return {"guidance": "incoming_howto"}
    # A mention of one's source account must not swallow a payment to somebody else.
    if re.search(rf"(?:给|向|往)(?!{OWN_ACCOUNT}|{RELATIVE_ACCOUNT})[^，,；;]+?{MOVEMENT}", compact) or re.search(
        rf"转(?:账)?给(?!{OWN_ACCOUNT}|{RELATIVE_ACCOUNT})", compact
    ):
        return None
    if re.search(r"现金|ATM|柜台|存款机|存取款机", text, re.I):
        return {"guidance": "cash_deposit"}
    if re.search(r"美元|港币|港元|欧元|英镑|日元|美金|\b(?:USD|HKD|EUR|GBP|JPY)\b", text, re.I):
        return {"guidance": "foreign_currency"}
    if FUTURE_TIME.search(text):
        return {"scheduled": True}
    if re.search(r"[-−－]\s*\d", text):
        return {"invalid_amount": True}
    destination_missing = not explicit and not own_account_context
    amounts = list(re.finditer(r"(?:\d+(?:\.\d+)?\s*[万千]?|[零〇一二两三四五六七八九十百千万]+)\s*[元块]", text))
    from .dialogue import money_slot

    try:
        amount = money_slot(text) if len(amounts) <= 1 else None
        if amount is None and not amounts:
            bare = re.search(rf"(?:{INBOUND_ACTION}|补(?:充|上)?)(?:一下)?\s*({MONEY})", text)
            amount = money_slot(bare[1]) if bare else None
    except ClarificationNeeded:
        return {"invalid_amount": True}
    if amount is not None and not 0 < amount <= 100_000_000:
        return {"invalid_amount": True}
    result = {"amount_fen": amount}
    if destination_missing:
        result["clarify_destination"] = True
    if re.search(r"补(?:足|够)(?:这笔转账)?(?:的)?差额|补齐(?:这笔转账)?(?:的)?差额", text):
        result["cover_shortfall"] = amount is None
    return result
