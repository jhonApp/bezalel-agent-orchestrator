# Escopar agentes por relevância da feature — design

## Contexto

`create_plan` (`src/orchestrator/nodes.py`) hoje cria uma task de frontend/backend/
python_ai para todo projeto que **existe** no workspace, independente de a feature
pedida ter algo a ver com aquele domínio. Como os 3 repositórios irmãos sempre existem,
toda execução despacha os 3 agentes — inclusive uma troca trivial de texto num botão do
frontend, que ainda assim aciona sessões reais de Codex para backend e Python/LangGraph,
com uma descrição de task genérica ("implemente mudanças de backend/API...") sem nada de
fato para fazer.

Isso foi descoberto investigando uma queixa real: o agente `reviewer` parecia "nunca sair
do modo ocioso" no painel. Root cause (confirmado via inspeção direta dos processos do
SO, não suposição): `dispatch_agents` roda frontend/backend/python_ai em paralelo via
`asyncio.gather`, e só avança para os gates (contratos → testes → segurança → review)
quando os **3** terminam — mesmo que só um domínio tenha trabalho real. O reviewer não
está travado; está genuinamente esperando um agente sem trabalho útil terminar de girar
em falso.

## Decisões (confirmadas com o usuário)

- Escopo desta spec: só o **corte de agentes irrelevantes**. Uma segunda mudança —
  paralelizar o pipeline de gates por projeto, para que a revisão de um projeto comece
  assim que aquele projeto termina, sem esperar os irmãos mais lentos — é maior (reescreve
  `run_contract_validation`/`run_tests`/`security_review`/`code_review`/`commit_changes`
  para operar por projeto, mais reducers de estado para o LangGraph mesclar campos
  compartilhados entre branches paralelas, mais reestruturar `graph.py` com `Send`) e fica
  para uma spec própria depois.
- Classificação por LLM (não regex/keyword) — mais preciso para pedidos ambíguos ou que
  cruzam domínio, custo baixo (uma chamada curta, sem edição de código).
- Em dúvida, o classificador inclui o domínio (fail-open) — preferível gastar uma
  execução a mais a deixar de tocar um domínio que precisava mudar. Isso vale também para
  falha total do classificador (timeout, erro de parse, exit não-zero): cai para "todos os
  domínios relevantes", idêntico ao comportamento atual — o classificador nunca pode
  bloquear ou reduzir o pipeline por conta própria falhando.
- Override manual: `ExecutionRequest` ganha `target_projects` opcional — quando informado,
  pula o classificador inteiramente (mesmo padrão de `dry_run`/`analysis_only`).
- Mecanismo: reaproveita `CodexCLI` com um role novo e leve (`classifier`), não uma
  integração nova direta com a API da OpenAI — reusa 100% do subprocess/parsing/retry que
  já existe; só mais uma chamada Codex com prompt e schema próprios.

## Arquitetura

```
discover_projects
        │  state["detected_projects"] populado
        ▼
classify_projects (NOVO nó)
        │  target_projects informado? → usa direto (filtrado por existing)
        │  senão → ExecutionRuntime.classify_relevant_projects(feature_request, existing)
        │           via CodexCLI.execute_json(prompt, workspace_root, CLASSIFIER_SCHEMA)
        │           falhou/None? → fail-open, todos os existing são relevantes
        ▼
  state["relevant_projects"] = [...]
        │
        ▼
create_plan
        │  cria task de frontend/backend/python_ai só para
        │  project_id ∈ (existing ∩ relevant_projects)
        ▼
  (resto do grafo inalterado — contracts/qa/security/reviewer já dependem de
   base_ids, a lista real de tasks criadas, então se adaptam sozinhos a 1, 2 ou 3
   domínios sem mudança de código)
```

Não roda em worktree: a classificação é leitura pura do texto do pedido, não edita nada,
não precisa de branch isolada. `classify_relevant_projects` chama o Codex com
`cwd=settings.workspace_root`, sem passar por `ExecutionRuntime.run_task()` (que sempre
prepara worktree e está acoplado à semântica de retry/TaskSpec dos agentes de código —
não serve para uma chamada de classificação avulsa).

## Componentes

### `src/adapters/codex_cli.py`

- Novo schema module-level, **separado** de `AGENT_SCHEMA` (a forma de resposta não tem
  nada a ver com `AgentResult`):
  ```python
  CLASSIFIER_SCHEMA = {
      "type": "object",
      "additionalProperties": False,
      "properties": {
          "frontend": {"type": "boolean"},
          "backend": {"type": "boolean"},
          "python": {"type": "boolean"},
      },
      "required": ["frontend", "backend", "python"],
  }
  ```
  Sem campo de confiança — o boolean já é a decisão (dúvida = `true`, isso é instrução de
  prompt, não estrutura de dado).
- Novo método `CodexCLI.execute_json(prompt: str, workdir: Path, schema: dict, label: str,
  timeout: int = 120) -> dict[str, Any] | None`: fatora a parte do `execute()` que monta o
  comando, escreve o schema em disco, roda o subprocess e lê `--output-last-message`, mas
  devolve o dict JSON cru (via `json.loads`) em vez de validar contra `AgentResult`.
  Retorna `None` em qualquer falha (timeout, exit não-zero, JSON inválido, arquivo de saída
  ausente) — o chamador decide o fallback (aqui, fail-open).

### `prompts/classifier.md` (novo arquivo)

Descreve os 3 domínios (frontend = bezalel-app React, backend = Bezalel .NET8,
python = Workflow-IA LangGraph) e instrui: marcar `true` se o pedido plausivelmente toca
aquele domínio **ou** se houver dúvida; só marcar `false` quando for claramente
irrelevante.

