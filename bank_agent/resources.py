"""Resolve user-named contracts only from an explicitly approved query receipt."""
import re
import hashlib
from .schemas import Intent
from .storage import canonical


def contract_version(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def card_unlock_grounding(message, facts):
    stated_cards=[c for c in facts['cards'] if c['name'] in message
                  or ('默认' in message and c['id']=='C_MAIN')
                  or c.get('last4') and c['last4'] in message]
    if (re.search(r'挂失|解挂',message) and re.search(r'限额|交易限制|游戏消费',message)
            and re.search(r'能恢复付款|恢复付款吗|说明风险|不要改变.*状态',message)
            and len(stated_cards)==1 and stated_cards[0].get('status')=='LOST'):
        return Intent(summary='挂失卡与交易限制的只读说明',answer={'kind':'explanation','text':
            '当前卡片处于挂失状态。解除自己设置的单笔、日限额或游戏消费限制不等于解挂，不能据此恢复付款；'
            '本轮不改变卡片或交易限制，也不发起验证。卡尚未找回或怀疑被盗用时，应维持挂失。'
            '找回卡并确认安全后，可由本人另行申请，按银行要求通过强验证解挂；无法找回请通过银行官方渠道补卡。'
            '挂失不等于账户上的所有银行代扣都已停止，仍需逐项查询。以上为当前演示规则，实际挂失范围及办理方式以银行为准。'})
    if not re.match(r"\s*(?:请|帮我|我要)?(?:解除|解锁)", message):
        return None
    if not ("卡" in message and re.search(r"交易限制|解锁", message)):
        return None
    if re.search(r"转账|申购|然后|并且|同时", message):
        return None
    cards = [c for c in facts["cards"] if c["name"] in message
             or ("默认" in message and c["id"] == "C_MAIN") or c.get("last4", "unmatched") in message]
    if len(cards) != 1:
        return None
    return Intent(summary="核对本人卡片状态后解除交易限制", tasks=[{"domain": "cards", "action": "cards.unlock",
        "params": {"card_id": cards[0]["id"]}}])


def subscription_grounding(message):
    if not re.search(r"会员|订阅|代扣|自动扣费|自动续费", message):
        return None
    from .intent_constraints import NEGATOR

    clauses = [c.strip() for c in re.split(r"[，,。！？!?；;\n]+|(?=" + NEGATOR + r"(?:取消|退订|关闭|撤销|改|提醒|停止))", message) if c.strip()]
    # Discard an entire negative clause; its trailing noun must never become a
    # positive action or a resource name (e.g. name_hint='。').
    positive = [c for c in clauses if not re.search(NEGATOR + r"(?:取消|退订|关闭|撤销|改|提醒|停止)", c)]
    text = "，".join(positive)
    if re.search(r"转账|申购|赎回|挂失", text):
        return None  # A mixed-domain plan still needs the general planner.
    bank_phrase = r"银行(?:卡)?(?:自动)?(?:代扣|扣款)|自动扣款|这张卡的代扣"
    bank_stop = bool(re.search(r"(?:停|停止|停掉|撤销|关闭).{0,10}(?:"+bank_phrase+r")", text))
    if not bank_stop and re.search(r"(?:怎么|如何|能否|是否).*(?:取消|退订)|(?:取消|退订).*(?:吗|么|区别)", text):
        return None
    reminder = "提醒" in text and bool(re.search(r"设置|提前|改成|改为|调整", text))
    cancel = bank_stop or bool(re.search(r"撤销|取消|关闭|停止|停掉|退订", text))
    detect = bool(re.search(r"识别|候选|可能是订阅|哪些扣款|哪些.*(?:订阅|扣费)", text))
    if detect and not reminder and not cancel:
        return Intent(summary="只识别周期扣费候选，不取消", tasks=[{"domain": "subscriptions", "action": "subscriptions.detect"}])
    if not reminder and not cancel:
        if re.search(r"查|看|列出", text):
            return Intent(summary="查询本人订阅合同，不办理取消", tasks=[{"domain": "subscriptions", "action": "subscriptions.query"}])
        return None
    names = []
    for clause in positive:
        if re.search(r"(?:告诉我|想知道|咨询).*(?:怎么|如何|去哪|哪里)", clause):
            continue
        if re.search(r"合同.{0,4}(?:别动|保留|不变)", clause):
            continue
        match = re.search(r"(.+?(?:会员|订阅))", clause)
        if not match:
            continue
        name = re.sub(r"^(?:(?:请|帮我|为我|我要|我想|把|将|我的|设置|撤销|取消|关闭|停止|调整|退订|只|先|停掉))+", "", match[1]).strip()
        if name and name not in {"会员", "订阅", "所有会员", "所有订阅", "全部订阅"}:
            names.append(name)
    if len(set(names)) != 1:
        return None  # Missing/multiple resources require clarification, never an arbitrary ID.
    name = names[0]
    params = {"subscription_id": name}
    if reminder:
        days = re.search(r"(?:提前|改为|改成)\s*(\d+|[零一二两三四五六七八九十]{1,3})\s*天", text)
        if not days:
            return None
        text = days[1]
        digits = {s: i for i, s in enumerate("零一二三四五六七八九")}
        text = text.replace("两", "二")
        if text.isdigit():
            value = int(text)
        elif "十" in text:
            high, low = text.split("十")
            value = digits.get(high, 1) * 10 + digits.get(low, 0)
        else:
            value = digits.get(text, -1)
        if not 0 <= value <= 30:
            return None
        params["days_before"] = value
        operation, summary = "subscriptions.remind", "设置续费提醒"
    else:
        bank_only = bool(re.search(r"只[^，,；;。]*(?:"+bank_phrase+r")|保留商户合同|商户合同保留|不要取消商户合同|商户(?:会员)?合同[^，,。；;\n]{0,4}(?:别动|不变|保留)", message))
        merchant_only = bool(re.search(r"只[^，,；;。]*商户(?:合同|自动续费)|保留银行代扣|银行代扣保留|不要撤销银行代扣", message))
        merchant_mentioned = bool(re.search(r"商户(?:会员)?(?:合同|自动续费|续费)", text))
        merchant_cancel = bool(re.search(r"商户(?:会员)?(?:合同|自动续费).{0,6}(?:取消|终止|停|关闭)|(?:取消|终止|关闭).{0,10}商户",text))
        if bank_stop and not merchant_cancel:
            bank_only = True
        if re.search(bank_phrase, text) and not merchant_mentioned:
            bank_only = True
        if merchant_mentioned and not re.search(bank_phrase, text):
            merchant_only = True
        if bank_only and merchant_only:
            return Intent(summary="核对取消范围", clarification="请明确要撤销银行代扣、取消商户合同，还是同时办理。")
        params["scope"] = "bank" if bank_only else "merchant" if merchant_only else "both"
        operation, summary = "subscriptions.cancel", "按指定范围办理订阅取消"
    return Intent(summary=summary, tasks=[{"domain": "subscriptions", "action": operation, "params": params}])
