"""Server-owned providers. A request selects an ID, never an endpoint or key."""
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from contextlib import nullcontext
import hashlib
import json
import os

from pydantic import Field, SecretStr

from .errors import BankError
from .runtime_context import task_state
from .schemas import StrictModel

PRESET_MODELS = {
    'deepseek': ['deepseek-flash', 'deepseek-v4-pro'],
    'glm': ['glm-5.3'],
    'mimo': ['mimo-v2.6-pro', 'mimo-v2.6-flash'],
    'kimi': ['kimi-k3.1-pro', 'kimi-k3.1-pro-highspeed'],
    'qwen': ['qwen3.8-max'],
    'minimax': ['MiniMax-M3.1-Flash', 'MiniMax-M3'],
}


class ProviderConfiguration(StrictModel):
    model: str = Field(min_length=1, max_length=150, pattern=r'^[A-Za-z0-9][A-Za-z0-9_.:/@-]*$')
    api_key: SecretStr | None = None


class ProviderDiscovery(StrictModel):
    api_key: SecretStr | None = None


PROVIDERS = {
    'deepseek': ('DeepSeek', '深度求索', 'deepseek-flash', 'https://api.deepseek.com', 'DEEPSEEK_API_KEY'),
    'glm': ('GLM', '智谱', 'glm-5.3', 'https://open.bigmodel.cn/api/paas/v4', 'GLM_API_KEY'),
    'mimo': ('MiMo', '小米', 'mimo-v2.6-pro', 'https://api.xiaomimimo.com/v1', 'MIMO_API_KEY'),
    'kimi': ('Kimi', '月之暗面', 'kimi-k3.1-pro', 'https://api.moonshot.cn/v1', 'MOONSHOT_API_KEY'),
    'qwen': ('Qwen', '通义千问', 'qwen3.8-max', 'https://dashscope.aliyuncs.com/compatible-mode/v1', 'DASHSCOPE_API_KEY'),
    'minimax': ('MiniMax', '稀宇科技', 'MiniMax-M3.1-Flash', 'https://api.minimax.cn/v1', 'MINIMAX_API_KEY'),
}


def provider_environment():
    """Called after dotenv loading; judge credentials are deliberately excluded."""
    return {key: {name: os.getenv('BANK_' + key.upper() + '_' + name.upper(), '')
                  for name in ('api_key', 'base_url', 'model', 'context_tokens')}
            | {'api_key': os.getenv('BANK_' + key.upper() + '_API_KEY') or os.getenv(spec[4], '')}
            for key, spec in PROVIDERS.items()}


@dataclass(frozen=True)
class ModelProfile:
    id: str
    name: str
    vendor: str
    model: str
    base_url: str
    api_key: str = field(repr=False)
    context_tokens: int = 131072
    enabled: bool = False
    credential_version: int = 0
    credential_source: str = 'server'

    def public(self):
        return {'id': self.id, 'name': self.name, 'vendor': self.vendor, 'model': self.model,
                'display_model': 'DeepSeek-V4.1-Flash-1017' if self.id == 'deepseek' and self.model == 'deepseek-flash' else self.model,
                'context_tokens': self.context_tokens, 'enabled': self.enabled,
                'icon': '/static/providers/' + self.id + '.svg',
                'status': 'configured' if self.enabled else 'not_configured'}


