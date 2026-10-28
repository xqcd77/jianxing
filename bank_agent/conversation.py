"""Read-only conversation answers, separate from bank tasks and their drafts.

Common onboarding questions work without a model. The planner's typed answer
channel handles other phrasings and explanations; neither path grants authority
or supplies unverified account facts.
"""

import re

from .schemas import ConversationAnswer, Intent


IDENTITY = (
    "我是简行，你的 AI 银行业务助手。你可以用一句话让我查账户、分析账单，"
    "或安排转账、管理理财、卡片和订阅。涉及资金变动时，我会先列出详情，请你核对确认。"
    "\n当前是演示环境，操作只作用于演示账户。"
)
GREETING = "你好，我是简行。查账、转账、管理卡片，或先了解能办什么，都可以直接和我说。"
THANKS = "不客气，有需要随时告诉我。"


def model_identity(settings):
    if settings is None:
        return "我是简行银行助手。当前使用的模型名称以服务端实际配置为准。"
    if settings.model_mode != "llm":
        return "当前运行的是本地规则演示模式，这轮对话没有调用远端大模型。切换到模型模式后，我会按实际配置说明使用的模型。"
    from .runtime_context import task_state
    runtime = task_state.get()
    model_name = runtime.model_name if runtime.model_explicit and runtime.model_name else settings.model_name
    return (
        f"简行当前配置的业务模型是 {model_name}，负责理解需求和规划任务。"
        "这是服务端实际配置的模型名称；账户数据、权限判断和交易执行由独立的银行业务程序处理。"
    )
EXPLANATIONS = {
    "usage": "直接说出你的需求就可以，例如“查本月账单”或“明天给妈妈转500元”。我会补问必要信息，列出办理详情；需要确认或验证时，页面会给出对应入口。你也可以从下方六类常用场景选择业务。",
    "balance": "账户余额是已经入账的资金总额；可用余额是扣除预留预算等占用后，目前能使用的部分。预计收入和尚未到账的赎回款，不会提前计入可用余额。需要的话，你可以让我查一下当前余额。",
    "confirmation": "查询和分析可以直接进行；涉及转账、取消订阅等变更，会先请你核对详情。大额或敏感操作还需要验证。聊天中的“确认”不会代替页面上的操作确认，验证码请在验证表单中填写。",
    "subscription": "取消会员要区分两件事：撤销银行代扣，以及终止商户的自动续费合同。我会先核对你的订阅，再展示可取消的范围；取消不代表退款。你可以告诉我要处理哪项会员。",
    "redemption": "赎回是把持有的理财份额申请卖出。能否赎回、何时到账，要看产品锁定期、受理时间和结算条件；申请成功后，资金仍需完成结算，才能用于转账。",
    "research": "可以。深入分析会先核查流水、已有收支安排或产品条款，再补查缺失证据，解释资金缺口、异常原因或产品不适合的原因。分析本身不会转账或购买产品；你选定建议后，才进入核对和办理。",
    "demo": "当前是银行业务演示环境，转账、申购等操作只影响演示账户，不会从真实银行卡扣款。你可以完整体验查询、核对、确认、验证和查看凭证的流程。",
    "limits": "我目前提供六类银行业务及账单、理财深入分析，具体可从常用场景查看。当前未连接真实银行和外部商户，不能真实扣款、放贷或保证投资收益；演示业务仍需要按页面要求确认和验证。",
}


def capabilities(domain=None):
    # Reuse the actual workbench catalogue rather than a second tool directory.
    from .businesses import BUSINESSES

    categories = [c for c in BUSINESSES if domain is None or c["id"] == domain]
    lines = [f"{c['label']}：{c['description']}" for c in categories]
    intro = "我可以帮你处理这些业务：" if domain is None else "可以，我能帮你处理："
    return intro + "\n" + "\n".join(lines) + (
        "\n也可以深入分析账单或比较理财条款。你可以直接说需求，例如“查本月账单”或“取消视频会员自动续费”。"
        "涉及变更时会先核对确认；当前操作只作用于演示账户。"
    )


