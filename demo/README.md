# The demo recording

`demo/out/refusal.gif` — 23 seconds, no install required, embeds in a GitHub
README and in a plain HTML page with the same one-line tag.

```markdown
![db-perf-toolkit refusing to drop three of four unused indexes](demo/out/refusal.gif)
```

```html
<img src="refusal.gif" alt="db-perf-toolkit refusing to drop three of four unused indexes" width="1200">
```

`demo/out/refusal.png` is the last frame as a still (181 KB — antialiased
text does not compress, so it is no cheaper than the animation), for social
cards, slides, and anywhere that cannot show motion. The last frame is
deliberately the whole argument at once — nothing scrolls off.

## Regenerating it

```bash
./scripts/record-demo.sh
```

Seeds a throwaway PostgreSQL container, writes the connection details to
`demo/out/env.sh`, renders `demo/demo.tape`, prints the file size, and tears
the container down. About three minutes, most of it the seed.

```bash
./scripts/record-demo.sh --keep      # leave the container up
./scripts/record-demo.sh --no-seed   # reuse a container from a previous --keep
```

`--no-seed` is the one to use while iterating on the tape: the recording only
runs read-only commands, so the same seeded database survives any number of
takes.

**Look at the result before committing it.** A mistimed `Sleep` produces a GIF
of exactly the right size showing exactly the wrong thing, and nothing in the
pipeline can tell the difference.

### What you need installed

| | |
|---|---|
| Docker | the throwaway PostgreSQL |
| `psql` | seeding, and beat 1 of the recording |
| `uv` | running the tool |
| `ffmpeg` | VHS encodes the GIF with it |
| VHS | optional — the script downloads a pinned release if it is missing |

VHS also drives a headless Chromium, which it downloads on first run
(~150 MB into `~/.cache/rod`). Two things that bite:

- **Do not use VHS v0.12.0.** On the WSL2 box this was built on it records
  zero frames, then exits 0 having written no file. v0.11.0 is the newest
  release verified to work, and is what `record-demo.sh` pins.
- **Under WSL, a `chrome` on `PATH` is often a wrapper around Chrome for
  Windows**, which cannot serve a debugging endpoint. VHS fails with
  `browser exited unexpectedly`. `record-demo.sh` strips such entries from
  `PATH` before invoking VHS so Chromium downloads instead.

Set `VHS_BIN` to use your own build. `DEMO_PORT` (default 55434) and
`DEMO_CONTAINER` move the database out of the way of `scripts/demo.sh`, which
uses 55432.

## Why the recording is shaped the way it is

The demo is not the diagnostic table. It is the refusal.

Beat 1 runs the query everyone reaches for — `pg_stat_user_indexes where
idx_scan = 0`. It returns **six** indexes. Two of them are primary keys.
Acting on that list drops two constraints.

Beat 2 runs `dbperf drop-unused-indexes` against the same database. One drop,
three refusals, each with the reason attached: unique, constraint-backed, and
below the size floor. That frame is on screen from about ten seconds in until
the end — a reader who pauses the loop anywhere in the second half is looking
at it.

Beat 3 prints the SQL rather than running it, so the `CREATE INDEX` rollback
and the `CONCURRENTLY` are visible. That is the part a DBA checks for before
trusting anything.

Nothing clears the screen, which is why the geometry is fixed the way it is.
At `Width 1200` / `Padding 25` the recorded shell reports **112 columns and 31
rows** (measured with `tput`, not guessed). The session is 29 rows, so it never
scrolls. That matters beyond tidiness: a scroll partway through a `Wait` was
observed to stop the match firing, and VHS then fails the whole recording.

The width is set by one line. The longest refusal is 106 characters —

```
  skip  "public"."orders_reference_key" — unique index (enforces uniqueness even without a constraint row)
```

— and it has to stay on one line or the point of the recording turns to mush.
Six columns of slack. If the tool's wording grows, widen the tape.

## Why VHS, and why GIF

**VHS**, because the tape is a script. This tool's output will change, and a
recording that cannot be regenerated from source goes stale and becomes a
liability — a README showing a version of the tool that no longer exists is
worse than no README at all. `demo/demo.tape` is 72 lines of readable
declarative text under version control; re-running it is one command.

asciinema records a live session, so re-recording means performing it again by
hand, or writing a shell script full of `sleep`s that simulates typing badly.
termtosvg has the same shape and its last release was 2020.

**GIF**, because it is the only format that works in both targets. Measured,
on the actual artefact:

| | Size | Renders in a GitHub README? |
|---|---|---|
| **GIF** (1200×745, 24 fps, 572 frames, 22.9 s) | **183 KB** | yes |
| MP4 (H.264, same frames) | 263 KB | no — GitHub markdown does not render `<video>` for a repo file |
| WebM (VP9 crf 40, same frames) | 286 KB | no, same reason |
| Animated SVG (termtosvg, same commands) | 10 KB (2.5 KB gzipped) | renders, but see below |
| asciinema `.cast` + player | ~10 KB | **no** — the player is JavaScript |

Two of those deserve the detail:

**asciinema is disqualified outright.** GitHub strips `<script>` from rendered
markdown, so the player cannot run. asciinema's own documentation says to link
a thumbnail image out to a page hosting the player, which means the thing a
stranger is supposed to watch is one click away on someone else's domain.

**SVG is 18× smaller and still the wrong choice.** Animated SVG referenced
through `<img>` does animate on GitHub — the browser puts it in the SVG
integration spec's *secure animated mode*, where script is off but declarative
animation is on. It does not animate **in Firefox**, apparently because of
GitHub's CSP, and
[github/markup#1864](https://github.com/github/markup/issues/1864) was closed
as not planned. Firefox users would see a frozen first frame with no
indication anything was meant to happen. For an artefact whose entire job is
that a stranger watches it, a silent failure for a whole browser's users is
not a size trade worth taking.

Note that the video formats came out *larger* than the GIF here. Terminal
output is flat colour and sharp edges: it is close to the best case for GIF's
palette and worst case for a codec built for photographic motion.

## Length

Twenty-three seconds, and that is near the ceiling. The refusal lands at about
ten seconds and holds for thirteen. A README GIF autoplays on a page the
reader did not come to watch a video on, so the useful measure is not total
runtime but how long the payoff is on screen — here, 55% of the loop. Adding
the `--execute` path and the re-diagnosis would push past a minute and cut
that fraction to a third. Those live in `scripts/demo.sh`, which a reader who
is already interested can run themselves.
