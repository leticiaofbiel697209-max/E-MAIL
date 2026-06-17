from __future__ import annotations

import html
import os
import re
from datetime import datetime
from email.header import decode_header
from typing import Any, Optional

try:
    from bs4 import BeautifulSoup
except ModuleNotFoundError:
    BeautifulSoup = None

try:
    from dotenv import load_dotenv
except ModuleNotFoundError:
    def load_dotenv(*args, **kwargs):
        return False


load_dotenv()

CATEGORIES = [
    "Pedido de orçamento",
    "Pedido de boleto",
    "Pedido de nota fiscal",
    "Comprovante enviado",
    "Cobrança de entrega",
    "Reclamação",
    "Pós-venda",
    "Financeiro",
    "Urgente",
    "Outros",
]
RESPONSIBLES = ["vendas", "financeiro", "entrega", "suporte", "diretoria"]


def env(name: str, default: Optional[str] = None) -> Optional[str]:
    value = os.getenv(name, default)
    if value == "":
        return default
    return value


def decode_mime_words(value: str | None) -> str:
    if not value:
        return ""
    decoded = []
    for text, charset in decode_header(value):
        if isinstance(text, bytes):
            for enc in [charset, "utf-8", "cp1252", "latin-1", "iso-8859-1"]:
                if not enc:
                    continue
                try:
                    decoded.append(text.decode(enc, errors="replace"))
                    break
                except Exception:
                    continue
            else:
                decoded.append(text.decode("utf-8", errors="replace"))
        else:
            decoded.append(text)
    return "".join(decoded).strip()


def html_to_text(content: str) -> str:
    if not content:
        return ""
    if BeautifulSoup is not None:
        soup = BeautifulSoup(content, "html.parser")
        for tag in soup(["script", "style"]):
            tag.decompose()
        return clean_text(html.unescape(soup.get_text("\n")))

    cleaned = re.sub(r"(?is)<(script|style).*?>.*?</\1>", " ", content)
    cleaned = re.sub(r"(?i)<br\s*/?>", "\n", cleaned)
    cleaned = re.sub(r"(?i)</p\s*>", "\n", cleaned)
    cleaned = re.sub(r"(?s)<.*?>", " ", cleaned)
    return clean_text(html.unescape(cleaned))


def clean_text(text: str) -> str:
    text = text or ""
    text = text.replace("\r", "\n")
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = re.sub(r"[ \t]{2,}", " ", text)
    return text.strip()


def safe_decode(payload: bytes, charset: str | None = None) -> str:
    for enc in [charset, "utf-8", "cp1252", "latin-1", "iso-8859-1"]:
        if not enc:
            continue
        try:
            return payload.decode(enc, errors="replace")
        except Exception:
            pass
    return payload.decode("utf-8", errors="replace")


def extract_sender(sender_raw: str) -> tuple[str, str]:
    from email.utils import parseaddr

    name, email_addr = parseaddr(sender_raw or "")
    return decode_mime_words(name) or email_addr, email_addr.lower()


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def find_entities(text: str) -> dict[str, Any]:
    text = text or ""
    cnpjs = re.findall(r"\b\d{2}\.?\d{3}\.?\d{3}/?\d{4}-?\d{2}\b", text)
    phones = re.findall(r"(?:\+55\s*)?(?:\(?\d{2}\)?\s*)?(?:9\s*)?\d{4}[-\s]?\d{4}", text)
    order_nums = re.findall(r"(?:pedido|ped\.|ordem)\s*(?:n[ºo°.]*)?\s*[:#-]?\s*(\d{3,})", text, flags=re.I)
    quote_nums = re.findall(r"(?:orçamento|orcamento|cotação|cotacao)\s*(?:n[ºo°.]*)?\s*[:#-]?\s*(\d{3,})", text, flags=re.I)
    values = re.findall(r"R\$\s*\d{1,3}(?:\.\d{3})*(?:,\d{2})?", text)
    return {
        "cnpj": sorted(set(cnpjs)),
        "telefone": sorted(set(p.strip() for p in phones if len(re.sub(r"\D", "", p)) >= 8)),
        "numero_pedido": sorted(set(order_nums)),
        "numero_orcamento": sorted(set(quote_nums)),
        "valor": sorted(set(values)),
    }


def bool_to_status(is_unread: bool) -> str:
    return "não lido" if is_unread else "lido"
