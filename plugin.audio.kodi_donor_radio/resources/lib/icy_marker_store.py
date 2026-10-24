# SPDX-License-Identifier: GPL-2.0-or-later
"""Private, bounded exact ICY marker labels owned by the relay service."""
import json
import os
from pathlib import Path
import re
import tempfile
import threading
import unicodedata

from events import sanitize


_MAX_BYTES = 2 * 1024 * 1024
_URI_SCHEME = re.compile(r'(?i)\b[a-z][a-z0-9+.-]*:')
_CREDENTIAL = re.compile(
    r'(?i)\b(?:[\w.-]*[-_])?(?:user(?:name)?|login|token|secret|password|passwd|'
    r'auth|credentials?|api[-_ ]?key)\s*[=:]')


def _safe_text(value):
    if (not isinstance(value, str) or not value.strip()
            or any(unicodedata.category(c).startswith('C') for c in value)
            or len(value.encode('utf-8')) > 256 or sanitize(value) != value
            or _URI_SCHEME.search(value) or _CREDENTIAL.search(value)):
        raise ValueError('Invalid marker rule')
    return value


def normalize_marker(marker):
    """Validate before normalization so hidden controls are never stripped away."""
    return _safe_text(_safe_text(marker).strip().upper())


class IcyMarkerStore:
    def __init__(self, profile_path):
        self.profile = Path(profile_path)
        self.path = self.profile / 'icy-markers.json'
        self._lock = threading.RLock()
        self._stations = {}
        self._load_failed = False
        try:
            with self.path.open('rb') as source:
                body = source.read(_MAX_BYTES + 1)
            if len(body) > _MAX_BYTES:
                raise ValueError('Invalid marker store')
            data = json.loads(body)
            if (not isinstance(data, dict) or set(data) != {'version', 'stations'}
                    or type(data['version']) is not int or data['version'] != 1
                    or not isinstance(data['stations'], dict) or len(data['stations']) > 256):
                raise ValueError('Invalid marker store')
            for station, rules in data['stations'].items():
                _safe_text(station)
                if not isinstance(rules, dict) or len(rules) > 8:
                    raise ValueError('Invalid marker store')
                for marker, action in rules.items():
                    if normalize_marker(marker) != marker or action not in ('start', 'end'):
                        raise ValueError('Invalid marker store')
            self._stations = data['stations']
        except FileNotFoundError:
            pass
        except (OSError, ValueError, UnicodeError, RecursionError):
            # A damaged or unsupported profile must not interrupt station playback.
            self._load_failed = True

    def lookup(self, station_uuid, marker):
        try:
            _safe_text(station_uuid)
            marker = normalize_marker(marker)
        except (ValueError, UnicodeError):
            return None
        with self._lock:
            return self._stations.get(station_uuid, {}).get(marker)

    def learn(self, station_uuid, marker, action):
        _safe_text(station_uuid)
        marker = normalize_marker(marker)
        if action not in ('start', 'end'):
            raise ValueError('Invalid marker action')
        with self._lock:
            if self._load_failed:
                raise ValueError('Marker store is unreadable; refusing to overwrite')
            stations = {key: dict(value) for key, value in self._stations.items()}
            rules = stations.setdefault(station_uuid, {})
            rules[marker] = action
            if len(stations) > 256 or len(rules) > 8:
                raise ValueError('Marker rule limit')
            self.profile.mkdir(parents=True, exist_ok=True, mode=0o700)
            os.chmod(str(self.profile), 0o700)
            fd, filename = tempfile.mkstemp(prefix='.icy-markers-', dir=str(self.profile))
            try:
                with os.fdopen(fd, 'w', encoding='utf-8') as output:
                    json.dump({'version': 1, 'stations': stations}, output, ensure_ascii=False)
                    output.flush()
                    os.fsync(output.fileno())
                os.replace(filename, str(self.path))
                self._stations = stations
            finally:
                if os.path.exists(filename):
                    os.unlink(filename)
