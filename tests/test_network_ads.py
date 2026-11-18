# SPDX-License-Identifier: GPL-2.0-or-later
import http.client
import json
import threading
import time
from contextlib import contextmanager
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest

from control import ControlClient
from models import Station
from network_ads import (NetworkAdConfig, NetworkAdError, NetworkAdService, NetworkVastResolver,
                         TEST_TAG, build_request, expand_macros, looks_like_mp3, validate_tag_url)
from relay import RelayServer
from sessions import SessionManager

MP3 = b'ID3\x03\x00\x00\x00\x00\x00\x00' + b'\xff\xfb\x90\x64' + b'\x00' * 2000
STATION = Station('station-uuid', 'Jazz FM', 'http://example.com/s', 'MP3', False,
                  country_code='us', language='english', tags='jazz,blues')


def inline(ad_id='43', sequence='1', duration='00:00:30', media='https://media.example/a.mp3',
           kind='audio/mpeg'):
    seq = ' sequence="%s"' % sequence if sequence else ''
    tracking = ''.join('<Tracking event="%s"><![CDATA[https://t.example/e?e=%s]]></Tracking>' % (e, e)
                       for e in ('start', 'firstQuartile', 'midpoint', 'thirdQuartile', 'complete'))
    return ('<Ad id="%s"%s adType="audio"><InLine><AdSystem>S</AdSystem>'
            '<Error><![CDATA[https://t.example/err?c=%%5BERRORCODE%%5D&a=%s]]></Error>'
            '<Impression><![CDATA[https://t.example/imp?a=%s]]></Impression>'
            '<AdTitle>Ad %s</AdTitle><Creatives><Creative><Linear>'
            '<TrackingEvents>%s</TrackingEvents><Duration>%s</Duration><MediaFiles>'
            '<MediaFile delivery="progressive" type="%s"><![CDATA[%s]]></MediaFile>'
            '</MediaFiles></Linear></Creative></Creatives></InLine></Ad>'
            % (ad_id, seq, ad_id, ad_id, ad_id, tracking, duration, kind, media))


def vast(*ads):
    return ('<?xml version="1.0"?><VAST version="4.2" xmlns="http://www.iab.com/VAST">%s</VAST>'
            % ''.join(ads)).encode()


class Response:
    def __init__(self, status, body, url):
        self.status, self._body, self.final_url, self.headers = status, body, url, {}

    def read(self, size=-1):
        data, self._body = (self._body, b'') if size < 0 else (self._body[:size], self._body[size:])
        return data

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class FakeClient:
    def __init__(self, routes):
        self.routes, self.requests = routes, []
        self.lock = threading.Lock()

    def open(self, url, headers, cancel, deadline=None, allowed_origin=None):
        with self.lock:
            self.requests.append((url, dict(headers)))
        for prefix, (status, body) in self.routes.items():
            if url.startswith(prefix):
                return Response(status, body, url)
        raise OSError('unreachable')


def config(**values):
    base = dict(enabled=True, tag_url=TEST_TAG, user_agent='Kodi/21.3', device_ip='8.8.8.8')
    base.update(values)
    return NetworkAdConfig(**base)


def test_request_passes_guide_parameters_and_listener_headers():
    url, headers = build_request(config(publisher_domain='radio.example', max_ads=2,
                                        break_ms=60000, blocked_categories='IAB9-32,IAB23',
                                        content_categories='357', gdpr='1', consent='abc',
                                        store_url='https://store.example/app', maid='m-1',
                                        schain='1.0,1!a.com,1,1'), STATION)
    query = {key: values[0] for key, values in parse_qs(urlsplit(url).query).items()}
    assert query == {
        'zoneId': 'ortb-test-zone', 'position': 'pre', 'content_genre': 'Jazz',
        'content_language': 'en', 'content_country': 'US', 'content_categories': '357',
        'blocked_categories': 'IAB9-32,IAB23', 'publisher_name': 'Jazz FM',
        'publisher_domain': 'radio.example', 'maxAds': '2', 'duration': '60000',
        'bundle_id': 'plugin.audio.kodi_donor_radio', 'store_url': 'https://store.example/app',
        'maid': 'm-1', 'gdpr': '1', 'consent': 'abc', 'schain': '1.0,1!a.com,1,1'}
    assert 'IAB9-32%2CIAB23' in url  # Values are URL encoded.
    assert headers['X-Device-IP'] == '8.8.8.8'
    assert headers['X-Device-User-Agent'] == 'Kodi/21.3'


