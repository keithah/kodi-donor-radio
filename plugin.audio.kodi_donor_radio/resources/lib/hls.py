# SPDX-License-Identifier: GPL-2.0-or-later
"""Bounded clear-HLS parsing; rewrite only explicitly supported references.

No network I/O or advertisement interpretation happens here. Callers decode a
bounded UTF-8 response and pass its final redirected URL. Transport URL policy
still validates every registered resource when it is fetched.
"""
from dataclasses import dataclass
from datetime import datetime
import math
import re
from typing import Mapping, Optional, Tuple
from urllib.parse import urljoin, urlsplit

from http_client import BODY_BYTES


class InvalidPlaylist(ValueError):
    pass


class UnsupportedPlaylist(InvalidPlaylist):
    pass


@dataclass(frozen=True)
class UriReference:
    line_index: int
    start: int
    end: int
    url: str
    kind: str


@dataclass(frozen=True)
class Segment:
    sequence: int
    discontinuity: int
    duration: float
    media_position: float
    uri: str


@dataclass(frozen=True)
class CueObservation:
    source: str
    attributes: Mapping[str, str]
    sequence: int
    discontinuity: int
    media_position: Optional[float]


@dataclass(frozen=True)
class Playlist:
    final_url: str
    lines: Tuple[str, ...]
    uri_references: Tuple[UriReference, ...]
    media_sequence: int
    discontinuity_sequence: int
    target_duration: Optional[float]
    segments: Tuple[Segment, ...]
    cues: Tuple[CueObservation, ...]
    is_master: bool


_UNSUPPORTED = frozenset({
    'EXT-X-SESSION-KEY', 'EXT-X-PART', 'EXT-X-PART-INF',
    'EXT-X-PRELOAD-HINT', 'EXT-X-SERVER-CONTROL', 'EXT-X-SKIP',
    'EXT-X-RENDITION-REPORT', 'EXT-X-CONTENT-STEERING', 'EXT-X-DEFINE',
})
_MASTER = frozenset({'EXT-X-MEDIA', 'EXT-X-STREAM-INF', 'EXT-X-I-FRAME-STREAM-INF', 'EXT-X-SESSION-DATA'})
_MEDIA = frozenset({
    'EXTINF', 'EXT-X-TARGETDURATION', 'EXT-X-MEDIA-SEQUENCE',
    'EXT-X-DISCONTINUITY-SEQUENCE', 'EXT-X-DISCONTINUITY', 'EXT-X-MAP',
    'EXT-X-BYTERANGE', 'EXT-X-PROGRAM-DATE-TIME', 'EXT-X-DATERANGE',
    'EXT-X-CUE-OUT', 'EXT-X-CUE-OUT-CONT', 'EXT-X-CUE-IN',
    'EXT-X-KEY', 'EXT-X-ENDLIST', 'EXT-X-PLAYLIST-TYPE', 'EXT-X-I-FRAMES-ONLY',
})
_ATTR_TAGS = frozenset({'EXT-X-MEDIA', 'EXT-X-MAP', 'EXT-X-STREAM-INF',
                         'EXT-X-I-FRAME-STREAM-INF', 'EXT-X-DATERANGE',
                         'EXT-X-KEY', 'EXT-X-START', 'EXT-X-SESSION-DATA'})
_URI_KEY = re.compile(r'(?:URI|URL)', re.I)
_URI_VALUE = re.compile(r'(?:[a-z][a-z0-9+.-]*://|\{\$)', re.I)


def _attributes(value):
    """Return decoded values and raw value spans, retaining original spelling.

    Common escaped quote/backslash values are accepted; other backslash escapes
    are rejected. Commas inside quoted values never split the attribute list.
    """
    attrs, spans, i = {}, {}, 0
    while i < len(value):
        match = re.match(r'([A-Za-z0-9-]+)=', value[i:])
        if not match:
            raise InvalidPlaylist('Malformed attribute list')
        key = match.group(1)
        if key in attrs:
            raise InvalidPlaylist('Duplicate attribute')
        i += len(match.group(0))
        quoted = i < len(value) and value[i] == '"'
        if quoted:
            i += 1
            start, decoded = i, []
            while i < len(value) and value[i] != '"':
                if value[i] == '\\':
                    i += 1
                    if i >= len(value) or value[i] not in ('"', '\\'):
                        raise InvalidPlaylist('Malformed attribute escape')
                decoded.append(value[i])
                i += 1
            if i >= len(value):
                raise InvalidPlaylist('Unterminated attribute')
            end = i
            i += 1
            decoded = ''.join(decoded)
        else:
            start = i
            while i < len(value) and value[i] != ',':
                if value[i].isspace() or value[i] in ('"', '\\'):
                    raise InvalidPlaylist('Malformed unquoted attribute')
                i += 1
            end, decoded = i, value[start:i]
        if not decoded:
            raise InvalidPlaylist('Empty attribute')
        attrs[key], spans[key] = decoded, (start, end, quoted)
        if i < len(value):
            if value[i] != ',' or i + 1 == len(value):
                raise InvalidPlaylist('Malformed attribute separator')
            i += 1
    if not attrs:
        raise InvalidPlaylist('Missing attributes')
    return attrs, spans


