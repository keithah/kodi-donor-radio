# SPDX-License-Identifier: GPL-2.0-or-later
"""Opt-in local diagnostics with conservative redaction and bounded storage."""
import json
import math
import re
import threading
from collections.abc import Mapping
from pathlib import Path
from typing import Callable

from models import Event, EventSink


EVIDENCE_BYTES = 2048
LOG_BYTES = 1024 * 1024
_REDACTED = "[REDACTED]"
_URL = re.compile(r"(?i)(?:[a-z][a-z0-9+.-]*:/{1,2}|//)")
_CREDENTIAL_TEXT = re.compile(
    r"(?i)\b(?:auth|username|authorization|proxy-authorization|cookie|set-cookie|password|passwd|"
    r"token|secret|api[-_ ]?key|credentials?)\s*[=:]|\b(?:bearer|basic)\s+\S+"
)
_CREDENTIAL_KEYS = (
    "authorization", "cookie", "password", "passwd", "token", "secret",
    "apikey", "credential", "userinfo", "username", "authentication",
)


def _truncate(text: str, limit: int = EVIDENCE_BYTES) -> str:
    return text.encode("utf-8", errors="replace")[:limit].decode("utf-8", errors="ignore")


def sanitize(value: object) -> object:
    """Copy bounded JSON data, redacting credentials before truncating text.

    Unstructured strings containing URLs or credential assignments are replaced
    entirely: partial URL parsing cannot safely preserve malformed cue/title data.
    Unknown objects are never stringified because their repr may contain secrets.
    """
    remaining = [128]

    def clean(item, depth=0):
        remaining[0] -= 1
        if remaining[0] < 0 or depth > 8:
            return "[TRUNCATED]"
        if isinstance(item, str):
            if _URL.search(item) or _CREDENTIAL_TEXT.search(item):
                return _REDACTED
            return _truncate(item)
        if item is None or isinstance(item, bool):
            return item
        if isinstance(item, int):
            return item if item.bit_length() <= 128 else _REDACTED
        if isinstance(item, float):
            return item if math.isfinite(item) else None
        if isinstance(item, Mapping):
            result = {}
            for index, (key, child) in enumerate(item.items()):
                if index >= 32 or remaining[0] <= 0:
                    break
                if not isinstance(key, str):
                    continue
                normalized = re.sub(r"[^a-z0-9]", "", key.lower())
                safe_key = _truncate(clean(key, depth + 1), 128)
                sensitive = (normalized in {"auth", "user", "login"}
                             or any(part in normalized for part in _CREDENTIAL_KEYS))
                result[safe_key] = _REDACTED if sensitive else clean(child, depth + 1)
            return result
        if isinstance(item, (list, tuple)):
            result = []
            for child in item[:32]:
                if remaining[0] <= 0:
                    break
                result.append(clean(child, depth + 1))
            return result
        return _REDACTED

    return clean(value)


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"))


def _bounded_value(value: object) -> object:
    safe = sanitize(value)
    encoded = safe if isinstance(safe, str) else _json(safe)
    if len(encoded.encode("utf-8")) > EVIDENCE_BYTES:
        return _truncate(encoded)
    return safe


class JsonEventSink:
    """Write sanitized JSON lines to the addon profile and Kodi's debug adapter.

    Each file is capped at 1 MiB, with two backups. A single sink serializes
    concurrent writes. A disabled sink does not touch its profile or callback.
    """
    def __init__(self, profile: Path, enabled: bool, debug_log: Callable[[str], None]):
        self.profile = Path(profile)
        self.enabled = enabled
        self.debug_log = debug_log
        self._lock = threading.Lock()

    def emit(self, event: Event) -> None:
        if not self.enabled:
            return
        # Independent field budgets preserve required timestamps even when an
        # upstream supplies hostile nested evidence. Bound every detail as well
        # so one oversized context cannot exceed an entire rotating file.
        record = {}
        for key, value in event.to_dict().items():
            if key == "details":
                record[key] = {name: _bounded_value(child) for name, child in value.items()}
            else:
                record[key] = _bounded_value(value)
        line = _json(record)
        payload = (line + "\n").encode("utf-8")
        with self._lock:
            self.debug_log(line)
            try:
                self.profile.mkdir(parents=True, exist_ok=True)
                path = self.profile / "events.jsonl"
                if path.exists() and path.stat().st_size + len(payload) > LOG_BYTES:
                    oldest = self.profile / "events.jsonl.2"
                    if oldest.exists():
                        oldest.unlink()
                    previous = self.profile / "events.jsonl.1"
                    if previous.exists():
                        previous.replace(oldest)
                    path.replace(previous)
                with path.open("ab") as output:
                    output.write(payload)
            except OSError:
                # Diagnostics cannot prevent radio playback on a read-only/full profile.
                self.debug_log("Diagnostic file write failed")
