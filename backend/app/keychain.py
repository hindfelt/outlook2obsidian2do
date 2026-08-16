"""API key storage.

Default is the macOS Keychain, so the key is not sitting in a plaintext file
that gets backed up or synced. Falls back to a 0600 file when `security` is
unavailable (non-macOS, or a locked keychain in a headless session).

Known caveat: `security add-generic-password` takes the secret as an argv
element, so it is briefly visible to `ps` on a multi-user machine. On a
single-user Mac that is an acceptable trade for keeping it out of a file; if you
disagree, set CREDENTIAL_STORE=file.
"""

from __future__ import annotations

import functools
import logging
import os
import subprocess
from pathlib import Path

log = logging.getLogger(__name__)

SERVICE = "outlook2obsidian2do"
ACCOUNT = "anthropic_api_key"

_FILE_STORE = Path(__file__).resolve().parents[2] / ".credentials"


def _store_kind() -> str:
    forced = os.environ.get("CREDENTIAL_STORE", "").strip().lower()
    if forced in {"keychain", "file"}:
        return forced
    return "keychain" if _security_available() else "file"


# Whether /usr/bin/security exists does not change while the process runs, and
# _store_kind() is on the path of every config read - do not fork for it twice.
@functools.lru_cache(maxsize=1)
def _security_available() -> bool:
    try:
        subprocess.run(
            ["/usr/bin/security", "-h"], capture_output=True, timeout=5, check=False
        )
        return Path("/usr/bin/security").exists()
    except (OSError, subprocess.SubprocessError):
        return False


# --- keychain ----------------------------------------------------------------


def _keychain_get() -> str | None:
    try:
        proc = subprocess.run(
            ["/usr/bin/security", "find-generic-password", "-s", SERVICE, "-a", ACCOUNT, "-w"],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        log.warning("keychain read failed: %s", exc)
        return None
    if proc.returncode != 0:
        return None
    value = proc.stdout.strip()
    return value or None


def _keychain_set(value: str) -> None:
    subprocess.run(
        [
            "/usr/bin/security", "add-generic-password",
            "-s", SERVICE, "-a", ACCOUNT, "-w", value,
            "-U",  # update if it already exists
        ],
        capture_output=True,
        text=True,
        timeout=15,
        check=True,
    )


def _keychain_delete() -> None:
    subprocess.run(
        ["/usr/bin/security", "delete-generic-password", "-s", SERVICE, "-a", ACCOUNT],
        capture_output=True,
        timeout=15,
        check=False,
    )


# --- file fallback -----------------------------------------------------------


def _file_get() -> str | None:
    if not _FILE_STORE.exists():
        return None
    value = _FILE_STORE.read_text(encoding="utf-8").strip()
    return value or None


def _file_set(value: str) -> None:
    _FILE_STORE.touch(mode=0o600, exist_ok=True)
    os.chmod(_FILE_STORE, 0o600)
    _FILE_STORE.write_text(value, encoding="utf-8")


def _file_delete() -> None:
    _FILE_STORE.unlink(missing_ok=True)


# --- public ------------------------------------------------------------------


def get_api_key() -> str | None:
    """Stored key, or the ANTHROPIC_API_KEY environment variable as a fallback."""
    stored = _keychain_get() if _store_kind() == "keychain" else _file_get()
    return stored or os.environ.get("ANTHROPIC_API_KEY") or None


def set_api_key(value: str) -> str:
    value = value.strip()
    if not value:
        raise ValueError("Empty API key")
    kind = _store_kind()
    if kind == "keychain":
        _keychain_set(value)
    else:
        _file_set(value)
    return kind


def clear_api_key() -> None:
    if _store_kind() == "keychain":
        _keychain_delete()
    else:
        _file_delete()


def hint(value: str | None) -> str:
    """Masked form safe to send to the task pane. Never return the raw key."""
    if not value:
        return ""
    if len(value) <= 12:
        return "…" + value[-4:]
    return f"{value[:7]}…{value[-4:]}"


def describe_store() -> str:
    return "macOS Keychain" if _store_kind() == "keychain" else str(_FILE_STORE)
