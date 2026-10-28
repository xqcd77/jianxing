"""Deterministic admission/read rules. No inference of durable user preferences."""
import math
import json
import re

from .errors import BankError


SENSITIVE = re.compile(
    r"sk-[\w-]+|(?:验证码|密码|api[_ -]?key|access[_ -]?token|secret)\s*[:：=]\s*\S+|"
    r"(?:验证码|密码)\s*(?:是|为)?\s*[A-Za-z0-9][A-Za-z0-9!@#$%^&*._-]*", re.I)
PRIVATE_KEY = re.compile(r'otp|password|api[_-]?key|access[_-]?token|refresh[_-]?token|secret|验证码|密码', re.I)
ACKNOWLEDGEMENT = re.compile(r'\s*(?:好的?|嗯|行|收到|谢谢|继续|继续吧|可以|确认|取消|停|停止|好了吗|进度如何)[！!。？?\s]*')
CURRENT_BALANCE = re.compile(r'\s*(?:(?:请|帮我)?(?:查|查询|查看|看看)(?:一下)?)?(?:账户)?(?:可用)?余额(?:是多少|有多少|多少)?[?？。！!\s]*')
DEFAULT_TTL = {'user_message': 30, 'user_preference': 365, 'product_snapshot': 7,
               'bank_receipt': 90, 'research_report': 1}


def contains_credentials(text, payload=None):
    def private(value):
        if isinstance(value, dict):
            return any(PRIVATE_KEY.fullmatch(str(k)) or private(v) for k, v in value.items())
        if isinstance(value, list):
            return any(private(v) for v in value)
        return isinstance(value, str) and bool(SENSITIVE.search(value))
    if SENSITIVE.search(text) or private(payload):
        return True
    try:
        return private(json.loads(text))
    except (ValueError, TypeError):
        return False


def ttl(source_type, explicit):
    value = DEFAULT_TTL[source_type] if explicit is None else explicit
    if type(value) not in {int, float} or not math.isfinite(value) or not 0 < value <= 3650:
        raise BankError('MEMORY_TTL_INVALID', '记忆有效期应在0至3650天之间', 422)
    return value


def capture_decision(message, response, intent):
    if contains_credentials(message) or '敏感凭证已拦截' in message:
        return 'skip_credentials'
    if ACKNOWLEDGEMENT.fullmatch(message):
        return 'skip_task_control'
    if response.get('status') == 'ANSWERED':
        return 'skip_conversation_only'
    if not intent or not (intent.get('tasks') or intent.get('additional_goals') or intent.get('clarification')):
        return 'skip_no_business_goal'
    return 'capture_task_context'


def read_mode(query, protected):
    # Start narrowly: current balance has its own authoritative bank operation.
    # Financial writes, references, composition and continuations keep full recall.
    if CURRENT_BALANCE.fullmatch(query) and not (protected and protected.plan_id):
        return 'current_facts_and_preferences'
    return 'semantic_recall'
