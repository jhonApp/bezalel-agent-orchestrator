# Streaming em tempo real dos agentes Codex — design

## Contexto

Hoje `CodexCLI.execute()` (`src/adapters/codex_cli.py`) invoca `process.communicate(prompt)`,
que só devolve dado quando o subprocesso `codex exec --json` termina. O dashboard e o
endpoint SSE (`GET /events`, `src/api/app.py`) já existem e já recebem `agent.started` /
`agent.finished` (via `LiveEventBroker`), mas nada é publicado *durante* a execução —
uma tarefa que roda até `agent_timeout_seconds` (1800s) fica opaca para quem observa.

`codex exec --json` emite eventos JSONL no stdout conforme processa (esse é o propósito
confirmado da flag `--json`, já usada pelo adapter). O gargalo é só a forma de leitura.

## Decisões (confirmadas com o usuário)

- Destino do tempo real: **dashboard web**, via o `/events` SSE já existente — sem mudar
  onde os eventos são consumidos, só o que é publicado.
- Granularidade: **stream bruto** do Codex — cada linha JSONL repassada quase crua, sem
  curadoria/mapeamento de tipos de evento (menos lógica frágil a mudanças de versão do CLI).

## Arquitetura

```
codex exec --json (stdout, linha a linha)
        │
        ▼
  pump assíncrono (stdout) ──┐
  pump assíncrono (stderr) ──┤── on_event(event) por linha, imediato
        │                    │
        ▼                    ▼
  buffer acumulado      runtime.publish(...)
  (para parsing final)        │
        │                     ▼
        │              LiveEventBroker.publish
        │                     │
        ▼                     ▼
  AgentResult final     GET /events (SSE) → browser
```

O parsing final do `AgentResult` (schema, `_parse_response`) continua idêntico — ele lê o
buffer acumulado ao fim, exatamente como hoje. A única adição é um *side channel* que
publica cada linha assim que ela chega.

## Componentes

### `adapters/codex_cli.py` — `CodexCLI.execute`

- Novo parâmetro opcional `on_event: Callable[[dict], Awaitable[None]] | None`.
- Troca `process.communicate(...)` por dois pumps concorrentes
  (`asyncio.create_task`) que fazem `readline()` em loop sobre `process.stdout` e
  `process.stderr` até EOF, cada um:
  1. decodifica a linha (`utf-8`, `errors="replace"`);
  2. tenta `json.loads` — sucesso vira `parsed`; falha mantém só o texto bruto;
  3. aplica `_redact()` no texto antes de repassar;
  4. acrescenta a linha ao buffer acumulado (substitui o que `communicate()` dava de graça);
  5. chama `await on_event({"stream": "stdout"|"stderr", "parsed": ..., "raw": ...})`
     se `on_event` foi passado — nunca deixa uma exceção do callback derrubar o pump
     (`try/except Exception` ao redor da chamada, loga e segue).
- Cancelamento/timeout: o loop de espera atual (`while not communicate.done(): ...`)
  vira uma espera pelos dois pumps + `process.wait()` com o mesmo `deadline`/`cancel_event`
  — mesma semântica, trocando só o quê se está esperando.
- `AGENT_SCHEMA`, `_exec_command`, `_parse_response`, `_redact` não mudam de contrato.

### `orchestrator/nodes.py` — `ExecutionRuntime.run_task`

- Monta um `on_event` fechando sobre `state["execution_id"]`, `task["task_id"]`,
  `task["agent"]` e passa pro `codex.execute(...)`:

  ```python
  async def on_event(event: dict) -> None:
      await runtime.publish({
          "type": "agent.stream",
          "execution_id": state["execution_id"],
          "agent": role,
          "task_id": task["task_id"],
          **event,
      })
  ```

- Nenhuma mudança em `dispatch_agents`, `run_contract_validation`, `code_review` — todos
  já chamam `runtime.run_task`, então ganham o streaming de graça.

### `api/app.py`

- Nenhuma mudança estrutural: `event_sink=broker.publish` já injetado no `ExecutionRuntime`
  dentro do `lifespan`; `/events` já faz fan-out via `LiveEventBroker`.
- Novo: rota de debug `GET /events/view` — página HTML mínima com `EventSource("/events")`
  que lista os eventos recebidos em uma lista rolável, sem dependência do dashboard externo
  (`projeto-agents-plataform`, fora deste repositório). Existe só para permitir validar o
  streaming ponta a ponta sem precisar tocar em outro projeto.

## Erros e limites

- Linha não-JSON (texto solto do Codex) → repassada como `raw`, `parsed=None`; não quebra
  nada a jusante.
- Client SSE lento → comportamento já existente do `LiveEventBroker` (fila de 100,
  `QueueFull` descarta silenciosamente); não alterado.
- Exceção dentro de `on_event` (ex.: subscriber quebrado) → capturada e logada dentro do
  pump; não derruba a execução do agente.
- Redação de segredo roda por linha, com o mesmo `_redact()` já usado no resultado final —
  nenhum padrão novo de segredo introduzido.

## Testes

- `tests/test_codex_cli.py`: mock de subprocess que escreve linhas incrementalmente
  (com pequenos `await asyncio.sleep(0)` entre elas) para provar que `on_event` é chamado
  *durante* a execução — não só depois que o processo termina — e que o `AgentResult`
  final continua sendo montado corretamente a partir do buffer acumulado.
- Teste de cancelamento/timeout ainda mata o processo e retorna o `AgentResult` esperado,
  agora com os pumps sendo encerrados corretamente (sem task pendente/leak).
- Teste novo cobrindo linha não-JSON no meio da stream (não deve quebrar o parsing final).

## Fora de escopo

- Não mexe no dashboard externo (`projeto-agents-plataform`) — fora deste repositório.
- Não adiciona curadoria/mapeamento de tipos de evento do Codex (decisão: stream bruto).
- Não muda a CLI `python -m orchestrator run` — ela não tem `event_sink` hoje e o usuário
  optou por dashboard web, não terminal.
