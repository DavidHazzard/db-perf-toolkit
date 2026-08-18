#!/usr/bin/env python3
"""Measure the write path: vacuum and index drops, before and after.

Destructive. Regenerate the scenarios from scripts/scenarios/ afterwards.

    ./scripts/bench_remediate.py --out remediate.json
"""

from __future__ import annotations

import argparse
import json
import subprocess
import threading
import time
from pathlib import Path

import psycopg

ROOT = Path(__file__).resolve().parent.parent


def dsn_for(db: str, port: int) -> str:
    return f"postgresql://postgres:demo@localhost:{port}/{db}"


def state(dsn: str) -> dict[str, int]:
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute("SELECT pg_stat_force_next_flush()")
        row = conn.execute("""
            SELECT pg_database_size(current_database()),
                   (SELECT count(*) FROM pg_stat_user_indexes),
                   COALESCE((SELECT sum(pg_relation_size(indexrelid))
                             FROM pg_stat_user_indexes), 0),
                   COALESCE((SELECT sum(n_dead_tup) FROM pg_stat_user_tables), 0),
                   COALESCE((SELECT sum(pg_relation_size(relid))
                             FROM pg_stat_user_tables), 0),
                   (SELECT count(*) FROM pg_stat_user_tables WHERE last_vacuum IS NOT NULL)
        """).fetchone()
    assert row is not None
    return {
        "db_bytes": int(row[0]),
        "indexes": int(row[1]),
        "index_bytes": int(row[2]),
        "dead_tuples": int(row[3]),
        "heap_bytes": int(row[4]),
        "vacuumed_tables": int(row[5]),
    }


def run_cli(dsn: str, args: list[str]) -> tuple[float, str]:
    started = time.perf_counter()
    proc = subprocess.run(
        ["uv", "run", "dbperf", "--dsn", dsn, *args],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    elapsed = time.perf_counter() - started
    if proc.returncode != 0:
        raise RuntimeError(f"{args}: exit {proc.returncode}\n{proc.stderr[:600]}")
    return elapsed, proc.stdout


class ConcurrentWriter:
    """Hammers a table with writes while indexes are dropped underneath it.

    DROP INDEX CONCURRENTLY is supposed to avoid the lock that would block
    writes. That claim is worth testing rather than repeating: if any commit
    stalls for seconds, CONCURRENTLY is not doing what the README says.
    """

    def __init__(self, dsn: str, table: str) -> None:
        self.dsn, self.table = dsn, table
        self.commits = 0
        self.max_stall_ms = 0.0
        self.errors: list[str] = []
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def _run(self) -> None:
        try:
            with psycopg.connect(self.dsn, autocommit=True) as conn:
                conn.execute(
                    f"CREATE TABLE IF NOT EXISTS {self.table} (id serial PRIMARY KEY, n int)"
                )
                while not self._stop.is_set():
                    t = time.perf_counter()
                    conn.execute(f"INSERT INTO {self.table} (n) VALUES (1)")
                    stall = (time.perf_counter() - t) * 1000
                    self.max_stall_ms = max(self.max_stall_ms, stall)
                    self.commits += 1
                    time.sleep(0.002)
        except Exception as exc:  # reported below, not swallowed
            self.errors.append(str(exc))

    def __enter__(self) -> ConcurrentWriter:
        self._thread.start()
        time.sleep(0.5)
        return self

    def __exit__(self, *exc: object) -> None:
        self._stop.set()
        self._thread.join(timeout=15)


def newest_manifest() -> Path | None:
    d = Path.home() / ".db-perf-toolkit" / "rollbacks"
    if not d.exists():
        return None
    files = sorted(d.glob("*.json"), key=lambda p: p.stat().st_mtime)
    return files[-1] if files else None


def remediate(scenario: str, db: str, port: int, writer_load: bool) -> dict[str, object]:
    dsn = dsn_for(db, port)
    before = state(dsn)

    vac_s, _ = run_cli(dsn, ["vacuum", "--execute"])
    after_vacuum = state(dsn)

    writer = None
    if writer_load:
        writer = ConcurrentWriter(dsn, "bench_concurrent_writes")
        writer.__enter__()
    try:
        drop_s, drop_out = run_cli(dsn, ["drop-unused-indexes", "--execute", "--yes"])
    finally:
        if writer:
            writer.__exit__()

    after = state(dsn)
    manifest_path = newest_manifest()

    restore: dict[str, object] = {}
    if manifest_path:
        data = json.loads(manifest_path.read_text())
        if data.get("database") == db and data.get("operations"):
            res_s, _ = run_cli(dsn, ["restore-indexes", "--from", str(manifest_path), "--execute"])
            restored = state(dsn)
            restore = {
                "seconds": round(res_s, 2),
                "statements": len(data["operations"]),
                "indexes_after_restore": restored["indexes"],
                "fully_restored": restored["indexes"] == before["indexes"],
            }

    return {
        "scenario": scenario,
        "database": db,
        "before": before,
        "after_vacuum": after_vacuum,
        "after": after,
        "vacuum_seconds": round(vac_s, 2),
        "drop_seconds": round(drop_s, 2),
        "dropped": drop_out.count("  ok      "),
        "manifest": str(manifest_path) if manifest_path else None,
        "restore": restore,
        "concurrent_writes": (
            {
                "commits": writer.commits,
                "max_stall_ms": round(writer.max_stall_ms, 1),
                "errors": writer.errors,
            }
            if writer
            else None
        ),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=55433)
    ap.add_argument("--out", default="remediate.json")
    args = ap.parse_args()

    results = []
    for scenario, db in (("small", "shop"), ("realistic", "bigmess"), ("horror", "horror")):
        print(f"remediating {scenario} ({db})...", flush=True)
        # Concurrent-write probe only on the largest, where drops take longest.
        results.append(remediate(scenario, db, args.port, writer_load=(db == "horror")))
        print(f"  done: {results[-1]['dropped']} dropped", flush=True)

    Path(args.out).write_text(json.dumps(results, indent=2))
    print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
