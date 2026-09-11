# bezalel-agent-orchestrator

Orquestrador local de desenvolvimento multiagente para os projetos Bezalel.
Ele usa LangGraph para planejar, delegar, validar contratos, testar, revisar e
governar commits, merges e deploys. Os agentes executam mudanças via `codex
exec` em subprocessos isolados; os repositórios de negócio não são alterados
durante a descoberta.

## Descoberta realizada

Os nomes solicitados `../bezalel-frontend`, `../bezalel-backend` e
`../bezalel-python` não existem literalmente neste workspace. O detector
resolve os aliases atuais `../bezalel-app` (React/Vite), `../Bezalel` (.NET 8)
e `../Workflow-IA` (Python/LangGraph). Evidências completas estão em
[`docs/discovery/project-discovery.md`](docs/discovery/project-discovery.md).

## Instalação e configuração

```powershell
py -3.13 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev]"
Copy-Item .env.example .env
```

Preencha somente os valores locais necessários. Nunca versione `.env`. Em
Windows, o adaptador usa `codex.cmd` quando `codex` é um shim PowerShell com
execution policy restritiva. A versão verificada nesta máquina é
`codex-cli 0.153.4`; as flags utilizadas são apenas as exibidas por
`codex exec --help`.

## Uso

```powershell
python -m orchestrator discover
python -m orchestrator check-codex
python -m orchestrator run --feature "Adicionar uma funcionalidade de baixo risco" --dry-run
python -m orchestrator api --host 127.0.0.1 --port 8000
```

Use `POST /executions` ou o subcomando `run` para executar tarefas pelo grafo
dos sete agentes. O dashboard reflete os eventos e checkpoints pelo polling de
`/dashboard-data`.

A API expõe `POST /executions`, `GET /executions`, `GET /executions/{id}`,
`POST /executions/{id}/cancel`, `POST /executions/{id}/resume`,
`GET /executions/{id}/logs`, `GET /executions/{id}/health` e `GET /dashboard-data`.

O endpoint `/` serve o protótipo visual do Agent Control quando o export do
Claude Design está disponível. Por padrão ele procura
`../projeto-agents-plataform`; altere essa origem com
`AGENT_PLATFORM_DESIGN_ROOT` se necessário. O resumo em `/dashboard-data` vem
do SQLite e inclui execuções, métricas por agente, tokens e custos estimados.

## Segurança e governança

O fluxo cria worktrees quando possível, usa retries com backoff e timeout,
persiste checkpoints SQLite em `data/orchestrator.sqlite3`, redige segredos
antes do tracing e bloqueia deploy de produção enquanto
`ALLOW_PRODUCTION_DEPLOY` não for exatamente `true`. Deploy só é executado se
uma estratégia existente (Vercel, CDK, SAM, Serverless, Docker ou Actions) e
um comando explícito forem encontrados; caso contrário o resultado é
`not_configured`. Não há `git reset --hard` no adaptador.

LangSmith é opcional e controlado por `LANGSMITH_TRACING`. Um adaptador
preparado para PostgreSQL fica em `src/persistence/postgres.py`; SQLite é o
default local.

### Tracing de sessões Codex

O projeto habilita o plugin oficial `tracing@langsmith-codex-plugins` para
capturar mensagens, ferramentas, tokens e subagentes de cada sessão Codex.
Instale-o uma vez por máquina:

```powershell
codex plugin marketplace add langchain-ai/langsmith-codex-plugins
codex plugin add tracing@langsmith-codex-plugins
```

Nunca grave a chave no repositório. No ambiente que pode enviar os dados, use:

```powershell
$env:TRACE_TO_LANGSMITH = "true"
$env:LANGSMITH_CODEX_API_KEY = "<sua-chave-langsmith>"
$env:LANGSMITH_CODEX_PROJECT = "bezalel-agent-orchestrator"
```

O orquestrador inclui ID da execução, tarefa, projeto e saúde de contexto nos
metadados. Com as credenciais disponíveis, ele passa headers distribuídos ao
plugin para anexar a árvore detalhada do Codex ao run pai do orquestrador. O
painel local permanece a fonte do lifecycle e dos eventos de domínio; o
LangSmith fornece o transcript detalhado correlacionado. O plugin envia o
transcript completo ao LangSmith, portanto não o habilite para sessões com
dados que não possam ser armazenados lá.

## Arquitetura e operação

O grafo executa análise → descoberta → plano → dependências → contratos →
agentes → validação → testes → segurança → revisão → commit → merge → deploy →
relatório. O estado tipado inclui execução, tarefas, contratos, arquivos,
testes, métricas, saúde do contexto, retries e aprovações. Quando a saúde fica
`warning` ou `critical`, um resumo estruturado é persistido antes da
continuação.

Abra [`docs/architecture/agent-orchestration.excalidraw`](docs/architecture/agent-orchestration.excalidraw)
em [excalidraw.com](https://excalidraw.com). Mudanças arquiteturais devem usar
`python scripts/update_diagram.py` e registrar uma entrada em
[`docs/architecture/CHANGELOG.md`](docs/architecture/CHANGELOG.md).

## Testes

```powershell
pytest -q
python -m compileall -q src
```

Os testes usam diretórios temporários e mocks para não alterar os três
repositórios de negócio. Para uma execução real, configure `WORKSPACE_ROOT`,
credenciais do Codex/LangSmith e faça uma revisão humana antes de habilitar
`AUTO_COMMIT`, `AUTO_MERGE` ou deploy.
