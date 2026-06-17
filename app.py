from __future__ import annotations

import json
import sqlite3
from datetime import date, timedelta
from typing import Any

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
from gestao_click import GestaoClickClient, build_quote_payload, validate_quote_payload
from response_generator import (
    generate_response,
    list_responses,
    mark_response_sent,
    save_generated_response,
    update_response,
)
from task_manager import close_task, create_task, list_tasks
from utils import CATEGORIES, env, extract_requested_items, find_entities, remove_quoted_replies, repair_mojibake

st.set_page_config(page_title="Central de E-mails Novaprint", page_icon="Email", layout="wide")

FINANCE_CATEGORIES = {"Financeiro", "Pedido de boleto", "Pedido de nota fiscal", "Comprovante enviado"}


def txt(value: Any) -> str:
    return repair_mojibake("" if value is None else str(value))


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
    return {**{txt(r["category"]): r["total"] for r in rows}, "tarefas_abertas": open_tasks}


def process_new_emails(days: int, include_old_unread: bool) -> None:
    conn = get_conn()
    try:
        client = EmailClient()
        fetched = client.fetch_recent_and_unread(days=days, include_old_unread=include_old_unread)
        inserted = 0
        classified = 0
        from database import upsert_email

        for email_data in fetched:
            if upsert_email(email_data, conn):
                inserted += 1

        rows = conn.execute("SELECT * FROM emails WHERE summary IS NULL OR summary='' ORDER BY id DESC").fetchall()
        for row in rows:
            classification = classify_email(txt(row["subject"]), txt(row["body"]))
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


def filtered_rows(conn: sqlite3.Connection, filters: dict[str, Any], group: str | None = None) -> list[sqlite3.Row]:
    rows = list_emails(filters, conn)
    if group == "financeiro":
        rows = [r for r in rows if txt(r["category"]) in FINANCE_CATEGORIES]
    if group == "orcamento":
        rows = [r for r in rows if txt(r["category"]) == "Pedido de orçamento"]
    return rows


def reclassify_rows(conn: sqlite3.Connection, rows: list[sqlite3.Row]) -> int:
    total = 0
    for row in rows:
        classification = classify_email(txt(row["subject"]), txt(row["body"]))
        update_classification(row["id"], classification, conn)
        total += 1
    log_event("INFO", "reclassificacao", f"{total} e-mails reclassificados", conn)
    st.cache_data.clear()
    return total


def email_card(conn: sqlite3.Connection, row: sqlite3.Row, key_prefix: str, integration_mode: str | None = None) -> None:
    urgency = int(row["urgency"] or 1)
    subject = txt(row["subject"])
    category = txt(row["category"] or "Outros")
    body = txt(row["body"])
    main_body = remove_quoted_replies(body)

    with st.container(border=True):
        top = st.columns([3, 1, 1])
        top[0].markdown(f"### {subject}")
        top[1].markdown(f"**Categoria:** {category}")
        top[2].markdown(f"**Urgência:** {urgency}/5")
        unread_label = "Não lido" if row["is_unread"] else "Lido"
        st.caption(
            f"De: {txt(row['sender_name'])} <{txt(row['sender_email'])}> | "
            f"Data: {txt(row['date'])} | Status: {txt(row['status'])} | {unread_label}"
        )
        st.write(txt(row["summary"]) or main_body[:300])
        st.info(
            f"Ação recomendada: {txt(row['recommended_action']) or 'Sem recomendação'} | "
            f"Responsável: {txt(row['responsible']) or '-'}"
        )

        detected = json.loads(row["detected_json"] or "{}")
        chips = []
        for key, values in detected.items():
            if values and key != "itens_solicitados":
                chips.append(f"**{key}:** {', '.join(map(txt, values))}")
        if chips:
            st.markdown(" | ".join(chips))

        if row["attachments"]:
            try:
                attachments = json.loads(row["attachments"])
                if attachments:
                    st.caption("Anexos: " + ", ".join(txt(a.get("filename", "anexo")) for a in attachments))
            except Exception:
                pass

        with st.expander("Ver corpo principal do e-mail"):
            st.text_area("Corpo sem histórico citado", main_body, height=220, disabled=True, key=f"body_{key_prefix}_{row['id']}")

        obs = st.text_area("Observação manual", txt(row["observation"]), key=f"obs_{key_prefix}_{row['id']}")
        c1, c2, c3, c4 = st.columns(4)
        if c1.button("Salvar observação", key=f"save_obs_{key_prefix}_{row['id']}"):
            update_observation(row["id"], obs, conn)
            st.success("Observação salva.")
        if c2.button("Marcar como resolvido", key=f"resolved_{key_prefix}_{row['id']}"):
            update_email_status(row["id"], "resolvido", conn)
            st.cache_data.clear()
            st.rerun()
        if c3.button("Gerar resposta", key=f"gen_{key_prefix}_{row['id']}"):
            response_body = generate_response(row)
            response_subject = f"Re: {subject}" if not subject.lower().startswith("re:") else subject
            save_generated_response(conn, row["id"], txt(row["sender_email"]), response_subject, response_body)
            st.cache_data.clear()
            st.success("Resposta gerada em rascunho.")
        if c4.button("Criar tarefa", key=f"task_save_{key_prefix}_{row['id']}"):
            title = txt(row["recommended_action"]) or subject
            create_task(conn, row["id"], title, txt(row["summary"]) or "", txt(row["responsible"]) or "suporte", None)
            st.success("Tarefa criada.")

        if integration_mode == "orcamento":
            quote_panel(conn, row, detected)
        elif integration_mode == "financeiro":
            finance_panel(conn, row, detected)


