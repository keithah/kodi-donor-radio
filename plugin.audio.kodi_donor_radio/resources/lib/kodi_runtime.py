# SPDX-License-Identifier: GPL-2.0-or-later
"""Kodi-only UI and service boundary; all media passes through the embedded relay."""
from contextlib import contextmanager
from dataclasses import asdict, replace
from datetime import datetime, timezone
import json
import hashlib
import math
import os
from pathlib import Path
import re
import tempfile
import threading
import time
from urllib.parse import parse_qs, urlencode, urlsplit

import xbmc
import xbmcaddon
import xbmcgui
import xbmcplugin
import xbmcvfs

from control import ControlClient, ServiceDescriptor
from ad_models import AdConfig
from ad_media import CreativeCache
from kodi_ad_player import KodiAdPlayback
from events import JsonEventSink
from http_client import Cancelled, HttpClient, read_bounded
from models import Station
from radio_browser import RadioBrowser
from radio_ui import (DirectoryCacheStore, FavoriteStore, HistoryStore, PlaceStore, RecentSearchStore, StationSummary,
                      coordinates, safe_favicon, station_identifier, stations_in_radius)
from relay import RelayServer
from sessions import SessionManager
from icy_marker_store import IcyMarkerStore
from playback_arbiter import PlaybackStartArbiter, profile_file_lock
from url_policy import UrlPolicy

STARTUP_TIMEOUT = 5.0
AD_CREATE_TIMEOUT = 30.0
ADAPTIVE_PREFLIGHT_TIMEOUT = 5.0
PAGE_SIZE = 50
GEO_SCAN_PAGES = 4
MAX_OFFSET = 10000000
_HEX48 = re.compile(r'^[0-9a-f]{48}$')
_HEX64 = re.compile(r'^[0-9a-f]{64}$')


def text(identifier):
    return xbmcaddon.Addon().getLocalizedString(identifier)


def profile_path():
    return Path(xbmcvfs.translatePath(xbmcaddon.Addon().getAddonInfo('profile')))


def _private_profile(profile):
    profile = Path(profile)
    profile.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(str(profile), 0o700)
    return profile


def _playback_arbiter():
    profile = _private_profile(profile_path())
    return PlaybackStartArbiter(profile / 'playback-start.lock')


@contextmanager
def _descriptor_lock(profile):
    """Serialize publish and compare/remove across Kodi's separate interpreters."""
    profile = _private_profile(profile)
    with profile_file_lock(profile / 'service.lock'):
        yield profile


def _descriptor(record):
    if (not isinstance(record, dict) or set(record) != {'port', 'instance_id', 'control_token'}
            or type(record['port']) is not int or not 1 <= record['port'] <= 65535
            or not isinstance(record['instance_id'], str) or not _HEX48.fullmatch(record['instance_id'])
            or not isinstance(record['control_token'], str) or not _HEX64.fullmatch(record['control_token'])):
        raise ValueError('Invalid service descriptor')
    return ServiceDescriptor(**record)


def read_descriptor(profile):
    with (Path(profile) / 'service.json').open('rb') as source:
        payload = source.read(2049)
    if len(payload) > 2048:
        raise ValueError('Invalid service descriptor')
    return _descriptor(json.loads(payload))


def publish_descriptor(profile, descriptor):
    _descriptor(asdict(descriptor))
    with _descriptor_lock(profile) as profile:
        fd, filename = tempfile.mkstemp(prefix='.service-', dir=str(profile))
        try:
            with os.fdopen(fd, 'w', encoding='utf-8') as output:
                json.dump(asdict(descriptor), output)
                output.flush()
                os.fsync(output.fileno())
            os.chmod(filename, 0o600)
            os.replace(filename, str(profile / 'service.json'))
        finally:
            if os.path.exists(filename):
                os.unlink(filename)


def remove_descriptor(profile, descriptor):
    with _descriptor_lock(profile) as profile:
        try:
            if read_descriptor(profile) == descriptor:
                (profile / 'service.json').unlink()
        except (OSError, ValueError):
            pass


def fixture_origin():
    """A numeric loopback host and explicit port are the only development exception."""
    addon = xbmcaddon.Addon()
    if not addon.getSettingBool('test_mode'):
        return None
    value = addon.getSetting('test_origin')
    parts = urlsplit(value)
    if (parts.scheme != 'http' or parts.hostname != '127.0.0.1'
            or parts.username is not None or parts.password is not None
            or parts.path or parts.query or parts.fragment or parts.port is None
            or not 1 <= parts.port <= 65535
            or value != 'http://127.0.0.1:%d' % parts.port):
        raise ValueError('Invalid fixture origin')
    return value


def runtime_policy():
    origin = fixture_origin()
    return UrlPolicy(test_origins={origin} if origin else frozenset())


def ad_settings():
    """Snapshot explicit fixture-only advertising choices for the next session."""
    addon = xbmcaddon.Addon()
    enabled = addon.getSettingBool('test_mode') and addon.getSettingBool('ad_enabled')
    preroll = enabled and addon.getSettingBool('ad_preroll')
    midroll = enabled and addon.getSettingBool('ad_midroll')
    if not (preroll or midroll):
        return AdConfig(False, False, False, '', '')
    fixture = fixture_origin()
    value = addon.getSetting('ad_vast_url')
    try:
        parts = urlsplit(value)
        valid = (parts.scheme == 'http' and parts.netloc == urlsplit(fixture).netloc
                 and parts.path.startswith('/') and parts.path != '/'
                 and not parts.username and not parts.password and not parts.fragment
                 and not any(ord(char) < 33 or ord(char) == 127 for char in value))
    except ValueError:
        valid = False
    if not valid:
        raise ValueError('Invalid VAST fixture URL')
    return AdConfig(True, bool(preroll), bool(midroll), value, fixture)


def event_sink(profile):
    return JsonEventSink(profile, xbmcaddon.Addon().getSettingBool('diagnostics'),
                         lambda line: xbmc.log(line, xbmc.LOGDEBUG))


def _error(identifier):
    xbmcgui.Dialog().ok(text(30000), text(identifier))


def _operation(function, monitor, cancel, cleanup=None):
    """Keep Kodi UI responsive; late create results are retired after cancellation."""
    progress = xbmcgui.DialogProgress()
    progress.create(text(30000), text(30013))
    done, lock, result = threading.Event(), threading.Lock(), {}
    def work():
        try:
            value = function()
            with lock:
                abandoned = cancel.is_set()
                if not abandoned:
                    result['value'] = value
            if abandoned and cleanup:
                cleanup(value)
        except Exception as error:
            with lock:
                result['error'] = error
        finally:
            done.set()
    thread = threading.Thread(target=work, daemon=True)
    thread.start()
    try:
        while True:
            if monitor.abortRequested() or progress.iscanceled():
                with lock:
                    cancel.set()
                    value = result.pop('value', None)
                if value is not None and cleanup:
                    cleanup(value)
                raise Cancelled('Request cancelled')
            if done.is_set():
                if 'error' in result:
                    raise result['error']
                return result.get('value')
            monitor.waitForAbort(.05)
    finally:
        progress.close()


def _service(profile, monitor, cancel, create_timeout=10):
    deadline = time.monotonic() + STARTUP_TIMEOUT
    while time.monotonic() < deadline:
        if monitor.abortRequested() or cancel.is_set():
            raise Cancelled('Request cancelled')
        try:
            descriptor = read_descriptor(profile)
            client = ControlClient(descriptor, timeout=min(.5, max(.01, deadline - time.monotonic())))
            health = client.health()
            if health == {'status': 'ok', 'instance_id': descriptor.instance_id}:
                return ControlClient(descriptor, timeout=create_timeout)
        except (OSError, ValueError):
            pass
        if monitor.waitForAbort(.1):
            raise Cancelled('Request cancelled')
    raise OSError('Service unavailable')


