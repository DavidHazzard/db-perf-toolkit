# db-perf-toolkit — the diagnosis demo.
#
# Rendered by scripts/record-demo.sh alongside demo.tape, against the same
# seeded database:
#
#   bash scripts/record-demo.sh diagnose
#
# Do not run this file directly. Without demo/out/env.sh there is no
# database to point at and no `dbperf` on PATH.
#
# Why two checks and not `dbperf report`: the full report is 45-56 lines
# against this database — measured, at every value of --limit — and the
# frame below holds 29. It cannot be made to fit without shrinking the font
# past legibility or letting it scroll, and a scrolling GIF cannot show the
# whole argument at once. The tool is not reformatted to fit the demo.
#
# These two were chosen because together they say something neither says
# alone: `orders` is starved of the index its reads want, and is at the same
# time paying to maintain five nobody reads. Under-indexed and over-indexed
# are not opposites, and that is the point a size-ranked list cannot make.
#
# Same geometry as demo.tape on purpose. Both GIFs render in the same
# tabbed component on the portfolio site, and a tab switch that resizes the
# frame reads as a bug.

Output demo/out/diagnose.gif

Require bash
Require psql

Set Shell bash
Set FontFamily "JetBrains Mono,DejaVu Sans Mono,Liberation Mono,monospace"
Set FontSize 16
Set LineHeight 1.2
Set Width 1200
Set Height 745
Set Padding 25
Set Framerate 24
Set TypingSpeed 25ms
Set Theme "Catppuccin Mocha"

# ---------------------------------------------------------------- setup
Hide
Type "source demo/out/env.sh" Enter
Type "clear" Enter
Sleep 2s
Show

# --------------------------------------------- beat 1: the reads suffer
Type "dbperf seq-scans" Enter
# "Live rows" is a column header, so the match does not depend on a row
# count that autovacuum timing could move.
Wait+Screen@60s /Live rows/
Sleep 3500ms

Type "# 20.8M rows read to answer 138 queries. now the other side:" Enter
Sleep 1600ms

# -------------------------------------------- beat 2: the writes pay for it
Type "dbperf index-burden" Enter
Wait+Screen@60s /Redundant idx writes/
Sleep 6s
