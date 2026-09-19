#!/usr/bin/env bash
#
# Record every tape in demo/ against one seeded database.
#
#   ./scripts/record-demo.sh            seed, record all tapes, tear down
#   ./scripts/record-demo.sh --keep     leave the container up afterwards
#   ./scripts/record-demo.sh --no-seed  reuse a container from a previous --keep
#   ./scripts/record-demo.sh diagnose   record only demo/diagnose.tape
#
# Each tape names its own Output path. They share one seeded database and one
# env.sh, because recording them against separate seeds would let the numbers
# in one contradict the numbers in another — and they are shown side by side.
#
# Requires Docker, psql, uv, and VHS (https://github.com/charmbracelet/vhs).
# If VHS is not on PATH this script downloads a pinned release into
# demo/out/.tools rather than failing; set VHS_BIN to use your own.
#
# The pathological schema is NOT defined here. It is lifted out of
# scripts/demo.sh at runtime so there is exactly one definition of it. If
# that script is restructured this one fails loudly instead of recording a
# demo of the wrong database.

set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO"

CONTAINER=${DEMO_CONTAINER:-dbperf-record}
PORT=${DEMO_PORT:-55434}
OUT="$REPO/demo/out"
TOOLS="$OUT/.tools"

# VHS v0.12.0 records zero frames on this project's WSL2 reference box and
# then exits 0 with no file, which is the worst possible failure mode for a
# demo pipeline. v0.11.0 is the newest release verified to work.
VHS_VERSION=0.11.0
TTYD_VERSION=1.7.7

KEEP=0
SEED=1
ONLY=()
for arg in "$@"; do
  case "$arg" in
    --keep)    KEEP=1 ;;
    --no-seed) SEED=0 ;;
    -h|--help) sed -n '2,15p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0 ;;
    -*) echo "unknown argument: $arg" >&2; exit 2 ;;
    *)  ONLY+=("$arg") ;;
  esac
done

