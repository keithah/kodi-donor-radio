# SPDX-License-Identifier: GPL-2.0-or-later
"""Local control records and an authenticated bounded stdlib client."""
from dataclasses import dataclass
import http.client
import json


@dataclass(frozen=True)
class ServiceDescriptor:
    port: int
    instance_id: str
    control_token: str


class ControlClient:
    def __init__(self, descriptor, timeout=10):
        self.descriptor, self.timeout = descriptor, timeout

    def _request(self, method, path, payload=None):
        conn = http.client.HTTPConnection('127.0.0.1', self.descriptor.port, timeout=self.timeout)
        try:
            conn.request(method, path, json.dumps(payload).encode() if payload is not None else None,
                         {'Authorization': 'Bearer ' + self.descriptor.control_token,
                          'Content-Type': 'application/json'})
            response = conn.getresponse()
            body = response.read(16385)
            if len(body) > 16384 or response.status not in (200, 201, 204):
                raise OSError('Relay control request failed')
            return json.loads(body) if body else None
        finally:
            conn.close()

    def create(self, station):
        return self._request('POST', '/sessions', {
            key: getattr(station, key) for key in ('uuid', 'name', 'url', 'codec', 'hls')})

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
