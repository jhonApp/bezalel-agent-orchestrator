"""Validate the architecture diagram and update its version/date without losing manual elements."""
from __future__ import annotations

import argparse
import json
from datetime import date
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DIAGRAM = ROOT / "docs" / "architecture" / "agent-orchestration.excalidraw"
CHANGELOG = ROOT / "docs" / "architecture" / "CHANGELOG.md"


def validate(document: dict) -> None:
    if document.get("type") != "excalidraw" or document.get("version") != 2:
        raise ValueError("not a version 2 Excalidraw document")
    if not isinstance(document.get("elements"), list) or not document["elements"]:
        raise ValueError("diagram has no elements")
    ids = [element.get("id") for element in document["elements"]]
    if any(not value for value in ids) or len(ids) != len(set(ids)):
        raise ValueError("diagram element ids must be unique")


def update(note: str | None = None) -> dict:
    document = json.loads(DIAGRAM.read_text(encoding="utf-8"))
    validate(document)
    version_text = next((e for e in document["elements"] if e.get("id") == "legend"), None)
    today = date.today().isoformat()
    if version_text:
        original = version_text.get("text", "")
        import re
        version_text["text"] = re.sub(r"Atualizado: .*", f"Atualizado: {today}", original)
        version_text["originalText"] = version_text["text"]
    document.setdefault("appState", {})["lastEditVersion"] = today
    DIAGRAM.write_text(json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if note:
        with CHANGELOG.open("a", encoding="utf-8") as stream:
            stream.write(f"\n## {today} — update\n\n- {note}\n")
    return {"version": "1.0.0", "updated": today, "elements": len(document["elements"]), "valid": True}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--note")
    parser.add_argument("--check", action="store_true", help="validate without changing the diagram")
    args = parser.parse_args()
    if args.check:
        document = json.loads(DIAGRAM.read_text(encoding="utf-8"))
        validate(document)
        print(json.dumps({"valid": True, "elements": len(document["elements"])}, ensure_ascii=False))
    else:
        print(json.dumps(update(args.note), ensure_ascii=False))