def test_request_keeps_tag_values_and_omits_unknown_optionals(monkeypatch):
    monkeypatch.setattr('network_ads.detect_device_ipv4', lambda: '')
    tag = 'https://ssp.example/vast/tag?zoneId=z&position=mid&publisher_domain=tag.example'
    url, headers = build_request(config(tag_url=tag, device_ip='', publisher_domain='other.example',
                                        gdpr='', consent='ignored'),
                                 Station('u', 'N', 'http://s', 'MP3', False))
    query = parse_qs(urlsplit(url).query)
    assert query['position'] == ['mid'] and query['publisher_domain'] == ['tag.example']
    assert 'gdpr' not in query and 'consent' not in query and 'content_genre' not in query
    assert 'X-Device-IP' not in headers


@pytest.mark.parametrize('value', ['http://ssp.example/t?zoneId=z', 'https://ssp.example/t',
                                   'https://ssp.example/t?zoneId=z&position=side', ''])
def test_tag_validation_rejects_unsafe_or_incomplete_tags(value):
    with pytest.raises(ValueError):
        validate_tag_url(value)


def test_macros_fill_raw_and_encoded_forms_and_keep_unknown():
    url = expand_macros('https://t/e?c=%5BERRORCODE%5D&p=[ADPLAYHEAD]&x=[UNKNOWN]', 405, 75.5)
    assert 'c=405' in url and 'p=00%3A01%3A15.500' in url and '[UNKNOWN]' in url


def test_resolver_orders_pod_and_limits_count():
    client = FakeClient({'https://ssp': (200, vast(inline('b', '2'), inline('a', '1'),
                                                   inline('c', '3')))})
    ads = NetworkVastResolver(client).resolve('https://ssp/tag', {}, threading.Event(), 2)
    assert [ad.ad_id for ad in ads] == ['a', 'b']
    assert ads[0].duration_seconds == 30 and ads[0].media_url == 'https://media.example/a.mp3'
    assert [event for event, _ in ads[0].tracking][:2] == ['start', 'firstQuartile']


def test_resolver_follows_wrapper_and_merges_tracking():
    wrapper = ('<Ad id="w"><Wrapper><Impression>https://t.example/wrap-imp</Impression>'
               '<Error>https://t.example/wrap-err</Error>'
               '<VASTAdTagURI>https://inner/vast</VASTAdTagURI></Wrapper></Ad>')
    client = FakeClient({'https://ssp': (200, vast(wrapper)),
                         'https://inner': (200, vast(inline('x', None)))})
    [ad] = NetworkVastResolver(client).resolve('https://ssp/tag', {}, threading.Event())
    assert ad.impressions[0] == 'https://t.example/wrap-imp' and len(ad.impressions) == 2
    assert 'https://t.example/wrap-err' in ad.error_urls


def test_resolver_no_fill_and_unsupported_media():
    assert NetworkVastResolver(FakeClient({'https://ssp': (200, vast())})).resolve(
        'https://ssp/t', {}, threading.Event()) == []
    client = FakeClient({'https://ssp': (200, vast(inline(kind='video/mp4')))})
    with pytest.raises(NetworkAdError) as caught:
        NetworkVastResolver(client).resolve('https://ssp/t', {}, threading.Event())
    assert caught.value.code == 403 and caught.value.error_urls


def test_mp3_sniffing():
    assert looks_like_mp3(MP3) and looks_like_mp3(b'\xff\xfb\x90\x64' + b'\0' * 10)
    assert not looks_like_mp3(b'<html>not audio</html>')


class Clock:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now


