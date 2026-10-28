"""Validated stage updates across SDK, workflow threads and public reads."""
from contextvars import ContextVar

progress_sink = ContextVar('bank_request_progress_sink', default=None)


def publish_progress(stage, label, partial=None):
    sink = progress_sink.get()
    if sink:
        sink(stage, label, partial)
