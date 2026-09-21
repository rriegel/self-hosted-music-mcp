"""Listen/library reports: recent, top, gap analysis, stale library, discoveries.

All joins run through listened_artists (LB credit string) × library tracks.
Credit strings are matched to library artist MBIDs by exact name (the library's
canonical names are already credit-normalized); unmatched LB names are reported
as unresolved — that's the Phase 3 name→MBID resolver's input, never silent loss.
"""

from __future__ import annotations

import re
import sqlite3
import time

from music_mcp.listens.credits import (
    normalize_name,
    owned_name_index,
    resolve_lb_credit,
    resolver_mbid_index,
)


def listens_recent(conn: sqlite3.Connection, limit: int = 20) -> dict:
    rows = conn.execute(
        """
        SELECT ts, artist_name, track_name, release_name
        FROM listens ORDER BY ts DESC LIMIT ?
        """,
        (limit,),
    ).fetchall()
    return {
        "listens": [
            {
                "ts": r["ts"],
                "artist": r["artist_name"],
                "track": r["track_name"],
                "release": r["release_name"],
            }
            for r in rows
        ],
        "count": len(rows),
    }


def listens_top_artists(conn: sqlite3.Connection, limit: int = 20) -> dict:
    rows = conn.execute(
        """
        SELECT artist_name, listen_count, first_listen_ts, last_listen_ts
        FROM listened_artists ORDER BY listen_count DESC LIMIT ?
        """,
        (limit,),
    ).fetchall()
    return {
        "artists": [dict(r) for r in rows],
        "count": len(rows),
        "total_distinct_artists": conn.execute(
            "SELECT COUNT(*) FROM listened_artists"
        ).fetchone()[0],
    }


_JUNK_PATTERNS = re.compile(
    r"(?i)\b(?:up first|npr|podcast|radio|fm\b|am\b|public radio|\.org|\.com)\b"
)


def _junk_reason(artist_name: str) -> str | None:
    """Heuristic junk classifier for non-music scrobbles (documented limits)."""
    if _JUNK_PATTERNS.search(artist_name):
        return "podcast/radio-pattern name (station or show in the artist field)"
    if " - " in artist_name:
        return "contains ' - ' (likely 'Station - Track' radio scrobble layout)"
    return None


def listens_gap_analysis(conn: sqlite3.Connection, min_listens: int = 5, limit: int = 100) -> dict:
    """Listened-but-maybe-not-owned, sorted into decision-ready buckets.

    Tiers (first match wins):
    1. normalized string match → owned (unicode/case/punctuation fold;
       ensemble-suffix subset: 'Lelio Luttazzi' ↔ 'Lelio Luttazzi Trio')
    2. confident resolver proposal (mb_resolutions: exact/case-insensitive,
       proposed or applied) → owned_but_unresolved; suggested_action names the
       mb_resolve → review → mb_apply workflow
    3. junk heuristics (podcast/radio/station patterns) → junk_suspects
    4. everything else → true_gaps (the shopping list)

    高中正義 vs Masayoshi Takanaka-class mismatches land in bucket 2 only after
    mb_resolve has proposed the pair — string tiers never transliterate.
    """
    owned = owned_name_index(conn)
    proposals = resolver_mbid_index(conn)
    total = conn.execute("SELECT COUNT(*) FROM listened_artists").fetchone()[0]

    buckets: dict[str, list[dict]] = {
        "true_gaps": [],
        "owned_but_unresolved": [],
        "junk_suspects": [],
    }
    owned_count = 0
    for row in conn.execute(
        "SELECT artist_name, listen_count, last_listen_ts FROM listened_artists ORDER BY listen_count DESC"
    ):
        name, listens = row["artist_name"], row["listen_count"]
        verdict = resolve_lb_credit(name, owned)
        if verdict["owned"]:
            owned_count += 1
            continue
        if listens < min_listens:
            continue

        base = {"artist": name, "listens": listens, "last_listen_ts": row["last_listen_ts"]}
        proposed_mbid = proposals.get(normalize_name(name))
        if proposed_mbid:
            buckets["owned_but_unresolved"].append(
                {
                    **base,
                    "proposed_mbid": proposed_mbid,
                    "reason": "library match missed by string tiers; resolver proposal exists",
                    "suggested_action": (
                        f"run mb_resolve then mb_apply to bind '{name}' to {proposed_mbid}; "
                        "it is almost certainly already owned"
                    ),
                }
            )
            continue
        if (reason := _junk_reason(name)) is not None:
            buckets["junk_suspects"].append({**base, "reason": reason})
            continue
        buckets["true_gaps"].append(
            {
                **base,
                "reason": "no owned-name match at any tier; no resolver proposal",
                "suggested_action": (
                    f"run mb_resolve for '{name}' to confirm it is genuinely unowned "
                    "before buying"
                ),
            }
        )
        if sum(len(v) for v in buckets.values()) >= limit:
            break

    return {
        "criterion": (
            f"listens >= {min_listens}, bucketed: normalized string match → owned; "
            "confident resolver proposal → owned_but_unresolved; podcast/radio "
            "heuristics → junk_suspects; remainder → true_gaps"
        ),
        "listened_artists_total": total,
        "matched_owned": owned_count,
        **buckets,
        "count": sum(len(v) for v in buckets.values()),
        "note": (
            "owned_but_unresolved items are NOT purchase targets — run mb_resolve/mb_apply "
            "first. junk_suspects are heuristic (station scrobbles, podcasts); true_gaps is "
            "the shopping list."
        ),
    }


def listens_stale_library(conn: sqlite3.Connection, months: int = 6) -> dict:
    """Owned artists with zero listens in the window, ranked by past play intensity."""
    cutoff = int(time.time()) - months * 30 * 86400
    rows = conn.execute(
        """
        SELECT a.artist_mbid, a.name,
               COUNT(DISTINCT t.parent_folder) AS owned_albums,
               MAX(l.last_listen_ts) AS last_listen_ts,
               SUM(l.listen_count) AS total_listens
        FROM artists a
        JOIN tracks t ON t.artist_mbid = a.artist_mbid
        LEFT JOIN listened_artists l ON norm_name(l.artist_name) = norm_name(a.name)
        WHERE a.name IS NOT NULL
          AND (l.last_listen_ts IS NULL OR l.last_listen_ts < ?)
        GROUP BY a.artist_mbid, a.name
        ORDER BY total_listens DESC NULLS LAST, a.name
        LIMIT 100
        """,
        (cutoff,),
    ).fetchall()
    # SQLite lacks NULLS LAST on older versions; emulate in python to be safe.
    ordered = sorted(
        (dict(r) for r in rows),
        key=lambda r: (r["total_listens"] is None, -(r["total_listens"] or 0), r["name"]),
    )
    return {
        "criterion": f"owned artists with no listens in the last {months} months",
        "stale": ordered[:100],
        "count": len(ordered),
    }


def listens_new_discoveries(conn: sqlite3.Connection, days: int = 90) -> dict:
    """Artists whose first-ever listen falls inside the window (discovery rate)."""
    cutoff = int(time.time()) - days * 86400
    rows = conn.execute(
        """
        SELECT artist_name, listen_count, first_listen_ts
        FROM listened_artists
        WHERE first_listen_ts >= ?
        ORDER BY first_listen_ts DESC
        LIMIT 100
        """,
        (cutoff,),
    ).fetchall()
    return {
        "criterion": f"first listen within the last {days} days",
        "discovered": [dict(r) for r in rows],
        "count": len(rows),
    }
