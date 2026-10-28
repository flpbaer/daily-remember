"""Armazenamento em SQLite compartilhado entre o bot e a CLI.

O bot e a CLI são processos separados; o modo WAL permite que a CLI grave
enquanto o bot lê, então dá para criar/editar lembretes com o bot rodando.
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Dict, Iterator, List, Optional

from .config import DB_PATH

SCHEMA = """
CREATE TABLE IF NOT EXISTS reminders (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL,
    channel_id  TEXT NOT NULL,
    title       TEXT,
    content     TEXT NOT NULL,
    mentions    TEXT NOT NULL DEFAULT '[]',
    links       TEXT NOT NULL DEFAULT '[]',
    schedule    TEXT NOT NULL,
    delivery    TEXT NOT NULL DEFAULT 'channel',
    recipients  TEXT NOT NULL DEFAULT '[]',
    active      INTEGER NOT NULL DEFAULT 1,
    next_run_at TEXT,
    last_run_at TEXT,
    send_now    INTEGER NOT NULL DEFAULT 0,
    test_to     TEXT,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS send_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    reminder_id INTEGER NOT NULL,
    sent_at     TEXT NOT NULL,
    status      TEXT NOT NULL,
    detail      TEXT
);
CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS discord_cache (
    kind  TEXT NOT NULL,
    id    TEXT NOT NULL,
    name  TEXT NOT NULL,
    guild TEXT NOT NULL,
    PRIMARY KEY (kind, id)
);
CREATE TABLE IF NOT EXISTS code_reviews (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id           TEXT NOT NULL,
    channel_id         TEXT NOT NULL,
    channel_message_id TEXT,
    author_id          TEXT NOT NULL,
    author_name        TEXT NOT NULL,
    kind               TEXT NOT NULL,
    task_url           TEXT NOT NULL,
    pr_url             TEXT NOT NULL,
    description        TEXT NOT NULL,
    status             TEXT NOT NULL DEFAULT 'pending',
    round              INTEGER NOT NULL DEFAULT 1,
    owner_channel_id   TEXT,
    owner_message_id   TEXT,
    created_at         TEXT NOT NULL,
    updated_at         TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS code_review_events (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    review_id INTEGER NOT NULL,
    at        TEXT NOT NULL,
    status    TEXT NOT NULL,
    by_name   TEXT NOT NULL,
    note      TEXT
);
"""

JSON_FIELDS = ("mentions", "links", "schedule", "recipients")
FIELDS = ("name", "channel_id", "title", "content", "mentions", "links", "schedule",
          "delivery", "recipients")

# Colunas adicionadas depois da primeira versão: bancos antigos ganham as colunas na abertura
MIGRATIONS = {
    "delivery": "ALTER TABLE reminders ADD COLUMN delivery TEXT NOT NULL DEFAULT 'channel'",
    "recipients": "ALTER TABLE reminders ADD COLUMN recipients TEXT NOT NULL DEFAULT '[]'",
    "test_to": "ALTER TABLE reminders ADD COLUMN test_to TEXT",
}

Reminder = Dict[str, Any]


def to_iso(dt: Optional[datetime]) -> Optional[str]:
    """Datas sempre em UTC e no mesmo formato, para poder comparar como texto."""
    if dt is None:
        return None
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def from_iso(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def now_iso() -> str:
    return to_iso(datetime.now(timezone.utc))


@contextmanager
def connect() -> Iterator[sqlite3.Connection]:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH), timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(SCHEMA)
    columns = {row[1] for row in conn.execute("PRAGMA table_info(reminders)")}
    for column, statement in MIGRATIONS.items():
        if column not in columns:
            conn.execute(statement)
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def _decode(row: sqlite3.Row) -> Reminder:
    data = dict(row)
    for field in JSON_FIELDS:
        data[field] = json.loads(data[field])
    return data


def _encode(data: Reminder) -> Dict[str, Any]:
    return {
        f: json.dumps(data[f], ensure_ascii=False) if f in JSON_FIELDS else data.get(f)
        for f in FIELDS
    }


# --- Lembretes -------------------------------------------------------------

def list_reminders() -> List[Reminder]:
    with connect() as conn:
        rows = conn.execute("SELECT * FROM reminders ORDER BY id").fetchall()
    return [_decode(r) for r in rows]


def get_reminder(reminder_id: int) -> Optional[Reminder]:
    with connect() as conn:
        row = conn.execute("SELECT * FROM reminders WHERE id = ?", (reminder_id,)).fetchone()
    return _decode(row) if row else None


def create_reminder(data: Reminder, next_run: Optional[datetime]) -> int:
    values = _encode(data)
    now = now_iso()
    with connect() as conn:
        cur = conn.execute(
            f"INSERT INTO reminders ({', '.join(FIELDS)}, active, next_run_at, created_at, updated_at) "
            f"VALUES ({', '.join('?' for _ in FIELDS)}, ?, ?, ?, ?)",
            (*values.values(), 1 if next_run else 0, to_iso(next_run), now, now),
        )
        return cur.lastrowid


def update_reminder(reminder_id: int, data: Reminder, next_run: Optional[datetime], active: bool) -> None:
    values = _encode(data)
    assignments = ", ".join(f"{f} = ?" for f in FIELDS)
    with connect() as conn:
        conn.execute(
            f"UPDATE reminders SET {assignments}, active = ?, next_run_at = ?, updated_at = ? WHERE id = ?",
            (*values.values(), 1 if active and next_run else 0,
             to_iso(next_run) if active else None, now_iso(), reminder_id),
        )


def set_active(reminder_id: int, active: bool, next_run: Optional[datetime]) -> None:
    with connect() as conn:
        conn.execute(
            "UPDATE reminders SET active = ?, next_run_at = ?, updated_at = ? WHERE id = ?",
            (1 if active else 0, to_iso(next_run) if active else None, now_iso(), reminder_id),
        )


def delete_reminder(reminder_id: int) -> None:
    with connect() as conn:
        conn.execute("DELETE FROM reminders WHERE id = ?", (reminder_id,))


def request_send_now(reminder_id: int) -> None:
    with connect() as conn:
        conn.execute("UPDATE reminders SET send_now = 1 WHERE id = ?", (reminder_id,))


def request_test(reminder_id: int, user_id: str) -> None:
    with connect() as conn:
        conn.execute("UPDATE reminders SET test_to = ? WHERE id = ?", (user_id, reminder_id))


def due_reminders(now: datetime) -> List[Reminder]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM reminders WHERE send_now = 1 OR test_to IS NOT NULL "
            "OR (active = 1 AND next_run_at IS NOT NULL AND next_run_at <= ?) ORDER BY id",
            (to_iso(now),),
        ).fetchall()
    return [_decode(r) for r in rows]


def finish_dispatch(reminder: Reminder, sent_at: Optional[datetime], advance: bool,
                    next_run: Optional[datetime]) -> None:
    """Marca o envio e avança o agendamento.

    O avanço só acontece se o próximo envio não mudou desde a leitura, para não
    sobrescrever uma edição feita pela CLI no meio do caminho.
    """
    with connect() as conn:
        conn.execute("UPDATE reminders SET send_now = 0, test_to = NULL WHERE id = ?", (reminder["id"],))
        if sent_at:
            conn.execute("UPDATE reminders SET last_run_at = ? WHERE id = ?",
                         (to_iso(sent_at), reminder["id"]))
        if advance:
            conn.execute(
                "UPDATE reminders SET next_run_at = ?, active = ? WHERE id = ? AND next_run_at = ?",
                (to_iso(next_run), 1 if next_run else 0, reminder["id"], reminder["next_run_at"]),
            )


# --- Histórico -------------------------------------------------------------

def log_send(reminder_id: int, status: str, detail: str = "") -> None:
    with connect() as conn:
        conn.execute(
            "INSERT INTO send_log (reminder_id, sent_at, status, detail) VALUES (?, ?, ?, ?)",
            (reminder_id, now_iso(), status, detail),
        )


def history(reminder_id: Optional[int] = None, limit: int = 20) -> List[Dict[str, Any]]:
    query = ("SELECT l.*, r.name FROM send_log l LEFT JOIN reminders r ON r.id = l.reminder_id")
    params: tuple = ()
    if reminder_id is not None:
        query += " WHERE l.reminder_id = ?"
        params = (reminder_id,)
    query += " ORDER BY l.id DESC LIMIT ?"
    with connect() as conn:
        rows = conn.execute(query, (*params, limit)).fetchall()
    return [dict(r) for r in rows]


# --- Configurações ---------------------------------------------------------

def get_setting(key: str) -> Optional[str]:
    with connect() as conn:
        row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else None


def set_setting(key: str, value: str) -> None:
    with connect() as conn:
        conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (key, value))


# --- Cache de canais/cargos/membros (preenchido pelo bot, usado pela CLI) ---

def replace_cache(kind: str, items: List[Dict[str, str]]) -> None:
    with connect() as conn:
        conn.execute("DELETE FROM discord_cache WHERE kind = ?", (kind,))
        conn.executemany(
            "INSERT OR REPLACE INTO discord_cache (kind, id, name, guild) VALUES (?, ?, ?, ?)",
            [(kind, i["id"], i["name"], i["guild"]) for i in items],
        )


def list_cache(kind: str) -> List[Dict[str, str]]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT id, name, guild FROM discord_cache WHERE kind = ? ORDER BY guild, name", (kind,)
        ).fetchall()
    return [dict(r) for r in rows]


# --- Code reviews ----------------------------------------------------------

OPEN_REVIEW_STATUSES = ("pending", "in_review", "changes_requested")


def create_review(guild_id: int, channel_id: int, author_id: int, author_name: str, kind: str,
                  task_url: str, pr_url: str, description: str) -> int:
    now = now_iso()
    with connect() as conn:
        cur = conn.execute(
            "INSERT INTO code_reviews (guild_id, channel_id, author_id, author_name, kind, task_url, "
            "pr_url, description, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (str(guild_id), str(channel_id), str(author_id), author_name, kind, task_url, pr_url,
             description, now, now),
        )
        conn.execute(
            "INSERT INTO code_review_events (review_id, at, status, by_name) VALUES (?, ?, 'pending', ?)",
            (cur.lastrowid, now, author_name),
        )
        return cur.lastrowid


def get_review(review_id: int) -> Optional[Dict[str, Any]]:
    with connect() as conn:
        row = conn.execute("SELECT * FROM code_reviews WHERE id = ?", (review_id,)).fetchone()
        if not row:
            return None
        events = conn.execute(
            "SELECT at, status, by_name, note FROM code_review_events WHERE review_id = ? ORDER BY id",
            (review_id,),
        ).fetchall()
    review = dict(row)
    review["events"] = [dict(e) for e in events]
    return review


def list_reviews(open_only: bool = True) -> List[Dict[str, Any]]:
    query = "SELECT * FROM code_reviews"
    params: tuple = ()
    if open_only:
        query += f" WHERE status IN ({', '.join('?' for _ in OPEN_REVIEW_STATUSES)})"
        params = OPEN_REVIEW_STATUSES
    with connect() as conn:
        rows = conn.execute(query + " ORDER BY id", params).fetchall()
    return [dict(r) for r in rows]


def add_review_event(review_id: int, status: str, by_name: str, note: Optional[str] = None,
                     new_round: bool = False) -> None:
    now = now_iso()
    with connect() as conn:
        conn.execute(
            "UPDATE code_reviews SET status = ?, updated_at = ?, round = round + ? WHERE id = ?",
            (status, now, 1 if new_round else 0, review_id),
        )
        conn.execute(
            "INSERT INTO code_review_events (review_id, at, status, by_name, note) VALUES (?, ?, ?, ?, ?)",
            (review_id, now, status, by_name, note or None),
        )


def set_review_card(review_id: int, channel_id: int, message_id: int) -> None:
    """Onde está o card do CR: a thread do ticket (ou o canal, se não deu para criar a thread)."""
    with connect() as conn:
        conn.execute("UPDATE code_reviews SET channel_id = ?, channel_message_id = ? WHERE id = ?",
                     (str(channel_id), str(message_id), review_id))


def set_review_owner_message(review_id: int, channel_id: int, message_id: int) -> None:
    with connect() as conn:
        conn.execute("UPDATE code_reviews SET owner_channel_id = ?, owner_message_id = ? WHERE id = ?",
                     (str(channel_id), str(message_id), review_id))
