"""Layer 4: audit log — JSON lines, tail order and limit."""

from __future__ import annotations

import json
from pathlib import Path

from kbc_access.audit import AuditLog


def _query(log: AuditLog, n: int) -> None:
    log.query(
        app_id="sales",
        email=f"user{n}@x.example",
        allowed=True,
        reason="ok",
        datasets=["drug_sales"],
        grants={"drug_sales": ["public_rows"]},
        masked_columns={"drug_sales": []},
        absent_columns={"drug_sales": ["margin"]},
        policy_fingerprint="abc",
        query_hash="def",
        row_count=n,
    )


def test_creates_parent_dir(tmp_path: Path) -> None:
    path = tmp_path / "deep" / "er" / "audit.jsonl"
    AuditLog(path)
    assert path.parent.is_dir()


def test_tail_missing_file_is_empty(tmp_path: Path) -> None:
    assert AuditLog(tmp_path / "audit.jsonl").tail(10) == []


def test_tail_newest_first_and_limit(tmp_path: Path) -> None:
    log = AuditLog(tmp_path / "audit.jsonl")
    for n in range(10):
        _query(log, n)
    tail = log.tail(3)
    assert [t["row_count"] for t in tail] == [9, 8, 7]
    assert [t["row_count"] for t in log.tail(100)] == list(range(9, -1, -1))
    assert [t["row_count"] for t in log.tail(1)] == [9]


def test_one_json_line_per_event(tmp_path: Path) -> None:
    path = tmp_path / "audit.jsonl"
    log = AuditLog(path)
    _query(log, 1)
    log.gate(app_id="sales", email=None, allowed=False, reason="not_authenticated", matched=(), hostile_headers=("x-forwarded-email",), path="/")
    log.admin(email="anna@x.example", action="grant", target="sales")
    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 3
    events = [json.loads(line) for line in lines]
    assert [e["event"] for e in events] == ["query", "gate", "admin"]
    assert all("ts" in e for e in events)
    assert events[1] == {**events[1], "email": None, "matched_grants": [], "hostile_headers": ["x-forwarded-email"], "path": "/"}
    assert set(events[2]) == {"ts", "event", "email", "action", "target"}


def test_gate_line_fields(tmp_path: Path) -> None:
    log = AuditLog(tmp_path / "audit.jsonl")
    log.gate(app_id="sales", email="a@b.example", allowed=True, reason="allowed", matched=("group:abc", "user:a@b.example"), hostile_headers=(), path="/x")
    [line] = log.tail(5)
    assert line["matched_grants"] == ["group:abc", "user:a@b.example"]
    assert line["allowed"] is True and line["reason"] == "allowed" and line["app_id"] == "sales"


def test_non_ascii_is_kept_readable(tmp_path: Path) -> None:
    path = tmp_path / "audit.jsonl"
    log = AuditLog(path)
    log.admin(email="žofie@x.example", action="create_user", target="žofie@x.example")
    assert "žofie" in path.read_text(encoding="utf-8")
    assert log.tail(1)[0]["email"] == "žofie@x.example"


def test_appends_across_instances(tmp_path: Path) -> None:
    path = tmp_path / "audit.jsonl"
    _query(AuditLog(path), 1)
    _query(AuditLog(path), 2)
    assert [t["row_count"] for t in AuditLog(path).tail(5)] == [2, 1]


def test_no_free_form_payload_argument() -> None:
    import inspect

    for method in (AuditLog.query, AuditLog.gate, AuditLog.admin):
        params = inspect.signature(method).parameters.values()
        assert not any(p.kind in (p.VAR_KEYWORD, p.VAR_POSITIONAL) for p in params)
