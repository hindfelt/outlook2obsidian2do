"""Local Obsidian vault I/O.

Everything the Writer agent produces goes through here, and nothing here trusts
a path that did not come from routes.json.
"""

from __future__ import annotations

import os
import re
import tempfile
import threading
from pathlib import Path

from .config import Route, settings

_write_lock = threading.Lock()

CHECKBOX_RE = re.compile(r"^- \[[ xX]\] .+")


class VaultError(RuntimeError):
    pass


def resolve_route_path(route: Route) -> Path:
    """Resolve a route to an absolute path, refusing anything outside the vault."""
    vault = settings.vault_path.resolve()
    if not vault.is_dir():
        raise VaultError(f"Vault path does not exist: {vault}")

    candidate = (vault / route.file).resolve()
    if candidate != vault and vault not in candidate.parents:
        raise VaultError(
            f"Route {route.id!r} points outside the vault: {route.file!r}"
        )
    return candidate


def validate_lines(lines: list[str]) -> list[str]:
    """Reject anything that is not a plain single-line Obsidian checklist item.

    The lines come out of an LLM that has read untrusted email text, so this is
    the last gate before touching the user's notes.
    """
    clean: list[str] = []
    for raw in lines:
        line = raw.strip("\n").strip()
        if "\n" in line or "\r" in line:
            raise VaultError(f"Refusing multi-line checklist entry: {line!r}")
        if not CHECKBOX_RE.match(line):
            raise VaultError(f"Refusing non-checklist line: {line!r}")
        if len(line) > 2000:
            raise VaultError("Refusing checklist line over 2000 characters")
        clean.append(line)
    return clean


def _section_heading(section: str) -> str:
    return f"## {section}"


def append_to_section(route: Route, lines: list[str]) -> Path:
    """Append checklist lines under `## <section>` in the route's file.

    Creates the file and/or the section if missing. Existing content is never
    rewritten - lines are inserted at the end of the section's block.
    """
    if not lines:
        raise VaultError("No lines to write")

    path = resolve_route_path(route)
    heading = _section_heading(route.section)

    with _write_lock:
        if path.exists():
            text = path.read_text(encoding="utf-8")
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            text = f"# {path.stem}\n"

        doc_lines = text.split("\n")
        heading_idx = next(
            (
                i
                for i, line in enumerate(doc_lines)
                if line.strip().lower() == heading.lower()
            ),
            None,
        )

        if heading_idx is None:
            # New section at the end of the file.
            while doc_lines and doc_lines[-1].strip() == "":
                doc_lines.pop()
            doc_lines.extend(["", heading, ""])
            doc_lines.extend(lines)
            doc_lines.append("")
        else:
            # Find the end of this section: next H1/H2, or end of file.
            end = len(doc_lines)
            for i in range(heading_idx + 1, len(doc_lines)):
                if re.match(r"^#{1,2} ", doc_lines[i]):
                    end = i
                    break
            insert_at = end
            while insert_at - 1 > heading_idx and doc_lines[insert_at - 1].strip() == "":
                insert_at -= 1
            doc_lines[insert_at:insert_at] = lines

        _write_atomic(path, "\n".join(doc_lines).rstrip("\n") + "\n")

    return path


def _write_atomic(path: Path, text: str) -> None:
    """Replace `path` in one step so a crash mid-write cannot leave a
    truncated note (which a sync client would then happily replicate)."""
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        # mkstemp creates 0600; keep the note's existing permissions.
        if path.exists():
            os.chmod(tmp, path.stat().st_mode & 0o7777)
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def route_catalog() -> list[dict[str, str]]:
    """The catalog handed to the Router agent."""
    return [
        {"id": r.id, "description": r.description, "destination": f"{r.file} > ## {r.section}"}
        for r in settings.routes.values()
    ]
