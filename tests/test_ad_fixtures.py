# SPDX-License-Identifier: GPL-2.0-or-later
"""End-to-end contract for developer-only VAST and audio fixtures."""
from contextlib import contextmanager
from dataclasses import FrozenInstanceError
import json
from pathlib import Path
import shutil
import subprocess
import sys
import threading
import time
from urllib.request import urlopen
from urllib.error import HTTPError
import xml.etree.ElementTree as ET

import pytest


ROOT = Path(__file__).resolve().parents[1]
TOOLS = ROOT / 'tools'


@contextmanager
def fixture_origin(assets):
    from tools.ad_fixture_server import make_server

    server = make_server(assets, port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield 'http://127.0.0.1:%d' % server.server_port
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def get(origin, path):
    with urlopen(origin + path, timeout=3) as response:
        return response.read().decode()


@pytest.fixture(scope='module')
def assets(tmp_path_factory):
    missing = [name for name in ('ffmpeg', 'ffprobe', 'espeak') if shutil.which(name) is None]
    if missing:
        pytest.skip('Ad fixture generation requires: ' + ', '.join(missing))
    target = tmp_path_factory.mktemp('ad-audio')
    assert (TOOLS / 'generate_ad_audio.py').is_file(), 'ad audio generator is missing'
    subprocess.run([sys.executable, str(TOOLS / 'generate_ad_audio.py'), '--assets', str(target)], check=True)
    return target


def test_direct_and_wrapped_vast_resolve_to_local_hls_without_beacons(assets):
    with fixture_origin(assets) as origin:
        direct = ET.fromstring(get(origin, '/vast/direct'))
        wrapped = ET.fromstring(get(origin, '/vast/wrapped'))
        assert direct.findtext('.//MediaFile') == origin + '/ads/replacement.m3u8'
        assert wrapped.findtext('.//VASTAdTagURI') == origin + '/vast/direct'
        assert wrapped.findtext('./Ad/Wrapper/Creatives/Creative/Linear/TrackingEvents/Tracking[@event="firstQuartile"]') == origin + '/track/placement-1/firstQuartile?layer=wrapper'
        assert wrapped.find('./Ad/Wrapper/TrackingEvents') is None
        with urlopen(wrapped.findtext('.//VASTAdTagURI'), timeout=3) as response:
            unwrapped = ET.fromstring(response.read())
        assert unwrapped.findtext('.//MediaFile') == origin + '/ads/replacement.m3u8'
        assert direct.findtext('.//Duration') == '00:00:04.000'
        assert '#EXTM3U' in get(origin, '/ads/replacement.m3u8')
        assert get(origin, '/stats')
        stats = json.loads(get(origin, '/stats'))
        assert stats['beacons'] == {}
        get(origin, '/track/placement-1/impression')
        assert json.loads(get(origin, '/stats'))['beacons'] == {'placement-1/impression': 1}


def test_server_rejects_other_numeric_loopback_hosts(tmp_path):
    from tools.ad_fixture_server import make_server

    with pytest.raises(ValueError, match='127\\.0\\.0\\.1'):
        make_server(tmp_path, host='127.0.0.2', port=0)


def test_slow_vast_exceeds_resolver_deadline_but_is_bounded(assets):
    with fixture_origin(assets) as origin:
        started = time.monotonic()
        with urlopen(origin + '/vast/slow', timeout=8) as response:
            parsed = ET.fromstring(response.read())
        elapsed = time.monotonic() - started
        assert 5 <= elapsed < 8
        assert parsed.findtext('.//MediaFile') == origin + '/ads/replacement.m3u8'
        assert json.loads(get(origin, '/stats'))['beacons'] == {}


def test_reset_clears_collector_and_rolling_window(assets):
    with fixture_origin(assets) as origin:
        first = get(origin, '/source/normal/index.m3u8')
        second = get(origin, '/source/normal/index.m3u8')
        assert first == second  # polling cannot advance the live media clock
        get(origin, '/track/placement-1/start')
        get(origin, '/reset')
        assert json.loads(get(origin, '/stats'))['beacons'] == {}
        assert get(origin, '/source/normal/index.m3u8') == first


def test_live_manifest_has_new_media_after_window_advances(assets):
    from tools.ad_fixture_server import source_manifest
    before = source_manifest('normal', 0)
    later = source_manifest('normal', 18)
    assert '/ads/source000.ts' in before
    assert '/ads/source023.ts' in later
    assert '#EXT-X-MEDIA-SEQUENCE:18' in later
    assert '#EXT-X-ENDLIST' not in later
    metadata = json.loads((assets / 'metadata.json').read_text())['assets']
    assert metadata['source023.ts']['first_pts'] > metadata['source005.ts']['last_pts']
    for index in range(23):
        before = metadata['source%03d.ts' % index]
        after = metadata['source%03d.ts' % (index + 1)]
        assert after['first_pts'] - before['last_pts'] == 1920


def test_stall_source_delays_only_new_segment_six(assets):
    from tools.ad_fixture_server import source_manifest
    first = source_manifest('stall', 0)
    next_window = source_manifest('stall', 1)
    assert '/stall/source006.ts' not in first
    assert '/stall/source006.ts' in next_window
    assert '/ads/source005.ts' in first


def test_finite_source_is_static_with_complete_marked_break(assets):
    with fixture_origin(assets) as origin:
        first = get(origin, '/source/finite/index.m3u8')
        second = get(origin, '/source/finite/index.m3u8')
        assert first == second
        assert '#EXT-X-ENDLIST' in first
        assert first.count('#EXTINF:') == 6
        assert '#EXT-X-CUE-OUT:' in first and '#EXT-X-CUE-IN' in first


def test_source_sections_and_replacement_have_matching_aac_frames(assets):
    with fixture_origin(assets) as origin:
        manifest = get(origin, '/source/normal/index.m3u8')
        assert manifest.index('source001.ts') < manifest.index('#EXT-X-CUE-OUT')
        assert manifest.index('#EXT-X-CUE-OUT') < manifest.index('source002.ts')
        assert manifest.index('source003.ts') < manifest.index('#EXT-X-CUE-IN')
        assert manifest.index('#EXT-X-CUE-IN') < manifest.index('source004.ts')
        replacement = get(origin, '/ads/replacement.m3u8')
        assert 'replacement000.ts' in replacement and 'replacement001.ts' in replacement
    metadata = json.loads((assets / 'metadata.json').read_text())
    for source, ad in [('source002.ts', 'replacement000.ts'),
                       ('source003.ts', 'replacement001.ts')]:
        assert metadata['assets'][source]['aac_frames'] == metadata['assets'][ad]['aac_frames']
        assert metadata['assets'][source]['samples'] == metadata['assets'][ad]['samples']
    assert metadata['tone_intervals_hz']['original_break'] != metadata['tone_intervals_hz']['replacement']


def test_failure_variants_and_source_mutation(assets):
    with fixture_origin(assets) as origin:
        assert ET.fromstring(get(origin, '/vast/no-fill')).find('.//Ad') is None
        with pytest.raises(ET.ParseError):
            ET.fromstring(get(origin, '/vast/invalid-xml'))
        cycle = ET.fromstring(get(origin, '/vast/wrapper-cycle'))
        assert cycle.findtext('.//VASTAdTagURI') == origin + '/vast/wrapper-cycle'
        depth = ET.fromstring(get(origin, '/vast/wrapper-depth'))
        assert depth.findtext('.//VASTAdTagURI') == origin + '/vast/wrapper-depth-1'
        unsupported = ET.fromstring(get(origin, '/vast/unsupported-media'))
        assert unsupported.find('.//MediaFile').attrib['type'] == 'video/mp4'
        with urlopen(origin + '/ads/unsupported.mp4') as response:
            assert b'ftyp' in response.read(64)
        wrong = ET.fromstring(get(origin, '/vast/wrong-duration'))
        assert wrong.findtext('.//Duration') == '00:00:09.000'
        before = get(origin, '/source/mutation/index.m3u8')
        after = get(origin, '/source/mutation/index.m3u8')
        assert 'source003.ts' in before and 'source003-mutated.ts' in after


def test_failed_media_depth_and_expired_window(assets):
    with fixture_origin(assets) as origin:
        failed = ET.fromstring(get(origin, '/vast/failed-media'))
        assert failed.findtext('.//MediaFile') == origin + '/ads/missing.m3u8'
        with pytest.raises(HTTPError) as missing:
            get(origin, '/ads/missing.m3u8')
        assert missing.value.code == 404
        route = '/vast/wrapper-depth'
        for _ in range(7):
            target = ET.fromstring(get(origin, route)).findtext('.//VASTAdTagURI')
            assert target.startswith(origin)
            route = target[len(origin):]
        assert route == '/vast/direct'
        assert 'source000.ts' in get(origin, '/source/expired-window/index.m3u8')
        expired = get(origin, '/source/expired-window/index.m3u8')
        assert 'source000.ts' not in expired and 'source004.ts' in expired
        assert json.loads(get(origin, '/stats'))['beacons'] == {}


def test_generated_tones_are_independently_decoded(assets):
    metadata = json.loads((assets / 'metadata.json').read_text())
    assert len(metadata['assets']) == 29
    assert {item['aac_frames'] for item in metadata['assets'].values()} == {94}
    assert all(item['decoded_tone_hz'] == item['expected_tone_hz']
               for item in metadata['assets'].values())
    assert all(item['codec'] == 'aac' and item['sample_rate'] == 48000
               and item['channels'] == 2 for item in metadata['assets'].values())
    for names in (['source%03d.ts' % i for i in range(6)],
                  ['replacement000.ts', 'replacement001.ts'],
                  ['preroll000.ts', 'preroll001.ts']):
        for left, right in zip(names, names[1:]):
            a, b = metadata['assets'][left], metadata['assets'][right]
            assert b['first_pts'] == a['last_pts'] + 1920


def test_shared_records_are_immutable():
    from ad_models import MediaInfo, TrackingUrl

    record = TrackingUrl('start', 'http://example.test/start')
    with pytest.raises(FrozenInstanceError):
        record.event = 'complete'
    info = MediaInfo('aac', 48000, 2, 96256, 0, 178560)
    with pytest.raises(FrozenInstanceError):
        info.samples = 0
