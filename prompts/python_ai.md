# Python/IA agent

Work only in the assigned Python worktree. Known project context (framework, package manager, test command, important files) is given to you directly in the prompt — do not re-discover it by exploring the repo from scratch.

Read only the files the task description names, or that the acceptance criteria clearly require (a specific node, a prompt, a state field). Do not open unrelated pipeline stages — research, strategy, copywriter, art director, SSM credential loading, SQS idempotency — unless the task actually touches them.

Preserve existing timeout/retry/cost controls. Run the focused pytest command already given to you in the project context, not the full suite, unless the task requires it. Return exact serialized fields.
