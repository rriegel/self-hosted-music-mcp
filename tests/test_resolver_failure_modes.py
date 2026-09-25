"""fix/resolver-failure-modes: regressions for the six 2026-09-21 dogfood failures.

Real incidents (mb_resolve review wave, 25 considered → 23 stored):
1. 'Mos Def' resolved to 'The YMD' (alias-substring beat the canonical artist;
   Yasiin Bey carried the exact alias 'Mos Def' at a lower MB score)
2. Collab folders pinned onto ONE member by MB score ('Mos Def;MF DOOM' →
   MF DOOM, 'MIKE;Tony Seltzer' → Tony Seltzer)
3. Two folder artists resolved to the SAME MBID (Larry June and Armand Hammer
   both latched onto The Alchemist)
4. Prefix matches labeled 'exact' ('Ama' → 'Ama Lou')
5. Foreign-script names degraded to 'fuzzy' (kanji/full-width folder names)
6. Counters off: '25 considered' printed 50 proposals (stale rows included);
   no-match artists never named
"""

from __future__ import annotations

import sqlite3
from collections.abc import Generator

import pytest

from music_mcp.library import db as db_mod
from music_mcp.mb import resolver
from music_mcp.mb.client import MBClient, _has_credit_separator, _prefer_name_or_alias_match

YASIIN_BEY = "1d9a1b16-5c58-4c1d-b2a4-9f5f5f5f5f01"
THE_YMD = "9c9c9c9c-1111-4c1d-b2a4-000000000001"
MF_DOOM = "4d6a50a1-33b2-4f31-a6c4-000000000002"
ALCHEMIST = "c8b03b58-8c2b-4a5c-b1a5-000000000003"


@pytest.fixture()
def cache(tmp_path) -> Generator[sqlite3.Connection]:
    conn = db_connect(tmp_path / "t.db")
    yield conn
    conn.close()


def db_connect(path):
    return db_mod.connect(path)


def _untagged_folder(conn, folder: str, artist: str, tracks: list[str], album: str | None = None):
    conn.execute(
        "INSERT OR IGNORE INTO albums (album_mbid, artist_mbid, title, folder, date, first_seen, last_seen) "
        "VALUES (?,?,?,?,?,?,?)",
        (None, None, album, f"Lib/{album or tracks[0]}", None, "t", "t"),
    )
    for t in tracks:
        conn.execute(
            "INSERT OR IGNORE INTO tracks (path, parent_folder, filename, suffix, artist, "
            "readable, mtime, size, first_seen, last_seen) VALUES (?,?,?,?,?,1,0,0,'t','t')",
            (f"Lib/{t}", f"Lib/{album or tracks[0]}", t, ".mp3", artist),
        )


class FakeMB:
    """Duck-typed MBClient: canned search + release-groups, records calls."""

    def __init__(self, search: dict[str, list[dict]], rgs: dict[str, list[dict]] | None = None):
        self._search = search
        self._rgs = rgs or {}
        self.calls: list[str] = []

    def search_artist(self, name, limit=3):
        self.calls.append(name)
        return {"artists": self._search.get(name, [])}

    def release_groups(self, mbid):
        return self._rgs.get(mbid, [])


# ── 1. alias/name equality outranks MB score ────────────────────────────────


def test_client_prefers_alias_equality_over_score():
    """Real MB payload for 'Mos Def': The YMD scored 100 via alias 'Yah Mos
    Def' substring; Yasiin Bey scored 76 WITH the exact alias 'Mos Def'.
    The true artist must come back first."""
    payload = [
        {"id": THE_YMD, "name": "The YMD", "score": 100,
         "aliases": [{"name": "The Yah Mos Def"}, {"name": "Yah Mos Def"}]},
        {"id": "black-star", "name": "Black Star", "score": 87,
         "aliases": [{"name": "Talib Kweli & Mos Def"}]},
        {"id": YASIIN_BEY, "name": "Yasiin Bey", "score": 76,
         "aliases": [{"name": "Mos Def"}, {"name": "Dante T. Smith"}]},
    ]
    ranked = _prefer_name_or_alias_match(payload, "Mos Def")
    assert ranked[0]["id"] == YASIIN_BEY


def test_client_prefers_name_equality_over_alias_and_score():
    """Stored-name equality outranks alias equality, which outranks score."""
    payload = [
        {"id": "alias-hit", "name": "Other", "score": 100, "aliases": [{"name": "Target"}]},
        {"id": "name-hit", "name": "Target", "score": 40, "aliases": []},
    ]
    ranked = _prefer_name_or_alias_match(payload, "target")
    assert ranked[0]["id"] == "name-hit"


