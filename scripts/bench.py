#!/usr/bin/env python3
"""Benchmark the diagnostic checks across scenarios.

Runs every check N times per scenario and records wall time and result
counts, then writes JSON and a markdown report. Timings are warm-cache and
same-host, so they measure the tool rather than the storage; the useful
signal is how they scale with catalog size, not their absolute value.

    ./scripts/bench.py --runs 5 --out bench.json
"""

from __future__ import annotations

import argparse
import json
import statistics
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

CHECKS = [
    ("slow-queries", ["slow-queries", "--limit", "10"]),
    ("seq-scans", ["seq-scans"]),
    ("unused-indexes", ["unused-indexes"]),
    ("index-burden", ["index-burden"]),
    ("bloat", ["bloat"]),
    ("blocking", ["blocking"]),
    ("report", ["report"]),
]


def dsn_for(db: str, port: int) -> str:
    return f"postgresql://postgres:demo@localhost:{port}/{db}"


def run_json(dsn: str, args: list[str]) -> tuple[float, object]:
    started = time.perf_counter()
    proc = subprocess.run(
        ["uv", "run", "dbperf", "--dsn", dsn, "--json", *args],
        cwd=ROOT, capture_output=True, text=True,
    )
    elapsed_ms = (time.perf_counter() - started) * 1000
    if proc.returncode != 0:
        raise RuntimeError(f"{args}: exit {proc.returncode}\n{proc.stderr[:400]}")
    return elapsed_ms, json.loads(proc.stdout)


def catalog_facts(dsn: str) -> dict[str, object]:
    import psycopg

    with psycopg.connect(dsn, autocommit=True) as conn:
        row = conn.execute("""
            SELECT pg_database_size(current_database()),
                   (SELECT count(*) FROM pg_stat_user_tables),
                   (SELECT count(*) FROM pg_stat_user_indexes),
                   COALESCE((SELECT sum(pg_relation_size(relid)) FROM pg_stat_user_tables), 0),
                   COALESCE((SELECT sum(pg_relation_size(indexrelid))
                             FROM pg_stat_user_indexes), 0)
        """).fetchone()
    assert row is not None
    return {
        "db_bytes": int(row[0]),
        "tables": int(row[1]),
        "indexes": int(row[2]),
        "heap_bytes": int(row[3]),
        "index_bytes": int(row[4]),
    }


def query_timings(dsn: str, runs: int) -> dict[str, dict[str, float]]:
    """Time the queries in-process, without CLI startup.

    End-to-end `uv run dbperf ...` is dominated by interpreter startup — over
    a second before a single byte reaches PostgreSQL — which would drown the
    thing being measured. These are the numbers that actually scale with
    catalog size.
    """
    from db_perf_toolkit.backends import connect

    out: dict[str, dict[str, float]] = {}
    with connect(dsn) as backend:
        probes = {
            "slow-queries": lambda: backend.slow_queries(10),
            "seq-scans": lambda: backend.seq_scan_hotspots(50, 1000),
            "unused-indexes": lambda: backend.unused_indexes(0),
            "index-burden": lambda: backend.index_burden(2),
            "bloat": lambda: backend.bloated_tables(10.0, 1000),
            "blocking": lambda: backend.blocking_chains(),
        }
        for name, fn in probes.items():
            samples = []
            for _ in range(runs):
                started = time.perf_counter()
                fn()
                samples.append((time.perf_counter() - started) * 1000)
            out[name] = {
                "min": round(min(samples), 2),
                "median": round(statistics.median(samples), 2),
                "max": round(max(samples), 2),
            }
    return out


def cli_startup_ms(runs: int) -> float:
    """The fixed cost of `uv run dbperf`, with no database work at all."""
    samples = []
    for _ in range(runs):
        started = time.perf_counter()
        subprocess.run(
            ["uv", "run", "dbperf", "--version"], cwd=ROOT, capture_output=True, text=True
        )
        samples.append((time.perf_counter() - started) * 1000)
    return round(statistics.median(samples), 1)


def measure(scenario: str, db: str, port: int, runs: int) -> dict[str, object]:
    dsn = dsn_for(db, port)
    facts = catalog_facts(dsn)
    timings: dict[str, list[float]] = {name: [] for name, _ in CHECKS}
    counts: dict[str, int] = {}
    payloads: dict[str, object] = {}

    for run in range(runs):
        for name, args in CHECKS:
            ms, data = run_json(dsn, args)
            timings[name].append(ms)
            if run == 0:
                counts[name] = len(data) if isinstance(data, list) else 1
                payloads[name] = data
        print(f"  {scenario}: run {run + 1}/{runs} done", file=sys.stderr)

    unused = payloads.get("unused-indexes") or []
    assert isinstance(unused, list)
    floor = 8 * 1024**2
    droppable = [
        i for i in unused
        if not i["is_unique"] and not i["enforces_constraint"]
        and i["scans"] == 0 and i["size_bytes"] >= floor
    ]
    under_floor = [i for i in unused if i["size_bytes"] < floor]
    burden = payloads.get("index-burden") or []
    assert isinstance(burden, list)

    return {
        "scenario": scenario,
        "database": db,
        "runs": runs,
        "catalog": facts,
        "query_ms": query_timings(dsn, runs),
        "timings_ms": {
            name: {
                "min": round(min(v), 1),
                "median": round(statistics.median(v), 1),
                "max": round(max(v), 1),
                "stdev": round(statistics.stdev(v), 1) if len(v) > 1 else 0.0,
            }
            for name, v in timings.items()
        },
        "counts": counts,
        "findings": {
            "unused_indexes": len(unused),
            "droppable": len(droppable),
            "droppable_bytes": sum(i["size_bytes"] for i in droppable),
            "under_floor": len(under_floor),
            "under_floor_bytes": sum(i["size_bytes"] for i in under_floor),
            "burden_tables": len(burden),
            "redundant_writes": sum(
                b["unused_count"] * b["writes"] for b in burden
            ),
            "worst_table": burden[0]["table"] if burden else None,
            "worst_unused": (
                f"{burden[0]['unused_count']}/{burden[0]['index_count']}" if burden else None
            ),
        },
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", type=int, default=5)
    ap.add_argument("--port", type=int, default=55433)
    ap.add_argument("--out", default="bench.json")
    args = ap.parse_args()

    scenarios = [("small", "shop"), ("realistic", "bigmess"), ("horror", "horror")]
    startup = cli_startup_ms(args.runs)
    print(f"  CLI startup floor: {startup}ms", file=sys.stderr)
    results = [measure(name, db, args.port, args.runs) for name, db in scenarios]

    Path(args.out).write_text(
        json.dumps({"cli_startup_ms": startup, "scenarios": results}, indent=2)
    )
    print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
