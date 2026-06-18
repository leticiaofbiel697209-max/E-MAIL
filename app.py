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


def find_finance_link(item: Any) -> str:
    if isinstance(item, dict):
        for key, value in item.items():
            key_name = str(key).lower()
            if any(term in key_name for term in ("link", "url", "pdf", "danfe", "boleto", "xml")):
                value_text = txt(value)
                if value_text.startswith(("http://", "https://")):
                    return value_text
            nested = find_finance_link(value)
            if nested:
                return nested
    elif isinstance(item, list):
        for value in item:
            nested = find_finance_link(value)
            if nested:
                return nested
    return ""


def apply_link_template(template: str, item: dict[str, Any] | None) -> str:
    if not template or not item:
        return ""
    if not any(token in template for token in ("{id}", "{codigo}", "{numero}", "{chave}", "{cliente_id}")):
        return ""
    values = {
        "id": txt(item.get("id")),
        "codigo": txt(item.get("codigo")),
        "numero": txt(item.get("numero_nf") or item.get("numero_nfe") or item.get("numero") or item.get("codigo") or item.get("id")),
        "chave": txt(item.get("chave") or item.get("chave_nfe") or item.get("chave_acesso")),
        "cliente_id": txt(item.get("cliente_id") or item.get("destinatario_id")),
    }
    try:
        return template.format(**values)
    except Exception:
        return ""


def best_finance_link(item: dict[str, Any] | None, template_env: str) -> str:
    return find_finance_link(item) or apply_link_template(env(template_env, ""), item)


def finance_option_label(item: dict[str, Any], kind: str) -> str:
    if kind == "recebimento":
        code = txt(item.get("codigo") or item.get("id"))
        value = txt(item.get("valor_total") or item.get("valor"))
        due = txt(item.get("data_vencimento"))
        return f"{code} | R$ {value or '-'} | venc. {due or '-'}"
    number = txt(item.get("numero_nf") or item.get("numero_nfe") or item.get("numero") or item.get("id"))
    value = txt(item.get("valor_total_nf") or item.get("valor_produtos") or item.get("valor"))
    date_value = txt(item.get("data_emissao") or item.get("data"))
    return f"NF {number} | R$ {value or '-'} | emissão {date_value or '-'}"


def build_finance_email_body(
    sender_name: str,
    receivable: dict[str, Any] | None,
    invoice: dict[str, Any] | None,
) -> str:
    lines = [
        f"Olá, {sender_name or 'tudo bem'}!",
        "",
        "Segue abaixo a documentação solicitada:",
        "",
    ]
    if invoice:
        invoice_number = txt(invoice.get("numero_nf") or invoice.get("numero_nfe") or invoice.get("numero") or invoice.get("id"))
        invoice_value = txt(invoice.get("valor_total_nf") or invoice.get("valor_produtos") or invoice.get("valor"))
        invoice_link = best_finance_link(invoice, "GESTAOCLICK_NOTA_LINK_TEMPLATE")
        lines.append(f"Nota fiscal: {invoice_number or 'sem número informado'}")
        if invoice_value:
            lines.append(f"Valor da nota: R$ {invoice_value}")
        lines.append(f"Link da nota: {invoice_link}" if invoice_link else "Link da nota: o Gestão Click não retornou link nesta consulta; anexe o PDF/XML antes do envio.")
        lines.append("")
    if receivable:
        receivable_code = txt(receivable.get("codigo") or receivable.get("id"))
        receivable_value = txt(receivable.get("valor_total") or receivable.get("valor"))
        receivable_due = txt(receivable.get("data_vencimento"))
        receivable_link = best_finance_link(receivable, "GESTAOCLICK_BOLETO_LINK_TEMPLATE")
        lines.append(f"Boleto/recebimento: {receivable_code or 'sem código informado'}")
        if receivable_value:
            lines.append(f"Valor: R$ {receivable_value}")
        if receivable_due:
            lines.append(f"Vencimento: {receivable_due}")
        lines.append(f"Link do boleto: {receivable_link}" if receivable_link else "Link do boleto: o Gestão Click não retornou link nesta consulta; anexe o boleto antes do envio.")
        lines.append("")
    lines.extend(["Qualquer dúvida, fico à disposição.", "", "Atenciosamente,", "Equipe Novaprint"])
    return "\n".join(lines)


