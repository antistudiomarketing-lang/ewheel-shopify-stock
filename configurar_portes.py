#!/usr/bin/env python3
"""
Configura os portes de envio da loja (Portugal), com base nos custos da eWheel.

  python configurar_portes.py            # pré-visualização (mostra o plano, não altera nada)
  python configurar_portes.py --aplicar  # aplica

Perfil geral (peças e acessórios):
  - Portugal continental: 4,95 € (encomendas < 50 €) | grátis a partir de 50 €
  - Madeira: 25,00 €
  - Açores: 30,00 €
Perfil "Trotinetes e bicicletas" (produtos com as etiquetas abaixo):
  - Portugal continental: grátis (ilhas: não disponível)
Valores sem IVA (a loja mostra preços sem IVA).
"""
import json, os, sys, time
import requests

SHOP = (os.getenv("SHOPIFY_SHOP") or "").replace("https://", "").strip("/")
CID, CSEC = os.getenv("SHOPIFY_CLIENT_ID", "").strip(), os.getenv("SHOPIFY_CLIENT_SECRET", "").strip()
API = "2026-07"
CONT, LIMITE, MADEIRA, ACORES = "4.95", "50.00", "25.00", "30.00"
TAGS_GRANDES = ["categoria-trotinetes", "categoria-bicicletas-e-bicicletas-eletri"]
S = requests.Session()


def token():
    r = S.post(f"https://{SHOP}/admin/oauth/access_token", timeout=60,
               data={"grant_type": "client_credentials", "client_id": CID, "client_secret": CSEC})
    r.raise_for_status()
    return r.json()["access_token"]


TOK = None


def gql(q, v=None):
    for a in range(6):
        r = S.post(f"https://{SHOP}/admin/api/{API}/graphql.json", timeout=120,
                   json={"query": q, "variables": v or {}}, headers={"X-Shopify-Access-Token": TOK})
        if r.status_code in (429, 500, 502, 503):
            time.sleep(3 * (a + 1)); continue
        r.raise_for_status()
        b = r.json()
        if b.get("errors"):
            if any((e.get("extensions") or {}).get("code") == "THROTTLED" for e in b["errors"]):
                time.sleep(3); continue
            raise RuntimeError(json.dumps(b["errors"], ensure_ascii=False)[:1500])
        return b["data"]
    raise RuntimeError("falhou")


def erros(r, onde):
    if r.get("userErrors"):
        raise RuntimeError(f"{onde}: {json.dumps(r['userErrors'], ensure_ascii=False)}")


def rate(nome, preco, minimo=None, maximo=None):
    m = {"name": nome, "active": True, "rateDefinition": {"price": {"amount": preco, "currencyCode": "EUR"}}}
    conds = []
    if minimo:
        conds.append({"operator": "GREATER_THAN_OR_EQUAL_TO", "criteria": {"amount": minimo, "currencyCode": "EUR"}})
    if maximo:
        conds.append({"operator": "LESS_THAN_OR_EQUAL_TO", "criteria": {"amount": maximo, "currencyCode": "EUR"}})
    if conds:
        m["priceConditionsToCreate"] = conds
    return m