def service(*ads, media=MP3):
    client = FakeClient({'https://ssp': (200, vast(*ads)),
                         'https://media.example': (200, media),
                         'https://t.example': (200, b'')})
    clock = Clock()
    events = []
    svc = NetworkAdService(client, SimpleNamespace(emit=events.append), clock=clock)
    svc.base_url = 'http://127.0.0.1:9'
    svc.config = config(max_ads=2)
    return svc, client, clock, events


def beacons(svc, client):
    """Return tracker hits once every queued beacon has been sent."""
    deadline = time.monotonic() + 5
    while svc._beacons.queue.unfinished_tasks and time.monotonic() < deadline:
        time.sleep(.01)
    assert not svc._beacons.queue.unfinished_tasks, 'beacon worker stalled'
    with client.lock:
        return [url for url, _ in client.requests if url.startswith('https://t.example')]


def play(svc, clock, token, until, step=.25):
    position = 0.0
    while position < until:
        clock.now += step
        position = round(position + step, 3)
        svc.observe(token, position)


def test_prepare_never_fires_tracking_and_chains_pod_then_station():
    svc, client, clock, events = service(inline('a', '1'), inline('b', '2'))
    items = svc.prepare(STATION, threading.Event(), 'plugin://plugin.audio.kodi_donor_radio/?mode=play')
    assert len(items) == 2 and svc.media(items[0]['token']) == MP3
    assert svc.token_for(items[0]['url']) == items[0]['token']
    assert beacons(svc, client) == []
    play(svc, clock, items[0]['token'], 30)
    assert svc.finish(items[0]['token'], 'ended') == items[1]['url']
    play(svc, clock, items[1]['token'], 30)
    assert svc.finish(items[1]['token'], 'ended').startswith('plugin://')
    hits = beacons(svc, client)
    for ad in ('a', 'b'):
        assert 'https://t.example/imp?a=%s' % ad in hits
    assert hits.count('https://t.example/e?e=complete') == 2
    assert hits.count('https://t.example/e?e=start') == 2
    svc.close()


def test_quartiles_follow_observed_time_and_stop_never_completes():
    svc, client, clock, events = service(inline('a', '1'))
    [item] = svc.prepare(STATION, threading.Event())
    play(svc, clock, item['token'], 16)
    assert svc.finish(item['token'], 'stopped') is None
    hits = beacons(svc, client)
    assert 'https://t.example/e?e=midpoint' in hits
    assert 'https://t.example/e?e=thirdQuartile' not in hits
    assert 'https://t.example/e?e=complete' not in hits
    timing = [e.details for e in events
              if e.kind == 'network_ad' and e.details.get('outcome') == 'sent']
    assert {d['event'] for d in timing} >= {'impression', 'start', 'firstQuartile', 'midpoint'}
    # Each event fires at or after its target and within one 0.25 s sample.
    assert all(0 <= d['position'] - d['target'] <= .25 for d in timing)
    svc.close()


def test_forward_seek_taints_and_blocks_later_events():
    svc, client, clock, events = service(inline('a', '1'))
    [item] = svc.prepare(STATION, threading.Event())
    play(svc, clock, item['token'], 2)
    clock.now += .25
    svc.observe(item['token'], 29.5)  # Jump to the end.
    svc.finish(item['token'], 'ended')
    hits = beacons(svc, client)
    assert 'https://t.example/e?e=start' in hits
    assert not any(e in url for url in hits for e in ('Quartile', 'midpoint', 'complete'))
    svc.close()


def test_creative_failure_fires_error_and_is_skipped():
    svc, client, clock, events = service(inline('a', '1'), media=b'<html>')
    assert svc.prepare(STATION, threading.Event()) == []
    assert 'https://t.example/err?c=403&a=a' in beacons(svc, client)
    svc.close()


def test_disabled_config_does_not_request():
    svc, client, clock, events = service(inline('a', '1'))
    svc.config = NetworkAdConfig()
    assert svc.prepare(STATION, threading.Event()) == []
    assert client.requests == []
    svc.close()


