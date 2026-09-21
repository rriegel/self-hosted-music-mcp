"""MCP server contract tests: tools registered, callable, correct shapes.

Per the plan's test strategy: in-memory (no stdio spawn). The server module
registers 22 tools wrapping the tested handlers; these tests assert the tool
list and exercise representative calls through the same code path a client
would use. Async via anyio (a transitive dep of the MCP SDK).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from music_mcp.server import mcp

EXPECTED_TOOLS = {
    "library_scan", "library_status", "library_artists", "library_albums",
    "library_find_dupes", "library_quality_report",
    "listens_sync", "listens_recent", "listens_top", "listens_gap_analysis",
    "listens_stale_library", "listens_new_discoveries",
    "mb_artist", "mb_artist_releases", "mb_search", "mb_resolve", "mb_apply",
    "mb_review",
    "discovery_similar_artists", "discovery_recommendations",
    "discovery_new_releases", "discovery_playlist",
    "watchlist_manage",
}


def _payload(result):
    """MCP 2.x call_tool returns a CallToolResult; extract the structured dict."""
    if isinstance(result, tuple):
        result = result[1]
    structured = getattr(result, "structuredContent", None)
    if structured is not None:
        return structured
    # fall back to first text content block parsed as JSON
    import json

    for block in getattr(result, "content", []) or []:
        text = getattr(block, "text", None)
        if text:
            try:
                return json.loads(text)
            except json.JSONDecodeError:
                continue
    raise AssertionError(f"no structured payload in result: {result!r}")


@pytest.fixture(scope="module")
def indexed_db(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A scanned index (via the fixture library) for the tool calls."""
    from music_mcp.library import db as db_mod
    from music_mcp.library import scanner

    path = tmp_path_factory.mktemp("mcp") / "index.db"
    conn = db_mod.connect(path)
    scanner.scan_library(Path(__file__).parent / "fixtures" / "library", conn)
    conn.close()
    return path


@pytest.fixture(autouse=True)
def _db_env(indexed_db: Path, monkeypatch):
    monkeypatch.setenv("MUSIC_DB", str(indexed_db))


@pytest.mark.anyio
async def test_all_expected_tools_registered():
    tools = {t.name for t in await mcp.list_tools()}
    assert tools >= EXPECTED_TOOLS
    assert len(tools) == len(EXPECTED_TOOLS)  # no surprises


@pytest.mark.anyio
async def test_library_status_call():
    payload = _payload(await mcp.call_tool("library_status", {}))
    assert payload["tracks"] == 5


@pytest.mark.anyio
async def test_listens_gap_call():
    payload = _payload(await mcp.call_tool("listens_gap_analysis", {"min_listens": 1}))
    assert "true_gaps" in payload and "owned_but_unresolved" in payload
    assert "not_owned" not in payload


@pytest.mark.anyio
async def test_watchlist_manage_roundtrip():
    added = _payload(await mcp.call_tool(
        "watchlist_manage", {"action": "add", "artists": [{"mbid": "wt-1", "name": "Watched"}]}
    ))
    listed = _payload(await mcp.call_tool("watchlist_manage", {"action": "list"}))
    removed = _payload(await mcp.call_tool("watchlist_manage", {"action": "remove", "mbids": ["wt-1"]}))
    assert added.get("added") == 1
    assert listed["total"] == 1
    assert removed.get("removed") == 1
