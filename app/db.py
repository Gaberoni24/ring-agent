"""SQLite capture log.

Single-writer usage (uvicorn worker + background consumer in one process),
so a thread-local connection with WAL mode is enough. Every capture the ring
delivers gets a row here before anything else happens to it — the table is
the source of truth for "did we lose anything".
"""

import sqlite3
import threading
import time
from pathlib import Path

_SCHEMA = """
CREATE TABLE IF NOT EXISTS captures (
    id          TEXT PRIMARY KEY,
    recorded_at INTEGER NOT NULL,
    transcription TEXT,
    audio_path  TEXT,
    status      TEXT NOT NULL DEFAULT 'received'
                CHECK (status IN ('received', 'processing', 'done', 'failed')),
    summary     TEXT,
    tool_trace  TEXT,
    tokens_used INTEGER,
    created_at  INTEGER NOT NULL,
    updated_at  INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_captures_status ON captures(status);
"""

# Added after the first deploy; init() adds whichever are missing.
#   kind: 'ring' (from the ring), or a notification-button child of
#         parent_id: 'undo', 'approved' (runs an approved plan), 'denied'
#   pending_approval: JSON {action, details, link, requested_at, reminded_at}
#         while the capture waits for an Approve/Deny tap
#   sweep_alerts: how many times scripts/sweep.py has reported it as stuck
_COLUMNS = {
    "kind": "TEXT NOT NULL DEFAULT 'ring'",
    "parent_id": "TEXT",
    "pending_approval": "TEXT",
    "sweep_alerts": "INTEGER NOT NULL DEFAULT 0",
}

_local = threading.local()
_db_path: Path | None = None


def init(db_path: Path) -> None:
    global _db_path
    _db_path = db_path
    conn = _connect()
    conn.executescript(_SCHEMA)
    have = {r["name"] for r in conn.execute("PRAGMA table_info(captures)")}
    for name, decl in _COLUMNS.items():
        if name not in have:
            conn.execute(f"ALTER TABLE captures ADD COLUMN {name} {decl}")
    conn.commit()