def test_client_search_routes_true_artist_first_for_mos_def():
    """End-to-end through MBClient.search_artist with stubbed raw responses:
    field query empty (renamed artist), plain query has the noise."""
    client = MBClient(db_mod.connect(":memory:"))
    field: dict = {"artists": []}
    plain = {"artists": [
        {"id": THE_YMD, "name": "The YMD", "score": 100,
         "aliases": [{"name": "The Yah Mos Def"}]},
        {"id": YASIIN_BEY, "name": "Yasiin Bey", "score": 76,
         "aliases": [{"name": "Mos Def"}]},
    ]}
    client._search_raw = lambda query, limit: field if query.startswith("artist:") else plain
    result = client.search_artist("Mos Def", limit=3)
    assert result["artists"][0]["id"] == YASIIN_BEY


def test_client_drops_concatenated_credit_entities():
    """MB indexes collab credits as their own artist ('Mike & The Mechanics');
    a single-name query must not return them above the real artist. Aliases
    are NOT filtered — real artists carry credit-style aliases."""
    payload = [
        {"id": "supergroup", "name": "Mike & The Mechanics", "score": 100, "aliases": []},
        {"id": "credit-alias", "name": "Duo", "score": 80, "aliases": [{"name": "Mike, The Voice"}]},
        {"id": "real-mike", "name": "Mike", "score": 55, "aliases": []},
    ]
    assert _has_credit_separator("Mike & The Mechanics") is True
    assert _has_credit_separator("Mos Def;MF DOOM") is True
    assert _has_credit_separator("&!") is False  # bare name: separator-safe
    client = MBClient(db_mod.connect(":memory:"))
    client._search_raw = lambda query, limit: {"artists": [] if query.startswith("artist:") else payload}
    result = client.search_artist("Mike", limit=5)
    ids = [a["id"] for a in result["artists"]]
    assert "supergroup" not in ids
    assert "credit-alias" in ids  # alias filtering deliberately not applied
    assert ids[0] == "real-mike"  # name equality wins regardless of score


# ── 2. collab split-credits: whole-credit seed loses to part-exact ─────────


def test_propose_collab_folder_prefers_part_exact_candidate():
    """'Mos Def;MF DOOM' with the whole-credit search returning MF DOOM as a
    SUBSTRING hit must not pin the folder onto MF DOOM; the split-part search
    finds Mos Def (Yasiin Bey) by alias and the collab folder lands in
    needs_review — never silently applied."""
    conn = db_connect(":memory:")
    _untagged_folder(conn, "Mos Def;MF DOOM", "Mos Def;MF DOOM", ["01 nine.mp3"], album="Nine")
    conn.commit()
    client = FakeMB(
        search={
            "Mos Def;MF DOOM": [{"id": MF_DOOM, "name": "MF DOOM", "score": 100}],  # substring hit
            "Mos Def": [{"id": YASIIN_BEY, "name": "Yasiin Bey", "score": 76,
                         "aliases": [{"name": "Mos Def"}]}],
        },
        rgs={YASIIN_BEY: [{"title": "Black on Both Sides"}]},
    )
    result = resolver.propose(client, conn, max_artists=5)
    row = conn.execute(
        "SELECT proposed_mbid, confidence, status, evidence FROM mb_resolutions "
        "WHERE folder_artist = 'Mos Def;MF DOOM'"
    ).fetchone()
    # part-tier candidate beats the whole-credit substring hit
    assert row["proposed_mbid"] == YASIIN_BEY
    assert row["confidence"] == "fuzzy"  # part/alias matches never claim 'exact'
    assert row["status"] == "needs_review"
    assert "part match ('Mos Def')" in (row["evidence"] or "")
    assert any(p["artist"] == "Mos Def;MF DOOM" and p["status"] == "needs_review" for p in result["proposals"])
    conn.close()


def test_propose_collab_member_exact_stays_needs_review():
    """A collab folder whose member IS found name-exact ('Karriem Riggins')
    resolves to that member but as needs_review — two artists share the
    folder, so it must not auto-apply."""
    conn = db_connect(":memory:")
    _untagged_folder(conn, "GENA;Liv.E;Karriem Riggins", "GENA;Liv.E;Karriem Riggins", ["01 x.mp3"])
    conn.commit()
    client = FakeMB(
        search={
            "GENA;Liv.E;Karriem Riggins": [{"id": "kr", "name": "Karriem Riggins", "score": 100}],
            "GENA": [],
            "Liv.E": [],
            "Karriem Riggins": [{"id": "kr", "name": "Karriem Riggins", "score": 100}],
        },
    )
    resolver.propose(client, conn, max_artists=5)
    row = conn.execute(
        "SELECT proposed_mbid, confidence, status FROM mb_resolutions WHERE folder_artist LIKE 'GENA%'"
    ).fetchone()
    assert row["proposed_mbid"] == "kr"
    assert row["confidence"] == "fuzzy"
    assert row["status"] == "needs_review"
    conn.close()


