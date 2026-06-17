from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, Iterable

from utils import now_iso

DB_PATH = Path("central_emails_novaprint.sqlite3")


def get_connection(db_path: Path | str = DB_PATH) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def init_db(conn: sqlite3.Connection | None = None) -> None:
    close = False
    if conn is None:
        conn = get_connection()
        close = True
    cur = conn.cursor()
    cur.executescript(
        """
        CREATE TABLE IF NOT EXISTS emails (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            message_id TEXT UNIQUE NOT NULL,
            imap_uid TEXT,
            sender_name TEXT,
            sender_email TEXT,
            subject TEXT,
            date TEXT,
            body TEXT,
            attachments TEXT,
            is_unread INTEGER DEFAULT 0,
            category TEXT DEFAULT 'Outros',
            summary TEXT,
            urgency INTEGER DEFAULT 1,
            recommended_action TEXT,
            responsible TEXT,
            detected_json TEXT,
            status TEXT DEFAULT 'novo',
            observation TEXT,
            created_at TEXT,
            updated_at TEXT
        );

        CREATE TABLE IF NOT EXISTS clientes_detectados (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            email_id INTEGER,
            sender_email TEXT,
            sender_name TEXT,
            cnpj TEXT,
            telefone TEXT,
            numero_pedido TEXT,
            numero_orcamento TEXT,
            valor TEXT,
            created_at TEXT,
            FOREIGN KEY(email_id) REFERENCES emails(id)
        );

        CREATE TABLE IF NOT EXISTS tarefas (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            email_id INTEGER,
            title TEXT NOT NULL,
            description TEXT,
            responsible TEXT,
            due_date TEXT,
            status TEXT DEFAULT 'aberta',
            created_at TEXT,
            updated_at TEXT,
            FOREIGN KEY(email_id) REFERENCES emails(id)
        );

        CREATE TABLE IF NOT EXISTS respostas_geradas (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            email_id INTEGER NOT NULL,
            to_email TEXT,
            subject TEXT,
            body TEXT,
            status TEXT DEFAULT 'rascunho',
            sent_at TEXT,
            created_at TEXT,
            updated_at TEXT,
            FOREIGN KEY(email_id) REFERENCES emails(id)
        );

        CREATE TABLE IF NOT EXISTS logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            level TEXT,
            action TEXT,
            details TEXT,
            created_at TEXT
        );
        """
    )
    conn.commit()
    if close:
        conn.close()


def log_event(level: str, action: str, details: str, conn: sqlite3.Connection | None = None) -> None:
    close = False
    if conn is None:
        conn = get_connection()
        close = True
    conn.execute(
        "INSERT INTO logs(level, action, details, created_at) VALUES (?, ?, ?, ?)",
        (level, action, details, now_iso()),
    )
    conn.commit()
    if close:
        conn.close()


