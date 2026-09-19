# db-perf-toolkit — the refusal demo.
#
# Rendered by scripts/record-demo.sh, which seeds the database, writes
# demo/out/env.sh with the connection details, then runs this tape:
#
#   bash scripts/record-demo.sh
#
# Do not run this file directly. Without demo/out/env.sh there is no
# database to point at and no `dbperf` on PATH.
#
# The point of the recording is the second command. Four indexes report
# zero scans; exactly one is dropped and the other three each say why
# not. Beat 1 exists to show what the query everyone reaches for would
# have done instead — it returns six, two of them primary keys.
#
# Nothing clears the screen. By the last frame the whole argument is
# visible at once, which is what a looping GIF needs: a reader who stops
# it anywhere in the second half still sees the refusals.

Output demo/out/refusal.gif

Require bash
Require psql

# Width: the longest refusal line is 106 characters ("unique index
# (enforces uniqueness even without a constraint row)"). Narrowing this
# wraps it and the money shot turns to mush.
#
# Height: measured, not guessed. At Width 1200 / Padding 25 this shell
# reports 112 columns and 31 rows (`tput cols; tput lines` inside the
# recording). The session is 29 rows, so the terminal never scrolls —
# which matters beyond tidiness: a scroll partway through a `Wait` has
# been observed here to stop the match ever firing.
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
# Connection details, prompt, and a warm Python import cache so the first
# command does not open on two seconds of dead air.
Hide
Type "source demo/out/env.sh" Enter
Type "clear" Enter
Sleep 2s
Show

# ----------------------------------------------- beat 1: the naive query
Type "# every index with no recorded scans:" Enter
Sleep 400ms
Type@12ms "psql -c 'select relname, indexrelname, idx_scan from pg_stat_user_indexes where idx_scan = 0'" Enter
Wait+Screen@60s /\(6 rows\)/
Sleep 2500ms

Type "# six. two of those are primary keys." Enter
Sleep 1400ms

# -------------------------------------------------- beat 2: THE REFUSAL
Type "dbperf drop-unused-indexes" Enter
Wait+Screen@60s /Dry run/
Sleep 6s

# ------------------------------------- beat 3: the SQL, and its rollback
Type "dbperf drop-unused-indexes --script" Enter
Wait+Screen@60s /DROP INDEX/
Sleep 5s
