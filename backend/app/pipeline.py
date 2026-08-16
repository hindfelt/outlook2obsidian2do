"""Orchestrates the three agents and performs the vault write."""

from __future__ import annotations

import logging
from collections import defaultdict
from typing import Callable

from .agents import extract_tasks, format_tasks, route_tasks
from .config import settings
from .models import EmailPayload, ProcessResponse, RoutingDecision, WrittenTask
from .vault import VaultError, append_to_section, validate_lines

log = logging.getLogger(__name__)


def process_email(
    email: EmailPayload,
    on_stage: Callable[[str], None] | None = None,
) -> ProcessResponse:
    warnings: list[str] = []
    stage = on_stage or (lambda _s: None)

    # --- Agent 1: extract ----------------------------------------------------
    stage("extracting")
    extraction = extract_tasks(email)
    tasks = extraction.tasks
    log.info("extractor: %d task(s)", len(tasks))

    if not tasks:
        return ProcessResponse(
            ok=True,
            dry_run=email.dry_run,
            summary=extraction.summary or "No actionable tasks found.",
            tasks=[],
            files_written=[],
            warnings=["No actionable tasks found in this email."],
        )

    # --- Agent 2: route ------------------------------------------------------
    stage("routing")
    routing = route_tasks(email, tasks)
    decisions: dict[int, RoutingDecision] = {}
    for d in routing.decisions:
        if not 0 <= d.task_index < len(tasks):
            warnings.append(
                f"Router returned an out-of-range index ({d.task_index}); ignored."
            )
            continue
        if d.route_id not in settings.routes:
            warnings.append(
                f"Router returned unknown route {d.route_id!r}; "
                f"filed under {settings.fallback_route_id!r}."
            )
            d = d.model_copy(
                update={"route_id": settings.fallback_route_id, "confidence": 0.0}
            )
        decisions[d.task_index] = d

    for i in range(len(tasks)):
        if i not in decisions:
            warnings.append(
                f"Router skipped task {i}; filed under {settings.fallback_route_id!r}."
            )
            decisions[i] = RoutingDecision(
                task_index=i,
                route_id=settings.fallback_route_id,
                confidence=0.0,
                reason="Router returned no decision for this task.",
            )

    # --- Agent 3: format -----------------------------------------------------
    stage("writing")
    indices = sorted(decisions)
    formatted = format_tasks(email, tasks, indices)
    markdown_by_index = {f.task_index: f.markdown for f in formatted.lines}

    missing = [i for i in indices if i not in markdown_by_index]
    if missing:
        warnings.append(f"Writer produced no line for task(s) {missing}; skipped.")

    # --- Gate ----------------------------------------------------------------
    # One malformed line must not discard the others: a local model has spent
    # minutes on this run. Rejected lines become warnings, the rest are written.
    clean_by_index: dict[int, str] = {}
    for i in indices:
        if i not in markdown_by_index:
            continue
        try:
            clean_by_index[i] = validate_lines([markdown_by_index[i]])[0]
        except VaultError as exc:
            warnings.append(f"Writer produced an invalid line for task {i}; skipped ({exc}).")
            log.warning("task %d rejected by the write gate: %s", i, exc)

    # --- Write ---------------------------------------------------------------
    by_route: dict[str, list[int]] = defaultdict(list)
    for i in indices:
        if i in clean_by_index:
            by_route[decisions[i].route_id].append(i)

    written: list[WrittenTask] = []
    files_written: list[str] = []

    for route_id, task_indices in by_route.items():
        route = settings.routes[route_id]
        lines = [clean_by_index[i] for i in task_indices]

        if not email.dry_run:
            path = append_to_section(route, lines)
            files_written.append(str(path))
            log.info("wrote %d line(s) to %s", len(lines), path)

        for i, line in zip(task_indices, lines):
            written.append(
                WrittenTask(
                    text=tasks[i].text,
                    route_id=route_id,
                    file=route.file,
                    section=route.section,
                    markdown=line,
                    confidence=decisions[i].confidence,
                    # Display only. A local model can pad this with junk; the
                    # first sentence is what the pane has room for anyway.
                    reason=decisions[i].reason[:200],
                )
            )

    return ProcessResponse(
        ok=True,
        dry_run=email.dry_run,
        summary=extraction.summary,
        tasks=written,
        files_written=files_written,
        warnings=warnings,
    )
