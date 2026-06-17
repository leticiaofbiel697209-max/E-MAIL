from __future__ import annotations

import json
import sqlite3

import streamlit as st

from ai_classifier import classify_email
from database import (
    get_connection,
    init_db,
    list_emails,
    list_logs,
    log_event,
    update_classification,
    update_email_status,
    update_observation,
)
from email_client import EmailClient, send_email_smtp
from response_generator import (
    generate_response,
    list_responses,
    mark_response_sent,
    save_generated_response,
    update_response,
)
from task_manager import close_task, create_task, list_tasks
from utils import CATEGORIES, env

st.set_page_config(page_title="Central de E-mails Novaprint", page_icon="Email", layout="wide")


def get_conn() -> sqlite3.Connection:
    conn = get_connection()
    init_db(conn)
    return conn


@st.cache_data(ttl=30)
def cached_counts() -> dict[str, int]:
    conn = get_connection()
    init_db(conn)
    rows = conn.execute("SELECT category, COUNT(*) total FROM emails GROUP BY category").fetchall()
    open_tasks = conn.execute("SELECT COUNT(*) total FROM tarefas WHERE status='aberta'").fetchone()["total"]
    conn.close()
    return {**{r["category"]: r["total"] for r in rows}, "tarefas_abertas": open_tasks}


def process_new_emails() -> None:
    conn = get_conn()
    try:
        client = EmailClient()
        fetched = client.fetch_recent_and_unread(days=7)
        inserted = 0
        classified = 0
        from database import upsert_email

        for email_data in fetched:
            if upsert_email(email_data, conn):
                inserted += 1

        rows = conn.execute("SELECT * FROM emails WHERE summary IS NULL OR summary='' ORDER BY id DESC").fetchall()
        for row in rows:
            classification = classify_email(row["subject"], row["body"])
            update_classification(row["id"], classification, conn)
            classified += 1

        log_event("INFO", "processamento", f"{inserted} e-mails novos, {classified} classificados", conn)
        st.cache_data.clear()
        st.success(f"Processamento concluído: {inserted} novos e-mails, {classified} classificados.")
    except Exception as exc:
        log_event("ERROR", "processamento", str(exc), conn)
        st.error(f"Erro ao processar e-mails: {exc}")
    finally:
        conn.close()


def email_card(conn: sqlite3.Connection, row: sqlite3.Row) -> None:
    urgency = int(row["urgency"] or 1)
    with st.container(border=True):
        top = st.columns([3, 1, 1])
        top[0].markdown(f"### {row['subject']}")
        top[1].markdown(f"**Categoria:** {row['category'] or 'Outros'}")
        top[2].markdown(f"**Urgência:** {urgency}/5")
        unread_label = "Não lido" if row["is_unread"] else "Lido"
        st.caption(
            f"De: {row['sender_name']} <{row['sender_email']}> | "
            f"Data: {row['date']} | Status: {row['status']} | {unread_label}"
        )
        st.write(row["summary"] or (row["body"] or "")[:300])
        st.info(
            f"Ação recomendada: {row['recommended_action'] or 'Sem recomendação'} | "
            f"Responsável: {row['responsible'] or '-'}"
        )

        detected = json.loads(row["detected_json"] or "{}")
        chips = []
        for key, values in detected.items():
            if values:
                chips.append(f"**{key}:** {', '.join(values)}")
        if chips:
            st.markdown(" | ".join(chips))

        if row["attachments"]:
            try:
                attachments = json.loads(row["attachments"])
                if attachments:
                    st.caption("Anexos: " + ", ".join(a.get("filename", "anexo") for a in attachments))
            except Exception:
                pass

        with st.expander("Ver corpo do e-mail"):
            st.text_area("Corpo", row["body"] or "", height=220, disabled=True, key=f"body_{row['id']}")

        obs = st.text_area("Observação manual", row["observation"] or "", key=f"obs_{row['id']}")
        c1, c2, c3, c4 = st.columns(4)
        if c1.button("Salvar observação", key=f"save_obs_{row['id']}"):
            update_observation(row["id"], obs, conn)
            st.success("Observação salva.")
        if c2.button("Marcar como resolvido", key=f"resolved_{row['id']}"):
            update_email_status(row["id"], "resolvido", conn)
            st.cache_data.clear()
            st.rerun()
        if c3.button("Gerar resposta", key=f"gen_{row['id']}"):
            body = generate_response(row)
            subject = f"Re: {row['subject']}" if not row["subject"].lower().startswith("re:") else row["subject"]
            save_generated_response(conn, row["id"], row["sender_email"], subject, body)
            st.cache_data.clear()
            st.success("Resposta gerada em rascunho.")
        if c4.button("Criar tarefa", key=f"task_save_{row['id']}"):
            title = row["recommended_action"] or row["subject"]
            create_task(conn, row["id"], title, row["summary"] or "", row["responsible"] or "suporte", None)
            st.success("Tarefa criada.")