def test_propose_single_name_folder_breaks_early_on_exact():
    """A single-artist folder with a whole-name exact hit stops searching:
    no part queries run (band names like 'Simon & Garfunkel' stay whole)."""
    conn = db_connect(":memory:")
    _untagged_folder(conn, "Simon & Garfunkel", "Simon & Garfunkel", ["01 x.mp3"])
    conn.commit()
    client = FakeMB(
        search={
            "Simon & Garfunkel": [{"id": "sg", "name": "Simon & Garfunkel", "score": 100}],
        },
    )
    resolver.propose(client, conn, max_artists=5)
    assert client.calls == ["Simon & Garfunkel"]
    row = conn.execute(
        "SELECT proposed_mbid, confidence, status FROM mb_resolutions WHERE folder_artist LIKE 'Simon%'"
    ).fetchone()
    assert row["proposed_mbid"] == "sg"
    assert row["confidence"] == "exact"
    conn.close()


# ── 3. one MBID ↔ one folder-artist (commit guard) ─────────────────────────


def test_commit_refuses_second_folder_artist_on_same_mbid():
    conn = db_connect(":memory:")
    for folder in ("Larry June;The Alchemist", "Armand Hammer;The Alchemist"):
        conn.execute(
            "INSERT INTO mb_resolutions (folder_artist, proposed_mbid, proposed_name, score, "
            "confidence, status, proposed_at) VALUES (?,?,?,?,?,?,?)",
            (folder, ALCHEMIST, "The Alchemist", 100, "exact", "proposed", 0),
        )
    conn.commit()
    result = resolver.commit(None, conn)
    assert result["applied_count"] == 1
    second = next(s for s in result["skipped"] if s["artist"] == "Armand Hammer;The Alchemist")
    assert "retarget" in second["reason"].lower()
    # first binding recorded as applied; second stays proposed for review
    assert conn.execute(
        "SELECT status FROM mb_resolutions WHERE folder_artist='Larry June;The Alchemist'"
    ).fetchone()[0] == "applied"
    conn.close()


def test_commit_refuses_mbid_already_bound_to_other_tracks():
    conn = db_connect(":memory:")
    conn.execute(
        "INSERT OR IGNORE INTO artists (artist_mbid, name, first_seen, last_seen) "
        "VALUES (?, 'The Alchemist', 't', 't')",
        (ALCHEMIST,),
    )
    conn.execute(
        "INSERT OR IGNORE INTO tracks (path, parent_folder, filename, suffix, artist, artist_mbid, "
        "readable, mtime, size, first_seen, last_seen) VALUES (?,?,?,?,?,?,1,0,0,'t','t')",
        ("Other/01.mp3", "Other", "t.mp3", ".mp3", "Armand Hammer", ALCHEMIST),
    )
    conn.execute(
        "INSERT INTO mb_resolutions (folder_artist, proposed_mbid, proposed_name, score, "
        "confidence, status, proposed_at) VALUES (?,?,?,?,?,?,?)",
        ("Larry June;The Alchemist", ALCHEMIST, "The Alchemist", 100, "exact", "proposed", 0),
    )
    conn.commit()
    result = resolver.commit(None, conn)
    assert result["applied_count"] == 0
    assert "retarget" in result["skipped"][0]["reason"].lower()
    conn.close()


# ── 4. prefix matches are never 'exact' — they don't propose at all ────────


def test_confidence_note_prefix_not_exact():
    assert resolver.confidence_note("Ama Lou", "Ama") == "prefix"
    assert resolver.confidence_note("MIKE", "Mike Oldfield") == "prefix"
    assert resolver.confidence_note("Ama", "Ama") == "exact"
    assert resolver.confidence_note("ama", "AMA") == "case-insensitive"


