# SPDX-License-Identifier: GPL-2.0-or-later
"""Local control records and an authenticated bounded stdlib client."""
from dataclasses import dataclass
import http.client
import json
import re


@dataclass(frozen=True)
class ServiceDescriptor:
    port: int
    instance_id: str
    control_token: str


class SelectionSuperseded(Exception):
    """A newer station selection took over while this one was being prepared."""


class _Conflict(OSError):
    """HTTP 409 from the relay; callers that ignore it see an ordinary OSError."""


class _NotFound(OSError):
    """HTTP 404 from the relay."""


class ControlClient:
    def __init__(self, descriptor, timeout=10):
        self.descriptor, self.timeout = descriptor, timeout

    def _request(self, method, path, payload=None):
        conn = http.client.HTTPConnection('127.0.0.1', self.descriptor.port, timeout=self.timeout)
        try:
            try:
                conn.request(method, path, json.dumps(payload).encode() if payload is not None else None,
                             {'Authorization': 'Bearer ' + self.descriptor.control_token,
                              'Content-Type': 'application/json'})
                response = conn.getresponse()
                body = response.read(16385)
                if response.status == 409:
                    raise _Conflict('Relay control request conflicted')
                if response.status == 404:
                    raise _NotFound('Relay control request found nothing')
                if len(body) > 16384 or response.status not in (200, 201, 204):
                    raise OSError('Relay control request failed')
                if not body:
                    return None
                try:
                    return json.loads(body)
                except ValueError as error:
                    raise OSError('Invalid relay control response') from error
            except http.client.HTTPException as error:
                raise OSError('Relay control request failed') from error
        finally:
            conn.close()

    def create(self, station, fixture_launch_nonce=None, continuation=None, selection=None):
        payload = {key: getattr(station, key) for key in ('uuid', 'name', 'url', 'codec', 'hls')}
        if fixture_launch_nonce is not None:
            payload['fixture_launch_nonce'] = fixture_launch_nonce
        if continuation is None and selection is None:
            return self._request('POST', '/sessions', payload)
        if continuation is not None:
            payload['continuation'] = continuation
        else:
            payload['selection'] = selection
        try:
            return self._request('POST', '/sessions', payload)
        except _Conflict:
            raise SelectionSuperseded() from None

    def continuation_station(self, token):
        """Fields of the station a break continues to; None if the relay forgot it."""
        try:
            result = self._request('GET', '/continuations/' + token)
        except _NotFound:
            return None
        fields = ('uuid', 'name', 'url', 'codec', 'hls')
        if (not isinstance(result, dict) or set(result) != set(fields)
                or not all(isinstance(result[key], str) for key in fields[:4])
                or not isinstance(result['hls'], bool)):
            raise OSError('Invalid relay control response')
        return result

    def reserve_selection(self):
        """Claim the next place in selection order before a slow station lookup."""
        result = self._request('POST', '/selections', {})
        selection = result.get('selection') if isinstance(result, dict) else None
        if type(selection) is not int or not 0 < selection < 2 ** 53:
            raise OSError('Invalid relay control response')
        return selection

    def prepare_network_ads(self, station, follow_url, selection=None):
        """Ask the service for a pre-roll break; returns validated relay items."""
        payload = {key: getattr(station, key) for key in (
            'uuid', 'name', 'url', 'codec', 'hls', 'country_code', 'language', 'tags')}
        payload['follow_url'] = follow_url
        if selection is not None:
            payload['selection'] = selection
        result = self._request('POST', '/network-ads', payload)
        if isinstance(result, dict) and result.get('superseded') is True:
            raise SelectionSuperseded()
        ads = result.get('ads') if isinstance(result, dict) else None
        if not isinstance(ads, list) or len(ads) > 5:
            raise OSError('Invalid relay control response')
        prefix = 'http://127.0.0.1:%d/network-ads/' % self.descriptor.port
        for item in ads:
            if (not isinstance(item, dict) or not isinstance(item.get('url'), str)
                    or re.fullmatch(re.escape(prefix) + r'[a-f0-9]{48}\.mp3', item['url']) is None
                    or not isinstance(item.get('title', ''), str)
                    or not isinstance(item.get('duration'), (int, float))):
                raise OSError('Invalid relay control response')
        return ads

    def reserve_fixture_launch(self, fixture_id):
        result = self._request('POST', '/fixture-launches', {'fixture_id': fixture_id})
        nonce = result.get('nonce') if isinstance(result, dict) else None
        if not isinstance(nonce, str) or re.fullmatch(r'[a-f0-9]{48}', nonce) is None:
            raise OSError('Invalid relay control response')
        return nonce

    def qualify_fixture_clock(self, session_id, nonce):
        self._request('POST', '/fixture-clock/qualify', {'session_id': session_id, 'nonce': nonce})

    def cancel_fixture_launch(self, nonce):
        self._request('DELETE', '/fixture-launches/' + nonce)

    def create_preview(self, station):
        """Create only when the service has no deliberate/pending session."""
        return self._request('POST', '/preview-sessions', {
            key: getattr(station, key) for key in ('uuid', 'name', 'url', 'codec', 'hls')})

    def stop(self, session_id):
        self._request('DELETE', '/sessions/' + session_id)

    def health(self):
        return self._request('GET', '/health')

    def current_icy_candidate(self):
        return self._request('GET', '/icy-marker/candidate')

    def learn_icy_marker(self, session_id, marker, action):
        return self._request('POST', '/icy-marker/learn',
                             {'session_id': session_id, 'marker': marker, 'action': action})

    def label_icy_marker(self, session_id, marker, action, remember):
        return self._request('POST', '/icy-marker/label',
                             {'session_id': session_id, 'marker': marker,
                              'action': action, 'remember': remember})