def _relay_path(value, port):
    try:
        parts = urlsplit(value)
        match = re.fullmatch(r'/media/([0-9a-f]{48})/[0-9a-f]{48}'
                             r'(?:\.(?:m3u8|ts|aac|mp3|mp4|m4s|m4a|ac3|ec3))?', parts.path)
        if (parts.scheme == 'http' and parts.netloc == '127.0.0.1:%d' % port
                and not parts.query and not parts.fragment and match):
            return match.group(1)
    except ValueError:
        pass
    return None


def _inputstream_version(addon_id):
    """Read an already enabled addon; never install or enable it."""
    try:
        raw = xbmc.executeJSONRPC(json.dumps({
            'jsonrpc': '2.0', 'id': 1, 'method': 'Addons.GetAddonDetails',
            'params': {'addonid': addon_id, 'properties': ['enabled', 'version']}}))
        if not isinstance(raw, str) or len(raw) > 16384:
            return None
        addon = json.loads(raw)['result']['addon']
        version = addon.get('version', '')
        match = re.fullmatch(r'([0-9]{1,3})\.[0-9]+\.[0-9]+(?:[.+-][a-zA-Z0-9.-]+)?', version)
        if addon.get('addonid') == addon_id and addon.get('enabled') is True and match is not None:
            return version
    except (AttributeError, KeyError, TypeError, ValueError, RuntimeError):
        pass
    return None


def _inputstream_enabled(addon_id, minimum_major):
    version = _inputstream_version(addon_id)
    return version is not None and int(version.split('.', 1)[0]) >= minimum_major


def _adaptive_enabled():
    return _inputstream_enabled('inputstream.adaptive', 21)


def _adaptive_ready(adaptive_url, native_url, descriptor, cancel):
    """Bound one exact-loopback master preflight before choosing Adaptive."""
    from hls import parse_playlist
    session_id = _relay_path(native_url, descriptor.port)
    if session_id is None or _relay_path(adaptive_url, descriptor.port) != session_id:
        return False
    relay_origin = 'http://127.0.0.1:%d' % descriptor.port
    try:
        client = HttpClient(UrlPolicy(test_origins={relay_origin}), max_redirects=0)
        deadline = time.monotonic() + ADAPTIVE_PREFLIGHT_TIMEOUT
        with client.open(adaptive_url, {}, cancel, deadline=deadline,
                         allowed_origin=relay_origin) as response:
            if (response.status != 200
                    or response.headers.get('content-type', '').split(';', 1)[0].strip().lower()
                    != 'application/vnd.apple.mpegurl'
                    or response.headers.get('content-encoding', 'identity').lower() != 'identity'):
                return False
            body = read_bounded(response, 4096)
        playlist = parse_playlist(body.decode('utf-8-sig'), adaptive_url)
        variants = [line for line in playlist.lines if line.startswith('#EXT-X-STREAM-INF:')]
        return (playlist.is_master and len(variants) == 1
                and variants[0].rstrip('\r\n').endswith(',CODECS="mp4a.40.2"')
                and len(playlist.uri_references) == 1
                and playlist.uri_references[0].url == native_url)
    except Cancelled:
        raise  # A user cancellation must stop the session, never start fallback.
    except (OSError, ValueError):
        return False


def _station(value):
    record = json.loads(value)
    if (not isinstance(record, dict) or set(record) != {'uuid','name','url','codec','hls'}
            or any(not isinstance(record[key], str) or not record[key] for key in ('uuid','name','url'))
            or not isinstance(record['codec'], str) or type(record['hls']) is not bool):
        raise ValueError('Invalid station')
    return Station(**record)


def _label_icy_marker(monitor, cancel):
    """Keep the displayed candidate identity through both listener decisions."""
    try:
        client = _operation(lambda: _service(profile_path(), monitor, cancel), monitor, cancel)
        candidate = _operation(client.current_icy_candidate, monitor, cancel)
        if candidate is None:
            _error(30056)
            return
        dialog = xbmcgui.Dialog()
        choice = dialog.select(text(30051) + ' ' + candidate['marker'],
                               [text(30052), text(30053), text(30054)])
        if choice not in (0, 1):
            return
        memory_choice = dialog.select(text(30055),
                                      [text(30059), text(30060), text(30054)])
        if memory_choice not in (0, 1):
            return
        remember = memory_choice == 0
        _operation(lambda: client.label_icy_marker(candidate['session_id'], candidate['marker'],
                                                   ('start', 'end')[choice], remember), monitor, cancel)
        dialog.ok(text(30000), text(30058))
    except Cancelled:
        pass
    except (OSError, ValueError, KeyError, TypeError):
        _error(30057)


# Fixture identity and paths are application constants, never navigation payloads.
_FIXTURES = (
    (30020, 'continuous.mp3', 'MP3', False), (30021, 'continuous.aac', 'AAC', False),
    (30022, 'icy.mp3', 'MP3', False), (30023, 'hls/normal/index.m3u8', 'AAC', True),
    (30024, 'hls/mid-break/index.m3u8', 'AAC', True),
    (30025, 'hls/missing-end/index.m3u8', 'AAC', True),
    (30026, 'hls/no-cue/index.m3u8', 'AAC', True),
    (30027, 'hls/malformed-cue/index.m3u8', 'AAC', True),
    (30045, 'source/normal/index.m3u8', 'AAC', True),
    (30046, 'source/mutation/index.m3u8', 'AAC', True),
    (30047, 'source/expired-window/index.m3u8', 'AAC', True),
    (30048, 'source/stall/index.m3u8', 'AAC', True),
    (30049, 'source/finite/index.m3u8', 'AAC', True),
    (30172, 'source/extended/index.m3u8', 'AAC', True),
    (30174, 'source/late-break/index.m3u8', 'AAC', True),
    (30175, 'source/recovery/index.m3u8', 'AAC', True),
    (30061, 'icy/explicit.mp3', 'MP3', False), (30062, 'icy/tildeats.mp3', 'MP3', False),
    (30063, 'icy/wrapped.mp3', 'MP3', False), (30064, 'icy/return-to-track.mp3', 'MP3', False),
    (30065, 'icy/paired-marker.mp3', 'MP3', False), (30066, 'icy/punctuation.mp3', 'MP3', False),
    (30067, 'icy/unresolved.mp3', 'MP3', False),
)


def _fixture_station(identifier):
    origin = fixture_origin()
    if origin is None:
        raise ValueError('Test mode disabled')
    for label, path, codec, hls in _FIXTURES:
        if identifier == 'fixture-%d' % label:
            return Station(identifier, text(label), origin + '/' + path, codec, hls)
    raise ValueError('Unknown fixture')


def _query_text(value):
    if (not isinstance(value, str) or len(value) > 256 or
            any(ord(char) < 32 or ord(char) == 127 for char in value)):
        raise ValueError('Invalid query')
    return value


_SEARCH_PROPERTY = 'kodi_donor_radio.search_pages'
_SEARCH_BYTES = 512 * 1024
_FAVORITES_REFRESH_PROPERTY = 'kodi_donor_radio.favorites_refresh'
_VISIBLE_ROUTE_PROPERTY = 'kodi_donor_radio.visible_route'


def _visible_route(action):
    try:
        xbmcgui.Window(10000).setProperty(_VISIBLE_ROUTE_PROPERTY, action)
    except (OSError, RuntimeError, ValueError, TypeError):
        pass


def _schedule_favorite_refresh(base):
    """Called only after the local directory has completed; no network work here."""
    window = None
    try:
        window = xbmcgui.Window(10000)
        if window.getProperty(_FAVORITES_REFRESH_PROPERTY):
            return
        window.setProperty(_FAVORITES_REFRESH_PROPERTY, 'true')
        # Kodi 21.3 RunPlugin executes a non-directory plugin with handle -1.
        # executebuiltin defaults to wait=False; no worker/progress dialog is needed.
        xbmc.executebuiltin('RunPlugin(%s?mode=favorites_refresh)' % base)
    except (OSError, RuntimeError, ValueError, TypeError):
        if window is not None:
            try:
                window.clearProperty(_FAVORITES_REFRESH_PROPERTY)
            except (OSError, RuntimeError, ValueError, TypeError):
                pass


