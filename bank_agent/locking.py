"""OS-released cross-process locks for graph threads and the scheduler.

Unlike expiring leases, a slow but alive worker never loses its lock. Windows
byte-range locks and Unix flock are released automatically on process death.
These locks coordinate local workers sharing one data directory.
"""

import asyncio
from contextlib import asynccontextmanager, contextmanager
from contextvars import ContextVar
import hashlib
import os
import time

from .errors import BankError

_held = ContextVar("bank_process_locks", default=frozenset())


class ProcessLocks:
    def __init__(self, data_dir):
        self.directory = data_dir / "locks"
        self.directory.mkdir(parents=True, exist_ok=True)

    def _name(self, key):
        return str(self.directory.resolve()) + ":" + key

    def busy(self, key):
        file = self._try(key)
        if file is None:
            return True
        self._release(file)
        return False

    def _try(self, key):
        file = open(self.directory / (hashlib.sha256(key.encode()).hexdigest() + ".lock"), "a+b")
        file.seek(0)
        if os.name == "nt":
            import msvcrt
            # Windows denies reading a byte locked by another process. Inspect
            # file size instead; contention should wait, not raise PermissionError.
            if os.fstat(file.fileno()).st_size == 0:
                file.write(b"0")
                file.flush()
            file.seek(0)
            try:
                msvcrt.locking(file.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError:
                file.close()
                return None
        else:
            import fcntl
            try:
                fcntl.flock(file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                file.close()
                return None
        return file

    @staticmethod
    def _release(file):
        if os.name == "nt":
            import msvcrt
            file.seek(0)
            msvcrt.locking(file.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(file.fileno(), fcntl.LOCK_UN)
        file.close()

    @contextmanager
    def hold(self, key, timeout=30):
        name = self._name(key)
        if name in _held.get():
            yield
            return
        deadline = time.monotonic() + timeout
        while (file := self._try(key)) is None:
            if time.monotonic() >= deadline:
                raise BankError("PLAN_BUSY", "这份安排正在处理，请稍后刷新核实结果", 409)
            time.sleep(0.04)
        token = _held.set(_held.get() | {name})
        try:
            yield
        finally:
            _held.reset(token)
            self._release(file)

    @asynccontextmanager
    async def ahold(self, key, timeout=30):
        name = self._name(key)
        if name in _held.get():
            yield
            return
        deadline = time.monotonic() + timeout
        while (file := self._try(key)) is None:
            if time.monotonic() >= deadline:
                raise BankError("PLAN_BUSY", "这份安排正在处理，请稍后刷新核实结果", 409)
            await asyncio.sleep(0.04)
        token = _held.set(_held.get() | {name})
        try:
            yield
        finally:
            _held.reset(token)
            self._release(file)


def coordinated(method):
    from functools import wraps
    from .storage import Plan

    @wraps(method)
    def locked(self, user_id, plan_id, *args, **kwargs):
        with self.db.read() as session:
            plan = session.get(Plan, plan_id)
            if not plan or plan.user_id != user_id:
                raise BankError("PLAN_NOT_FOUND", "计划不存在或无权访问", 404)
            key = "graph:" + plan.thread_id
        with self.locks.hold(key):
            return method(self, user_id, plan_id, *args, **kwargs)
    return locked
