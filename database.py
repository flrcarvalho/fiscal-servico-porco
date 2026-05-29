import sqlite3
import os
import secrets
from datetime import datetime

DB_PATH = os.environ.get("DB_PATH", "fiscal.db")

def get_conn():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_conn()
    c = conn.cursor()

    c.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            telegram_id TEXT UNIQUE NOT NULL,
            telegram_username TEXT,
            created_at TEXT DEFAULT (datetime('now'))
        )
    """)

    c.execute("""
        CREATE TABLE IF NOT EXISTS licenses (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_telegram_id TEXT NOT NULL,
            email TEXT NOT NULL,
            password TEXT NOT NULL,
            label TEXT,
            active INTEGER DEFAULT 1,
            created_at TEXT DEFAULT (datetime('now')),
            FOREIGN KEY (user_telegram_id) REFERENCES users(telegram_id)
        )
    """)

    c.execute("""
        CREATE TABLE IF NOT EXISTS invite_codes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            code TEXT UNIQUE NOT NULL,
            used INTEGER DEFAULT 0,
            used_by TEXT,
            created_at TEXT DEFAULT (datetime('now'))
        )
    """)

    c.execute("""
        CREATE TABLE IF NOT EXISTS monitor_state (
            license_id INTEGER PRIMARY KEY,
            last_bet_id TEXT,
            robot_status TEXT DEFAULT 'UNKNOWN',
            summary_message_id TEXT,
            alert_message_id TEXT,
            last_check TEXT,
            FOREIGN KEY (license_id) REFERENCES licenses(id)
        )
    """)

    conn.commit()
    conn.close()

# ── Invite codes ────────────────────────────────────────────

def create_invite(n=1):
    conn = get_conn()
    codes = []
    for _ in range(n):
        code = secrets.token_hex(4).upper()
        conn.execute("INSERT INTO invite_codes (code) VALUES (?)", (code,))
        codes.append(code)
    conn.commit()
    conn.close()
    return codes

def use_invite(code, telegram_id):
    conn = get_conn()
    row = conn.execute(
        "SELECT * FROM invite_codes WHERE code=? AND used=0", (code.upper(),)
    ).fetchone()
    if not row:
        conn.close()
        return False
    conn.execute(
        "UPDATE invite_codes SET used=1, used_by=? WHERE code=?",
        (telegram_id, code.upper())
    )
    conn.commit()
    conn.close()
    return True

def invite_exists(code):
    conn = get_conn()
    row = conn.execute(
        "SELECT * FROM invite_codes WHERE code=? AND used=0", (code.upper(),)
    ).fetchone()
    conn.close()
    return row is not None

# ── Users ────────────────────────────────────────────────────

def user_exists(telegram_id):
    conn = get_conn()
    row = conn.execute("SELECT id FROM users WHERE telegram_id=?", (str(telegram_id),)).fetchone()
    conn.close()
    return row is not None

def create_user(telegram_id, username=None):
    conn = get_conn()
    conn.execute(
        "INSERT OR IGNORE INTO users (telegram_id, telegram_username) VALUES (?,?)",
        (str(telegram_id), username)
    )
    conn.commit()
    conn.close()

# ── Licenses ─────────────────────────────────────────────────

def add_license(telegram_id, email, password, label=None):
    conn = get_conn()
    cur = conn.execute(
        "INSERT INTO licenses (user_telegram_id, email, password, label) VALUES (?,?,?,?)",
        (str(telegram_id), email, password, label or email)
    )
    lid = cur.lastrowid
    conn.execute(
        "INSERT INTO monitor_state (license_id) VALUES (?)", (lid,)
    )
    conn.commit()
    conn.close()
    return lid

def get_licenses(telegram_id):
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM licenses WHERE user_telegram_id=? AND active=1",
        (str(telegram_id),)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]

def get_all_active_licenses():
    conn = get_conn()
    rows = conn.execute(
        "SELECT l.*, s.last_bet_id, s.robot_status, s.summary_message_id, s.alert_message_id "
        "FROM licenses l "
        "LEFT JOIN monitor_state s ON l.id = s.license_id "
        "WHERE l.active=1"
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]

def remove_license(license_id, telegram_id):
    conn = get_conn()
    conn.execute(
        "UPDATE licenses SET active=0 WHERE id=? AND user_telegram_id=?",
        (license_id, str(telegram_id))
    )
    conn.commit()
    conn.close()

# ── Monitor state ─────────────────────────────────────────────

def update_monitor_state(license_id, **kwargs):
    conn = get_conn()
    fields = ", ".join(f"{k}=?" for k in kwargs)
    values = list(kwargs.values()) + [license_id]
    conn.execute(f"UPDATE monitor_state SET {fields} WHERE license_id=?", values)
    conn.commit()
    conn.close()

def get_monitor_state(license_id):
    conn = get_conn()
    row = conn.execute(
        "SELECT * FROM monitor_state WHERE license_id=?", (license_id,)
    ).fetchone()
    conn.close()
    return dict(row) if row else {}
