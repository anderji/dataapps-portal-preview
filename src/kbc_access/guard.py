"""Layer 3 — the data guard: one object an app asks for secured data.

It ties the policy, the principal's attributes, the compiler, the rewriter,
the backend and the audit log together so an app never builds SQL against a
physical table itself.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass

from kbc_access.audit import AuditLog
from kbc_access.backends.duckdb_backend import DuckDBBackend, QueryResult
from kbc_access.compiler import CompiledDataset, compile_dataset
from kbc_access.identity import Principal
from kbc_access.policy import PolicySet
from kbc_access.rewriter import QueryRejected, query_hash, rewrite
from kbc_access.store import EMPTY_SECURITY, PrincipalSecurity

# Shown instead of the masking salt when compiled SQL is displayed.
SALT_REDACTION = "<salt>"
# Characters that render verbatim inside a SQL string literal in every dialect.
SALT_PATTERN = re.compile(r"^[A-Za-z0-9_-]{16,}$")


@dataclass(frozen=True)
class GuardedResult:
    result: QueryResult | None
    error: str | None
    datasets: tuple[str, ...]
    # Compiled SQL with the masking salt redacted, for the "how was this secured" panel.
    display_sql: str | None


class DataGuard:
    def __init__(
        self,
        policy: PolicySet,
        backend: DuckDBBackend,
        audit: AuditLog,
        *,
        salt: str,
        app_id: str,
        max_query_chars: int,
        security: Callable[[Principal], PrincipalSecurity],
    ) -> None:
        """`security` yields, for a principal, who they are for subject matching and every dataset's lines."""
        if not SALT_PATTERN.fullmatch(salt):
            raise ValueError("Masking salt must be URL-safe characters only (A-Z a-z 0-9 _ -)")
        self._max_query_chars = max_query_chars
        self._policy = policy
        self._backend = backend
        self._audit = audit
        self._salt = salt
        self._app_id = app_id
        self._security = security

    @property
    def policy(self) -> PolicySet:
        return self._policy

    def secured(self, principal: Principal) -> dict[str, CompiledDataset]:
        """Every dataset the principal may see, compiled for them."""
        out: dict[str, CompiledDataset] = {}
        view = self._security(principal)
        for name, dataset in self._policy.datasets.items():
            compiled = compile_dataset(
                dataset, principal, view.audience, view.datasets.get(name, EMPTY_SECURITY),
                salt=self._salt, dialect=self._backend.dialect,
            )
            if compiled is not None:
                out[name] = compiled
        return out

    def _redact(self, sql: str) -> str:
        return sql.replace(self._salt, SALT_REDACTION)

    def run(self, principal: Principal, sql: str) -> GuardedResult:
        """Rewrite, execute and audit one query. Never raises on a user error."""
        secured = self.secured(principal)
        try:
            rewritten = rewrite(sql, secured, dialect=self._backend.dialect, max_chars=self._max_query_chars)
        except QueryRejected as exc:
            self._audit_query(principal, sql, allowed=False, reason="rejected", used=(), secured=secured, rows=None)
            return GuardedResult(None, str(exc), (), None)
        try:
            result = self._backend.execute(rewritten.sql)
        except Exception as exc:  # duckdb.Error subclasses; message is about the user's SQL
            message = str(exc).replace(self._salt, SALT_REDACTION)
            self._audit_query(principal, sql, allowed=False, reason="execution_error", used=rewritten.datasets, secured=secured, rows=None)
            return GuardedResult(None, f"Query failed: {message}", rewritten.datasets, self._redact(rewritten.sql))
        self._audit_query(principal, sql, allowed=True, reason="ok", used=rewritten.datasets, secured=secured, rows=len(result.rows))
        return GuardedResult(self._redact_rows(result), None, rewritten.datasets, self._redact(rewritten.sql))

    def _redact_rows(self, result: QueryResult) -> QueryResult:
        """Second line of defence: no string cell may ever carry the masking salt."""
        rows = [
            tuple(v.replace(self._salt, SALT_REDACTION) if isinstance(v, str) else v for v in row)
            for row in result.rows
        ]
        return QueryResult(columns=result.columns, rows=rows, truncated=result.truncated)

    def _audit_query(
        self,
        principal: Principal,
        sql: str,
        *,
        allowed: bool,
        reason: str,
        used: tuple[str, ...],
        secured: dict[str, CompiledDataset],
        rows: int | None,
    ) -> None:
        compiled = [secured[d] for d in used if d in secured]
        self._audit.query(
            app_id=self._app_id,
            email=principal.email,
            allowed=allowed,
            reason=reason,
            datasets=list(used),
            grants={c.dataset: list(c.grants_fired) for c in compiled},
            masked_columns={c.dataset: list(c.masked_columns) for c in compiled},
            absent_columns={c.dataset: list(c.absent_columns) for c in compiled},
            policy_fingerprint=self._policy.fingerprint,
            query_hash=query_hash(sql),
            row_count=rows,
        )