def _refresh_favorites(browser, monitor, base):
    """Quiet background action: fetch every batch before one profile transaction."""
    try:
        store = FavoriteStore(profile_path())
        favorites = store.list()
        summaries = []
        for offset in range(0, len(favorites), PAGE_SIZE):
            if monitor.abortRequested():
                raise Cancelled('Refresh cancelled')
            ids = [station.station_uuid for station in favorites[offset:offset + PAGE_SIZE]]
            summaries.extend(_summary(station) for station in browser.by_uuids(ids))
        if monitor.abortRequested():
            raise Cancelled('Refresh cancelled')
        store.refresh(summaries)
    except (OSError, RuntimeError, ValueError, TypeError, RecursionError):
        try:
            xbmcgui.Window(10000).clearProperty(_FAVORITES_REFRESH_PROPERTY)
        except (OSError, RuntimeError, ValueError, TypeError):
            pass
        return
    try:
        # The session route covers navigation inside this addon. FolderPath also
        # protects other addons/windows, which do not invoke our route tracker.
        if xbmcgui.Window(10000).getProperty(_VISIBLE_ROUTE_PROPERTY) != 'favorites':
            return
        current = urlsplit(xbmc.getInfoLabel('Container.FolderPath'))
        expected = urlsplit(base)
        params = parse_qs(current.query)
        if (current.scheme == expected.scheme and current.netloc == expected.netloc and
                current.path.rstrip('/') == expected.path.rstrip('/') and
                params.get('mode', params.get('action')) == ['favorites']):
            xbmc.executebuiltin('Container.Refresh')
    except (OSError, RuntimeError, ValueError, TypeError):
        pass


def _cache_key(*values):
    return json.dumps(values, ensure_ascii=False, allow_nan=False, separators=(',', ':'))


def _summary(station):
    return (StationSummary.from_station(replace(station, favicon=safe_favicon(station.favicon)))
            if isinstance(station, Station) else station)


def _directory_page(kind, key, fetch, monitor, cancel):
    """Optional profile cache: failed storage never replaces a usable live page."""
    _check_ui_cancel(monitor, cancel)
    ttl = (7 if kind in ('countries', 'tags') else 1) * 86400
    cached = None
    try:
        cached = _operation(lambda: DirectoryCacheStore(profile_path()).get(
            kind, key, ttl, allow_stale=True), monitor, cancel)
    except Cancelled:
        raise
    except (OSError, ValueError, TypeError, RecursionError):
        pass
    _check_ui_cancel(monitor, cancel)
    if cached is not None and cached.fresh:
        return cached.rows, cached.continuation
    try:
        rows, continuation = _operation(fetch, monitor, cancel)
    except Cancelled:
        raise
    except (OSError, ValueError, TypeError, RecursionError):
        if cached is not None:
            return cached.rows, cached.continuation
        raise
    _check_ui_cancel(monitor, cancel)
    if kind in ('place', 'map'):
        rows = [_summary(row) for row in rows]
    elif kind == 'tags':
        valid = []
        for row in rows:
            try:
                tag = _query_text(row)
                if tag.strip():
                    valid.append(tag)
            except ValueError:
                continue
        rows = valid
    try:
        _operation(lambda: DirectoryCacheStore(profile_path()).put(
            kind, key, rows, continuation=continuation), monitor, cancel)
    except Cancelled:
        raise
    except (OSError, ValueError, TypeError, RecursionError):
        pass
    return rows, continuation


def _search_page(key, fetch, monitor, cancel):
    """Kodi session memory only; validate and bound the cross-invocation payload."""
    window, raw = None, ''
    try:
        window = xbmcgui.Window(10000)
        raw = window.getProperty(_SEARCH_PROPERTY)
    except (OSError, RuntimeError, ValueError, TypeError):
        pass

    def save_property(value):
        if window is None:
            return
        try:
            if value is None:
                window.clearProperty(_SEARCH_PROPERTY)
            else:
                window.setProperty(_SEARCH_PROPERTY, value)
        except (OSError, RuntimeError, ValueError, TypeError):
            pass  # Optional session storage cannot hide a successful directory fetch.

    pages = []
    try:
        if raw:
            if len(raw.encode('utf-8')) > _SEARCH_BYTES:
                raise ValueError('Search cache too large')
            document = json.loads(raw)
            if (not isinstance(document, dict) or set(document) != {'version', 'pages'} or
                    type(document['version']) is not int or document['version'] != 2 or
                    not isinstance(document['pages'], list) or len(document['pages']) > 64):
                raise ValueError('Invalid search cache')
            identities = set()
            for page in document['pages']:
                if (not isinstance(page, dict) or set(page) != {'key', 'rows', 'more', 'stored_at'} or
                        not isinstance(page['key'], str) or not _HEX64.fullmatch(page['key']) or
                        page['key'] in identities or type(page['more']) is not bool or
                        not isinstance(page['rows'], list) or len(page['rows']) > PAGE_SIZE):
                    raise ValueError('Invalid search page')
                stored_at = page['stored_at']
                if (type(stored_at) is int and stored_at < 0 or
                        type(stored_at) is float and (not math.isfinite(stored_at) or stored_at < 0) or
                        type(stored_at) not in (int, float)):
                    raise ValueError('Invalid search timestamp')
                for row in page['rows']:
                    StationSummary.from_dict(row)
                identities.add(page['key'])
                pages.append(page)
    except (ValueError, TypeError, UnicodeError, RecursionError):
        save_property(None)
        pages = []
    identity = hashlib.sha256(key.encode('utf-8')).hexdigest()
    _check_ui_cancel(monitor, cancel)
    match = next((page for page in pages if page['key'] == identity), None)
    now = time.time()
    if match is not None and 0 <= now - match['stored_at'] < 86400:
        return [StationSummary.from_dict(row) for row in match['rows']], match['more']
    try:
        rows, more = _operation(fetch, monitor, cancel)
    except Cancelled:
        raise
    except (OSError, ValueError, TypeError, RecursionError):
        if match is None:
            raise
        return [StationSummary.from_dict(row) for row in match['rows']], match['more']
    _check_ui_cancel(monitor, cancel)
    rows = [_summary(row) for row in rows]
    pages = [page for page in pages if page['key'] != identity]
    pages.append({'key': identity, 'rows': [row.to_dict() for row in rows], 'more': more,
                  'stored_at': now})
    pages = pages[-64:]
    while pages:
        payload = json.dumps({'version': 2, 'pages': pages}, ensure_ascii=False, allow_nan=False,
                             separators=(',', ':'))
        if len(payload.encode('utf-8')) <= _SEARCH_BYTES:
            save_property(payload)
            break
        pages.pop(0)
    if not pages:
        save_property(None)
    return rows, more


def _place_stations(browser, place, offset, cancel):
    """Stop on the first populated page or after four pages; never skip matching rows."""
    cursor = offset
    for _ in range(GEO_SCAN_PAGES):
        if cancel.is_set():
            raise Cancelled('Request cancelled')
        stations, more = browser.geographic(place.country_code, cursor, PAGE_SIZE)
        cursor += PAGE_SIZE
        matches = stations_in_radius(stations, place)
        matches.sort(key=lambda station: (-station.click_count, station.name.casefold(), station.uuid))
        if matches or not more:
            return matches, cursor if more and cursor <= MAX_OFFSET else None
        if cursor > MAX_OFFSET:
            break
    return [], cursor if cursor <= MAX_OFFSET else None


_RADIUS_MILES = (10, 25, 60, 100, 250)
_ACTION_ROUTES = frozenset(('setup', 'map', 'station_info', 'vote', 'settings', 'label_icy_marker',
                            'favorite_add', 'favorite_remove', 'place_default',
                            'place_radius', 'place_remove'))


def _real_station_uuid(value):
    value = station_identifier(value)
    if value.startswith('fixture-'):
        raise ValueError('Fixture requires explicit fixture route')
    return value