def quote_panel(conn: sqlite3.Connection, row: sqlite3.Row, detected: dict[str, Any]) -> None:
    with st.expander("Gestão Click: preparar orçamento em aberto"):
        gc = GestaoClickClient()
        cnpj_detectado = first_value(detected.get("cnpj"))
        manual_cnpj = st.text_input("CNPJ do cliente", value=cnpj_detectado, key=f"quote_cnpj_{row['id']}")
        cliente_id = st.text_input("Cliente ID no Gestão Click", key=f"quote_cliente_{row['id']}")
        codigo = st.text_input("Número/código do orçamento", key=f"quote_codigo_{row['id']}")
        situacao_id = st.text_input(
            "Situação ID do orçamento em aberto",
            value=env("GESTAOCLICK_DEFAULT_SITUACAO_ORCAMENTO_ID", "") or "",
            key=f"quote_situacao_{row['id']}",
        )

        if st.button("Buscar cliente por CNPJ", key=f"quote_find_client_{row['id']}"):
            try:
                clients = gc.search_clients(cnpj=manual_cnpj)
                st.session_state[f"quote_clients_{row['id']}"] = clients
            except Exception as exc:
                st.error(f"Erro ao buscar cliente: {exc}")
        clients = st.session_state.get(f"quote_clients_{row['id']}", [])
        if clients:
            st.dataframe(clients, use_container_width=True)
            st.caption("Copie o ID do cliente desejado para o campo acima.")

        guessed_items = detected.get("itens_solicitados") or extract_requested_items(txt(row["body"]))
        items_text = "\n".join(f"{item.get('quantidade', '1')};{item.get('produto', '')};" for item in guessed_items)
        items_text = st.text_area(
            "Produtos do orçamento: uma linha por item no formato quantidade;produto_id ou nome;valor_unitario",
            value=items_text,
            height=130,
            key=f"quote_items_{row['id']}",
        )
        produtos = parse_products_text(items_text)
        payload = build_quote_payload(
            cliente_id=cliente_id,
            codigo=codigo,
            situacao_id=situacao_id,
            produtos=produtos,
            observacoes=f"Origem: e-mail #{row['id']} - {txt(row['subject'])}\n\n{remove_quoted_replies(txt(row['body']))[:1500]}",
        )
        st.json(payload)
        validation_errors = validate_quote_payload(payload)
        if validation_errors:
            st.error("Antes de criar no Gestão Click, corrija:\n\n- " + "\n- ".join(validation_errors))
        approved = st.checkbox("Aprovo criar este orçamento no Gestão Click", key=f"quote_approve_{row['id']}")
        if st.button("Criar orçamento no Gestão Click", disabled=(not approved or bool(validation_errors)), key=f"quote_create_{row['id']}"):
            try:
                result = gc.create_quote(payload)
                log_event("INFO", "gestao_click_orcamento", json.dumps(result, ensure_ascii=False), conn)
                st.success("Orçamento criado no Gestão Click.")
                st.json(result)
            except Exception as exc:
                log_event("ERROR", "gestao_click_orcamento", str(exc), conn)
                st.error(f"Erro ao criar orçamento: {exc}")


