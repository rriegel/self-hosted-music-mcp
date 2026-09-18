"""Spike: SQLite WAL smoke test — concurrent readers while a writer commits.

Phase 0 question: can a writer (cron-style incremental sync) and several
readers (agent tool calls) share one SQLite file safely?

Usage: uv run python -m music_mcp.spike.wal_smoke [--db PATH]
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path


def writer(db_path: Path, rows: int) -> dict:
    conn = sqlite3.connect(db_path, timeout=30)
    started = time.monotonic()
    with conn:
        conn.executemany(
            "INSERT INTO listens (ts, artist_mbid, track) VALUES (?, ?, ?)",
            [(i, "mbid-%d" % i, "track-%d" % i) for i in range(rows)],
        )
    conn.close()
    return {"role": "writer", "rows": rows, "elapsed_s": round(time.monotonic() - started, 3)}


def reader(db_path: Path, reads: int) -> dict:
    conn = sqlite3.connect(db_path, timeout=30)
    conn.execute("PRAGMA busy_timeout=5000")
    counts = []
    started = time.monotonic()
    for _ in range(reads):
        counts.append(conn.execute("SELECT COUNT(*) FROM listens").fetchone()[0])
        time.sleep(0.05)
    conn.close()
    return {
        "role": "reader",
        "reads": reads,
        "count_progression": [counts[0], counts[-1]],
        "elapsed_s": round(time.monotonic() - started, 3),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=None)
    args = parser.parse_args()

    db_path = Path(args.db) if args.db else Path(tempfile.mkdtemp()) / "spike_wal.db"
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("CREATE TABLE IF NOT EXISTS listens (ts INTEGER, artist_mbid TEXT, track TEXT)")
    conn.close()

    with ThreadPoolExecutor(max_workers=4) as pool:
        w = pool.submit(writer, db_path, 5000)
        r1 = pool.submit(reader, db_path, 10)
        r2 = pool.submit(reader, db_path, 10)
        r3 = pool.submit(reader, db_path, 10)
        results = [w.result(), r1.result(), r2.result(), r3.result()]

    integrity = sqlite3.connect(db_path).execute("PRAGMA integrity_check").fetchone()[0]
    summary = {"db": str(db_path), "journal_mode": "wal", "integrity": integrity, "threads": results}
    print(json.dumps(summary, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