def suggested_customer_email(row: sqlite3.Row, clients: list[dict[str, Any]] | None = None) -> str:
    for client in clients or []:
        if isinstance(client, dict) and txt(client.get("email")):
            return txt(client.get("email"))
    return txt(row["sender_email"])


def send_direct_reply(
    conn: sqlite3.Connection,
    row: sqlite3.Row,
    to_email: str,
    subject: str,
    body: str,
    log_source: str,
) -> None:
    send_email_smtp(to_email, subject, body, txt(row["message_id"]))
    update_email_status(row["id"], "resolvido", conn)
    log_event("INFO", log_source, f"E-mail {row['id']} enviado para {to_email} e marcado como resolvido.", conn)


def safe_table_rows(data: list[dict[str, Any]], kind: str) -> list[dict[str, str]]:
    if isinstance(data, dict):
        data = data.get("data") or [data]
    if not isinstance(data, list):
        data = [data]
    rows = []
    for item in data:
        if not isinstance(item, dict):
            item = {"valor": item}
        if kind == "clientes":
            rows.append(
                {
                    "id": txt(item.get("id") or item.get("cliente_id")),
                    "nome": txt(item.get("nome") or item.get("razao_social") or item.get("nome_fantasia")),
                    "documento": txt(item.get("cpf_cnpj") or item.get("cnpj") or item.get("cpf")),
                    "email": txt(item.get("email")),
                    "loja": txt(item.get("nome_loja") or item.get("loja_id")),
                }
            )
        elif kind == "recebimentos":
            rows.append(
                {
                    "id": txt(item.get("id")),
                    "codigo": txt(item.get("codigo")),
                    "cliente": txt(item.get("nome_cliente")),
                    "valor": txt(item.get("valor_total") or item.get("valor")),
                    "vencimento": txt(item.get("data_vencimento")),
                    "status": txt(item.get("liquidado")),
                    "forma": txt(item.get("nome_forma_pagamento")),
                    "link": best_finance_link(item, "GESTAOCLICK_BOLETO_LINK_TEMPLATE"),
                    "loja": txt(item.get("nome_loja") or item.get("loja_id")),
                }
            )
        elif kind == "notas":
            rows.append(
                {
                    "id": txt(item.get("id")),
                    "numero_nf": txt(item.get("numero_nf") or item.get("numero_nfe")),
                    "cliente": txt(item.get("destinatario_nome") or item.get("nome_cliente")),
                    "valor": txt(item.get("valor_total_nf") or item.get("valor_produtos")),
                    "emissao": txt(item.get("data_emissao")),
                    "situacao": txt(item.get("situacao_nf")),
                    "link": best_finance_link(item, "GESTAOCLICK_NOTA_LINK_TEMPLATE"),
                    "loja": txt(item.get("nome_loja") or item.get("loja_id")),
                }
            )
    return rows


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
        client_key = f"quote_cliente_{row['id']}"
        clients_key = f"quote_clients_{row['id']}"
        situations_key = f"quote_situations_{row['id']}"
        cnpj_detectado = first_value(detected.get("cnpj"))
        manual_cnpj = st.text_input("CNPJ do cliente", value=cnpj_detectado, key=f"quote_cnpj_{row['id']}")

        if st.button("Buscar cliente por CNPJ", key=f"quote_find_client_{row['id']}"):
            try:
                clients = gc.search_clients(cnpj=manual_cnpj)
                st.session_state[clients_key] = clients
                if clients:
                    first_client = clients[0]
                    st.session_state[client_key] = str(first_client.get("id") or first_client.get("cliente_id") or "")
                    st.success(f"Cliente localizado: {first_client.get('nome') or first_client.get('razao_social') or first_client.get('nome_fantasia') or 'sem nome'}")
                    st.rerun()
                else:
                    st.warning("Nenhum cliente encontrado para este CNPJ.")
            except Exception as exc:
                st.error(f"Erro ao buscar cliente: {exc}")

        cliente_id = st.text_input("Cliente ID no Gestão Click", key=client_key)
        clients = st.session_state.get(clients_key, [])
        if clients:
            compact_clients = [
                {
                    "id": c.get("id") or c.get("cliente_id"),
                    "nome": c.get("nome") or c.get("razao_social") or c.get("nome_fantasia"),
                    "cpf_cnpj": c.get("cpf_cnpj") or c.get("cnpj") or c.get("cpf"),
                    "email": c.get("email"),
                }
                for c in clients
            ]
            st.dataframe(compact_clients, use_container_width=True, hide_index=True)

        codigo = st.text_input("Número/código do orçamento", key=f"quote_codigo_{row['id']}")

        if st.button("Carregar situações de orçamento", key=f"quote_load_situations_{row['id']}"):
            try:
                st.session_state[situations_key] = gc.list_quote_situations()
            except Exception as exc:
                st.error(f"Erro ao carregar situações: {exc}")

        situations = st.session_state.get(situations_key, [])
        situacao_id = env("GESTAOCLICK_DEFAULT_SITUACAO_ORCAMENTO_ID", "") or ""
        if situations:
            options = []
            for s in situations:
                sid = str(s.get("id") or s.get("situacao_id") or "")
                name = s.get("nome") or s.get("descricao") or s.get("nome_situacao") or "sem nome"
                if sid:
                    options.append((sid, f"{sid} - {name}"))
            labels = [label for _, label in options]
            current_index = next((i for i, (sid, _) in enumerate(options) if sid == str(situacao_id)), 0)
            selected_label = st.selectbox("Escolher situação do orçamento", labels, index=current_index, key=f"quote_situation_select_{row['id']}")
            situacao_id = options[labels.index(selected_label)][0]
        else:
            situacao_id = st.text_input(
                "Situação ID do orçamento em aberto",
                value=situacao_id,
                key=f"quote_situacao_manual_{row['id']}",
            )

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
            observacoes=f"Origem: e-mail #{row['id']} - {txt(row['subject'])}",
        )
        st.markdown("**Resumo que será enviado ao Gestão Click**")
        st.write(
            {
                "cliente_id": payload.get("cliente_id"),
                "codigo": payload.get("codigo"),
                "situacao_id": payload.get("situacao_id"),
                "produtos": len(payload.get("produtos") or []),
            }
        )
        validation_errors = validate_quote_payload(payload)
        if validation_errors:
            st.error("Antes de criar no Gestão Click, corrija:\n\n- " + "\n- ".join(validation_errors))
        approved = st.checkbox("Aprovo criar este orçamento no Gestão Click", key=f"quote_approve_{row['id']}")
        if st.button("Criar orçamento no Gestão Click", disabled=(not approved or bool(validation_errors)), key=f"quote_create_{row['id']}"):
            try:
                result = gc.create_quote(payload)
                log_event("INFO", "gestao_click_orcamento", json.dumps(result, ensure_ascii=False), conn)
                st.success("Orçamento criado no Gestão Click.")
            except Exception as exc:
                log_event("ERROR", "gestao_click_orcamento", str(exc), conn)
                st.error(f"Erro ao criar orçamento: {exc}")


