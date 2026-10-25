# SPDX-License-Identifier: GPL-2.0-or-later
"""Live-smoke classifications through the real resolver, HTTP and HLS parsers."""
import importlib.util
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import threading

import pytest

from http_client import HttpClient
from models import Station
from url_policy import UrlPolicy


def load_tool():
    path = Path(__file__).resolve().parents[1] / 'tools' / 'smoke_radio_browser.py'
    assert path.is_file(), 'Live-smoke tool is not implemented'
    spec = importlib.util.spec_from_file_location('smoke_tool', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def origin():
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.server.paths.append(self.path)
            route = self.path.split('?')[0]
            content_type = 'audio/mpeg'
            body = b'\xff\xfb' * 512
            status = 200
            if route in ('/hls', '/bad-hls', '/encrypted'):
                content_type = 'application/vnd.apple.mpegurl'
                body = b'#EXTM3U\n#EXT-X-TARGETDURATION:2\n#EXTINF:2,\n/segment?token=secret\n'
                if route == '/bad-hls':
                    body = b'#EXTM3U\n#EXT-X-TARGETDURATION:bad\n'
                if route == '/encrypted':
                    body = b'#EXTM3U\n#EXT-X-KEY:METHOD=AES-128,URI="/key"\n#EXT-X-TARGETDURATION:2\n'
            if route == '/offline':
                status, body = 503, b'unavailable'
            if route == '/bad-icy':
                self.send_response(200)
                self.send_header('icy-metaint', 'nope')
            else:
                self.send_response(status)
            if route == '/zero-icy':
                self.send_header('icy-metaint', '0')
            self.send_header('Content-Type', content_type)
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        def log_message(self, *args):
            pass
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    server.paths = []
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = 'http://127.0.0.1:%s' % server.server_port
    try:
        yield url, server.paths
    finally:
        server.shutdown()
        server.server_close()
        thread.join(2)


@pytest.mark.parametrize('route, outcome, protocol', [
    ('/audio', 'reachable', 'continuous'),
    ('/hls', 'reachable', 'hls'),
    ('/bad-hls', 'parser_failure', 'hls'),
    ('/encrypted', 'unsupported', 'hls'),
    ('/offline', 'reachability_failure', None),
    ('/bad-icy', 'parser_failure', 'continuous'),
    ('/zero-icy', 'reachable', 'continuous'),
])
def test_probe_classifies_without_leaking_urls_or_counting_clicks(origin, route, outcome, protocol):
    module = load_tool()
    url, paths = origin
    client = HttpClient(UrlPolicy(test_origins={url}))
    station = Station('00000000-0000-0000-0000-000000000001', 'private title',
                      url + route + '?password=secret', 'MP3', False)
    result = module.probe_station(station, client, timeout=2)
    assert result['outcome'] == outcome
    assert result['protocol'] == protocol
    assert result['station_uuid'] == '00000000-0000-0000-0000-000000000001'
    assert result['scheme'] == 'http'
    assert 'secret' not in str(result) and 'private title' not in str(result)
    assert not any('/json/url/' in path for path in paths)
    if route == '/hls':
        assert '/segment?token=secret' in paths
        assert result['sample_bytes'] > 0


def test_cli_bounds_sampling_and_does_not_report_clicks(origin, monkeypatch, capsys):
    import json
    from radio_browser import RadioBrowser
    module = load_tool()
    url, paths = origin
    client = HttpClient(UrlPolicy(test_origins={url}))
    # Substitute the external directory; stream transport/resolution remain real.
    def search(self, name, country, tag, offset, limit):
        assert (name, country, tag, offset, limit) == ('', '', '', 0, 1)
        return [Station('00000000-0000-0000-0000-000000000001', 'title', url + '/audio', 'MP3', False)]
    monkeypatch.setattr(RadioBrowser, 'search', search)
    monkeypatch.setattr(module, 'HttpClient', lambda: client)
    monkeypatch.setattr(module.sys, 'argv', ['smoke_radio_browser.py', '--limit', '1'])
    assert module.main() == 0
    result = json.loads(capsys.readouterr().out)
    assert result['discovery'] == 'ok'
    assert len(result['stations']) == 1
    assert result['stations'][0]['outcome'] == 'reachable'
    assert paths == ['/audio']
