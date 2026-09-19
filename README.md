# Self-Hosted Music MCP

An MCP server that gives AI agents a real map of your music collection — local library,
ListenBrainz history, and MusicBrainz metadata joined on permanent MBIDs, queryable from
any agent.

**The music MCP for people who own their music.**

Planning docs live outside this repo (Obsidian vault). This repo is code + tests only.

## Status

Phase 3 (MusicBrainz layer + name→MBID resolver) — code on branch, real-data verified.
Phases 1 (library index) and 2 (ListenBrainz listens) are merged. Next: Phase 4 —
compound `discovery_*` tools and MCP server wiring.

## Setup

Development runs on [uv](https://docs.astral.sh/uv/) — a single static binary that
manages Python versions, virtualenvs, and dependencies (no sudo, no system Python
changes; the snap package is a lagging mirror, prefer the official installer).

Install uv (Linux/macOS):

```sh
curl -LsSf https://astral.sh/uv/install.sh | sh
# then restart your shell or: source ~/.bashrc  (uv lives in ~/.local/bin)
```

Windows: `powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"`

Then, from the repo root:

```sh
uv sync          # creates .venv; also fetches Python 3.13 if your system lacks it
```

## Development

```sh
uv run pytest        # unit tests only; -m integration opts in to real-data tests
uv run ruff check .
```

`uv run` executes inside the project venv — there is nothing to activate. All other
commands below use the same prefix.

Requires Python 3.13+ (uv provides it).

## Configuration

Machine-specific paths come from environment variables — mount points differ between
the NAS host and containers/other boxes. Put these in your shell profile or an
uncommitted `.env` file:

| Variable | Used by | Example |
|---|---|---|
| `MUSIC_LIBRARY_ROOT` | `library` scan | `/path/to/music` |
| `MUSIC_DB` | all index/cache commands | `/path/to/music-index.db` |
| `LB_USER` | `listens` sync/reports | ListenBrainz username |
| `LB_TOKEN` | `listens sync` | ListenBrainz user token (optional; public data works without it) |
| `MUSIC_WATCHLIST` | spike `join_e2e` only | `/path/to/watchlist.json` |

Every value can also be passed as a CLI argument (`--help` shows which), which wins
over the env var.

## Spike (Phase 0)

Read-only programs that were run against the real library to validate the MBID join
(results and verdict: `docs/spike/findings.md`). Re-run any of them from the repo root:

```sh
uv run python -m music_mcp.spike.library_sample          # MBID coverage sample (40 artists default)
uv run python -m music_mcp.spike.library_sample --max-artists 100   # larger sample
uv run python -m music_mcp.spike.lb_check                # ListenBrainz endpoints
uv run python -m music_mcp.spike.mb_check                # rate-limited MB client (default artist)
uv run python -m music_mcp.spike.mb_check 83d91898-7763-47d2-b9cb-065c48cd3809   # specific artist
uv run python -m music_mcp.spike.join_e2e                # watchlist x library join
uv run python -m music_mcp.spike.wal_smoke               # SQLite WAL concurrency
```

These touch the local music library and live APIs by design — keep them off CI (they
are excluded via pytest's default marker filtering; spike modules are not imported by
tests except through their unit-tested helpers).

## Scope

Read-only knowledge layer over your own data: library index, listens, canonical metadata.
No playback control, no acquisition, no web UI. Mutations (tag writes) are explicit and
opt-in.

## Library index (Phase 1)

The offline index: walks your library, reads tags, and stores artists/albums/tracks
in a SQLite (WAL) cache. Re-runs are incremental (unchanged files skipped via
mtime+size); the library is only ever read.

```sh
export MUSIC_LIBRARY_ROOT="/path/to/music"        # your library mount (see Configuration)
export MUSIC_DB="/path/to/music-index.db"         # where the cache lives (also: --db)

uv run python -m music_mcp.library scan           # incremental scan into the cache
uv run python -m music_mcp.library scan --full    # re-read every file, ignore mtimes
uv run python -m music_mcp.library status         # counts, MBID coverage %, formats
uv run python -m music_mcp.library artists --filter-mbid missing   # resolver worklist
uv run python -m music_mcp.library albums "Artist Name"            # by MBID or tag name
```

What to expect: the first scan reads every audio file (several minutes over a network
mount); a second `scan` right after should report `added: 0` with most files
`unchanged` and finish in seconds. `status` shows the MBID coverage the resolver
work in Phase 3 will improve.

## Listens layer (Phase 2)

ListenBrainz listens synced into the same cache (incremental by timestamp; token
optional for public profiles). Joins listen history against the library:

```sh
export LB_USER="your-lb-username"                 # optional: LB_TOKEN (private profiles)

uv run python -m music_mcp.listens sync           # incremental sync (--pages N bounds it)
uv run python -m music_mcp.listens recent         # newest listens
uv run python -m music_mcp.listens top            # top artists by listen count
uv run python -m music_mcp.listens gap            # listened but not owned (shopping list)
uv run python -m music_mcp.listens stale          # owned but dormant (rediscovery list)
uv run python -m music_mcp.listens discoveries    # artists first listened in the window
```

Report windows: `--min-listens` (gap threshold), `--months` (stale window),
`--days` (discovery window). LB artist names are credit strings ("A, B",
"A feat. C"); ownership checks split them and match any part case-insensitively
against library names, so owned collaborations don't fake a gap. Truly-unowned
and owned-but-untagged artists both remain listed — the Phase 3 name→MBID
resolver separates them.

## MusicBrainz layer + resolver (Phase 3)

Rate-limited (1 req/s) MusicBrainz lookups with a 30-day SQLite response cache
(MB metadata is effectively immutable), plus the resolver that assigns MBIDs to
untagged library artists.

```sh
uv run python -m music_mcp.mb artist <artist_mbid>        # artist metadata (cached)
uv run python -m music_mcp.mb releases <artist_mbid>      # official album/EP release-groups
uv run python -m music_mcp.mb search --query "Name"       # alias-aware artist search
uv run python -m music_mcp.mb lookup --mbid <mbid> --entity release

uv run python -m music_mcp.mb resolve                     # propose MBIDs for untagged artists
uv run python -m music_mcp.mb apply                       # write confident proposals into the index
```

**How resolution works (propose → review → apply):**
- `resolve` is read-only: it searches MB for each untagged artist in the index
  (splitting multi-artist folder names like `A;B`), and stores candidates with a
  confidence label (`exact` / `case-insensitive` / `fuzzy` / `none`). Expect several
  minutes for a full library (MB rate limit); every response is cached, so re-runs
  are free.
- `apply` writes only confident matches (exact/case-insensitive name at score ≥95)
  into the **index** — it never writes to your audio files' tags. Applied MBIDs
  survive `--full` rescans (files that later gain real tags win). Fuzzy and
  part-match proposals stay unapplied for your review.
- `resolve`/`apply` refuse empty indexes, so a misplaced `MUSIC_DB` announces itself
  instead of returning a happy empty result.

**Library intelligence over the index:**

```sh
uv run python -m music_mcp.library dupes                   # duplicate releases / mislabeled folders
uv run python -m music_mcp.library dupes --scope folder    # or release_group / title
uv run python -m music_mcp.library quality                 # tag gaps, credit variants, bitrates
uv run python -m music_mcp.library quality --min-bitrate 256000
```

`dupes` reports three signals: the same release MBID in multiple folders (true
duplicates), the same normalized album title within one artist, and a folder whose
files carry multiple release identities. `quality` shows missing-MBID percentages,
artists credited several ways, and below-threshold bitrates per format.
