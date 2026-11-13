# Integração eWheel → Shopify (Loja da Mobilidade Elétrica)

Sincronização automática do stock da eWheel para a Shopify, a correr no
GitHub Actions de 15 em 15 minutos, sem depender de nenhum computador ligado.

## Ficheiros

| Ficheiro | Função |
|---|---|
| `sincronizar_stock.py` | Script de sincronização (o que corre no GitHub) |
| `skus_elegiveis.txt` | Lista de SKUs que podem ser sincronizados (gerada a partir de `catalogo_pronto_para_importar.csv`) |
| `workflow-github/sincronizar-stock.yml` | Agendamento de 15 em 15 minutos — **mover para `.github/workflows/`** antes de enviar para o GitHub |
| `requirements.txt` | Dependências Python |
| `sincronizar_stock_ewheel_todos.py` | Versão anterior (manual), mantida como referência |

## Como funciona

1. Lê o stock global da eWheel (`GET /v1/stock`, todas as páginas).
2. Lê as variantes da Shopify e a quantidade atual na localização.
3. **Só escreve os SKUs cujo stock mudou**, em lotes de 100.
4. Regista cada execução em `logs/execucoes.csv` e cada alteração em `logs/alteracoes_stock.csv`.

### Salvaguardas (a execução é cancelada sem alterar nada)
- Erro, timeout ou resposta inesperada da eWheel (após 5 tentativas). **Um erro nunca é tratado como stock 0.**
- Mais de 5% dos SKUs da loja em falta no feed (feed incompleto).
- Mais de 5% dos artigos (mínimo 50) a passar de >0 para 0 de uma só vez.
  Se isto for real, correr manualmente com a opção "forcar".
- SKUs fora do feed, duplicados na Shopify ou fora de `skus_elegiveis.txt` nunca são alterados.

Códigos de saída: `0` OK · `1` cancelado/erro geral · `2` concluído com erros em alguns SKUs.
Em `1` ou `2` a execução fica a vermelho no GitHub e o dono do repositório recebe um email.

## Uso local (Windows)

```
py -m pip install -r requirements.txt
py sincronizar_stock.py            # pré-visualização
py sincronizar_stock.py --apply    # aplicar
```
Lê as credenciais do `.env` (o nome antigo `EWEEL_API_KEY` continua a funcionar).

## Instalação no GitHub (uma vez)

1. Criar um repositório novo no GitHub (ver nota sobre minutos abaixo).
2. Mover `workflow-github/sincronizar-stock.yml` para `.github/workflows/sincronizar-stock.yml` e enviar esta pasta. O `.gitignore` já exclui o `.env`, os CSV e os logs.
3. Em **Settings → Secrets and variables → Actions → New repository secret**, criar:
   - `SHOPIFY_SHOP` (ex.: `mobilidade-eletrica-j2qk3fhh.myshopify.com`)
   - `SHOPIFY_CLIENT_ID`
   - `SHOPIFY_CLIENT_SECRET`
   - `EWHEEL_API_KEY`
   - (opcional, separador *Variables*) `SHOPIFY_LOCATION_NAME`, se a localização não for "Shop location".
4. Em **Actions → Sincronizar stock eWheel → Run workflow**, correr primeiro com `preview` e ver o resumo.
5. Se estiver correto, o agendamento passa a correr sozinho.

### Nota sobre minutos do GitHub Actions
De 15 em 15 minutos são ~2.900 execuções/mês, e cada uma conta pelo menos 1 minuto.
- **Repositório público:** minutos ilimitados e gratuitos. O código não tem credenciais
  (estão nos *secrets*), mas o código e a lista de SKUs ficam visíveis. Num repositório
  público sem atividade durante 60 dias o GitHub desativa os agendamentos; basta reativar.
- **Repositório privado (plano Free):** 2.000 min/mês, o que não chega para 15 em 15 minutos.
  Opções: mudar o `cron` para 30 em 30 minutos (`7,37 * * * *`), ou usar GitHub Pro (3.000 min).

## Encomendas B2C
Pendente da nova API B2C da eWheel para a conta 8662. **Não usar** `variantReference` /
`customerReference` (API antiga Upango, a ser encerrada). Os scripts
`integrar_encomenda_teste_ewheel*.py` usam a API antiga e servem só de histórico.

## Segurança
- Rodar a chave eWheel e o *client secret* da Shopify se tiverem sido expostos durante os testes,
  e atualizar os *secrets* no GitHub.
- Nunca colocar credenciais em código, capturas de ecrã ou relatórios.
