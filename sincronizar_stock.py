#!/usr/bin/env python3
"""
Sincronização de stock eWheel -> Shopify (versão para execução agendada).

Diferenças face a sincronizar_stock_ewheel_todos.py:
  * Só escreve na Shopify os SKUs cujo stock mudou (diferencial).
  * Escreve em lotes de 100 (inventorySetQuantities), em vez de 1 a 1.
  * Repete pedidos com falhas temporárias (timeouts, 429, 5xx).
  * Salvaguardas que cancelam a execução SEM alterar nada quando:
      - o feed da eWheel vem incompleto (faltam demasiados SKUs);
      - demasiados artigos passariam de >0 para 0 de uma só vez.
  * Lê a configuração de variáveis de ambiente (GitHub Actions) ou do .env.
  * Regista cada execução em logs/execucoes.csv e cada alteração em
    logs/alteracoes_stock.csv.

Utilização:
  python sincronizar_stock.py            # pré-visualização, não altera nada
  python sincronizar_stock.py --apply    # aplica as alterações
  python sincronizar_stock.py --apply --forcar   # ignora a salvaguarda de zeros
                                                  # (usar só após verificar)

Códigos de saída: 0 = OK; 1 = cancelado/erro geral (nada ou quase nada
alterado); 2 = concluído mas com erros em alguns SKUs.
"""

import argparse
import csv
import os
import sys
import time
import uuid
from datetime import datetime
from pathlib import Path

import requests

try:
    from dotenv import load_dotenv
except ImportError:  # no GitHub Actions as variáveis vêm do ambiente
    load_dotenv = None

BASE = Path(__file__).resolve().parent
if load_dotenv:
    load_dotenv(BASE / ".env")


def env(*names, default=""):
    for name in names:
        value = (os.getenv(name) or "").strip()
        if value:
            return value
    return default


SHOP = env("SHOPIFY_SHOP").replace("https://", "").replace("http://", "").strip("/").split("/")[0]
CLIENT_ID = env("SHOPIFY_CLIENT_ID")
CLIENT_SECRET = env("SHOPIFY_CLIENT_SECRET")
EWHEEL_KEY = env("EWHEEL_API_KEY", "EWEEL_API_KEY")  # aceita o nome antigo do .env
API_VERSION = env("SHOPIFY_API_VERSION", default="2026-07")
LOCATION_NAME = env("SHOPIFY_LOCATION_NAME", default="Shop location")
EWHEEL_STOCK_URL = env("EWHEEL_STOCK_URL", default="https://partners.ewheel.es/v1/stock")

# Salvaguardas (podem ser ajustadas por variável de ambiente)
MAX_FEED_MISSING_RATIO = float(env("MAX_FEED_MISSING_RATIO", default="0.05"))
MAX_NEW_ZEROS_RATIO = float(env("MAX_NEW_ZEROS_RATIO", default="0.05"))
MAX_NEW_ZEROS_MIN = int(env("MAX_NEW_ZEROS_MIN", default="50"))

SKU_SOURCES = [BASE / "skus_elegiveis.txt", BASE / "catalogo_pronto_para_importar.csv"]
LOG_DIR = BASE / "logs"
RUNS_LOG = LOG_DIR / "execucoes.csv"
CHANGES_LOG = LOG_DIR / "alteracoes_stock.csv"
BATCH_SIZE = 100
RETRY_STATUS = {429, 500, 502, 503, 504}

SESSION = requests.Session()
TOKEN = None


class Abort(Exception):
    """Execução cancelada por segurança, antes de qualquer alteração."""


# --------------------------------------------------------------------------- #
# HTTP
# --------------------------------------------------------------------------- #

def http(method, url, attempts=5, timeout=60, **kwargs):
    last_error = None
    for attempt in range(attempts):
        try:
            r = SESSION.request(method, url, timeout=timeout, **kwargs)
        except (requests.ConnectionError, requests.Timeout) as e:
            last_error = f"{type(e).__name__}: {e}"
        else:
            if r.status_code not in RETRY_STATUS:
                return r
            last_error = f"HTTP {r.status_code}: {r.text[:300]}"
            retry_after = r.headers.get("Retry-After")
            if retry_after and retry_after.replace(".", "", 1).isdigit():
                time.sleep(min(float(retry_after), 60))
                continue
        if attempt < attempts - 1:
            time.sleep(min(2 ** attempt * 2, 30))
    raise RuntimeError(f"{method} {url.split('?')[0]} falhou após {attempts} tentativas ({last_error})")


