"""CLI-level tests: the empty-index guard on mb resolve/apply."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from music_mcp.library import db as db_mod
from music_mcp.mb import cli as mb_cli


@pytest.fixture()
def fresh_db(tmp_path: Path) -> Path:
    """An empty (schema-only) cache — like the stray default-location DB."""
    path = tmp_path / "empty.db"
    db_mod.connect(path).close()
    return path


@pytest.fixture()
def seeded_db(tmp_path: Path) -> Path:
    """A cache with one scanned track (via the fixture library)."""
    from music_mcp.library import scanner

    path = tmp_path / "seeded.db"
    conn = db_mod.connect(path)
    scanner.scan_library(Path(__file__).parent / "fixtures" / "library", conn)
    conn.close()
    return path


def _run(capsys: pytest.CaptureFixture, argv: list[str]) -> int:
    try:
        return mb_cli.main(argv)
    except SystemExit as exc:  # sys.exit(1) path
        return int(exc.code or 0)
    finally:
        pass


def test_resolve_refuses_empty_index(fresh_db: Path, capsys: pytest.CaptureFixture):
    code = _run(capsys, ["--db", str(fresh_db), "resolve"])
    out = capsys.readouterr().out
    assert code == 1
    payload = json.loads(out)
    assert "no tracks" in payload["error"]
    assert "empty index" in payload["error"]
    assert "MUSIC_DB" in payload["hint"]


def test_apply_refuses_empty_index(fresh_db: Path, capsys: pytest.CaptureFixture):
    code = _run(capsys, ["--db", str(fresh_db), "apply"])
    out = capsys.readouterr().out
    assert code == 1
    assert "empty index" in json.loads(out)["error"]


def test_resolve_runs_on_seeded_index(seeded_db: Path, capsys: pytest.CaptureFixture):
    """With tracks present, the guard passes and resolve proceeds (0 untagged
    considered here is fine — the fixture library's artists are untagged by
    name only when they lack MBIDs; the guard only checks track presence)."""
    try:
        code = mb_cli.main(["--db", str(seeded_db), "resolve", "--max", "1"])
    except SystemExit as exc:
        code = int(exc.code or 0)
    # no guard rejection (exit 0 or a resolver result); must NOT print the guard error
    out = capsys.readouterr().out
    assert "refusing to" not in out
    assert code in (0, 1)  # 1 only if LB/MB network is unavailable; guard not the cause
    assert "no tracks in" not in out