def inbox_tab(default_filter: str | None = None) -> None:
    conn = get_conn()
    key_prefix = default_filter or "todos"
    st.subheader("Caixa de Entrada Inteligente")
    c1, c2, c3, c4, c5 = st.columns([1, 1, 1, 1, 1])
    if c1.button("Processar novos e-mails", type="primary", key=f"process_{key_prefix}"):
        process_new_emails()
    category_options = ["Todos"] + CATEGORIES
    default_idx = category_options.index(default_filter) if default_filter in category_options else 0
    category = c2.selectbox("Categoria", category_options, index=default_idx, key=f"category_{key_prefix}")
    urgency = c3.selectbox("Urgência", ["Todas", 1, 2, 3, 4, 5], key=f"urgency_{key_prefix}")
    status = c4.selectbox("Status", ["Todos", "novo", "resolvido"], key=f"status_{key_prefix}")
    sender = c5.text_input("Remetente", key=f"sender_{key_prefix}")
    rows = list_emails({"category": category, "urgency": urgency, "sender": sender, "status": status}, conn)
    st.caption(f"{len(rows)} e-mails encontrados")
    for row in rows:
        email_card(conn, row)
    conn.close()


def responses_tab() -> None:
    conn = get_conn()
    st.subheader("Respostas Geradas")
    rows = list_responses(conn)
    if not rows:
        st.info("Nenhuma resposta gerada ainda.")
    for r in rows:
        with st.container(border=True):
            st.markdown(f"### {r['subject']}")
            st.caption(f"Para: {r['to_email']} | Status: {r['status']} | Categoria: {r['category']}")
            subject = st.text_input("Assunto", r["subject"], key=f"resp_subject_{r['id']}")
            body = st.text_area("Resposta", r["body"], height=260, key=f"resp_body_{r['id']}")
            c1, c2 = st.columns(2)
            if c1.button("Salvar edição", key=f"resp_save_{r['id']}"):
                update_response(conn, r["id"], subject, body)
                st.success("Rascunho atualizado.")
            st.code(body, language="text")
            confirm = st.checkbox("Tem certeza que deseja enviar este e-mail?", key=f"confirm_{r['id']}")
            if c2.button("Enviar resposta", key=f"send_{r['id']}", disabled=(not confirm or r["status"] == "enviado")):
                try:
                    update_response(conn, r["id"], subject, body)
                    original = conn.execute("SELECT message_id FROM emails WHERE id=?", (r["email_id"],)).fetchone()
                    send_email_smtp(r["to_email"], subject, body, original["message_id"] if original else None)
                    mark_response_sent(conn, r["id"])
                    st.success("E-mail enviado com confirmação manual.")
                except Exception as exc:
                    log_event("ERROR", "envio_email", str(exc), conn)
                    st.error(f"Erro ao enviar: {exc}")
    conn.close()


