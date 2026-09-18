# Self-Hosted Music MCP

An MCP server that gives AI agents a real map of your music collection — local library,
ListenBrainz history, and MusicBrainz metadata joined on permanent MBIDs, queryable from
any agent.

**The music MCP for people who own their music.**

Planning docs live outside this repo (Obsidian vault). This repo is code + tests only.

## Status

Phase 0 — spike. Proving the MBID join on real data before building anything.

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
| `MUSIC_LIBRARY_ROOT` | `library_sample` | `/mnt/terra-6tb-1/media/music` (NAS host) |
| `MUSIC_WATCHLIST` | `join_e2e` | `/opt/data/scripts/music-release-radar/watchlist.json` |
| `LB_TOKEN` | `lb_check` | ListenBrainz user token (optional; public data works without it) |

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
