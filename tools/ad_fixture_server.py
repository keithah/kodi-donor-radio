#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
"""Developer-only VAST, beacon, and controlled HLS origin on numeric loopback.

Generate assets with generate_ad_audio.py first. GET /reset clears request and
beacon counts plus all per-scenario rolling windows. No request except /track
records a beacon. This origin is never packaged in the Kodi addon.
"""
import argparse
from collections import Counter
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import ipaddress
import json
from pathlib import Path
import re
import threading
import time
from xml.etree import ElementTree as ET

if __package__:
    from .fixture_server import send_fixture_response
else:
    from fixture_server import send_fixture_response


SOURCE_SCENARIOS = ('normal', 'mutation', 'expired-window')
AD_ASSETS = ('replacement', 'preroll')
EVENTS = ('impression', 'start', 'firstQuartile', 'midpoint',
          'thirdQuartile', 'complete', 'error')


def media_manifest(asset):
    if asset not in AD_ASSETS:
        raise ValueError('Unknown ad asset')
    return ('#EXTM3U\n#EXT-X-VERSION:3\n#EXT-X-TARGETDURATION:3\n'
            '#EXT-X-MEDIA-SEQUENCE:0\n#EXTINF:2.005333,\n'
            '%s000.ts\n#EXTINF:2.005333,\n%s001.ts\n'
            '#EXT-X-ENDLIST\n') % (asset, asset)


def source_manifest(scenario, window):
    if scenario not in SOURCE_SCENARIOS:
        raise ValueError('Unknown source scenario')
    first = min(window, 2) if scenario != 'expired-window' else (0 if window == 0 else 4)
    lines = ['#EXTM3U', '#EXT-X-VERSION:3', '#EXT-X-TARGETDURATION:3',
             '#EXT-X-MEDIA-SEQUENCE:%d' % first]
    for index in range(first, 6):
        if index == 2:
            lines.append('#EXT-X-CUE-OUT:4.010667')
        if index == 4:
            lines.append('#EXT-X-CUE-IN')
        filename = 'source%03d.ts' % index
        if scenario == 'mutation' and window > 0 and index == 3:
            filename = 'source003-mutated.ts'
        lines.extend(('#EXTINF:2.005333,', '/ads/' + filename))
    return '\n'.join(lines) + '\n'


def vast_xml(scenario, origin):
    if scenario == 'empty':
        return b''
    if scenario == 'invalid-xml':
        return b'<VAST><Ad><InLine>'
    root = ET.Element('VAST', {'version': '4.3'})
    if scenario == 'no-fill':
        return ET.tostring(root, encoding='utf-8', xml_declaration=True)
    if scenario == 'wrapped' or scenario == 'wrapper-cycle' or scenario == 'wrapper-depth' or scenario.startswith('wrapper-depth-'):
        ad = ET.SubElement(root, 'Ad', {'id': scenario})
        wrapper = ET.SubElement(ad, 'Wrapper', {'followAdditionalWrappers': 'true',
                                                 'allowMultipleAds': 'false'})
        ET.SubElement(wrapper, 'AdSystem').text = 'Controlled Fixture'
        if scenario == 'wrapped':
            target = 'direct'
        elif scenario == 'wrapper-cycle':
            target = 'wrapper-cycle'
        else:
            depth = 0 if scenario == 'wrapper-depth' else int(scenario.rsplit('-', 1)[1])
            target = 'wrapper-depth-%d' % (depth + 1) if depth < 6 else 'direct'
        ET.SubElement(wrapper, 'VASTAdTagURI').text = origin + '/vast/' + target
        ET.SubElement(wrapper, 'Impression').text = origin + '/track/placement-1/impression'
        ET.SubElement(wrapper, 'Error').text = origin + '/track/placement-1/error?[ERRORCODE]'
        events = ET.SubElement(wrapper, 'TrackingEvents')
        ET.SubElement(events, 'Tracking', {'event': 'start'}).text = origin + '/track/placement-1/start'
        return ET.tostring(root, encoding='utf-8', xml_declaration=True)
    if scenario not in ('direct', 'unsupported-media', 'wrong-duration', 'failed-media', 'preroll'):
        raise ValueError('Unknown VAST scenario')
    ad = ET.SubElement(root, 'Ad', {'id': 'placement-1'})
    inline = ET.SubElement(ad, 'InLine')
    ET.SubElement(inline, 'AdSystem').text = 'Controlled Fixture'
    ET.SubElement(inline, 'AdTitle').text = 'Synthetic %s' % scenario
    ET.SubElement(inline, 'Impression').text = origin + '/track/placement-1/impression'
    ET.SubElement(inline, 'Error').text = origin + '/track/placement-1/error?[ERRORCODE]'
    creatives = ET.SubElement(inline, 'Creatives')
    linear = ET.SubElement(ET.SubElement(creatives, 'Creative'), 'Linear')
    ET.SubElement(linear, 'Duration').text = '00:00:09.000' if scenario == 'wrong-duration' else '00:00:04.000'
    events = ET.SubElement(linear, 'TrackingEvents')
    for event in EVENTS[1:-1]:
        ET.SubElement(events, 'Tracking', {'event': event}).text = origin + '/track/placement-1/' + event
    mediafiles = ET.SubElement(linear, 'MediaFiles')
    if scenario == 'unsupported-media':
        mime, path = 'video/mp4', '/ads/unsupported.mp4'
    elif scenario == 'failed-media':
        mime, path = 'application/vnd.apple.mpegurl', '/ads/missing.m3u8'
    else:
        mime = 'application/vnd.apple.mpegurl'
        path = '/ads/preroll.m3u8' if scenario == 'preroll' else '/ads/replacement.m3u8'
    ET.SubElement(mediafiles, 'MediaFile', {'delivery': 'streaming', 'type': mime}).text = origin + path
    return ET.tostring(root, encoding='utf-8', xml_declaration=True)


