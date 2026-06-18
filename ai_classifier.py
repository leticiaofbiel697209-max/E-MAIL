from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any

try:
    from openai import OpenAI
except Exception:
    OpenAI = None

from utils import CATEGORIES, RESPONSIBLES, env, extract_requested_items, find_entities, normalize_for_search, remove_quoted_replies


def _gemini_text(prompt: str) -> str:
    api_key = env("GEMINI_API_KEY")
    if not api_key:
        return ""
    model = env("GEMINI_MODEL", "gemini-1.5-flash")
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={api_key}"
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.1, "response_mime_type": "application/json"},
    }
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=40) as resp:
        data = json.loads(resp.read().decode("utf-8", errors="replace"))
    return data["candidates"][0]["content"]["parts"][0].get("text", "")


def _parse_json_text(text: str) -> dict[str, Any]:
    cleaned = (text or "").strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.strip("`").strip()
        if cleaned.lower().startswith("json"):
            cleaned = cleaned[4:].strip()
    return json.loads(cleaned or "{}")


def _fallback_classification(subject: str, body: str) -> dict[str, Any]:
    main_body = remove_quoted_replies(body)
    text = normalize_for_search(f"{subject}\n{main_body}")
    category = "Outros"
    responsible = "suporte"
    urgency = 2
    action = "Ler e responder conforme o conteúdo."
    rules = [
        (("orcamento", "cotacao", "preco", "valor", "quanto fica"), "Pedido de orçamento", "vendas", 3, "Responder solicitando ou confirmando itens, quantidades, medidas, prazo e dados fiscais para orçamento."),
        (("boleto", "segunda via", "2ª via", "vencimento"), "Pedido de boleto", "financeiro", 3, "Verificar financeiro e enviar o boleto correto."),
        (("nota fiscal", "nf", "danfe", "nfe", "xml"), "Pedido de nota fiscal", "financeiro", 3, "Localizar a nota fiscal e responder com dados ou anexo."),
        (("comprovante", "pagamento realizado", "paguei", "pix"), "Comprovante enviado", "financeiro", 3, "Conferir comprovante e dar baixa quando confirmado."),
        (("entrega", "rastreamento", "chegou", "transportadora", "atraso"), "Cobrança de entrega", "entrega", 4, "Consultar status da entrega e retornar com prazo."),
        (("reclama", "problema", "defeito", "não funciona", "nao funciona", "n?o funciona", "insatisfeito"), "Reclamação", "suporte", 4, "Responder com acolhimento e abrir tratativa."),
        (("urgente", "hoje", "imediato", "prioridade"), "Urgente", "diretoria", 5, "Tratar imediatamente ou encaminhar ao responsável."),
        (("cobranca", "pagamento", "financeiro", "debito", "em aberto", "nota fiscal", "boleto"), "Financeiro", "financeiro", 3, "Localizar cliente no Gestão Click e validar boletos/notas antes de responder."),
        (("pós-venda", "pos venda", "feedback", "troca"), "Pós-venda", "suporte", 2, "Fazer atendimento de pós-venda."),
        (("relatorio", "prospeccao", "prospect", "planilha", "segue a planilha"), "Outros", "suporte", 1, "Arquivar ou responder apenas se houver solicitação clara."),
    ]
    for keys, cat, resp, urg, act in rules:
        if any(k in text for k in keys):
            category, responsible, urgency, action = cat, resp, urg, act
            break
    if category != "Urgente" and any(k in text for k in ("urgente", "imediato", "prioridade", "atrasado", "parado")):
        urgency = max(urgency, 4)
    if any(k in text for k in ("relatorio", "prospeccao", "segue a planilha")):
        urgency = min(urgency, 2)
        if category == "Urgente":
            category = "Outros"
            responsible = "suporte"
    return {
        "category": category,
        "summary": (main_body or subject or "E-mail sem conteúdo")[:260],
        "urgency": urgency,
        "recommended_action": action,
        "responsible": responsible,
        "detected": {
            **find_entities(f"{subject}\n{main_body}"),
            "itens_solicitados": extract_requested_items(main_body),
        },
    }


def classify_email(subject: str, body: str) -> dict[str, Any]:
    local_result = _fallback_classification(subject, body)
    api_key = env("OPENAI_API_KEY")
    prompt = f"""
Você é um assistente operacional da Novaprint. Classifique o e-mail abaixo.
Responda APENAS JSON válido com os campos:
category, summary, urgency, recommended_action, responsible, detected.
category deve ser uma destas: {CATEGORIES}
responsible deve ser um destes: {RESPONSIBLES}
urgency deve ser inteiro de 1 a 5.
detected deve conter arrays: cnpj, telefone, numero_pedido, numero_orcamento, valor.

Assunto: {subject}
Corpo principal, sem histórico citado:
{remove_quoted_replies(body)[:6000]}
"""
    if not api_key or OpenAI is None:
        try:
            gemini_content = _gemini_text(prompt)
            if not gemini_content:
                return local_result
            data = _parse_json_text(gemini_content)
            if data.get("category") not in CATEGORIES:
                data["category"] = local_result["category"]
            if data.get("responsible") not in RESPONSIBLES:
                data["responsible"] = local_result["responsible"]
            data["urgency"] = max(1, min(5, int(data.get("urgency", local_result["urgency"]))))
            detected = find_entities(f"{subject}\n{body}")
            ai_detected = data.get("detected") or {}
            data["detected"] = {k: sorted(set((ai_detected.get(k) or []) + detected.get(k, []))) for k in detected}
            return data
        except Exception:
            return local_result

    client = OpenAI(api_key=api_key)
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