# Which tapes to record. A named tape must exist; asking for one that does
# not is a typo, and silently recording nothing would look like success.
TAPES=()
if [[ ${#ONLY[@]} -gt 0 ]]; then
  for name in "${ONLY[@]}"; do
    tape="demo/${name%.tape}.tape"
    [[ -f "$tape" ]] || { echo "no such tape: $tape" >&2; exit 2; }
    TAPES+=("$tape")
  done
else
  while IFS= read -r tape; do TAPES+=("$tape"); done < <(find demo -maxdepth 1 -name '*.tape' | sort)
fi
[[ ${#TAPES[@]} -gt 0 ]] || { echo "no tapes found in demo/" >&2; exit 2; }

say()  { printf '\n\033[1;36m== %s\033[0m\n' "$*"; }
die()  { printf '\033[1;31m%s\033[0m\n' "$*" >&2; exit 1; }

cleanup() {
  if [[ $KEEP -eq 0 ]]; then
    docker rm -f "$CONTAINER" >/dev/null 2>&1 || true
  fi
}
trap cleanup EXIT

# ----------------------------------------------------------------- deps
for bin in docker psql uv; do
  command -v "$bin" >/dev/null || die "$bin is required and was not found on PATH"
done

mkdir -p "$OUT" "$TOOLS"

resolve_vhs() {
  if [[ -n "${VHS_BIN:-}" ]]; then
    echo "$VHS_BIN"; return
  fi
  if command -v vhs >/dev/null; then
    echo "vhs"; return
  fi
  if [[ ! -x "$TOOLS/vhs" ]]; then
    say "Downloading VHS v$VHS_VERSION and ttyd v$TTYD_VERSION into demo/out/.tools"
    command -v curl >/dev/null || die "curl is required to fetch VHS, or set VHS_BIN"
    curl -fsSL -o "$TOOLS/vhs.tar.gz" \
      "https://github.com/charmbracelet/vhs/releases/download/v${VHS_VERSION}/vhs_${VHS_VERSION}_Linux_x86_64.tar.gz"
    tar xzf "$TOOLS/vhs.tar.gz" -C "$TOOLS" --strip-components=1 \
      "vhs_${VHS_VERSION}_Linux_x86_64/vhs"
    curl -fsSL -o "$TOOLS/ttyd" \
      "https://github.com/tsl0922/ttyd/releases/download/${TTYD_VERSION}/ttyd.x86_64"
    chmod +x "$TOOLS/vhs" "$TOOLS/ttyd"
  fi
  echo "$TOOLS/vhs"
}

VHS="$(resolve_vhs)"

# VHS drives a headless Chromium through rod, which takes the first `chrome`
# or `chromium` it finds on PATH. Under WSL that is often a one-line wrapper
# around Chrome for Windows, which cannot serve a debugging endpoint and
# makes VHS fail with "browser exited unexpectedly". Drop any PATH entry
# whose chrome is not a native executable, and let rod download its own.
clean_path() {
  local out="" dir cand
  IFS=: read -ra dirs <<<"$PATH"
  for dir in "${dirs[@]}"; do
    [[ -z "$dir" ]] && continue
    local skip=0
    for cand in chrome chromium chromium-browser google-chrome google-chrome-stable; do
      if [[ -x "$dir/$cand" ]] && ! head -c4 "$dir/$cand" 2>/dev/null | grep -q $'\x7fELF'; then
        skip=1
      fi
    done
    [[ $skip -eq 1 ]] && continue
    out="${out:+$out:}$dir"
  done
  echo "$TOOLS:$out"
}

command -v ffmpeg >/dev/null || die "ffmpeg is required by VHS and was not found on PATH"

# ----------------------------------------------------------------- seed
export PGPASSWORD=demo
DSN="postgresql://postgres:demo@localhost:${PORT}/shop"

rule()   { printf '\n\033[1;36m%s\033[0m\n' "== $* =="; }
psql_q() { psql -h localhost -p "$PORT" -U postgres -d shop -q -v ON_ERROR_STOP=1 "$@"; }

if [[ $SEED -eq 1 ]]; then
  say "Seeding the demo database (this is the slow part, ~90s)"

  # Everything scripts/demo.sh does between starting PostgreSQL and its
  # first diagnosis: create the schema, load it, apply the workload. The
  # markers are load-bearing.
  SEED_BLOCK="$(awk '
    /^rule "Starting PostgreSQL"/ { f = 1 }
    /^rule "1\. DIAGNOSE/          { f = 0 }
    f
  ' scripts/demo.sh)"

  grep -q 'orders_notes_idx' <<<"$SEED_BLOCK" \
    || die "Could not lift the seed section out of scripts/demo.sh.
That script has been restructured. Fix the awk markers in this script
rather than pasting a second copy of the schema in here."

  eval "$SEED_BLOCK"
else
  say "Skipping the seed — expecting a database on port $PORT"
  psql -h localhost -p "$PORT" -U postgres -d shop -tAc 'select 1' >/dev/null \
    || die "No database on port $PORT. Drop --no-seed."
fi

# ------------------------------------------------------------ tape env
# demo.tape sources this. Anything host-specific lives here so the tape
# itself stays static and readable.
say "Writing demo/out/env.sh"
# The tape runs `dbperf`, not `uv run dbperf`, so the venv has to exist.
# Only sync when it does not — this script has no business rewriting
# uv.lock as a side effect of making a GIF.
[[ -x "$REPO/.venv/bin/dbperf" ]] || uv sync --quiet
cat > "$OUT/env.sh" <<ENVSH
# Generated by scripts/record-demo.sh. Not checked in.
source "$REPO/.venv/bin/activate"
export DBPERF_DSN="$DSN"
export PGHOST=localhost PGPORT=$PORT PGUSER=postgres PGDATABASE=shop PGPASSWORD=demo
export PS1='\$ '
export PROMPT_COMMAND=
# Warm the import cache so the first recorded command is not two seconds of
# a blinking cursor.
dbperf --help >/dev/null 2>&1 || true
psql -tAc 'select 1' >/dev/null 2>&1 || true
ENVSH

# --------------------------------------------------------------- record
# Each tape declares its own `Output demo/out/<name>.gif`. That path is read
# back out of the tape rather than assumed, so a tape that writes somewhere
# unexpected is caught here instead of leaving a stale GIF in place and
# reporting success.
declare -a PRODUCED=()

for tape in "${TAPES[@]}"; do
  gif="$(awk '/^Output /{print $2; exit}' "$tape")"
  [[ -n "$gif" ]] || die "$tape has no Output line, so there is nothing to record."
  gif="$REPO/$gif"

  say "Recording $tape -> ${gif#$REPO/}"
  rm -f "$gif"
  env PATH="$(clean_path)" TERM=xterm-256color "$VHS" "$tape"

  [[ -s "$gif" ]] || die "VHS exited without producing ${gif#$REPO/}.
VHS can exit 0 having captured no frames — check that ttyd and a native
Chromium are reachable, and that VHS is not v0.12.0."

  # A still of the final frame, for the portfolio page and for anywhere a
  # GIF is the wrong answer. Taken from the GIF rather than VHS's own
  # Screenshot command, which was unreliable here.
  ffmpeg -y -v error -sseof -0.5 -i "$gif" -vframes 1 "${gif%.gif}.png"
  PRODUCED+=("$gif" "${gif%.gif}.png")
done

say "Done"
for f in "${PRODUCED[@]}"; do
  printf '  %-28s %8s  (%s bytes)\n' "${f#$REPO/}" \
    "$(du -h "$f" | cut -f1)" "$(stat -c %s "$f")"
  if [[ $f == *.gif ]]; then
    printf '  %-28s %8s\n' "  dimensions" \
      "$(ffprobe -v error -select_streams v:0 -show_entries stream=width,height -of csv=p=0:s=x "$f")"
  fi
done
echo
echo "  Look at them before committing. A mistimed Sleep produces a GIF that"
echo "  is the right size and shows the wrong thing."

if [[ $KEEP -eq 1 ]]; then
  echo "  container $CONTAINER left running on port $PORT"
fi
