"""MCP server (stdio): exposes the library/listens/mb/discovery handlers as tools.

Design (per the plan's test strategy): the SDK owns JSON-RPC/stdio; handlers are
plain functions returning dicts — this module only wires them as tools. Read-only
by default; watchlist mutations are explicit tools.

Run: uv run python -m music_mcp.server    (stdio; add via `hermes mcp add`)
"""

from __future__ import annotations

import os
from pathlib import Path

from mcp.server.mcpserver import MCPServer

from music_mcp.discovery import recommend, releases
from music_mcp.discovery import watchlist as wl
from music_mcp.library import db, quality, reports, scanner
from music_mcp.listens import reports as listens_reports
from music_mcp.listens.sync import refresh_listened_artists, sync_listens
from music_mcp.mb import resolver
from music_mcp.mb.client import MBClient

mcp = MCPServer(
    "self-hosted-music-mcp",
    instructions=(
        "Read-only knowledge layer over a personal music collection: library index, "
        "ListenBrainz listens, MusicBrainz metadata, joined on MBIDs. Start with "
        "library_status / listens_top; discovery tools for radar/recs/playlists. "
        "File-tag writes never happen; watchlist_manage add/import are the only mutations. "
        "listens_gap_analysis returns decision-ready buckets: true_gaps (shopping list — "
        "confirm via mb_resolve before buying), owned_but_unresolved (NOT purchase targets; "
        "run mb_resolve to store proposals, review them, then mb_apply to bind the MBIDs), "
        "junk_suspects (podcast/radio heuristics). Present each bucket separately with its "
        "suggested_action; never tell the user to buy something in owned_but_unresolved."
    ),
)


def _conn():
    """One connection per tool call (WAL handles concurrency; calls stay independent)."""
    path = Path(os.environ.get("MUSIC_DB") or "library-index.db")
    return db.connect(path)


def _require_tracks(conn) -> None:
    """Guard shared with the CLI: refuse DB mutations/lookups on an empty index."""
    if conn.execute("SELECT COUNT(*) FROM tracks").fetchone()[0] == 0:
        raise ValueError(
            "index has no tracks — run library_scan against your library first "
            "(is MUSIC_DB pointing at your scanned cache?)"
        )


# ---- library tools ---------------------------------------------------------

@mcp.tool()
def library_scan(full: bool = False, limit_artists: int | None = None) -> dict:
    """Incrementally scan the music library into the index. full=True re-reads
    every file (ignores mtimes). Set MUSIC_LIBRARY_ROOT first."""
    conn = _conn()
    try:
        root = os.environ.get("MUSIC_LIBRARY_ROOT")
        if not root:
            raise ValueError("MUSIC_LIBRARY_ROOT not set")
        result = scanner.scan_library(Path(root), conn, incremental=not full)
        if limit_artists:
            result["note"] = f"limit_artists ignored by scan; passed {limit_artists}"
        return result
    finally:
        conn.close()


@mcp.tool()
def library_status() -> dict:
    """Index health: track/artist/album counts, MBID coverage %, formats, last scan."""
    conn = _conn()
    try:
        return reports.library_status(conn)
    finally:
        conn.close()


@mcp.tool()
def library_artists(filter_mbid: str | None = None) -> dict:
    """Indexed artists with counts. filter_mbid='missing' → resolver worklist."""
    conn = _conn()
    try:
        return reports.library_artists(conn, filter_mbid=filter_mbid)
    finally:
        conn.close()


@mcp.tool()
def library_albums(artist: str) -> dict:
    """Albums for one artist by artist_mbid or name (credit-variety tolerant)."""
    conn = _conn()
    try:
        return reports.library_albums(conn, artist)
    finally:
        conn.close()


@mcp.tool()
def library_find_dupes(scope: str = "all") -> dict:
    """Duplicate releases / mislabeled folders / split artist entities.
    scope: all|release_group|title|folder|artist_entities (entities needs MB;
    detects one real artist cataloged under multiple MBIDs)."""

    conn = _conn()
    try:
        client = MBClient(conn) if scope == "artist_entities" else None
        return quality.library_find_dupes(conn, scope=scope, client=client)
    finally:
        conn.close()


@mcp.tool()
def library_quality_report(min_bitrate: int = 192000) -> dict:
    """Tag-quality report: missing MBID %, credit variants, low-bitrate tracks."""
    conn = _conn()
    try:
        return quality.library_quality_report(conn, min_bitrate=min_bitrate)
    finally:
        conn.close()