# --------------------------------------------------------------------------- #
# Shopify
# --------------------------------------------------------------------------- #

def shopify_auth():
    global TOKEN
    r = http("POST", f"https://{SHOP}/admin/oauth/access_token", data={
        "grant_type": "client_credentials",
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
    }, timeout=30)
    if not r.ok:
        raise RuntimeError(f"Autenticação Shopify HTTP {r.status_code}: {r.text[:500]}")
    TOKEN = r.json()["access_token"]


def gql(query, variables=None):
    url = f"https://{SHOP}/admin/api/{API_VERSION}/graphql.json"
    for attempt in range(8):
        r = http("POST", url, json={"query": query, "variables": variables or {}},
                 headers={"X-Shopify-Access-Token": TOKEN, "Content-Type": "application/json"})
        if not r.ok:
            raise RuntimeError(f"Shopify HTTP {r.status_code}: {r.text[:800]}")
        body = r.json()
        errors = body.get("errors")
        if errors:
            if any((e.get("extensions") or {}).get("code") == "THROTTLED" for e in errors):
                time.sleep(2 + attempt * 2)
                continue
            raise RuntimeError(f"Shopify GraphQL: {errors}")
        # Abrandar se o "balde" de custo da API estiver quase vazio.
        throttle = ((body.get("extensions") or {}).get("cost") or {}).get("throttleStatus") or {}
        available = throttle.get("currentlyAvailable")
        restore = throttle.get("restoreRate") or 50
        if available is not None and available < 300:
            time.sleep((300 - available) / restore)
        return body["data"]
    raise RuntimeError("Shopify: limite de pedidos (THROTTLED) repetido.")


def get_location():
    nodes = gql("query { locations(first: 100) { nodes { id name isActive } } }")["locations"]["nodes"]
    active = [n for n in nodes if n["isActive"]]
    if not active:
        raise Abort("Não existe nenhuma localização ativa na Shopify.")
    wanted = [n for n in active if n["name"].strip().lower() == LOCATION_NAME.strip().lower()]
    if wanted:
        return wanted[0]
    if len(active) == 1:
        return active[0]
    raise Abort(
        f"Não encontrei a localização '{LOCATION_NAME}' e existem várias ativas "
        f"({', '.join(n['name'] for n in active)}). Define SHOPIFY_LOCATION_NAME."
    )


VARIANTS_QUERY = """
query Variants($after: String, $loc: ID!) {
  productVariants(first: 100, after: $after) {
    nodes {
      sku
      inventoryItem {
        id
        tracked
        inventoryLevel(locationId: $loc) {
          quantities(names: ["available"]) { name quantity }
        }
      }
    }
    pageInfo { hasNextPage endCursor }
  }
}
"""


def get_shopify_variants(eligible, location_id):
    """Devolve {sku: variante}; SKUs duplicados na Shopify ficam com None."""
    found = {}
    cursor = None
    while True:
        data = gql(VARIANTS_QUERY, {"after": cursor, "loc": location_id})["productVariants"]
        for v in data["nodes"]:
            sku = (v.get("sku") or "").strip()
            if sku and sku in eligible:
                found[sku] = None if sku in found else v
        if not data["pageInfo"]["hasNextPage"]:
            return found
        cursor = data["pageInfo"]["endCursor"]


def current_available(variant):
    level = variant["inventoryItem"].get("inventoryLevel")
    if not level:
        return None
    for q in level.get("quantities") or []:
        if q.get("name") == "available":
            return q.get("quantity")
    return None


def activate_tracking(item_id):
    res = gql("""
    mutation Track($id: ID!, $input: InventoryItemInput!) {
      inventoryItemUpdate(id: $id, input: $input) {
        inventoryItem { id tracked }
        userErrors { field message }
      }
    }""", {"id": item_id, "input": {"tracked": True}})["inventoryItemUpdate"]
    if res["userErrors"] or not (res["inventoryItem"] or {}).get("tracked"):
        raise RuntimeError(f"Não foi possível ativar o controlo de inventário: {res['userErrors']}")


