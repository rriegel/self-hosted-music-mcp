"""Listens CLI (Phase 2): sync + reports over the shared music cache.

Env config (see README Configuration): MUSIC_DB, LB_USER, LB_TOKEN.
Global flags accepted before or after the subcommand.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from music_mcp.args import reinstate, split_global_flags
from music_mcp.library import db
from music_mcp.listens import reports
from music_mcp.listens.client import LBClient
from music_mcp.listens.sync import refresh_listened_artists, sync_listens

VALUE_FLAGS = {"--db", "--user", "--limit", "--months", "--days", "--min-listens", "--pages"}
BOOL_FLAGS: set[str] = set()


def _db_path(pulled: dict) -> Path:
    return Path(pulled.get("--db") or os.environ.get("MUSIC_DB") or "library-index.db")


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    pulled, rest = split_global_flags(argv, VALUE_FLAGS, BOOL_FLAGS)

    parser = argparse.ArgumentParser(prog="music_mcp.listens", description=__doc__)
    parser.add_argument("--db", default=None, help="cache file (default: $MUSIC_DB)")
    parser.add_argument("--user", default=None, help="LB username (default: $LB_USER)")
    parser.add_argument("--limit", type=int, default=20, help="row cap for report commands")
    parser.add_argument("--months", type=int, default=6, help="stale window (months)")
    parser.add_argument("--days", type=int, default=90, help="discovery window (days)")
    parser.add_argument("--min-listens", type=int, default=5, help="gap-analysis threshold")
    parser.add_argument("--pages", type=int, default=None, help="sync: hard page cap (bounded syncs)")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("sync", help="incremental listens sync from ListenBrainz")
    sub.add_parser("recent", help="most recent listens")
    sub.add_parser("top", help="top artists by listen count")
    sub.add_parser("gap", help="listened but not owned (shopping list)")
    sub.add_parser("stale", help="owned but not listened recently (rediscovery list)")
    sub.add_parser("discoveries", help="artists first listened within the window")

    args = parser.parse_args(reinstate(pulled) + rest)

    conn = db.connect(_db_path(pulled))
    try:
        if args.command == "sync":
            client = LBClient(user=args.user)
            result = sync_listens(conn, client, stop_after_pages=args.pages)
            result["distinct_artists"] = refresh_listened_artists(conn)
        elif args.command == "recent":
            result = reports.listens_recent(conn, limit=args.limit)
        elif args.command == "top":
            result = reports.listens_top_artists(conn, limit=args.limit)
        elif args.command == "gap":
            result = reports.listens_gap_analysis(conn, min_listens=args.min_listens)
        elif args.command == "stale":
            result = reports.listens_stale_library(conn, months=args.months)
        elif args.command == "discoveries":
            result = reports.listens_new_discoveries(conn, days=args.days)
        else:  # pragma: no cover
            parser.error(f"unknown command: {args.command}")
    finally:
        conn.close()

    print(json.dumps(result, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
