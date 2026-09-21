# Avaliação de qualidade por agente — design

## Contexto

O painel hoje mostra status (completed/failed/blocked/rate_limited), custo estimado e
findings bloqueantes de segurança/contrato por execução, mas não avalia a *qualidade* do
que cada agente entregou — só se passou ou não nos gates. O pedido é uma nota por 7 eixos
(correção, relevância, uso de fonte real, alucinação, formato válido, cumprimento de
regras, segurança), mais uma comparação de qualidade/custo/latência entre versões do
prompt de um agente ao longo do tempo.

## Decisões (confirmadas com o usuário)

- Alvo: os 7 agentes Codex deste orchestrator (código), não o pipeline de conteúdo do
  Workflow-IA. Os eixos foram reinterpretados para diff de código (ver "Eixos" abaixo).
- Eixo "tom adequado" removido — sem equivalente em diff de código. Dos 8 eixos do
  pedido original, ficam 7 (ver tabela abaixo).
- Cálculo híbrido: eixos que já têm dado real (`test_results`, `security_findings`,
  sucesso de parse) são heurística pura, zero custo extra. Só os eixos que exigem
  julgamento semântico entram no prompt do `reviewer` já existente — nenhuma chamada
  Codex nova.
- Granularidade: por **(execução, projeto)**, não por task. Cada domínio (frontend/
  backend/python) tem dono exclusivo (`CLAUDE.md`), então projeto ⇔ 1 agente produtor por
  execução — a nota do reviewer sobre o diff de um projeto já cobre exatamente um agente.
- Versionamento de prompt: manual, um campo `prompt_version` por role em
  `agents/registry.py`, bumpado à mão quando o texto do prompt muda.
- Local no painel: nova aba "Qualidade" dedicada (não misturada em Agentes/Execuções).
- Comparação de versão: sempre contra a versão string imediatamente anterior do mesmo
  agente, nunca contra um baseline escolhido à mão.

## Eixos e origem do dado

| Eixo | Origem | Como |
|---|---|---|
| Correta | heurística | `test_results` do projeto passaram e a task não ficou `failed`/`blocked` |
| Relevante | LLM (reviewer) | diff ataca a task pedida, sem mudança fora do escopo |
| Fonte utilizada | LLM (reviewer) | mudanças se apoiam em código/contrato real do repo |
| Alucinação | LLM (reviewer) | citou arquivo/função/API inexistente, ou summary não bate com o diff real (menor é melhor) |
| Formato válido | heurística | JSON do agente parseou (`_parse_response` não caiu em fallback) e o diff aplicou limpo |
| Cumprimento das regras | LLM (reviewer) | seguiu `CLAUDE.md` (dono do domínio, resolver conventions, contract rules) |
| Segurança | heurística | ausência de `security_findings` com `severity="blocking"` naquele projeto |

`quality_score` (nota única, 0–100) = média de {correta, relevante, fonte_utilizada,
formato_valido, cumprimento_regras, seguranca} e `(100 - alucinacao)`. Eixo ausente
(reviewer não devolveu `quality`) entra como `null` e é excluído da média, não vira 0.

## Arquitetura

```
dispatch_agents (frontend/backend/python_ai)
        │  carimba task["result"]["prompt_version"] = AGENT_ROLES[agent]["prompt_version"]
        ▼
code_review → _run_gate_agent_across_projects(..., on_project_result=callback)
        │  reviewer roda 1x por projeto alterado (já existe), devolve AgentResult
        │  com result.quality = {relevante, fonte_utilizada, alucinacao, cumprimento_regras}
        ▼
  callback (novo): funde heurística + LLM → 1 entrada em state["quality_scores"]
        │
        ▼
  ExecutionStateModel.quality_scores (persistido como o resto do state)
        │
        ▼
  GET /dashboard-data (item.quality_scores)   GET /quality-data (agregado por versão)
        │                                              │
        ▼                                              ▼
              painel — aba "Qualidade"
```

## Componentes

### `agents/registry.py`

- `AGENT_ROLES[role]` ganha `"prompt_version": str` (ex.: `"1.0"`). Bump manual quando o
  `.md` do role muda.

### `adapters/codex_cli.py` — `AGENT_SCHEMA`

- Novo campo opcional (fora de `required`) `quality`: objeto com `relevante`,
  `fonte_utilizada`, `alucinacao`, `cumprimento_regras` (inteiros 0–100). Só o
  `reviewer.md` instrui o modelo a preenchê-lo; outros roles simplesmente não o incluem
  no JSON — `additionalProperties: False` já tolera campo opcional ausente.
- `_parse_response` já faz `json.loads` do texto inteiro; só precisa repassar `quality`
  para `AgentResult` (novo campo `quality: dict[str, int] | None = None`).

### `prompts/reviewer.md`

