"""fix/resolver-evidence: homograph defense, proposal review, entity-split detection.

Fixtures come from the 2026-09-20 resolver dogfood incident:
- folder 'Ama' proposed MBID a5ea16ca ("Ama", Spanish group) at score 92 labeled
  'exact' — pure name matching, no music evidence consulted
- MusicBrainz had the same artist under two entities (be578aa2 'AMA' vs
  2d2f1395 'Ama Lou'); the agent had to hand-write SQL to fix both problems
"""

from __future__ import annotations

import sqlite3
from collections.abc import Generator

import pytest

from music_mcp.library import db as db_mod
from music_mcp.library.quality import library_find_dupes
from music_mcp.mb import resolver

AMA_SPANISH = "a5ea16ca-0000-4000-8000-000000000000"
AMA_LOU = "2d2f1395-0419-4bf9-a1e7-511946385717"
AMA_DUPE = "be578aa2-79dd-4bf9-a1e7-511946385717"


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
        (None, None, album, f"Ama/{album or 'x'}" if album else f"Ama/{tracks[0]}", None, "t", "t"),
    )
    for t in tracks:
        conn.execute(
            "INSERT OR IGNORE INTO tracks (path, parent_folder, filename, suffix, artist, "
            "readable, mtime, size, first_seen, last_seen) VALUES (?,?,?,?,?,1,0,0,'t','t')",
            (f"Ama/{t}", f"Ama/{album or 'x'}" if album else f"Ama/{tracks[0]}", t, ".mp3", artist),
        )


class FakeMB:
    """Duck-typed MBClient: search + release-groups, no network."""

    def __init__(self, search: dict[str, list[dict]], rgs: dict[str, list[dict]]):
        self._search = search
        self._rgs = rgs
        self.rg_calls: list[str] = []

    def search_artist(self, name, limit=3):
        return {"artists": self._search.get(name, [])}

    def release_groups(self, mbid):
        self.rg_calls.append(mbid)
        return self._rgs.get(mbid, [])


def _proposal_row(conn, folder_artist: str, mbid: str, name: str, status: str = "proposed"):
    conn.execute(
        "INSERT OR REPLACE INTO mb_resolutions (folder_artist, proposed_mbid, proposed_name, "
        "score, confidence, status, proposed_at) VALUES (?,?,?,?,?,?,?)",
        (folder_artist, mbid, name, 100, "exact", status, 0),
    )


# ── evidence-checked propose ─────────────────────────────────────────────────


def test_propose_flags_name_only_match_without_album_evidence():
    """Real incident: folder 'Ama' (albums Life's Better/Aura...) proposed the
    Spanish group 'Ama' — name-exact but discographically empty overlap."""

    conn = db_connect(":memory:")
    _untagged_folder(conn, "Ama", "Ama", ["01 lifes better.mp3", "02 aura.mp3"], album="AMA")
    conn.commit()
    client = FakeMB(
        search={"Ama": [{"id": AMA_SPANISH, "name": "Ama", "score": 92}]},
        rgs={AMA_SPANISH: [{"title": "Turmoil Bliss"}, {"title": "Slip"}]},
    )
    result = resolver.propose(client, conn, max_artists=5)
    row = conn.execute(
        "SELECT confidence, status, evidence FROM mb_resolutions WHERE folder_artist = 'Ama'"
    ).fetchone()
    assert row["status"] == "needs_review"
    assert "album evidence" in row["evidence"].lower()
    assert row["confidence"] == "exact"  # name tier unchanged; status carries the doubt
    assert any(p["artist"] == "Ama" and p["status"] == "needs_review" for p in result["proposals"])


def test_propose_confirms_match_with_album_evidence():

    conn = db_connect(":memory:")
    _untagged_folder(conn, "Ama Lou", "Ama Lou", ["01 need it bad.mp3"], album="AMA")
    conn.commit()
    client = FakeMB(
        search={"Ama Lou": [{"id": AMA_LOU, "name": "Ama Lou", "score": 100}]},
        rgs={AMA_LOU: [{"title": "AMA"}, {"title": "Need It Bad"}, {"title": "I Came Home Late"}]},
    )
    resolver.propose(client, conn, max_artists=5)
    row = conn.execute(
        "SELECT confidence, status, evidence FROM mb_resolutions WHERE folder_artist = 'Ama Lou'"
    ).fetchone()
    assert row["status"] == "proposed"
    assert "AMA" in (row["evidence"] or "")
    conn.close()