def activate_location(item_id, location_id):
    res = gql("""
    mutation Activate($item: ID!, $location: ID!, $key: String!) {
      inventoryActivate(inventoryItemId: $item, locationId: $location) @idempotent(key: $key) {
        inventoryLevel { id }
        userErrors { field message }
      }
    }""", {"item": item_id, "location": location_id, "key": str(uuid.uuid4())})["inventoryActivate"]
    if res["userErrors"]:
        raise RuntimeError(f"Não foi possível ativar a localização: {res['userErrors']}")


SET_QTY_MUTATION = """
mutation SetQty($input: InventorySetQuantitiesInput!, $key: String!) {
  inventorySetQuantities(input: $input) @idempotent(key: $key) {
    userErrors { field message }
  }
}
"""


def set_quantities(items, location_id):
    """items: lista de dicts com item_id e new. Devolve lista de userErrors."""
    res = gql(SET_QTY_MUTATION, {
        "input": {
            "name": "available",
            "reason": "correction",
            "quantities": [{
                "inventoryItemId": it["item_id"],
                "locationId": location_id,
                "quantity": it["new"],
                "changeFromQuantity": None,
            } for it in items],
        },
        "key": str(uuid.uuid4()),
    })["inventorySetQuantities"]
    return res["userErrors"]


# --------------------------------------------------------------------------- #
# eWheel
# --------------------------------------------------------------------------- #

def get_ewheel_stock():
    """Lê o stock global (GET /v1/stock, paginado por next_cursor).

    Qualquer resposta inesperada cancela a execução: nunca se assume stock 0.
    """
    headers = {"X-API-Key": EWHEEL_KEY, "Accept": "application/json"}
    stocks, cursor, pages, seen = {}, None, 0, set()
    while True:
        r = http("GET", EWHEEL_STOCK_URL, headers=headers,
                 params={"cursor": cursor} if cursor else {})
        if not r.ok:
            raise Abort(f"eWheel /v1/stock HTTP {r.status_code}: {r.text[:500]}")
        try:
            payload = r.json()
        except ValueError:
            raise Abort("eWheel /v1/stock não devolveu JSON válido.")

        if isinstance(payload, list):
            rows, next_cursor = payload, None
        elif isinstance(payload, dict):
            next_cursor = payload.get("next_cursor")
            rows = next((payload[k] for k in ("items", "data", "results", "stock", "stocks")
                         if isinstance(payload.get(k), list)), None)
            if rows is None:
                raise Abort(f"Formato inesperado em /v1/stock; chaves: {list(payload)}")
        else:
            raise Abort("Formato inesperado em /v1/stock.")

        if not rows and pages == 0:
            raise Abort("A eWheel devolveu uma primeira página vazia.")

        for row in rows:
            if not isinstance(row, dict):
                raise Abort("Linha de stock inválida na resposta da eWheel.")
            sku = str(row.get("sku") or "").strip()
            qty = row.get("available")
            if not sku:
                raise Abort("Linha de stock sem SKU na resposta da eWheel.")
            if isinstance(qty, bool) or not isinstance(qty, (int, float)) or qty < 0 or int(qty) != qty:
                raise Abort(f"Valor 'available' inválido para o SKU {sku}: {qty!r}")
            if sku in stocks:
                raise Abort(f"SKU duplicado no feed da eWheel: {sku}")
            stocks[sku] = int(qty)

        pages += 1
        if not next_cursor:
            break
        next_cursor = str(next_cursor)
        if next_cursor in seen:
            raise Abort("A eWheel repetiu o next_cursor; paginação interrompida.")
        seen.add(next_cursor)
        cursor = next_cursor

    return stocks, pages


# --------------------------------------------------------------------------- #
# Auxiliares
# --------------------------------------------------------------------------- #

