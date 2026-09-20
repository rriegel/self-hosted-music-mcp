"""discovery CLI (Phase 4): watchlist, similar artists, recommendations,
new releases, playlists."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from music_mcp.args import reinstate, split_global_flags
from music_mcp.discovery import recommend
from music_mcp.discovery import watchlist as wl
from music_mcp.library import db

VALUE_FLAGS = {
    "--db", "--mbid", "--limit", "--offset", "--exclude", "--seed-limit", "--per-seed",
    "--filter", "--min-seed-listens", "--path", "--source",
}
BOOL_FLAGS: set[str] = set()


def _db_path(pulled: dict) -> Path:
    return Path(pulled.get("--db") or os.environ.get("MUSIC_DB") or "library-index.db")


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    pulled, rest = split_global_flags(argv, VALUE_FLAGS, BOOL_FLAGS)

    parser = argparse.ArgumentParser(prog="music_mcp.discovery", description=__doc__)
    parser.add_argument("--db", default=None, help="cache file (default: $MUSIC_DB)")
    parser.add_argument("--mbid", default=None, help="artist MBID (similar)")
    parser.add_argument("--limit", type=int, default=50, help="result cap")
    parser.add_argument("--offset", type=int, default=0, help="watchlist list offset")
    parser.add_argument("--exclude", default="none", choices=["none", "owned"], help="similar: filter")
    parser.add_argument("--seed-limit", type=int, default=10, help="recommendations: top N listened seeds")
    parser.add_argument("--per-seed", type=int, default=15, help="recommendations: neighbors per seed")
    parser.add_argument("--min-seed-listens", type=int, default=5, help="recommendations: seed threshold")
    parser.add_argument("--filter", dest="filter_mode", default="not_in_library",
                        choices=["not_in_library", "in_library_unplayed", "all"],
                        help="recommendations: which candidates to keep")
    parser.add_argument("--path", default=None, help="watchlist import/export file path")
    sub = parser.add_subparsers(dest="command", required=True)

    imp = sub.add_parser("watchlist-import", help="import (merge) the radar watchlist.json")
    imp.add_argument("file", help="path to watchlist.json")
    sub.add_parser("watchlist", help="list watchlist entries")
    add = sub.add_parser("watchlist-add", help="add artists: JSON [{mbid,name}] on stdin or --path file")
    add.add_argument("json_arg", nargs="?", default=None)
    rm = sub.add_parser("watchlist-remove", help="remove artists by comma-separated MBIDs")
    rm.add_argument("mbids")
    exp = sub.add_parser("watchlist-export", help="write radar-shaped JSON (future cron migration)")
    exp.add_argument("file", nargs="?", default=None)

    sub.add_parser("similar", help="LB similar artists for --mbid")
    sub.add_parser("recommendations", help="similar-graph x library recommendations")

    args = parser.parse_args(reinstate(pulled) + rest)

    conn = db.connect(_db_path(pulled))
    try:
        if args.command == "watchlist-import":
            result = wl.import_radar_json(conn, Path(args.file))
        elif args.command == "watchlist":
            result = wl.list_watchlist(conn, limit=args.limit, offset=args.offset)
        elif args.command == "watchlist-add":
            payload = args.json_arg or (Path(args.path).read_text() if args.path else None)
            if not payload:
                print(json.dumps({"error": "no artists given", "hint": "pass JSON or --path"}))
                return 1
            result = wl.add_artists(conn, json.loads(payload))
        elif args.command == "watchlist-remove":
            result = wl.remove_artists(conn, [m.strip() for m in args.mbids.split(",") if m.strip()])
        elif args.command == "watchlist-export":
            result = wl.export_radar_format(conn, Path(args.path or args.file) if (args.path or args.file) else None)
        elif args.command == "similar":
            if not args.mbid:
                print(json.dumps({"error": "--mbid required"}))
                return 1
            result = recommend.discovery_similar_artists(conn, args.mbid, limit=args.limit, exclude=args.exclude)
        elif args.command == "recommendations":
            result = recommend.discovery_recommendations(
                conn, seed_limit=args.seed_limit, per_seed=args.per_seed,
                filter_mode=args.filter_mode, min_seed_listens=args.min_seed_listens, limit=args.limit,
            )
        else:  # pragma: no cover
            parser.error(f"unknown command: {args.command}")
    finally:
        conn.close()

    print(json.dumps(result, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
