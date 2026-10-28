"""Bank submission boundary. An uncertain submission is NEVER automatically retried.

The included adapter is transactional and local. A real network adapter must use
an outbox and independent submission worker; network I/O is not run in a SQLite
write transaction. This protocol and reconciliation path are ready to test now.
"""

from dataclasses import dataclass
from typing import Literal, Protocol

from sqlalchemy import select

from .errors import BankError
from .storage import Receipt


class SubmissionUncertain(Exception):
    """The request may have been accepted; do not assume a failure or resubmit."""


@dataclass(frozen=True)
class LookupResult:
    status: Literal["SUCCEEDED", "PENDING", "UNAVAILABLE", "NOT_EXECUTED_FINAL"]
    receipt: dict | None = None


class BankAdapter(Protocol):
    atomic_local: bool

    def submit(self, session, user_id, operation, params, idempotency_key) -> dict: ...

    def lookup(self, session, user_id, operation, params, idempotency_key) -> LookupResult: ...


class SandboxBankAdapter:
    atomic_local = True

    def __init__(self, bank):
        self.bank = bank

    def healthcheck(self, session):
        from sqlalchemy import func
        from .storage import Account, LedgerEntry

        # A custom/fault-injection adapter must provide its own health evidence.
        return (
            type(self) is SandboxBankAdapter
            and (session.scalar(select(func.sum(LedgerEntry.delta_fen))) or 0) == 0
            and (session.scalar(select(func.sum(Account.balance_fen))) or 0) == 0
        )

    def submit(self, session, user_id, operation, params, idempotency_key):
        return self.bank.execute_once(session, user_id, operation, params, idempotency_key)

    def lookup(self, session, user_id, operation, params, idempotency_key):
        from .research_tools import digest

        receipt = session.scalar(select(Receipt).where(Receipt.idempotency_key == idempotency_key))
        if not receipt:
            # A missing receipt alone cannot prove that an external request will never execute.
            return LookupResult("PENDING")
        if receipt.owner_id != user_id or receipt.operation != operation or receipt.params_hash != digest(params):
            raise BankError("IDEMPOTENCY_CONFLICT", "原提交与回执归属或参数不一致")
        return LookupResult("SUCCEEDED", receipt.result)


class LostResponseSandboxAdapter(SandboxBankAdapter):
    """Fault-injection adapter: commit locally, lose the reply, recover by receipt."""

    def submit(self, session, user_id, operation, params, idempotency_key):
        result = super().submit(session, user_id, operation, params, idempotency_key)
        if operation in {"funds.transfer", "wealth.purchase"}:
            raise SubmissionUncertain("reply_lost")
        return result
