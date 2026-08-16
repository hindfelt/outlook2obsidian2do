#!/usr/bin/env python3
"""Live end-to-end run against a throwaway vault. Real model calls.

    .venv/bin/python scripts/live_test.py ollama gemma4:31b
    .venv/bin/python scripts/live_test.py anthropic claude-haiku-4-5
"""

from __future__ import annotations

import os
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

TMP = Path(tempfile.mkdtemp(prefix="vault-live-"))
os.environ["OBSIDIAN_VAULT_PATH"] = str(TMP)
os.environ.setdefault("ROUTES_FILE", str(ROOT / "routes.example.json"))

from backend.app import runtime_config  # noqa: E402
from backend.app.models import EmailPayload  # noqa: E402
from backend.app.pipeline import process_email  # noqa: E402

EMAIL = EmailPayload(
    subject="Re: Platform separation + CFO search",
    sender="Alex Reed <alex.reed@acme.example>",
    received_at="2026-08-07T09:12:00Z",
    body="""Riley,

Good call yesterday. Two things.

First, can you and Jordan put together the straw man for the Acme platform split
before we bring Sam in? I'd like target structure and headcount allocation.
Let's get a couple of working calls in the diary over the next two weeks.

Second, the CFO search is drifting. Candidate quality has been poor and we are
now behind. I'll reset it with HR this week.

Alex
""",
)


def main() -> int:
    provider = sys.argv[1] if len(sys.argv) > 1 else "ollama"
    model = sys.argv[2] if len(sys.argv) > 2 else "gemma4:31b"

    # Persist nothing: point the config file somewhere disposable.
    runtime_config.CONFIG_FILE = TMP / "config.local.json"
    runtime_config.update(
        provider=provider,
        extractor_model=model,
        router_model=model,
        writer_model=model,
    )
    print(f"provider={provider} model={model}\nvault={TMP}\n")

    start = time.monotonic()
    result = process_email(EMAIL, on_stage=lambda s: print(f"  … {s}", flush=True))
    elapsed = time.monotonic() - start

    print(f"\ndone in {elapsed:.0f}s")
    print(f"summary: {result.summary}\n")
    for task in result.tasks:
        print(f"  [{task.route_id} {task.confidence:.2f}] {task.file} > ## {task.section}")
        print(f"    {task.markdown}")
    for warning in result.warnings:
        print(f"  warning: {warning}")

    print("\n--- written ---")
    for path in sorted(TMP.rglob("*.md")):
        print(f"\n### {path.relative_to(TMP)}")
        print(path.read_text(encoding="utf-8"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
