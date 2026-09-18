# Phase 0 Spike — Findings

**Date:** 2026-09-17/18 · **Run on:** rriegel-box (Beelink), real data, read-only
**All commands:** `uv run python -m music_mcp.spike.<module>` — outputs in `docs/spike/`

## Verdict: GO

The MBID join works on real data, well above the ~60% gate, and the resolver path
the plan required exists as proven prior art (the release-radar script already does
name → MBID resolution in production).

## Results by checklist item

### 1. Library MBID coverage — PASS (92.5%)
40 of 540 artist dirs sampled (seed 17), 1,273 files, **100% readable, zero unreadable dirs**.

| Tag | Coverage |
|---|---|
| artist_mbid | **92.5%** |
| album_mbid | 92.5% |
| release_track_mbid | 92.5% |
| recording_mbid | 0.9% (expected — Picard writes Release Track Id, not Track Id) |
| artist / album / date | 100% |

Formats in sample: 1261 MP3, 12 M4A. (Full library is ~517 artists; sample frames the
number; full-scan Phase 1 will give the exact figure.)

### 2. ListenBrainz — PASS, with one architecture-relevant surprise
Public endpoints (listens, top stats) work without a token. But **listens and stats are
name-based — zero MBIDs** (Pano Scrobbler doesn't attach them). The plan's assumption
that "LB listens carry MBIDs natively" is false *for this user's data*.

**Impact:** the name→MBID resolver is mandatory core infrastructure, not a fallback.
This is a real plan amendment, and it's survivable (see 4).

Token status: not needed for public data; still create one for Phase 2 (incremental
listen sync, private-data robustness).

### 3. Rate-limited MB client — PASS
1.1s spacing + 503/429 backoff + descriptive UA, mirroring the radar script's proven
conventions. Real lookups succeeded.

### 4. End-to-end join — PASS (90.5%)
listens-side (watchlist) × library sample, joined on artist MBID:
- 42 owned artists with MBIDs; **38 already in the watchlist — 90.5% join rate**
- 21 untagged artists in sample; 7 matched to watchlist by name
- MB search resolver demo: 2/4 hits on first attempt (exact-match logic from the radar
  script will improve this)

### 5. SQLite WAL — PASS
Concurrent readers + one writer: no lock errors, readers observed count 0→5000,
integrity_check ok. Schema approach is viable.

## Plan amendments (for the vault planning doc)

1. **Resolver promoted to core layer.** Because LB listens carry no MBIDs, every
   listened-artist join goes through name→MBID resolution. Reuse the radar script's
   approach (MB `artist:"name"` search, exact match first, score ≥80 fallback).
   Caching resolved names is mandatory (MB 1 req/s makes re-resolution expensive).
2. **Artist-credit complexity is real.** Files like "MIKE & Surf Gang" and collab
   strings join to one artist's MBID in tags but appear as multi-name credits in LB.
   Phase 2+ needs a policy for credit splitting before gap analysis is trustworthy.
3. **recording_mbid vs release_track_mbid:** design all track-level joins on
   release_track_mbid (92.5%) and resolve to recordings via MB relationships; do not
   expect recording MBIDs in files.