def tasks_tab() -> None:
    conn = get_conn()
    st.subheader("Tarefas")
    rows = list_tasks(conn)
    if not rows:
        st.info("Nenhuma tarefa criada.")
    for row in rows:
        with st.container(border=True):
            st.markdown(f"**{row['title']}**")
            st.caption(
                f"Responsável: {row['responsible']} | Prazo: {row['due_date'] or '-'} | "
                f"Status: {row['status']} | Cliente: {row['sender_name']} <{row['sender_email']}>"
            )
            st.write(row["description"] or "")
            if row["status"] != "concluida" and st.button("Concluir tarefa", key=f"close_task_{row['id']}"):
                close_task(conn, row["id"])
                st.rerun()
    conn.close()


def config_tab() -> None:
    st.subheader("Configurações")
    st.write("O sistema usa as variáveis do arquivo `.env`. A senha nunca deve ser colocada no código.")
    config = [
        {"Variável": "EMAIL_IMAP_HOST", "Status/valor": env("EMAIL_IMAP_HOST", "não configurado")},
        {"Variável": "EMAIL_IMAP_PORT", "Status/valor": env("EMAIL_IMAP_PORT", "993")},
        {"Variável": "EMAIL_SMTP_HOST", "Status/valor": env("EMAIL_SMTP_HOST", "não configurado")},
        {"Variável": "EMAIL_SMTP_PORT", "Status/valor": env("EMAIL_SMTP_PORT", "587")},
        {"Variável": "EMAIL_USER", "Status/valor": env("EMAIL_USER", "não configurado")},
        {"Variável": "EMAIL_PASSWORD", "Status/valor": "configurado" if env("EMAIL_PASSWORD") else "não configurado"},
        {"Variável": "OPENAI_API_KEY", "Status/valor": "configurado" if env("OPENAI_API_KEY") else "não configurado - usando fallback por regras"},
    ]
    st.dataframe(config, use_container_width=True, hide_index=True)
    st.divider()
    st.markdown("### Integrações futuras preparadas")
    st.write("Gestão Click API | CRM Inteligente | Gerador de Orçamentos | Portal das Vendedoras | Rotas de entrega")


def logs_tab() -> None:
    conn = get_conn()
    st.subheader("Logs")
    rows = list_logs(conn=conn)
    if rows:
        st.dataframe([dict(r) for r in rows], use_container_width=True, hide_index=True)
    else:
        st.info("Sem logs ainda.")
    conn.close()


def main() -> None:
    st.title("Central de E-mails Novaprint")
    st.caption("MVP local para IMAP/SMTP, classificação inteligente, tarefas e respostas com confirmação manual.")
    counts = cached_counts()
    m1, m2, m3, m4 = st.columns(4)
    m1.metric("Orçamentos", counts.get("Pedido de orçamento", 0))
    m2.metric("Financeiro", counts.get("Financeiro", 0) + counts.get("Pedido de boleto", 0) + counts.get("Pedido de nota fiscal", 0))
    m3.metric("Urgentes", counts.get("Urgente", 0))
    m4.metric("Tarefas abertas", counts.get("tarefas_abertas", 0))

    tabs = st.tabs(
        [
            "Caixa de Entrada Inteligente",
            "Urgentes",
            "Pedidos de Orçamento",
            "Financeiro",
            "Entregas",
            "Respostas Geradas",
            "Configurações",
            "Logs",
        ]
    )
    with tabs[0]:
        inbox_tab()
    with tabs[1]:
        inbox_tab("Urgente")
    with tabs[2]:
        inbox_tab("Pedido de orçamento")
    with tabs[3]:
        inbox_tab("Financeiro")
        tasks_tab()
    with tabs[4]:
        inbox_tab("Cobrança de entrega")
    with tabs[5]:
        responses_tab()
    with tabs[6]:
        config_tab()
    with tabs[7]:
        logs_tab()


if __name__ == "__main__":
    main()