# ---- listens tools ---------------------------------------------------------

@mcp.tool()
def listens_sync(pages: int | None = None) -> dict:
    """Incremental ListenBrainz listens sync (needs LB_USER; optional LB_TOKEN)."""
    conn = _conn()
    try:
        from music_mcp.listens.client import LBClient

        result = sync_listens(conn, LBClient(), stop_after_pages=pages)
        result["distinct_artists"] = refresh_listened_artists(conn)
        return result
    finally:
        conn.close()


@mcp.tool()
def listens_recent(limit: int = 20) -> dict:
    """Most recent listens."""
    conn = _conn()
    try:
        return listens_reports.listens_recent(conn, limit=limit)
    finally:
        conn.close()


@mcp.tool()
def listens_top(limit: int = 20) -> dict:
    """Top artists by listen count."""
    conn = _conn()
    try:
        return listens_reports.listens_top_artists(conn, limit=limit)
    finally:
        conn.close()


@mcp.tool()
def listens_gap_analysis(min_listens: int = 5) -> dict:
    """Listened but maybe-not-owned, bucketed: true_gaps (shopping list),
    owned_but_unresolved (tag/alias mismatch — fix via mb_resolve + mb_apply,
    do NOT buy), junk_suspects (podcasts/radio). Each item carries a
    suggested_action; surface them instead of re-deriving with raw SQL."""
    conn = _conn()
    try:
        return listens_reports.listens_gap_analysis(conn, min_listens=min_listens)
    finally:
        conn.close()


@mcp.tool()
def listens_stale_library(months: int = 6) -> dict:
    """Owned artists with no recent listens (rediscovery), ranked by past plays."""
    conn = _conn()
    try:
        return listens_reports.listens_stale_library(conn, months=months)
    finally:
        conn.close()


@mcp.tool()
def listens_new_discoveries(days: int = 90) -> dict:
    """Artists whose first-ever listen falls inside the window."""
    conn = _conn()
    try:
        return listens_reports.listens_new_discoveries(conn, days=days)
    finally:
        conn.close()


# ---- MusicBrainz tools -----------------------------------------------------

@mcp.tool()
def mb_artist(artist_mbid: str) -> dict:
    """MusicBrainz artist metadata (cached; MB ≈ immutable)."""
    conn = _conn()
    try:
        from music_mcp.mb.reports import mb_artist

        return mb_artist(MBClient(conn), artist_mbid)
    finally:
        conn.close()


@mcp.tool()
def mb_artist_releases(artist_mbid: str) -> dict:
    """Official album/EP release-groups for an artist, date-sorted (cached)."""
    conn = _conn()
    try:
        from music_mcp.mb.reports import mb_artist_releases

        return mb_artist_releases(MBClient(conn), artist_mbid)
    finally:
        conn.close()


@mcp.tool()
def mb_search(query: str, limit: int = 5) -> dict:
    """Alias-aware MusicBrainz artist search (handles renamed artists)."""
    conn = _conn()
    try:
        from music_mcp.mb.reports import mb_search

        return mb_search(MBClient(conn), query, limit=limit)
    finally:
        conn.close()


@mcp.tool()
def mb_resolve(max_artists: int = 25) -> dict:
    """Propose MBIDs for untagged library artists (read-only; stores proposals)."""
    conn = _conn()
    try:
        _require_tracks(conn)
        return resolver.propose(MBClient(conn), conn, max_artists=max_artists)
    finally:
        conn.close()


@mcp.tool()
def mb_apply(
    action: str = "apply", batch_size: int = 10,
    from_mbid: str | None = None, into_mbid: str | None = None,
) -> dict:
    """Apply resolver proposals into the index (action='apply'; never file tags),
    or merge a duplicate MB entity (action='retarget', needs from_mbid+into_mbid;
    moves albums/tracks to the canonical entity, cache-only)."""
    conn = _conn()
    try:
        _require_tracks(conn)
        if action == "retarget":
            if not from_mbid or not into_mbid:
                raise ValueError("retarget requires from_mbid and into_mbid")
            return resolver.retarget(conn, from_mbid=from_mbid, into_mbid=into_mbid)
        if action != "apply":
            raise ValueError(f"unknown action: {action}")
        return resolver.commit(MBClient(conn), conn, batch_size=batch_size)
    finally:
        conn.close()


