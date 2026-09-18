"""Spike: sample the Terra library with mutagen, measure real MBID coverage.

Read-only. Walks a sample of artist directories, reads tags, reports:
- MBID coverage % (musicbrainz_artistid / musicbrainz_albumid / musicbrainz_releasetrackid)
- basic tag presence (artist/album/date)
- format distribution

Usage: uv run python -m music_mcp.spike.library_sample [SAMPLE_DIR] [--max-artists N]
Writes findings JSON to stdout.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import sys
from collections import Counter
from pathlib import Path

from music_mcp.library.tags import AUDIO_SUFFIXES, BASIC_TAGS, MBID_TAGS, read_tags


def sample_library(root: Path, max_artists: int, seed: int = 17) -> dict:
    artist_dirs = sorted(d for d in root.iterdir() if d.is_dir())
    rng = random.Random(seed)
    sampled = artist_dirs if len(artist_dirs) <= max_artists else rng.sample(artist_dirs, max_artists)

    files: list[dict] = []
    unreadable_dirs: list[str] = []
    for artist_dir in sorted(sampled):
        try:
            found = sorted(p for p in artist_dir.rglob("*") if p.suffix.lower() in AUDIO_SUFFIXES)
        except OSError as exc:
            unreadable_dirs.append(f"{artist_dir.name}: {exc}")
            continue
        for path in found:
            files.append(read_tags(path))

    total = len(files)
    readable = [f for f in files if f["readable"]]

    def coverage(field: str) -> float:
        return round(100 * sum(1 for f in readable if f.get(field)) / len(readable), 1) if readable else 0.0

    summary = {
        "root": str(root),
        "artist_dirs_in_root": len(artist_dirs),
        "artist_dirs_sampled": len(sampled),
        "files_seen": total,
        "files_readable": len(readable),
        "unreadable_dirs": unreadable_dirs,
        "coverage_pct": {field: coverage(field) for field in MBID_TAGS},
        "basic_tag_pct": {field: coverage(field) for field in BASIC_TAGS},
        "formats": dict(Counter(f["format"] for f in readable)),
    }
    return {"summary": summary, "files": files}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "sample_dir", nargs="?", default=None, help="library root (default: $MUSIC_LIBRARY_ROOT)"
    )
    parser.add_argument("--max-artists", type=int, default=40)
    parser.add_argument("--seed", type=int, default=17)
    parser.add_argument("--out", type=Path, default=None, help="also write full JSON to this path")
    args = parser.parse_args()

    root_specified = args.sample_dir or os.environ.get("MUSIC_LIBRARY_ROOT")
    if not root_specified:
        print(json.dumps({
            "error": "no library root given",
            "hint": "pass it as an argument, or: export MUSIC_LIBRARY_ROOT=\"/path/to/music\"",
        }))
        return 1
    root = Path(root_specified)
    if not root.is_dir():
        print(json.dumps({
            "error": f"not a directory: {root}",
            "hint": "is the NAS mounted? mount points differ per machine (host vs container)",
        }))
        return 1
    result = sample_library(root, args.max_artists, seed=args.seed)
    if args.out:
        args.out.write_text(json.dumps(result, indent=1))
    print(json.dumps(result["summary"], indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
