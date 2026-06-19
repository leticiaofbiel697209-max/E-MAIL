from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from datetime import date
from decimal import Decimal, InvalidOperation
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
        self.auto_fetch_documents = (env("GESTAOCLICK_AUTO_FETCH_DOCUMENTS", "false") or "").lower() in ("1", "true", "sim", "yes")

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
            with urllib.request.urlopen(req, timeout=8) as resp:
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
        if str(path).startswith(("http://", "https://")):
            url = str(path)
        else:
            url = f"{self.base_url}/{path.strip('/')}"
        req = urllib.request.Request(url, headers=self._headers(), method="GET")
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
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

    def list_all_pages(self, path: str, params: dict[str, Any], max_pages: int | None = None) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        seen_pages: set[int] = set()
        page = int(params.get("pagina") or 1)
        page_limit = max_pages or int(env("GESTAOCLICK_MAX_PAGES", "4") or 4)
        while page not in seen_pages and len(seen_pages) < page_limit:
            seen_pages.add(page)
            page_params = dict(params)
            page_params["pagina"] = page
            try:
                result = self.request("GET", path, params=page_params)
            except GestaoClickError:
                break
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

    def resolve_receivable_resource(self, receivable_item: dict[str, Any] | str | int) -> dict[str, Any]:
        item = receivable_item if isinstance(receivable_item, dict) else {"id": receivable_item}
        tokens = unique_values(
            [
                item.get("id"),
                item.get("codigo"),
                item.get("pedido_id"),
                item.get("venda_id"),
                item.get("boleto_id"),
                item.get("nosso_numero"),
                item.get("documento"),
            ]
        )
        paths = [
            f"/recebimentos/{token}/boleto"
            for token in tokens
        ] + [
            f"/recebimentos/boleto/{token}" for token in tokens
        ] + [
            f"/recebimentos/{token}/pdf" for token in tokens
        ] + [
            f"/recebimentos/pdf/{token}" for token in tokens
        ] + [
            f"/recebimentos/imprimir/{token}" for token in tokens
        ] + [
            f"/boletos/{token}" for token in tokens
        ] + [
            f"/boletos/{token}/pdf" for token in tokens
        ] + [
            f"https://gestaoclick.com/boleto/{token}" for token in tokens
        ]
        for path in paths:
            resource = self.request_resource(path)
            if resource:
                return resource
        return {}

    def list_receivables(self, cliente_id: str | int, limit: int = 50) -> list[dict[str, Any]]:
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
                if self.auto_fetch_documents:
                    item = self.attach_receivable_resource(item)
            enriched.append(item)
        return enriched

    def get_sale(self, sale_id: str | int) -> dict[str, Any]:
        result = self.request("GET", f"/vendas/{sale_id}")
        data = result.get("data") or {}
        return data if isinstance(data, dict) else {}

    def list_sales(self, cliente_id: str | int, limit: int = 50) -> list[dict[str, Any]]:
        params = self._store_params({"cliente_id": cliente_id, "limit": limit, "tipo": "produto"})
        items = self.list_all_pages("/vendas", params, max_pages=int(env("GESTAOCLICK_SALES_MAX_PAGES", "3") or 3))
        enriched: list[dict[str, Any]] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            item_id = item.get("id")
            if item_id:
                try:
                    item = {**item, **self.get_sale(item_id)}
                except Exception:
                    pass
            enriched.append(item)
        return enriched

    def list_sales_for_clients(self, cliente_ids: list[str | int], limit: int = 50) -> list[dict[str, Any]]:
        seen: set[str] = set()
        merged: list[dict[str, Any]] = []
        for cliente_id in cliente_ids:
            if not str(cliente_id or "").strip():
                continue
            for item in self.list_sales(cliente_id, limit=limit):
                item_id = str(item.get("id") or item.get("codigo") or "")
                if item_id and item_id in seen:
                    continue
                if item_id:
                    seen.add(item_id)
                merged.append(item)
        return merged

    def attach_receivable_resource(self, item: dict[str, Any]) -> dict[str, Any]:
        item_id = item.get("id")
        if not item_id:
            return item
        resource = self.resolve_receivable_resource(item)
        if resource.get("link"):
            item["link_boleto_api"] = resource["link"]
        if resource.get("pdf_bytes"):
            item["_pdf_attachment"] = resource["pdf_bytes"]
            item["_pdf_filename"] = f"boleto_{item.get('codigo') or item_id}.pdf"
        return item

    def list_receivables_for_clients(self, cliente_ids: list[str | int], limit: int = 50) -> list[dict[str, Any]]:
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

    def enrich_finance_documents(
        self,
        cliente_ids: list[str | int],
        receivables: list[dict[str, Any]],
        invoices: list[dict[str, Any]],
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        sales = self.list_sales_for_clients(cliente_ids)
        if not sales:
            return receivables, invoices
        return (
            [attach_sale_link(item, sales, "boleto") for item in receivables],
            [attach_sale_link(item, sales, "nota") for item in invoices],
        )

    def get_product_invoice(self, invoice_id: str | int) -> dict[str, Any]:
        result = self.request("GET", f"/notas_fiscais_produtos/{invoice_id}")
        return result.get("data") or {}

    def resolve_product_invoice_resource(self, invoice_item: dict[str, Any] | str | int) -> dict[str, Any]:
        item = invoice_item if isinstance(invoice_item, dict) else {"id": invoice_item}
        tokens = unique_values(
            [
                item.get("id"),
                item.get("numero_nf"),
                item.get("numero_nfe"),
                item.get("numero"),
                item.get("pedido_id"),
                item.get("chave"),
                item.get("chave_nfe"),
                item.get("chave_acesso"),
            ]
        )
        paths = [
            f"/notas_fiscais_produtos/{token}/danfe"
            for token in tokens
        ] + [
            f"/notas_fiscais_produtos/danfe/{token}" for token in tokens
        ] + [
            f"/notas_fiscais_produtos/{token}/pdf" for token in tokens
        ] + [
            f"/notas_fiscais_produtos/pdf/{token}" for token in tokens
        ] + [
            f"/notas_fiscais_produtos/imprimir/{token}" for token in tokens
        ] + [
            f"/notas_fiscais_produtos/{token}/imprimir" for token in tokens
        ] + [
            f"/notas_fiscais_produtos/{token}/xml" for token in tokens
        ] + [
            f"https://gestaoclick.com/nfe/danfe/{token}" for token in tokens
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
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        attempts = [
            {"destinatario_id": cliente_id, "limit": limit},
            {"destinatario_id_cliente": cliente_id, "limit": limit},
            {"id_destinatario": cliente_id, "limit": limit},
            {"id_cliente": cliente_id, "limit": limit},
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
                        if self.auto_fetch_documents:
                            item = self.attach_product_invoice_resource(item)
                    if isinstance(item, dict):
                        dedupe_id = str(item.get("id") or item.get("numero_nf") or item.get("numero_nfe") or "")
                        if dedupe_id and dedupe_id in seen:
                            continue
                        if dedupe_id:
                            seen.add(dedupe_id)
                        merged.append(item)
        return merged

    def attach_product_invoice_resource(self, item: dict[str, Any]) -> dict[str, Any]:
        item_id = item.get("id")
        if not item_id:
            return item
        resource = self.resolve_product_invoice_resource(item)
        if resource.get("link"):
            item["link_danfe_api"] = resource["link"]
        if resource.get("pdf_bytes"):
            item["_pdf_attachment"] = resource["pdf_bytes"]
            item["_pdf_filename"] = f"nota_{item.get('numero_nf') or item_id}.pdf"
        return item

    def list_product_invoices_for_clients(
        self,
        cliente_ids: list[str | int],
        cnpj: str = "",
        numero_nf: str = "",
        limit: int = 50,
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


def unique_values(values: list[Any]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        text = str(value or "").strip()
        if not text or text in seen:
            continue
        seen.add(text)
        result.append(text)
    return result


def money_value(value: Any) -> Decimal | None:
    text = str(value or "").strip()
    if not text:
        return None
    text = text.replace("R$", "").replace(" ", "").replace(".", "").replace(",", ".") if "," in text else text
    try:
        return Decimal(text).quantize(Decimal("0.01"))
    except (InvalidOperation, ValueError):
        return None


def compact_date(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    if "/" in text:
        parts = text.split("/")
        if len(parts) == 3:
            return f"{parts[2]}-{parts[1].zfill(2)}-{parts[0].zfill(2)}"
    return text[:10]


def collect_nested_values(value: Any, keys: tuple[str, ...]) -> list[str]:
    found: list[str] = []
    if isinstance(value, dict):
        for key, item in value.items():
            if str(key).lower() in keys and item not in (None, ""):
                found.append(str(item))
            found.extend(collect_nested_values(item, keys))
    elif isinstance(value, list):
        for item in value:
            found.extend(collect_nested_values(item, keys))
    return found


def sale_public_hash(sale: dict[str, Any]) -> str:
    for key in ("hash", "codigo_hash", "hash_publico", "token"):
        if sale.get(key):
            return str(sale.get(key))
    nested = collect_nested_values(sale, ("hash", "codigo_hash", "hash_publico", "token"))
    return nested[0] if nested else ""


def sale_payment_rows(sale: dict[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for item in sale.get("pagamentos") or []:
        if isinstance(item, dict):
            payment = item.get("pagamento") if isinstance(item.get("pagamento"), dict) else item
            if isinstance(payment, dict):
                rows.append(payment)
    return rows


def document_ids(item: dict[str, Any]) -> set[str]:
    keys = (
        "id",
        "codigo",
        "pedido_id",
        "venda_id",
        "orcamento_id",
        "numero_nf",
        "numero_nfe",
        "numero",
    )
    return {str(value).strip() for value in collect_nested_values(item, keys) if str(value).strip()}


def sale_ids(sale: dict[str, Any]) -> set[str]:
    ids = document_ids(sale)
    ids.update(str(value).strip() for value in collect_nested_values(sale, ("pedido_id",)) if str(value).strip())
    return ids


def same_money(left: Any, right: Any) -> bool:
    left_value = money_value(left)
    right_value = money_value(right)
    return left_value is not None and right_value is not None and left_value == right_value


def best_sale_for_document(item: dict[str, Any], sales: list[dict[str, Any]], kind: str) -> dict[str, Any] | None:
    ids = document_ids(item)
    for sale in sales:
        if ids.intersection(sale_ids(sale)):
            return sale

    item_value = item.get("valor_total_nf") or item.get("valor_produtos") or item.get("valor_total") or item.get("valor")
    item_date = compact_date(item.get("data_emissao") or item.get("data_vencimento") or item.get("data"))
    scored: list[tuple[int, dict[str, Any]]] = []
    for sale in sales:
        score = 0
        if same_money(item_value, sale.get("valor_total")):
            score += 3
        sale_date = compact_date(sale.get("data") or sale.get("data_primeira_parcela"))
        if item_date and sale_date and item_date == sale_date:
            score += 2
        for payment in sale_payment_rows(sale):
            if same_money(item_value, payment.get("valor")):
                score += 3
            if item_date and compact_date(payment.get("data_vencimento")) == item_date:
                score += 2
        if kind == "nota" and item.get("pedido_id") and str(item.get("pedido_id")) in sale_ids(sale):
            score += 5
        if score:
            scored.append((score, sale))
    if not scored:
        return None
    scored.sort(key=lambda pair: pair[0], reverse=True)
    return scored[0][1]


def attach_sale_link(item: dict[str, Any], sales: list[dict[str, Any]], kind: str) -> dict[str, Any]:
    if not isinstance(item, dict):
        return item
    if find_public_link(item) or item.get("hash") or item.get("codigo_hash") or item.get("hash_publico"):
        return item
    sale = best_sale_for_document(item, sales, kind)
    if not sale:
        return item
    public_hash = sale_public_hash(sale)
    if not public_hash:
        return item
    enriched = dict(item)
    # O hash da venda ajuda a diagnosticar o vínculo, mas não é o hash público
    # do DANFE/boleto. Usá-lo como link fiscal gera URLs incorretas.
    enriched["venda_hash"] = public_hash
    enriched["hash_origem"] = "venda_relacionada"
    enriched["venda_id"] = sale.get("id") or enriched.get("venda_id")
    enriched["venda_codigo"] = sale.get("codigo") or enriched.get("venda_codigo")
    return enriched


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
