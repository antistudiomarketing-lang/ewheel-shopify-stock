#!/usr/bin/env python3
"""
Encomendas Shopify -> eWheel (API B2C /v1/orders) e seguimento eWheel -> Shopify.

  python encomendas_ewheel.py                 # pré-visualização: mostra o que enviaria (não altera nada)
  python encomendas_ewheel.py --aplicar       # envia encomendas novas e atualiza seguimentos
  python encomendas_ewheel.py --teste-api     # só testa a ligação: permissões Shopify + encomenda fictícia na eWheel
                                              # (apenas com chave de teste ewk_test_: nunca é servida nem cobrada)

O que faz com --aplicar:
  1. Procura encomendas Shopify pagas, por expedir, criadas desde EWHEEL_ENCOMENDAS_DESDE (AAAA-MM-DD)
     e sem a etiqueta "ewheel-enviada".
  2. Envia-as para a eWheel (com chave de teste fica só a etiqueta "ewheel-teste": a encomenda
     NÃO é feita e tem de ser feita à mão no portal B2B)
      (referência = número da encomenda sem "#", ex.: LME1001), com a morada do cliente.
     - aceite -> etiqueta "ewheel-enviada"
     - recusada (sem stock, SKU inválido...) -> etiqueta "ewheel-erro" + motivo em logs/encomendas.csv
       (para voltar a tentar: corrigir e retirar a etiqueta "ewheel-erro")
  3. Lê o estado das encomendas na eWheel. Quando uma está "shipped", marca a encomenda Shopify como
     expedida com a transportadora e o número de seguimento (o cliente recebe o email da Shopify)
     e põe a etiqueta "ewheel-expedida".

A referência impede duplicados: reenviar a mesma encomenda nunca cria uma segunda na eWheel.
"""
import csv, json, os, re, sys, time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

import requests

try:
    from dotenv import load_dotenv
except ImportError:
    load_dotenv = None

BASE = Path(__file__).resolve().parent
if load_dotenv:
    load_dotenv(BASE / ".env")


def env(*names, default=""):
    for n in names:
        v = (os.getenv(n) or "").strip()
        if v:
            return v
    return default


SHOP = env("SHOPIFY_SHOP").replace("https://", "").replace("http://", "").strip("/").split("/")[0]
CID = env("SHOPIFY_CLIENT_ID")
CSEC = env("SHOPIFY_CLIENT_SECRET")
EWKEY = env("EWHEEL_API_KEY", "EWEEL_API_KEY")
EWURL = env("EWHEEL_ORDERS_URL", default="https://partners.ewheel.es/v1/orders")
DESDE = env("EWHEEL_ENCOMENDAS_DESDE", default="2026-11-20")
SERVICO = env("EWHEEL_SHIPPING_SERVICE", default="standard")  # standard | express
API = "2026-07"
LOGS = BASE / "logs"
LOGS.mkdir(exist_ok=True)
S = requests.Session()
TOKEN = None

T_ENVIADA, T_ERRO, T_EXPEDIDA, T_TESTE = "ewheel-enviada", "ewheel-erro", "ewheel-expedida", "ewheel-teste"


# ---------------------------------------------------------------- HTTP / Shopify
def http(method, url, attempts=5, **kw):
    kw.setdefault("timeout", 60)
    for a in range(attempts):
        try:
            r = S.request(method, url, **kw)
        except requests.RequestException:
            if a == attempts - 1:
                raise
            time.sleep(5 * 3 ** a); continue
        if r.status_code in (429, 500, 502, 503, 504) and a < attempts - 1:
            time.sleep(float(r.headers.get("Retry-After") or 5 * 3 ** a)); continue
        return r
    return r


def auth():
    global TOKEN
    r = http("POST", f"https://{SHOP}/admin/oauth/access_token",
             data={"grant_type": "client_credentials", "client_id": CID, "client_secret": CSEC})
    r.raise_for_status()
    b = r.json()
    TOKEN = b["access_token"]
    return set((b.get("scope") or "").split(","))


def gql(q, v=None):
    for a in range(6):
        r = http("POST", f"https://{SHOP}/admin/api/{API}/graphql.json", json={"query": q, "variables": v or {}},
                 headers={"X-Shopify-Access-Token": TOKEN})
        r.raise_for_status()
        b = r.json()
        if b.get("errors"):
            if any((e.get("extensions") or {}).get("code") == "THROTTLED" for e in b["errors"]):
                time.sleep(2 + 2 * a); continue
            raise RuntimeError(b["errors"])
        return b["data"]
    raise RuntimeError("THROTTLED")


def ew(method, path="", body=None, params=None):
    h = {"X-API-Key": EWKEY, "Accept": "application/json"}
    if body is not None:
        h["Content-Type"] = "application/json"
    return http(method, EWURL + path, headers=h, json=body, params=params)


def ref_de(name):
    return re.sub(r"[^A-Za-z0-9._/#-]", "", name.replace("#", ""))[:30]