def _topic(text):
    text = re.sub(r"\s+", "", text).lower()
    text = re.sub(r"^(?:(?:你好|您好|请问|我想问一下|我想问下|请|麻烦你|简行)[，,：:]?)+", "", text)
    if not text or re.fullmatch(r"(?:你好|您好|嗨|哈喽|早上好|下午好|晚上好|在吗|有人吗|hello|hi|hey)", text):
        return "greeting", None
    if re.fullmatch(r"(?:好的|好|嗯|ok)?(?:谢谢|谢谢你|谢谢您|多谢|辛苦了|感谢你|感谢|thankyou|thanks)", text):
        return "thanks", None
    if re.fullmatch(
        r"(?:(?:你|简行)(?:现在|当前|目前)?(?:的|用的|使用的|接入的|背后的|基于的|是什么)?(?:大语言|大)?模型"
        r"(?:是什么|叫什么|是哪个|是什么版本)?|你(?:用|使用|接入|基于)的?(?:是)?(?:什么|哪个|哪种)(?:大语言|大)?模型|"
        r"你是(?:什么|哪个|哪种)(?:大语言|大)?模型|模型(?:名称|版本)(?:是什么)?)(?:呢|吗)?", text,
    ):
        return "model", None
    if re.fullmatch(
        r"(?:(?:你|您)(?:到底|究竟)?(?:是谁|叫什么(?:名字)?|是做什么的|是干什么的|是干啥的|是什么助手|是什么)|"
        r"(?:介绍|介绍一下|介绍下)(?:你自己|自己|一下你|你|简行)|自我介绍(?:一下)?|做个自我介绍|"
        r"你是(?:ai还是人工|机器人还是人工|真人吗|人工吗|ai吗|机器人吗))(?:呢|呀|啊|吗)?", text
    ):
        return "identity", None
    if any(re.fullmatch(p, text) for p in [
        r"(?:你们?|您|简行|这里)?(?:现在|目前|都|还)?(?:能|可以|能够)(?:帮我)?(?:做|干|办|办理|处理|提供)(?:一些|些|点)?(?:什么|啥|哪些)(?:业务|事情|事|功能|服务|操作)?(?:呢|吗)?",
        r"(?:你们?|您|简行|这里)?(?:现在|目前|都|还)?(?:有|支持|提供)(?:什么|哪些|啥)(?:业务|功能|服务|能力|操作)(?:(?:可以|能)?(?:办理|做))?(?:呢|吗)?",
        r"(?:你|您)?(?:还能|能|可以)(?:干嘛|干啥|做啥)(?:呢|吗)?",
        r"(?:介绍一下|介绍下|介绍|说说|看看|了解一下)(?:你的|你们的|简行的|这里的)?(?:业务|功能|能力|服务|业务范围)",
        r"(?:业务范围|功能介绍|业务介绍|你能帮我什么|我能在这里做什么|除了转账还能做什么)",
    ]):
        return "capabilities", None
    services = {
        "转账": "funds", "预约转账": "funds", "aa收款": "funds", "查余额": "funds",
        "查账单": "bills", "分析账单": "bills", "账单分析": "bills",
        "理财": "wealth", "比较理财": "wealth", "管理卡片": "cards", "挂失银行卡": "cards",
        "取消视频会员": "subscriptions", "取消订阅": "subscriptions", "管理订阅": "subscriptions",
        "资金分配": "life", "跨场景联动": "life",
    }
    service = re.fullmatch(r"(?:你们?|这里)?(?:能|可以|支持)(?:帮我)?(.+?)(?:吗|么|不)", text)
    if service and service[1] in services:
        return "capabilities", services[service[1]]
    examples = {
        "usage": r"(?:怎么用|怎么使用|如何使用|怎么开始|从哪里开始|我不会用|不知道怎么用|教我用一下|帮我介绍一下用法)",
        "balance": r"(?:(?:账户)?余额和可用余额(?:有)?什么区别|可用余额和(?:账户)?余额(?:有)?什么区别|什么是可用余额|可用余额是什么意思)",
        "confirmation": r"(?:为什么(?:转账|办理业务|操作)?(?:还)?(?:需要|要)确认|为什么(?:转账|办理业务)?(?:还)?(?:需要|要)(?:验证|验证码)|验证码在哪里(?:填|输入)|怎么确认(?:转账|业务)?)",
        "subscription": r"(?:取消会员和撤销代扣(?:有)?什么区别|取消订阅会退款吗|取消会员会退款吗|什么是银行代扣)",
        "redemption": r"(?:什么是赎回|赎回是什么意思|赎回申请成功(?:就|是|等于)到账了吗|为什么赎回(?:后)?还没到账)",
        "research": r"(?:(?:你|这里)?(?:能|可以|支持)(?:帮我)?(?:做)?(?:账单|理财)?(?:深入分析|深度分析|深度研究|deepresearch)(?:账单|理财)?吗|(?:深入分析|深度规划|深度研究)(?:模式)?(?:是什么意思|有什么用|是做什么的))",
        "demo": r"(?:(?:这里|现在|当前|这)?(?:是(?:真实|真的)银行吗|是演示(?:环境)?吗|操作会(?:真的|真实)?扣钱吗|会(?:真的|真实)?转账吗)|这里的钱是真的吗|会从我的银行卡扣钱吗)",
        "limits": r"(?:你(?:有)?什么不能做|你不能做什么|有哪些业务不支持|你有什么限制|你会保证收益吗)",
    }
    for topic, pattern in examples.items():
        if re.fullmatch(pattern, text):
            return topic, None
    return None


