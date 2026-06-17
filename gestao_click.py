from __future__ import annotations

import json
import urllib.parse
import urllib.request
from datetime import date
from typing import Any

from utils import env


class GestaoClickError(RuntimeError):
    pass


class GestaoClickClient:
    def __init__(self) -> None:
        self.base_url = (env("GESTAOCLICK_BASE_URL", "https://api.gestaoclick.com/api") or "").rstrip("/")
        self.access_token = env("GESTAOCLICK_ACCESS_TOKEN")
        self.secret_access_token = env("GESTAOCLICK_SECRET_ACCESS_TOKEN")

    def is_configured(self) -> bool:
        return bool(self.base_url and self.access_token and self.secret_access_token)

    def _headers(self) -> dict[str, str]:
        if not self.is_configured():
            raise GestaoClickError("Configure GESTAOCLICK_ACCESS_TOKEN e GESTAOCLICK_SECRET_ACCESS_TOKEN no .env.")
        return {
            "Content-Type": "application/json",
            "access-token": self.access_token or "",
            "secret-access-token": self.secret_access_token or "",
        }

    def request(self, method: str, path: str, params: dict[str, Any] | None = None, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        query = f"?{urllib.parse.urlencode(params)}" if params else ""
        url = f"{self.base_url}/{path.strip('/')}{query}"
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
        req = urllib.request.Request(url, data=data, headers=self._headers(), method=method.upper())
        try:
            with urllib.request.urlopen(req, timeout=40) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
        except Exception as exc:
            raise GestaoClickError(str(exc)) from exc
        try:
            result = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise GestaoClickError(f"Resposta inválida do Gestão Click: {raw[:300]}") from exc
        if str(result.get("status", "")).lower() not in ("success", "sucesso", "") and result.get("code") not in (200, "200"):
            raise GestaoClickError(json.dumps(result, ensure_ascii=False)[:800])
        return result

    def search_clients(self, cnpj: str = "", email: str = "", nome: str = "") -> list[dict[str, Any]]:
        params: dict[str, Any] = {"limit": 20}
        if cnpj:
            params["cpf_cnpj"] = only_digits(cnpj)
        elif email:
            params["email"] = email
        elif nome:
            params["nome"] = nome
        result = self.request("GET", "/clientes", params=params)
        return result.get("data") or []

    def search_products(self, nome: str) -> list[dict[str, Any]]:
        result = self.request("GET", "/produtos", params={"nome": nome, "limit": 20})
        return result.get("data") or []

    def create_quote(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self.request("POST", "/orcamentos", payload=payload)

    def list_receivables(self, cliente_id: str | int, limit: int = 20) -> list[dict[str, Any]]:
        result = self.request("GET", "/recebimentos", params={"cliente_id": cliente_id, "limit": limit})
        return result.get("data") or []

    def list_product_invoices(self, cliente_id: str | int, limit: int = 20) -> list[dict[str, Any]]:
        result = self.request("GET", "/notas_fiscais_produtos", params={"destinatario_id": cliente_id, "limit": limit})
        return result.get("data") or []


def only_digits(value: str) -> str:
    return "".join(ch for ch in str(value or "") if ch.isdigit())


def build_quote_payload(
    cliente_id: str,
    codigo: str,
    situacao_id: str,
    produtos: list[dict[str, Any]],
    observacoes: str,
) -> dict[str, Any]:
    payload_products = []
    for item in produtos:
        product_id = str(item.get("produto_id", "")).strip()
        nome = str(item.get("nome_produto") or item.get("produto") or "").strip()
        quantidade = str(item.get("quantidade") or "1").replace(",", ".")
        valor_venda = str(item.get("valor_venda") or "0").replace(",", ".")
        if not product_id and not nome:
            continue
        product_payload: dict[str, Any] = {
            "nome_produto": nome,
            "quantidade": quantidade,
            "valor_venda": valor_venda,
            "tipo_desconto": "R$",
            "desconto_valor": "0.00",
            "desconto_porcentagem": "0.00",
        }
        if product_id:
            product_payload["produto_id"] = product_id
            product_payload["id"] = product_id
        payload_products.append({"produto": product_payload})

    return {
        "tipo": "produto",
        "codigo": codigo,
        "cliente_id": cliente_id,
        "situacao_id": situacao_id,
        "data": date.today().isoformat(),
        "observacoes": observacoes,
        "observacoes_interna": "Criado pela Central de E-mails Novaprint após aprovação manual.",
        "produtos": payload_products,
    }