def _check_ui_cancel(monitor, cancel):
    if monitor.abortRequested() or cancel.is_set():
        raise Cancelled('Request cancelled')


def _dialog_choice(dialog, heading, options, monitor, cancel):
    _check_ui_cancel(monitor, cancel)
    choice = dialog.select(text(heading), options)
    _check_ui_cancel(monitor, cancel)
    if type(choice) is not int or not 0 <= choice < len(options):
        raise Cancelled('Dialog cancelled')
    return choice


def _choose_radius(dialog, monitor, cancel):
    choice = _dialog_choice(dialog, 30135, ['%d %s' % (radius, text(30136))
                            for radius in _RADIUS_MILES], monitor, cancel)
    # The user chooses miles; stores and distance filtering use kilometers.
    return _RADIUS_MILES[choice] * 1.609344


def _setup_candidates(browser, country_code, query, cancel):
    """Bounded station-derived places using only fields the directory supplies.

    City is optional/nonstandard in Radio Browser records. If absent, expose the
    station's state as an area, or its coordinates as a station area; never label
    either as a verified city.
    """
    candidates = {}
    for page in range(GEO_SCAN_PAGES):
        if cancel.is_set():
            raise Cancelled('Request cancelled')
        stations, more = browser.geographic(country_code, page * PAGE_SIZE, PAGE_SIZE)
        for station in stations[:PAGE_SIZE]:
            try:
                latitude, longitude = coordinates(station.latitude, station.longitude)
                city = _query_text(station.city).strip()
                state = _query_text(station.state).strip()
                if city:
                    name = city
                    source_name = city
                elif state:
                    name = '%s · %s' % (text(30153), state)
                    source_name = state
                else:
                    name = '%s · %.3f, %.3f' % (text(30153), latitude, longitude)
                    source_name = name
                if len(name) > 128 or (query and query.casefold() not in source_name.casefold()):
                    continue
                code = station.country_code
                if code and not re.fullmatch(r'[A-Z]{2}', code):
                    continue
                if country_code and code != country_code:
                    continue
                # Nearby stations in one city should not fill the place picker
                # with coordinate variants; preserve distant same-name places.
                key = (name.casefold(), code, round(latitude, 1), round(longitude, 1))
                candidates.setdefault(key, dict(name=name, latitude=latitude, longitude=longitude,
                                                country_code=code, country=_query_text(station.country)))
            except (ValueError, TypeError):
                continue
        if not more:
            break
    return [candidates[key] for key in sorted(candidates)]


def _guided_setup(browser, monitor, cancel):
    dialog, pending = xbmcgui.Dialog(), []
    store = PlaceStore(profile_path())
    available = store.limit - len(store.list())
    if available <= 0:
        _error(30140)
        return
    while len(pending) < available:
        source = _dialog_choice(dialog, 30130, [text(30131), text(30132)], monitor, cancel)
        countries = _operation(browser.country_entries, monitor, cancel)
        if not countries:
            _error(30012)
            return
        choice = _dialog_choice(dialog, 30002, [name for name, _ in countries], monitor, cancel)
        country_code = countries[choice][1]
        if not re.fullmatch(r'[A-Z]{2}', country_code):
            raise ValueError('Invalid country code')
        query = ''
        if source == 1:
            query = _query_text(dialog.input(text(30133))).strip()
            _check_ui_cancel(monitor, cancel)
            if not query:
                raise Cancelled('Dialog cancelled')
        candidates = _operation(lambda: _setup_candidates(browser, country_code, query, cancel), monitor, cancel)
        if not candidates:
            _error(30139)
            if not pending:
                return
            continue
        choice = _dialog_choice(dialog, 30134, [
            '%s%s (%.3f, %.3f)' % (c['name'], ' · ' + c['country'] if c['country'] else '',
                                   c['latitude'], c['longitude']) for c in candidates], monitor, cancel)
        candidate = candidates[choice]
        pending.append({key: candidate[key] for key in ('name', 'latitude', 'longitude', 'country_code')})
        pending[-1]['radius_km'] = _choose_radius(dialog, monitor, cancel)
        options = [text(30137), text(30138)] if len(pending) < available else [text(30138)]
        decision = _dialog_choice(dialog, 30110, options, monitor, cancel)
        if len(options) == 1 or decision == 1:
            _check_ui_cancel(monitor, cancel)
            store.save_many(pending)
            xbmc.executebuiltin('Container.Refresh')
            return


def _lookup_summary(browser, station_uuid, monitor, cancel):
    station = _operation(lambda: browser.by_uuid(station_uuid), monitor, cancel)
    if station.uuid != station_uuid:
        raise ValueError('Invalid station lookup')
    return StationSummary.from_station(replace(station, favicon=safe_favicon(station.favicon)))


def _station_info(browser, params, monitor, cancel):
    station_uuid = _real_station_uuid(params.get('uuid', ''))
    try:
        favorites = FavoriteStore(profile_path()).list()
    except (OSError, ValueError, TypeError):
        favorites = []
    summary = next((station for station in favorites
                    if station.station_uuid == station_uuid), None)
    if summary is None:
        summary = _lookup_summary(browser, station_uuid, monitor, cancel)
    details = [summary.name, summary.station_uuid]
    details.extend(value for value in (summary.city, summary.country, summary.codec, summary.tags) if value)
    if summary.bitrate:
        details.append('%d %s' % (summary.bitrate, text(30125)))
    if summary.latitude is not None and summary.longitude is not None:
        details.append('%.3f, %.3f' % (summary.latitude, summary.longitude))
    xbmcgui.Dialog().ok(text(30124), '\n'.join(details))


def _vote(browser, params, monitor, cancel):
    station_uuid = _real_station_uuid(params.get('uuid', ''))
    dialog = xbmcgui.Dialog()
    if not dialog.yesno(text(30168), text(30169)):
        return
    accepted = _operation(lambda: browser.vote(station_uuid), monitor, cancel)
    dialog.ok(text(30168), text(30170 if accepted else 30171))


def _profile_action(mode, params, browser, monitor, cancel):
    """Mutations leave the current directory intact until an explicit refresh."""
    _check_ui_cancel(monitor, cancel)
    if mode in ('favorite_add', 'favorite_remove'):
        station_uuid = _real_station_uuid(params.get('uuid', ''))
        store = FavoriteStore(profile_path())
        if mode == 'favorite_add':
            summary = _lookup_summary(browser, station_uuid, monitor, cancel)
            _check_ui_cancel(monitor, cancel)
            store.add(summary)
        else:
            store.remove(station_uuid)
    else:
        place_id = station_identifier(params.get('id', ''))
        store = PlaceStore(profile_path())
        place = next((p for p in store.list() if p.place_id == place_id), None)
        if place is None:
            raise ValueError('Unknown place')
        if mode == 'place_default':
            store.set_default(place_id)
        elif mode == 'place_radius':
            radius = _choose_radius(xbmcgui.Dialog(), monitor, cancel)
            store.set_radius(place_id, radius)
        elif mode == 'place_remove':
            if not xbmcgui.Dialog().yesno(text(30128), text(30129) + '\n' + place.name):
                return
            _check_ui_cancel(monitor, cancel)
            store.remove(place_id)
    xbmc.executebuiltin('Container.Refresh')


def _map_view(place=None, station=None):
    from radio_map import MapView
    center_lat = getattr(station, 'latitude', None) if station is not None else None
    center_lon = getattr(station, 'longitude', None) if station is not None else None
    if (center_lat is None or center_lon is None) and place is not None:
        center_lat, center_lon = place.latitude, place.longitude
    if center_lat is None or center_lon is None:
        return MapView(90, -90, -180, 180, 1000, 650)
    radius_km = getattr(place, 'radius_km', 120) if place is not None else 120
    lat_span = max(0.5, min(45, radius_km / 80.0))
    lon_span = max(0.5, min(90, lat_span /
                            max(.2, abs(math.cos(math.radians(center_lat))))))
    north, south = min(90, center_lat + lat_span), max(-90, center_lat - lat_span)
    west, east = center_lon - lon_span, center_lon + lon_span
    while west < -180: west += 360
    while west > 180: west -= 360
    while east < -180: east += 360
    while east > 180: east -= 360
    if west == east:
        east = west + 1
        if east > 180: east = -180
    return MapView(north, south, west, east, 1000, 650)