@contextmanager
def relay(svc):
    server = RelayServer(manager=SessionManager(), network_ads=svc)
    descriptor = server.start()
    try:
        yield server, descriptor
    finally:
        server.close()
        svc.close()


def get(port, path, headers=None, method='GET'):
    conn = http.client.HTTPConnection('127.0.0.1', port, timeout=5)
    try:
        conn.request(method, path, headers=headers or {})
        response = conn.getresponse()
        return response.status, dict(response.getheaders()), response.read()
    finally:
        conn.close()


def test_relay_prepares_through_control_and_serves_ranges():
    svc, client, clock, events = service(inline('a', '1'))
    with relay(svc) as (server, descriptor):
        control = ControlClient(descriptor)
        [item] = control.prepare_network_ads(
            STATION, 'plugin://plugin.audio.kodi_donor_radio/?mode=play&uuid=x&ads=0')
        path = urlsplit(item['url']).path
        status, headers, body = get(descriptor.port, path)
        assert status == 200 and body == MP3 and headers['Content-Type'] == 'audio/mpeg'
        status, headers, body = get(descriptor.port, path, {'Range': 'bytes=10-13'})
        assert status == 206 and body == MP3[10:14]
        assert headers['Content-Range'] == 'bytes 10-13/%d' % len(MP3)
        assert get(descriptor.port, path, {'Range': 'bytes=99999-'})[0] == 416
        status, headers, _ = get(descriptor.port, path, {'Range': 'bytes=99999-'}, 'HEAD')
        assert status == 200 and headers['Content-Length'] == str(len(MP3))
        assert get(descriptor.port, '/network-ads/' + 'a' * 48 + '.mp3')[0] == 404
        with pytest.raises(OSError):
            control.prepare_network_ads(STATION, 'https://elsewhere.example/')
        unauthorized = ControlClient(type(descriptor)(descriptor.port, descriptor.instance_id,
                                                      '0' * 64))
        with pytest.raises(OSError):
            unauthorized.prepare_network_ads(STATION, '')


def test_beacons_carry_only_kodi_user_agent():
    svc, client, clock, events = service(inline('a', '1'))
    [item] = svc.prepare(STATION, threading.Event())
    play(svc, clock, item['token'], 1)
    beacons(svc, client)
    with client.lock:
        sent = [h for url, h in client.requests if url.startswith('https://t.example')]
    assert sent and all(h == {'User-Agent': 'Kodi/21.3'} for h in sent)
    svc.close()


def test_retention_stays_capped_and_drops_finished_items():
    svc, client, clock, events = service(*(inline(str(n), str(n + 1)) for n in range(5)))
    svc.config = config(max_ads=5)
    first = svc.prepare(STATION, threading.Event())
    svc.finish(first[0]['token'], 'stopped')
    for _ in range(3):
        clock.now += 1
        svc.prepare(STATION, threading.Event())
    assert len(svc._items) <= 8
    assert svc.media(first[0]['token']) is None
    svc.close()


def test_concurrent_prepares_are_bounded():
    svc, client, clock, events = service(inline('a', '1'))
    for _ in range(2):
        assert svc._preparing.acquire(blocking=False)
    assert svc.prepare(STATION, threading.Event()) == []
    assert [e.details['event'] for e in events] == ['vast_busy']
    assert client.requests == []
    svc.close()


def test_playback_error_moves_on_but_user_stop_does_not():
    svc, client, clock, events = service(inline('a', '1'), inline('b', '2'))
    follow = 'plugin://plugin.audio.kodi_donor_radio/?mode=play'
    first, second = svc.prepare(STATION, threading.Event(), follow)
    assert svc.finish(first['token'], 'error') == second['url']
    assert 'https://t.example/err?c=405&a=a' in beacons(svc, client)
    assert svc.finish(second['token'], 'switched') is None
    svc.close()


