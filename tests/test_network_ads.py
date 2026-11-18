# SPDX-License-Identifier: GPL-2.0-or-later
import http.client
import json
import socket
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
        assert svc.selection_for(item['token']) == server.manager.selection_count() == 1
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
    query = parse_qs(urlsplit(url).query, keep_blank_values=True)
    assert ('consent' in query) is consent
    assert all(value in ('0', '1') for value in query.get('gdpr', []))
    if suffix in ('&gdpr=', '&gdpr=true'):
        assert query['gdpr'] == [gdpr]  # The configured value replaces an invalid one.


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


def test_release_reports_a_supersession_that_lands_before_it():
    manager = SessionManager()
    mine = manager.begin_selection()
    newer = manager.begin_selection()
    assert manager.end_selection(mine) is True and manager._pending is newer
    assert manager.end_selection(newer) is False and manager._pending is None
    manager.close()


def test_a_preview_is_not_a_deliberate_selection():
    manager = SessionManager()
    gate = threading.Event()
    def resolver(url, client, cancel):
        gate.wait(5)
        raise OSError('offline')
    manager.resolver = resolver
    worker = threading.Thread(target=lambda: pytest.raises(Exception, manager.create_preview, STATION))
    worker.start()
    deadline = time.monotonic() + 5
    while manager._pending is None and time.monotonic() < deadline:
        time.sleep(.01)
    assert manager._pending is not None and manager.selection_count() == 0
    gate.set(); worker.join(5)
    selection = manager.begin_selection()
    assert manager.selection_count() == 1
    manager.end_selection(selection)
    manager.close()


def test_every_deliberate_create_counts_as_a_selection():
    manager = SessionManager()
    manager.resolver = lambda url, client, cancel: (_ for _ in ()).throw(OSError('offline'))
    with pytest.raises(OSError):
        manager.create(STATION)
    assert manager.selection_count() == 1
    manager.end_selection(manager.begin_selection())
    assert manager.selection_count() == 2
    manager.close()


def test_newer_selection_stops_serving_unstarted_creatives():
    svc, client, clock, events = service(inline('a', '1'), inline('b', '2'))
    with relay(svc) as (server, descriptor):
        follow = 'plugin://plugin.audio.kodi_donor_radio/?mode=play&uuid=x&ads=0'
        first, second = ControlClient(descriptor).prepare_network_ads(STATION, follow)
        assert svc.media(first['token']) == MP3
        play(svc, clock, first['token'], 1)  # The first creative is on air.
        server.manager.end_selection(server.manager.begin_selection())  # The listener chose again.
        assert svc.media(first['token']) == MP3  # On air: keeps streaming.
        assert svc.media(second['token']) is None  # Never started: refused.
        status = get(descriptor.port, urlsplit(second['url']).path)[0]
        assert status == 404


def test_station_continuation_carries_and_checks_its_selection():
    from control import SelectionSuperseded
    svc, client, clock, events = service(inline('a', '1'))
    with relay(svc) as (server, descriptor):
        control = ControlClient(descriptor)
        follow = 'plugin://plugin.audio.kodi_donor_radio/?mode=play&uuid=x&ads=0'
        control.prepare_network_ads(STATION, follow)
        [item] = svc._items.values()
        assert item.next_url.startswith(follow + '&continuation=')
        token = parse_qs(urlsplit(item.next_url).query)['continuation'][0]
        server.manager.end_selection(server.manager.begin_selection())  # Newer choice.
        with pytest.raises(SelectionSuperseded):
            control.create(STATION, continuation=token)
        assert server.manager.selection_count() == 2  # The stale continuation did not count.
        payload = {k: getattr(STATION, k) for k in ('uuid', 'name', 'url', 'codec', 'hls')}
        with pytest.raises(OSError) as caught:
            control._request('POST', '/sessions', dict(payload, continuation='X' * 32))
        assert not isinstance(caught.value, SelectionSuperseded)  # A malformed token is a 400.


def test_replaying_a_station_continuation_is_a_plain_choice():
    from sessions import SelectionSuperseded
    manager = SessionManager()
    manager.resolver = lambda url, client, cancel: (_ for _ in ()).throw(OSError('offline'))
    first = manager.register_continuation(manager.begin_selection().selection)
    manager.end_selection(manager._pending)
    with pytest.raises(OSError):
        manager.create(STATION, continuation=first)  # Its hand-off runs (stream offline here).
    for _ in range(3):
        manager.end_selection(manager.begin_selection())  # Other stations, other breaks.
    with pytest.raises(OSError):
        manager.create(STATION, continuation=first)  # The listener replays that item.
    with pytest.raises(OSError):
        manager.create(STATION, continuation='0' * 32)  # A URL from an earlier service run.
    stale = manager.register_continuation(manager.begin_selection().selection)
    manager.end_selection(manager._pending)
    manager.end_selection(manager.begin_selection())
    with pytest.raises(SelectionSuperseded):
        manager.create(STATION, continuation=stale)  # A hand-off behind a newer choice.
    manager.close()

