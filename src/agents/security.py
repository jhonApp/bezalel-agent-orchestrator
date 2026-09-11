from __future__ import annotations

import re
from pathlib import Path

from schemas.models import ProjectDetection, SecurityFinding


SECRET_PATTERNS = [
    (re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"), "private key material"),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "AWS access key identifier"),
    (re.compile(r"(?i)\b(?:api[_-]?key|secret|password|token)\s*[:=]\s*['\"][^'\"]{12,}['\"]"), "hard-coded credential-like value"),
]
SKIP = {".git", ".claude", ".vscode", "node_modules", "venv", ".venv", "__pycache__", "bin", "obj", ".pytest_cache", "dist", "cdk.out", "coverage"}
TEXT_SUFFIXES = {".py", ".cs", ".csproj", ".json", ".toml", ".yaml", ".yml", ".xml", ".js", ".jsx", ".ts", ".tsx", ".env", ".ini", ".cfg", ".md", ".txt", ".ps1", ".sh"}


def scan_project(project: ProjectDetection, relative_paths: list[str] | None = None) -> list[SecurityFinding]:
    root = Path(project.path)
    if not root.exists():
        return [SecurityFinding(severity="blocking", path=str(root), message="project path does not exist")]
    findings: list[SecurityFinding] = []
    tracked_env = relative_paths is None and (root / ".env").exists() and not (root / ".env.example").exists()
    if tracked_env:
        findings.append(SecurityFinding(severity="warning", path=".env", message=".env exists; verify it is ignored and contains no committed secrets"))
    scanned = 0
    candidates = root.rglob("*") if relative_paths is None else (root / item for item in relative_paths)
    for path in candidates:
        if not path.is_file() or any(part in SKIP for part in path.parts):
            continue
        try:
            path.resolve().relative_to(root.resolve())
        except ValueError:
            continue
        if path.suffix.lower() not in TEXT_SUFFIXES:
            continue
        if path.stat().st_size > 1_000_000:
            continue
        scanned += 1
        if scanned > 10000:
            findings.append(SecurityFinding(severity="warning", path=str(root), message="security scan capped at 10000 text files"))
            break
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for pattern, label in SECRET_PATTERNS:
            if pattern.search(text):
                findings.append(SecurityFinding(severity="blocking", path=str(path.relative_to(root)), message=label,
                                                evidence="value redacted; rotate and remove from history"))
    return findings
