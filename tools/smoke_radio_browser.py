# SPDX-License-Identifier: GPL-2.0-or-later
"""Bounded directory/stream probes; never call the Radio Browser click endpoint.

These reads prove neither audible playback nor advertisement/cue coverage.
Only UUIDs, schemes, protocol classifications and byte counts are emitted.
"""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import sys
import threading
from urllib.parse import urlsplit
from uuid import UUID

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'plugin.audio.kodi_donor_radio' / 'resources' / 'lib'))

from hls import UnsupportedPlaylist, parse_playlist
from http_client import HttpClient, HttpError, USER_AGENT, read_bounded
from icy import IcyDecoder
from radio_browser import RadioBrowser
from station_resolver import resolve_station
from url_policy import UnsafeUrl


def _uuid(value):
    try:
        return str(UUID(value))
    except (ValueError, TypeError, AttributeError):
        return None


def _hls_sample(url, client, cancel):
    for _ in range(3):
        with client.open(url, {'User-Agent': USER_AGENT}, cancel) as response:
            if response.status != 200:
                raise HttpError('Manifest unavailable')
            playlist = parse_playlist(read_bounded(response).decode('utf-8-sig'), response.final_url)
        if playlist.is_master:
            variants = [ref.url for ref in playlist.uri_references if ref.kind == 'playlist']
            if not variants:
                raise ValueError('No sample rendition')
            url = variants[0]
            continue
        if not playlist.segments:
            raise ValueError('No sample segment')
        with client.open(playlist.segments[0].uri, {'User-Agent': USER_AGENT}, cancel) as response:
            if response.status != 200:
                raise HttpError('Segment unavailable')
            sample = response.read(4096)
            if not sample:
                raise HttpError('Empty segment')
            return len(sample)
    raise UnsupportedPlaylist('Sample rendition depth exceeded')


def probe_station(station, client=None, timeout=15):
    client = client or HttpClient()
    scheme = urlsplit(station.url).scheme
    result = {'station_uuid': _uuid(station.uuid),
              'scheme': scheme if scheme in ('http', 'https') else None,
              'protocol': None, 'outcome': None, 'sample_bytes': 0}
    cancel = threading.Event()
    timer = threading.Timer(timeout, cancel.set)
    timer.daemon = True
    timer.start()
    source = None
    try:
        source = resolve_station(station.url, client, cancel)
        result['protocol'] = source.protocol
        if source.protocol == 'hls':
            size = _hls_sample(source.final_url, client, cancel)
        else:
            sample = source.initial_bytes
            metaint = source.response.headers.get('icy-metaint')
            if metaint is not None:
                if not re.fullmatch(r'[0-9]{1,10}', metaint):
                    raise ValueError('Invalid ICY interval')
                if int(metaint) > 0:
                    decoder = IcyDecoder(int(metaint), lambda metadata: None)
                    sample = decoder.feed(sample)
                    decoder.finish()
            if not sample:
                raise HttpError('Empty continuous sample')
            size = len(sample)
        result.update(outcome='reachable', sample_bytes=size)
    except UnsafeUrl:
        result['outcome'] = 'policy_rejection'
    except UnsupportedPlaylist:
        result['outcome'] = 'unsupported'
    except OSError:
        result['outcome'] = 'reachability_failure'
    except ValueError:
        result['outcome'] = 'parser_failure'
    finally:
        if source is not None and source.response is not None:
            source.response.close()
        cancel.set()
        timer.cancel()
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--limit', type=int, default=3, choices=range(1, 11), metavar='1..10')
    parser.add_argument('--timeout', type=int, default=15, choices=range(1, 31), metavar='1..30',
                        help='deadline in seconds for discovery and separately for each station')
    args = parser.parse_args()
    started = datetime.now(timezone.utc).isoformat()
    cancel = threading.Event()
    timer = threading.Timer(args.timeout, cancel.set)
    timer.daemon = True
    timer.start()
    try:
        stations = RadioBrowser(cancel=cancel).search('', '', '', 0, args.limit)
    except OSError:
        print(json.dumps({'at': started, 'discovery': 'reachability_failure', 'stations': []}))
        return 1
    except ValueError:
        print(json.dumps({'at': started, 'discovery': 'parser_failure', 'stations': []}))
        return 2
    finally:
        cancel.set()
        timer.cancel()
    results = [probe_station(station, timeout=args.timeout) for station in stations[:args.limit]]
    print(json.dumps({'at': started, 'discovery': 'ok', 'stations': results}, indent=2))
    if any(result['outcome'] == 'parser_failure' for result in results):
        return 2
    return 0 if results and all(result['outcome'] == 'reachable' for result in results) else 1


if __name__ == '__main__':
    sys.exit(main())
