#!/usr/bin/env python3
"""Offline end-to-end check: runs the full pipeline against a throwaway vault
with the three Claude calls stubbed out. No API key needed.

    .venv/bin/python scripts/smoke_test.py
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

TMP = Path(tempfile.mkdtemp(prefix="vault-smoke-"))
os.environ["OBSIDIAN_VAULT_PATH"] = str(TMP)
os.environ["ROUTES_FILE"] = str(ROOT / "routes.example.json")
os.environ["ANTHROPIC_API_KEY"] = "not-used-in-smoke-test"

from backend.app import llm, pipeline  # noqa: E402
from backend.app.models import (  # noqa: E402
    EmailPayload,
    ExtractedTask,
    ExtractionOutput,
    FormattedTask,
    RoutingDecision,
    RoutingOutput,
    WriterOutput,
)
from backend.app.vault import VaultError, validate_lines  # noqa: E402

FAKE = {
    ExtractionOutput: ExtractionOutput(
        summary="Alex wants working calls set up and the platform owner decided.",
        tasks=[
            ExtractedTask(
                text="Set up two working calls with Alex and Jordan in the next two weeks",
                owner="me",
                owner_name="Riley",
                due="2026-08-21",
                priority="high",
                explicit=True,
                evidence="set up a couple of working calls in the next two weeks",
            ),
            ExtractedTask(
                text="Nudge the Northwind lawyers on the transition services markup",
                owner="sender",
                owner_name="Speaker 2",
                due="",
                priority="normal",
                explicit=True,
                evidence="nudge Northwind lawyers on the TSA markup",
            ),
        ],
    ),
    RoutingOutput: RoutingOutput(
        decisions=[
            RoutingDecision(task_index=0, route_id="acme", confidence=0.92,
                            reason="Alex and Jordan are Acme leadership."),
            RoutingDecision(task_index=1, route_id="northwind", confidence=0.81,
                            reason="Transition services markup is the Northwind carve-out."),
        ]
    ),
    WriterOutput: WriterOutput(
        lines=[
            FormattedTask(
                task_index=0,
                markdown='- [ ] **Riley** - Set up two working calls with Alex and '
                'Jordan in the next two weeks #email (due: 2026-08-21) '
                '_(from: Alex Reed, "Org design", 2026-08-07)_',
            ),
            FormattedTask(
                task_index=1,
                markdown='- [ ] **Speaker 2** - Nudge the Northwind lawyers on the '
                'transition services markup #email '
                '_(from: Alex Reed, "Org design", 2026-08-07)_',
            ),
        ]
    ),
}


def fake_structured(*, output_format, **_kwargs):
    # The router narrows RoutingOutput to a subclass with route_id as an enum;
    # match on the base class so the stub still applies.
    for base, value in FAKE.items():
        if issubclass(output_format, base):
            return output_format.model_validate(value.model_dump())
    raise KeyError(output_format)


def main() -> int:
    llm.structured = fake_structured
    pipeline.extract_tasks.__globals__["structured"] = fake_structured
    pipeline.route_tasks.__globals__["structured"] = fake_structured
    pipeline.format_tasks.__globals__["structured"] = fake_structured

    email = EmailPayload(
        subject="Org design",
        sender="Alex Reed <alex.reed@acme.example>",
        body="Let's set up a couple of working calls in the next two weeks. "
        "Also someone should nudge the Northwind lawyers on the TSA markup.",
        received_at="2026-08-07T09:00:00Z",
    )

    result = pipeline.process_email(email)

    print(f"vault: {TMP}")
    print(f"summary: {result.summary}")
    for task in result.tasks:
        print(f"  [{task.route_id}] {task.file} > ## {task.section}")
        print(f"      {task.markdown}")
    assert result.warnings == [], result.warnings
    assert len(result.tasks) == 2

    print("\n--- files ---")
    for path in sorted(TMP.rglob("*.md")):
        print(f"\n### {path.relative_to(TMP)}")
        print(path.read_text(encoding="utf-8"))

    # Second run must append into the existing sections, not duplicate headings.
    pipeline.process_email(email)
    todo = (TMP / "00 Inbox" / "TODO.md").read_text(encoding="utf-8")
    assert todo.count("## Acme") == 1, "section heading duplicated on second run"
    assert todo.count("Set up two working calls") == 2, "second append missing"

    # The write gate must reject anything that is not a plain checklist line.
    for bad in [
        "Not a checklist",
        "- [ ] one\n- [ ] two",
        "## Injected heading",
        "- [ ] " + "x" * 2100,
    ]:
        try:
            validate_lines([bad])
        except VaultError:
            pass
        else:
            raise AssertionError(f"validate_lines accepted {bad!r}")

    # Surrounding whitespace is normalized away rather than rejected.
    assert validate_lines(["  - [ ] indented  "]) == ["- [ ] indented"]

    print("\nOK - pipeline, append-idempotency and write gate all pass.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