def _map_stations(browser, country_code, selected_station, cancel):
    stations, offset = [], 0
    for _ in range(GEO_SCAN_PAGES):
        if cancel.is_set():
            raise Cancelled('Request cancelled')
        page, more = browser.geographic(country_code, offset, PAGE_SIZE)
        stations.extend(page[:PAGE_SIZE])
        offset += PAGE_SIZE
        if not more:
            break
    limit = GEO_SCAN_PAGES * PAGE_SIZE
    selected_uuid = (getattr(selected_station, 'uuid', getattr(selected_station, 'station_uuid', ''))
                     if selected_station is not None else '')
    if selected_station is not None and all(s.uuid != selected_uuid for s in stations):
        # The context action must remain visible even when the bounded scan is
        # full, so reserve one slot for it and keep the earliest scan results.
        stations = [selected_station] + stations[:limit - 1]
    return stations[:limit]


def _open_map(params, monitor, cancel, base):
    selected_uuid = _real_station_uuid(params['uuid']) if params.get('uuid') else ''
    from kodi_radio_map import RadioMapWindow
    from radio_map import MapView
    browser = RadioBrowser(cancel=cancel)
    unavailable = ''
    selected = None
    if selected_uuid:
        try:
            selected = _operation(lambda: browser.by_uuid(selected_uuid), monitor, cancel)
        except Cancelled:
            raise
        except (OSError, ValueError):
            unavailable = xbmcaddon.Addon().getLocalizedString(30164)
            try:
                favorites = FavoriteStore(profile_path()).list()
            except (OSError, ValueError, TypeError):
                favorites = []
            selected = next((station for station in favorites
                             if station.station_uuid == selected_uuid), None)
    try:
        places = PlaceStore(profile_path()).list()
    except (OSError, ValueError, TypeError):
        places = []
    place = next((candidate for candidate in places if candidate.is_default),
                 places[0] if places else None)
    country_code = (selected.country_code if selected is not None else
                    place.country_code if place is not None else '')
    if selected is not None and selected.latitude is not None:
        view = _map_view(station=selected)
    elif place is not None:
        view = _map_view(place=place)
    else:
        view = MapView(90, -90, -180, 180, 1000, 650)
    try:
        key = _cache_key(country_code, *(float(value) + 0.0 for value in
                         (view.north, view.south, view.west, view.east)), view.width, view.height)
        stations, _ = _directory_page('map', key,
            lambda: (_map_stations(browser, country_code, None, cancel), None), monitor, cancel)
    except Cancelled:
        raise
    except (OSError, ValueError, TypeError):
        stations = []
        discovery_error = xbmcaddon.Addon().getLocalizedString(30163)
        unavailable = unavailable + ' · ' + discovery_error if unavailable else discovery_error
    stations = list(stations)
    if selected is not None:
        selected_id = _summary(selected).station_uuid
        if all(_summary(station).station_uuid != selected_id for station in stations):
            stations = [selected] + list(stations[:GEO_SCAN_PAGES * PAGE_SIZE - 1])

    clients, clients_lock = {}, threading.Lock()
    def start_preview(station_uuid, request_cancel):
        if cancel.is_set() or request_cancel.is_set():
            raise Cancelled('Request cancelled')
        known_mirrors = browser.mirror_urls
        preview_browser = RadioBrowser(
            cancel=request_cancel,
            mirror_lookup=(lambda: list(known_mirrors)) if known_mirrors else None)
        station = preview_browser.by_uuid(_real_station_uuid(station_uuid))
        if request_cancel.is_set():
            raise Cancelled('Request cancelled')
        descriptor = read_descriptor(profile_path())
        client = ControlClient(descriptor, timeout=10)
        # Service-side create is non-destructive and refuses if a deliberate
        # or pending session exists; Player guards alone are too late.
        session = client.create_preview(station)
        if not isinstance(session, dict):
            raise ValueError('Invalid preview response')
        session_id = _relay_path(session.get('playback_url', ''), descriptor.port)
        if not session_id or session.get('id') != session_id:
            raise ValueError('Invalid preview response')
        mime_type = session.get('mime_type', 'application/octet-stream')
        if (not isinstance(mime_type, str) or len(mime_type) > 128
                or not re.fullmatch(r'[a-z0-9!#$&^_.+-]+/[a-z0-9!#$&^_.+-]+', mime_type)):
            try: client.stop(session_id)
            except (OSError, ValueError): pass
            raise ValueError('Invalid preview MIME type')
        if request_cancel.is_set():
            try: client.stop(session_id)
            except (OSError, ValueError): pass
            raise Cancelled('Request cancelled')
        with clients_lock:
            clients[session_id] = client
        return {'session_id': session_id, 'playback_url': session['playback_url'],
                'mime_type': mime_type}

    def stop_preview(session_id):
        with clients_lock:
            client = clients.pop(session_id, None)
        if client is not None:
            try: client.stop(session_id)
            except (OSError, ValueError): pass

    window = RadioMapWindow(
        'map.xml', xbmcaddon.Addon().getAddonInfo('path'), 'Default', '1080i', False,
        origin_route=params.get('origin', base + '?' + urlencode({'mode': 'home'})),
        selected_uuid=selected_uuid, stations=stations, view=view,
        preview_enabled=xbmcaddon.Addon().getSettingBool('preview_on_browse'),
        addon_path=xbmcaddon.Addon().getAddonInfo('path'), plugin_base=base,
        profile_path=profile_path(),
        geometry_path=(Path(xbmcaddon.Addon().getAddonInfo('path')) /
                       'resources' / 'map' / 'natural_earth_110m_land.json'),
        preview_start=start_preview, preview_stop=stop_preview,
        playback_arbiter=_playback_arbiter(),
        status_message=unavailable,
    )
    try:
        window.doModal()
    finally:
        # Native dismissal/abort can return without invoking our close override.
        window.cleanup()


def _ui_transition(mode, params, monitor, cancel, base='plugin://plugin.audio.kodi_donor_radio'):
    """Native action routes and the seam used by the shared map window."""
    if mode == 'setup':
        _guided_setup(RadioBrowser(cancel=cancel), monitor, cancel)
    elif mode == 'station_info':
        _station_info(RadioBrowser(cancel=cancel), params, monitor, cancel)
    elif mode == 'vote':
        _vote(RadioBrowser(cancel=cancel), params, monitor, cancel)
    elif mode == 'map':
        _open_map(params, monitor, cancel, base)


