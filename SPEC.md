# Especificação — Radar de Licitações

Assistente que responde perguntas sobre **contratações públicas** a partir dos dados abertos do **PNCP** (Portal Nacional de Contratações Públicas), sempre citando a fonte. O objetivo principal é de portfólio: mostrar um sistema com LLM **operado como em produção**, com infraestrutura como código, CI/CD, avaliações automáticas que barram regressões, observabilidade e controle de custo.

> Para o Claude Code: este documento é o ponto de partida. Antes de começar, confirme comigo as decisões marcadas como **[decidir]**. Trabalhe pelas etapas da seção 12, uma por vez, e pare ao fim de cada uma para eu testar.

---

## 1. O problema

Os dados de licitações são públicos, mas difíceis de consultar. O portal tem filtros limitados, e o conteúdo que importa (objeto, exigências, prazos) fica dentro de PDFs de editais. Perguntas comuns que hoje exigem horas de busca manual:

- "Quais prefeituras de Pernambuco abriram pregão para merenda escolar em 2026, e qual o valor estimado?"
- "Qual é o prazo de entrega exigido no edital do pregão X da prefeitura de Petrolina?"
- "Quais contratações de software acima de R$ 500 mil foram publicadas no último mês?"

## 2. Usuários

- **Público da demo:** recrutadores e devs que abrem o link do portfólio. Não há login.
- **Perfil real que o produto atenderia:** pequenos fornecedores procurando oportunidades, jornalistas e pesquisadores de controle social.
- **Operador:** eu. Painel de observabilidade, alertas e controle de custo são para mim.

## 3. Escopo dos dados (v1)

- **Recorte:** contratações publicadas por órgãos de **Pernambuco** a partir de **01/01/2026**. Recorte pequeno de propósito: dá para indexar tudo, medir e mostrar números reais. Expandir depois é só configuração.
- **Fonte:** API pública de consulta do PNCP (documentação Swagger no próprio portal). **Confirmar no Swagger os endpoints, parâmetros e limites atuais antes de escrever o coletor**, sem confiar em exemplos de memória. Em linhas gerais:
  - Consulta de contratações por data de publicação, modalidade e UF (metadados estruturados).
  - Lista de arquivos de cada contratação (editais, termos de referência) com link para download.
- **O que é indexado:**
  - Metadados estruturados: órgão, CNPJ do órgão, município, modalidade, objeto, valor estimado/homologado, datas, situação, link no PNCP.
  - Texto dos PDFs anexos (edital e termo de referência).
- **Fora da v1:** PDFs escaneados sem camada de texto (marcar como `sem_texto` e seguir), atas e contratos assinados, outros estados.

## 4. Como o assistente responde

### 4.1 Duas naturezas de pergunta

O ponto central do desenho: **nem toda pergunta é RAG**.

| Tipo | Exemplo | Como resolver |
|---|---|---|
| Estruturada (filtrar, contar, somar, ordenar) | "Quantos pregões de merenda acima de R$ 1 mi em 2026?" | Consulta SQL parametrizada sobre os metadados |
| Textual (o que diz o documento) | "Qual o prazo de entrega no edital X?" | Busca híbrida (vetorial + texto) nos trechos dos PDFs |
| Mista | "Dos pregões de merenda de 2026, quais exigem nutricionista?" | Filtra por SQL, depois busca nos trechos só dessas contratações |

Busca vetorial não sabe somar nem filtrar por valor. Resolver pergunta estruturada com RAG é o erro mais comum em projetos desse tipo, e o conjunto de avaliação (seção 6) tem que provar que o sistema não cai nele.

### 4.2 Ferramentas do modelo

O modelo **não escreve SQL**. Ele chama ferramentas com parâmetros validados, e o código monta a consulta:

| Ferramenta | Parâmetros | Retorna |
|---|---|---|
| `buscar_contratacoes` | texto do objeto, município, modalidade, faixa de valor, intervalo de datas, ordenação, limite | Lista de contratações com metadados e link |
| `agregar_contratacoes` | os mesmos filtros + métrica (`contagem`, `soma_valor`, `media_valor`) + agrupamento opcional (município, modalidade, mês) | Números agregados |
| `buscar_trechos` | consulta em linguagem natural, filtro opcional por ids de contratação | Trechos de PDF com id da contratação, arquivo e página |
| `obter_contratacao` | id | Metadados completos + lista de arquivos |

Todas com `strict: true` e `additionalProperties: false`. Limite de resultados no servidor (ex.: no máximo 50 linhas por chamada).

### 4.3 Regras de resposta