def test_reserved_selection_stops_an_older_break_before_the_preroll_request():
    from control import SelectionSuperseded
    svc, client, clock, events = service(inline('a', '1'), inline('b', '2'))
    with relay(svc) as (server, descriptor):
        control = ControlClient(descriptor)
        follow = 'plugin://plugin.audio.kodi_donor_radio/?mode=play&uuid=x&ads=0'
        first, second = control.prepare_network_ads(STATION, follow)  # Break A: selection 1.
        play(svc, clock, first['token'], 1)
        reserved = control.reserve_selection()  # The listener picks B; lookup not done yet.
        assert reserved == 2 and server.manager.selection_count() == 2
        assert svc.media(second['token']) is None  # A's unstarted creative is refused already.
        # B's pre-roll request takes the reserved place instead of a new one.
        items = control.prepare_network_ads(STATION, follow, selection=reserved)
        assert items and server.manager.selection_count() == 2
        assert svc.selection_for(items[0]['token']) == 2
        # A request whose reservation was overtaken is superseded.
        stale = control.reserve_selection()
        control.reserve_selection()
        with pytest.raises(SelectionSuperseded):
            control.prepare_network_ads(STATION, follow, selection=stale)


def test_reserved_begin_selection_semantics():
    manager = SessionManager()
    pending = manager.begin_selection()
    reserved = manager.reserve_selection()
    assert pending.is_set() and reserved == 2  # A reservation cancels in-flight work.
    current = manager.begin_selection(reserved)
    assert not current.is_set() and current.selection == 2 and manager.selection_count() == 2
    assert manager.end_selection(current) is False
    late = manager.begin_selection(1)  # Reserved before the newest selection: already lost.
    assert late.is_set() and manager.end_selection(late) is True
    manager.close()


def test_no_fill_fallback_cannot_overtake_a_newer_reservation():
    from sessions import SelectionSuperseded
    manager = SessionManager()
    manager.resolver = lambda url, client, cancel: (_ for _ in ()).throw(OSError('offline'))
    a = manager.reserve_selection()
    manager.end_selection(manager.begin_selection(a))  # A's pre-roll: no fill.
    b = manager.reserve_selection()  # The listener picks B; its lookup is slow.
    with pytest.raises(SelectionSuperseded):
        manager.create(STATION, reserved=a)  # A's late station fallback yields.
    assert manager.selection_count() == b
    current = manager.begin_selection(b)  # B still owns its place.
    assert not current.is_set()
    manager.end_selection(current)
    with pytest.raises(OSError):
        manager.create(STATION, reserved=b)  # B's own fallback proceeds (stream offline here).
    assert manager.selection_count() == b  # It took the reserved place, not a new one.
    manager.close()


_FOLLOW_ADS0 = 'plugin://plugin.audio.kodi_donor_radio/?mode=play&uuid=x&ads=0'


def _short_post(descriptor, path, body, declared):
    """Declare `declared` bytes, send only `body`, then half-close the request."""
    sock = socket.create_connection(('127.0.0.1', descriptor.port), timeout=5)
    try:
        sock.sendall(('POST %s HTTP/1.1\r\nHost: 127.0.0.1\r\nAuthorization: Bearer %s\r\n'
                      'Content-Type: application/json\r\nContent-Length: %d\r\n\r\n'
                      % (path, descriptor.control_token, declared)).encode() + body)
        sock.shutdown(socket.SHUT_WR)
        data = b''
        while True:
            chunk = sock.recv(65536)
            if not chunk:
                break
            data += chunk
        return int(data.split(b' ', 2)[1])
    finally:
        sock.close()


def test_range_unit_is_case_insensitive():
    svc, *_ = service(inline('a', '1'))
    with relay(svc) as (server, descriptor):
        [item] = ControlClient(descriptor).prepare_network_ads(STATION, _FOLLOW_ADS0)
        status, _, body = get(descriptor.port, urlsplit(item['url']).path, {'Range': 'Bytes=10-13'})
        assert status == 206 and body == MP3[10:14]


def test_short_selection_body_reserves_nothing():
    svc, *_ = service(inline('a', '1'))
    with relay(svc) as (server, descriptor):
        assert _short_post(descriptor, '/selections', b'{}', 3) == 400
        assert server.manager.selection_count() == 0


@pytest.mark.parametrize('path', ['/network-ads', '/sessions'])
def test_short_body_starts_no_selection(path):
    svc, *_ = service(inline('a', '1'))
    with relay(svc) as (server, descriptor):
        payload = {key: getattr(STATION, key) for key in ('uuid', 'name', 'url', 'codec', 'hls')}
        if path == '/network-ads':
            payload.update({key: getattr(STATION, key) for key in ('country_code', 'language', 'tags')})
            payload['follow_url'] = _FOLLOW_ADS0
        body = json.dumps(payload).encode()
        assert _short_post(descriptor, path, body, len(body) + 1) == 400
        assert server.manager.selection_count() == 0


# CodeRabbit round on 9cc2d6a.


def test_tag_with_invalid_port_is_rejected():
    with pytest.raises(ValueError):
        validate_tag_url('https://ssp.audiocdn.com:abc/vast/tag?zoneId=z&position=pre')


