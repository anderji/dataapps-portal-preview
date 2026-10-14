"""Layer 4 — audit.

One JSON line per decision. Lines carry identifiers (who, which app, which
grants, which datasets and columns, policy fingerprint, query hash) and
never data: no row values and no attribute values, or the audit log itself
becomes the leak. Callers pass only the fields below; there is no free-form
payload argument on purpose.
"""

from __future__ import annotations

import json
from collections import deque
from datetime import UTC, datetime
from pathlib import Path
from threading import Lock

_lock = Lock()


class AuditLog:
    def __init__(self, path: Path) -> None:
        self._path = path
        path.parent.mkdir(parents=True, exist_ok=True)

    def _write(self, event: dict[str, object]) -> None:
        event = {"ts": datetime.now(UTC).isoformat(timespec="milliseconds"), **event}
        line = json.dumps(event, ensure_ascii=False, sort_keys=True)
        with _lock, self._path.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")

    def gate(
        self,
        *,
        app_id: str,
        email: str | None,
        allowed: bool,
        reason: str,
        matched: tuple[str, ...],
        hostile_headers: tuple[str, ...],
        path: str,
    ) -> None:
        self._write(
            {
                "event": "gate",
                "app_id": app_id,
                "email": email,
                "allowed": allowed,
                "reason": reason,
                "matched_grants": list(matched),
                "hostile_headers": list(hostile_headers),
                "path": path,
            }
        )

    def query(
        self,
        *,
        app_id: str,
        email: str,
        allowed: bool,
        reason: str,
        datasets: list[str],
        grants: dict[str, list[str]],
        masked_columns: dict[str, list[str]],
        absent_columns: dict[str, list[str]],
        policy_fingerprint: str,
        query_hash: str,
        row_count: int | None,
    ) -> None:
        self._write(
            {
                "event": "query",
                "app_id": app_id,
                "email": email,
                "allowed": allowed,
                "reason": reason,
                "datasets": datasets,
                "grants": grants,
                "masked_columns": masked_columns,
                "absent_columns": absent_columns,
                "policy_fingerprint": policy_fingerprint,
                "query_hash": query_hash,
                "row_count": row_count,
            }
        )

    def admin(self, *, email: str, action: str, target: str) -> None:
        """Admin change: `target` is an identifier (app id, email, group id), never a value."""
        self._write({"event": "admin", "email": email, "action": action, "target": target})

    def tail(self, lines: int) -> list[dict[str, object]]:
        if not self._path.exists():
            return []
        with self._path.open(encoding="utf-8") as fh:
            last = deque(fh, maxlen=lines)
        return [json.loads(line) for line in reversed(last)]
