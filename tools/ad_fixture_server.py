#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
"""Developer-only VAST, beacon, and controlled HLS origin on 127.0.0.1.

Generate assets with generate_ad_audio.py first. GET /reset clears request and
beacon counts plus all per-scenario rolling windows. No request except /track
records a beacon. This origin is never packaged in the Kodi addon.

The late-break and recovery scenarios use the extended 128-segment assets.
Recovery holds its media clock at zero for 12 seconds, then advances without
counting that hold as missing media. Both publish complete break intervals
together so an earlier partial window cannot commit the original break audio.
"""
import argparse
from collections import Counter, deque
import ctypes
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import re
import sys
import threading
import time
from time import get_clock_info, monotonic_ns
from xml.etree import ElementTree as ET

if __package__:
    from .fixture_server import send_fixture_response
    from .generate_ad_audio import NORMAL_SOURCE_SEGMENTS, EXTENDED_SOURCE_SEGMENTS
else:
    from fixture_server import send_fixture_response
    from generate_ad_audio import NORMAL_SOURCE_SEGMENTS, EXTENDED_SOURCE_SEGMENTS


SOURCE_SCENARIOS = ('normal', 'mutation', 'expired-window', 'finite', 'stall', 'extended',
                    'late-break', 'recovery')
LONG_SOURCE_SCENARIOS = ('extended', 'late-break', 'recovery')
DEFERRED_BREAKS = {'late-break': (70, 72), 'recovery': (6, 8)}
# Window 3 first contains the complete break. Withhold it until 18.016 s: later
# than the 16.043 s initial programme+preroll, before Kodi's default 20 s low-speed
# network timeout. The old hold 20 scenario released at 26 s and tested timeout.
RECOVERY_HOLD_SECONDS = 12
AD_ASSETS = ('replacement', 'preroll')
LIVE_SEGMENT_SECONDS = 94 * 1024 / 48000
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
    source_count = EXTENDED_SOURCE_SEGMENTS if scenario in LONG_SOURCE_SCENARIOS else NORMAL_SOURCE_SEGMENTS
    last_window = source_count - 6
    if scenario == 'finite':
        first = 0
    elif scenario == 'expired-window':
        first = 0 if window == 0 else max(4, min(window, last_window))
    else:
        first = min(window, last_window)
    break_start, break_end = DEFERRED_BREAKS.get(scenario, (2, 4))
    stop = min(first + 6, source_count)
    if scenario in DEFERRED_BREAKS and first <= break_start < stop <= break_end:
        # Publishing even one original break segment would make that interval
        # immutable before cue-in arrived. Keep the preceding programme visible
        # until one window contains the full break and its following cue-in.
        stop = break_start
    lines = ['#EXTM3U', '#EXT-X-VERSION:3', '#EXT-X-TARGETDURATION:3',
             '#EXT-X-MEDIA-SEQUENCE:%d' % first]
    for index in range(first, stop):
        if index == break_start:
            lines.append('#EXT-X-CUE-OUT:4.010667')
        if index == break_end:
            lines.append('#EXT-X-CUE-IN')
        filename = 'source%03d.ts' % index
        if scenario == 'mutation' and window > 0 and index == 3:
            filename = 'source003-mutated.ts'
        prefix = '/stall/' if scenario == 'stall' and index == 6 else '/ads/'
        lines.extend(('#EXTINF:2.005333,', prefix + filename))
    if scenario == 'finite' or (scenario in LONG_SOURCE_SCENARIOS and first == last_window):
        lines.append('#EXT-X-ENDLIST')
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
        creatives = ET.SubElement(wrapper, 'Creatives')
        linear = ET.SubElement(ET.SubElement(creatives, 'Creative'), 'Linear')
        events = ET.SubElement(linear, 'TrackingEvents')
        ET.SubElement(events, 'Tracking', {'event': 'start'}).text = origin + '/track/placement-1/start'
        ET.SubElement(events, 'Tracking', {'event': 'firstQuartile'}).text = origin + '/track/placement-1/firstQuartile?layer=wrapper'
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


