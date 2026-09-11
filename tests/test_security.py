from pathlib import Path

from agents.security import scan_project


def test_security_finds_private_key(tmp_path: Path):
    secret = tmp_path / "config.txt"
    secret.write_text("-----BEGIN PRIVATE KEY-----\nsecret\n-----END PRIVATE KEY-----", encoding="utf-8")
    findings = scan_project(type("Project", (), {"path": str(tmp_path), "project_id": "x"})())
    assert any(item.severity == "blocking" for item in findings)


def test_security_can_scan_only_changed_paths(tmp_path: Path):
    (tmp_path / "legacy.txt").write_text(
        "-----BEGIN PRIVATE KEY-----\nsecret\n-----END PRIVATE KEY-----", encoding="utf-8"
    )
    (tmp_path / "changed.ts").write_text("export const healthy = true", encoding="utf-8")

    findings = scan_project(
        type("Project", (), {"path": str(tmp_path), "project_id": "x"})(),
        ["changed.ts"],
    )

    assert findings == []
