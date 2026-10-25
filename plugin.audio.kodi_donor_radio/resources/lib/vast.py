# SPDX-License-Identifier: GPL-2.0-or-later
"""Bounded resolver for one controlled VAST 4.x audio Linear ad."""
import math
import re
import secrets
import time
from urllib.parse import quote, urljoin, urlsplit
from xml.etree import ElementTree as ET

from ad_models import AdError, ResolvedAd, TrackingUrl
from http_client import Cancelled, ResponseTooLarge, read_bounded
from url_policy import origin


_MAX_XML = 256 * 1024
_MAX_URL = 8192
_EVENTS = frozenset(('start', 'firstQuartile', 'midpoint', 'thirdQuartile', 'complete'))
_MACRO = re.compile(r'\[([A-Za-z][A-Za-z0-9_]*)\]')
_DURATION = re.compile(r'^(\d{2}):(\d{2}):(\d{2})(?:\.(\d{1,3}))?$')


def substitute_macros(url, error_code=None, cachebuster=None):
    """Return an encoded controlled tracking URL, or None if a macro is unknown."""
    if any(name.upper() not in ('ERRORCODE', 'CACHEBUSTING') for name in _MACRO.findall(url)):
        return None
    values = {'CACHEBUSTING': str(cachebuster) if cachebuster is not None
              else '%08d' % secrets.randbelow(100000000)}
    if error_code is not None:
        values['ERRORCODE'] = str(error_code)
    if 'ERRORCODE' in (name.upper() for name in _MACRO.findall(url)) and error_code is None:
        return None
    return _MACRO.sub(lambda match: quote(values[match.group(1).upper()], safe=''), url)


def _tag(element):
    return element.tag.rsplit('}', 1)[-1]


def _children(element, tag):
    return [child for child in element if _tag(child) == tag]


def _one(element, tag):
    found = _children(element, tag)
    return found[0] if len(found) == 1 else None


def _text(element):
    return (element.text or '').strip() if element is not None else ''


