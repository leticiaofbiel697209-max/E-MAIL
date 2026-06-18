from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
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

    def request_resource(self, path: str) -> dict[str, Any]:
        url = f"{self.base_url}/{path.strip('/')}"
        req = urllib.request.Request(url, headers=self._headers(), method="GET")
        try:
            with urllib.request.urlopen(req, timeout=40) as resp:
                content = resp.read()
                content_type = resp.headers.get("Content-Type", "")
                final_url = resp.geturl()
        except urllib.error.HTTPError:
            return {}
        except Exception:
            return {}

        if final_url and final_url != url and "api.gestaoclick.com" not in final_url:
            return {"link": final_url}
        if "application/pdf" in content_type.lower() or content.startswith(b"%PDF"):
            return {"pdf_bytes": content}
        if "json" in content_type.lower():
            try:
                data = json.loads(content.decode("utf-8", errors="replace"))
            except Exception:
                data = {}
            link = find_public_link(data)
            if link:
                return {"link": link}
        text = content[:500].decode("utf-8", errors="replace")
        link = find_public_link({"text": text})
        return {"link": link} if link else {}

    def list_all_pages(self, path: str, params: dict[str, Any], max_pages: int = 10) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        seen_pages: set[int] = set()
        page = int(params.get("pagina") or 1)
        while page not in seen_pages and len(seen_pages) < max_pages:
            seen_pages.add(page)
            page_params = dict(params)
            page_params["pagina"] = page
            result = self.request("GET", path, params=page_params)
            data = result.get("data") or []
            if isinstance(data, list):
                items.extend(item for item in data if isinstance(item, dict))
            meta = result.get("meta") if isinstance(result.get("meta"), dict) else {}
            next_page = meta.get("proxima_pagina")
            total_pages = int(meta.get("total_paginas") or page)
            if next_page:
                page = int(next_page)
            elif page < total_pages:
                page += 1
            else:
                break
        return items

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

    def resolve_receivable_resource(self, receivable_id: str | int) -> dict[str, Any]:
        paths = [
            f"/recebimentos/{receivable_id}/boleto",
            f"/recebimentos/boleto/{receivable_id}",
            f"/recebimentos/{receivable_id}/pdf",
            f"/recebimentos/pdf/{receivable_id}",
            f"/recebimentos/imprimir/{receivable_id}",
        ]
        for path in paths:
            resource = self.request_resource(path)
            if resource:
                return resource
        return {}

    def list_receivables(self, cliente_id: str | int, limit: int = 100) -> list[dict[str, Any]]:
        params = self._store_params({"cliente_id": cliente_id, "limit": limit})
        items = self.list_all_pages("/recebimentos", params)
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
                resource = self.resolve_receivable_resource(item_id)
                if resource.get("link"):
                    item["link_boleto_api"] = resource["link"]
                if resource.get("pdf_bytes"):
                    item["_pdf_attachment"] = resource["pdf_bytes"]
                    item["_pdf_filename"] = f"boleto_{item.get('codigo') or item_id}.pdf"
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

    def resolve_product_invoice_resource(self, invoice_id: str | int) -> dict[str, Any]:
        paths = [
            f"/notas_fiscais_produtos/{invoice_id}/danfe",
            f"/notas_fiscais_produtos/danfe/{invoice_id}",
            f"/notas_fiscais_produtos/{invoice_id}/pdf",
            f"/notas_fiscais_produtos/pdf/{invoice_id}",
            f"/notas_fiscais_produtos/imprimir/{invoice_id}",
        ]
        for path in paths:
            resource = self.request_resource(path)
            if resource:
                return resource
        return {}

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
            data = filter_invoices_for_client(
                self.list_all_pages("/notas_fiscais_produtos", self._store_params(params)),
                cliente_id,
                cnpj,
                numero_nf,
            )
            if data:
                for item in data:
                    item_id = item.get("id") if isinstance(item, dict) else None
                    if item_id:
                        try:
                            item = {**item, **self.get_product_invoice(item_id)}
                        except Exception:
                            pass
                        resource = self.resolve_product_invoice_resource(item_id)
                        if resource.get("link"):
                            item["link_danfe_api"] = resource["link"]
                        if resource.get("pdf_bytes"):
                            item["_pdf_attachment"] = resource["pdf_bytes"]
                            item["_pdf_filename"] = f"nota_{item.get('numero_nf') or item_id}.pdf"
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


def find_public_link(value: Any) -> str:
    if isinstance(value, dict):
        for key, item in value.items():
            key_name = str(key).lower()
            if any(term in key_name for term in ("link", "url", "pdf", "danfe", "boleto")):
                item_text = str(item or "")
                if item_text.startswith(("http://", "https://")):
                    return item_text
            nested = find_public_link(item)
            if nested:
                return nested
    elif isinstance(value, list):
        for item in value:
            nested = find_public_link(item)
            if nested:
                return nested
    elif isinstance(value, str):
        start = min([idx for idx in [value.find("https://"), value.find("http://")] if idx >= 0], default=-1)
        if start >= 0:
            end = len(value)
            for sep in ['"', "'", " ", "\n", "\r", "<"]:
                pos = value.find(sep, start)
                if pos > start:
                    end = min(end, pos)
            return value[start:end]
    return ""


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
