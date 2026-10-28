"""User prohibitions are checked independently of a model's proposed actions."""
import re

NEGATOR = r"(?:不要|别|不许|禁止|不得|不能|暂不|先不|不需要|不用|不)(?:(?:再|替我|帮我|为我|擅自|直接|自动|去|进行))*"
ACTION_PATTERNS = {
    "subscriptions.cancel": r"(?:取消|退订|撤销|停掉|关闭)(?:订阅|会员|自动续费|银行代扣|代扣|合同)?",
    "wealth.purchase": r"(?:申购|购买|买|投资)",
    "wealth.redeem": r"赎回",
    "funds.transfer": r"(?:转账|转款|转钱|汇款|付款)",
    "funds.release": r"(?:解除预留|释放预留)",
    "cards.restore": r"解挂",
}


def forbidden_operations(message):
    """Global prohibitions only. A separate positive instruction keeps scope local.

    '不要取消音乐，取消视频' must not ban all cancellation. Such resource-scoped
    clauses are resolved by the subscription parser rather than this global gate.
    """
    clauses = [c.strip() for c in re.split(r"[，,。！？!?；;\n]+", message) if c.strip()]
    result = []
    for operation, action in ACTION_PATTERNS.items():
        negative = re.compile(NEGATOR + action)
        denied = any(negative.search(c) for c in clauses)
        positive = any(re.search(action, negative.sub("", c)) for c in clauses)
        if denied and not positive:
            result.append(operation)
    return result


def constrain_intent(intent, message):
    """Remove prohibited proposals before DAG construction, never grant authority."""
    from .schemas import Intent

    denied = set(forbidden_operations(message))
    removed = [task for task in intent.tasks if task.action in denied]
    if not removed:
        return intent
    kept = [task for task in intent.tasks if task.action not in denied]
    ids = {task.id for task in removed if task.id}
    if any(set(task.depends_on) & ids or any("$"+key+"." in str(task.params) for key in ids) for task in kept):
        return Intent(summary="需按你的限制重新核对计划", clarification="你的要求包含明确不办理的操作，原计划已停止。请确认剩余业务的办理范围。")
    if not kept:
        return Intent(summary="保留你的不办理要求", answer={"kind": "explanation", "text": "已保留你的限制，这些变更不会办理。你可以继续查询信息，或明确说明需要保留的其他安排。"})
    return intent.model_copy(update={"tasks": kept})
