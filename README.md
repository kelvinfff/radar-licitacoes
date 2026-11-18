# Radar de Licitações

Assistente que responde perguntas sobre contratações públicas a partir dos dados abertos do [PNCP](https://pncp.gov.br), citando a fonte. A especificação completa está em [`SPEC.md`](SPEC.md).

> **Em construção.** Etapa atual: 1 de 8, coleta dos metadados das contratações. Arquitetura, números de avaliação e demo entram neste README na Etapa 8.

## Como rodar

Requisitos: [uv](https://docs.astral.sh/uv/) e Docker.

```bash
cp .env.example .env
docker compose up -d db          # Postgres 18 + pgvector em 127.0.0.1:5433
uv sync
uv run alembic upgrade head
```

### Coleta

```bash
uv run radar ingest                                    # tudo o que estiver pendente até ontem
uv run radar ingest --de 2026-11-01 --ate 2026-11-07   # publicações de um período (backfill)
uv run radar ingest --de 2026-11-01 --forcar           # coleta de novo um período já concluído
uv run radar status                                    # o que já está no banco
```

Códigos de saída: `0` sucesso (ou outra ingestão já em andamento), `1` falha ou coleta parcial, `2` erro de uso.

### Testes

```bash
uv run pytest                        # tudo; os testes de integração usam o Postgres do compose
uv run pytest -m "not integracao"    # só os de unidade, sem banco
uv run ruff check . && uv run mypy
```

Os testes do coletor rodam contra respostas reais da API, gravadas em `tests/fixtures/pncp/` por `scripts/gravar_fixtures.py`.

## Como a coleta funciona

- **Recorte:** contratações de órgãos de `PNCP_UF` publicadas a partir de `PNCP_DATA_INICIO` (padrão: PE, desde 01/01/2026).
- **Janelas:** a API exige a modalidade em toda consulta e devolve no máximo 50 itens por página. O coletor divide o trabalho em janelas de até 7 dias por modalidade, e cada janela é baixada por inteiro e gravada numa única transação.
- **Checkpoint:** cada (fonte, modalidade, dia) concluído fica em `coletas_concluidas`. Se a execução cair no meio, a próxima continua de onde parou. O dia de hoje nunca é coletado, porque ainda não terminou.
- **Idempotência:** a gravação é um upsert pela chave natural (CNPJ do órgão, ano, sequencial) que só reescreve a linha quando o item mudou. Rodar de novo não duplica nada.
- **Atualizações:** contratações mudam depois de publicadas (homologação, suspensão, anulação). Depois das publicações, o coletor busca o endpoint de atualizações a partir do dia da primeira coleta.
- **Falhas:** novas tentativas com backoff exponencial em erros 429/5xx e de rede. Uma janela que falha não marca seus dias como concluídos; depois de 3 janelas seguidas com falha, a execução para.
- **Paginação conferida:** se chegar menos registros do que a API anunciou, os dias não são marcados e a janela é refeita na próxima execução.
- **Quarentena:** itens que não passam na validação ficam em `registros_invalidos` com o erro, em vez de sumir.
- **Uma execução por vez**, garantida por um advisory lock do Postgres. Cada execução fica registrada em `execucoes_ingestao` com suas métricas.
