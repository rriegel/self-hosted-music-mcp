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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="music_mcp.library", description=__doc__)
    parser.add_argument("--db", default=None, help="cache file (default: $MUSIC_DB or ./library-index.db)")
    sub = parser.add_subparsers(dest="command", required=True)

    scan = sub.add_parser("scan", help="incremental scan of the library into the cache")
    scan.add_argument("--root", default=None, help="library root (default: $MUSIC_LIBRARY_ROOT)")
    scan.add_argument("--full", action="store_true", help="ignore mtimes; re-read everything")

    sub.add_parser("status", help="index health + coverage")
    artists = sub.add_parser("artists", help="indexed artists")
    artists.add_argument("--filter-mbid", default=None, help="'missing' or a specific artist_mbid")

    albums = sub.add_parser("albums", help="albums for one artist (mbid or name)")
    albums.add_argument("artist")

    args = parser.parse_args(argv)

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