def finance_panel(conn: sqlite3.Connection, row: sqlite3.Row, detected: dict[str, Any]) -> None:
    with st.expander("Gestão Click: financeiro e fiscal"):
        gc = GestaoClickClient()
        client_key = f"fin_cliente_{row['id']}"
        clients_key = f"fin_clients_{row['id']}"
        cnpj_detectado = first_value(detected.get("cnpj"))
        manual_cnpj = st.text_input("CNPJ do cliente", value=cnpj_detectado, key=f"fin_cnpj_{row['id']}")
        numero_nf = st.text_input("Número da nota fiscal, se souber", key=f"fin_numero_nf_{row['id']}")
        c1, c2, c3 = st.columns(3)
        if c1.button("Buscar cliente", key=f"fin_find_client_{row['id']}"):
            try:
                clients = gc.search_clients(cnpj=manual_cnpj)
                st.session_state[clients_key] = clients
                if clients:
                    first_client = clients[0]
                    st.session_state[client_key] = str(first_client.get("id") or first_client.get("cliente_id") or "")
                    st.success(f"Cliente localizado: {first_client.get('nome') or first_client.get('razao_social') or first_client.get('nome_fantasia') or 'sem nome'}")
                    st.rerun()
                else:
                    st.warning("Nenhum cliente encontrado para este CNPJ.")
            except Exception as exc:
                st.error(f"Erro ao buscar cliente: {exc}")

        cliente_id = st.text_input("Cliente ID no Gestão Click", key=client_key)
        if c2.button("Consultar recebimentos", key=f"fin_receivables_{row['id']}"):
            try:
                st.session_state[f"fin_receivables_data_{row['id']}"] = gc.list_receivables(cliente_id)
            except Exception as exc:
                st.error(f"Erro ao consultar financeiro: {exc}")
        if c3.button("Consultar notas fiscais", key=f"fin_invoices_{row['id']}"):
            try:
                notas = gc.list_product_invoices(cliente_id, cnpj=manual_cnpj, numero_nf=numero_nf)
                st.session_state[f"fin_invoices_data_{row['id']}"] = notas
                if not notas:
                    st.warning("Nenhuma nota correspondente a este Cliente ID/CNPJ foi encontrada.")
            except Exception as exc:
                st.error(f"Erro ao consultar notas: {exc}")

        for label, key in [
            ("Clientes encontrados", clients_key),
            ("Recebimentos", f"fin_receivables_data_{row['id']}"),
            ("Notas fiscais", f"fin_invoices_data_{row['id']}"),
        ]:
            data = st.session_state.get(key)
            if data:
                st.markdown(f"**{label}**")
                kind = "clientes" if "Clientes" in label else "recebimentos" if "Recebimentos" in label else "notas"
                st.dataframe(safe_table_rows(data, kind), use_container_width=True, hide_index=True)

        receivables = st.session_state.get(f"fin_receivables_data_{row['id']}") or []
        invoices = st.session_state.get(f"fin_invoices_data_{row['id']}") or []
        if receivables or invoices:
            st.markdown("**Montar e-mail com nota/boleto**")
            auto_to_email = st.text_input(
                "Enviar para",
                value=suggested_customer_email(row, st.session_state.get(clients_key) or []),
                key=f"fin_auto_to_{row['id']}",
            )
            receipt_options = ["Não incluir boleto/recebimento"] + [
                finance_option_label(item, "recebimento") for item in receivables if isinstance(item, dict)
            ]
            invoice_options = ["Não incluir nota fiscal"] + [
                finance_option_label(item, "nota") for item in invoices if isinstance(item, dict)
            ]
            selected_receipt = st.selectbox("Boleto/recebimento para enviar", receipt_options, key=f"fin_select_receipt_{row['id']}")
            selected_invoice = st.selectbox("Nota fiscal para enviar", invoice_options, key=f"fin_select_invoice_{row['id']}")
            receipt_index = receipt_options.index(selected_receipt) - 1
            invoice_index = invoice_options.index(selected_invoice) - 1
            receivable = receivables[receipt_index] if receipt_index >= 0 and receipt_index < len(receivables) else None
            invoice = invoices[invoice_index] if invoice_index >= 0 and invoice_index < len(invoices) else None
            invoice_link = st.text_input(
                "Link da nota fiscal",
                value=best_finance_link(invoice, "GESTAOCLICK_NOTA_LINK_TEMPLATE") if invoice else "",
                key=f"fin_invoice_link_{row['id']}_{invoice_index}",
            )
            receipt_link = st.text_input(
                "Link do boleto/recebimento",
                value=best_finance_link(receivable, "GESTAOCLICK_BOLETO_LINK_TEMPLATE") if receivable else "",
                key=f"fin_receipt_link_{row['id']}_{receipt_index}",
            )
            if invoice and invoice_link:
                invoice = {**invoice, "link_manual": invoice_link}
            if receivable and receipt_link:
                receivable = {**receivable, "link_manual": receipt_link}
            if invoice and not invoice_link:
                st.warning("A API não retornou link da nota. Configure GESTAOCLICK_NOTA_LINK_TEMPLATE nos Secrets para montar automaticamente.")
            if receivable and not receipt_link:
                st.warning("A API não retornou link do boleto. Configure GESTAOCLICK_BOLETO_LINK_TEMPLATE nos Secrets para montar automaticamente.")
            auto_body = build_finance_email_body(txt(row["sender_name"]) or txt(row["sender_email"]), receivable, invoice)
            st.text_area(
                "Prévia do e-mail financeiro",
                auto_body,
                height=220,
                key=f"fin_auto_preview_{row['id']}_{receipt_index}_{invoice_index}_{hash(invoice_link + receipt_link)}",
            )
            approve_auto = st.checkbox("Aprovo criar/enviar e-mail com estes itens", key=f"fin_auto_approve_{row['id']}")
            d1, d2 = st.columns(2)
            if d1.button(
                "Criar rascunho com nota/boleto",
                disabled=(not approve_auto or not auto_to_email or not (receivable or invoice)),
                key=f"fin_auto_draft_{row['id']}",
            ):
                save_generated_response(conn, row["id"], auto_to_email, f"Re: {txt(row['subject'])}", auto_body)
                log_event("INFO", "rascunho_financeiro_auto", f"E-mail {row['id']} com nota/boleto para {auto_to_email}.", conn)
                st.success("Rascunho criado em Respostas Geradas. Revise e confirme o envio.")
            if d2.button(
                "Enviar agora e marcar resolvido",
                disabled=(not approve_auto or not auto_to_email or not (receivable or invoice)),
                key=f"fin_auto_send_{row['id']}",
            ):
                try:
                    send_direct_reply(conn, row, auto_to_email, f"Re: {txt(row['subject'])}", auto_body, "envio_financeiro_direto")
                    st.success("E-mail enviado e marcado como resolvido.")
                    st.rerun()
                except Exception as exc:
                    log_event("ERROR", "envio_financeiro_direto", str(exc), conn)
                    st.error(f"Erro ao enviar: {exc}")

        st.warning("Envio de nota, boleto ou resposta ao cliente deve ser feito somente após aprovação manual.")
        manual_to_email = st.text_input(
            "Enviar resposta manual para",
            value=suggested_customer_email(row, st.session_state.get(clients_key) or []),
            key=f"fin_manual_to_{row['id']}",
        )
        draft = st.text_area(
            "Texto para resposta após conferência",
            value="Olá, conferimos sua solicitação no financeiro/fiscal e seguiremos com o envio após aprovação interna.",
            key=f"fin_draft_{row['id']}",
        )
        approved = st.checkbox("Aprovo gerar rascunho desta resposta financeira", key=f"fin_approve_{row['id']}")
        if st.button("Gerar rascunho aprovado", disabled=not approved, key=f"fin_create_draft_{row['id']}"):
            save_generated_response(conn, row["id"], manual_to_email, f"Re: {txt(row['subject'])}", draft)
            log_event("INFO", "rascunho_financeiro_aprovado", f"E-mail {row['id']} para {manual_to_email}", conn)
            st.success("Rascunho financeiro gerado. O envio ainda exige confirmação na aba Respostas Geradas.")
        if st.button("Enviar resposta financeira agora e marcar resolvido", disabled=(not approved or not manual_to_email), key=f"fin_send_direct_{row['id']}"):
            try:
                send_direct_reply(conn, row, manual_to_email, f"Re: {txt(row['subject'])}", draft, "envio_financeiro_manual")
                st.success("E-mail enviado e marcado como resolvido.")
                st.rerun()
            except Exception as exc:
                log_event("ERROR", "envio_financeiro_manual", str(exc), conn)
                st.error(f"Erro ao enviar: {exc}")