def main(aplicar):
    global TOK
    TOK = token()
    # províncias de Portugal (para separar continente / Madeira / Açores)
    pc = gql("""{ deliveryProfiles(first:10){ nodes{ id name default
        profileLocationGroups{ locationGroup{ id } locationGroupZones(first:30){ nodes{ zone{ id name countries{ code{ countryCode } provinces{ code name } } } } } } } } }""")
    perfis = pc["deliveryProfiles"]["nodes"]
    geral = [p for p in perfis if p["default"]][0]
    zonas_existentes, provs = [], {}
    for g in geral["profileLocationGroups"]:
        for z in g["locationGroupZones"]["nodes"]:
            zonas_existentes.append(z["zone"]["id"])
            for c in z["zone"]["countries"]:
                if c["code"]["countryCode"] == "PT":
                    for p in c["provinces"]:
                        provs[p["code"]] = p["name"]
    if not provs:
        # obter a lista de distritos através de uma zona temporária não é possível; usar códigos ISO conhecidos
        provs = {f"PT-{i:02d}": f"PT-{i:02d}" for i in range(1, 19)}
        provs.update({"PT-20": "Açores", "PT-30": "Madeira"})
    acores = [c for c, n in provs.items() if c == "PT-20" or "çores" in n or "zores" in n]
    madeira = [c for c, n in provs.items() if c == "PT-30" or "adeira" in n]
    continente = [c for c in provs if c not in acores + madeira]
    loc = [l["id"] for l in gql("{ locations(first:5){ nodes{ id isActive } } }")["locations"]["nodes"] if l["isActive"]][0]

    zonas_geral = [
        {"name": "Portugal continental", "countries": [{"code": "PT", "provinces": [{"code": c} for c in continente]}],
         "methodDefinitionsToCreate": [rate("Envio standard (24-48h)", CONT, maximo="49.99"),
                                       rate("Envio grátis (24-48h)", "0.00", minimo=LIMITE)]},
        {"name": "Madeira", "countries": [{"code": "PT", "provinces": [{"code": c} for c in madeira]}],
         "methodDefinitionsToCreate": [rate("Envio standard (3-7 dias)", MADEIRA)]},
        {"name": "Açores", "countries": [{"code": "PT", "provinces": [{"code": c} for c in acores]}],
         "methodDefinitionsToCreate": [rate("Envio standard (3-7 dias)", ACORES)]},
    ]
    # variantes dos produtos grandes (trotinetes e bicicletas)
    vids, a = [], None
    q = " OR ".join(f"tag:{t}" for t in TAGS_GRANDES)
    while True:
        d = gql("""query Q($q:String!,$a:String){ products(first:100, after:$a, query:$q){ nodes{ variants(first:100){ nodes{ id } } }
                 pageInfo{ hasNextPage endCursor } } }""", {"q": q, "a": a})["products"]
        for p in d["nodes"]:
            vids += [v["id"] for v in p["variants"]["nodes"]]
        if not d["pageInfo"]["hasNextPage"]:
            break
        a = d["pageInfo"]["endCursor"]

    print("Continente:", len(continente), "distritos | Madeira:", madeira, "| Açores:", acores)
    print("Zonas atuais a substituir no perfil geral:", len(zonas_existentes))
    print("Variantes de trotinetes/bicicletas:", len(vids))
    if not aplicar:
        print("Pré-visualização: nada foi alterado."); return

    lg_id = geral["profileLocationGroups"][0]["locationGroup"]["id"] if geral["profileLocationGroups"] else None
    if lg_id:
        inp = {"zonesToDelete": zonas_existentes,
               "locationGroupsToUpdate": [{"id": lg_id, "zonesToCreate": zonas_geral}]}
    else:
        inp = {"locationGroupsToCreate": [{"locations": [loc], "zonesToCreate": zonas_geral}]}
    r = gql("""mutation U($id:ID!,$p:DeliveryProfileInput!){ deliveryProfileUpdate(id:$id, profile:$p){ profile{ id } userErrors{ field message } } }""",
            {"id": geral["id"], "p": inp})["deliveryProfileUpdate"]
    erros(r, "perfil geral")
    print("Perfil geral configurado.")

    nome = "Trotinetes e bicicletas"
    existente = [p for p in perfis if p["name"] == nome]
    zona_grandes = [{"name": "Portugal continental", "countries": [{"code": "PT", "provinces": [{"code": c} for c in continente]}],
                     "methodDefinitionsToCreate": [rate("Envio grátis (3 dias)", "0.00")]}]
    if existente:
        print("Perfil", nome, "já existe: só associo as variantes.")
        r = gql("""mutation U($id:ID!,$p:DeliveryProfileInput!){ deliveryProfileUpdate(id:$id, profile:$p){ userErrors{ message } } }""",
                {"id": existente[0]["id"], "p": {"variantsToAssociate": vids}})["deliveryProfileUpdate"]
    else:
        r = gql("""mutation C($p:DeliveryProfileInput!){ deliveryProfileCreate(profile:$p){ profile{ id } userErrors{ field message } } }""",
                {"p": {"name": nome, "locationGroupsToCreate": [{"locations": [loc], "zonesToCreate": zona_grandes}],
                       "variantsToAssociate": vids}})["deliveryProfileCreate"]
    erros(r, nome)
    print(f"Perfil '{nome}' configurado com {len(vids)} variantes.")


if __name__ == "__main__":
    if not (SHOP and CID and CSEC):
        sys.exit("Faltam SHOPIFY_SHOP / SHOPIFY_CLIENT_ID / SHOPIFY_CLIENT_SECRET")
    main("--aplicar" in sys.argv)
