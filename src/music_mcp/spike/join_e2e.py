"""Spike: one scripted end-to-end join — listens artist -> MB artist -> owned files.

Uses Phase 0 artifacts:
- library sample output (library_sample.py) for the owned side (files + MBID tags)
- the release radar watchlist (MBID-keyed artists with listen counts) for the listened side
- live MB search (rate-limited, sparingly) to demonstrate resolving artists with no MBID tag

Usage:
  uv run python -m music_mcp.spike.join_e2e [--library-sample PATH] [--watchlist PATH]
      [--mb-resolve N] [--out PATH]
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from music_mcp.spike.mb_check import mb_get

DEFAULT_SAMPLE = "spike_library_sample.json"


def load_owned_artists(sample_path: Path) -> dict:
    """artist_mbid -> {names, files} from the library sample; plus untagged name -> file count."""
    data = json.loads(sample_path.read_text())
    files = data.get("files", [])
    owned: dict[str, dict] = {}
    untagged: dict[str, int] = {}
    for f in files:
        if not f.get("readable"):
            continue
        mbid, name = f.get("artist_mbid"), f.get("artist")
        if mbid:
            entry = owned.setdefault(mbid, {"names": set(), "files": 0})
            entry["files"] += 1
            if name:
                entry["names"].add(name)
        elif name:
            untagged[name] = untagged.get(name, 0) + 1
    for entry in owned.values():
        entry["names"] = sorted(entry["names"])
    return {"by_mbid": owned, "untagged_by_name": untagged}


def load_watchlist(path: Path) -> dict:
    """MBID -> {name, listen_count, sources} from the radar watchlist."""
    data = json.loads(path.read_text())
    return {
        a["mbid"]: {"name": a.get("name"), "listen_count": a.get("listen_count", 0), "sources": a.get("sources", [])}
        for a in data.get("artists", [])
        if a.get("mbid")
    }


def resolve_artist_via_mb(name: str) -> dict:
    """MB search for an artist by name (rate-limited inside mb_get). Returns best candidate."""
    try:
        result = mb_get(f"/artist?query={name}&fmt=json&limit=3")
    except Exception as exc:  # noqa: BLE001 - spike: failure is a finding, not a crash
        return {"query": name, "error": str(exc)}
    artists = result.get("artists", [])
    if not artists:
        return {"query": name, "candidate": None}
    best = artists[0]
    return {"query": name, "candidate": {"mbid": best.get("id"), "name": best.get("name"), "score": best.get("score")}}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--library-sample", default=DEFAULT_SAMPLE)
    parser.add_argument(
        "--watchlist", default=os.environ.get("MUSIC_WATCHLIST"), help="default: $MUSIC_WATCHLIST"
    )
    parser.add_argument("--mb-resolve", type=int, default=3, help="how many untagged artists to resolve via MB")
    parser.add_argument("--out", default=None, help="also write JSON to this path")
    args = parser.parse_args()

    if not args.watchlist:
        print(json.dumps({
            "error": "no watchlist path given",
            "hint": "export MUSIC_WATCHLIST=\"/path/to/watchlist.json\"",
        }))
        return 1

    owned = load_owned_artists(Path(args.library_sample))
    watchlist = load_watchlist(Path(args.watchlist))

    by_mbid = owned["by_mbid"]
    joined = []
    for mbid, entry in by_mbid.items():
        watched = watchlist.get(mbid)
        joined.append(
            {
                "artist_mbid": mbid,
                "tagged_names": entry["names"],
                "files": entry["files"],
                "in_watchlist": watched is not None,
                "listen_count": watched["listen_count"] if watched else None,
                "watchlist_name": watched["name"] if watched else None,
            }
        )

    # Fallback path: untagged files matched to watchlist by name (case-insensitive).
    wl_names = {v["name"].lower(): v for v in watchlist.values() if v.get("name")}
    name_matches = [
        {"tagged_name": name, "files": count, "listen_count": wl_names[name.lower()]["listen_count"]}
        for name, count in owned["untagged_by_name"].items()
        if name.lower() in wl_names
    ]

    # Resolver demo: send a few untagged artist names through MB search.
    resolve_targets = list(owned["untagged_by_name"])[: args.mb_resolve]
    mb_resolutions = [resolve_artist_via_mb(name) for name in resolve_targets]

    n_owned = len(by_mbid)
    n_joined = sum(1 for j in joined if j["in_watchlist"])
    summary = {
        "owned_artists_with_mbid": n_owned,
        "owned_artists_in_watchlist": n_joined,
        "join_rate_pct": round(100 * n_joined / n_owned, 1) if n_owned else 0.0,
        "untagged_artists_in_sample": len(owned["untagged_by_name"]),
        "untagged_matched_by_name": len(name_matches),
        "mb_resolution_attempts": len(mb_resolutions),
        "mb_resolution_hits": sum(1 for r in mb_resolutions if r.get("candidate")),
        "sample_joined": sorted(joined, key=lambda j: -(j["listen_count"] or 0))[:10],
        "sample_name_matches": name_matches[:10],
        "sample_mb_resolutions": mb_resolutions,
    }
    print(json.dumps(summary, indent=1))
    if args.out:
        Path(args.out).write_text(json.dumps(summary, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
