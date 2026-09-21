"""Fix/gap-analysis-buckets: name normalization, bucketed gap analysis, radar dedupe.

Fixtures come from the 2026-09-20 real-data dogfood run — every false positive the
agent had to excavate by hand via raw SQL is now an asserted contract:
- hi‐posi (U+2010 hyphen in library) vs "Hi-Posi" (ASCII in listens)
- LEE HI (library) vs LeeHi (listens)
- 高中正義 (library, kanji) vs Masayoshi Takanaka (listens) — string tier CANNOT
  bridge this; the mb_resolutions table is what classifies it (documented boundary)
- Lelio Luttazzi Trio (library) vs Lelio Luttazzi (listens)
- NPR/radio-station junk that polluted the old single-bucket output
"""

from __future__ import annotations

import sqlite3
import time
from collections.abc import Generator
from pathlib import Path

import pytest

from music_mcp.discovery import releases as rel_mod
from music_mcp.library import db as db_mod
from music_mcp.listens import credits, reports

KANJI = "高中正義"
ROMAJI = "Masayoshi Takanaka"
U2010 = "hi‐posi"  # unicode hyphen, as the library stores it


@pytest.fixture()
def cache(tmp_path: Path) -> Generator[sqlite3.Connection]:
    conn = db_mod.connect(tmp_path / "t.db")
    yield conn
    conn.close()


def _add_owned_artist(conn: sqlite3.Connection, mbid: str, name: str) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO artists (artist_mbid, name, first_seen, last_seen) VALUES (?,?,?,?)",
        (mbid, name, "t", "t"),
    )
    conn.execute(
        "INSERT OR IGNORE INTO tracks (path, parent_folder, filename, suffix, artist, artist_mbid, "
        "readable, mtime, size, first_seen, last_seen) VALUES "
        "(?,?,?,'.mp3','x',?,1,0,0,'t','t')",
        (f"/o/{mbid}.mp3", "o", "x.mp3", mbid),
    )
    conn.commit()


def _listen(conn: sqlite3.Connection, credit: str, count: int) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO listened_artists VALUES (?,?,?,?)",
        (credit, count, 0, int(time.time())),
    )


def _proposal(
    conn: sqlite3.Connection,
    folder_artist: str,
    mbid: str,
    proposed_name: str,
    status: str = "proposed",
) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO mb_resolutions (folder_artist, proposed_mbid, proposed_name, "
        "score, confidence, status, proposed_at) VALUES (?,?,?,?,?,?,?)",
        (folder_artist, mbid, proposed_name, 100, "exact", status, time.time()),
    )


# ── normalize_name ───────────────────────────────────────────────────────────


def test_normalize_unicode_hyphen_matches_ascii():
    assert credits.normalize_name(U2010) == credits.normalize_name("hi-posi")


def test_normalize_case_and_whitespace():
    assert credits.normalize_name("LEE HI") == credits.normalize_name("LeeHi")


def test_normalize_is_idempotent_and_folds_punctuation():
    n = credits.normalize_name("Belle & Sebastian")
    assert n == credits.normalize_name("belle &  sebastian!")
    assert credits.normalize_name(n) == n


def test_normalize_does_not_transliterate_kanji():
    # Documented boundary: the string tier never bridges scripts; that is the
    # mb_resolutions tier's job. This test pins the boundary so nobody 'fixes'
    # it with an ASCII fold that silently mangles non-Latin names.
    assert credits.normalize_name(KANJI) != credits.normalize_name(ROMAJI)
    assert credits.normalize_name(KANJI) == KANJI


# ── resolve_lb_credit: string tiers ──────────────────────────────────────────


def test_credit_matches_unicode_hyphen_variant():
    owned = {credits.normalize_name(n) for n in (U2010,)}
    assert credits.resolve_lb_credit("Hi-Posi", owned)["owned"] is True


def test_credit_matches_whitespace_variant():
    owned = {credits.normalize_name("LEE HI")}
    assert credits.resolve_lb_credit("LeeHi", owned)["owned"] is True


def test_credit_matches_ensemble_suffix_subset():
    owned = {credits.normalize_name("Lelio Luttazzi Trio")}
    verdict = credits.resolve_lb_credit("Lelio Luttazzi", owned)
    assert verdict["owned"] is True
    assert verdict["matched_via"] == ["Lelio Luttazzi"]


# ── gap_analysis buckets ─────────────────────────────────────────────────────


