"""Similar-artists (LB labs) + recommendations crossing the graph against the library."""

from __future__ import annotations

import json
from urllib.parse import quote
from urllib.request import Request, urlopen

from music_mcp.listens.credits import normalize_name, owned_name_index

LABS_BASE = "https://labs.api.listenbrainz.org"
# Session-based algorithm from the proven release-radar script
SIMILAR_ALGO = (
    "session_based_days_1825_session_300_contribution_3_threshold_10_limit_100_filter_True_skip_30"
)
UA = "self-hosted-music-mcp/0.1.0 (https://github.com/rriegel/self-hosted-music-mcp)"


def _labs_get(path: str, retries: int = 3) -> list | dict:
    last_exc: Exception | None = None
    for attempt in range(retries):
        try:
            req = Request(f"{LABS_BASE}{path}", headers={"User-Agent": UA, "Accept": "application/json"})
            with urlopen(req, timeout=30) as resp:
                data = json.loads(resp.read())
                # labs returns a bare list for similar-artists
                return data if isinstance(data, list) else data
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            if attempt < retries - 1:
                import time

                time.sleep((attempt + 1) * 2)
    raise RuntimeError(f"labs request failed after {retries} attempts: {path}: {last_exc}")


def similar_artists(artist_mbid: str, limit: int = 25) -> list[dict]:
    """LB labs similar-artists graph for one artist (session-based algorithm)."""
    path = f"/similar-artists/json?artist_mbids={quote(artist_mbid)}&algorithm={SIMILAR_ALGO}"
    rows = _labs_get(path)
    result = []
    for row in rows[:limit]:
        result.append(
            {
                "artist_mbid": row.get("artist_mbid"),
                "name": row.get("name"),
                "score": row.get("score"),
                "reference_mbid": row.get("reference_mbid"),
            }
        )
    return result


def discovery_similar_artists(conn, artist_mbid: str, limit: int = 25, exclude: str = "none") -> dict:
    """One artist's LB similar artists, optionally keeping only acquisition candidates."""
    items = similar_artists(artist_mbid, limit=limit * 2 if exclude == "owned" else limit)
    if exclude == "owned":
        owned = owned_name_index(conn)
        items = [i for i in items if normalize_name(i["name"] or "") not in owned][:limit]
        excluded = "owned artists"
    else:
        excluded = None
    return {
        "artist_mbid": artist_mbid,
        "exclude": excluded,
        "similar": items,
        "count": len(items),
    }


def discovery_recommendations(
    conn, seed_limit: int = 10, per_seed: int = 15, filter_mode: str = "not_in_library",
    min_seed_listens: int = 5, limit: int = 50, seed_mbids: list[str] | None = None,
) -> dict:
    """Recommendations from the LB similar-artists graph crossed against the library.

    Seeds: the user's most-listened artists (listened_artists joined to artists
    by canonical name for MBIDs), or explicit seed_mbids. Each seed contributes
    its similar artists; results are aggregated (sum of scores) and filtered:

    - filter_mode='not_in_library': candidates you don't own (shopping list)
    - filter_mode='in_library_unplayed': candidates you own but never scrobbled
      (rediscovery)
    - filter_mode='all': everything, with owned/unowned labeled

    Note: LB collaborative-filtering recommendations endpoint was 404 at build
    time (probed); this graph-based approach is the working substitute.
    """
    owned = owned_name_index(conn)

    if seed_mbids:
        seeds = [{"artist_mbid": m, "name": None, "listen_count": None} for m in seed_mbids]
    else:
        # top listened artists that HAVE an owned MBID (graph queries need MBIDs)
        seeds = [
            {"artist_mbid": r["artist_mbid"], "name": r["name"], "listen_count": r["listen_count"]}
            for r in conn.execute(
                """
                SELECT a.artist_mbid, a.name, la.listen_count
                FROM listened_artists la
                JOIN artists a ON norm_name(a.name) = norm_name(la.artist_name)
                WHERE a.artist_mbid IS NOT NULL AND la.listen_count >= ?
                ORDER BY la.listen_count DESC LIMIT ?
                """,
                (min_seed_listens, seed_limit),
            ).fetchall()
        ]

    scores: dict[str, dict] = {}
    for seed in seeds:
        try:
            neighbors = similar_artists(seed["artist_mbid"], limit=per_seed)
        except RuntimeError:
            continue  # one seed failing shouldn't kill the batch
        for n in neighbors:
            mbid = n.get("artist_mbid")
            if not mbid or mbid == seed["artist_mbid"]:
                continue
            entry = scores.setdefault(
                mbid,
                {"artist_mbid": mbid, "name": n.get("name"), "score_sum": 0, "via": []},
            )
            entry["score_sum"] += int(n.get("score") or 0)
            if seed["name"]:
                entry["via"].append(seed["name"])

    recs = []
    listened_names = {
        normalize_name(row["artist_name"])
        for row in conn.execute("SELECT artist_name FROM listened_artists WHERE listen_count > 0")
    }
    for entry in scores.values():
        name = entry["name"] or ""
        is_owned = normalize_name(name) in owned
        if filter_mode == "not_in_library" and is_owned:
            continue
        if filter_mode == "in_library_unplayed" and (not is_owned or normalize_name(name) in listened_names):
            continue  # must be owned AND never scrobbled
        if filter_mode == "all":
            pass
        recs.append(
            {
                "artist_mbid": entry["artist_mbid"],
                "name": name,
                "affinity": entry["score_sum"],
                "recommended_via": entry["via"][:3],
                "owned": is_owned,
            }
        )
    recs.sort(key=lambda r: -r["affinity"])
    recs = recs[:limit]

    return {
        "filter_mode": filter_mode,
        "seeds_used": len(seeds),
        "seed_names": [s["name"] for s in seeds if s.get("name")],
        "recommendations": recs,
        "count": len(recs),
    }
