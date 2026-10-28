"""Observe validated model results and usage, excluding credentials and private reasoning."""
from contextvars import ContextVar
from threading import Lock
import time
import hashlib
import json
from uuid import uuid4

from bank_agent.errors import BankError


_call = ContextVar("evaluation_model_call", default=None)
SENSITIVE = {"otp", "password", "old_password", "new_password", "access_token", "secret", "api_key",
             "totp_secret", "authorization", "reasoning_content", "rationale", "demo_challenge_id", "challenge_id"}


def scrub(value):
    if isinstance(value, dict):
        return {k: scrub(v) for k, v in value.items() if k.lower() not in SENSITIVE
                and not (k.lower() == "code" and isinstance(v, str) and v.isdigit() and len(v) == 6)}
    if isinstance(value, list):
        return [scrub(v) for v in value]
    if isinstance(value, str):
        import re
        return re.sub(r"sk-[A-Za-z0-9_-]+", "[REDACTED]", value)
    return value


class Trace:
    def __init__(self, max_calls=10):
        self.models, self.events = [], []
        self.lock = Lock()
        self.max_calls = max_calls
        self.calls_started = 0
        self.probes = {}

    def event(self, kind, **data):
        with self.lock:
            self.events.append({"kind": kind, **scrub(data)})

    def observe(self, model):
        original_decode = model._decode
        original_request = model._request

        def request(role, payload, schema):
            result = original_request(role, payload, schema)
            record = _call.get()
            if record is not None:
                text = json.dumps(result.get("messages", []), ensure_ascii=False, sort_keys=True)
                record["input_hash"] = hashlib.sha256(text.encode()).hexdigest()
                record["probe_matches"] = {name: needle in text for name, needle in self.probes.items()}
            return result

        model._request = request

        def decode(response, schema):
            current = _call.get()
            if current is not None:
                try:
                    data = response.json()
                    current["usage"] = scrub(data.get("usage"))
                    current["provider_model"] = data.get("model")
                    current["http_status"] = response.status_code
                except ValueError:
                    current["usage"] = None
            return original_decode(response, schema)

        model._decode = decode
        original_sync = model.structured

        def begin(role):
            with self.lock:
                if self.calls_started >= self.max_calls:
                    raise BankError("EVALUATION_BUDGET", "评测模型调用预算已用尽", 503)
                self.calls_started += 1
            record = {"id": uuid4().hex[:12], "role": role, "usage": None, "status": "started",
                      "transport": "synthetic_http" if getattr(model, "http_transport", None) is not None else "external_http"}
            return record, _call.set(record), time.perf_counter()

        def end(record, token, started):
            record["duration_ms"] = round((time.perf_counter() - started) * 1000, 2)
            with self.lock:
                self.models.append(record)
            _call.reset(token)

        def sync(role, payload, schema):
            record, token, started = begin(role)
            try:
                result = original_sync(role, payload, schema)
                record.update(status="ok", validated_output=scrub(result.model_dump(mode="json")))
                return result
            except Exception as error:
                record.update(status="error", error=getattr(error, "code", type(error).__name__))
                if getattr(error, "diagnostic", None):
                    record["diagnostic"] = scrub(error.diagnostic)
                raise
            finally:
                end(record, token, started)

        async def async_call(role, payload, schema):
            record, token, started = begin(role)
            try:
                # The production fallback detects an instance sync override. Temporarily
                # keeping that override would turn this observation into two counted calls.
                result = await original_async_without_override()(role, payload, schema)
                record.update(status="ok", validated_output=scrub(result.model_dump(mode="json")))
                return result
            except Exception as error:
                record.update(status="error", error=getattr(error, "code", type(error).__name__))
                if getattr(error, "diagnostic", None):
                    record["diagnostic"] = scrub(error.diagnostic)
                raise
            finally:
                end(record, token, started)

        # Bind the class method on a lightweight view without changing the shared instance
        # during asynchronous calls. Its decoder forwards usage into this context.
        import copy
        def original_async_without_override():
            view = copy.copy(model)
            view.__dict__.pop("structured", None)
            return type(model).astructured.__get__(view, type(model))
        model.structured, model.astructured = sync, async_call

    def usage(self):
        fields = ("prompt_tokens", "completion_tokens", "total_tokens", "prompt_cache_hit_tokens", "prompt_cache_miss_tokens")
        return {name: sum(m["usage"][name] for m in self.models if isinstance(m.get("usage"), dict)
                         and isinstance(m["usage"].get(name), int))
                if self.models and all(isinstance(m.get("usage"), dict) and isinstance(m["usage"].get(name), int)
                                       for m in self.models) else (0 if not self.models else None) for name in fields}