class ModelRegistry:
    def __init__(self, settings, db=None, cipher=None):
        self.settings = settings
        self.db, self.cipher = db, cipher
        self.profiles = {}
        self.default_id = next((key for key in PROVIDERS if {
            'deepseek': 'deepseek', 'glm': 'bigmodel', 'mimo': 'mimo', 'kimi': 'moonshot',
            'qwen': 'aliyuncs', 'minimax': 'minimax',
        }[key] in settings.model_base_url), 'configured')
        for key, (name, vendor, model, url, _) in PROVIDERS.items():
            values = settings.model_providers.get(key, {})
            is_default = key == self.default_id
            credential = values.get('api_key') or (settings.model_api_key if is_default else '')
            limit = int(values.get('context_tokens') or settings.model_context_tokens)
            if limit <= settings.model_token_safety_margin + 4096:
                raise BankError('MODEL_CONFIG_INVALID', '模型窗口配置不足，请核对服务端配置', 503)
            self.profiles[key] = ModelProfile(
                key, name, vendor, values.get('model') or (settings.model_name if is_default else model),
                (values.get('base_url') or (settings.model_base_url if is_default else url)).rstrip('/'),
                credential, limit, bool(credential) or (is_default and settings.model_mode == 'demo'),
            )
        if self.default_id == 'configured':
            self.profiles['configured'] = ModelProfile('configured', '服务端模型', '自定义', settings.model_name,
                settings.model_base_url, settings.model_api_key, settings.model_context_tokens,
                bool(settings.model_api_key) or settings.model_mode == 'demo')

    @staticmethod
    def _key(owner, provider):
        return 'model_profile_' + hashlib.sha256((owner + ':' + provider).encode()).hexdigest()

    def get(self, model_id=None, *, require_enabled=True, owner_id=None, session=None, version=None):
        profile = self.profiles.get(model_id or self.default_id)
        if profile is None:
            raise BankError('MODEL_UNKNOWN', '请选择列表中的模型', 422)
        owner_id = owner_id if owner_id is not None else task_state.get().owner_id
        if self.db is not None and owner_id and version != 0:
            from .storage import Document
            from cryptography.fernet import InvalidToken
            with (nullcontext(session) if session is not None else self.db.read()) as source:
                pointer = source.get(Document, self._key(owner_id, profile.id))
                data = pointer.data if pointer and pointer.owner_id == owner_id and pointer.kind == 'model_profile' else {}
                selected_version = version if version is not None else data.get('credential_version', 0)
                if selected_version:
                    credential = source.get(Document, self._key(owner_id, profile.id) + '_key_' + str(selected_version))
                    if not credential or credential.owner_id != owner_id or credential.kind != 'model_credential':
                        raise BankError('MODEL_KEY_UNAVAILABLE', '模型密钥无法读取，请重新填写', 409)
                    try:
                        secret = json.loads(self.cipher.decrypt(credential.data['ciphertext'].encode()))
                        if secret['owner'] != owner_id or secret['provider'] != profile.id:
                            raise ValueError('Credential scope mismatch')
                        profile = replace(profile, api_key=secret['value'], base_url=PROVIDERS[profile.id][3],
                            enabled=True, credential_version=selected_version, credential_source='personal')
                    except (InvalidToken, ValueError, KeyError, TypeError):
                        raise BankError('MODEL_KEY_UNAVAILABLE', '模型密钥无法读取，请重新填写', 409) from None
                if data:
                    profile = replace(profile, model=data['model'])
        if require_enabled and not profile.enabled:
            raise BankError('MODEL_NOT_CONFIGURED', '请打开该厂商设置，填写API Key并选择模型', 409)
        return profile

    def catalog(self, owner_id=None):
        return {'default_id': self.default_id, 'mode': self.settings.model_mode,
                'models': [self.get(key, owner_id=owner_id, require_enabled=False).public() for key in self.profiles]}

    def configuration(self, owner_id, model_id):
        profile = self.get(model_id, owner_id=owner_id, require_enabled=False)
        return {**profile.public(), 'has_personal_key': profile.credential_version > 0,
                'has_saved_key': bool(profile.api_key), 'credential_source': profile.credential_source if profile.api_key else 'none',
                'options': list(dict.fromkeys([profile.model, *PRESET_MODELS.get(profile.id, [])]))}

    @staticmethod
    def _secret(value):
        secret = value.get_secret_value().strip() if value is not None else ''
        if secret and (len(secret) > 4096 or len(secret) < 8 or not secret.isascii() or any(c.isspace() for c in secret)):
            raise BankError('MODEL_KEY_INVALID', 'API Key格式不正确，请重新填写', 422)
        return secret

    def configure(self, owner_id, model_id, request):
        if self.db is None or self.cipher is None or model_id not in PROVIDERS:
            raise BankError('MODEL_CONFIG_UNAVAILABLE', '该厂商配置暂不可用', 409)
        secret = self._secret(request.api_key)
        from .storage import Document, save_document, now_iso
        with self.db.write() as session:
            current = self.get(model_id, owner_id=owner_id, session=session, require_enabled=False)
            if not secret and not current.api_key:
                raise BankError('MODEL_KEY_REQUIRED', '请填写该厂商的API Key', 422)
            version = current.credential_version
            if secret:
                version += 1
                ciphertext = self.cipher.encrypt(json.dumps({'owner': owner_id, 'provider': model_id, 'value': secret}).encode()).decode()
                save_document(session, self._key(owner_id, model_id) + '_key_' + str(version), 'model_credential', owner_id,
                              {'ciphertext': ciphertext, 'provider': model_id, 'version': version})
            pointer = session.get(Document, self._key(owner_id, model_id))
            revision = pointer.data.get('revision', 0) + 1 if pointer else 1
            save_document(session, self._key(owner_id, model_id), 'model_profile', owner_id,
                {'provider': model_id, 'model': request.model, 'credential_version': version,
                 'revision': revision, 'updated_at': now_iso()})
        return self.configuration(owner_id, model_id)

    async def discover(self, owner_id, model_id, request):
        import httpx
        if model_id not in PROVIDERS:
            raise BankError('MODEL_UNKNOWN', '请选择列表中的厂商', 422)
        secret = self._secret(request.api_key)
        profile = self.get(model_id, owner_id=owner_id, require_enabled=False)
        key, url = (secret, PROVIDERS[model_id][3]) if secret else (profile.api_key, profile.base_url)
        if not key:
            raise BankError('MODEL_KEY_REQUIRED', '请先填写API Key，再获取可用模型', 422)
        headers = {'Authorization': 'Bearer ' + key}
        if model_id == 'mimo':
            headers['api-key'] = key
        try:
            async with httpx.AsyncClient(timeout=12, follow_redirects=False,
                transport=getattr(self, 'discovery_transport', None)) as client:
                response = await client.get(url + '/models', headers=headers)
            if response.status_code != 200:
                raise ValueError('Provider unavailable')
            items = response.json().get('data', [])
            import re
            models = sorted({m['id'] for m in items if isinstance(m, dict) and isinstance(m.get('id'), str)
                and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.:/@-]{0,149}', m['id'])})
            if not models:
                raise ValueError('Empty model list')
            return {'models': models[:200], 'source': 'provider'}
        except (httpx.HTTPError, ValueError, TypeError, AttributeError):
            raise BankError('MODEL_LIST_UNAVAILABLE', '暂时无法读取厂商模型列表，可先选择预设或填写模型ID', 502) from None

    def effective(self, settings):
        runtime = task_state.get()
        if not runtime.model_id:
            return settings  # Existing non-interactive/evaluation paths retain exact configuration.
        profile = self.get(runtime.model_id, require_enabled=False, version=runtime.model_credential_version)
        # The server's default adapter can be overridden locally by an existing
        # integration/test. An unconfigured default must not erase that key, or
        # prevent deterministic rules from working without a remote provider.
        credential = profile.api_key or (settings.model_api_key if profile.id == self.default_id else '')
        return replace(settings, model_name=runtime.model_name or profile.model,
                       model_base_url=profile.base_url, model_api_key=credential,
                       model_context_tokens=runtime.model_context_tokens or profile.context_tokens)

    @contextmanager
    def scope(self, model_id, *, model_name='', context_tokens=None, require_enabled=True, explicit=True, owner_id=None):
        previous = task_state.get()
        if owner_id and previous.owner_id and previous.owner_id != owner_id:
            raise BankError('HARNESS_OWNER_MISMATCH', '任务主体不一致', 403)
        profile = self.get(model_id, owner_id=owner_id, require_enabled=require_enabled)
        token = task_state.set(replace(task_state.get(), model_id=profile.id,
            model_name=model_name or profile.model, model_context_tokens=context_tokens or profile.context_tokens,
            model_explicit=explicit, owner_id=owner_id or previous.owner_id,
            model_credential_version=profile.credential_version))
        try:
            yield profile
        finally:
            task_state.reset(token)

    def adapt(self, request, role):
        """Provider protocol differences, after all role-specific prompt construction."""
        profile = self.get(task_state.get().model_id, require_enabled=False,
                           version=task_state.get().model_credential_version)
        if not task_state.get().model_id or profile.id in {'deepseek', 'configured'}:
            return request
        request.pop('reasoning_effort', None)
        request.pop('thinking', None)
        request['max_tokens'] = min(request.get('max_tokens', 4096), self.settings.model_max_tokens)
        # Structured proposals are bounded single turns. No latent transcript is
        # carried between roles or used as an authorization source.
        if profile.id in {'glm', 'mimo', 'kimi'}:
            request['thinking'] = {'type': 'disabled'}
        if profile.id == 'qwen':
            request['enable_thinking'] = False  # Non-streaming structured output.
        if profile.id == 'kimi':
            request['temperature'] = 1.0
        model = request.get('model', '')
        if profile.id == 'glm' and model == 'glm-5.3':
            request['thinking'] = {'type': 'enabled'}
            request['reasoning_effort'] = 'low'
            request['temperature'] = 1.0
        if profile.id == 'kimi' and model in {'kimi-k3.1-pro', 'kimi-k3.1-pro-highspeed'}:
            # K3.1 always reasons; the old K2 thinking-off switch is invalid.
            request.pop('thinking', None)
            request['reasoning_effort'] = 'low'
            request['top_p'] = 0.95
        if profile.id in {'mimo', 'minimax'}:
            request['max_completion_tokens'] = request.pop('max_tokens')
        if profile.id == 'minimax' or request.get('tools'):
            request.pop('response_format', None)
        return request

    def headers(self, settings):
        headers = {'Authorization': 'Bearer ' + settings.model_api_key}
        if task_state.get().model_id == 'mimo':
            headers['api-key'] = settings.model_api_key
        return headers