def upsert_email(email_data: dict[str, Any], conn: sqlite3.Connection | None = None) -> bool:
    close = False
    if conn is None:
        conn = get_connection()
        close = True
    now = now_iso()
    try:
        conn.execute(
            """
            INSERT INTO emails(
                message_id, imap_uid, sender_name, sender_email, subject, date, body,
                attachments, is_unread, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                email_data["message_id"],
                email_data.get("imap_uid"),
                email_data.get("sender_name"),
                email_data.get("sender_email"),
                email_data.get("subject"),
                email_data.get("date"),
                email_data.get("body"),
                json.dumps(email_data.get("attachments", []), ensure_ascii=False),
                1 if email_data.get("is_unread") else 0,
                now,
                now,
            ),
        )
        conn.commit()
        inserted = True
    except sqlite3.IntegrityError:
        inserted = False
    if close:
        conn.close()
    return inserted


def update_classification(email_id: int, classification: dict[str, Any], conn: sqlite3.Connection | None = None) -> None:
    close = False
    if conn is None:
        conn = get_connection()
        close = True
    detected = classification.get("detected", {})
    conn.execute(
        """
        UPDATE emails
        SET category=?, summary=?, urgency=?, recommended_action=?, responsible=?, detected_json=?, updated_at=?
        WHERE id=?
        """,
        (
            classification.get("category", "Outros"),
            classification.get("summary", ""),
            int(classification.get("urgency", 1)),
            classification.get("recommended_action", ""),
            classification.get("responsible", "suporte"),
            json.dumps(detected, ensure_ascii=False),
            now_iso(),
            email_id,
        ),
    )
    conn.execute("DELETE FROM clientes_detectados WHERE email_id=?", (email_id,))
    conn.execute(
        """
        INSERT INTO clientes_detectados(email_id, sender_email, sender_name, cnpj, telefone, numero_pedido, numero_orcamento, valor, created_at)
        SELECT id, sender_email, sender_name, ?, ?, ?, ?, ?, ? FROM emails WHERE id=?
        """,
        (
            ", ".join(detected.get("cnpj", [])),
            ", ".join(detected.get("telefone", [])),
            ", ".join(detected.get("numero_pedido", [])),
            ", ".join(detected.get("numero_orcamento", [])),
            ", ".join(detected.get("valor", [])),
            now_iso(),
            email_id,
        ),
    )
    conn.commit()
    log_event("INFO", "classificacao", f"E-mail {email_id} classificado como {classification.get('category')}", conn)
    if close:
        conn.close()


def list_emails(filters: dict[str, Any] | None = None, conn: sqlite3.Connection | None = None) -> list[sqlite3.Row]:
    close = False
    if conn is None:
        conn = get_connection()
        close = True
    filters = filters or {}
    clauses = []
    params: list[Any] = []
    if filters.get("category") and filters["category"] != "Todos":
        clauses.append("category=?")
        params.append(filters["category"])
    if filters.get("urgency") and filters["urgency"] != "Todas":
        clauses.append("urgency=?")
        params.append(int(filters["urgency"]))
    if filters.get("sender"):
        clauses.append("(sender_email LIKE ? OR sender_name LIKE ?)")
        params.extend([f"%{filters['sender']}%", f"%{filters['sender']}%"])
    if filters.get("status") and filters["status"] != "Todos":
        clauses.append("status=?")
        params.append(filters["status"])
    if filters.get("date_start"):
        clauses.append("substr(date, 1, 10) >= ?")
        params.append(str(filters["date_start"]))
    if filters.get("date_end"):
        clauses.append("substr(date, 1, 10) <= ?")
        params.append(str(filters["date_end"]))
    where = " WHERE " + " AND ".join(clauses) if clauses else ""
    sort = filters.get("sort", "newest")
    if sort == "oldest":
        order_by = "date ASC, id ASC"
    elif sort == "urgent":
        order_by = "urgency DESC, date DESC, id DESC"
    else:
        order_by = "date DESC, id DESC"
    rows = conn.execute(f"SELECT * FROM emails{where} ORDER BY {order_by}", params).fetchall()
    if close:
        conn.close()
    return rows


def get_email(email_id: int, conn: sqlite3.Connection | None = None) -> sqlite3.Row | None:
    close = False
    if conn is None:
        conn = get_connection()
        close = True
    row = conn.execute("SELECT * FROM emails WHERE id=?", (email_id,)).fetchone()
    if close:
        conn.close()
    return row


def update_email_status(email_id: int, status: str, conn: sqlite3.Connection | None = None) -> None:
    close = False
    if conn is None:
        conn = get_connection()
        close = True
    conn.execute("UPDATE emails SET status=?, updated_at=? WHERE id=?", (status, now_iso(), email_id))
    conn.commit()
    log_event("INFO", "status", f"E-mail {email_id} marcado como {status}", conn)
    if close:
        conn.close()


def update_observation(email_id: int, observation: str, conn: sqlite3.Connection | None = None) -> None:
    close = False
    if conn is None:
        conn = get_connection()
        close = True
    conn.execute("UPDATE emails SET observation=?, updated_at=? WHERE id=?", (observation, now_iso(), email_id))
    conn.commit()
    if close:
        conn.close()


def list_logs(limit: int = 200, conn: sqlite3.Connection | None = None) -> list[sqlite3.Row]:
    close = False
    if conn is None:
        conn = get_connection()
        close = True
    rows = conn.execute("SELECT * FROM logs ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    if close:
        conn.close()
    return rows