def test_new_prepare_never_evicts_the_pod_on_air():
    svc, client, clock, events = service(*(inline(str(n), str(n + 1)) for n in range(5)))
    svc.config = config(max_ads=5)
    follow = 'plugin://plugin.audio.kodi_donor_radio/?mode=play'
    pod = svc.prepare(STATION, threading.Event(), follow)
    play(svc, clock, pod[0]['token'], 1)
    clock.now += 1
    svc.prepare(STATION, threading.Event(), follow)  # Abandoned second break.
    assert all(svc.media(item['token']) is not None for item in pod)
    assert svc.finish(pod[0]['token'], 'ended') == pod[1]['url']
    svc.close()


def test_explicit_seek_ends_tracking_and_finish_frees_bytes():
    svc, client, clock, events = service(inline('a', '1'))
    [item] = svc.prepare(STATION, threading.Event())
    play(svc, clock, item['token'], 1)
    svc.seeked(item['token'])  # Small seek the clock check would tolerate.
    play(svc, clock, item['token'], 30)
    svc.finish(item['token'], 'ended')
    hits = beacons(svc, client)
    assert not any(e in url for url in hits for e in ('Quartile', 'midpoint', 'complete'))
    assert svc.media(item['token']) is None
    svc.close()


def test_complete_needs_end_near_declared_duration_on_both_sides():
    svc, client, clock, events = service(inline('a', '1'))
    [item] = svc.prepare(STATION, threading.Event())
    play(svc, clock, item['token'], 60)  # File runs twice the declared 30 s.
    svc.finish(item['token'], 'ended')
    hits = beacons(svc, client)
    assert 'https://t.example/e?e=thirdQuartile' in hits
    assert 'https://t.example/e?e=complete' not in hits
    svc.close()


def test_close_cancels_preparation_and_stores_nothing():
    svc, client, clock, events = service(inline('a', '1'))
    svc.close()
    assert svc.prepare(STATION) == []
    assert svc._items == {} and client.requests == []


def test_complete_allows_a_slightly_long_creative():
    svc, client, clock, events = service(inline('a', '1'))
    [item] = svc.prepare(STATION, threading.Event())
    play(svc, clock, item['token'], 32)
    svc.finish(item['token'], 'ended')
    assert 'https://t.example/e?e=complete' in beacons(svc, client)
    svc.close()


def test_unstarted_first_creative_is_claimed_once_and_only_soon():
    svc, client, clock, events = service(inline('a', '1'), inline('b', '2'))
    follow = 'plugin://plugin.audio.kodi_donor_radio/?mode=play'
    first, second = svc.prepare(STATION, threading.Event(), follow)
    assert svc.unstarted_first() is None  # Never handed to Kodi.
    svc.media(first['token'])
    assert svc.unstarted_first() == first['token']
    assert svc.unstarted_first() is None
    assert svc.finish(first['token'], 'error') == second['url']
    first, _ = svc.prepare(STATION, threading.Event(), follow)
    svc.media(first['token'])
    clock.now += 16
    assert svc.unstarted_first() is None
    first, _ = svc.prepare(STATION, threading.Event(), follow)
    svc.observe(first['token'], 0.1)
    assert svc.unstarted_first() is None
    svc.close()


def test_consent_follows_the_gdpr_value_the_request_sends():
    tag = TEST_TAG + '&gdpr=1'
    query = parse_qs(urlsplit(build_request(config(tag_url=tag, gdpr='', consent='abc'),
                                            STATION)[0]).query)
    assert query['gdpr'] == ['1'] and query['consent'] == ['abc']
    tag = TEST_TAG + '&gdpr=0'
    query = parse_qs(urlsplit(build_request(config(tag_url=tag, gdpr='1', consent='abc'),
                                            STATION)[0]).query)
    assert query['gdpr'] == ['0'] and 'consent' not in query


def test_only_the_played_linear_receives_tracking():
    second = ('<Creative><Linear><TrackingEvents>'
              '<Tracking event="start">https://t.example/unplayed</Tracking></TrackingEvents>'
              '<Duration>00:00:15</Duration><MediaFiles><MediaFile delivery="progressive" '
              'type="audio/mpeg">https://media.example/b.mp3</MediaFile></MediaFiles>'
              '</Linear></Creative></Creatives>')
    xml = inline('a', None).replace('</Creatives>', second, 1)
    client = FakeClient({'https://ssp': (200, vast(xml))})
    [ad] = NetworkVastResolver(client).resolve('https://ssp/tag', {}, threading.Event())
    assert ad.media_url == 'https://media.example/a.mp3'
    assert 'https://t.example/unplayed' not in [url for _, url in ad.tracking]
    assert [event for event, _ in ad.tracking] == [
        'start', 'firstQuartile', 'midpoint', 'thirdQuartile', 'complete']


