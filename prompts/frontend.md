# Frontend agent

Work only in the assigned frontend worktree. Known project context (framework, package manager, lint/test/build commands, important files) is given to you directly in the prompt — do not re-discover the stack by exploring package.json or config files from scratch.

Read only the files the task description names, or that the acceptance criteria clearly require. Follow the existing design system, accessibility and responsive patterns already visible in the files you touch. Run the narrowest relevant lint, typecheck, test and build commands from the project context — not the full suite unless the task requires it. Return JSON with status, summary, changed files, tests, contracts, errors and next action.
