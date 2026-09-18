"""Library index CLI (Phase 1): scan / status / artists / albums.

Env config (see README Configuration): MUSIC_LIBRARY_ROOT, MUSIC_DB.
All commands print JSON; exit code 0 on success.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from music_mcp.library import db, reports, scanner

DEFAULT_DB = "library-index.db"


def _db_path(args) -> Path:
    return Path(getattr(args, "db", None) or os.environ.get("MUSIC_DB") or DEFAULT_DB)


def _library_root(args) -> Path:
    root = getattr(args, "root", None) or os.environ.get("MUSIC_LIBRARY_ROOT")
    if not root:
        print(json.dumps({
            "error": "no library root given",
            "hint": 'pass --root, or: export MUSIC_LIBRARY_ROOT="/path/to/music"',
        }))
        sys.exit(1)
    root = Path(root)
    if not root.is_dir():
        print(json.dumps({"error": f"not a directory: {root}", "hint": "is the NAS mounted?"}))
        sys.exit(1)
    return root


_GLOBAL_FLAGS = {"--db", "--root", "--full", "--filter-mbid"}


def _split_global_flags(argv: list[str]) -> tuple[dict[str, list[str]], list[str]]:
    """Pull global flags out of argv wherever they appear (before or after the
    subcommand). Argparse only recognizes them pre-subcommand, but users write
    them after — both should work."""
    pulled: dict[str, list[str]] = {}
    rest: list[str] = []
    i = 0
    while i < len(argv):
        token = argv[i]
        if token in _GLOBAL_FLAGS:
            pulled.setdefault(token, []).append(argv[i + 1])
            i += 2
        elif token == "--full":
            pulled["--full"] = []
            i += 1
        else:
            rest.append(token)
            i += 1
    return pulled, rest


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    pulled, argv = _split_global_flags(argv)

    parser = argparse.ArgumentParser(prog="music_mcp.library", description=__doc__)
    parser.add_argument("--db", default=None, help="cache file (default: $MUSIC_DB or ./library-index.db)")
    parser.add_argument("--root", default=None, help="library root (default: $MUSIC_LIBRARY_ROOT)")
    parser.add_argument("--full", action="store_true", help="scan: ignore mtimes; re-read every file")
    parser.add_argument("--filter-mbid", default=None, help="artists: 'missing' or a specific artist_mbid")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("scan", help="incremental scan of the library into the cache")
    sub.add_parser("status", help="index health + coverage")
    sub.add_parser("artists", help="indexed artists")
    albums = sub.add_parser("albums", help="albums for one artist (mbid or name)")
    albums.add_argument("artist")

    # Reinstate pulled flags at the front so the main parser sees them.
    reinstated: list[str] = []
    for flag, values in pulled.items():
        if not values:
            reinstated.append(flag)
        else:
            reinstated.extend([flag, values[-1]])
    args = parser.parse_args(reinstated + argv)

    conn = db.connect(_db_path(args))
    try:
        if args.command == "scan":
            root = _library_root(args)
            result = scanner.scan_library(root, conn, incremental=not args.full)
        elif args.command == "status":
            result = reports.library_status(conn)
        elif args.command == "artists":
            result = reports.library_artists(conn, filter_mbid=args.filter_mbid)
        elif args.command == "albums":
            result = reports.library_albums(conn, args.artist)
        else:  # pragma: no cover - argparse enforces choices
            parser.error(f"unknown command: {args.command}")
    finally:
        conn.close()

    print(json.dumps(result, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