def _connect() -> sqlite3.Connection:
    conn = getattr(_local, "conn", None)
    if conn is None or getattr(_local, "path", None) != _db_path:
        assert _db_path is not None, "db.init() not called"
        conn = sqlite3.connect(_db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        _local.conn = conn
        _local.path = _db_path
    return conn


def _now() -> int:
    return int(time.time() * 1000)


def insert_capture(
    capture_id: str,
    recorded_at: int,
    transcription: str | None,
    audio_path: str | None,
) -> bool:
    """Insert a new capture. Returns False if the id already exists —
    a duplicate webhook delivery, which must not reset status or re-run
    the agent."""
    conn = _connect()
    now = _now()
    cur = conn.execute(
        """INSERT OR IGNORE INTO captures
           (id, recorded_at, transcription, audio_path, status, created_at, updated_at)
           VALUES (?, ?, ?, ?, 'received', ?, ?)""",
        (capture_id, recorded_at, transcription, audio_path, now, now),
    )
    conn.commit()
    return cur.rowcount == 1


def insert_child(
    parent_id: str,
    kind: str,
    transcription: str,
    status: str = "received",
    summary: str | None = None,
) -> bool:
    """Queue (or, with status='done', just record) a capture started by a
    notification button. Its id is '<parent>:<kind>', so a repeated tap is
    a no-op. Returns False if it already existed."""
    conn = _connect()
    now = _now()
    cur = conn.execute(
        """INSERT OR IGNORE INTO captures
           (id, recorded_at, transcription, status, summary, kind, parent_id,
            created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (f"{parent_id}:{kind}", now, transcription, status, summary, kind,
         parent_id, now, now),
    )
    conn.commit()
    return cur.rowcount == 1


def set_pending_approval(capture_id: str, plan_json: str) -> None:
    conn = _connect()
    conn.execute(
        "UPDATE captures SET pending_approval = ?, updated_at = ? WHERE id = ?",
        (plan_json, _now(), capture_id),
    )
    conn.commit()


def update_pending_approval(capture_id: str, plan_json: str) -> bool:
    """Rewrite a still-pending approval (e.g. reminder bookkeeping) without
    resurrecting one that was decided in the meantime."""
    conn = _connect()
    cur = conn.execute(
        "UPDATE captures SET pending_approval = ? "
        "WHERE id = ? AND pending_approval IS NOT NULL",
        (plan_json, capture_id),
    )
    conn.commit()
    return cur.rowcount == 1


def take_pending_approval(capture_id: str) -> str | None:
    """Atomically clear and return a capture's pending approval; None if
    there is none (already decided or expired). Approve and Deny both go
    through here, so only the first tap wins."""
    conn = _connect()
    row = conn.execute(
        "SELECT pending_approval FROM captures WHERE id = ?", (capture_id,)
    ).fetchone()
    if row is None or row["pending_approval"] is None:
        return None
    cur = conn.execute(
        "UPDATE captures SET pending_approval = NULL, updated_at = ? "
        "WHERE id = ? AND pending_approval = ?",
        (_now(), capture_id, row["pending_approval"]),
    )
    conn.commit()
    return row["pending_approval"] if cur.rowcount == 1 else None


def pending_approvals() -> list[sqlite3.Row]:
    return _connect().execute(
        "SELECT * FROM captures WHERE pending_approval IS NOT NULL ORDER BY created_at"
    ).fetchall()


def claim_next_received() -> sqlite3.Row | None:
    """Atomically move the oldest 'received' capture to 'processing' and
    return it."""
    conn = _connect()
    row = conn.execute(
        "SELECT * FROM captures WHERE status = 'received' ORDER BY created_at LIMIT 1"
    ).fetchone()
    if row is None:
        return None
    cur = conn.execute(
        "UPDATE captures SET status = 'processing', updated_at = ? "
        "WHERE id = ? AND status = 'received'",
        (_now(), row["id"]),
    )
    conn.commit()
    if cur.rowcount != 1:  # someone else claimed it
        return None
    return row


def finish_capture(
    capture_id: str,
    status: str,
    summary: str | None = None,
    tool_trace: str | None = None,
    tokens_used: int | None = None,
) -> None:
    assert status in ("done", "failed")
    conn = _connect()
    conn.execute(
        """UPDATE captures SET status = ?, summary = ?, tool_trace = ?,
           tokens_used = ?, updated_at = ? WHERE id = ?""",
        (status, summary, tool_trace, tokens_used, _now(), capture_id),
    )
    conn.commit()


def requeue_stale_processing() -> int:
    """On startup, put rows stuck in 'processing' (crashed mid-run) back to
    'received' so they get another attempt."""
    conn = _connect()
    cur = conn.execute(
        "UPDATE captures SET status = 'received', updated_at = ? "
        "WHERE status = 'processing'",
        (_now(),),
    )
    conn.commit()
    return cur.rowcount


def get_capture(capture_id: str) -> sqlite3.Row | None:
    return _connect().execute(
        "SELECT * FROM captures WHERE id = ?", (capture_id,)
    ).fetchone()


def queue_depth() -> int:
    row = _connect().execute(
        "SELECT COUNT(*) AS n FROM captures WHERE status IN ('received', 'processing')"
    ).fetchone()
    return row["n"]


def stuck_captures(older_than_ms: int, max_alerts: int) -> list[sqlite3.Row]:
    """Rows that never reached 'done', are older than the cutoff and have
    been reported fewer than max_alerts times — scripts/sweep.py notifies
    about these."""
    cutoff = _now() - older_than_ms
    return _connect().execute(
        "SELECT * FROM captures WHERE status IN ('received', 'failed') "
        "AND created_at < ? AND sweep_alerts < ? ORDER BY created_at",
        (cutoff, max_alerts),
    ).fetchall()


def count_sweep_alert(capture_ids: list[str]) -> None:
    conn = _connect()
    conn.executemany(
        "UPDATE captures SET sweep_alerts = sweep_alerts + 1 WHERE id = ?",
        [(i,) for i in capture_ids],
    )
    conn.commit()


def recent_captures(limit: int = 6, exclude_id: str | None = None) -> list[sqlite3.Row]:
    """Most recent finished captures, newest first — the agent's
    conversation history. Rows still 'received'/'processing' are skipped so
    the capture currently being handled never shows up as its own past."""
    return _connect().execute(
        "SELECT * FROM captures WHERE status IN ('done', 'failed') "
        "AND (? IS NULL OR id != ?) ORDER BY created_at DESC LIMIT ?",
        (exclude_id, exclude_id, limit),
    ).fetchall()


def search_captures(query: str, limit: int = 10) -> list[sqlite3.Row]:
    """Case-insensitive substring match over transcript, summary and tool
    trace of finished captures, newest first."""
    pattern = f"%{query}%"
    return _connect().execute(
        "SELECT * FROM captures WHERE status IN ('done', 'failed') "
        "AND (transcription LIKE ? OR summary LIKE ? OR tool_trace LIKE ?) "
        "ORDER BY created_at DESC LIMIT ?",
        (pattern, pattern, pattern, limit),
    ).fetchall()