def make_tracking_clock():
    """Return a timestamp reader plus its honest cross-process clock domain.

    Older Python releases subtract a process origin from macOS monotonic time,
    even though get_clock_info names mach_absolute_time. Read Mach directly for
    comparisons with Core Audio host timestamps; never silently substitute a
    different epoch if native initialization fails.
    """
    info = get_clock_info('monotonic')
    python_clock = {'api': 'time.monotonic_ns', 'implementation': info.implementation,
                    'resolution_seconds': info.resolution,
                    'monotonic': info.monotonic, 'adjustable': info.adjustable}
    if sys.platform != 'darwin':
        return monotonic_ns, dict(python_clock, domain='python_monotonic',
                                  cross_process_comparable=False)

    class MachTimebase(ctypes.Structure):
        _fields_ = [('numer', ctypes.c_uint32), ('denom', ctypes.c_uint32)]

    native = ctypes.CDLL('/usr/lib/libSystem.B.dylib')
    native.mach_timebase_info.argtypes = [ctypes.POINTER(MachTimebase)]
    native.mach_timebase_info.restype = ctypes.c_int
    native.mach_absolute_time.argtypes = []
    native.mach_absolute_time.restype = ctypes.c_uint64
    timebase = MachTimebase()
    if native.mach_timebase_info(ctypes.byref(timebase)) != 0 or not timebase.numer or not timebase.denom:
        raise RuntimeError('Mach timebase initialization failed')
    numer, denom = int(timebase.numer), int(timebase.denom)

    def read_mach_ns():
        # Python integer arithmetic preserves all tick bits and cannot overflow
        # when the uint64 tick count is multiplied by the timebase numerator.
        return native.mach_absolute_time() * numer // denom

    return read_mach_ns, {'api': 'mach_absolute_time', 'implementation': 'mach_absolute_time()',
                          'domain': 'mach_absolute_time', 'cross_process_comparable': True,
                          'timebase_numer': numer, 'timebase_denom': denom,
                          'resolution_seconds': numer / denom / 1_000_000_000,
                          'monotonic': True, 'adjustable': False,
                          'python_monotonic': python_clock}


def make_server(assets, host='127.0.0.1', port=8766):
    if host != '127.0.0.1':
        raise ValueError('Advertising fixtures must bind numeric 127.0.0.1')
    assets = Path(assets)
    requests, beacons, windows = Counter(), Counter(), Counter()
    tracking_observations = deque(maxlen=256)
    read_tracking_ns, tracking_clock = make_tracking_clock()
    starts = {}
    lock = threading.Lock()

    class Handler(BaseHTTPRequestHandler):
        protocol_version = 'HTTP/1.1'

        def do_GET(self):
            route = self.path.split('?', 1)[0]
            if route == '/reset':
                with lock:
                    requests.clear()
                    beacons.clear()
                    tracking_observations.clear()
                    windows.clear()
                    starts.clear()
                return self.respond(b'{}', 'application/json')
            if route == '/stats':
                with lock:
                    body = json.dumps({'requests': dict(requests), 'beacons': dict(beacons),
                                       'windows': dict(windows),
                                       'tracking_observations': list(tracking_observations),
                                       'tracking_clock': tracking_clock}).encode()
                return self.respond(body, 'application/json')
            with lock:
                requests[route] += 1
            match = re.fullmatch(r'/track/([A-Za-z0-9_-]+)/([A-Za-z]+)', route)
            if match and match.group(2) in EVENTS:
                with lock:
                    beacons[match.group(1) + '/' + match.group(2)] += 1
                    # Record arrival only, not playback. Event names are from
                    # the fixed allowlist; URLs and arbitrary query data stay out.
                    tracking_observations.append({'event': match.group(2),
                                                  'monotonic_ns': read_tracking_ns()})
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
                    now = time.monotonic()
                    started = starts.setdefault(scenario, now)
                    elapsed = now - started
                    if scenario == 'recovery':
                        elapsed = max(0, elapsed - RECOVERY_HOLD_SECONDS)
                    # Mutation and expiry deliberately accelerate by request
                    # so the error is reproducible without a wall-clock wait.
                    window = (windows[scenario] if scenario in ('mutation', 'expired-window')
                              else int(elapsed / LIVE_SEGMENT_SECONDS))
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
                    return self.respond(path.read_bytes(), 'video/mp2t', ranges=True)
            if route == '/stall/source006.ts':
                time.sleep(6)
                path = assets / 'source006.ts'
                if path.is_file():
                    return self.respond(path.read_bytes(), 'video/mp2t', ranges=True)
            if route == '/ads/unsupported.mp4':
                path = assets / 'unsupported.mp4'
                if path.is_file():
                    return self.respond(path.read_bytes(), 'video/mp4')
            return self.respond(b'', 'text/plain', 404)

        def respond(self, body, mime, status=200, ranges=False):
            send_fixture_response(self, body, mime, status, ranges)

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