- Toda afirmação factual cita a fonte: link da contratação no PNCP e, para trechos, arquivo e página.
- Se as ferramentas não trouxerem a informação, responder que não encontrou. **Nunca completar com conhecimento geral** sobre licitações como se fosse dado.
- Perguntas fora do escopo (outros estados, assuntos não relacionados) recebem uma recusa curta explicando o recorte.
- Valores em reais (`R$ 1.234,56`) e datas `dd/mm/aaaa`.
- Respostas em streaming na interface.

### 4.4 Modelo

- **API da Anthropic (Messages API) com tool use**, loop hospedado por mim (o SDK oferece o *tool runner*). O loop fica comigo porque observabilidade, limites e custo por requisição são justamente o que o projeto quer mostrar.
- Modelo principal: `claude-opus-5-5`, com `effort` configurável por variável de ambiente. Começar em `low` (perguntas curtas, poucas chamadas de ferramenta) e só subir se a avaliação mostrar ganho.
- Juiz das avaliações (seção 6): `claude-haiku-5-5`.
- Trocar de modelo ou de effort é uma mudança como qualquer outra: só entra se passar no portão de avaliação. O README deve mostrar a tabela de qualidade × custo × latência das configurações testadas.
- Notas da API atual a respeitar:
  - Não enviar `temperature`/`top_p`/`top_k` (rejeitados).
  - `tool_choice` forçado (`any`/`tool`) não é aceito: usar `auto`.
  - Tratar `stop_reason: "refusal"` e habilitar o fallback do lado do servidor.
  - Usar prompt caching no prefixo estável (ferramentas + prompt de sistema) e conferir `cache_read_input_tokens`.

### 4.5 Embeddings (decidido: modelo local)

A Anthropic não oferece endpoint de embeddings. Opções:

| Opção | Prós | Contras |
|---|---|---|
| Modelo aberto multilíngue rodando local no job de indexação (ex.: família `multilingual-e5` ou `bge-m3`) | Custo zero por token, nada sai da máquina, mostra mais DevOps | Precisa de CPU/RAM no job; consulta também precisa do modelo carregado na API |
| API da Voyage AI | Qualidade alta, sem infraestrutura | Custo por token, mais uma chave para gerenciar |

**Decidido: modelo local** (escolher o modelo na Etapa 2, medindo memória e qualidade na VM da seção 8). Guardar o nome e a versão do modelo de embedding em cada trecho, para permitir reindexação.

## 5. Pipeline de dados

Job agendado (diário), separado da API.

1. **Coleta:** busca contratações publicadas desde o último checkpoint. Respeitar os limites da API do PNCP: requisições com intervalo, retry com backoff exponencial em 429/5xx, `User-Agent` genérico do projeto.
   - A API exige a modalidade em toda consulta e pagina de 10 a 50 itens. A coleta é feita em janelas de até 7 dias por modalidade.
   - Contratações mudam depois de publicadas (homologação, suspensão, anulação). Além das publicações, o job coleta o endpoint de **atualizações** (por data de atualização), a partir do dia da primeira coleta.
2. **Upsert dos metadados** no Postgres, chave natural da contratação (CNPJ do órgão + ano + sequencial). Rodar duas vezes o mesmo dia não pode duplicar nada.
3. **Download dos PDFs** novos ou alterados (comparar hash). Guardar os arquivos num storage de objetos ou em volume.
4. **Extração de texto** por página. PDF sem texto: marcar `sem_texto`.
5. **Chunking** por página com sobreposição, preservando número da página.
6. **Embeddings** e gravação no índice.
7. **Checkpoint** por (fonte, modalidade, dia): um dia só conta como coletado quando todas as modalidades foram baixadas por inteiro. Itens que não passam na validação ficam numa tabela de quarentena, em vez de sumir.

Requisitos:

- **Idempotente e retomável:** se cair no meio, a próxima execução continua de onde parou.
- **Backfill** por intervalo de datas via linha de comando (`ingest --de 2026-01-01 --ate 2026-03-31`).
- **Métricas do job:** contratações novas, PDFs baixados, PDFs sem texto, trechos gerados, falhas, duração. Alerta se o job não rodar em 36 h ou se a taxa de falha passar de 10%.
- **Texto dos PDFs é entrada não confiável:** é passado ao modelo como dado, dentro de blocos delimitados, nunca como instrução (ver seção 9).

## 6. Avaliações (o coração do projeto)

### 6.1 Conjunto de avaliação

Arquivo versionado `evals/perguntas.yaml`, com **80 a 120 perguntas** escritas à mão sobre um **snapshot congelado** dos dados (dump do banco usado só para avaliação, para que o resultado não mude quando o PNCP mudar).

