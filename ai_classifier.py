from __future__ import annotations

import json
from typing import Any

try:
    from openai import OpenAI
except Exception:
    OpenAI = None

from utils import CATEGORIES, RESPONSIBLES, env, find_entities


def _fallback_classification(subject: str, body: str) -> dict[str, Any]:
    text = f"{subject}\n{body}".lower()
    category = "Outros"
    responsible = "suporte"
    urgency = 2
    action = "Ler e responder conforme o conteúdo."
    rules = [
        (("orçamento", "orcamento", "or?amento", "cotação", "cotacao", "cota?ao", "preço", "preco", "pre?o"), "Pedido de orçamento", "vendas", 3, "Responder solicitando ou confirmando itens e dados para orçamento."),
        (("boleto", "segunda via", "2ª via", "vencimento"), "Pedido de boleto", "financeiro", 3, "Verificar financeiro e enviar o boleto correto."),
        (("nota fiscal", "nf", "danfe", "nfe", "xml"), "Pedido de nota fiscal", "financeiro", 3, "Localizar a nota fiscal e responder com dados ou anexo."),
        (("comprovante", "pagamento realizado", "paguei", "pix"), "Comprovante enviado", "financeiro", 3, "Conferir comprovante e dar baixa quando confirmado."),
        (("entrega", "rastreamento", "chegou", "transportadora", "atraso"), "Cobrança de entrega", "entrega", 4, "Consultar status da entrega e retornar com prazo."),
        (("reclama", "problema", "defeito", "não funciona", "nao funciona", "n?o funciona", "insatisfeito"), "Reclamação", "suporte", 4, "Responder com acolhimento e abrir tratativa."),
        (("urgente", "hoje", "imediato", "prioridade"), "Urgente", "diretoria", 5, "Tratar imediatamente ou encaminhar ao responsável."),
        (("cobrança", "cobranca", "pagamento", "financeiro", "débito", "debito"), "Financeiro", "financeiro", 3, "Encaminhar ou validar com o financeiro."),
        (("pós-venda", "pos venda", "feedback", "troca"), "Pós-venda", "suporte", 2, "Fazer atendimento de pós-venda."),
    ]
    for keys, cat, resp, urg, act in rules:
        if any(k in text for k in keys):
            category, responsible, urgency, action = cat, resp, urg, act
            break
    return {
        "category": category,
        "summary": (body or subject or "E-mail sem conteúdo")[:220],
        "urgency": urgency,
        "recommended_action": action,
        "responsible": responsible,
        "detected": find_entities(f"{subject}\n{body}"),
    }


def classify_email(subject: str, body: str) -> dict[str, Any]:
    local_result = _fallback_classification(subject, body)
    api_key = env("OPENAI_API_KEY")
    if not api_key or OpenAI is None:
        return local_result

    client = OpenAI(api_key=api_key)
    prompt = f"""
Você é um assistente operacional da Novaprint. Classifique o e-mail abaixo.
Responda APENAS JSON válido com os campos:
category, summary, urgency, recommended_action, responsible, detected.
category deve ser uma destas: {CATEGORIES}
responsible deve ser um destes: {RESPONSIBLES}
urgency deve ser inteiro de 1 a 5.
detected deve conter arrays: cnpj, telefone, numero_pedido, numero_orcamento, valor.

Assunto: {subject}
Corpo:
{body[:6000]}
"""
    try:
        resp = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": prompt}],
            temperature=0.1,
            response_format={"type": "json_object"},
        )
        data = json.loads(resp.choices[0].message.content or "{}")
        if data.get("category") not in CATEGORIES:
            data["category"] = "Outros"
        if data.get("responsible") not in RESPONSIBLES:
            data["responsible"] = "suporte"
        data["urgency"] = max(1, min(5, int(data.get("urgency", 1))))
        if data.get("category") == "Outros" and local_result.get("category") != "Outros":
            data.update(
                {
                    "category": local_result["category"],
                    "urgency": max(data["urgency"], local_result["urgency"]),
                    "recommended_action": local_result["recommended_action"],
                    "responsible": local_result["responsible"],
                }
            )
        detected = find_entities(f"{subject}\n{body}")
        ai_detected = data.get("detected") or {}
        data["detected"] = {k: sorted(set((ai_detected.get(k) or []) + detected.get(k, []))) for k in detected}
        return data
    except Exception:
        return local_result