def registo(linhas):
    f = LOGS / "encomendas.csv"
    novo = not f.exists()
    with open(f, "a", newline="", encoding="utf-8-sig") as fh:
        w = csv.writer(fh, delimiter=";")
        if novo:
            w.writerow(["data", "encomenda", "acao", "resultado", "detalhe"])
        agora = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        for l in linhas:
            w.writerow([agora, *l])


def resumo(txt):
    print(txt)
    p = os.getenv("GITHUB_STEP_SUMMARY")
    if p:
        with open(p, "a", encoding="utf-8") as fh:
            fh.write(txt + "\n")


# ---------------------------------------------------------------- 1) enviar encomendas
QORD = """query O($q:String!,$a:String){ orders(first:25, after:$a, query:$q, sortKey:CREATED_AT){
  nodes{ id name email phone tags note displayFinancialStatus displayFulfillmentStatus
    shippingAddress{ name firstName lastName company address1 address2 zip city countryCodeV2 phone }
    lineItems(first:100){ nodes{ sku currentQuantity requiresShipping name } } }
  pageInfo{ hasNextPage endCursor } } }"""
TAGS = """mutation T($id:ID!,$t:[String!]!){ tagsAdd(id:$id, tags:$t){ userErrors{ message } } }"""


def encomendas_novas():
    q = (f"financial_status:paid fulfillment_status:unfulfilled created_at:>={DESDE} "
         f"-tag:{T_ENVIADA} -tag:{T_ERRO} -tag:{T_TESTE} -status:cancelled")
    out, a = [], None
    while True:
        d = gql(QORD, {"q": q, "a": a})["orders"]
        out += d["nodes"]
        if not d["pageInfo"]["hasNextPage"]:
            return out
        a = d["pageInfo"]["endCursor"]


def montar(o):
    linhas = {}
    for li in o["lineItems"]["nodes"]:
        if not li["requiresShipping"] or not li["currentQuantity"]:
            continue
        if not li["sku"]:
            raise ValueError(f"artigo sem SKU: {li['name']}")
        linhas[li["sku"]] = linhas.get(li["sku"], 0) + li["currentQuantity"]
    if not linhas:
        raise ValueError("sem artigos para enviar")
    a = o.get("shippingAddress")
    if not a:
        raise ValueError("encomenda sem morada de envio")
    nome = a.get("name") or f"{a.get('firstName') or ''} {a.get('lastName') or ''}".strip()
    body = {
        "reference": ref_de(o["name"]),
        "lines": [{"sku": s, "quantity": q} for s, q in linhas.items()],
        "shipping_address": {
            "name": nome, "company": a.get("company") or "", "street": a.get("address1") or "",
            "street2": a.get("address2") or "", "postal_code": a.get("zip") or "", "city": a.get("city") or "",
            "country": a.get("countryCodeV2") or "PT", "phone": a.get("phone") or o.get("phone") or "",
            "email": o.get("email") or "",
        },
        "shipping_service": SERVICO,
    }
    if o.get("note"):
        body["notes"] = o["note"][:500]
    return body


def enviar(aplicar):
    novas = encomendas_novas()
    reg, ok, erros = [], 0, 0
    for o in novas:
        try:
            body = montar(o)
        except ValueError as e:
            erros += 1
            reg.append([o["name"], "enviar", "ERRO", str(e)])
            if aplicar:
                gql(TAGS, {"id": o["id"], "t": [T_ERRO]})
            continue
        if not aplicar:
            print("ENVIARIA", o["name"], json.dumps(body["lines"]), body["shipping_address"]["city"],
                  body["shipping_address"]["country"])
            continue
        r = ew("POST", body=body)
        if r.status_code in (200, 202):
            b = r.json()
            ok += 1
            # Com chave de teste (modo "shadow") a encomenda NÃO é feita na eWheel: fica só "ewheel-teste"
            # e tem de ser feita à mão no portal B2B.
            real = b.get("mode") == "live" and b.get("environment") != "test"
            gql(TAGS, {"id": o["id"], "t": [T_ENVIADA if real else T_TESTE]})
            reg.append([o["name"], "enviar", "OK", f"estado={b.get('status')} modo={b.get('mode')} "
                                                  f"subtotal={b.get('subtotal')} duplicado={b.get('duplicate')}"])
        elif r.status_code in (400, 409, 422):
            erros += 1
            gql(TAGS, {"id": o["id"], "t": [T_ERRO]})
            reg.append([o["name"], "enviar", f"RECUSADA {r.status_code}", r.text[:800]])
        else:  # 401/403/5xx: não marca; tenta de novo na próxima execução
            erros += 1
            reg.append([o["name"], "enviar", f"FALHOU {r.status_code}", r.text[:800]])
    registo(reg)
    for l in reg:
        print(" | ".join(l))
    return len(novas), ok, erros