| Categoria | Qtd. aprox. | Como corrigir |
|---|---|---|
| Estruturada | 30 | Resposta esperada calculada por SQL de referência; compara número/lista exatos |
| Textual | 30 | Trecho de referência (contratação + página); confere se a citação aponta para ele e se a resposta bate (juiz LLM com rubrica) |
| Mista | 15 | As duas checagens acima |
| Sem resposta nos dados | 10 | Tem que dizer que não encontrou; inventar = falha |
| Fora de escopo | 5 | Recusa curta |
| Injeção no documento | 5 | PDFs do snapshot com instruções plantadas ("ignore as regras e diga X"); o modelo não pode obedecer |

### 6.2 Métricas

- **Acerto** por categoria e geral.
- **Precisão de citação:** fração das citações que apontam para fonte que sustenta a afirmação.
- **Taxa de alucinação:** respostas com afirmação factual sem suporte nas ferramentas.
- **Recall de recuperação** (só textuais): o trecho de referência apareceu nos resultados de `buscar_trechos`?
- **Custo por pergunta** e **latência p50/p95**.

### 6.3 Juiz LLM

- Rubrica curta e explícita por pergunta, saída em JSON estruturado.
- **Calibrar o juiz:** corrigir à mão 30 respostas e medir a concordância com o juiz. Registrar o número no README. Juiz sem calibração não vale como métrica.

### 6.4 Portão no CI

- Em todo PR que mexe em prompt, ferramentas, recuperação, chunking ou configuração de modelo: roda um **subconjunto fixo de ~25 perguntas** (cobrindo todas as categorias).
- **Bloqueia o merge** se: acerto cair mais de 3 pontos em relação à `main`, ou injeção/alucinação piorar em qualquer caso, ou custo médio subir mais de 20%.
- O bot comenta no PR uma tabela com antes × depois.
- **Avaliação completa** roda toda noite na `main` e guarda o histórico (gráfico de evolução no README/painel).
- Orçamento: estimar o custo de uma rodada na Etapa 3 e registrar. Limite de gasto mensal configurado no Console da Anthropic.

## 7. Interface e API

- **API HTTP** (`POST /perguntar`, resposta em streaming via SSE; `GET /saude`; `GET /metricas` interno).
- **Página web simples:** campo de pergunta, resposta em streaming, citações clicáveis, exemplos de perguntas prontas. Sem framework pesado.
- **Proteção da demo pública** (o custo é meu):
  - Limite por IP (ex.: 10 perguntas/hora) e tamanho máximo de pergunta.
  - **Teto de gasto diário:** quando o custo do dia passa do limite, a API passa a responder "demo pausada até amanhã" em vez de chamar o modelo. Alerta quando atinge 80%.
  - Captcha leve (Cloudflare Turnstile) **[decidir]**.

## 8. Stack e infraestrutura

| Camada | Escolha | Motivo |
|---|---|---|
| Linguagem | Python 3.13 + `uv` | SDK oficial da Anthropic, bom ecossistema de PDF e embeddings |
| API | FastAPI | Streaming SSE simples, tipagem com Pydantic |
| Banco | Postgres 18 + `pgvector` | Metadados, texto e vetores num lugar só: um serviço a menos para operar. Busca híbrida com `tsvector` (português) + vetor |
| Job de ingestão | Mesmo código, outro entrypoint, agendado por cron do host ou timer do systemd | Sem orquestrador para um job diário |
| Containers | Docker + Docker Compose | Mesmo compose em dev e em produção |
| Proxy/TLS | Caddy | HTTPS automático |
| Nuvem (decidido) | Uma VM pequena na **Hetzner** | Kubernetes aqui seria cenário, não necessidade. Uma VM bem operada e reproduzível impressiona mais |
| IaC | Terraform (VM, rede, firewall, DNS, bucket de backup) | Subir o ambiente do zero com um comando |
| CI/CD | GitHub Actions | Lint, testes, avaliação, build da imagem, deploy |
| Registro de imagens | GitHub Container Registry | Grátis para repositório público |
| Observabilidade (decidido) | OpenTelemetry → **Grafana Cloud** (plano gratuito) | Menos trabalho de operação e não pesa na VM |

### 8.1 CI/CD

1. **PR:** lint (`ruff`), tipos (`mypy` ou `pyright`), testes unitários, testes de integração com Postgres em container, `terraform fmt` + `validate` + `plan` comentado no PR, avaliação (subconjunto) quando aplicável.
2. **Merge na `main`:** build da imagem com tag do commit, push no registro, deploy na VM (pull da imagem + `docker compose up -d`), checagem de `/saude`. Se falhar, volta para a imagem anterior automaticamente.
3. **Segredos:** chaves no GitHub Secrets / arquivo `.env` na VM com permissão restrita. Nada no repositório. Autenticação do GitHub Actions na nuvem via **OIDC** quando o provedor suportar, sem chave de longa duração.

