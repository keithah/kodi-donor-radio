# SPDX-License-Identifier: GPL-2.0-or-later
"""Resolve bounded M3U/PLS indirection without discarding continuous probes."""
import re
from dataclasses import dataclass
from typing import Any, Optional
from urllib.parse import urldefrag, urljoin, urlsplit

from http_client import BODY_BYTES, HttpError, USER_AGENT, read_bounded


class ResolutionError(ValueError):
    pass


@dataclass(frozen=True)
class ResolvedSource:
    """Owner must close response and relay initial_bytes before reading it.

    For HLS response is None: final_url is a manifest fetched by the HLS layer.
    For continuous streams response is the exact open probe, with its cursor
    immediately after initial_bytes. Ownership transfers only on successful return.
    """
    final_url: str
    protocol: str
    response: Optional[Any] = None
    initial_bytes: bytes = b''


def resolve_station(url, client, cancel):
    seen = set()
    for depth in range(4):
        key = urldefrag(url)[0]
        if key in seen:
            raise ResolutionError('Playlist loop')
        seen.add(key)
        response = client.open(url, {'User-Agent': USER_AGENT, 'Icy-MetaData': '1'}, cancel)
        transferred = False
        try:
            if response.status != 200:
                raise HttpError('Station HTTP error')
            final_url = response.final_url
            final_key = urldefrag(final_url)[0]
            if final_key != key and final_key in seen:
                raise ResolutionError('Playlist redirect loop')
            seen.add(final_key)
            mime = response.headers.get('content-type', response.headers.get('Content-Type', '')).split(';')[0].lower()
            # All sources get a small sniff: MIME/extension may lie about a playlist.
            prefix = response.read(512)
            text = prefix.decode('utf-8-sig', errors='replace').lstrip()
            # A socket read may split the signature even when MIME is generic.
            while prefix and len(prefix) < 512 and (
                    not text or any(marker.startswith(text.lower()) for marker in ('#extm3u', '[playlist]'))
                    or b'\xef\xbb\xbf'.startswith(prefix)):
                block = response.read(512 - len(prefix))
                if not block:
                    break
                prefix += block
                text = prefix.decode('utf-8-sig', errors='replace').lstrip()
            extension = urlsplit(final_url).path.lower()
            is_playlist = (mime in ('audio/x-mpegurl', 'application/x-mpegurl', 'application/vnd.apple.mpegurl', 'audio/mpegurl', 'audio/x-scpls')
                           or text.startswith('#EXTM3U') or text.lower().startswith('[playlist]')
                           or extension.endswith(('.m3u', '.m3u8', '.pls')))
            if not is_playlist:
                transferred = True
                return ResolvedSource(final_url, 'continuous', response, prefix)
            body = read_bounded(response, BODY_BYTES, prefix)
            text = body.decode('utf-8-sig', errors='replace')
            if '#EXT-X-' in text:
                return ResolvedSource(final_url, 'hls')
            if depth >= 3:
                raise ResolutionError('Playlist indirection limit')
            if text.lstrip().lower().startswith('[playlist]') or mime == 'audio/x-scpls':
                matches = re.findall(r'^\s*file(\d+)\s*=\s*(.+?)\s*$', text, re.I | re.M)
                targets = [value for _, value in sorted(matches, key=lambda item: int(item[0]))]
            else:
                targets = [line.strip() for line in text.splitlines() if line.strip() and not line.lstrip().startswith('#')]
            if not targets:
                raise ResolutionError('Playlist has no stream URL')
            url = urljoin(final_url, targets[0])
        finally:
            if not transferred:
                response.close()
    raise ResolutionError('Playlist indirection limit')