def run_plugin(argv):
    base, handle = argv[0], int(argv[1])
    params = {key: values[0] for key, values in parse_qs(argv[2].lstrip('?') if len(argv)>2 else '').items()}
    action = params.get('mode', params.get('action', 'home'))
    monitor, cancel = xbmc.Monitor(), threading.Event()
    browser = RadioBrowser(cancel=cancel)
    if action == 'favorites_refresh':
        _refresh_favorites(browser, monitor, base)
        return
    if (action in ('map', 'setup', 'settings') or
            handle >= 0 and action not in ('play', 'play_fixture', 'favorite_add', 'favorite_remove',
                                          'place_default', 'place_radius', 'place_remove',
                                          'station_info', 'vote', 'label_icy_marker')):
        _visible_route(action)
    def url(**values): return base + '?' + urlencode(values)
    def item(label, folder=True, playable=False, context=(), **values):
        entry = xbmcgui.ListItem(label=label)
        if context:
            entry.addContextMenuItems([(text(label_id), 'RunPlugin(%s)' % url(**route))
                                       for label_id, route in context])
        if playable:
            entry.setProperty('IsPlayable', 'true')
        xbmcplugin.addDirectoryItem(handle, url(**values), entry, isFolder=folder)

    def originating_route():
        fields = {
            'place': ('id', 'offset'), 'favorites': ('offset',),
            'tag': ('tag', 'offset'), 'country': ('code', 'offset'),
            'search': ('name', 'country', 'tag', 'offset'),
            'stations': ('name', 'country', 'tag', 'offset'),
        }
        route_mode = action if action in fields else 'home'
        values = {'mode': route_mode}
        for key in fields.get(route_mode, ()):
            if key in params:
                values[key] = params[key]
        return url(**values)

    favorite_ids = None
    def station_item(station):
        nonlocal favorite_ids
        summary = (StationSummary.from_station(replace(station, favicon=safe_favicon(station.favicon)))
                   if isinstance(station, Station) else station)
        entry = xbmcgui.ListItem(label=summary.name)
        details = ' '.join(value for value in (summary.codec, str(summary.bitrate) if summary.bitrate else '') if value)
        entry.setLabel2(' · '.join(value for value in (summary.city, details) if value))
        # Kodi fetches ListItem artwork itself, outside UrlPolicy and redirect
        # checks. Until remote artwork is fetched through a validated cache,
        # use only the bundled monogram and never hand Kodi directory URLs.
        art = os.path.join(xbmcaddon.Addon().getAddonInfo('path'),
                           'resources', 'station-monogram.png')
        entry.setArt({'thumb': art, 'icon': art})
        music_info = entry.getMusicInfoTag()
        music_info.setTitle(summary.name)
        music_info.setGenres([tag.strip() for tag in summary.tags.split(',') if tag.strip()])
        music_info.setComment(' · '.join(value for value in (summary.city, summary.country, summary.tags) if value))
        entry.setProperty('IsPlayable', 'true')
        entry.setIsFolder(False)
        if favorite_ids is None:
            try:
                favorite_ids = {s.station_uuid for s in FavoriteStore(profile_path()).list()}
            except (OSError, ValueError, TypeError):
                favorite_ids = set()
        favorite = summary.station_uuid in favorite_ids
        entry.addContextMenuItems([(text(label), 'RunPlugin(%s)' % url(
            mode=target, uuid=summary.station_uuid,
            **({'origin': originating_route()} if target == 'map' else {})))
            for label, target in ((30120, 'play'),
                (30122 if favorite else 30121, 'favorite_remove' if favorite else 'favorite_add'),
                (30123, 'map'), (30124, 'station_info'), (30168, 'vote'))])
        xbmcplugin.addDirectoryItem(handle, url(mode='play', uuid=summary.station_uuid), entry, isFolder=False)
    if action == 'launch_fixture':
        client, nonce = None, None
        def cancel_launch(value):
            if client is not None and isinstance(value, str) and _HEX48.fullmatch(value):
                try: client.cancel_fixture_launch(value)
                except (OSError, ValueError): pass
        try:
            station = _fixture_station(params.get('id', ''))
            if (not station.hls or not ad_settings().enabled
                    or _inputstream_version('inputstream.ffmpegdirect') != '22.2.10'):
                raise ValueError('Qualified fixture playback unavailable')
            client = _operation(lambda: _service(profile_path(), monitor, cancel), monitor, cancel)
            nonce = _operation(lambda: client.reserve_fixture_launch(station.uuid),
                               monitor, cancel, cancel_launch)
            if not isinstance(nonce, str) or not _HEX48.fullmatch(nonce):
                raise ValueError('Invalid fixture launch')
            # A named core survives plugin resolution. The inputstream-player
            # default alone can be overridden by user player-selection rules.
            request = json.dumps({'jsonrpc': '2.0', 'id': 1, 'method': 'Player.Open',
                'params': {'item': {'file': url(mode='play_fixture', id=station.uuid,
                                              launch_nonce=nonce)},
                           'options': {'playername': 'VideoPlayer', 'resume': False}}})
            if cancel.is_set() or monitor.abortRequested():
                raise Cancelled('Request cancelled')
            response = None
            def start_fixture():
                nonlocal response
                if cancel.is_set() or monitor.abortRequested():
                    raise Cancelled('Request cancelled')
                response = xbmc.executeJSONRPC(request)
            _playback_arbiter().start(start_fixture)
            if not isinstance(response, str) or len(response) > 16384:
                raise ValueError('Invalid playback response')
            result = json.loads(response)
            if not isinstance(result, dict) or result.get('result') != 'OK' or 'error' in result:
                raise ValueError('Fixture playback rejected')
        except (OSError, ValueError, KeyError, TypeError, RuntimeError):
            cancel_launch(nonce)
            if not cancel.is_set(): _error(30011)
        return
    if action in ('play', 'play_fixture'):
        client, session = None, None
        def stop(value):
            if client is not None and isinstance(value, dict) and _HEX48.fullmatch(str(value.get('id',''))):
                try: client.stop(value['id'])
                except (OSError, ValueError): pass
        try:
            launch_nonce = params.get('launch_nonce')
            if launch_nonce is not None and (action != 'play_fixture' or handle < 0
                    or not _HEX48.fullmatch(launch_nonce)):
                raise ValueError('Invalid fixture launch')
            if action == 'play_fixture':
                station = _fixture_station(params.get('id', ''))
            else:
                uuid = _real_station_uuid(params.get('uuid', ''))
                station = _operation(lambda: browser.by_uuid(uuid), monitor, cancel)
                if station.uuid != uuid:
                    raise ValueError('Invalid station lookup')
            try:
                # The service can still hold its previous valid ad config while
                # Kodi settings are being edited, so use the longer bound for all
                # controlled HLS fixtures regardless of the plugin's snapshot.
                ad_create = station.uuid.startswith('fixture-') and station.hls
                client = _operation(lambda: _service(profile_path(), monitor, cancel,
                    AD_CREATE_TIMEOUT if ad_create else 10), monitor, cancel)
            except OSError as error:
                if isinstance(error, Cancelled): raise
                _error(30010)
                if handle >= 0:
                    xbmcplugin.setResolvedUrl(handle, False, xbmcgui.ListItem())
                return
            session = _operation(lambda: client.create(station, fixture_launch_nonce=launch_nonce)
                                 if launch_nonce is not None else client.create(station),
                                 monitor, cancel, stop)
            if not isinstance(session, dict):
                raise ValueError('Invalid relay response')
            session_id = _relay_path(session.get('playback_url',''), client.descriptor.port)
            if session_id is None or session.get('id') != session_id:
                raise ValueError('Invalid relay response')
            mime = session.get('mime_type', 'application/octet-stream')
            if (not isinstance(mime, str) or len(mime) > 128
                    or not re.fullmatch(r'[a-z0-9!#$&^_.+-]+/[a-z0-9!#$&^_.+-]+', mime)):
                raise ValueError('Invalid relay MIME type')
            playback_url = session['playback_url']
            adaptive_url = session.get('adaptive_playback_url')
            continuous_url = session.get('continuous_playback_url')
            if continuous_url is not None and (
                    action != 'play_fixture' or mime != 'application/vnd.apple.mpegurl'
                    or not playback_url.endswith('.m3u8')
                    or continuous_url != playback_url[:-5] + '.aac'):
                raise ValueError('Invalid continuous relay response')
            adaptive = buffered = False
            if adaptive_url is not None:
                if (action != 'play_fixture' or mime != 'application/vnd.apple.mpegurl'
                        or not isinstance(adaptive_url, str) or not adaptive_url.endswith('.m3u8')
                        or _relay_path(adaptive_url, client.descriptor.port) != session_id):
                    raise ValueError('Invalid Adaptive relay response')
                buffered = (continuous_url is not None and
                            _inputstream_enabled('inputstream.ffmpegdirect', 22))
                adaptive = not buffered and _adaptive_enabled()
                if buffered or adaptive:
                    ready = _operation(lambda: _adaptive_ready(
                        adaptive_url, playback_url, client.descriptor, cancel), monitor, cancel)
                    buffered, adaptive = buffered and ready, adaptive and ready
                if buffered:
                    playback_url, mime = continuous_url, 'audio/aac'
                elif adaptive:
                    playback_url = adaptive_url
            if (launch_nonce is not None and buffered and playback_url == continuous_url
                    and _inputstream_version('inputstream.ffmpegdirect') == '22.2.10'):
                _operation(lambda: client.qualify_fixture_clock(session_id, launch_nonce),
                           monitor, cancel)
            entry = xbmcgui.ListItem(label=station.name, path=playback_url)
            # Master requires an explicit media type for resolved AAC URLs.
            entry.getMusicInfoTag().setTitle(station.name)
            # JSON-RPC can retain a video tag on the original plugin item.
            # Resolve that ambiguous type explicitly to Kodi's music playlist.
            entry.setProperty('playlist_type_hint', '0')
            entry.setProperty('IsPlayable', 'true')
            if buffered:
                entry.setProperty('inputstream', 'inputstream.ffmpegdirect')
                entry.setProperty('inputstream-player', 'videodefaultplayer')
                entry.setProperty('inputstream.ffmpegdirect.stream_mode', 'timeshift')
                entry.setProperty('inputstream.ffmpegdirect.is_realtime_stream', 'false')
            elif adaptive:
                entry.setProperty('inputstream', 'inputstream.adaptive')
                entry.setProperty('inputstream.adaptive.play_timeshift_buffer', 'true')
            # Kodi's redirect/content lookup performs a disposable GET before
            # decoder playback. That would consume our single continuous reader.
            # The service already resolved redirects and the authoritative MIME.
            entry.setMimeType(mime)
            entry.setContentLookup(False)
            if handle >= 0:
                xbmcplugin.setResolvedUrl(handle, True, entry)
            else:
                player = xbmc.Player()
                _playback_arbiter().start(
                    lambda: player.play(playback_url, entry))
        except (OSError, ValueError, KeyError, TypeError, RuntimeError):
            if session: stop(session)
            if not cancel.is_set(): _error(30011)
            if handle >= 0:
                xbmcplugin.setResolvedUrl(handle, False, xbmcgui.ListItem())
        else:
            # Playback is already resolved. Directory reporting must never revoke it.
            if not station.uuid.startswith('fixture-'):
                played_at = datetime.now(timezone.utc).isoformat()
                try:
                    HistoryStore(profile_path()).record_play(station.uuid, played_at)
                except (OSError, ValueError, TypeError):
                    pass  # Profile persistence cannot revoke playback or suppress reporting.
                try:
                    favorites = FavoriteStore(profile_path())
                    if any(s.station_uuid == station.uuid for s in favorites.list()):
                        favorites.mark_played(station.uuid, played_at)
                except (OSError, ValueError):
                    pass  # A profile write must not revoke already-started playback.
                try:
                    browser.click(station.uuid)
                except Exception:
                    pass
        return
    if handle >= 0:
        xbmcplugin.setContent(handle, 'songs')
    listing_succeeded = True
    refresh_favorites = False
    retry = {'mode': 'home'}
    try:
        offset = int(params.get('offset', '0'))
        if not 0 <= offset <= MAX_OFFSET:
            raise ValueError('Invalid page')
        if action == 'home':
            needs_setup = False
            home_window = xbmcgui.Window(10000)
            if (xbmcaddon.Addon().getSettingBool('guided_setup') and
                    not home_window.getProperty('kodi_donor_radio.setup_offered')):
                try:
                    needs_setup = not PlaceStore(profile_path()).list()
                except (OSError, ValueError, TypeError):
                    pass  # The saved-place fallback row below reports this failure.
            if needs_setup:
                home_window.setProperty('kodi_donor_radio.setup_offered', 'true')
                try:
                    _ui_transition('setup', {}, monitor, cancel, base)
                except (Cancelled, OSError, ValueError, TypeError):
                    # A cancelled or unavailable setup must not hide local navigation.
                    pass
            if xbmcaddon.Addon().getSettingBool('offer_setup_row'):
                item(text(30110), folder=False, mode='setup')
            try:
                places = PlaceStore(profile_path()).list()
            except (OSError, ValueError, TypeError):
                places = []
                item(text(30165), folder=False, mode='home')
            for place in places:
                item(place.name, mode='place', id=place.place_id, context=[
                    (label, {'mode': target, 'id': place.place_id}) for label, target in (
                        (30126, 'place_default'), (30127, 'place_radius'), (30128, 'place_remove'))])
            for label, target in ((30111, 'favorites'), (30003, 'tags'), (30002, 'countries'),
                                  (30001, 'search'), (30112, 'map'), (30004, 'settings'),
                                  (30050, 'label_icy_marker')):
                item(text(label), folder=target not in ('map', 'settings', 'label_icy_marker'), mode=target)
            if xbmcaddon.Addon().getSettingBool('test_mode'):
                item(text(30007), mode='tests')
        elif action == 'label_icy_marker':
            _label_icy_marker(monitor, cancel)
            listing_succeeded = False
        elif action == 'settings':
            xbmcaddon.Addon().openSettings()
            listing_succeeded = False
        elif action in ('setup', 'map', 'station_info', 'vote'):
            _ui_transition(action, params, monitor, cancel, base)
            listing_succeeded = False
        elif action in ('favorite_add', 'favorite_remove', 'place_default', 'place_radius', 'place_remove'):
            listing_succeeded = False
            _profile_action(action, params, browser, monitor, cancel)
        elif action == 'favorites':
            favorites = FavoriteStore(profile_path()).list()
            for station in favorites[offset:offset + PAGE_SIZE]:
                station_item(station)
            if offset + PAGE_SIZE < len(favorites):
                item(text(30005), mode=action, offset=offset + PAGE_SIZE)
            if not favorites:
                _error(30113)
            refresh_favorites = bool(favorites)
        elif action == 'place':
            place_id = station_identifier(params.get('id', ''))
            place = next((place for place in PlaceStore(profile_path()).list() if place.place_id == place_id), None)
            if place is None:
                raise ValueError('Unknown place')
            retry = {'mode': action, 'id': place_id, 'offset': offset}
            if place.latitude is None or place.longitude is None:
                _error(30114)
            else:
                stations, next_offset = _directory_page('place',
                    _cache_key(place.place_id, float(place.radius_km), float(place.latitude),
                               float(place.longitude), place.country_code, offset),
                    lambda: _place_stations(browser, place, offset, cancel), monitor, cancel)
                for station in stations:
                    station_item(station)
                if next_offset is not None:
                    if not stations:
                        _error(30116)
                    item(text(30005), mode=action, id=place_id, offset=next_offset)
                elif not stations:
                    _error(30113)
        elif action in ('search', 'stations', 'tag', 'country'):
            name = _query_text(params.get('name', ''))
            country, tag = _query_text(params.get('country', '')), _query_text(params.get('tag', ''))
            if action == 'search' and 'name' not in params:
                name = xbmcgui.Dialog().input(text(30001))
                if not name:
                    listing_succeeded = False
                    return
                name = _query_text(name)
            if action == 'search' and offset == 0 and name.strip():
                try:
                    RecentSearchStore(profile_path()).record(
                        name, 'name', datetime.now(timezone.utc).isoformat())
                except (OSError, ValueError, TypeError):
                    pass  # Search results remain available if profile storage fails.
            if action == 'country':
                code = params.get('code', '')
                if not re.fullmatch(r'[A-Z]{2}', code):
                    raise ValueError('Invalid country code')
                retry = {'mode': action, 'code': code, 'offset': offset}
                fetch = lambda: browser.search_country_page(code, offset, PAGE_SIZE)
            else:
                retry = {'mode': action, 'name': name, 'country': country, 'tag': tag, 'offset': offset}
                fetch = lambda: browser.search_page(name, country, tag, offset, PAGE_SIZE)
            stations, has_more = _search_page(
                _cache_key(action, params.get('code', ''), name, country, tag, offset),
                fetch, monitor, cancel)
            for station in stations:
                station_item(station)
            if has_more and offset + PAGE_SIZE <= MAX_OFFSET:
                item(text(30005), **dict(retry, offset=offset + PAGE_SIZE))
            if not stations:
                _error(30113)
        elif action in ('countries', 'tags'):
            retry = {'mode': action, 'offset': offset}
            if action == 'countries':
                names, _ = _directory_page('countries', 'all',
                    lambda: (browser.country_entries(), None), monitor, cancel)
                has_more = offset + PAGE_SIZE < len(names)
            else:
                names, has_more = _directory_page('tags', _cache_key(offset),
                    lambda: browser.tags_page(offset, PAGE_SIZE), monitor, cancel)
            page = names[offset:offset+PAGE_SIZE] if action == 'countries' else names
            for name in page:
                if action == 'countries':
                    label, code = name
                    item(label, mode='country', code=code)
                else:
                    try:
                        tag = _query_text(name)
                    except ValueError:
                        continue
                    item(tag, mode='tag', tag=tag)
            if has_more and offset + PAGE_SIZE <= MAX_OFFSET:
                item(text(30005), mode=action, offset=offset+PAGE_SIZE)
            if not page:
                _error(30113)
        elif action == 'tests':
            if fixture_origin():
                try:
                    qualified = ad_settings().enabled
                except ValueError:
                    qualified = False
                for label, _, _, hls in _FIXTURES:
                    identifier = 'fixture-%d' % label
                    context = [(30173, {'mode': 'launch_fixture', 'id': identifier})] if qualified and hls else ()
                    item(text(label), folder=False, playable=True, context=context,
                         mode='play_fixture', id=identifier)
        else:
            raise ValueError('Unknown navigation action')
    except Cancelled:
        listing_succeeded = False
    except (OSError, ValueError, TypeError):
        listing_succeeded = False
        if action in _ACTION_ROUTES:
            _error(30167)
        else:
            _error(30012)
            item(text(30006), **retry)
            listing_succeeded = True  # A rendered Retry directory completed successfully.
    finally:
        if handle >= 0:
            xbmcplugin.endOfDirectory(handle, succeeded=listing_succeeded, cacheToDisc=False)
            if listing_succeeded and refresh_favorites:
                _schedule_favorite_refresh(base)


