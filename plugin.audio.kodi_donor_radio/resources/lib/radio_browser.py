# SPDX-License-Identifier: GPL-2.0-or-later
"""Bounded Radio Browser reads and deliberately non-retried click counting."""
import json
import random
import re
import socket
import threading
import time
from urllib.parse import quote, urlencode, urlsplit

from http_client import Cancelled, HttpClient, HttpError, USER_AGENT, bounded_lookup, read_bounded
from models import Station
from radio_ui import bounded_text, coordinates, safe_favicon, station_identifier


def discover_mirrors(cancel=None, timeout=5):
    """Bound documented forward and reverse DNS with one discovery deadline."""
    cancel = cancel if cancel is not None else threading.Event()
    deadline = time.monotonic() + timeout
    return bounded_lookup(lambda: _lookup_mirrors(cancel, deadline), timeout, cancel)


def _lookup_mirrors(cancel, deadline):
    names = set()
    for record in socket.getaddrinfo('all.api.radio-browser.info', 443, type=socket.SOCK_STREAM):
        if cancel.is_set():
            raise Cancelled('Request cancelled')
        if time.monotonic() >= deadline:
            raise TimeoutError('Mirror discovery timed out')
        try:
            name = socket.gethostbyaddr(record[4][0])[0].rstrip('.').lower()
            if name.endswith('.api.radio-browser.info'):
                names.add('https://' + name)
        except OSError:
            continue
    mirrors = sorted(names)
    random.shuffle(mirrors)
    return mirrors