def read_eligible_skus():
    for path in SKU_SOURCES:
        if not path.exists():
            continue
        if path.suffix == ".txt":
            skus = {line.strip() for line in path.read_text(encoding="utf-8-sig").splitlines()}
        else:
            with path.open(encoding="utf-8-sig", newline="") as f:
                skus = {(row.get("sku") or "").strip() for row in csv.DictReader(f)}
        skus.discard("")
        if skus:
            return skus, path.name
    raise Abort("Não encontrei skus_elegiveis.txt nem catalogo_pronto_para_importar.csv.")


def append_csv(path, header, rows):
    if not rows:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    new = not path.exists()
    with path.open("a", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        if new:
            w.writerow(header)
        w.writerows(rows)


def summary_md(lines):
    """Escreve o resumo na página da execução do GitHub Actions (se aplicável)."""
    target = os.getenv("GITHUB_STEP_SUMMARY")
    if target:
        with open(target, "a", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")


def require_config():
    missing = [k for k, v in {
        "SHOPIFY_SHOP": SHOP, "SHOPIFY_CLIENT_ID": CLIENT_ID,
        "SHOPIFY_CLIENT_SECRET": CLIENT_SECRET, "EWHEEL_API_KEY": EWHEEL_KEY,
    }.items() if not v]
    if missing:
        raise Abort("Faltam variáveis de configuração: " + ", ".join(missing))


# --------------------------------------------------------------------------- #
# Principal
# --------------------------------------------------------------------------- #

def run(apply, force):
    started = time.monotonic()
    now = datetime.now().astimezone().isoformat(timespec="seconds")
    stats = {"recebidos": 0, "paginas": 0, "correspondencias": 0, "sem_alteracao": 0,
             "alterados": 0, "erros": 0, "sem_feed": 0, "duplicados": 0}
    log_rows = []

    def finish(state, detail=""):
        append_csv(CHANGES_LOG, ["data_hora", "sku", "antes", "depois", "resultado", "detalhe"], log_rows)
        append_csv(RUNS_LOG,
                   ["data_hora", "modo", "estado", "recebidos_ewheel", "paginas", "correspondencias",
                    "sem_alteracao", "alterados", "erros", "sem_feed", "duplicados", "duracao_s", "detalhe"],
                   [[now, "apply" if apply else "preview", state, stats["recebidos"], stats["paginas"],
                     stats["correspondencias"], stats["sem_alteracao"], stats["alterados"], stats["erros"],
                     stats["sem_feed"], stats["duplicados"], round(time.monotonic() - started, 1), detail]])
        summary_md([f"### Sincronização de stock eWheel → Shopify: **{state}**", "",
                    "| Indicador | Valor |", "|---|---|"]
                   + [f"| {k} | {v} |" for k, v in stats.items()]
                   + ([f"", f"**Detalhe:** {detail}"] if detail else []))

    try:
        require_config()
        eligible, source = read_eligible_skus()
        print(f"SKUs elegíveis ({source}): {len(eligible)}")

        shopify_auth()
        location = get_location()
        print(f"Localização Shopify: {location['name']}")

        variants = get_shopify_variants(eligible, location["id"])
        duplicates = [s for s, v in variants.items() if v is None]
        matched = {s: v for s, v in variants.items() if v is not None}
        stats["duplicados"] = len(duplicates)
        stats["correspondencias"] = len(matched)
        print(f"Variantes Shopify correspondentes: {len(matched)} | duplicadas (ignoradas): {len(duplicates)}")
        if not matched:
            raise Abort("Nenhuma variante Shopify corresponde aos SKUs elegíveis.")

        feed, pages = get_ewheel_stock()
        stats["recebidos"], stats["paginas"] = len(feed), pages
        print(f"Artigos recebidos da eWheel: {len(feed)} | páginas: {pages}")

        # Salvaguarda 1: feed incompleto
        missing = sorted(s for s in matched if s not in feed)
        stats["sem_feed"] = len(missing)
        if len(missing) > len(matched) * MAX_FEED_MISSING_RATIO:
            raise Abort(f"Feed eWheel incompleto: {len(missing)} de {len(matched)} SKUs não vieram "
                        f"(limite {MAX_FEED_MISSING_RATIO:.0%}). Nada foi alterado.")

        changes = []
        for sku, v in sorted(matched.items()):
            if sku not in feed:
                continue
            item = v["inventoryItem"]
            cur = current_available(v)
            needs_track = not item["tracked"]
            needs_activate = item.get("inventoryLevel") is None
            if not needs_track and not needs_activate and cur == feed[sku]:
                stats["sem_alteracao"] += 1
                continue
            changes.append({"sku": sku, "item_id": item["id"], "old": cur, "new": feed[sku],
                            "track": needs_track, "activate": needs_activate})

        # Salvaguarda 2: demasiados artigos a passar para zero de uma vez
        new_zeros = [c for c in changes if c["new"] == 0 and (c["old"] or 0) > 0]
        zero_limit = max(MAX_NEW_ZEROS_MIN, int(len(matched) * MAX_NEW_ZEROS_RATIO))
        print(f"Alterações necessárias: {len(changes)} | sem alteração: {stats['sem_alteracao']} | "
              f"a passar para zero: {len(new_zeros)} (limite {zero_limit})")
        if len(new_zeros) > zero_limit and not force:
            raise Abort(f"{len(new_zeros)} artigos passariam para stock 0 de uma vez (limite {zero_limit}). "
                        "Verificar o feed e, se estiver correto, executar manualmente com --forcar.")

        if not apply:
            print("\nPRÉ-VISUALIZAÇÃO — exemplos (SKU: Shopify -> eWheel):")
            for c in changes[:25]:
                print(f"  {c['sku']}: {c['old']} -> {c['new']}")
            print("\nNenhuma quantidade foi alterada. Usar --apply para aplicar.")
            finish("PREVIEW")
            return 0

        def log(c, result, detail=""):
            log_rows.append([now, c["sku"], c["old"], c["new"], result, detail])

        # Preparar itens que nunca foram sincronizados (tracking / localização)
        ready = []
        for c in changes:
            try:
                if c["track"]:
                    activate_tracking(c["item_id"])
                if c["activate"]:
                    activate_location(c["item_id"], location["id"])
                ready.append(c)
            except Exception as e:
                stats["erros"] += 1
                log(c, "ERRO", str(e))

        # Escrita em lotes; se um lote falhar, repete artigo a artigo para isolar o erro
        for i in range(0, len(ready), BATCH_SIZE):
            batch = ready[i:i + BATCH_SIZE]
            try:
                errors = set_quantities(batch, location["id"])
            except Exception as e:
                errors = [{"message": str(e)}]
            if not errors:
                for c in batch:
                    stats["alterados"] += 1
                    log(c, "OK")
                continue
            for c in batch:
                try:
                    errs = set_quantities([c], location["id"])
                except Exception as e:
                    errs = [{"message": str(e)}]
                if errs:
                    stats["erros"] += 1
                    log(c, "ERRO", "; ".join(e.get("message", str(e)) for e in errs))
                else:
                    stats["alterados"] += 1
                    log(c, "OK")

        state = "OK" if stats["erros"] == 0 else "COM ERROS"
        finish(state)
        print(f"\n===== {state} ===== alterados: {stats['alterados']} | erros: {stats['erros']} | "
              f"sem alteração: {stats['sem_alteracao']} | fora do feed: {stats['sem_feed']}")
        for row in log_rows:
            if row[4] == "ERRO":
                print(f"  ERRO SKU {row[1]}: {row[5]}")
        return 0 if stats["erros"] == 0 else 2

    except Abort as e:
        print(f"\nCANCELADO (nada alterado): {e}")
        finish("CANCELADO", str(e))
        return 1
    except Exception as e:
        print(f"\nERRO GERAL: {e}")
        finish("ERRO", str(e))
        return 1


def main():
    p = argparse.ArgumentParser(description="Sincroniza stock eWheel -> Shopify.")
    p.add_argument("--apply", action="store_true", help="Aplica as alterações (sem isto é só pré-visualização).")
    p.add_argument("--forcar", action="store_true", help="Ignora a salvaguarda de muitos zeros de uma vez.")
    a = p.parse_args()
    try:
        sys.exit(run(a.apply, a.forcar))
    except KeyboardInterrupt:
        print("\nInterrompido.")
        sys.exit(130)


if __name__ == "__main__":
    main()