def make_server(assets, host='127.0.0.1', port=8766):
    address = ipaddress.ip_address(host)
    if address.version != 4 or not address.is_loopback:
        raise ValueError('Advertising fixtures must bind numeric IPv4 loopback')
    assets = Path(assets)
    requests, beacons, windows = Counter(), Counter(), Counter()
    lock = threading.Lock()

    class Handler(BaseHTTPRequestHandler):
        protocol_version = 'HTTP/1.1'

        def do_GET(self):
            route = self.path.split('?', 1)[0]
            if route == '/reset':
                with lock:
                    requests.clear()
                    beacons.clear()
                    windows.clear()
                return self.respond(b'{}', 'application/json')
            if route == '/stats':
                with lock:
                    body = json.dumps({'requests': dict(requests), 'beacons': dict(beacons),
                                       'windows': dict(windows)}).encode()
                return self.respond(body, 'application/json')
            with lock:
                requests[route] += 1
            match = re.fullmatch(r'/track/([A-Za-z0-9_-]+)/([A-Za-z]+)', route)
            if match and match.group(2) in EVENTS:
                with lock:
                    beacons[match.group(1) + '/' + match.group(2)] += 1
                return self.respond(b'', 'text/plain')
            match = re.fullmatch(r'/vast/([a-z0-9-]+)', route)
            if match:
                scenario = match.group(1)
                if scenario == 'slow':
                    time.sleep(6)
                    scenario = 'direct'
                origin = 'http://127.0.0.1:%d' % self.server.server_port
                try:
                    return self.respond(vast_xml(scenario, origin), 'application/xml')
                except ValueError:
                    pass
            match = re.fullmatch(r'/source/([a-z-]+)/index\.m3u8', route)
            if match and match.group(1) in SOURCE_SCENARIOS:
                scenario = match.group(1)
                with lock:
                    window = windows[scenario]
                    windows[scenario] += 1
                return self.respond(source_manifest(scenario, window).encode(),
                                    'application/vnd.apple.mpegurl')
            match = re.fullmatch(r'/ads/(replacement|preroll)\.m3u8', route)
            if match:
                return self.respond(media_manifest(match.group(1)).encode(),
                                    'application/vnd.apple.mpegurl')
            match = re.fullmatch(r'/ads/([a-z0-9-]+\.ts)', route)
            if match:
                path = assets / match.group(1)
                if path.is_file():
                    return self.respond(path.read_bytes(), 'video/mp2t')
            if route == '/ads/unsupported.mp4':
                path = assets / 'unsupported.mp4'
                if path.is_file():
                    return self.respond(path.read_bytes(), 'video/mp4')
            return self.respond(b'', 'text/plain', 404)

        def respond(self, body, mime, status=200):
            send_fixture_response(self, body, mime, status)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer((host, port), Handler)
    server.daemon_threads = True
    return server


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--assets', type=Path, default=Path('tests/generated-audio/ad'))
    parser.add_argument('--host', default='127.0.0.1')
    parser.add_argument('--port', type=int, default=8766)
    args = parser.parse_args()
    server = make_server(args.assets, args.host, args.port)
    print('Controlled ad fixture origin: http://%s:%d' % (args.host, server.server_port))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == '__main__':
    main()
