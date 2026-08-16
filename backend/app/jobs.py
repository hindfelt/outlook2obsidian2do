"""In-process job queue.

A local model needs minutes per email, which is far too long to hold an HTTP
request open from a task pane. POST /api/tasks returns a job id immediately and
the pane polls; you can close it and the write still happens.

Jobs run one at a time on a single worker thread. Two concurrent 17 GB local
models only fight over memory and Ollama serialises them anyway; a queue with a
visible position is more honest than two spinners.

Deliberately in-memory: jobs are worthless after a restart, and the vault - not
this dict - is the durable record.
"""

from __future__ import annotations

import logging
import queue
import threading
import time
import uuid
from dataclasses import dataclass, field, replace
from typing import Literal

from .models import EmailPayload, ProcessResponse
from .pipeline import process_email

log = logging.getLogger(__name__)

Status = Literal["queued", "running", "done", "error"]

MAX_JOBS = 200
STAGES = ("queued", "extracting", "routing", "writing", "done")

# A second click on the same email inside this window returns the first job
# instead of writing the same to-dos again. Long enough to cover a slow local
# run plus a "did that work?" re-click; short enough that a deliberate re-run
# tomorrow still goes through.
REUSE_WINDOW_S = 60 * 60


@dataclass
class Job:
    id: str
    status: Status = "queued"
    stage: str = "queued"
    subject: str = ""
    provider: str = ""
    item_id: str | None = None
    dry_run: bool = False
    created_at: float = field(default_factory=time.time)
    finished_at: float | None = None
    result: ProcessResponse | None = None
    error: str | None = None
    # Set on the response only when submit() handed back an existing job.
    reused: bool = False
    # Kept until the job runs; never serialised.
    email: EmailPayload | None = field(default=None, repr=False)

    def public(self) -> dict:
        return {
            "id": self.id,
            "status": self.status,
            "stage": self.stage,
            "subject": self.subject,
            "provider": self.provider,
            "item_id": self.item_id,
            "dry_run": self.dry_run,
            "reused": self.reused,
            "created_at": self.created_at,
            "elapsed_s": round((self.finished_at or time.time()) - self.created_at, 1),
            # 0 when running or finished; N when N jobs must finish first.
            "queue_position": _position(self),
            "result": self.result.model_dump() if self.result else None,
            "error": self.error,
        }


_jobs: dict[str, Job] = {}
# item_id -> id of the most recent real (non-preview) job for that email.
_by_item: dict[str, str] = {}
_lock = threading.Lock()

_queue: queue.Queue[Job] = queue.Queue()
_running: Job | None = None
_worker: threading.Thread | None = None


def _position(job: Job) -> int:
    if job.status != "queued":
        return 0
    with _lock:
        ahead = sum(
            1 for j in _jobs.values()
            if j.status == "queued" and j.created_at < job.created_at
        )
        return ahead + (1 if _running is not None else 0)


def _run(job: Job) -> None:
    global _running
    _running = job
    job.status = "running"
    job.stage = "extracting"
    try:
        # The pipeline runs the three agents in order; the stage label is
        # advanced optimistically so the pane shows progress rather than a
        # two-minute spinner with no information in it.
        result = process_email(job.email, on_stage=lambda s: setattr(job, "stage", s))
        job.result = result
        job.status = "done"
        job.stage = "done"
    except Exception as exc:  # noqa: BLE001 - surfaced to the pane verbatim
        log.exception("job %s failed", job.id)
        job.status = "error"
        job.error = str(exc)
    finally:
        job.finished_at = time.time()
        job.email = None
        _running = None


def _worker_loop() -> None:
    while True:
        job = _queue.get()
        try:
            _run(job)
        finally:
            _queue.task_done()


def _ensure_worker() -> None:
    global _worker
    if _worker is None or not _worker.is_alive():
        _worker = threading.Thread(target=_worker_loop, name="job-worker", daemon=True)
        _worker.start()


def _prune() -> None:
    if len(_jobs) <= MAX_JOBS:
        return
    for jid in sorted(_jobs, key=lambda j: _jobs[j].created_at)[: len(_jobs) - MAX_JOBS]:
        job = _jobs.pop(jid, None)
        if job and job.item_id and _by_item.get(job.item_id) == jid:
            _by_item.pop(job.item_id, None)


def _reusable(email: EmailPayload) -> Job | None:
    """An earlier real run of this exact email that should stand in for a new one.

    Previews never count - a preview followed by a real run is the normal
    workflow, not a duplicate. Failed runs never count either, so a retry after
    fixing the cause is one click.
    """
    if email.dry_run or email.force or not email.item_id:
        return None
    prev_id = _by_item.get(email.item_id)
    prev = _jobs.get(prev_id) if prev_id else None
    if prev is None or prev.status == "error":
        return None
    if time.time() - prev.created_at > REUSE_WINDOW_S:
        return None
    return prev


def submit(email: EmailPayload, provider: str) -> Job:
    with _lock:
        prev = _reusable(email)
        if prev is not None:
            log.info(
                "reusing job %s for item %s (%s, %.0fs old)",
                prev.id, prev.item_id, prev.status, time.time() - prev.created_at,
            )
            # Flag on a shallow copy so the stored job's own polls stay unflagged.
            return replace(prev, reused=True)

        job = Job(
            id=uuid.uuid4().hex[:12],
            subject=email.subject,
            provider=provider,
            item_id=email.item_id,
            dry_run=email.dry_run,
            email=email,
        )
        _jobs[job.id] = job
        if job.item_id and not job.dry_run:
            _by_item[job.item_id] = job.id
        _prune()

    _ensure_worker()
    _queue.put(job)
    return job


def get(job_id: str) -> Job | None:
    return _jobs.get(job_id)


def recent(limit: int = 10) -> list[dict]:
    with _lock:
        jobs = sorted(_jobs.values(), key=lambda j: j.created_at, reverse=True)
    return [j.public() for j in jobs[:limit]]
