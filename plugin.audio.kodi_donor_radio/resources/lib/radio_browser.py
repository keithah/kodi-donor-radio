# SPDX-License-Identifier: GPL-2.0-or-later
"""Bounded Radio Browser reads and deliberately non-retried click counting."""
import json
import random
import socket
import threading
from urllib.parse import quote, urlencode, urlsplit

from http_client import HttpClient, HttpError, USER_AGENT, read_bounded
from models import Station


def discover_mirrors():
    """Use the documented all.api DNS lookup followed by reverse DNS."""
    names = set()
    for record in socket.getaddrinfo('all.api.radio-browser.info', 443, type=socket.SOCK_STREAM):
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
    def __init__(self, client=None, mirror_lookup=None):
        self.client = client or HttpClient()
        self.mirror_lookup = mirror_lookup or discover_mirrors
        self._mirrors = None
        self._preferred = None

    def _servers(self):
        if self._mirrors is None:
            self._mirrors = list(dict.fromkeys(self.mirror_lookup()))
        if not self._mirrors:
            raise HttpError('No Radio Browser mirrors available')
        return ([self._preferred] if self._preferred else []) + [m for m in self._mirrors if m != self._preferred]

    def _json(self, path, params=None, retry=True):
        query = '?' + urlencode(params) if params is not None else ''
        last_error = None
        for mirror in self._servers():
            try:
                with self.client.open(mirror.rstrip('/') + path + query,
                                      {'User-Agent': USER_AGENT}, threading.Event()) as response:
                    if response.status != 200:
                        raise HttpError('Directory HTTP error')
                    result = json.loads(read_bounded(response).decode('utf-8'))
                self._preferred = mirror
                return result
            except (OSError, ValueError) as error:
                last_error = error
                if not retry:
                    break
        raise last_error or HttpError('Directory request failed')

    @staticmethod
    def _page(offset, limit):
        if offset < 0 or limit < 1 or limit > 50:
            raise ValueError('Invalid directory page')
        return {'offset': offset, 'limit': limit}

    def search(self, name, country, tag, offset, limit=50):
        params = {'name': name, 'country': country, 'tag': tag}
        params.update(self._page(offset, limit))
        params['hidebroken'] = 'true'
        rows = self._json('/json/stations/search', params)
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
            stations.append(Station(uuid, title, url, codec, bool(hls)))
        return stations

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