def finance_panel(conn: sqlite3.Connection, row: sqlite3.Row, detected: dict[str, Any]) -> None:
    with st.expander("Gestão Click: financeiro e fiscal"):
        gc = GestaoClickClient()
        cnpj_detectado = first_value(detected.get("cnpj"))
        manual_cnpj = st.text_input("CNPJ do cliente", value=cnpj_detectado, key=f"fin_cnpj_{row['id']}")
        cliente_id = st.text_input("Cliente ID no Gestão Click", key=f"fin_cliente_{row['id']}")
        c1, c2, c3 = st.columns(3)
        if c1.button("Buscar cliente", key=f"fin_find_client_{row['id']}"):
            try:
                st.session_state[f"fin_clients_{row['id']}"] = gc.search_clients(cnpj=manual_cnpj)
            except Exception as exc:
                st.error(f"Erro ao buscar cliente: {exc}")
        if c2.button("Consultar recebimentos", key=f"fin_receivables_{row['id']}"):
            try:
                st.session_state[f"fin_receivables_{row['id']}"] = gc.list_receivables(cliente_id)
            except Exception as exc:
                st.error(f"Erro ao consultar financeiro: {exc}")
        if c3.button("Consultar notas fiscais", key=f"fin_invoices_{row['id']}"):
            try:
                st.session_state[f"fin_invoices_{row['id']}"] = gc.list_product_invoices(cliente_id)
            except Exception as exc:
                st.error(f"Erro ao consultar notas: {exc}")

        for label, key in [
            ("Clientes encontrados", f"fin_clients_{row['id']}"),
            ("Recebimentos", f"fin_receivables_{row['id']}"),
            ("Notas fiscais", f"fin_invoices_{row['id']}"),
        ]:
            data = st.session_state.get(key)
            if data:
                st.markdown(f"**{label}**")
                st.dataframe(data, use_container_width=True)

        st.warning("Envio de nota, boleto ou resposta ao cliente deve ser feito somente após aprovação manual.")
        draft = st.text_area(
            "Texto para resposta após conferência",
            value="Olá, conferimos sua solicitação no financeiro/fiscal e seguiremos com o envio após aprovação interna.",
            key=f"fin_draft_{row['id']}",
        )
        approved = st.checkbox("Aprovo gerar rascunho desta resposta financeira", key=f"fin_approve_{row['id']}")
        if st.button("Gerar rascunho aprovado", disabled=not approved, key=f"fin_create_draft_{row['id']}"):
            save_generated_response(conn, row["id"], txt(row["sender_email"]), f"Re: {txt(row['subject'])}", draft)
            log_event("INFO", "rascunho_financeiro_aprovado", f"E-mail {row['id']}", conn)
            st.success("Rascunho financeiro gerado. O envio ainda exige confirmação na aba Respostas Geradas.")


def inbox_tab(default_filter: str | None = None, group: str | None = None, integration_mode: str | None = None) -> None:
    conn = get_conn()
    key_prefix = (default_filter or group or "todos").replace(" ", "_")
    st.subheader("Caixa de Entrada Inteligente")
    c1, c2, c3, c4, c5 = st.columns([1.2, 1, 1, 1, 1])
    days = c1.number_input("Últimos dias", min_value=1, max_value=365, value=7, step=1, key=f"days_{key_prefix}")
    if c1.button("Processar novos e-mails", type="primary", key=f"process_{key_prefix}"):
        process_new_emails(int(days), include_old_unread=False)
    category_options = ["Todos"] + CATEGORIES
    default_idx = category_options.index(default_filter) if default_filter in category_options else 0
    category = c2.selectbox("Categoria", category_options, index=default_idx, key=f"category_{key_prefix}")
    urgency = c3.selectbox("Urgência", ["Todas", 1, 2, 3, 4, 5], key=f"urgency_{key_prefix}")
    status = c4.selectbox("Status", ["novo", "Todos", "resolvido"], key=f"status_{key_prefix}")
    sender = c5.text_input("Remetente", key=f"sender_{key_prefix}")

    end_date = date.today()
    start_date = end_date - timedelta(days=int(days))
    filters = {
        "category": "Todos" if group in ("financeiro", "orcamento") else category,
        "urgency": urgency,
        "sender": sender,
        "status": status,
        "date_start": start_date,
        "date_end": end_date,
    }
    rows = filtered_rows(conn, filters, group=group)
    if st.button("Reclassificar e-mails deste período", key=f"reclassify_{key_prefix}"):
        count = reclassify_rows(conn, rows)
        st.success(f"{count} e-mails reclassificados.")
        st.rerun()
    st.caption(f"{len(rows)} e-mails encontrados no período de {start_date} até {end_date}")
    for row in rows:
        email_card(conn, row, key_prefix, integration_mode=integration_mode)
    conn.close()