class VastResolver:
    def __init__(self, client, fixture_origin):
        self.client = client
        self.fixture_origin = fixture_origin

    def _url(self, value, base, tracking=False):
        value = (value or '').strip()
        if not value or len(value) > _MAX_URL or (not tracking and '[' in value):
            raise AdError(100, 'Invalid advertising URL')
        if tracking and (_MACRO.sub('', value).find('[') >= 0 or
                         any(name.upper() not in ('ERRORCODE', 'CACHEBUSTING') for name in _MACRO.findall(value))):
            return None
        resolved = urljoin(base, value)
        if len(resolved) > _MAX_URL:
            raise AdError(100, 'Invalid advertising URL')
        try:
            parts = urlsplit(resolved)
            if (origin(resolved) != origin(self.fixture_origin) or
                    parts.scheme not in ('http', 'https') or parts.username or parts.password or
                    any(ord(char) < 33 or ord(char) == 127 for char in resolved)):
                raise ValueError()
        except ValueError:
            raise AdError(100, 'Advertising origin rejected') from None
        return resolved

    def _parse(self, data):
        try:
            text = data.decode('utf-8-sig')
        except UnicodeDecodeError:
            raise AdError(100, 'Invalid VAST XML')
        if re.search(r'<!\s*(?:DOCTYPE|ENTITY)\b', text, re.I):
            raise AdError(100, 'Invalid VAST XML')
        try:
            root = ET.fromstring(text)
        except (ET.ParseError, ValueError):
            raise AdError(100, 'Invalid VAST XML') from None
        count = 0
        stack = [(root, 1)]
        while stack:
            node, depth = stack.pop()
            count += 1
            if count > 4096 or depth > 32:
                raise AdError(100, 'VAST XML exceeds limits')
            stack.extend((child, depth + 1) for child in node)
        if _tag(root) != 'VAST' or not root.attrib.get('version', '').startswith('4.'):
            raise AdError(100, 'Unsupported VAST document')
        return root

    def _tracking(self, container, base):
        records = []
        for item in _children(container, 'Impression'):
            value = self._url(_text(item), base, tracking=True)
            if value:
                records.append(TrackingUrl('impression', value))
        for events in _children(container, 'TrackingEvents'):
            for item in _children(events, 'Tracking'):
                event = item.attrib.get('event', '')
                if event in _EVENTS:
                    value = self._url(_text(item), base, tracking=True)
                    if value:
                        records.append(TrackingUrl(event, value))
        if _tag(container) == 'Wrapper':
            for creatives in _children(container, 'Creatives'):
                for creative in _children(creatives, 'Creative'):
                    for linear in _children(creative, 'Linear'):
                        records.extend(self._tracking(linear, base))
        return records

    def _errors(self, container, base):
        return [value for item in _children(container, 'Error')
                if (value := self._url(_text(item), base, tracking=True))]

    def resolve(self, url, cancel):
        deadline = time.monotonic() + 5
        seen = set()
        inherited_tracking, inherited_errors = [], []
        wrappers = 0
        follow = True
        try:
            while True:
                current = self._url(url, self.fixture_origin)
                if current in seen:
                    raise AdError(302, 'VAST wrapper cycle')
                seen.add(current)
                with self.client.open(current, {'Accept': 'application/xml'}, cancel,
                                      deadline=deadline, allowed_origin=self.fixture_origin) as response:
                    if response.status != 200:
                        raise AdError(301, 'VAST request failed')
                    root = self._parse(read_bounded(response, _MAX_XML))
                    base = response.final_url
                ads = _children(root, 'Ad')
                if not ads:
                    if wrappers:
                        raise AdError(303, 'VAST wrapper returned no ad')
                    return None
                if len(ads) != 1 or ads[0].attrib.get('sequence'):
                    raise AdError(403, 'Unsupported VAST pod')
                ad = ads[0]
                wrapper = _one(ad, 'Wrapper')
                inline = _one(ad, 'InLine')
                if (wrapper is None) == (inline is None):
                    raise AdError(100, 'Invalid VAST ad')
                container = wrapper if wrapper is not None else inline
                inherited_errors.extend(self._errors(container, base))
                if _children(container, 'AdVerifications') or _children(container, 'InteractiveCreativeFile'):
                    raise AdError(403, 'Unsupported required VAST feature')
                inherited_tracking.extend(self._tracking(container, base))
                if wrapper is not None:
                    if not follow:
                        raise AdError(302, 'Wrapper following disabled')
                    wrappers += 1
                    if wrappers > 5:
                        raise AdError(302, 'Too many VAST wrappers')
                    if wrapper.attrib.get('allowMultipleAds', 'false').lower() == 'true':
                        raise AdError(403, 'Unsupported VAST pod')
                    follow = wrapper.attrib.get('followAdditionalWrappers', 'true').lower() != 'false'
                    uri = _one(wrapper, 'VASTAdTagURI')
                    url = self._url(_text(uri), base)
                    continue
                creatives = _one(inline, 'Creatives')
                creative = _children(creatives, 'Creative') if creatives is not None else []
                if len(creative) != 1:
                    raise AdError(403, 'Unsupported VAST creative')
                linear = _one(creative[0], 'Linear')
                if linear is None or _children(linear, 'InteractiveCreativeFile') or _children(linear, 'Icons'):
                    raise AdError(403, 'Unsupported VAST creative')
                duration_text = _text(_one(linear, 'Duration'))
                duration_match = _DURATION.fullmatch(duration_text)
                if duration_match is None:
                    raise AdError(403, 'Invalid VAST duration')
                hours, minutes, seconds = map(int, duration_match.group(1, 2, 3))
                if minutes >= 60 or seconds >= 60:
                    raise AdError(403, 'Invalid VAST duration')
                duration = hours * 3600 + minutes * 60 + seconds + int((duration_match.group(4) or '0').ljust(3, '0')) / 1000
                if not math.isfinite(duration) or not 0 < duration <= 60:
                    raise AdError(403, 'Invalid VAST duration')
                mediafiles = _one(linear, 'MediaFiles')
                choices = _children(mediafiles, 'MediaFile') if mediafiles is not None else []
                compatible = [item for item in choices if item.attrib.get('type', '').lower() in
                              ('application/vnd.apple.mpegurl', 'application/x-mpegurl') and
                              item.attrib.get('delivery', 'streaming').lower() == 'streaming']
                if not compatible:
                    raise AdError(403, 'No supported VAST media')
                media_url = self._url(_text(compatible[0]), base)
                tracking = inherited_tracking + self._tracking(linear, base)
                return ResolvedAd(ad.attrib.get('id', ''), media_url, duration,
                                  tuple(dict.fromkeys(tracking)), tuple(dict.fromkeys(inherited_errors)))
        except Cancelled:
            raise
        except AdError as error:
            raise AdError(error.code, error.reason, tuple(dict.fromkeys(inherited_errors))) from None
        except (OSError, TimeoutError, ResponseTooLarge, ValueError):
            raise AdError(301, 'VAST transport failed', tuple(dict.fromkeys(inherited_errors))) from None