def test_unstarted_follower_of_superseded_break_is_not_served():
    svc, client, clock, events = service(inline('a', '1'), inline('b', '2'))
    count = [1]
    svc.current_selection = lambda: count[0]
    items = svc.prepare(STATION, threading.Event(), 'plugin://x', selection=1)
    assert len(items) == 2
    first, second = items[0]['token'], items[1]['token']
    assert svc.media(first)
    svc.observe(first, .5)
    svc.finish(first, 'ended')  # marks second live for retention
    count[0] = 2  # listener picks again before Kodi opens the second creative
    assert svc.media(second) is None


def test_stopped_pod_releases_unplayed_creatives():
    svc, client, clock, events = service(inline('a', '1'), inline('b', '2'))
    items = svc.prepare(STATION, threading.Event(), 'plugin://x', selection=1)
    first, second = items[0]['token'], items[1]['token']
    svc.observe(first, .5)
    svc.finish(first, 'stopped')
    clock.now += 3600
    held = svc._items.get(second)
    assert held is None or held.data == b''


def _resolver(body):
    client = FakeClient({'https://ssp': (200, body), 'https://wrap.example': (200, body)})
    return NetworkVastResolver(client), client


def test_later_https_mp3_rendition_is_used():
    ad = inline('a', '1', media='http://media.example/a.mp3').replace(
        '</MediaFiles>',
        '<MediaFile delivery="progressive" type="audio/mpeg">'
        '<![CDATA[https://media.example/b.mp3]]></MediaFile></MediaFiles>')
    resolver, _ = _resolver(vast(ad))
    ads = resolver.resolve(TEST_TAG, {}, threading.Event(), 1)
    assert ads and not isinstance(ads[0], NetworkAdError)
    assert ads[0].media_url == 'https://media.example/b.mp3'


def test_malformed_tracking_url_does_not_discard_pod():
    bad = inline('a', '1').replace('<![CDATA[https://t.example/e?e=midpoint]]>',
                                   '<![CDATA[http://[bad/track]]>')
    resolver, _ = _resolver(vast(bad, inline('b', '2')))
    ads = resolver.resolve(TEST_TAG, {}, threading.Event(), 2)
    assert len(ads) == 2 and not isinstance(ads[1], NetworkAdError)


def test_wrapper_no_ad_fires_root_error_url():
    wrapper = ('<?xml version="1.0"?><VAST version="4.2"><Ad id="w" sequence="1"><Wrapper>'
               '<AdSystem>S</AdSystem><Error><![CDATA[https://t.example/werr]]></Error>'
               '<VASTAdTagURI><![CDATA[https://wrap.example/next]]></VASTAdTagURI>'
               '</Wrapper></Ad></VAST>').encode()
    empty = ('<?xml version="1.0"?><VAST version="4.3">'
             '<Error><![CDATA[https://t.example/noad]]></Error></VAST>').encode()
    client = FakeClient({'https://ssp': (200, wrapper), 'https://wrap.example': (200, empty)})
    with pytest.raises(NetworkAdError) as info:
        NetworkVastResolver(client).resolve(TEST_TAG, {}, threading.Event(), 1)
    assert info.value.code == 303
    assert 'https://t.example/noad' in info.value.error_urls


def test_prepare_cancelled_before_publishing_stores_nothing():
    svc, client, clock, events = service(inline('a', '1'), inline('b', '2'))
    cancel = threading.Event()
    real_open = client.open
    def open_then_cancel(url, headers, c, deadline=None, allowed_origin=None):
        r = real_open(url, headers, c, deadline, allowed_origin)
        if 'b.mp3' in url or url.endswith('a.mp3') and False:
            pass
        return r
    client.open = open_then_cancel
    # Cancel after the last download but before publish: simulate via clock hook.
    orig_lock = svc._lock
    class Hook:
        def __enter__(self):
            cancel.set()
            return orig_lock.__enter__()
        def __exit__(self, *a):
            return orig_lock.__exit__(*a)
    svc._lock = Hook()
    items = svc.prepare(STATION, cancel, 'plugin://x', selection=1)
    svc._lock = orig_lock
    assert items == [] and not svc._items


def test_relay_keeps_the_break_station_until_its_continuation_is_used():
    svc, *_ = service(inline('a', '1'))
    with relay(svc) as (server, descriptor):
        control = ControlClient(descriptor)
        [item] = control.prepare_network_ads(
            STATION, 'plugin://plugin.audio.kodi_donor_radio/?mode=play&uuid=x&ads=0')
        token = next(reversed(server.manager._continuations))
        kept = control.continuation_station(token)
        assert kept == {key: getattr(STATION, key) for key in ('uuid', 'name', 'url', 'codec', 'hls')}
        assert control.continuation_station('f' * 32) is None
        assert get(descriptor.port, '/continuations/' + token)[0] == 401


def test_non_ascii_sequence_does_not_discard_the_pod():
    resolver, _ = _resolver(vast(inline('a', '1'), inline('b', '²')))
    ads = resolver.resolve(TEST_TAG, {}, threading.Event(), 2)
    assert [ad.ad_id for ad in ads] == ['a']
