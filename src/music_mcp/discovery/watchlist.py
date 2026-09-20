"""Watchlist management: import from the radar JSON, CRUD, radar-format export.

The MCP SQLite `watchlist` table is the MCP-side source of truth. The Friday
cron stays untouched — it keeps reading its own watchlist.json; `export`
writes a radar-shaped file for a future cron migration (see vault planning doc).
"""

from __future__ import annotations

import json
import time
from pathlib import Path


def import_radar_json(conn, path: Path) -> dict:
    """Import (merge) the Friday cron's watchlist.json into the watchlist table."""
    data = json.loads(Path(path).read_text())
    artists = data.get("artists", [])
    now = time.time()
    added = 0
    for a in artists:
        mbid = a.get("mbid")
        name = a.get("name")
        if not mbid or not name:
            continue  # names are not join keys; skip MBID-less entries
        sources = json.dumps(a.get("sources") or [])
        listen_count = int(a.get("listen_count") or 0)
        cur = conn.execute(
            """
            INSERT INTO watchlist (artist_mbid, name, sources, listen_count, added_at)
            VALUES (?,?,?,?,?)
            ON CONFLICT(artist_mbid) DO UPDATE SET
                name = excluded.name,
                sources = excluded.sources,
                listen_count = MAX(watchlist.listen_count, excluded.listen_count)
            """,
            (mbid, name, sources, listen_count, now),
        )
        if cur.rowcount == 1 and conn.total_changes:
            pass  # rowcount on upsert is unreliable; count via SELECT below
        added += 1
    conn.commit()
    total = conn.execute("SELECT COUNT(*) FROM watchlist").fetchone()[0]
    return {"imported": added, "total_in_watchlist": total, "source": str(path)}


def list_watchlist(conn, limit: int = 50, offset: int = 0) -> dict:
    total = conn.execute("SELECT COUNT(*) FROM watchlist").fetchone()[0]
    rows = conn.execute(
        "SELECT artist_mbid, name, sources, listen_count FROM watchlist "
        "ORDER BY listen_count DESC, name LIMIT ? OFFSET ?",
        (limit, offset),
    ).fetchall()
    return {
        "total": total,
        "showing": len(rows),
        "artists": [
            {
                "artist_mbid": r["artist_mbid"],
                "name": r["name"],
                "sources": json.loads(r["sources"]),
                "listen_count": r["listen_count"],
            }
            for r in rows
        ],
    }


def add_artists(conn, artists: list[dict]) -> dict:
    """Add artists: [{mbid, name, sources?, listen_count?}]. Existing rows get
    source/listen-count merged (sources union, max listen count)."""
    now = time.time()
    added, updated = 0, 0
    for a in artists:
        mbid, name = a.get("mbid"), a.get("name")
        if not mbid or not name:
            continue
        sources = json.dumps(a.get("sources") or ["manual"])
        listen_count = int(a.get("listen_count") or 0)
        existing = conn.execute(
            "SELECT sources, listen_count FROM watchlist WHERE artist_mbid = ?", (mbid,)
        ).fetchone()
        if existing is None:
            conn.execute(
                "INSERT INTO watchlist (artist_mbid, name, sources, listen_count, added_at) "
                "VALUES (?,?,?,?,?)",
                (mbid, name, sources, listen_count, now),
            )
            added += 1
        else:
            current_sources = set(json.loads(existing["sources"]))
            new_sources = current_sources | set(json.loads(sources))
            conn.execute(
                "UPDATE watchlist SET name = ?, sources = ?, listen_count = MAX(listen_count, ?) "
                "WHERE artist_mbid = ?",
                (name, json.dumps(sorted(new_sources)), listen_count, mbid),
            )
            updated += 1
    conn.commit()
    return {"added": added, "merged": updated}


def remove_artists(conn, mbids: list[str]) -> dict:
    removed = 0
    for mbid in mbids:
        cur = conn.execute("DELETE FROM watchlist WHERE artist_mbid = ?", (mbid,))
        removed += cur.rowcount
    conn.commit()
    return {"removed": removed}


def export_radar_format(conn, out_path: Path | None = None) -> dict:
    """Write radar-shaped JSON: {created, total_artists, artists: [{mbid, name,
    sources, listen_count}]}. For a future cron migration; the cron is untouched now."""
    rows = conn.execute(
        "SELECT artist_mbid, name, sources, listen_count FROM watchlist ORDER BY name"
    ).fetchall()
    payload = {
        "created": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()),
        "total_artists": len(rows),
        "artists": [
            {
                "mbid": r["artist_mbid"],
                "name": r["name"],
                "sources": json.loads(r["sources"]),
                "listen_count": r["listen_count"],
            }
            for r in rows
        ],
    }
    if out_path:
        out_path = Path(out_path)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(payload, indent=1))
    return {"exported": len(rows), "path": str(out_path) if out_path else None}
