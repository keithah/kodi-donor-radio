# SPDX-License-Identifier: GPL-2.0-or-later
"""Opt-in network VAST pre-roll (Live365 Audience Network style tags).

The addon requests a configured HTTPS VAST tag before a Radio Browser station,
caches each returned progressive MP3 creative in memory, serves it to Kodi from
the loopback relay, and fires Impression and Linear tracking URLs from observed
Kodi player time. The station then starts after the last creative ends.

Fetching or caching a creative never counts as an impression. A user stop,
playback error or forward seek never produces later quartile/complete events,
and each event is dispatched at most once.
"""
from dataclasses import dataclass, field
from datetime import datetime, timezone
import ipaddress
import math
import queue
import re
import secrets
import socket
import threading
import time
from urllib.parse import parse_qsl, quote, urlencode, urljoin, urlsplit, urlunsplit
from xml.etree import ElementTree as ET

from http_client import Cancelled, ResponseTooLarge, read_bounded
from models import Event


TEST_TAG = 'https://ssp.audiocdn.com/vast/tag?zoneId=ortb-test-zone&position=pre'
BUNDLE_ID = 'plugin.audio.kodi_donor_radio'
EVENTS = ('start', 'firstQuartile', 'midpoint', 'thirdQuartile', 'complete')
_QUARTILES = (('firstQuartile', .25), ('midpoint', .5), ('thirdQuartile', .75))
_MAX_XML = 256 * 1024
_MAX_MEDIA = 12 * 1024 * 1024
_MAX_URL = 8192
_MAX_ADS = 5
_MAX_WRAPPERS = 5
_MAX_DURATION = 180
_RETAIN_SECONDS = 15 * 60
_MAX_RETAINED = 8
_PREPARE_SECONDS = 30.0  # Below the plugin's 35 s control timeout.
_MAX_PREPARING = 2
_MEDIA_TYPES = ('audio/mpeg', 'audio/mp3', 'audio/x-mpeg')
_DURATION = re.compile(r'^(\d{1,2}):(\d{2}):(\d{2})(?:\.(\d{1,3}))?$')
_MACRO = re.compile(r'\[([A-Za-z][A-Za-z0-9_]*)\]|%5[Bb]([A-Za-z][A-Za-z0-9_]*)%5[Dd]')
_TOKEN = re.compile(r'^[0-9a-f]{48}$')
# Radio Browser exposes language names; the tag wants ISO 639-1 codes.
_LANGUAGES = {
    'english': 'en', 'spanish': 'es', 'french': 'fr', 'german': 'de', 'italian': 'it',
    'portuguese': 'pt', 'brazilian portuguese': 'pt', 'dutch': 'nl', 'russian': 'ru',
    'polish': 'pl', 'greek': 'el', 'turkish': 'tr', 'arabic': 'ar', 'chinese': 'zh',
    'mandarin': 'zh', 'cantonese': 'zh', 'japanese': 'ja', 'korean': 'ko', 'hindi': 'hi',
    'swedish': 'sv', 'norwegian': 'no', 'danish': 'da', 'finnish': 'fi', 'czech': 'cs',
    'slovak': 'sk', 'hungarian': 'hu', 'romanian': 'ro', 'bulgarian': 'bg', 'croatian': 'hr',
    'serbian': 'sr', 'slovenian': 'sl', 'ukrainian': 'uk', 'hebrew': 'he', 'persian': 'fa',
    'indonesian': 'id', 'malay': 'ms', 'thai': 'th', 'vietnamese': 'vi', 'tagalog': 'tl',
    'filipino': 'tl', 'catalan': 'ca', 'basque': 'eu', 'galician': 'gl', 'irish': 'ga',
    'welsh': 'cy', 'icelandic': 'is', 'estonian': 'et', 'latvian': 'lv', 'lithuanian': 'lt',
    'afrikaans': 'af', 'swahili': 'sw', 'bengali': 'bn', 'urdu': 'ur', 'tamil': 'ta',
}


class NetworkAdError(Exception):
    """VAST error code plus the Error URLs collected before the failure."""
    def __init__(self, code, reason, error_urls=()):
        self.code, self.reason, self.error_urls = code, reason, tuple(error_urls)
        super().__init__(reason)


