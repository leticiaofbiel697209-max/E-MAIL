from __future__ import annotations

import json
import urllib.error
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
        self.default_loja_id = env("GESTAOCLICK_DEFAULT_LOJA_ID")

    def is_configured(self) -> bool:
        return bool(self.base_url and self.access_token and self.secret_access_token)

    def _headers(self) -> dict[str, str]:
        if not self.is_configured():
            raise GestaoClickError("Configure GESTAOCLICK_ACCESS_TOKEN e GESTAOCLICK_SECRET_ACCESS_TOKEN nos Secrets.")
        return {
            "Content-Type": "application/json",
            "access-token": self.access_token or "",
            "secret-access-token": self.secret_access_token or "",
        }

    def _store_params(self, params: dict[str, Any] | None = None) -> dict[str, Any]:
        params = dict(params or {})
        if self.default_loja_id and "loja_id" not in params:
            params["loja_id"] = self.default_loja_id
        return params

    def request(
        self,
        method: str,
        path: str,
        params: dict[str, Any] | None = None,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        query = f"?{urllib.parse.urlencode(params)}" if params else ""
        url = f"{self.base_url}/{path.strip('/')}{query}"
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
        req = urllib.request.Request(url, data=data, headers=self._headers(), method=method.upper())
        try:
            with urllib.request.urlopen(req, timeout=40) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode("utf-8", errors="replace")
            message = raw.strip() or str(exc)
            raise GestaoClickError(f"HTTP {exc.code} em {url}: {message[:1200]}") from exc
        except Exception as exc:
            raise GestaoClickError(f"Erro ao chamar {url}: {exc}") from exc

        try:
            result = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise GestaoClickError(f"Resposta inválida do Gestão Click: {raw[:300]}") from exc
        if str(result.get("status", "")).lower() not in ("success", "sucesso", "") and result.get("code") not in (200, "200"):
            raise GestaoClickError(json.dumps(result, ensure_ascii=False)[:1200])
        return result

    def list_stores(self) -> list[dict[str, Any]]:
        result = self.request("GET", "/lojas")
        return result.get("data") or []

    def search_clients(self, cnpj: str = "", email: str = "", nome: str = "") -> list[dict[str, Any]]:
        params: dict[str, Any] = {"limit": 20}
        if cnpj:
            params["cpf_cnpj"] = only_digits(cnpj)
        elif email:
            params["email"] = email
        elif nome:
            params["nome"] = nome
        result = self.request("GET", "/clientes", params=self._store_params(params))
        return result.get("data") or []

    def search_products(self, nome: str) -> list[dict[str, Any]]:
        result = self.request("GET", "/produtos", params=self._store_params({"nome": nome, "limit": 20}))
        return result.get("data") or []

    def list_quote_situations(self) -> list[dict[str, Any]]:
        result = self.request("GET", "/situacoes_orcamentos")
        return result.get("data") or []

    def create_quote(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self.request("POST", "/orcamentos", payload=payload)

    def get_receivable(self, receivable_id: str | int) -> dict[str, Any]:
        result = self.request("GET", f"/recebimentos/{receivable_id}")
        return result.get("data") or {}

    def list_receivables(self, cliente_id: str | int, limit: int = 100) -> list[dict[str, Any]]:
        params = self._store_params({"cliente_id": cliente_id, "limit": limit})
        result = self.request("GET", "/recebimentos", params=params)
        items = result.get("data") or []
        enriched = []
        for item in items:
            if not isinstance(item, dict):
                continue
            item_id = item.get("id")
            if item_id:
                try:
                    item = {**item, **self.get_receivable(item_id)}
                except Exception:
                    pass
            enriched.append(item)
        return enriched

    def list_receivables_for_clients(self, cliente_ids: list[str | int], limit: int = 100) -> list[dict[str, Any]]:
        seen: set[str] = set()
        merged: list[dict[str, Any]] = []
        for cliente_id in cliente_ids:
            if not str(cliente_id or "").strip():
                continue
            for item in self.list_receivables(cliente_id, limit=limit):
                item_id = str(item.get("id") or item.get("codigo") or "")
                if item_id and item_id in seen:
                    continue
                if item_id:
                    seen.add(item_id)
                merged.append(item)
        return merged

    def get_product_invoice(self, invoice_id: str | int) -> dict[str, Any]:
        result = self.request("GET", f"/notas_fiscais_produtos/{invoice_id}")
        return result.get("data") or {}

    def list_product_invoices(
        self,
        cliente_id: str | int,
        cnpj: str = "",
        numero_nf: str = "",
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        attempts = [
            {"destinatario_id": cliente_id, "limit": limit},
            {"destinatario_id_cliente": cliente_id, "limit": limit},
            {"cliente_id": cliente_id, "limit": limit},
        ]
        if cnpj:
            attempts.append({"cpf_cnpj": only_digits(cnpj), "limit": limit})
        if numero_nf:
            attempts.append({"numero_nf": numero_nf, "limit": limit})
        seen: set[str] = set()
        merged: list[dict[str, Any]] = []
        for params in attempts:
            result = self.request("GET", "/notas_fiscais_produtos", params=self._store_params(params))
            data = filter_invoices_for_client(result.get("data") or [], cliente_id, cnpj, numero_nf)
            if data:
                for item in data:
                    item_id = item.get("id") if isinstance(item, dict) else None
                    if item_id:
                        try:
                            item = {**item, **self.get_product_invoice(item_id)}
                        except Exception:
                            pass
                    if isinstance(item, dict):
                        dedupe_id = str(item.get("id") or item.get("numero_nf") or item.get("numero_nfe") or "")
                        if dedupe_id and dedupe_id in seen:
                            continue
                        if dedupe_id:
                            seen.add(dedupe_id)
                        merged.append(item)
        return merged

    def list_product_invoices_for_clients(
        self,
        cliente_ids: list[str | int],
        cnpj: str = "",
        numero_nf: str = "",
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        seen: set[str] = set()
        merged: list[dict[str, Any]] = []
        for cliente_id in cliente_ids:
            if not str(cliente_id or "").strip():
                continue
            for item in self.list_product_invoices(cliente_id, cnpj=cnpj, numero_nf=numero_nf, limit=limit):
                item_id = str(item.get("id") or item.get("numero_nf") or item.get("numero_nfe") or "")
                if item_id and item_id in seen:
                    continue
                if item_id:
                    seen.add(item_id)
                merged.append(item)
        return merged


def only_digits(value: str) -> str:
    return "".join(ch for ch in str(value or "") if ch.isdigit())


def filter_invoices_for_client(
    invoices: list[dict[str, Any]],
    cliente_id: str | int,
    cnpj: str = "",
    numero_nf: str = "",
) -> list[dict[str, Any]]:
    wanted_id = str(cliente_id or "").strip()
    wanted_doc = only_digits(cnpj)
    wanted_nf = str(numero_nf or "").strip()
    filtered = []
    for item in invoices:
        if not isinstance(item, dict):
            continue
        possible_ids = [
            item.get("cliente_id"),
            item.get("id_cliente"),
            item.get("destinatario_id"),
            item.get("destinatario_id_cliente"),
            item.get("id_destinatario"),
        ]
        cliente = item.get("cliente") if isinstance(item.get("cliente"), dict) else {}
        destinatario = item.get("destinatario") if isinstance(item.get("destinatario"), dict) else {}
        possible_ids.extend([cliente.get("id"), destinatario.get("id"), cliente.get("cliente_id")])
        possible_docs = [
            item.get("cpf_cnpj"),
            item.get("cnpj"),
            item.get("documento"),
            item.get("destinatario_cpf_cnpj"),
            item.get("destinatario_documento"),
            item.get("destinatario_cnpj"),
            item.get("destinatario_cpf"),
            cliente.get("cpf_cnpj"),
            cliente.get("cnpj"),
            cliente.get("cpf"),
            destinatario.get("cpf_cnpj"),
            destinatario.get("cnpj"),
            destinatario.get("cpf"),
            destinatario.get("documento"),
        ]
        possible_numbers = [
            item.get("numero_nf"),
            item.get("numero_nfe"),
            item.get("numero"),
            item.get("codigo"),
        ]
        id_match = wanted_id and any(str(value or "").strip() == wanted_id for value in possible_ids)
        doc_match = wanted_doc and any(only_digits(str(value or "")) == wanted_doc for value in possible_docs)
        nf_match = wanted_nf and any(str(value or "").strip() == wanted_nf for value in possible_numbers)
        if nf_match or doc_match or id_match:
            filtered.append(item)
    return filtered


def build_quote_payload(
    cliente_id: str,
    codigo: str,
    situacao_id: str,
    produtos: list[dict[str, Any]],
    observacoes: str,
) -> dict[str, Any]:
    payload_products = []
    for item in produtos:
        product_id = str(item.get("produto_id") or item.get("id") or "").strip()
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
            product_payload["id"] = int(product_id) if product_id.isdigit() else product_id
        payload_products.append({"produto": product_payload})

    loja_id = env("GESTAOCLICK_DEFAULT_LOJA_ID")
    payload: dict[str, Any] = {
        "tipo": "produto",
        "codigo": int(codigo) if str(codigo).isdigit() else codigo,
        "cliente_id": int(cliente_id) if str(cliente_id).isdigit() else cliente_id,
        "situacao_id": int(situacao_id) if str(situacao_id).isdigit() else situacao_id,
        "data": date.today().isoformat(),
        "observacoes": observacoes,
        "observacoes_interna": "Criado pela Central de E-mails Novaprint após aprovação manual.",
        "produtos": payload_products,
    }
    if loja_id:
        payload["loja_id"] = int(loja_id) if str(loja_id).isdigit() else loja_id
    return payload


def validate_quote_payload(payload: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    if not str(payload.get("loja_id") or "").strip():
        errors.append("Configure GESTAOCLICK_DEFAULT_LOJA_ID com o ID da loja Novaprint.")
    if not str(payload.get("cliente_id") or "").strip():
        errors.append("Informe o Cliente ID do Gestão Click.")
    if not str(payload.get("codigo") or "").strip():
        errors.append("Informe o número/código do orçamento.")
    if not str(payload.get("situacao_id") or "").strip():
        errors.append("Informe a Situação ID do orçamento em aberto.")
    products = payload.get("produtos") or []
    if not products:
        errors.append("Informe pelo menos um produto.")
    for idx, item in enumerate(products, start=1):
        product = item.get("produto") or {}
        if not str(product.get("id") or product.get("nome_produto") or "").strip():
            errors.append(f"Produto {idx}: informe produto_id ou nome.")
        try:
            quantity = float(str(product.get("quantidade") or "0").replace(",", "."))
        except ValueError:
            quantity = 0
        if quantity <= 0:
            errors.append(f"Produto {idx}: quantidade precisa ser maior que zero.")
    return errors
