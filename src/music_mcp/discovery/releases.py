"""discovery_new_releases: the release radar as an interactive tool, and
discovery_playlist: constraint-based tracklists from your own files.

new_releases joins four sources the Friday cron can't:
- MB release search over a date window (all new releases)
- your library index (owned artists)
- your listens (listened artists, play counts)
- the watchlist table (MCP-owned)

Filters: owned | listened | watchlist | all. Ranking: play_count | affinity | date.
Genre enrichment via release-group tags, cached in mb_cache.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta

from music_mcp.mb.client import MBClient

GENRE_TTL = 30 * 86400


def _iso_days_ago(days: int) -> str:
    return (datetime.now(UTC) - timedelta(days=days)).strftime("%Y-%m-%d")


def _iso_today() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%d")


def _fetch_releases_window(client: MBClient, since: str, until: str, max_pages: int = 8) -> list[dict]:
    """MB release search over a date window, paginated (100/page, like the radar script)."""
    releases: list[dict] = []
    offset = 0
    while offset < max_pages * 100:
        data = client.get(
            "/release",
            {
                "query": f"date:[{since} TO {until}]",
                "limit": 100,
                "offset": offset,
            },
        )
        page = data.get("releases", [])
        if not page:
            break
        releases.extend(page)
        if len(releases) >= data.get("count", 0) or offset + 100 >= data.get("count", 0):
            break
        offset += 100
    return releases


def _owned_and_listened_names(conn) -> tuple[set[str], dict[str, int], dict[str, str]]:
    """(owned names lowercased, listened name → count, listened name → mbid)."""
    owned = set()
    for r in conn.execute("SELECT DISTINCT artist FROM tracks WHERE artist IS NOT NULL"):
        owned.add(r["artist"].lower())
    for r in conn.execute("SELECT DISTINCT name FROM artists WHERE name IS NOT NULL"):
        owned.add(r["name"].lower())

    listened: dict[str, int] = {}
    for r in conn.execute("SELECT artist_name, listen_count FROM listened_artists"):
        listened[r["artist_name"].lower()] = r["listen_count"]

    mbid_by_name: dict[str, str] = {}
    for r in conn.execute("SELECT name, artist_mbid FROM artists WHERE name IS NOT NULL"):
        mbid_by_name[r["name"].lower()] = r["artist_mbid"]
    return owned, listened, mbid_by_name


def _genre_for_release_group(client: MBClient, rg_mbid: str | None) -> list[str]:
    """Release-group genres via tags, cached in mb_cache (30d TTL shared with lookups)."""
    if not rg_mbid:
        return []
    key = f"rg-genres:{rg_mbid}"
    row = client.conn.execute("SELECT response, fetched_at FROM mb_cache WHERE key = ?", (key,)).fetchone()
    if row and time.time() - row["fetched_at"] < GENRE_TTL:
        return json_loads(row["response"])
    try:
        data = client.get(f"/release-group/{rg_mbid}", {"inc": "tags+genres"})
        genres = sorted(
            {t.get("name") for t in (data.get("genres") or data.get("tags") or []) if t.get("name")}
        )[:6]
    except Exception:  # noqa: BLE001 - genre enrichment must never fail the radar
        genres = []
    client.conn.execute(
        "INSERT INTO mb_cache (key, url, response, fetched_at) VALUES (?,?,?,?) "
        "ON CONFLICT(key) DO UPDATE SET response=excluded.response, fetched_at=excluded.fetched_at",
        (key, f"rg-genres:{rg_mbid}", json_dumps(genres), time.time()),
    )
    client.conn.commit()
    return genres


def json_loads(s: str):
    import json

    return json.loads(s)


def json_dumps(obj) -> str:
    import json

    return json.dumps(obj)


def discovery_new_releases(
    conn,
    client: MBClient | None = None,
    filter_mode: str = "watchlist",
    since_days: int = 7,
    rank_by: str = "play_count",
    limit: int = 30,
    with_genres: bool = False,
) -> dict:
    """New releases joined against ownership/listening/watchlist.

    Defaults mirror the Friday cron (watchlist, 7d) but every axis is a knob:
    - filter_mode: 'owned' (artists you have), 'listened' (artists you scrobble),
      'watchlist' (MCP watchlist table), 'all' (everything — huge window caveat)
    - rank_by: 'play_count' (your listens), 'affinity' (listens + watchlist bonus),
      'date' (newest first, cron-like)
    """
    client = client or MBClient(conn)
    since, until = _iso_days_ago(since_days), _iso_today()
    owned, listened, mbid_by_name = _owned_and_listened_names(conn)

    watchlist_names: dict[str, int] = {}
    for r in conn.execute("SELECT name, listen_count FROM watchlist"):
        watchlist_names[r["name"].lower()] = r["listen_count"]

    raw = _fetch_releases_window(client, since, until)
    items: list[dict] = []
    for rel in raw:
        credit = rel.get("artist-credit") or []
        artist_names = [c.get("name") or c.get("artist", {}).get("name", "") for c in credit]
        primary_artist = artist_names[0] if artist_names else ""
        rg = rel.get("release-group") or {}
        # the cron dedupes on release-group; a "release" is one physical edition
        entry = {
            "release_mbid": rel.get("id"),
            "release_group_mbid": rg.get("id"),
            "title": rel.get("title"),
            "artist": primary_artist,
            "artist_mbids": [c.get("artist", {}).get("id") for c in credit if c.get("artist", {}).get("id")],
            "date": rel.get("date") or rel.get("release-group", {}).get("first-release-date"),
            "primary_type": rg.get("primary-type"),
            "country": (rel.get("country") or [None])[0] if rel.get("country") else None,
        }
        # join against the four sources (case-insensitive name matching; any credit part)
        artist_l = primary_artist.lower()
        in_watchlist = artist_l in watchlist_names or any(p.lower() in watchlist_names for p in artist_names)
        play_count = max(
            (listened.get(p.lower(), 0) for p in artist_names), default=0
        )
        watched_count = watchlist_names.get(artist_l, 0)
        entry.update(
            {
                "owned": artist_l in owned or any(p.lower() in owned for p in artist_names),
                "listened": play_count > 0,
                "in_watchlist": in_watchlist,
                "play_count": play_count,
                "watchlist_listen_count": watched_count,
            }
        )

        keep = {
            "owned": entry["owned"],
            "listened": entry["listened"],
            "watchlist": in_watchlist,
            "all": True,
        }[filter_mode]
        if keep:
            items.append(entry)

    # Dedupe: MB search returns every physical edition of a release-group, and
    # pagination can repeat rows across page boundaries. One entry per
    # release_group_mbid (earliest date wins, first-seen on ties); groupless
    # releases dedupe on release_mbid. The old note below CLAIMED this happened
    # — it didn't (2026-09-20 dogfood caught duplicate rows in live output).
    best: dict[tuple[str, str], dict] = {}
    order: list[tuple[str, str]] = []
    for e in items:
        key = ("rg", e["release_group_mbid"]) if e["release_group_mbid"] else ("rel", e["release_mbid"])
        if key not in best:
            best[key] = e
            order.append(key)
        elif (e["date"] or "9999") < (best[key]["date"] or "9999"):
            best[key] = e
    items = [best[k] for k in order]

    if rank_by == "play_count":
        items.sort(key=lambda e: (-(e["play_count"]), e["date"] or "", e["title"] or ""))
    elif rank_by == "affinity":
        items.sort(
            key=lambda e: (
                -(e["play_count"] * 2 + e["watchlist_listen_count"]),
                e["date"] or "",
            )
        )
    else:  # date
        items.sort(key=lambda e: e["date"] or "", reverse=True)
    items = items[:limit]

    if with_genres:
        for e in items:
            e["genres"] = _genre_for_release_group(client, e["release_group_mbid"])

    return {
        "filter_mode": filter_mode,
        "window": {"since": since, "until": until},
        "rank_by": rank_by,
        "candidates_seen": len(raw),
        "releases": items,
        "count": len(items),
        "note": "one entry per release-group (earliest edition); 'all' over a wide "
        "window is thousands of rows.",
    }


def discovery_playlist(
    conn,
    genres: list[str] | None = None,
    artist_mbid: str | None = None,
    listened_within_days: int | None = None,
    not_listened_within_days: int | None = None,
    limit: int = 20,
    seed: int | None = None,
) -> dict:
    """Tracklist from your OWN files matching constraints, m3u-ready (real paths).

    Constraints (all optional, AND-combined):
    - artist_mbid: only this artist's tracks
    - listened_within_days / not_listened_within_days: familiarity filter via
      listened_artists join (recency of last listen)
    """
    import random

    where = ["t.readable = 1"]
    params: list = []
    if artist_mbid:
        where.append("t.artist_mbid = ?")
        params.append(artist_mbid)

    if listened_within_days is not None:
        cutoff = int(time.time()) - listened_within_days * 86400
        where.append(
            "t.artist_mbid IN (SELECT a.artist_mbid FROM artists a "
            "JOIN listened_artists l ON norm_name(l.artist_name) = norm_name(a.name) "
            "WHERE l.last_listen_ts >= ?)"
        )
        params.append(cutoff)
    if not_listened_within_days is not None:
        cutoff = int(time.time()) - not_listened_within_days * 86400
        where.append(
            "(t.artist_mbid IS NULL OR t.artist_mbid NOT IN ("
            "SELECT a2.artist_mbid FROM artists a2 "
            "JOIN listened_artists l2 ON norm_name(l2.artist_name) = norm_name(a2.name) "
            "WHERE l2.last_listen_ts >= ?))"
        )
        params.append(cutoff)

    rows = conn.execute(
        f"""
        SELECT path, artist, album, filename AS title_stub, date, artist_mbid
        FROM tracks t
        WHERE {' AND '.join(where)}
        """,
        params,
    ).fetchall()

    picked = rows
    if seed is not None:
        rng = random.Random(seed)
        picked = rng.sample(rows, min(limit, len(rows)))
    else:
        picked = rows[:limit]

    return {
        "constraints": {
            "artist_mbid": artist_mbid,
            "listened_within_days": listened_within_days,
            "not_listened_within_days": not_listened_within_days,
        },
        "candidates": len(rows),
        "tracks": [
            {
                "path": r["path"],  # real Terra path, m3u-ready
                "artist": r["artist"],
                "album": r["album"],
                "file": r["title_stub"],
                "artist_mbid": r["artist_mbid"],
            }
            for r in picked
        ],
        "count": len(picked),
        "note": "tracks are index rows (file paths); nothing is played or modified",
    }
