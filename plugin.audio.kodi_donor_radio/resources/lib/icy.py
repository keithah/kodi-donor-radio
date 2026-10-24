# SPDX-License-Identifier: GPL-2.0-or-later
"""Incrementally remove ICY framing while observing metadata independently.

The caller handles plain streams without icy-metaint and downstream headers.
Metadata callbacks carry observations only; titles never classify ad breaks.
"""
import re
from typing import Callable


_FIELDS = re.compile(r"(?:^|;)\s*([A-Za-z][A-Za-z0-9_-]*)='(.*?)'(?=;|$)", re.DOTALL)


class IcyDecoder:
    """Decode a single ICY stream, retaining at most 255 * 16 metadata bytes.

    Each complete nonzero metadata block invokes ``on_metadata`` with a dict
    of quoted ICY fields. Invalid UTF-8 is replaced, and unrecognized fields
    yield an empty dict. Zero length blocks contain no observation. Callbacks
    may contain untrusted text: sanitize it at the event sink before logging.
    """

    def __init__(self, metaint: int, on_metadata: Callable[[dict], None]):
        if isinstance(metaint, bool) or not isinstance(metaint, int) or metaint <= 0:
            raise ValueError('metaint must be a positive integer')
        self._metaint = metaint
        self._on_metadata = on_metadata
        self._audio_remaining = metaint
        self._metadata_remaining = 0
        self._metadata = bytearray()
        self._finished = False

    def feed(self, chunk: bytes) -> bytes:
        """Return audio immediately; buffer only an incomplete metadata block."""
        if self._finished:
            raise ValueError('decoder is finished')
        audio = bytearray()
        position = 0
        while position < len(chunk):
            if self._audio_remaining:
                count = min(self._audio_remaining, len(chunk) - position)
                audio.extend(chunk[position:position + count])
                self._audio_remaining -= count
                position += count
            elif self._metadata_remaining:
                count = min(self._metadata_remaining, len(chunk) - position)
                self._metadata.extend(chunk[position:position + count])
                self._metadata_remaining -= count
                position += count
                if not self._metadata_remaining:
                    text = self._metadata.rstrip(b'\0').decode('utf-8', errors='replace')
                    self._metadata.clear()
                    self._audio_remaining = self._metaint
                    try:
                        self._on_metadata(dict(_FIELDS.findall(text)))
                    except Exception:
                        # Observation failures must never cost audio bytes.
                        pass
            else:
                self._metadata_remaining = chunk[position] * 16
                position += 1
                if not self._metadata_remaining:
                    self._audio_remaining = self._metaint
        return bytes(audio)

    def finish(self) -> None:
        """Discard incomplete metadata at EOF; already emitted audio is intact.

        An EOF within an audio interval is valid and needs no flush because
        feed returns every audio byte immediately. EOF at the length byte or
        inside metadata is tolerated without emitting a partial observation.
        """
        self._metadata.clear()
        self._finished = True