def responses_tab() -> None:
    conn = get_conn()
    st.subheader("Respostas Geradas")
    rows = list_responses(conn)
    if not rows:
        st.info("Nenhuma resposta gerada ainda.")
    for r in rows:
        with st.container(border=True):
            st.markdown(f"### {txt(r['subject'])}")
            st.caption(f"Para: {txt(r['to_email'])} | Status: {txt(r['status'])} | Categoria: {txt(r['category'])}")
            subject = st.text_input("Assunto", txt(r["subject"]), key=f"resp_subject_{r['id']}")
            body = st.text_area("Resposta", txt(r["body"]), height=260, key=f"resp_body_{r['id']}")
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
                    send_email_smtp(txt(r["to_email"]), subject, body, original["message_id"] if original else None)
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
            st.markdown(f"**{txt(row['title'])}**")
            st.caption(
                f"Responsável: {txt(row['responsible'])} | Prazo: {txt(row['due_date']) or '-'} | "
                f"Status: {txt(row['status'])} | Cliente: {txt(row['sender_name'])} <{txt(row['sender_email'])}>"
            )
            st.write(txt(row["description"]))
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
        {"Variável": "GESTAOCLICK_ACCESS_TOKEN", "Status/valor": "configurado" if env("GESTAOCLICK_ACCESS_TOKEN") else "não configurado"},
        {"Variável": "GESTAOCLICK_SECRET_ACCESS_TOKEN", "Status/valor": "configurado" if env("GESTAOCLICK_SECRET_ACCESS_TOKEN") else "não configurado"},
    ]
    st.dataframe(config, use_container_width=True, hide_index=True)
    st.divider()
    st.markdown("### Integrações")
    st.write("Gestão Click: clientes, produtos, orçamentos, recebimentos e notas fiscais. Toda ação de criação/envio exige aprovação manual.")


def logs_tab() -> None:
    conn = get_conn()
    st.subheader("Logs")
    rows = list_logs(conn=conn)
    if rows:
        st.dataframe([{k: txt(v) for k, v in dict(r).items()} for r in rows], use_container_width=True, hide_index=True)
    else:
        st.info("Sem logs ainda.")
    conn.close()


def first_value(values: Any) -> str:
    if isinstance(values, list) and values:
        return txt(values[0])
    return ""


def parse_products_text(value: str) -> list[dict[str, str]]:
    products = []
    for line in (value or "").splitlines():
        parts = [part.strip() for part in line.split(";")]
        if len(parts) < 2:
            continue
        quantidade = parts[0] or "1"
        product_ref = parts[1]
        valor = parts[2] if len(parts) > 2 else "0"
        item: dict[str, str] = {"quantidade": quantidade, "valor_venda": valor}
        if product_ref.isdigit():
            item["produto_id"] = product_ref
            item["nome_produto"] = product_ref
        else:
            item["nome_produto"] = product_ref
        products.append(item)
    return products


def main() -> None:
    st.title("Central de E-mails Novaprint")
    st.caption("IMAP/SMTP local, classificação inteligente, Gestão Click e respostas com aprovação manual.")
    counts = cached_counts()
    m1, m2, m3, m4 = st.columns(4)
    m1.metric("Orçamentos", counts.get("Pedido de orçamento", 0))
    m2.metric("Financeiro", sum(counts.get(cat, 0) for cat in FINANCE_CATEGORIES))
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
        inbox_tab("Pedido de orçamento", group="orcamento", integration_mode="orcamento")
    with tabs[3]:
        inbox_tab(group="financeiro", integration_mode="financeiro")
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