def test_wrapper_tracking_survives_inline_scoping():
    wrapper = ('<Ad id="w"><Wrapper><Impression>https://t.example/wrap-imp</Impression>'
               '<Creatives><Creative><Linear><TrackingEvents>'
               '<Tracking event="start">https://t.example/wrap-start</Tracking>'
               '</TrackingEvents></Linear></Creative></Creatives>'
               '<VASTAdTagURI>https://inner/vast</VASTAdTagURI></Wrapper></Ad>')
    client = FakeClient({'https://ssp': (200, vast(wrapper)),
                         'https://inner': (200, vast(inline('x', None)))})
    [ad] = NetworkVastResolver(client).resolve('https://ssp/tag', {}, threading.Event())
    urls = [url for _, url in ad.tracking]
    assert 'https://t.example/wrap-start' in urls and 'https://t.example/e?e=start' in urls


def test_paused_ad_on_air_outlives_the_age_limit():
    svc, client, clock, events = service(inline('a', '1'), inline('b', '2'))
    follow = 'plugin://plugin.audio.kodi_donor_radio/?mode=play'
    pod = svc.prepare(STATION, threading.Event(), follow)
    play(svc, clock, pod[0]['token'], 1)
    clock.now += 20 * 60  # Paused longer than the retention window.
    svc.prepare(STATION, threading.Event(), follow)
    assert all(svc.media(item['token']) is not None for item in pod)
    assert svc.finish(pod[0]['token'], 'ended') == pod[1]['url']
    svc.close()


@pytest.mark.parametrize('suffix,gdpr,consent', [
    ('&gdpr=', '1', True), ('&gdpr=true', '1', True),
    ('&gdpr=1&gdpr=0', '0', True), ('&gdpr=0&gdpr=1', '', True),
    ('&gdpr=0&gdpr=0', '1', False)])
def test_consent_ignores_invalid_tag_gdpr_and_honours_any_gdpr_1(suffix, gdpr, consent):
    url = build_request(config(tag_url=TEST_TAG + suffix, gdpr=gdpr, consent='abc'), STATION)[0]
    assert ('consent' in parse_qs(urlsplit(url).query)) is consent


def test_newer_selection_supersedes_a_preroll_in_flight():
    from control import SelectionSuperseded
    gate, entered = threading.Event(), threading.Event()
    svc, client, clock, events = service(inline('a', '1'))
    original = client.open
    def slow(url, headers, cancel, deadline=None, allowed_origin=None):
        if url.startswith('https://ssp'):
            entered.set()
            gate.wait(5)
        return original(url, headers, cancel, deadline, allowed_origin)
    client.open = slow
    with relay(svc) as (server, descriptor):
        control = ControlClient(descriptor)
        outcome = []
        def ask():
            try:
                outcome.append(control.prepare_network_ads(STATION, ''))
            except SelectionSuperseded as error:
                outcome.append(error)
        worker = threading.Thread(target=ask)
        worker.start()
        assert entered.wait(5)
        newer = server.manager.begin_selection()  # The listener picks another station.
        gate.set()
        worker.join(10)
        assert isinstance(outcome[0], SelectionSuperseded)
        server.manager.end_selection(newer)


def test_preroll_selection_cancels_an_older_pending_create():
    manager = SessionManager()
    older = manager.begin_selection()
    newer = manager.begin_selection()
    assert older.is_set() and not newer.is_set()
    manager.end_selection(older)  # A stale owner never clears the newer one.
    assert manager._pending is newer
    manager.end_selection(newer)
    assert manager._pending is None
    manager.close()