class RelayPlayer(xbmc.Player):
    def __init__(self, manager, port, fixture_origin=None):
        super().__init__()
        self.manager, self.port, self.owned = manager, port, None
        self._generation = 0
        self._armed_generation = None
        self.ads = KodiAdPlayback(manager, self, fixture_origin=fixture_origin,
                                  is_caching=lambda: xbmc.getCondVisibility('Player.Caching'))

    def _started(self, av_started):
        try: current = _relay_path(self.getPlayingFile(), self.port)
        except RuntimeError: current = None
        old = self.owned
        if current != old:
            self._generation += 1
            self._armed_generation = None
        self.owned = current
        if old is not None and old != current:
            self.ads.on_end('cancelled', old)
            self.manager.stop(old)
        if current is not None:
            self.ads.bind(current)
            if av_started:
                self.ads.on_av_started(current)
            else:
                self.ads.on_playback_started(current)
            self._arm_current()

    def onAVStarted(self):
        self._started(True)

    def onPlayBackStarted(self):
        self._started(False)

    def _finished(self, reason):
        # A stop callback for the old stream can arrive after a rapid switch.
        if self.isPlaying() or xbmc.getCondVisibility('Player.Caching'):
            return
        try:
            current = _relay_path(self.getPlayingFile(), self.port)
        except RuntimeError:
            current = None
        if (self.owned is None or self._armed_generation != self._generation
                or current not in (None, self.owned)):
            return
        old, self.owned = self.owned, None
        self._armed_generation = None
        if old is not None:
            self.ads.on_end(reason, old)
            self.manager.stop(old)

    def onPlayBackStopped(self): self._finished('cancelled')
    def onPlayBackEnded(self): self._finished('source_end')
    def onPlayBackError(self): self._finished('cancelled')
    def _arm_current(self):
        if self.owned is None or not self.isPlaying():
            return
        try:
            current = _relay_path(self.getPlayingFile(), self.port)
        except RuntimeError:
            return
        if current == self.owned:
            self._armed_generation = self._generation

    def _callback_session(self):
        if self._armed_generation != self._generation or not self.isPlaying():
            return None
        try:
            current = _relay_path(self.getPlayingFile(), self.port)
        except RuntimeError:
            return None
        return self.owned if current == self.owned else None

    def onPlayBackPaused(self):
        identity = self._callback_session()
        if identity is not None and xbmc.getCondVisibility('Player.Paused'):
            self.ads.on_pause(identity)

    def onPlayBackResumed(self):
        identity = self._callback_session()
        if identity is not None and not xbmc.getCondVisibility('Player.Paused'):
            self.ads.on_resume(identity)

    def onPlayBackSpeedChanged(self, speed):
        identity = self._callback_session()
        if identity is not None: self.ads.on_speed_changed(speed, identity)

    def onPlayBackSeek(self, time, seek_offset):
        identity = self._callback_session()
        if identity is not None: self.ads.on_seek(identity)

    def onPlayBackSeekChapter(self, chapter):
        identity = self._callback_session()
        if identity is not None: self.ads.on_seek(identity)

    def sample_ads(self):
        if self.owned is None:
            return
        try:
            current = _relay_path(self.getPlayingFile(), self.port)
        except RuntimeError:
            return
        if current == self.owned:
            self._arm_current()
            self.ads.sample()