@mcp.tool()
def mb_review(
    action: str = "list", status: str | None = None,
    folder_artist: str | None = None, mbid: str | None = None, name: str | None = None,
) -> dict:
    """Review resolver proposals. action: list (all proposals + evidence) |
    set (correct one after MB verification: folder_artist + mbid[, name]) |
    reject (folder_artist). set marks it manually-verified so commit applies it."""
    conn = _conn()
    try:
        _require_tracks(conn)
        if action == "list":
            return resolver.review_list(conn, status=status)
        if action == "set":
            if not folder_artist or not mbid:
                raise ValueError("set requires folder_artist and mbid")
            return resolver.review_set(conn, folder_artist=folder_artist, mbid=mbid, name=name)
        if action == "reject":
            if not folder_artist:
                raise ValueError("reject requires folder_artist")
            return resolver.review_reject(conn, folder_artist=folder_artist)
        raise ValueError(f"unknown action: {action}")
    finally:
        conn.close()


# ---- discovery tools -------------------------------------------------------

@mcp.tool()
def discovery_similar_artists(artist_mbid: str, limit: int = 25, exclude: str = "none") -> dict:
    """LB similar-artists graph for one artist; exclude='owned' → acquisition candidates."""
    conn = _conn()
    try:
        return recommend.discovery_similar_artists(conn, artist_mbid, limit=limit, exclude=exclude)
    finally:
        conn.close()


@mcp.tool()
def discovery_recommendations(
    filter_mode: str = "not_in_library", seed_limit: int = 10, limit: int = 50
) -> dict:
    """Recommendations: LB similar-graph x library. filter_mode: not_in_library |
    in_library_unplayed | all."""
    conn = _conn()
    try:
        _require_tracks(conn)
        return recommend.discovery_recommendations(
            conn, seed_limit=seed_limit, filter_mode=filter_mode, limit=limit
        )
    finally:
        conn.close()


@mcp.tool()
def discovery_new_releases(
    filter_mode: str = "watchlist", since_days: int = 7, rank_by: str = "play_count",
    limit: int = 30, with_genres: bool = False,
) -> dict:
    """Release radar as a tool: MB releases in the window joined to owned/listened/
    watchlist. filter_mode: owned|listened|watchlist|all. rank_by: play_count|affinity|date."""
    conn = _conn()
    try:
        _require_tracks(conn)
        return releases.discovery_new_releases(
            conn, MBClient(conn), filter_mode=filter_mode, since_days=since_days,
            rank_by=rank_by, limit=limit, with_genres=with_genres,
        )
    finally:
        conn.close()


@mcp.tool()
def discovery_playlist(
    artist_mbid: str | None = None,
    listened_within_days: int | None = None,
    not_listened_within_days: int | None = None,
    limit: int = 20,
    seed: int | None = None,
) -> dict:
    """Tracklist from YOUR files matching constraints (real paths, m3u-ready).
    Familiarity filters use listen recency. Nothing is played or modified."""
    conn = _conn()
    try:
        _require_tracks(conn)
        return releases.discovery_playlist(
            conn, artist_mbid=artist_mbid, listened_within_days=listened_within_days,
            not_listened_within_days=not_listened_within_days, limit=limit, seed=seed,
        )
    finally:
        conn.close()


@mcp.tool()
def watchlist_manage(
    action: str, mbids: list[str] | None = None, artists: list[dict] | None = None,
    path: str | None = None, limit: int = 50,
) -> dict:
    """Watchlist CRUD. action: list | add | remove | import | export.
    add/import are the only mutating actions (explicit by design)."""
    conn = _conn()
    try:
        if action == "list":
            return wl.list_watchlist(conn, limit=limit)
        if action == "add":
            if not artists:
                raise ValueError("add requires artists=[{mbid,name,...}]")
            return wl.add_artists(conn, artists)
        if action == "remove":
            if not mbids:
                raise ValueError("remove requires mbids=[...]")
            return wl.remove_artists(conn, mbids)
        if action == "import":
            if not path:
                raise ValueError("import requires path= (radar watchlist.json)")
            return wl.import_radar_json(conn, Path(path))
        if action == "export":
            return wl.export_radar_format(conn, Path(path) if path else None)
        raise ValueError(f"unknown action: {action}")
    finally:
        conn.close()


def main() -> None:
    """stdio entrypoint."""
    mcp.run()


if __name__ == "__main__":
    main()
