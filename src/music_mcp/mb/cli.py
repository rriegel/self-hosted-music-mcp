"""music_mcp.mb CLI (Phase 3): MusicBrainz lookups + library resolver.

Env config: MUSIC_DB (shared cache). Global flags accepted anywhere.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from music_mcp.args import reinstate, split_global_flags
from music_mcp.library import db
from music_mcp.mb import resolver
from music_mcp.mb.client import MBClient
from music_mcp.mb.reports import mb_artist, mb_artist_releases, mb_lookup, mb_search

VALUE_FLAGS = {"--db", "--mbid", "--name", "--entity", "--query", "--limit", "--max", "--batch-size"}
BOOL_FLAGS: set[str] = set()


def _db_path(pulled: dict) -> Path:
    return Path(pulled.get("--db") or os.environ.get("MUSIC_DB") or "library-index.db")


def _guard_empty_index(conn, db_path: Path, command: str) -> None:
    """Refuse resolve/apply against an index with no tracks — a fresh/empty DB
    silently produces happy-looking empty results (real Phase 3 incident: the
    CLI default created ./library-index.db and resolve 'succeeded' on nothing)."""
    tracks = conn.execute("SELECT COUNT(*) FROM tracks").fetchone()[0]
    if tracks == 0:
        print(json.dumps({
            "error": f"no tracks in {db_path} — refusing to {command} against an empty index",
            "hint": (
                "is MUSIC_DB pointing at your scanned cache? "
                "scan first: uv run python -m music_mcp.library scan "
                f"--db {db_path}"
            ),
        }))
        sys.exit(1)


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    pulled, rest = split_global_flags(argv, VALUE_FLAGS, BOOL_FLAGS)

    parser = argparse.ArgumentParser(prog="music_mcp.mb", description=__doc__)
    parser.add_argument("--db", default=None, help="cache file (default: $MUSIC_DB)")
    parser.add_argument("--mbid", default=None, help="an MBID (lookup)")
    parser.add_argument("--name", default=None, help="artist name (resolve/search)")
    parser.add_argument("--entity", default="artist", help="entity type (lookup/search)")
    parser.add_argument("--query", default=None, help="search query (search)")
    parser.add_argument("--limit", type=int, default=5, help="search result cap")
    parser.add_argument("--max", type=int, default=25, help="resolve: cap artists processed (MB 1 req/s!)")
    parser.add_argument("--batch-size", type=int, default=10, help="resolve commit: batch size")
    sub = parser.add_subparsers(dest="command", required=True)

    artist = sub.add_parser("artist", help="artist metadata by MBID")
    artist.add_argument("mbid")
    releases = sub.add_parser("releases", help="official album/EP release-groups for an artist")
    releases.add_argument("mbid")
    sub.add_parser("search", help="search artists by --query")
    sub.add_parser("lookup", help="lookup any entity by --mbid and --entity")

    sub.add_parser("resolve", help="propose MBIDs for untagged library artists (read-only)")
    sub.add_parser("apply", help="write accepted resolutions into the index (no file tag changes)")

    args = parser.parse_args(reinstate(pulled) + rest)

    db_path = _db_path(pulled)
    conn = db.connect(db_path)
    try:
        if args.command == "artist":
            result = mb_artist(MBClient(conn), args.mbid)
        elif args.command == "releases":
            result = mb_artist_releases(MBClient(conn), args.mbid)
        elif args.command == "search":
            result = mb_search(MBClient(conn), args.query or args.name or "", limit=args.limit)
        elif args.command == "lookup":
            result = mb_lookup(MBClient(conn), args.mbid or "", args.entity)
        elif args.command == "resolve":
            _guard_empty_index(conn, db_path, command="resolve")
            result = resolver.propose(MBClient(conn), conn, max_artists=args.max)
        elif args.command == "apply":
            _guard_empty_index(conn, db_path, command="apply")
            result = resolver.commit(MBClient(conn), conn, batch_size=args.batch_size)
        else:  # pragma: no cover
            parser.error(f"unknown command: {args.command}")
    finally:
        conn.close()

    print(json.dumps(result, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