# ---------------------------------------------------------------- 2) seguimento
QBYNAME = """query N($q:String!){ orders(first:1, query:$q){ nodes{ id name tags
  fulfillmentOrders(first:10){ nodes{ id status } } } } }"""
FULFIL = """mutation F($f: FulfillmentInput!){ fulfillmentCreate(fulfillment:$f){
  fulfillment{ id status } userErrors{ field message } } }"""


def encomendas_ewheel():
    out, cursor = [], None
    for _ in range(20):
        params = {"limit": 100}
        if cursor:
            params["cursor"] = cursor
        r = ew("GET", params=params)
        r.raise_for_status()
        b = r.json()
        out += b.get("data") or b.get("orders") or b.get("items") or []
        if not b.get("has_more"):
            break
        cursor = b.get("next_cursor")
    return out


def seguimento(aplicar):
    reg, feitas = [], 0
    for e in encomendas_ewheel():
        if e.get("status") != "shipped" or not e.get("shipments"):
            continue
        ref = e["reference"]
        n = gql(QBYNAME, {"q": f"name:{ref}"})["orders"]["nodes"]
        if not n or ref_de(n[0]["name"]) != ref:
            n = gql(QBYNAME, {"q": f"name:#{ref}"})["orders"]["nodes"]
        if not n:
            reg.append([ref, "seguimento", "ERRO", "encomenda Shopify não encontrada"]); continue
        o = n[0]
        if T_EXPEDIDA in o["tags"]:
            continue
        fos = [f["id"] for f in o["fulfillmentOrders"]["nodes"] if f["status"] in ("OPEN", "IN_PROGRESS")]
        s = e["shipments"][0]
        info = {"company": s.get("carrier") or "UPS", "number": s.get("tracking_number") or ""}
        if s.get("tracking_url"):
            info["url"] = s["tracking_url"]
        if not aplicar:
            print("EXPEDIRIA", o["name"], info); continue
        if fos:
            r = gql(FULFIL, {"f": {"lineItemsByFulfillmentOrder": [{"fulfillmentOrderId": i} for i in fos],
                                   "trackingInfo": info, "notifyCustomer": True}})["fulfillmentCreate"]
            if r["userErrors"]:
                reg.append([o["name"], "seguimento", "ERRO", json.dumps(r["userErrors"], ensure_ascii=False)]); continue
        gql(TAGS, {"id": o["id"], "t": [T_EXPEDIDA]})
        feitas += 1
        reg.append([o["name"], "seguimento", "OK", f"{info['company']} {info['number']}"])
    registo(reg)
    for l in reg:
        print(" | ".join(l))
    return feitas


# ---------------------------------------------------------------- teste
def teste_api(scopes):
    precisa = {"read_orders", "write_orders", "read_merchant_managed_fulfillment_orders",
               "write_merchant_managed_fulfillment_orders"}
    falta = sorted(p for p in precisa if p not in scopes and p.replace("read_", "write_") not in scopes)
    print("Permissões Shopify em falta:", ", ".join(falta) if falta else "nenhuma")
    if not EWKEY.startswith("ewk_test_"):
        print("A chave eWheel não é de teste: não envio encomenda fictícia."); return
    body = {"reference": "TESTE-" + datetime.now().strftime("%m%d%H%M%S"),
            "lines": [{"sku": "CMM648", "quantity": 1}],
            "shipping_address": {"name": "Teste Integracao", "street": "Rua de Teste 1", "postal_code": "1000-001",
                                 "city": "Lisboa", "country": "PT", "phone": "", "email": ""},
            "shipping_service": "standard", "notes": "Teste de integração (chave de teste)"}
    r = ew("POST", body=body)
    print("POST /v1/orders ->", r.status_code, r.headers.get("X-Api-Environment"), r.text[:600])
    r = ew("GET", "/" + quote(body["reference"], safe=""))
    print("GET /v1/orders/{ref} ->", r.status_code, r.text[:600])
    r = ew("GET", params={"limit": 3})
    print("GET /v1/orders ->", r.status_code, r.text[:400])


def main():
    if not (SHOP and CID and CSEC and EWKEY):
        sys.exit("Faltam variáveis: SHOPIFY_SHOP, SHOPIFY_CLIENT_ID, SHOPIFY_CLIENT_SECRET, EWHEEL_API_KEY")
    scopes = auth()
    if "--teste-api" in sys.argv:
        teste_api(scopes); return
    aplicar = "--aplicar" in sys.argv
    modo = "teste (shadow)" if EWKEY.startswith("ewk_test_") else "produção"
    n, ok, err = enviar(aplicar)
    exp = seguimento(aplicar)
    resumo(f"### Encomendas eWheel ({'aplicar' if aplicar else 'pré-visualização'}, chave de {modo})\n"
           f"- Encomendas novas encontradas: {n} | enviadas: {ok} | com erro: {err}\n"
           f"- Marcadas como expedidas: {exp}")
    sys.exit(1 if err else 0)


if __name__ == "__main__":
    main()