def test_propose_prefix_only_hit_proposes_nothing():
    """'Ama' vs 'Ama Lou' is a substring hit (tier 0) — propose stores no
    candidate at all instead of a score-100 'exact' false positive."""
    conn = db_connect(":memory:")
    _untagged_folder(conn, "Ama", "Ama", ["01 x.mp3"], album="AMA")
    conn.commit()
    client = FakeMB(
        search={"Ama": [{"id": "ama-lou", "name": "Ama Lou", "score": 100}]},
        rgs={"ama-lou": [{"title": "Unrelated"}]},
    )
    result = resolver.propose(client, conn, max_artists=5)
    row = conn.execute(
        "SELECT proposed_mbid, confidence, evidence FROM mb_resolutions WHERE folder_artist='Ama'"
    ).fetchone()
    assert row["proposed_mbid"] is None
    assert row["confidence"] == "none"
    assert result["unresolved_artists"] == ["Ama"]
    conn.close()


# ── 5. foreign-script names classify correctly ─────────────────────────────


def test_propose_fullwidth_name_case_insensitive_not_fuzzy():
    """Full-width Latin 'ＨＹ' vs MB's ASCII 'HY': normalize_name-based
    classification keeps the match confident instead of degrading to fuzzy."""
    conn = db_connect(":memory:")
    _untagged_folder(conn, "ＨＹ", "ＨＹ", ["01 z.mp3"])
    conn.commit()
    client = FakeMB(search={"ＨＹ": [{"id": "hy", "name": "HY", "score": 100}]})
    resolver.propose(client, conn, max_artists=5)
    row = conn.execute(
        "SELECT confidence, status FROM mb_resolutions WHERE folder_artist='ＨＹ'"
    ).fetchone()
    assert row["confidence"] == "case-insensitive"
    assert row["status"] == "proposed"
    conn.close()


def test_propose_kanji_album_evidence_not_lost():
    """CJK album titles are short: the containment floor must not eat them.
    Folder album '言葉' vs MB release-group '言葉 (Single)' → evidence found."""
    conn = db_connect(":memory:")
    _untagged_folder(conn, "浜崎真理", "浜崎真理", ["01 kotoha.mp3"], album="言葉")
    conn.commit()
    client = FakeMB(
        search={"浜崎真理": [{"id": "mari", "name": "浜崎真理", "score": 100}]},
        rgs={"mari": [{"title": "言葉 (Single)"}]},
    )
    resolver.propose(client, conn, max_artists=5)
    row = conn.execute(
        "SELECT confidence, status, evidence FROM mb_resolutions WHERE folder_artist='浜崎真理'"
    ).fetchone()
    assert row["confidence"] == "exact"
    assert row["status"] == "proposed"
    assert "言葉" in (row["evidence"] or "")
    conn.close()


# ── 6. counters describe THIS run; stale rows never resurface ──────────────


def test_propose_counters_this_run_and_unresolved_named():
    conn = db_connect(":memory:")
    _untagged_folder(conn, "Real Artist", "Real Artist", ["01 a.mp3"])
    _untagged_folder(conn, "Ghost Artist", "Ghost Artist", ["01 b.mp3"])
    conn.commit()
    client = FakeMB(
        search={"Real Artist": [{"id": "real", "name": "Real Artist", "score": 100}]},
        rgs={"real": [{"title": "Any Album"}]},
    )
    first = resolver.propose(client, conn, max_artists=10)
    assert first["artists_considered"] == 2
    assert first["proposals_stored"] == 1  # new candidates, not table rows
    assert first["no_mb_match"] == 1
    assert first["unresolved_artists"] == ["Ghost Artist"]
    assert first["re_proposed_existing"] == 0
    # proposals list covers THIS run only — no stale-row duplication
    assert len(first["proposals"]) == 2

    second = resolver.propose(client, conn, max_artists=10)
    assert second["re_proposed_existing"] == 2
    assert second["proposals_stored"] == 1
    assert second["no_mb_match"] == 1
    assert len(second["proposals"]) == 2
    conn.close()


def test_propose_counters_preserve_manual_decisions():
    conn = db_connect(":memory:")
    _untagged_folder(conn, "Decided", "Decided", ["01 d.mp3"])
    conn.commit()
    client = FakeMB(search={"Decided": [{"id": "d1", "name": "Decided", "score": 100}]})
    resolver.propose(client, conn, max_artists=5)
    conn.execute("UPDATE mb_resolutions SET status='rejected' WHERE folder_artist='Decided'")
    conn.commit()
    rerun = resolver.propose(client, conn, max_artists=5)
    assert rerun["decisions_preserved"] == {"rejected": 1}
    assert conn.execute(
        "SELECT status FROM mb_resolutions WHERE folder_artist='Decided'"
    ).fetchone()[0] == "rejected"
    conn.close()