class ServiceMonitor(xbmc.Monitor):
    def __init__(self):
        super().__init__()
        self.settings_changed = False
        self.ad_changed = False
        self._base_settings = self._base_snapshot()

    def _base_snapshot(self):
        addon = xbmcaddon.Addon()
        return (addon.getSettingBool('diagnostics'), addon.getSettingBool('test_mode'),
                addon.getSetting('test_origin'))

    def onSettingsChanged(self):
        current = self._base_snapshot()
        if current != self._base_settings:
            self._base_settings = current
            self.settings_changed = True
        else:
            self.ad_changed = True


def run_service():
    monitor = ServiceMonitor()
    while not monitor.abortRequested():
        server, descriptor, player = None, None, None
        try:
            monitor.ad_changed = False
            profile = _private_profile(profile_path())
            try:
                CreativeCache.cleanup_abandoned(profile)
            except (OSError, ValueError):
                # Stale ad-cache cleanup must not prevent ordinary radio playback.
                pass
            monitor.settings_changed = False
            try:
                config = ad_settings()
            except ValueError:
                # A bad ad-only value must not prevent ordinary radio playback.
                xbmcgui.Dialog().notification(text(30000), text(30068), xbmcgui.NOTIFICATION_ERROR, 5000)
                config = AdConfig(False, False, False, '', '')
            manager = SessionManager(client=HttpClient(policy=runtime_policy()), sink=event_sink(profile),
                                     ad_config=config, cache_root=profile,
                                     icy_marker_store=IcyMarkerStore(profile))
            server = RelayServer(manager=manager)
            descriptor = server.start()
            player = RelayPlayer(manager, descriptor.port, fixture_origin())
            publish_descriptor(profile, descriptor)
            while not monitor.abortRequested() and not monitor.settings_changed:
                if monitor.ad_changed:
                    monitor.ad_changed = False
                    try:
                        manager.ad_config = ad_settings()
                    except ValueError:
                        # An incomplete edit must not retire the playing session.
                        pass
                manager.expire()
                player.sample_ads()
                if monitor.waitForAbort(.25): break
        except (OSError, ValueError):
            xbmcgui.Dialog().notification(text(30000), text(30014), xbmcgui.NOTIFICATION_ERROR, 5000)
            # Stay subscribed so correcting settings can restart a failed service.
            while (not monitor.abortRequested() and not monitor.settings_changed
                   and not monitor.ad_changed):
                if monitor.waitForAbort(.5): break
        finally:
            if player is not None:
                player.ads.close()
            if server is not None: server.close()
            if descriptor is not None: remove_descriptor(profile, descriptor)
