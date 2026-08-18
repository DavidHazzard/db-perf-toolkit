#!/usr/bin/env python3
"""Turn bench.json into BENCHMARK.md."""

from __future__ import annotations

import json
import sys
from pathlib import Path

CHECKS = ["slow-queries", "seq-scans", "unused-indexes", "index-burden", "bloat", "blocking"]
LABEL = {"small": "Small", "realistic": "Realistic", "horror": "Horror"}


def size(n: int) -> str:
    if n >= 1024**3:
        return f"{n / 1024**3:.2f} GB"
    if n >= 1024**2:
        return f"{n / 1024**2:.0f} MB"
    return f"{n / 1024:.0f} kB"


def main() -> int:
    data = json.loads(Path(sys.argv[1]).read_text())
    out = Path(sys.argv[2])
    scen = data["scenarios"]
    runs = scen[0]["runs"]
    by = {s["scenario"]: s for s in scen}
    order = ["small", "realistic", "horror"]

    L = []
    w = L.append
    w("# Benchmark")
    w("")
    w(f"Every check run **{runs} times** against three PostgreSQL 16 databases of "
      "escalating awfulness. Regenerate with:")
    w("")
    w("```bash")
    w("./scripts/bench.py --runs 5 --out bench.json")
    w("./scripts/bench_report.py bench.json BENCHMARK.md")
    w("```")
    w("")
    w("Scenario schemas live in [`scripts/scenarios/`](scripts/scenarios/).")
    w("")

    w("## The three databases")
    w("")
    w("| | Small | Realistic | Horror |")
    w("|---|---|---|---|")
    w("| | one app table | a decade of growth | college project → startup → PE → offshore |")
    rows = [
        ("Size", lambda c: size(c["db_bytes"])),
        ("Tables", lambda c: f"{c['tables']:,}"),
        ("Indexes", lambda c: f"{c['indexes']:,}"),
        ("Heap", lambda c: size(c["heap_bytes"])),
        ("Index bytes", lambda c: size(c["index_bytes"])),
    ]
    for label, fn in rows:
        w(f"| {label} | " + " | ".join(fn(by[s]["catalog"]) for s in order) + " |")
    w("| Index-to-heap | " + " | ".join(
        f"**{100 * by[s]['catalog']['index_bytes'] / by[s]['catalog']['heap_bytes']:.0f}%**"
        for s in order) + " |")
    w("")

    w("## Query time")
    w("")
    w(f"Median of {runs} runs, measured in-process. Milliseconds.")
    w("")
    w("| Check | Small | Realistic | Horror | Scaling |")
    w("|---|---|---|---|---|")
    for check in CHECKS:
        vals = [by[s]["query_ms"][check]["median"] for s in order]
        factor = f"{vals[2] / vals[0]:.0f}×" if vals[0] > 0 else "—"
        w(f"| `{check}` | {vals[0]:.1f} | {vals[1]:.1f} | {vals[2]:.1f} | {factor} |")
    w("")
    w(f"**CLI startup floor: {data['cli_startup_ms']:.0f} ms.** That is `uv run dbperf "
      "--version` — interpreter and import cost before a single byte reaches PostgreSQL. "
      "It dominates end-to-end wall time at every scale, so the table above measures the "
      "queries rather than the launcher. A first pass at this benchmark reported ~1s per "
      "check and was measuring Python startup.")
    w("")
    idx_growth = by["horror"]["catalog"]["indexes"] / by["small"]["catalog"]["indexes"]
    worst = max(
        by["horror"]["query_ms"][c]["median"] / max(by["small"]["query_ms"][c]["median"], 0.01)
        for c in CHECKS
    )
    w(f"Catalog size grows **{idx_growth:.0f}x** from Small to Horror "
      f"({by['small']['catalog']['indexes']:,} to "
      f"{by['horror']['catalog']['indexes']:,} indexes); the heaviest check grows "
      f"about {worst:.0f}x. Sub-linear, because the statistics views are indexed and the "
      "cost is dominated by `pg_relation_size()` calls per row returned.")
    w("")

    w("## What each database is guilty of")
    w("")
    w("| | Small | Realistic | Horror |")
    w("|---|---|---|---|")
    f = {s: by[s]["findings"] for s in order}
    def unused_cell(s: str) -> str:
        total = by[s]["catalog"]["indexes"]
        n = f[s]["unused_indexes"]
        return f"{n:,} of {total:,} ({100 * n / total:.0f}%)"

    w("| Unused indexes | " + " | ".join(unused_cell(s) for s in order) + " |")
    w("| Tables carrying unused indexes | " + " | ".join(
        f"{f[s]['burden_tables']:,}" for s in order) + " |")
    w("| Worst table | " + " | ".join(
        f"`{f[s]['worst_table']}` ({f[s]['worst_unused']} unused)" for s in order) + " |")
    w("| Redundant index writes | " + " | ".join(
        f"{f[s]['redundant_writes']:,}" for s in order) + " |")
    w("")

    w("## The finding that changed the tool")
    w("")
    w("`drop-unused-indexes` applies an 8MB floor, borrowed from Ola Hallengren's "
      "`@MinNumberOfPages = 1000`. Running all three scenarios showed where that borrow "
      "breaks down.")
    w("")
    w("| | Small | Realistic | Horror |")
    w("|---|---|---|---|")
    w("| Above floor — would drop | " + " | ".join(
        f"{f[s]['droppable']} → **{size(f[s]['droppable_bytes'])}**" for s in order) + " |")
    w("| Below floor — refused | " + " | ".join(
        f"{f[s]['under_floor']:,} → {size(f[s]['under_floor_bytes'])}" for s in order) + " |")
    w("")
    small_r = f["realistic"]["droppable_bytes"] / max(f["realistic"]["under_floor_bytes"], 1)
    horror_r = f["horror"]["under_floor_bytes"] / max(f["horror"]["droppable_bytes"], 1)
    w(f"On the realistic database the floor works exactly as intended: the indexes it "
      f"surfaces hold {small_r:.1f}× more than everything it dismisses. **On the horror "
      f"database it inverts** — the {f['horror']['under_floor']:,} indexes dismissed as "
      f"too small to bother with hold {horror_r:.1f}× *more* than the "
      f"{f['horror']['droppable']} it would act on.")
    w("")
    w("And bytes are the lesser cost. `orders_2020` carries 12 indexes, 11 unread; every "
      "INSERT pays 11 B-tree writes that serve no query, whether those indexes are 16kB "
      "or 16MB. A per-index size floor is structurally unable to see that.")
    w("")
    w("That is what the `index-burden` check exists for: it ranks tables by "
      "`unused indexes × row modifications` rather than by size. The floor was a sound "
      "borrow for *maintenance* — rebuilding a tiny index really is pointless — and the "
      "wrong instrument for *dropping*.")
    w("")

    w("## Caveats")
    w("")
    w("- Warm cache, single host, PostgreSQL 16 in Docker with `fsync=off`. These measure "
      "the tool's scaling, not your storage.")
    w("- `autovacuum=off` in every scenario, so bloat persists to be measured. Real servers "
      "reclaim continuously.")
    w("- Row counts are from the first run; timings from all runs.")
    out.write_text("\n".join(L) + "\n")
    print(f"wrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