class RadioBrowser:
    def __init__(self, client=None, mirror_lookup=None, cancel=None, connect_timeout=5):
        self.client = client or HttpClient()
        self.mirror_lookup = mirror_lookup
        self.cancel = cancel if cancel is not None else threading.Event()
        self.connect_timeout = connect_timeout
        self._mirrors = None
        self._preferred = None

    def _servers(self):
        if self._mirrors is None:
            if self.mirror_lookup is None:
                mirrors = discover_mirrors(self.cancel, self.connect_timeout)
            else:
                mirrors = bounded_lookup(self.mirror_lookup, self.connect_timeout, self.cancel)
            self._mirrors = list(dict.fromkeys(mirrors))
        if not self._mirrors:
            raise HttpError('No Radio Browser mirrors available')
        return ([self._preferred] if self._preferred else []) + [m for m in self._mirrors if m != self._preferred]

    def _json(self, path, params=None, retry=True):
        query = '?' + urlencode(params) if params is not None else ''
        last_error = None
        for mirror in self._servers():
            if self.cancel.is_set():
                raise Cancelled('Request cancelled')
            try:
                with self.client.open(mirror.rstrip('/') + path + query,
                                      {'User-Agent': USER_AGENT}, self.cancel) as response:
                    if response.status != 200:
                        raise HttpError('Directory HTTP error')
                    result = json.loads(read_bounded(response).decode('utf-8'))
                self._preferred = mirror
                return result
            except Cancelled:
                raise
            except (OSError, ValueError) as error:
                last_error = error
                if not retry:
                    break
        raise last_error or HttpError('Directory request failed')

    @staticmethod
    def _page(offset, limit):
        if (type(offset) is not int or type(limit) is not int or
                not 0 <= offset <= 10000000 or not 1 <= limit <= 50):
            raise ValueError('Invalid directory page')
        return {'offset': offset, 'limit': limit}

    def search(self, name, country, tag, offset, limit=50):
        return self.search_page(name, country, tag, offset, limit)[0]

    def search_page(self, name, country, tag, offset, limit=50):
        params = {'name': name, 'country': country, 'tag': tag}
        if country:
            params['countryExact'] = 'true'
        if tag:
            params['tagExact'] = 'true'
        params.update(self._page(offset, limit))
        params['hidebroken'] = 'true'
        rows = self._json('/json/stations/search', params)
        return self._stations(rows), len(rows) >= limit

    @staticmethod
    def _country_code(code, optional=False):
        if not isinstance(code, str) or not (optional and code == '' or re.fullmatch(r'[A-Z]{2}', code)):
            raise ValueError('Invalid country code')
        return code

    def search_country(self, code, offset, limit=50):
        return self.search_country_page(code, offset, limit)[0]

    def search_country_page(self, code, offset, limit=50):
        params = {'countrycode': self._country_code(code), 'hidebroken': 'true',
                  'order': 'clickcount', 'reverse': 'true'}
        params.update(self._page(offset, limit))
        rows = self._json('/json/stations/search', params)
        return self._stations(rows), len(rows) >= limit

    def geographic(self, code, offset, limit=50):
        """One directory page, not a radius query; retain the raw cursor on malformed rows."""
        self._country_code(code, optional=True)
        params = {'has_geo_info': 'true', 'hidebroken': 'true',
                  'order': 'clickcount', 'reverse': 'true'}
        if code:
            params['countrycode'] = code
        params.update(self._page(offset, limit))
        rows = self._json('/json/stations/search', params)
        stations = self._stations(rows)
        return stations[:limit], len(rows) >= limit

    def country_entries(self):
        rows = self._json('/json/countries', {'limit': 300})
        if not isinstance(rows, list):
            raise ValueError('Invalid country list')
        entries = []
        for row in rows[:300]:
            if not isinstance(row, dict):
                continue
            code = row.get('iso_3166_1')
            try:
                self._country_code(code)
            except ValueError:
                continue
            name = bounded_text(row.get('name'), 256)
            if name.strip():
                entries.append((name, code))
        return entries

    @staticmethod
    def _stations(rows):
        if not isinstance(rows, list):
            raise ValueError('Invalid station list')
        stations = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            uuid, title = row.get('stationuuid'), row.get('name')
            url = row.get('url_resolved') or row.get('url')
            codec, hls = row.get('codec', ''), row.get('hls', 0)
            if (not all(isinstance(value, str) and value for value in (uuid, title, url))
                    or not isinstance(codec, str) or hls not in (0, 1, False, True)):
                continue
            try:
                if urlsplit(url).scheme not in ('http', 'https') or not urlsplit(url).hostname:
                    continue
            except ValueError:
                continue
            try:
                station_identifier(uuid)
            except ValueError:
                continue
            latitude, longitude = row.get('geo_lat'), row.get('geo_long')
            try:
                coordinates(latitude, longitude)
            except ValueError:
                latitude = longitude = None
            metadata = {name: bounded_text(row.get(source), limit) for name, source, limit in (
                ('country', 'country', 256), ('country_code', 'countrycode', 2),
                ('state', 'state', 256), ('city', 'city', 256), ('language', 'language', 256),
                ('tags', 'tags', 1024))}
            code = metadata['country_code'].upper()
            metadata['country_code'] = code if len(code) == 2 and code.isascii() and code.isalpha() else ''
            title = bounded_text(title, 512)
            if not title.strip():
                continue
            for name, source, maximum in (('bitrate', 'bitrate', 100000),
                                          ('click_count', 'clickcount', 2147483647),
                                          ('votes', 'votes', 2147483647)):
                value = row.get(source, 0)
                metadata[name] = value if type(value) is int and 0 <= value <= maximum else 0
            stations.append(Station(uuid, bounded_text(title, 512), url,
                                    bounded_text(codec, 64), bool(hls),
                                    favicon=safe_favicon(row.get('favicon', '')),
                                    latitude=latitude, longitude=longitude, **metadata))
        return stations

    def by_uuid(self, station_uuid):
        station_identifier(station_uuid)
        stations = self._stations(self._json('/json/stations/byuuid', {'uuids': station_uuid}))
        for station in stations:
            if station.uuid == station_uuid:
                return station
        raise ValueError('Station not found')

    def _names(self, path, params):
        rows = self._json(path, params)
        if not isinstance(rows, list):
            raise ValueError('Invalid directory list')
        return [row['name'] for row in rows if isinstance(row, dict) and isinstance(row.get('name'), str)]

    def countries(self):
        return self._names('/json/countries', {'limit': 300})

    def tags(self, offset, limit=50):
        return self._names('/json/tags', self._page(offset, limit))

    def click(self, uuid):
        """One invocation per user selection; uncertain delivery is never retried."""
        try:
            self._json('/json/url/' + quote(uuid, safe=''), retry=False)
        except (OSError, ValueError):
            pass  # Click counting cannot prevent listening.
