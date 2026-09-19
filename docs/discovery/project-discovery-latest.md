# Bezalel project discovery

Generated: 2026-09-19T13:45:07.041297

Expected aliases `bezalel-frontend`, `bezalel-backend`, and `bezalel-python` were resolved through configured paths and existing candidates.

## frontend
- Expected name: `bezalel-frontend`
- Path: `D:\Project\Bezalel-Package\bezalel-app`
- Exists: `True`
- Language: `TypeScript`
- Framework: React, Vite, TypeScript, Tailwind, Vitest
- Package/dependency manager: `npm`
- Commands: `{'dev': 'npm run vite', 'build': 'npm run vite build', 'lint': 'npm run eslint .', 'test': 'npm run vitest run'}`
- Tests: Vitest
- Deploy: Vercel (vercel.json)
- Integrations: none detected
- Important files: package.json, vite.config.ts, tailwind.config.ts, vercel.json
- Risks: multiple package manager lockfiles detected: package-lock.json, bun.lockb

## backend
- Expected name: `bezalel-backend`
- Path: `D:\Project\Bezalel-Package\Bezalel`
- Exists: `True`
- Language: `C#`
- Framework: .NET net8.0
- Package/dependency manager: `unknown`
- Commands: `{'restore': 'dotnet restore', 'build': 'dotnet build', 'test': 'dotnet test'}`
- Tests: dotnet test; 10 test project(s) detected
- Deploy: AWS CDK, GitHub Actions
- Integrations: AWS DynamoDB, AWS SQS, AWS S3, AWS Lambda, AWS CDK
- Important files: Bezalel.sln, Bezalel.API/Bezalel.API.sln, .claude/worktrees/agent-a4187eb4c94628e25/Bezalel.sln, Bezalel.API/Bezalel.API.csproj, Bezalel.Aplication/Bezalel.Aplication.csproj, Bezalel.Core/Bezalel.Core.csproj, Bezalel.Infrastructure/Bezalel.Infrastructure.csproj, Bezalel.Infrastructure.Iac/Bezalel.Infrastructure.Iac.csproj, Bezalel.Ioc/Bezalel.Ioc.csproj, Bezalel.Test/Bezalel.Test.csproj, Workers/Bezalel.Workers.HandleAnalysisWorker/Bezalel.Workers.HandleAnalysisWorker.csproj, Workers/Bezalel.Workers.InstagramPublishWorker/Bezalel.Workers.InstagramPublishWorker.csproj, Workers/Bezalel.Workers.InstagramTokenRefreshWorker/Bezalel.Workers.InstagramTokenRefreshWorker.csproj, Workers/Bezalel.Workers.SlideEditWorker/Bezalel.Workers.SlideEditWorker.csproj, Workers/Bezalel.Workers.StalledJobMonitor/Bezalel.Workers.StalledJobMonitor.csproj
- Risks: multiple solution/project entry points; select the affected project explicitly

## python
- Expected name: `bezalel-python`
- Path: `D:\Project\Bezalel-Package\Workflow-IA`
- Exists: `True`
- Language: `Python`
- Framework: LangGraph, LangChain, Gemini, OpenAI, AWS boto3, AWS Lambda
- Package/dependency manager: `pip`
- Commands: `{'test': 'python -m pytest', 'lint': 'ruff check .', 'typecheck': 'mypy .'}`
- Tests: pytest
- Deploy: GitHub Actions, Docker, deployment scripts
- Integrations: AWS boto3, AWS Lambda
- Important files: requirements.txt, requirements-dev.txt, graph.py, lambda_function.py, main.py, make_zip.py, mock_nodes.py, nodes.py, reference_analyzer_lambda.py, state.py, test_antes_depois_edits.py, test_character_reference_instruction.py, test_contact_info_instruction.py, test_contato_em_modo_venda.py, test_content_format_instruction.py
- Risks: model-provider calls can consume tokens; use mocks in validation
