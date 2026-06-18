from __future__ import annotations

import sqlite3
from typing import Any

try:
    from openai import OpenAI
except Exception:
    OpenAI = None

from database import log_event
from utils import env, extract_requested_items, find_entities, normalize_for_search, now_iso, remove_quoted_replies


def generate_response(email_row: sqlite3.Row | dict[str, Any]) -> str:
    subject = email_row["subject"]
    body = remove_quoted_replies(email_row["body"])
    sender = email_row["sender_name"] or email_row["sender_email"]
    category = email_row["category"]
    category_key = normalize_for_search(category)
    action = email_row["recommended_action"] or "Responder de forma profissional."
    detected = find_entities(f"{subject}\n{body}")
    items = extract_requested_items(body)

    api_key = env("OPENAI_API_KEY")
    if not api_key or OpenAI is None:
        if category_key == "pedido de orcamento":
            itens = "\n".join(f"- {item['quantidade']} x {item['produto']}" for item in items) or "- itens e quantidades ainda precisam ser confirmados"
            return f"""Olá, {sender}.

Recebemos sua solicitação de orçamento.

Identificamos inicialmente:
{itens}

Para montarmos o orçamento corretamente, por favor confirme os itens, quantidades, medidas/acabamentos desejados e os dados fiscais para cadastro.

Atenciosamente,
Equipe Novaprint"""
        if category in ("Financeiro", "Pedido de boleto", "Pedido de nota fiscal", "Comprovante enviado") or category_key in {
            "financeiro",
            "pedido de boleto",
            "pedido de nota fiscal",
            "comprovante enviado",
        }:
            return f"""Olá, {sender}.

Recebemos sua solicitação financeira referente a: {subject}.

Vamos conferir os dados no sistema antes de enviar boleto, nota fiscal ou confirmação de baixa. Caso tenha CNPJ, número do pedido/orçamento ou comprovante, por favor mantenha essas informações nesta conversa para agilizar a conferência.

Atenciosamente,
Equipe Novaprint"""
        return f"""Olá, {sender}.

Recebemos sua mensagem sobre: {subject}.

Obrigado pelo contato. Vamos verificar as informações e retornar com a tratativa adequada o quanto antes.

Atenciosamente,
Equipe Novaprint"""

    client = OpenAI(api_key=api_key)
    prompt = f"""
Crie uma resposta profissional em português para o cliente da Novaprint.
Não invente números de pedido, boleto, prazo ou nota fiscal.
Se faltar informação, peça de forma objetiva.
Tom cordial, claro e comercial.

Cliente/remetente: {sender}
Categoria: {category}
Ação recomendada: {action}
Dados detectados: {detected}
Itens detectados: {items}
Assunto original: {subject}
Corpo original:
{body[:6000]}
"""
    try:
        resp = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": prompt}],
            temperature=0.3,
        )
        return (resp.choices[0].message.content or "").strip()
    except Exception:
        return f"""Olá, {sender}.

Recebemos sua mensagem e já estamos verificando internamente.

Retornaremos com as informações corretas o quanto antes.

Atenciosamente,
Equipe Novaprint"""


def save_generated_response(conn: sqlite3.Connection, email_id: int, to_email: str, subject: str, body: str) -> int:
    cur = conn.execute(
        """
        INSERT INTO respostas_geradas(email_id, to_email, subject, body, status, created_at, updated_at)
        VALUES (?, ?, ?, ?, 'rascunho', ?, ?)
        """,
        (email_id, to_email, subject, body, now_iso(), now_iso()),
    )
    conn.commit()
    response_id = int(cur.lastrowid)
    log_event("INFO", "resposta_gerada", f"Resposta {response_id} gerada para e-mail {email_id}", conn)
    return response_id


def update_response(conn: sqlite3.Connection, response_id: int, subject: str, body: str, to_email: str | None = None) -> None:
    if to_email is not None:
        conn.execute(
            "UPDATE respostas_geradas SET to_email=?, subject=?, body=?, updated_at=? WHERE id=?",
            (to_email, subject, body, now_iso(), response_id),
        )
        conn.commit()
        return
    conn.execute(
        "UPDATE respostas_geradas SET subject=?, body=?, updated_at=? WHERE id=?",
        (subject, body, now_iso(), response_id),
    )
    conn.commit()


def mark_response_sent(conn: sqlite3.Connection, response_id: int) -> None:
    conn.execute(
        "UPDATE respostas_geradas SET status='enviado', sent_at=?, updated_at=? WHERE id=?",
        (now_iso(), now_iso(), response_id),
    )
    conn.commit()
    log_event("INFO", "email_enviado", f"Resposta {response_id} enviada", conn)


def list_responses(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT r.*, e.sender_name, e.sender_email, e.category
        FROM respostas_geradas r
        JOIN emails e ON e.id = r.email_id
        ORDER BY r.id DESC
        """
    ).fetchall()