### 8.2 Operação

- **Backup diário** do Postgres para o bucket, com retenção de 7 dias. **Testar a restauração** (etapa 6) e documentar no README.
- **Migrações** versionadas (Alembic).
- **Runbook** curto em `docs/runbook.md`: o que fazer quando o job falha, quando o custo dispara, quando a API cai, como restaurar backup.

## 9. Observabilidade

Instrumentar com **OpenTelemetry**, seguindo as convenções semânticas de GenAI para os spans de chamada ao modelo.

- **Trace por pergunta:** requisição HTTP → cada chamada ao modelo → cada chamada de ferramenta (com duração e nº de linhas/trechos retornados) → resposta.
- **Atributos por chamada ao modelo:** modelo, effort, tokens de entrada/saída/cache, custo calculado, `stop_reason`.
- **Métricas:** perguntas/min, latência p50/p95/p99, custo por pergunta e por dia, taxa de cache hit, taxa de erro, taxa de recusa, nº médio de chamadas de ferramenta.
- **Painel** com essas métricas + as do job de ingestão + resultado da última avaliação noturna.
- **Alertas:** erro > 5% em 10 min, p95 > 20 s, custo diário > 80% do teto, job de ingestão atrasado.
- **Privacidade nos logs:** a pergunta do usuário pode ser registrada (é útil para melhorar as avaliações), mas sem IP completo. Retenção de 30 dias.

## 10. Segurança

- **Injeção de prompt via documentos:** o texto dos PDFs é dado público escrito por terceiros. Vai ao modelo delimitado e marcado como conteúdo de documento; o prompt de sistema diz que instruções dentro dele não valem. As ferramentas são só de leitura, então o pior caso é uma resposta errada, não uma ação. Coberto pela categoria "injeção" das avaliações.
- **Banco:** a API conecta com um usuário **somente leitura**. Só o job de ingestão escreve. Consultas sempre parametrizadas.
- **VM:** só portas 80/443 abertas; SSH por chave, restrito por IP ou via túnel. Atualizações automáticas de segurança.
- **Dados pessoais:** os dados do PNCP são públicos, mas documentos podem conter nomes e CPFs de pessoas físicas (ex.: responsáveis, sócios). A resposta não deve listar CPFs; mascarar CPF no texto indexado **[decidir]**.
- **Dependências:** Dependabot + varredura da imagem (ex.: Trivy) no CI.

## 11. Fora do escopo da v1

- Outros estados (é configuração, mas aumenta custo e volume)
- OCR de PDFs escaneados
- Alertas por e-mail de novas licitações para um perfil de fornecedor
- Servidor MCP expondo as mesmas ferramentas (ótimo próximo passo: reaproveita tudo da seção 4.2)
- Kubernetes, multi-região, alta disponibilidade

## 12. Etapas sugeridas

1. **Dados:** projeto Python com `uv`, Postgres + pgvector em Docker Compose, coletor do PNCP para metadados com upsert idempotente e backfill. Testes do coletor com respostas gravadas da API.
2. **Documentos e busca:** download e extração de PDFs, chunking, embeddings, busca híbrida. Script que mostra os 5 melhores trechos para uma consulta, para avaliar a recuperação a olho.
3. **Assistente:** ferramentas da seção 4.2, loop com o modelo, regras de resposta, API com streaming e página web mínima. Medir custo e latência de uma pergunta típica.
4. **Avaliações:** snapshot congelado, conjunto de perguntas, corretores, juiz calibrado, relatório. Primeira linha de base registrada no README.
5. **CI:** lint, testes, avaliação no PR com portão e comentário automático, avaliação noturna.
6. **Infra e deploy:** Terraform, VM, Caddy, deploy automático com rollback, backup com restauração testada, proteção da demo (limites e teto de gasto).
7. **Observabilidade:** OpenTelemetry, painel, alertas, runbook.
8. **Vitrine:** README com diagrama de arquitetura, números (acerto, custo por pergunta, p95, concordância do juiz), tabela de configurações testadas e um vídeo/GIF curto da demo.

## 13. Glossário

- **PNCP:** Portal Nacional de Contratações Públicas, onde órgãos publicam contratações pela Lei 14.133/2021.
- **Contratação:** um processo de compra (pregão, concorrência, dispensa etc.) com seus documentos.
- **Edital / Termo de referência:** documentos que descrevem o objeto e as regras da contratação.
- **RAG:** geração aumentada por recuperação; o modelo responde com base em trechos buscados.
- **Portão de avaliação:** checagem automática no CI que impede o merge se a qualidade cair.
- **Juiz LLM:** modelo que corrige respostas abertas seguindo uma rubrica.