@dataclass(frozen=True)
class NetworkAdConfig:
    enabled: bool = False
    tag_url: str = ''
    publisher_domain: str = ''
    publisher_name: str = ''
    content_genre: str = ''
    content_categories: str = ''
    blocked_categories: str = ''
    max_ads: int = 1
    break_ms: int = 0
    device_ip: str = ''
    user_agent: str = ''
    store_url: str = ''
    maid: str = ''
    gdpr: str = ''
    consent: str = ''
    schain: str = ''


@dataclass(frozen=True)
class NetworkAd:
    ad_id: str
    sequence: int
    title: str
    media_url: str
    duration_seconds: float
    impressions: tuple
    tracking: tuple  # ((event, url), ...)
    error_urls: tuple


def validate_tag_url(value):
    """Return a normalized HTTPS tag URL with zoneId and position, else raise."""
    value = (value or '').strip()
    parts = urlsplit(value)
    params = dict(parse_qsl(parts.query, keep_blank_values=True))
    if (parts.scheme != 'https' or not parts.hostname or parts.username or parts.password
            or parts.fragment or len(value) > _MAX_URL or not params.get('zoneId')
            or params.get('position', 'pre') not in ('pre', 'mid', 'post')
            or any(ord(char) < 33 or ord(char) == 127 for char in value)):
        raise ValueError('Invalid network VAST tag')
    return value


def detect_device_ipv4():
    """The device's own IPv4 when it is publicly routable; '' behind NAT.

    A UDP connect selects the outbound interface without sending a packet.
    """
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            probe.connect(('192.0.2.1', 9))
            address = ipaddress.ip_address(probe.getsockname()[0])
    except (OSError, ValueError):
        return ''
    return str(address) if address.is_global else ''


def _language_code(value):
    for name in re.split(r'[,;/]', (value or '').lower()):
        name = name.strip()
        if re.fullmatch(r'[a-z]{2}', name):
            return name
        if name in _LANGUAGES:
            return _LANGUAGES[name]
    return ''


def _genre(tags, fallback):
    for tag in (tags or '').split(','):
        tag = tag.strip()
        if tag and len(tag) <= 64:
            return tag.title() if tag.islower() else tag
    return fallback


def build_request(config, station, position='pre'):
    """Return (tag URL, headers) carrying every guide parameter we can supply.

    Values already present in the configured tag (zoneId, publisher_domain...)
    win over derived ones, so a partner-provided tag is never rewritten.
    """
    base = validate_tag_url(config.tag_url)
    parts = urlsplit(base)
    pairs = parse_qsl(parts.query, keep_blank_values=True)
    present = {key for key, _value in pairs}
    tag_gdpr = [value for key, value in pairs if key == 'gdpr']
    effective_gdpr = tag_gdpr[-1] if tag_gdpr else config.gdpr
    country = (getattr(station, 'country_code', '') or '').strip().upper()
    derived = (
        ('position', position),
        ('content_genre', _genre(getattr(station, 'tags', ''), config.content_genre.strip())),
        ('content_language', _language_code(getattr(station, 'language', ''))),
        ('content_country', country if re.fullmatch(r'[A-Z]{2}', country) else ''),
        ('content_categories', config.content_categories.strip()),
        ('blocked_categories', config.blocked_categories.strip()),
        ('publisher_name', config.publisher_name.strip() or (getattr(station, 'name', '') or '').strip()[:128]),
        ('publisher_domain', config.publisher_domain.strip()),
        ('maxAds', str(max(1, min(_MAX_ADS, int(config.max_ads or 1))))),
        ('duration', str(int(config.break_ms)) if int(config.break_ms or 0) > 0 else ''),
        ('bundle_id', BUNDLE_ID),
        ('store_url', config.store_url.strip()),
        ('maid', config.maid.strip()),
        ('gdpr', config.gdpr if config.gdpr in ('0', '1') else ''),
        ('consent', config.consent.strip() if effective_gdpr == '1' else ''),
        ('schain', config.schain.strip()),
    )
    pairs.extend((key, value) for key, value in derived if value and key not in present)
    url = urlunsplit((parts.scheme, parts.netloc, parts.path,
                      urlencode(pairs, quote_via=quote), ''))
    headers = {'Accept': 'application/xml, text/xml'}
    device_ip = config.device_ip.strip() or detect_device_ipv4()
    try:
        if device_ip and ipaddress.ip_address(device_ip).version == 4:
            headers['X-Device-IP'] = device_ip
    except ValueError:
        pass
    if config.user_agent:
        headers['X-Device-User-Agent'] = config.user_agent[:512]
        headers['User-Agent'] = config.user_agent[:512]
    return url, headers