- Adiciona instrução: além dos findings bloqueantes, preencher `quality` com os 4 eixos
  acima (0–100, `alucinacao` invertido — 100 = certeza de alucinação).

### `orchestrator/nodes.py`

- `dispatch_agents`: ao montar `task["result"] = result.model_dump(...)`, acrescenta
  `task["result"]["prompt_version"] = AGENT_ROLES[task["agent"]].get("prompt_version")`.
- `_run_gate_agent_across_projects(runtime, state, task, changed_projects,
  on_project_result=None)`: novo parâmetro opcional, chamado como
  `await on_project_result(project_id, agent_result)` a cada iteração do loop, **antes**
  do merge final (que hoje descarta granularidade por projeto). `run_contract_validation`
  não passa o callback — comportamento inalterado.
- `code_review`: passa um callback que:
  1. lê `test_results`/`security_findings` do projeto no `state` (heurística);
  2. lê `agent_result.quality` (LLM, pode ser `None`);
  3. resolve `prompt_version` olhando a task do `state["plan"]` cujo `project_id` bate e
     `agent` é o dono do domínio (frontend/backend/python_ai);
  4. monta a entrada e dá `append` em `state.setdefault("quality_scores", [])`.

### `schemas/models.py` / `orchestrator/state.py`

- `AgentResult.quality: dict[str, int] | None = None` (novo campo opcional).
- `ExecutionStateModel.quality_scores: list[dict[str, Any]] = Field(default_factory=list)`
  e o mesmo campo em `ExecutionState` (TypedDict) — mesmo padrão já usado para
  `pull_requests`.

### `api/app.py`

- `/dashboard-data`: cada item de `items[]` ganha `"quality_scores": state.get(
  "quality_scores", [])` — mesmo padrão de `commits`/`pull_requests`.
- Novo `GET /quality-data`:
  - `by_execution`: achata `quality_scores` de todas as execuções (via
    `langgraph_states()`, mesma fonte já usada por `dashboard_data`), mais recente
    primeiro.
  - `by_version`: agrupa por `(agent, prompt_version)` → `{avg_quality, avg_cost_usd,
    avg_duration_seconds, runs}`; ordena versões do mesmo agente por string; calcula
    delta de qualidade/custo/latência contra a versão anterior na lista ordenada
    (primeira versão de um agente não tem delta).

### Painel (`web/Plataforma de Administração.dc.html`)

- Nova aba "Qualidade" na nav, entre Custos e Saúde.
- Bloco 1: card por `(execução, projeto)` com os eixos em %, eixo ausente mostra "—".
- Bloco 2: tabela por agente, 1 linha por `prompt_version`, colunas qualidade/custo/
  latência com deltas (+/-%) — dado de `GET /quality-data`.

## Erros e limites

- Reviewer não devolve `quality` → eixos LLM ficam `null` no dashboard ("—"), heurística
  continua populada normalmente. Não bloqueia nada — nota mede qualidade, não é gate.
- `code_review` pedindo mudanças (`changes_requested`) não impede calcular a nota.
- Roda igual em `dry_run`/`analysis_only` — mesma chamada do reviewer já existente, sem
  mudança de fluxo.
- Projeto sem task de domínio correspondente (não deveria acontecer dado exclusividade de
  `CLAUDE.md`, mas por robustez) → `prompt_version` fica `null`, entrada ainda é gravada.

## Testes

- Função pura de heurística (test_results + security_findings + parse-ok → 3 eixos):
  casos passou/falhou/sem teste configurado, com/sem finding bloqueante.
- Função pura de blend do `quality_score` (média ignorando eixos `null`).
- `dispatch_agents` carimba `prompt_version` correto por role.
- `_run_gate_agent_across_projects` chama o callback por projeto, na ordem certa, com o
  `AgentResult` daquele projeto especificamente (não o merge final) — `run_contract_validation`
  seguindo sem callback não muda de comportamento (regressão).
- `code_review` monta `state["quality_scores"]` com 1 entrada por projeto revisado.
- `/dashboard-data` expõe `quality_scores` por execução (mesmo padrão do teste já
  existente para `commits`/`pull_requests`).
- `/quality-data`: agregação por versão, delta calculado contra a versão string anterior,
  primeira versão de um agente sem delta.
- `AGENT_SCHEMA` aceita `quality` ausente sem quebrar o parse de nenhum outro role.

## Fora de escopo

- Não versiona conteúdo do Workflow-IA (fora deste repositório/domínio).
- Não adiciona chamada Codex dedicada de "juiz" — decisão explícita foi reaproveitar o
  reviewer existente.
- Não escolhe baseline manual de comparação — sempre versão anterior por ordenação de
  string.
- Não migra dado histórico: execuções já persistidas sem `quality_scores` simplesmente
  não aparecem na aba até rodarem de novo com o código novo.