def test_propose_skips_evidence_when_folder_has_no_album_titles():

    conn = db_connect(":memory:")
    _untagged_folder(conn, "Mystery Artist", "Mystery Artist", ["01 x.mp3"])  # no album row
    conn.commit()
    client = FakeMB(
        search={"Mystery Artist": [{"id": "m-1", "name": "Mystery Artist", "score": 100}]},
        rgs={"m-1": [{"title": "Anything"}]},
    )
    resolver.propose(client, conn, max_artists=5)
    row = conn.execute(
        "SELECT status, evidence FROM mb_resolutions WHERE folder_artist = 'Mystery Artist'"
    ).fetchone()
    # no evidence either way → stays auto-applyable (name tier unchanged)
    assert row["status"] == "proposed"
    conn.close()


# ── mb_review: supported proposal corrections ────────────────────────────────


def test_review_set_overrides_and_commit_applies(tmp_path):

    conn = db_connect(tmp_path / "t.db")
    _proposal_row(conn, "Ama", AMA_SPANISH, "Ama")
    conn.commit()

    result = resolver.review_set(conn, folder_artist="Ama", mbid=AMA_LOU, name="Ama Lou")
    assert result["updated"] == 1
    row = conn.execute(
        "SELECT proposed_mbid, score, confidence, status FROM mb_resolutions WHERE folder_artist='Ama'"
    ).fetchone()
    assert row["proposed_mbid"] == AMA_LOU and row["status"] == "proposed"
    assert row["score"] == 100

    applied = resolver.commit(None, conn)
    assert applied["applied_count"] == 1
    assert applied["applied"][0]["mbid"] == AMA_LOU
    conn.close()


def test_review_reject_blocks_apply():

    conn = db_connect(":memory:")
    _proposal_row(conn, "Bad Artist", "bad-1", "Bad Artist")
    conn.commit()
    resolver.review_reject(conn, folder_artist="Bad Artist")
    applied = resolver.commit(None, conn)
    assert applied["applied_count"] == 0
    assert conn.execute(
        "SELECT status FROM mb_resolutions WHERE folder_artist='Bad Artist'"
    ).fetchone()[0] == "rejected"
    conn.close()


def test_review_list_shape(tmp_path):

    conn = db_connect(tmp_path / "t.db")
    _proposal_row(conn, "A", "m-a", "A")
    _proposal_row(conn, "B", "m-b", "B")
    conn.execute("UPDATE mb_resolutions SET status='rejected' WHERE folder_artist='B'")
    listing = resolver.review_list(conn)
    assert {r["folder_artist"] for r in listing["proposals"]} == {"A", "B"}
    assert {r["status"] for r in listing["proposals"]} == {"proposed", "rejected"}
    conn.close()


# ── entity-split detection ───────────────────────────────────────────────────


def test_find_dupes_artist_entities_flags_colocated_overlap(cache):
    """be578aa2 'AMA' vs 2d2f1395 'Ama Lou': same library root folder, MB RG sets overlap."""
    for mbid, name in ((AMA_LOU, "Ama Lou"), (AMA_DUPE, "AMA")):
        cache.execute(
            "INSERT OR IGNORE INTO artists (artist_mbid, name, first_seen, last_seen) VALUES (?,?,?,?)",
            (mbid, name, "t", "t"),
        )
        cache.execute(
            "INSERT OR IGNORE INTO albums (album_mbid, artist_mbid, title, folder, date, first_seen, last_seen) "
            "VALUES (?,?,?,?,?,?,?)",
            (None, mbid, "I Came Home Late" if mbid == AMA_LOU else "AMA",
             f"Ama/{'I Came Home Late' if mbid == AMA_LOU else 'AMA'}", None, "t", "t"),
        )
    cache.commit()
    client = FakeMB(
        search={},
        rgs={
            AMA_LOU: [{"title": "AMA"}, {"title": "Need It Bad"}, {"title": "I Came Home Late"}],
            AMA_DUPE: [{"title": "AMA"}, {"title": "Need It Bad"}],
        },
    )
    result = library_find_dupes(cache, scope="artist_entities", client=client)
    flagged = result["findings"]["artist_entities"]
    assert len(flagged) == 1
    hit = flagged[0]
    assert {hit["artist_mbid_a"], hit["artist_mbid_b"]} == {AMA_LOU, AMA_DUPE}
    assert hit["shared_release_groups"] == ["AMA", "Need It Bad"]
    assert "canonical" in hit["suggested_action"].lower()