def expand_macros(url, error_code=None, playhead=None, asset=None):
    """Fill known VAST macros in raw or percent-encoded form; keep unknown ones."""
    now = datetime.now(timezone.utc).isoformat(timespec='milliseconds').replace('+00:00', 'Z')
    def clock(seconds):
        millis = int(round(max(0.0, seconds) * 1000))
        return '%02d:%02d:%02d.%03d' % (millis // 3600000, millis // 60000 % 60,
                                        millis // 1000 % 60, millis % 1000)
    values = {'CACHEBUSTING': '%08d' % secrets.randbelow(100000000), 'TIMESTAMP': now}
    if error_code is not None:
        values['ERRORCODE'] = str(error_code)
    if playhead is not None:
        values['ADPLAYHEAD'] = values['MEDIAPLAYHEAD'] = clock(playhead)
    if asset:
        values['ASSETURI'] = asset
    def replace(match):
        name = (match.group(1) or match.group(2)).upper()
        return quote(values[name], safe='') if name in values else match.group(0)
    return _MACRO.sub(replace, url)


def _tag(element):
    return element.tag.rsplit('}', 1)[-1]


def _children(element, tag):
    return [child for child in element if _tag(child) == tag] if element is not None else []


def _one(element, tag):
    found = _children(element, tag)
    return found[0] if found else None


def _text(element):
    return (element.text or '').strip() if element is not None else ''


def _url(value, base):
    value = (value or '').strip()
    if not value or len(value) > _MAX_URL:
        return None
    resolved = urljoin(base, value)
    parts = urlsplit(resolved)
    if (parts.scheme not in ('http', 'https') or not parts.hostname or parts.username
            or parts.password or any(ord(char) < 33 or ord(char) == 127 for char in resolved)):
        return None
    return resolved


def parse_vast(data):
    try:
        text = data.decode('utf-8-sig')
    except UnicodeDecodeError:
        raise NetworkAdError(100, 'Invalid VAST XML') from None
    if re.search(r'<!\s*(?:DOCTYPE|ENTITY)\b', text, re.I):
        raise NetworkAdError(100, 'Invalid VAST XML')
    try:
        root = ET.fromstring(text)
    except (ET.ParseError, ValueError):
        raise NetworkAdError(100, 'Invalid VAST XML') from None
    count, stack = 0, [(root, 1)]
    while stack:
        node, depth = stack.pop()
        count += 1
        if count > 8192 or depth > 32:
            raise NetworkAdError(100, 'VAST XML exceeds limits')
        stack.extend((child, depth + 1) for child in node)
    if _tag(root) != 'VAST' or root.attrib.get('version', '')[:2] not in ('2.', '3.', '4.'):
        raise NetworkAdError(102, 'Unsupported VAST version')
    return root


def _duration(text):
    match = _DURATION.fullmatch(text or '')
    if match is None:
        return None
    hours, minutes, seconds = map(int, match.group(1, 2, 3))
    if minutes >= 60 or seconds >= 60:
        return None
    value = hours * 3600 + minutes * 60 + seconds + int((match.group(4) or '0').ljust(3, '0')) / 1000
    return value if math.isfinite(value) and 0 < value <= _MAX_DURATION else None


def _linear_tracking(linear, base):
    tracking = []
    for events in _children(linear, 'TrackingEvents'):
        for item in _children(events, 'Tracking'):
            event = item.attrib.get('event', '')
            if event in EVENTS and (u := _url(_text(item), base)):
                tracking.append((event, u))
    return tracking


def _collect(container, base, linears=True):
    """Ad-level impressions and errors; Linear tracking too unless linears is False.

    An InLine passes linears=False: its tracking is taken from the one Linear
    whose media plays, so an unplayed Linear never receives beacons.
    """
    impressions = [u for item in _children(container, 'Impression') if (u := _url(_text(item), base))]
    errors = [u for item in _children(container, 'Error') if (u := _url(_text(item), base))]
    tracking = []
    if linears:
        for creatives in _children(container, 'Creatives'):
            for creative in _children(creatives, 'Creative'):
                for linear in _children(creative, 'Linear'):
                    tracking.extend(_linear_tracking(linear, base))
    return impressions, errors, tracking


class NetworkVastResolver:
    """Resolve an ad pod of InLine audio ads, following bounded Wrapper chains."""
    def __init__(self, client, timeout=8):
        self.client, self.timeout = client, timeout

    def _fetch(self, url, headers, cancel, deadline):
        with self.client.open(url, headers, cancel, deadline=deadline) as response:
            if response.status == 204:
                return None, response.final_url
            if response.status != 200:
                raise NetworkAdError(301, 'VAST request failed')
            return parse_vast(read_bounded(response, _MAX_XML)), response.final_url

    def resolve(self, url, headers, cancel, max_ads=1):
        deadline = time.monotonic() + self.timeout
        try:
            root, base = self._fetch(url, headers, cancel, deadline)
        except (Cancelled, NetworkAdError):
            raise
        except (OSError, TimeoutError, ResponseTooLarge, ValueError):
            raise NetworkAdError(301, 'VAST transport failed') from None
        if root is None:
            return []
        ads = _children(root, 'Ad')
        pod = sorted((a for a in ads if a.attrib.get('sequence', '').isdigit()),
                     key=lambda a: int(a.attrib['sequence']))
        # A pod wins; otherwise standalone ads form a buffet and we take the first ones.
        chosen = (pod or [a for a in ads if not a.attrib.get('sequence')])[:max(1, min(max_ads, _MAX_ADS))]
        result = []
        for index, ad in enumerate(chosen):
            try:
                result.append(self._ad(ad, base, headers, cancel, deadline, index + 1, [], [], [], 0))
            except NetworkAdError as error:
                if not result and len(chosen) == 1:
                    raise
                # A pod member that fails is skipped; its Error URLs still fire.
                result.append(error)
        return result

    def _ad(self, ad, base, headers, cancel, deadline, sequence, impressions, errors, tracking, depth):
        wrapper, inline = _one(ad, 'Wrapper'), _one(ad, 'InLine')
        container = inline if inline is not None else wrapper
        if container is None:
            raise NetworkAdError(101, 'VAST ad has no InLine or Wrapper', errors)
        more_impressions, more_errors, more_tracking = _collect(container, base, inline is None)
        impressions, errors, tracking = (impressions + more_impressions, errors + more_errors,
                                         tracking + more_tracking)
        if inline is None:
            if depth >= _MAX_WRAPPERS:
                raise NetworkAdError(302, 'Too many VAST wrappers', errors)
            target = _url(_text(_one(wrapper, 'VASTAdTagURI')), base)
            if target is None:
                raise NetworkAdError(301, 'Invalid wrapper URI', errors)
            try:
                root, next_base = self._fetch(target, headers, cancel, deadline)
            except Cancelled:
                raise
            except NetworkAdError as error:
                raise NetworkAdError(error.code, error.reason, errors) from None
            except (OSError, TimeoutError, ResponseTooLarge, ValueError):
                raise NetworkAdError(301, 'VAST wrapper transport failed', errors) from None
            inner = _children(root, 'Ad') if root is not None else []
            if not inner:
                raise NetworkAdError(303, 'VAST wrapper returned no ad', errors)
            return self._ad(inner[0], next_base, headers, cancel, deadline, sequence,
                            impressions, errors, tracking, depth + 1)
        for creatives in _children(inline, 'Creatives'):
            for creative in _children(creatives, 'Creative'):
                linear = _one(creative, 'Linear')
                if linear is None:
                    continue
                duration = _duration(_text(_one(linear, 'Duration')))
                if duration is None:
                    raise NetworkAdError(101, 'Invalid VAST duration', errors)
                media = [m for m in _children(_one(linear, 'MediaFiles'), 'MediaFile')
                         if m.attrib.get('type', '').lower() in _MEDIA_TYPES
                         and m.attrib.get('delivery', 'progressive').lower() == 'progressive']
                media_url = _url(_text(media[0]), base) if media else None
                if media_url is None or urlsplit(media_url).scheme != 'https':
                    raise NetworkAdError(403, 'No supported MP3 media file', errors)
                return NetworkAd(
                    ad.attrib.get('id', '')[:128], sequence,
                    _text(_one(inline, 'AdTitle'))[:256], media_url, duration,
                    tuple(dict.fromkeys(impressions)),
                    tuple(dict.fromkeys(tracking + _linear_tracking(linear, base))),
                    tuple(dict.fromkeys(errors)))
        raise NetworkAdError(201, 'VAST ad has no Linear creative', errors)


def looks_like_mp3(data):
    if data[:3] == b'ID3' and len(data) > 10:
        size = 0
        for byte in data[6:10]:
            size = (size << 7) | (byte & 0x7f)
        offset = 10 + size + (10 if data[5] & 0x10 else 0)
    else:
        offset = 0
    for index in range(offset, min(len(data) - 1, offset + 4096)):
        if data[index] == 0xff and data[index + 1] & 0xe0 == 0xe0 and (data[index + 1] >> 1) & 3:
            return True
    return False


class _Beacons:
    """Single-attempt background GETs; a slow tracker never stalls playback."""
    def __init__(self, client, emit):
        self.client, self.emit = client, emit
        self.cancel = threading.Event()
        self.queue = queue.Queue(maxsize=256)
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def send(self, record, url, headers):
        try:
            self.queue.put_nowait((record, url, headers))
        except queue.Full:
            self.emit(dict(record, outcome='dropped'))

    def _run(self):
        while not self.cancel.is_set():
            try:
                item = self.queue.get(timeout=.25)
            except queue.Empty:
                continue
            if item is None:
                self.queue.task_done()
                return
            record, url, headers = item
            started = time.monotonic()
            try:
                with self.client.open(url, headers, self.cancel,
                                      deadline=time.monotonic() + 8) as response:
                    status = response.status
                    read_bounded(response, 64 * 1024)
                outcome = 'sent' if 200 <= status < 400 else 'http_%d' % status
            except Exception as error:  # Tracker failure must not affect playback.
                outcome = type(error).__name__
            self.emit(dict(record, outcome=outcome,
                           latency_ms=int((time.monotonic() - started) * 1000)))
            self.queue.task_done()

    def close(self, wait=True):
        try:
            self.queue.put_nowait(None)
        except queue.Full:
            self.cancel.set()
        if wait:
            self.thread.join(10)
        self.cancel.set()


@dataclass
class _Playback:
    token: str
    ad: NetworkAd
    data: bytes
    created: float
    next_url: str = ''
    station_uuid: str = ''
    fired: set = field(default_factory=set)
    started: bool = False
    live: bool = False  # Kodi is playing it or was just told to.
    requested: bool = False  # Kodi asked the relay for its bytes.
    tainted: bool = False
    finished: bool = False
    last_position: float = -1.0
    last_wall: float = 0.0
    max_position: float = 0.0


class NetworkAdService:
    """Service-side state for prepared creatives and their playback tracking."""
    def __init__(self, client, sink=None, clock=time.monotonic):
        self.client, self.sink, self.clock = client, sink, clock
        self.base_url = ''
        self.config = NetworkAdConfig()
        self._lock = threading.Lock()
        self._items = {}
        self._preparing = threading.BoundedSemaphore(_MAX_PREPARING)
        self._closing = threading.Event()
        self._first = None  # (token, prepared_at) of the newest break.
        self._beacons = _Beacons(client, self._emit)

    def _emit(self, details):
        if self.sink is None:
            return
        try:
            self.sink.emit(Event('network_ad', details.get('token', '')[:12], details.get('station', ''),
                                 'vast', 'network', details.get('event', ''),
                                 datetime.now(timezone.utc).isoformat(), self.clock(),
                                 {k: v for k, v in details.items() if k not in ('token', 'station')}))
        except Exception:
            pass

    def route(self, token):
        return '%s/network-ads/%s.mp3' % (self.base_url, token)

    def _headers(self):
        # Trackers see the listener's own request; X-Device-* stays on the ad request.
        user_agent = self.config.user_agent
        return {'User-Agent': user_agent} if user_agent else {}

    def _fire_errors(self, urls, code, token='', station=''):
        headers = self._headers()
        for url in urls:
            self._beacons.send({'token': token, 'station': station, 'event': 'error', 'code': code},
                               expand_macros(url, error_code=code), headers)

    def prepare(self, station, cancel=None, follow_url=''):
        """Resolve and download a pre-roll break; return playable items or [].

        Without a caller event, closing the service cancels the work.
        """
        config = self.config
        if not config.enabled or self._closing.is_set():
            return []
        cancel = cancel or self._closing
        if not self._preparing.acquire(blocking=False):
            self._emit({'event': 'vast_busy', 'station': station.uuid})
            return []
        try:
            return self._prepare(config, station, cancel, follow_url)
        finally:
            self._preparing.release()

    def _prepare(self, config, station, cancel, follow_url):
        deadline = time.monotonic() + _PREPARE_SECONDS
        url, headers = build_request(config, station)
        resolver = NetworkVastResolver(self.client)
        started = self.clock()
        try:
            ads = resolver.resolve(url, headers, cancel, config.max_ads)
        except NetworkAdError as error:
            self._emit({'event': 'vast_error', 'station': station.uuid, 'code': error.code,
                        'reason': error.reason})
            self._fire_errors(error.error_urls, error.code, station=station.uuid)
            return []
        self._emit({'event': 'vast_resolved', 'station': station.uuid,
                    'ads': len(ads), 'elapsed_ms': int((self.clock() - started) * 1000)})
        prepared = []
        media_headers = {k: v for k, v in headers.items() if k == 'User-Agent'}
        for ad in ads:
            if isinstance(ad, NetworkAdError):
                self._fire_errors(ad.error_urls, ad.code, station=station.uuid)
                continue
            try:
                if time.monotonic() >= deadline:
                    raise TimeoutError('Pre-roll budget spent')
                with self.client.open(ad.media_url, media_headers, cancel,
                                      deadline=min(deadline, time.monotonic() + 15)) as response:
                    if response.status != 200:
                        raise NetworkAdError(401, 'Creative unavailable')
                    data = read_bounded(response, _MAX_MEDIA)
                if not looks_like_mp3(data):
                    raise NetworkAdError(403, 'Creative is not MP3')
            except Cancelled:
                raise
            except Exception as error:
                code = error.code if isinstance(error, NetworkAdError) else (
                    402 if isinstance(error, TimeoutError) else 401)
                self._emit({'event': 'creative_error', 'station': station.uuid, 'code': code})
                self._fire_errors(ad.error_urls, code, station=station.uuid)
                continue
            prepared.append(_Playback(secrets.token_hex(24), ad, data, self.clock(),
                                      station_uuid=station.uuid))
        for index, item in enumerate(prepared):
            item.next_url = prepared[index + 1].token if index + 1 < len(prepared) else follow_url
        with self._lock:
            if self._closing.is_set():
                return []
            self._expire_locked(len(prepared))
            for item in prepared:
                self._items[item.token] = item
            if prepared:
                self._first = (prepared[0].token, self.clock())
        return [{'token': item.token, 'url': self.route(item.token), 'title': item.ad.title,
                 'duration': item.ad.duration_seconds} for item in prepared]

    def _expire_locked(self, incoming=0):
        now = self.clock()
        for token, item in list(self._items.items()):
            if item.finished:
                del self._items[token]
        # Never evict the creative on air or the rest of its chain, by age or by
        # count (a paused ad may be older than _RETAIN_SECONDS); the cap may
        # then be exceeded by at most one pod.
        protected = set()
        for item in self._items.values():
            if not item.live:
                continue
            while item is not None and item.token not in protected:
                protected.add(item.token)
                item = self._items.get(item.next_url)
        for token, item in list(self._items.items()):
            if token not in protected and now - item.created > _RETAIN_SECONDS:
                del self._items[token]
        while len(self._items) + incoming > _MAX_RETAINED:
            spare = [item for item in self._items.values() if item.token not in protected]
            if not spare:
                break
            oldest = min(spare, key=lambda item: item.created)
            del self._items[oldest.token]

    def media(self, token):
        if not _TOKEN.fullmatch(token or ''):
            return None
        with self._lock:
            item = self._items.get(token)
            if item is None or item.finished:
                return None
            item.requested = True
            return item.data

    def unstarted_first(self, within=15.0):
        """Token of the newest break's first creative if Kodi never started it.

        Only a creative Kodi already requested from the relay qualifies, so an
        abandoned break never claims another item's error.
        """
        with self._lock:
            token, prepared_at = self._first or (None, 0.0)
            item = self._items.get(token)
            if (item is None or item.live or item.finished or not item.requested
                    or self.clock() - prepared_at > within):
                return None
            self._first = None
            return token

    def forget_first(self):
        with self._lock:
            self._first = None

    def seeked(self, token):
        """An explicit seek ends tracking for that creative."""
        with self._lock:
            item = self._items.get(token)
        if item is not None:
            item.tainted = True

    def token_for(self, url):
        parts = urlsplit(url or '')
        match = re.fullmatch(r'/network-ads/([0-9a-f]{48})\.mp3', parts.path)
        if not match or '%s://%s' % (parts.scheme, parts.netloc) != self.base_url or parts.query:
            return None
        with self._lock:
            return match.group(1) if match.group(1) in self._items else None

    def _fire(self, item, event, position):
        if event in item.fired:
            return
        item.fired.add(event)
        urls = item.ad.impressions if event == 'impression' else tuple(
            url for name, url in item.ad.tracking if name == event)
        headers = self._headers()
        for url in urls:
            self._beacons.send({'token': item.token, 'station': item.station_uuid, 'event': event,
                                'position': round(position, 3),
                                'target': round(self._target(item, event), 3),
                                'duration': item.ad.duration_seconds},
                               expand_macros(url, playhead=position, asset=item.ad.media_url), headers)

    @staticmethod
    def _target(item, event):
        duration = item.ad.duration_seconds
        return {'impression': 0.0, 'start': 0.0, 'firstQuartile': duration * .25,
                'midpoint': duration * .5, 'thirdQuartile': duration * .75,
                'complete': duration}.get(event, 0.0)

    def observe(self, token, position, playing=True, paused=False):
        """Feed one Kodi player sample (seconds into the creative)."""
        with self._lock:
            item = self._items.get(token)
        if item is None or item.finished or not math.isfinite(position):
            return
        item.live = True
        now = self.clock()
        if not playing or paused:
            item.last_wall = now
            return
        if item.last_position >= 0:
            allowed = (now - item.last_wall) * 1.5 + 1.0
            if position - item.last_position > allowed or position < item.last_position - 1.0:
                item.tainted = True
        item.last_position, item.last_wall = position, now
        if item.tainted:
            return
        item.max_position = max(item.max_position, position)
        if not item.started and position > 0:
            item.started = True
            self._fire(item, 'impression', position)
            self._fire(item, 'start', position)
        if not item.started:
            return
        for event, fraction in _QUARTILES:
            if position >= item.ad.duration_seconds * fraction:
                self._fire(item, event, position)

    def finish(self, token, reason):
        """End one creative. Return the next URL to play when it completed."""
        with self._lock:
            item = self._items.get(token)
        if item is None or item.finished:
            return None
        item.finished = True
        item.data = b''  # Free the creative now; the chain metadata stays.
        natural = reason == 'ended'
        if (natural and item.started and not item.tainted
                and item.ad.duration_seconds - 1.5 <= item.max_position
                <= item.ad.duration_seconds * 1.1 + 1.5
                and {'firstQuartile', 'midpoint', 'thirdQuartile'} <= item.fired):
            self._fire(item, 'complete', item.max_position)
        if reason == 'error' and not item.started:
            self._fire_errors(item.ad.error_urls, 405, item.token, item.station_uuid)
        self._emit({'token': item.token, 'station': item.station_uuid, 'event': 'finished',
                    'reason': reason, 'position': round(item.max_position, 3),
                    'tainted': item.tainted})
        if reason not in ('ended', 'error'):
            return None  # A user stop or skip never starts anything else.
        nxt = item.next_url
        if _TOKEN.fullmatch(nxt or ''):
            with self._lock:
                following = self._items.get(nxt)
                if following is not None:
                    following.live = True
            return self.route(nxt)
        return nxt or None

    def close(self, wait=True):
        self._closing.set()
        self._beacons.close(wait)
        with self._lock:
            self._items.clear()