def test_gap_analysis_buckets_real_dogfood_shapes(cache: sqlite3.Connection):
    _add_owned_artist(cache, "a-1", U2010)  # owned, unicode hyphen
    _add_owned_artist(cache, "a-2", KANJI)  # owned, kanji-only
    _add_owned_artist(cache, "a-3", "Lelio Luttazzi Trio")

    _listen(cache, "Hi-Posi", 11)  # owned via normalization
    _listen(cache, ROMAJI, 7)  # string tiers fail; resolver proposal saves it
    _listen(cache, "Ama Lou", 14)  # true gap
    _listen(cache, "Up First from NPR", 45)  # junk (podcast)
    _listen(cache, "Ann Arbor - Jazz", 67)  # junk (station-style)
    _proposal(cache, KANJI, "takanaka-mbid", ROMAJI)
    cache.commit()

    gap = reports.listens_gap_analysis(cache, min_listens=5)

    assert "not_owned" not in gap  # old flat bucket is gone
    assert [g["artist"] for g in gap["true_gaps"]] == ["Ama Lou"]
    assert gap["true_gaps"][0]["listens"] == 14
    assert gap["true_gaps"][0]["suggested_action"]

    unresolved = {g["artist"]: g for g in gap["owned_but_unresolved"]}
    assert ROMAJI in unresolved
    assert "mb_apply" in unresolved[ROMAJI]["suggested_action"]

    junk = {g["artist"]: g for g in gap["junk_suspects"]}
    assert "Up First from NPR" in junk
    assert "Ann Arbor - Jazz" in junk
    assert junk["Up First from NPR"]["reason"]

    # owned via normalized string match shows up in matched_owned, nowhere else
    all_bucketed = {
        g["artist"]
        for b in ("true_gaps", "owned_but_unresolved", "junk_suspects")
        for g in gap[b]
    }
    assert U2010 not in all_bucketed and "Hi-Posi" not in all_bucketed
    assert gap["matched_owned"] >= 1


def test_gap_analysis_action_steers_to_resolver_workflow(cache: sqlite3.Connection):
    # a gap candidate that has a stored resolver proposal is owned_but_unresolved,
    # with the action pointing at the resolver workflow
    _listen(cache, "Fresh Find", 9)
    _listen(cache, "Really Gone", 6)
    _proposal(cache, "some-library-folder-artist", "ff-mbid", "Fresh Find")
    cache.commit()

    gap = reports.listens_gap_analysis(cache, min_listens=5)
    gap_names = {g["artist"] for g in gap["true_gaps"]}
    assert "Fresh Find" not in gap_names and "Really Gone" in gap_names
    unresolved = {g["artist"]: g for g in gap["owned_but_unresolved"]}
    assert "Fresh Find" in unresolved
    assert "mb_apply" in unresolved["Fresh Find"]["suggested_action"]


# ── discovery_new_releases dedupe ────────────────────────────────────────────


class FakeMBClient:
    """Duck-typed MBClient: canned .get() responses, no network, minimal conn."""

    def __init__(self, pages: list[list[dict]], count: int | None = None):
        self._pages = pages
        self._count = count if count is not None else sum(len(p) for p in pages)
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.execute(
            "CREATE TABLE mb_cache (key TEXT PRIMARY KEY, url TEXT, response TEXT, fetched_at REAL)"
        )

    def get(self, path, params=None):
        offset = (params or {}).get("offset", 0)
        page = self._pages[offset // 100] if offset // 100 < len(self._pages) else []
        return {"releases": page, "count": self._count}


def _release(rel_id: str, rg: str, artist: str, date: str = "2026-09-15") -> dict:
    return {
        "id": rel_id,
        "title": f"Edition {rg}",
        "date": date,
        "artist-credit": [{"name": artist, "artist": {"id": f"ar-{artist}", "name": artist}}],
        "release-group": {"id": f"rg-{artist}-{rg}", "primary-type": "Album"},
    }


def test_new_releases_dedupes_release_group_editions(cache: sqlite3.Connection):
    # two editions of the same release-group + an exact duplicate row
    fake = FakeMBClient([[
        _release("r1", "g1", "Ama Lou", "2026-09-15"),
        _release("r2", "g1", "Ama Lou", "2026-09-16"),  # same RG, later edition
        _release("r1", "g1", "Ama Lou", "2026-09-15"),  # exact duplicate of r1
        _release("r3", "g2", "Ama Lou", "2026-09-14"),
    ]])
    _listen(cache, "Ama Lou", 20)
    cache.commit()

    result = rel_mod.discovery_new_releases(
        cache, client=fake, filter_mode="listened", since_days=7, with_genres=False
    )
    rg_ids = [r["release_group_mbid"] for r in result["releases"]]
    assert len(rg_ids) == len(set(rg_ids)), f"dup release-groups in output: {rg_ids}"
    assert rg_ids.count("rg-Ama Lou-g1") == 1
    # deterministic pick: earliest-dated edition wins
    kept = next(r for r in result["releases"] if r["release_group_mbid"] == "rg-Ama Lou-g1")
    assert kept["release_mbid"] == "r1" and kept["date"] == "2026-09-15"


def test_new_releases_pagination_overlap_dedupes(cache: sqlite3.Connection):
    # MB search can return the same release on consecutive page boundaries
    first = [_release("p1-a", "ga", "Ama Lou"), _release("p1-b", "gb", "Ama Lou")]
    second = [_release("p1-b", "gb", "Ama Lou"), _release("p2-a", "gc", "Ama Lou")]
    # real MB reports the true total, so the fetcher keeps paginating past tiny test pages
    fake = FakeMBClient([first, second], count=250)
    _listen(cache, "Ama Lou", 20)
    cache.commit()

    result = rel_mod.discovery_new_releases(
        cache, client=fake, filter_mode="listened", since_days=7
    )
    rel_ids = [r["release_mbid"] for r in result["releases"]]
    assert sorted(set(rel_ids)) == ["p1-a", "p1-b", "p2-a"]