def _integer(value, positive=False):
    if not re.fullmatch(r'[0-9]+', value):
        raise InvalidPlaylist('Invalid integer')
    number = int(value)
    if positive and not number:
        raise InvalidPlaylist('Expected positive integer')
    return number


def _duration(value):
    if not re.fullmatch(r'[0-9]+(?:\.[0-9]+)?', value):
        raise InvalidPlaylist('Invalid duration')
    number = float(value)
    if not math.isfinite(number):
        raise InvalidPlaylist('Nonfinite duration')
    return number


def _resolve(uri, final_url):
    if not uri or any(c.isspace() or ord(c) < 32 or c in '\\"' for c in uri) or '{$' in uri:
        raise InvalidPlaylist('Invalid resource URI')
    url = urljoin(final_url, uri)
    try:
        parts = urlsplit(url)
        if parts.scheme not in ('http', 'https') or not parts.hostname or parts.username or parts.password:
            raise InvalidPlaylist('Unsupported resource URI')
        parts.port
    except ValueError as error:
        raise InvalidPlaylist('Invalid resource URI') from error
    return url


def parse_playlist(text: str, final_url: str) -> Playlist:
    """Parse a complete ordinary HLS playlist without altering source lines.

    media_position starts at zero for this window only. DATERANGE placement is
    not a boundary timestamp, so its media_position is unknown (None).
    """
    if len(text.encode('utf-8')) > BODY_BYTES:
        raise InvalidPlaylist('Playlist exceeds size limit')
    if any(ord(c) < 32 and c not in '\r\n' for c in text) or '\ufeff' in text:
        raise InvalidPlaylist('Invalid playlist characters')
    lines = tuple(text.splitlines(keepends=True))
    if not lines or lines[0].rstrip('\r\n') != '#EXTM3U':
        raise InvalidPlaylist('Missing HLS signature')
    _resolve(final_url, final_url)
    refs, segments, cues = [], [], []
    sequence, disc_base, disc, position = 0, 0, 0, 0.0
    target, pending_duration, pending_variant = None, None, False
    master, media, seen = False, False, set()
    for index, raw in enumerate(lines[1:], 1):
        line = raw.rstrip('\r\n')
        if not line:
            continue
        if not line.startswith('#'):
            url = _resolve(line, final_url)
            if pending_variant:
                kind, pending_variant = 'playlist', False
            elif pending_duration is not None:
                kind = 'segment'
                segments.append(Segment(sequence + len(segments), disc, pending_duration, position, url))
                position += pending_duration
                pending_duration = None
            else:
                raise InvalidPlaylist('URI without segment or variant declaration')
            refs.append(UriReference(index, 0, len(line), url, kind))
            continue
        if not line.startswith('#EXT'):
            continue
        tag, separator, value = line[1:].partition(':')
        if tag in _UNSUPPORTED:
            raise UnsupportedPlaylist('Unsupported HLS feature')
        if tag == 'EXTM3U':
            raise InvalidPlaylist('Repeated HLS signature')
        if tag in ('EXT-X-DISCONTINUITY', 'EXT-X-ENDLIST', 'EXT-X-CUE-IN',
                   'EXT-X-I-FRAMES-ONLY', 'EXT-X-INDEPENDENT-SEGMENTS') and separator:
            raise InvalidPlaylist('Unexpected flag value')
        master = master or tag in _MASTER
        media = media or tag in _MEDIA
        attrs, spans = {}, {}
        if tag in _ATTR_TAGS:
            attrs, spans = _attributes(value)
        elif tag == 'EXT-X-CUE-OUT':
            attrs = _attributes(value)[0] if '=' in value else ({'DURATION': value} if value else {})
        elif tag == 'EXT-X-CUE-OUT-CONT':
            if '=' in value:
                attrs = _attributes(value)[0]
            else:
                if _URI_KEY.search(value) or _URI_VALUE.search(value):
                    raise UnsupportedPlaylist('Unsupported URI-bearing cue')
                attrs = {'RAW': value}
        allowed_uri = 'URI' if tag in ('EXT-X-MEDIA', 'EXT-X-MAP', 'EXT-X-I-FRAME-STREAM-INF') else None
        if attrs:
            for key in attrs:
                if _URI_KEY.search(key) and key != allowed_uri:
                    raise UnsupportedPlaylist('Unsupported URI-bearing attribute')
        elif tag not in _MEDIA and tag not in ('EXT-X-VERSION', 'EXT-X-INDEPENDENT-SEGMENTS'):
            if _URI_KEY.search(value) or _URI_VALUE.search(value):
                raise UnsupportedPlaylist('Unsupported URI-bearing tag')
        if master and media:
            raise InvalidPlaylist('Mixed master and media tags')
        if allowed_uri:
            if 'URI' not in attrs and tag != 'EXT-X-MEDIA':
                raise InvalidPlaylist('Missing URI attribute')
            if tag == 'EXT-X-MEDIA' and attrs.get('TYPE') == 'CLOSED-CAPTIONS' and 'URI' in attrs:
                raise InvalidPlaylist('Closed captions cannot contain a URI')
            if 'URI' in attrs:
                start, end, quoted = spans['URI']
                if not quoted:
                    raise InvalidPlaylist('URI must be quoted')
                offset = len(tag) + 2
                refs.append(UriReference(index, offset + start, offset + end,
                                         _resolve(attrs['URI'], final_url),
                                         'map' if tag == 'EXT-X-MAP' else 'playlist'))
        if tag == 'EXT-X-KEY':
            if attrs != {'METHOD': 'NONE'}:
                raise UnsupportedPlaylist('Encrypted HLS is unsupported')
        elif tag == 'EXTINF':
            if pending_duration is not None or ',' not in value:
                raise InvalidPlaylist('Malformed segment duration')
            pending_duration = _duration(value.split(',', 1)[0])
        elif tag == 'EXT-X-STREAM-INF':
            if pending_variant or 'BANDWIDTH' not in attrs:
                raise InvalidPlaylist('Malformed variant')
            _integer(attrs['BANDWIDTH'], positive=True)
            pending_variant = True
        elif tag in ('EXT-X-TARGETDURATION', 'EXT-X-MEDIA-SEQUENCE', 'EXT-X-DISCONTINUITY-SEQUENCE', 'EXT-X-VERSION'):
            if tag in seen or (segments and tag != 'EXT-X-VERSION'):
                raise InvalidPlaylist('Repeated or late playlist header')
            seen.add(tag)
            number = _integer(value, tag in ('EXT-X-TARGETDURATION', 'EXT-X-VERSION'))
            if tag == 'EXT-X-TARGETDURATION':
                target = float(number)
            elif tag == 'EXT-X-MEDIA-SEQUENCE':
                if cues:
                    raise InvalidPlaylist('Sequence must precede cues')
                sequence = number
            elif tag == 'EXT-X-DISCONTINUITY-SEQUENCE':
                if disc or cues:
                    raise InvalidPlaylist('Discontinuity sequence must precede observations')
                disc_base = disc = number
        elif tag == 'EXT-X-DISCONTINUITY':
            disc += 1
        elif tag == 'EXT-X-BYTERANGE':
            if not re.fullmatch(r'[1-9][0-9]*(?:@[0-9]+)?', value):
                raise InvalidPlaylist('Invalid byte range')
        elif tag == 'EXT-X-PROGRAM-DATE-TIME':
            try:
                if 'T' not in value:
                    raise ValueError('Missing timestamp')
                datetime.fromisoformat(value.replace('Z', '+00:00'))
            except ValueError as error:
                raise InvalidPlaylist('Invalid program date time') from error
        if tag in ('EXT-X-DATERANGE', 'EXT-X-CUE-OUT', 'EXT-X-CUE-OUT-CONT', 'EXT-X-CUE-IN'):
            cues.append(CueObservation(tag, attrs, sequence + len(segments), disc,
                                       None if tag == 'EXT-X-DATERANGE' else position))
    if pending_duration is not None or pending_variant:
        raise InvalidPlaylist('Missing declared URI')
    if not master and target is None:
        raise InvalidPlaylist('Missing target duration')
    return Playlist(final_url, lines, tuple(refs), sequence, disc_base, target,
                    tuple(segments), tuple(cues), master)


def rewrite_playlist(playlist: Playlist, register) -> str:
    """Replace registered URI spans, preserving every other source character.

    register owns session resource limits and returns an opaque local route.
    Registration failure propagates; no partially rewritten text is returned.
    """
    lines = list(playlist.lines)
    for ref in playlist.uri_references:
        route = register(ref.url)
        if not route.startswith('/') or route.startswith('//') or any(c.isspace() or c in '\\"' for c in route):
            raise InvalidPlaylist('Registration must return an opaque local route')
        lines[ref.line_index] = lines[ref.line_index][:ref.start] + route + lines[ref.line_index][ref.end:]
    return ''.join(lines)