def inbox_tab(default_filter: str | None = None, group: str | None = None, integration_mode: str | None = None) -> None:
    conn = get_conn()
    key_prefix = (default_filter or group or "todos").replace(" ", "_")
    st.subheader("Caixa de Entrada Inteligente")
    c1, c2, c3, c4, c5, c6 = st.columns([1.2, 1, 1, 1, 1, 1.2])
    days = c1.number_input("Últimos dias", min_value=1, max_value=365, value=7, step=1, key=f"days_{key_prefix}")
    if c1.button("Processar novos e-mails", type="primary", key=f"process_{key_prefix}"):
        process_new_emails(int(days), include_old_unread=False)
    if c1.button("Atualizar tela", key=f"refresh_{key_prefix}"):
        st.rerun()
    category_options = ["Todos"] + CATEGORIES
    default_idx = category_options.index(default_filter) if default_filter in category_options else 0
    category = c2.selectbox("Categoria", category_options, index=default_idx, key=f"category_{key_prefix}")
    urgency = c3.selectbox("Urgência", ["Todas", 1, 2, 3, 4, 5], key=f"urgency_{key_prefix}")
    status = c4.selectbox("Status", ["novo", "Todos", "resolvido"], key=f"status_{key_prefix}")
    sender = c5.text_input("Remetente", key=f"sender_{key_prefix}")
    sort_label = c6.selectbox("Ordem", ["Mais novo primeiro", "Mais antigo primeiro", "Urgência primeiro"], key=f"sort_{key_prefix}")
    sort = {"Mais novo primeiro": "newest", "Mais antigo primeiro": "oldest", "Urgência primeiro": "urgent"}[sort_label]

    end_date = date.today()
    start_date = end_date - timedelta(days=int(days))
    filters = {
        "category": "Todos" if group in ("financeiro", "orcamento") else category,
        "urgency": urgency,
        "sender": sender,
        "status": status,
        "date_start": start_date,
        "date_end": end_date,
        "sort": sort,
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
            to_email = st.text_input("Para", txt(r["to_email"]), key=f"resp_to_{r['id']}")
            subject = st.text_input("Assunto", txt(r["subject"]), key=f"resp_subject_{r['id']}")
            body = st.text_area("Resposta", txt(r["body"]), height=260, key=f"resp_body_{r['id']}")
            uploaded_files = st.file_uploader(
                "Anexar nota, boleto, XML ou outro arquivo",
                accept_multiple_files=True,
                key=f"resp_attachments_{r['id']}",
            )
            c1, c2 = st.columns(2)
            if c1.button("Salvar edição", key=f"resp_save_{r['id']}"):
                update_response(conn, r["id"], subject, body, to_email)
                st.success("Rascunho atualizado.")
            st.code(body, language="text")
            confirm = st.checkbox("Tem certeza que deseja enviar este e-mail?", key=f"confirm_{r['id']}")
            if c2.button("Enviar resposta", key=f"send_{r['id']}", disabled=(not confirm or r["status"] == "enviado")):
                try:
                    update_response(conn, r["id"], subject, body, to_email)
                    original = conn.execute("SELECT message_id FROM emails WHERE id=?", (r["email_id"],)).fetchone()
                    attachments = [
                        {
                            "filename": file.name,
                            "content": file.getvalue(),
                            "mime_type": file.type,
                        }
                        for file in uploaded_files
                    ]
                    send_email_smtp(
                        to_email,
                        subject,
                        body,
                        original["message_id"] if original else None,
                        attachments=attachments,
                    )
                    mark_response_sent(conn, r["id"])
                    log_event("INFO", "envio_email", f"Resposta {r['id']} enviada para {to_email} com {len(attachments)} anexo(s).", conn)
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
        {"Variável": "EMAIL_SMTP_USE_SSL", "Status/valor": env("EMAIL_SMTP_USE_SSL", "false")},
        {"Variável": "EMAIL_SMTP_ALT_HOSTS", "Status/valor": env("EMAIL_SMTP_ALT_HOSTS", "não configurado")},
        {"Variável": "EMAIL_USER", "Status/valor": env("EMAIL_USER", "não configurado")},
        {"Variável": "EMAIL_PASSWORD", "Status/valor": "configurado" if env("EMAIL_PASSWORD") else "não configurado"},
        {"Variável": "EMAIL_FROM_NAME", "Status/valor": env("EMAIL_FROM_NAME", "Novaprint")},
        {"Variável": "OPENAI_API_KEY", "Status/valor": "configurado" if env("OPENAI_API_KEY") else "não configurado - usando fallback por regras"},
        {"Variável": "GEMINI_API_KEY", "Status/valor": "configurado" if env("GEMINI_API_KEY") else "não configurado"},
        {"Variável": "GEMINI_MODEL", "Status/valor": env("GEMINI_MODEL", "gemini-1.5-flash")},
        {"Variável": "GESTAOCLICK_ACCESS_TOKEN", "Status/valor": "configurado" if env("GESTAOCLICK_ACCESS_TOKEN") else "não configurado"},
        {"Variável": "GESTAOCLICK_SECRET_ACCESS_TOKEN", "Status/valor": "configurado" if env("GESTAOCLICK_SECRET_ACCESS_TOKEN") else "não configurado"},
        {"Variável": "GESTAOCLICK_DEFAULT_LOJA_ID", "Status/valor": env("GESTAOCLICK_DEFAULT_LOJA_ID", "não configurado")},
        {"Variável": "GESTAOCLICK_NOTA_LINK_TEMPLATE", "Status/valor": "configurado" if env("GESTAOCLICK_NOTA_LINK_TEMPLATE") else "não configurado"},
        {"Variável": "GESTAOCLICK_BOLETO_LINK_TEMPLATE", "Status/valor": "configurado" if env("GESTAOCLICK_BOLETO_LINK_TEMPLATE") else "não configurado"},
    ]
    st.dataframe(config, use_container_width=True, hide_index=True)
    st.divider()
    st.markdown("### Teste de envio SMTP")
    smtp_port = txt(env("EMAIL_SMTP_PORT", "587"))
    smtp_ssl = txt(env("EMAIL_SMTP_USE_SSL", "false")).lower()
    if smtp_port == "587" and smtp_ssl in ("1", "true", "sim", "yes"):
        st.warning("Para porta 587, normalmente use EMAIL_SMTP_USE_SSL=false. O app usa STARTTLS nessa porta.")
    if smtp_port == "465" and smtp_ssl not in ("1", "true", "sim", "yes"):
        st.warning("Para porta 465, normalmente use EMAIL_SMTP_USE_SSL=true.")
    st.caption("Se o SMTP configurado falhar, o app também tenta mail./smtp. do domínio, 465/SSL e 587/STARTTLS automaticamente.")
    for label, template in [
        ("nota", env("GESTAOCLICK_NOTA_LINK_TEMPLATE", "")),
        ("boleto", env("GESTAOCLICK_BOLETO_LINK_TEMPLATE", "")),
    ]:
        if template and not any(token in template for token in ("{id}", "{codigo}", "{numero}", "{chave}", "{cliente_id}")):
            st.warning(f"O template de {label} está fixo. Use um modelo com {{id}}, {{codigo}}, {{numero}}, {{chave}} ou {{cliente_id}} para variar por cliente/documento.")
    test_to = st.text_input("Enviar teste para", value=env("EMAIL_USER", ""), key="smtp_test_to")
    test_confirm = st.checkbox("Confirmo enviar um e-mail de teste", key="smtp_test_confirm")
    if st.button("Enviar teste SMTP", disabled=not test_confirm):
        conn = get_conn()
        try:
            send_email_smtp(
                test_to,
                "Teste de envio - Central de E-mails Novaprint",
                "Este é um teste de envio SMTP feito pela Central de E-mails Novaprint.",
            )
            log_event("INFO", "teste_smtp", f"Teste enviado para {test_to}", conn)
            st.success("Teste enviado. Confira a caixa de entrada e o spam/lixo eletrônico.")
        except Exception as exc:
            log_event("ERROR", "teste_smtp", str(exc), conn)
            st.error(f"Erro no teste SMTP: {exc}")
        finally:
            conn.close()
    if st.button("Enviar teste como resposta", disabled=not test_confirm):
        conn = get_conn()
        try:
            send_email_smtp(
                test_to,
                "Re: Teste de resposta - Central de E-mails Novaprint",
                "Este teste usa o mesmo formato das respostas do sistema, sem cabeçalhos de conversa.",
            )
            log_event("INFO", "teste_smtp_resposta", f"Teste de resposta enviado para {test_to}", conn)
            st.success("Teste como resposta enviado. Confira caixa de entrada e spam.")
        except Exception as exc:
            log_event("ERROR", "teste_smtp_resposta", str(exc), conn)
            st.error(f"Erro no teste como resposta: {exc}")
        finally:
            conn.close()
    st.divider()
    st.markdown("### Integrações")
    st.write("Gestão Click: clientes, produtos, orçamentos, recebimentos e notas fiscais. Toda ação de criação/envio exige aprovação manual.")
    if st.button("Listar lojas do Gestão Click"):
        try:
            stores = GestaoClickClient().list_stores()
            rows = [
                {
                    "id": txt(store.get("id") or store.get("loja_id")),
                    "nome": txt(store.get("nome") or store.get("nome_loja") or store.get("razao_social")),
                    "cnpj": txt(store.get("cnpj") or store.get("cpf_cnpj")),
                }
                for store in stores
            ]
            st.dataframe(rows, use_container_width=True, hide_index=True)
            st.info("Copie o ID da linha Novaprint e cole em GESTAOCLICK_DEFAULT_LOJA_ID nos Secrets do Streamlit.")
        except Exception as exc:
            st.error(f"Erro ao listar lojas: {exc}")


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