def test_entity_dupes_not_flagged_without_overlap(cache):
    for mbid, name in ((AMA_LOU, "Ama Lou"), (AMA_DUPE, "AMA")):
        cache.execute(
            "INSERT OR IGNORE INTO artists (artist_mbid, name, first_seen, last_seen) VALUES (?,?,?,?)",
            (mbid, name, "t", "t"),
        )
    cache.commit()
    client = FakeMB(
        search={},
        rgs={AMA_LOU: [{"title": "Unrelated Album"}], AMA_DUPE: [{"title": "Different Album"}]},
    )
    result = library_find_dupes(cache, scope="artist_entities", client=client)
    # empty findings are omitted by the report contract
    assert result["findings"].get("artist_entities") in (None, [])
    assert result["total_findings"] == 0


# ── retarget: supported entity merge ────────────────────────────────────────


def test_retarget_moves_albums_and_tracks(tmp_path):

    conn = db_connect(tmp_path / "t.db")
    for mbid, name in ((AMA_LOU, "Ama Lou"), (AMA_DUPE, "AMA")):
        conn.execute(
            "INSERT OR IGNORE INTO artists (artist_mbid, name, first_seen, last_seen) VALUES (?,?,?,?)",
            (mbid, name, "t", "t"),
        )
    for folder, mbid, title in (("Ama/AMA", AMA_DUPE, "AMA"), ("Ama/ICHL", AMA_LOU, "I Came Home Late")):
        conn.execute(
            "INSERT OR IGNORE INTO albums (album_mbid, artist_mbid, title, folder, date, first_seen, last_seen) "
            "VALUES (?,?,?,?,?,?,?)", (None, mbid, title, folder, None, "t", "t"),
        )
        conn.execute(
            "INSERT OR IGNORE INTO tracks (path, parent_folder, filename, suffix, artist, artist_mbid, "
            "readable, mtime, size, first_seen, last_seen) VALUES (?,?,?,?,?,?,'x',0,0,'t','t')",
            (f"{folder}/01.mp3", folder, "01.mp3", ".mp3", "AMA", mbid),
        )
    conn.commit()

    result = resolver.retarget(conn, from_mbid=AMA_DUPE, into_mbid=AMA_LOU)
    assert result["albums_moved"] == 1 and result["tracks_moved"] == 1
    assert conn.execute("SELECT COUNT(*) FROM artists WHERE artist_mbid=?", (AMA_DUPE,)).fetchone()[0] == 0
    assert conn.execute(
        "SELECT artist_mbid FROM albums WHERE folder='Ama/AMA'"
    ).fetchone()[0] == AMA_LOU
    assert conn.execute(
        "SELECT artist_mbid FROM tracks WHERE path='Ama/AMA/01.mp3'"
    ).fetchone()[0] == AMA_LOU
    conn.close()


def test_retarget_guards(tmp_path):

    conn = db_connect(tmp_path / "t.db")
    conn.execute(
        "INSERT OR IGNORE INTO artists (artist_mbid, name, first_seen, last_seen) VALUES (?,?,?,?)",
        (AMA_LOU, "Ama Lou", "t", "t"),
    )
    conn.commit()
    import pytest

    with pytest.raises(ValueError):
        resolver.retarget(conn, from_mbid=AMA_LOU, into_mbid=AMA_LOU)  # self
    with pytest.raises(ValueError):
        resolver.retarget(conn, from_mbid="absent-mbid", into_mbid=AMA_LOU)  # not in index
    conn.close()