def conversation_intent(message, settings=None):
    """Match complete informational utterances, never a substring of an order."""
    from .financial_explanations import explanation
    financial = explanation(message)
    if financial:
        return Intent(summary="金融业务解释与条件计算", answer=ConversationAnswer(kind="explanation", text=financial))
    parts = [part.strip() for part in re.split(r"[，,。！？!?；;\n]+", message.strip()) if part.strip()]
    if not parts:
        return None
    topics = [_topic(part) for part in parts]
    if any(topic is None for topic in topics):
        return None  # Mixed greeting/action and unknown wording go to the planner.
    useful = [topic for topic in topics if topic[0] != "greeting"] or topics[:1]
    replies = []
    for topic, domain in dict.fromkeys(useful):
        replies.append(
            IDENTITY if topic == "identity" else model_identity(settings) if topic == "model"
            else capabilities(domain) if topic == "capabilities"
            else GREETING if topic == "greeting" else THANKS if topic == "thanks" else EXPLANATIONS[topic]
        )
    kind = useful[-1][0]
    return Intent(summary="咨询与交流", answer=ConversationAnswer(
        kind=kind if kind in {"identity", "model", "capabilities", "greeting", "thanks"} else "explanation",
        text="\n".join(replies)[:1200],
    ))


def answer_reply(answer, settings=None, user_message=None):
    """Free-form explanations may not impersonate a bank query or receipt."""
    from .memory import SENSITIVE
    if user_message:
        from .financial_explanations import explanation
        verified = explanation(user_message)
        if verified:
            return verified  # Recomputed from the request, never trust a model-set flag.

    if answer.kind == "model":
        return model_identity(settings)  # Never trust the model's self-reported identity.
    text = (answer.text.strip().replace("银行网关结果", "实际办理结果")
            .replace("银行网关", "办理页面").replace("沙箱环境", "演示环境").replace("沙箱", "演示环境"))
    if not text or SENSITIVE.search(text) or re.search(
        r"\b[a-z]+\.[a-z_]+\b|\b\w+_\w+\b|\d+\s*分(?:钱)?|"
        r"(?:已(?:经)?(?:成功)?(?:帮你|为你|替你)?(?:完成转账|转账|转出|转入|到账|扣款|申购|赎回|挂失|取消订阅|解除锁定)|办理成功)|"
        r"(?:余额|可用资金|持仓|剩余额度|收益率|净值).{0,18}\d|"
        r"(?:无需|不用|不需要)(?:任何)?(?:确认|验证|授权)|(?:保证|承诺).{0,8}(?:收益|盈利)|保本保收益",
        text, re.I,
    ):
        return "我可以解释业务规则和办理流程。具体账户数据及办理结果需要实际查询核实；你想了解哪一部分？"
    return text
