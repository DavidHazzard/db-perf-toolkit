"""Rollback manifests.

Every destructive run writes one before touching anything. It records the
exact statement needed to undo each operation, so a drop is recoverable
rather than merely regrettable.

The manifest is written to local disk, never to the target database — the
tool must not require write access somewhere just to keep its own notes.
"""

from __future__ import annotations

import json
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from db_perf_toolkit.models import Plan

MANIFEST_VERSION = 1
DEFAULT_DIR = Path.home() / ".db-perf-toolkit" / "rollbacks"


def default_path(database: str, when: datetime | None = None) -> Path:
    stamp = (when or datetime.now(UTC)).strftime("%Y%m%dT%H%M%SZ")
    safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in database)
    return DEFAULT_DIR / f"{safe}-{stamp}.json"


def write(plan: Plan, path: Path, *, host: str | None = None) -> Path:
    """Persist rollback SQL for every destructive operation in the plan."""
    payload: dict[str, Any] = {
        "manifest_version": MANIFEST_VERSION,
        "created_at": datetime.now(UTC).isoformat(),
        "engine": plan.engine,
        "database": plan.database,
        "host": host,
        "operations": [asdict(op) for op in plan.operations if op.destructive],
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return path


def read(path: Path) -> dict[str, Any]:
    data: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    version = data.get("manifest_version")
    if version != MANIFEST_VERSION:
        raise ValueError(f"unsupported manifest version {version!r} in {path}")
    return data


def rollback_statements(data: dict[str, Any]) -> list[tuple[str, str]]:
    """(target, sql) pairs that undo a previous run."""
    out: list[tuple[str, str]] = []
    for op in data.get("operations", []):
        sql = op.get("rollback_sql")
        if sql:
            out.append((str(op.get("target", "?")), str(sql)))
    return out
