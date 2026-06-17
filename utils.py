from __future__ import annotations

import html
import os
import re
import unicodedata
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
    value = os.getenv(name)
    if value not in (None, ""):
        return str(value)

    # Streamlit Cloud does not use .env files. It exposes values through
    # st.secrets, configured in App settings > Secrets.
    try:
        import streamlit as st

        if name in st.secrets:
            secret_value = st.secrets[name]
            if secret_value not in (None, ""):
                return str(secret_value)
    except Exception:
        pass

    value = default
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


def repair_mojibake(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    if not any(marker in value for marker in ("Ã", "Â", "ð", "�")):
        return value
    try:
        fixed = value.encode("latin-1", errors="ignore").decode("utf-8", errors="ignore")
        return fixed or value
    except Exception:
        return value


def remove_quoted_replies(text: str) -> str:
    text = repair_mojibake(text or "")
    cut_patterns = [
        r"\nEm \d{1,2}/\d{1,2}/\d{4}.*?escreveu:",
        r"\nOn .* wrote:",
        r"\nDe:\s.*\nEnviado:",
        r"\n_{5,}\n",
        r"\n-{5,}\n",
    ]
    first_cut = len(text)
    for pattern in cut_patterns:
        match = re.search(pattern, text, flags=re.I | re.S)
        if match:
            first_cut = min(first_cut, match.start())
    cleaned_lines = []
    for line in text[:first_cut].splitlines():
        if line.strip().startswith(">"):
            continue
        cleaned_lines.append(line)
    return clean_text("\n".join(cleaned_lines))


def normalize_for_search(text: str) -> str:
    text = repair_mojibake(text or "").replace("?", "c")
    text = unicodedata.normalize("NFKD", text)
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    return text.lower()


def extract_requested_items(text: str) -> list[dict[str, str]]:
    text = remove_quoted_replies(text)
    text = re.sub(r"\bCNPJ\b.*", "", text, flags=re.I)
    text = re.sub(r"\b(?:telefone|tel\.?|e-mail|email)\b.*", "", text, flags=re.I)
    items: list[dict[str, str]] = []
    patterns = [
        r"(?P<qtd>\d+(?:[,.]\d+)?)\s*(?:un|und|unid|unidade|unidades|x)?\s+(?:de\s+)?(?P<produto>[A-Za-zÀ-ÿ0-9][A-Za-zÀ-ÿ0-9\s\-_/.,]{3,80})",
        r"(?P<produto>[A-Za-zÀ-ÿ0-9][A-Za-zÀ-ÿ0-9\s\-_/.,]{3,80})\s*[-:]\s*(?P<qtd>\d+(?:[,.]\d+)?)\s*(?:un|und|unid|unidade|unidades)?",
    ]
    stop_words = ("cnpj", "telefone", "email", "e-mail", "orçamento", "orcamento", "pedido", "valor")
    lines: list[str] = []
    for raw_line in text.splitlines():
        pieces = re.split(r"\s+(?:e|,|\+)\s+(?=\d+(?:[,.]\d+)?\s)", raw_line, flags=re.I)
        lines.extend(pieces)
    for line in lines:
        line = line.strip(" -•\t")
        if len(line) < 5:
            continue
        for pattern in patterns:
            match = re.search(pattern, line, flags=re.I)
            if not match:
                continue
            produto = clean_text(match.group("produto")).strip(" .,-")
            qtd = match.group("qtd").replace(",", ".")
            if produto and not any(word in normalize_for_search(produto) for word in stop_words):
                items.append({"produto": produto[:100], "quantidade": qtd})
            break
    unique = []
    seen = set()
    for item in items:
        key = (normalize_for_search(item["produto"]), item["quantidade"])
        if key not in seen:
            seen.add(key)
            unique.append(item)
    return unique[:12]
