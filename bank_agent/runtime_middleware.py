"""Measure a full ASGI response, including streamed bodies and disconnects."""

import asyncio
from time import perf_counter

from .runtime_context import TaskContext, task_state
from .storage import uid


class RuntimeMiddleware:
    def __init__(self, app, telemetry):
        self.app, self.telemetry = app, telemetry

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        request_id, started = uid("request"), perf_counter()
        token = task_state.set(TaskContext(task_id=uid("task"), request_id=request_id))
        state = scope.setdefault("state", {})
        state["runtime_track"] = scope["path"].startswith("/api/") and not scope["path"].startswith("/api/runtime/")
        status, outcome = 500, "failed"

        async def observed_send(message):
            nonlocal status, outcome
            if message["type"] == "http.response.start":
                status = message["status"]
                message = {**message, "headers": [*message.get("headers", []), (b"x-request-id", request_id.encode())]}
            if message["type"] == "http.response.body" and not message.get("more_body", False):
                outcome = "succeeded" if status < 400 else "failed"
            await send(message)

        try:
            await self.app(scope, receive, observed_send)
        except asyncio.CancelledError:
            outcome = "cancelled"
            raise
        except Exception:
            outcome = "failed"
            raise
        finally:
            try:
                owner = state.get("runtime_owner")
                if owner and state["runtime_track"]:
                    route = getattr(scope.get("route"), "path", "")
                    completion = asyncio.create_task(asyncio.to_thread(
                        self.telemetry.emit, "request.finished", owner,
                        details={"method": scope["method"], "route": route, "status_code": status,
                                 "outcome": outcome, "duration_ms": (perf_counter() - started) * 1000},
                    ))
                    try:
                        await asyncio.shield(completion)
                    except asyncio.CancelledError:
                        await completion
                        raise
            finally:
                task_state.reset(token)