### `src/agents/registry.py`

- `AGENT_ROLES["classifier"] = {"label": "Domain Classifier", "project_id": None,
  "prompt": "classifier.md", "prompt_version": "1.0"}`. Fica fora de `_DOMAIN_AGENTS`
  (`orchestrator/quality.py`) e de qualquer fluxo de nota de qualidade — não produz diff,
  não é revisado.

### `src/orchestrator/nodes.py`

- `ExecutionRuntime.classify_relevant_projects(self, feature_request: str, existing:
  set[str]) -> list[str]`:
  ```python
  async def classify_relevant_projects(self, feature_request: str, existing: set[str]) -> list[str]:
      prompt = role_prompt("classifier", self.settings.orchestrator_root / "prompts")
      prompt += f"\n\nFeature request:\n{feature_request}"
      result = await self.codex.execute_json(prompt, self.settings.workspace_root, CLASSIFIER_SCHEMA, "Classifier")
      if result is None:
          return sorted(existing)
      domains = {"frontend": "frontend", "backend": "backend", "python": "python"}
      return [project for domain, project in domains.items() if result.get(domain, True) and project in existing]
  ```
- Novo nó `classify_projects(runtime, state)`:
  ```python
  async def classify_projects(runtime: ExecutionRuntime, state: dict[str, Any]) -> dict[str, Any]:
      existing = {p["project_id"] for p in state.get("detected_projects", []) if p.get("exists")}
      override = state.get("target_projects")
      if override is not None:
          relevant = [p for p in override if p in existing]
      else:
          relevant = await runtime.classify_relevant_projects(state["feature_request"], existing)
      state["relevant_projects"] = relevant
      state["next_action"] = "create_plan"
      return await runtime.persist(state, "classify_projects", "projects.classified", {"relevant": relevant})
  ```
- `create_plan`: troca a condição de criação de task de `if project in existing:` para
  `if project in existing and project in state.get("relevant_projects", existing):` — o
  `existing` como default preserva o comportamento atual caso `relevant_projects` nunca
  tenha sido setado (defensivo, não deveria acontecer já que `classify_projects` sempre
  roda antes).

### `src/orchestrator/graph.py`

- Insere `("classify_projects", classify_projects)` na lista de nós, entre
  `discover_projects` e `create_plan` — só isso, o resto da cadeia linear de `add_edge`
  já se ajusta pela posição na lista.

### `schemas/models.py` / `orchestrator/state.py`

- `ExecutionRequest.target_projects: list[str] | None = None`.
- `ExecutionStateModel.target_projects: list[str] | None = None` e
  `ExecutionStateModel.relevant_projects: list[str] = Field(default_factory=list)`
  (mesmo par em `ExecutionState` TypedDict). `target_projects` fica fora de `approvals`
  (que é `dict[str, bool]`) porque carrega uma lista, não um booleano.
- `initial_state()`: passa `target_projects=request.target_projects` para o
  `ExecutionStateModel`.

## Erros e limites

- Classificador falha (timeout, erro de parse, exit não-zero) → fail-open total, idêntico
  ao comportamento de hoje (todos os domínios existentes são relevantes). Nunca bloqueia
  o pipeline.
- Campo de domínio ausente na resposta do classificador (schema deveria impedir, mas por
  segurança) → `result.get(domain, True)`, fail-open por campo também.
- `target_projects=[]` explícito é válido e diferente de não informar (`None`) — usuário
  pode genuinamente querer zero agentes de domínio (feature só de config/infra tratada
  pelos agentes de gate). Contratos/QA/segurança/reviewer já toleram `changed_projects=[]`
  hoje (retornam "no files changed" sem erro).
- `target_projects` citando um projeto que não existe no workspace é silenciosamente
  filtrado (`and project in existing` no override também) — nunca cria task para um
  projeto inexistente.

## Testes

- `classify_relevant_projects`: fail-open quando `execute_json` retorna `None`;
  fail-open por campo quando a resposta tem chave faltando; filtra por `existing`
  corretamente.
- `execute_json` (novo em `CodexCLI`): sucesso parseia o dict; JSON malformado retorna
  `None`; exit não-zero retorna `None` — mesmo padrão de fake-subprocess já usado em
  `tests/test_codex_cli.py`.
- `classify_projects` (nó): com `target_projects` informado (pula classificador,
  filtra por existing); sem override (usa o mock do classificador); classificador
  retornando subconjunto real.
- `create_plan`: só cria task de domínio para projeto em `existing ∩ relevant_projects`;
  regressão garantindo que contratos/qa/segurança/reviewer continuam dependendo
  corretamente de `base_ids` reduzido (1 ou 2 domínios, não sempre 3).
- `ExecutionRequest.target_projects` flui até `initial_state()` corretamente, incluindo
  o caso `[]` explícito (diferente de `None`).

## Fora de escopo

- Pipeline de gates por projeto em paralelo (revisão começar assim que UM projeto termina
  sem esperar os outros) — spec própria, por causa do tamanho (reescreve 5 funções de nó
  para operar por projeto, mais reducers de estado do LangGraph, mais `Send` no grafo).
- Não muda `dispatch_agents`, `run_contract_validation`, `run_tests`, `security_review`,
  `code_review` ou `commit_changes` — eles continuam operando sobre os projetos que
  efetivamente ganharam task, sem saber nem precisar saber que existe um filtro de
  relevância antes deles.
- Não adiciona configuração nova de timeout — `execute_json` usa um timeout fixo de 120s
  (classificação de texto, não edição de código; não precisa do timeout de 30min dos
  agentes).
