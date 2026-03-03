import os
import sqlite3
import json
from datetime import datetime
from config import DATABASE_PATH


def get_conn():
    db_dir = os.path.dirname(DATABASE_PATH)
    if db_dir:
        os.makedirs(db_dir, exist_ok=True)
    conn = sqlite3.connect(DATABASE_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_conn()
    c = conn.cursor()

    c.execute("""
        CREATE TABLE IF NOT EXISTS users (
            telegram_id   INTEGER PRIMARY KEY,
            username      TEXT,
            first_seen    TEXT DEFAULT (datetime('now')),
            last_active   TEXT DEFAULT (datetime('now'))
        )
    """)

    c.execute("""
        CREATE TABLE IF NOT EXISTS requests (
            id                 INTEGER PRIMARY KEY AUTOINCREMENT,
            telegram_id        INTEGER NOT NULL,
            ig_username        TEXT NOT NULL,
            result_data        TEXT,
            following_count    INTEGER DEFAULT 0,
            non_follower_count INTEGER DEFAULT 0,
            payment_ref        TEXT,
            payment_method     TEXT,
            status             TEXT DEFAULT 'pending_payment',
            created_at         TEXT DEFAULT (datetime('now')),
            updated_at         TEXT DEFAULT (datetime('now')),
            FOREIGN KEY (telegram_id) REFERENCES users(telegram_id)
        )
    """)

    # Migrate existing databases that don't yet have payment_method column
    existing_cols = [r[1] for r in c.execute("PRAGMA table_info(requests)").fetchall()]
    if "payment_method" not in existing_cols:
        c.execute("ALTER TABLE requests ADD COLUMN payment_method TEXT")

    conn.commit()
    conn.close()


# ── Users ────────────────────────────────────────────────

def upsert_user(telegram_id: int, username: str):
    conn = get_conn()
    conn.execute("""
        INSERT INTO users (telegram_id, username, last_active)
        VALUES (?, ?, datetime('now'))
        ON CONFLICT(telegram_id) DO UPDATE SET
            username    = excluded.username,
            last_active = datetime('now')
    """, (telegram_id, username))
    conn.commit()
    conn.close()


def get_all_users():
    conn = get_conn()
    rows = conn.execute("SELECT * FROM users ORDER BY first_seen DESC").fetchall()
    conn.close()
    return [dict(r) for r in rows]


# ── Requests ─────────────────────────────────────────────

def create_request(telegram_id: int, ig_username: str,
                   non_followers: list, following_count: int) -> int:
    conn = get_conn()
    c = conn.cursor()
    c.execute("""
        INSERT INTO requests
            (telegram_id, ig_username, result_data, following_count, non_follower_count)
        VALUES (?, ?, ?, ?, ?)
    """, (
        telegram_id,
        ig_username,
        json.dumps(non_followers),
        following_count,
        len(non_followers)
    ))
    request_id = c.lastrowid
    conn.commit()
    conn.close()
    return request_id


def update_payment_ref(request_id: int, payment_ref: str, payment_method: str = None):
    conn = get_conn()
    conn.execute("""
        UPDATE requests
        SET payment_ref = ?, payment_method = ?, status = 'payment_submitted',
            updated_at = datetime('now')
        WHERE id = ?
    """, (payment_ref, payment_method, request_id))
    conn.commit()
    conn.close()


def update_request_status(request_id: int, status: str):
    conn = get_conn()
    conn.execute("""
        UPDATE requests
        SET status = ?, updated_at = datetime('now')
        WHERE id = ?
    """, (status, request_id))
    conn.commit()
    conn.close()


def get_request(request_id: int) -> dict | None:
    conn = get_conn()
    row = conn.execute("SELECT * FROM requests WHERE id = ?", (request_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


def get_all_requests(status_filter: str = None):
    conn = get_conn()
    if status_filter:
        rows = conn.execute("""
            SELECT r.*, u.username as tg_username
            FROM requests r
            LEFT JOIN users u ON r.telegram_id = u.telegram_id
            WHERE r.status = ?
            ORDER BY r.created_at DESC
        """, (status_filter,)).fetchall()
    else:
        rows = conn.execute("""
            SELECT r.*, u.username as tg_username
            FROM requests r
            LEFT JOIN users u ON r.telegram_id = u.telegram_id
            ORDER BY r.created_at DESC
        """).fetchall()
    conn.close()
    return [dict(r) for r in rows]


# ── Analytics ─────────────────────────────────────────────

def get_stats() -> dict:
    conn = get_conn()
    c = conn.cursor()

    total_users       = c.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    total_requests    = c.execute("SELECT COUNT(*) FROM requests").fetchone()[0]
    pending           = c.execute("SELECT COUNT(*) FROM requests WHERE status = 'payment_submitted'").fetchone()[0]
    approved          = c.execute("SELECT COUNT(*) FROM requests WHERE status = 'approved'").fetchone()[0]
    rejected          = c.execute("SELECT COUNT(*) FROM requests WHERE status = 'rejected'").fetchone()[0]

    # Daily signups for last 7 days
    daily = c.execute("""
        SELECT DATE(created_at) as day, COUNT(*) as cnt
        FROM requests
        WHERE created_at >= DATE('now', '-6 days')
        GROUP BY day
        ORDER BY day
    """).fetchall()

    conn.close()
    return {
        "total_users":    total_users,
        "total_requests": total_requests,
        "pending":        pending,
        "approved":       approved,
        "rejected":       rejected,
        "daily":          [dict(r) for r in daily],
    }


# Initialise on import
init_db()
