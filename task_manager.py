from __future__ import annotations

import sqlite3
from utils import now_iso
from database import log_event


def create_task(conn: sqlite3.Connection, email_id: int, title: str, description: str, responsible: str, due_date: str | None = None) -> int:
    cur = conn.execute(
        """
        INSERT INTO tarefas(email_id, title, description, responsible, due_date, status, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, 'aberta', ?, ?)
        """,
        (email_id, title, description, responsible, due_date, now_iso(), now_iso()),
    )
    conn.commit()
    task_id = int(cur.lastrowid)
    log_event("INFO", "tarefa_criada", f"Tarefa {task_id} criada para e-mail {email_id}", conn)
    return task_id


def list_tasks(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT t.*, e.subject, e.sender_name, e.sender_email
        FROM tarefas t
        LEFT JOIN emails e ON e.id = t.email_id
        ORDER BY t.status ASC, t.id DESC
        """
    ).fetchall()


def close_task(conn: sqlite3.Connection, task_id: int) -> None:
    conn.execute("UPDATE tarefas SET status='concluida', updated_at=? WHERE id=?", (now_iso(), task_id))
    conn.commit()
