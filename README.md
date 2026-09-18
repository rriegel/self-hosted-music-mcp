# Self-Hosted Music MCP

An MCP server that gives AI agents a real map of your music collection — local library,
ListenBrainz history, and MusicBrainz metadata joined on permanent MBIDs, queryable from
any agent.

**The music MCP for people who own their music.**

Planning docs live outside this repo (Obsidian vault). This repo is code + tests only.

## Status

Phase 0 — spike. Proving the MBID join on real data before building anything.

## Development

```sh
uv sync
uv run pytest        # unit tests only; -m integration opts in to real-data tests
uv run ruff check .
```

Requires Python 3.13+. Real-data work (library scans, live LB/MB calls) runs on the host
machine that owns the library — see `scripts/spike/` for the Phase 0 spike programs.

## Scope

Read-only knowledge layer over your own data: library index, listens, canonical metadata.
No playback control, no acquisition, no web UI. Mutations (tag writes) are explicit and
opt-in.
