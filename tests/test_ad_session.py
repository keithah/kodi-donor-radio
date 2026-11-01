# SPDX-License-Identifier: GPL-2.0-or-later
"""Ad-session segment reads must remain independent of playlist fetches."""
from threading import Event, Lock, RLock, Thread
from types import SimpleNamespace

import pytest

from ad_session import AdSession


def test_midroll_prefix_is_reported_as_live_to_clock_anchor(monkeypatch):
    from types import SimpleNamespace
    from hls import parse_playlist
    from url_policy import origin

    url = 'http://radio.test/live.m3u8'
    playlist = parse_playlist(
        '#EXTM3U\n#EXT-X-TARGETDURATION:3\n#EXT-X-MEDIA-SEQUENCE:0\n'
        '#EXTINF:2.0,\na.ts\n#EXT-X-CUE-OUT:4.0\n'
        '#EXTINF:2.0,\nb.ts\n#EXTINF:2.0,\nc.ts\n#EXT-X-ENDLIST\n', url)
    session = AdSession.__new__(AdSession)
    session._render_lock = Lock()
    session._lock = RLock()
    session._closed = False
    session._terminal = False
    session.source_origin = origin(url)
    session.config = SimpleNamespace(midroll=True)
    session._midroll_done = Event()
    session._cache_window = lambda _playlist: {}
    session._compose_locked = lambda selected, _route, _sources, source_finite=None: (
        selected, source_finite)

    rendered, source_finite = session.render(playlist, lambda identity: identity)

    assert rendered.segments == playlist.segments[:1]
    assert not any(line.strip() == '#EXT-X-ENDLIST' for line in rendered.lines)
    assert source_finite is False


def test_published_source_read_does_not_wait_for_playlist_render_lock():
    session = AdSession.__new__(AdSession)
    session._lock = Lock()
    session._source_by_id = {'published': b'immutable segment'}
    session.creative = SimpleNamespace(read=lambda _resource_id: None)

    result, done = [], Event()
    worker = Thread(target=lambda: (result.append(session.read('published')), done.set()))
    session._lock.acquire()
    worker.start()
    try:
        assert done.wait(.5), 'published-byte read must not wait for the render lock'
    finally:
        session._lock.release()
        worker.join(1)
    assert result == [b'immutable segment']


def test_source_cache_retires_old_unrequested_routes_to_reserve_byte_budget(monkeypatch):
    from collections import OrderedDict
    import ad_session as module
    monkeypatch.setattr(module, 'SOURCE_BYTES', 10)
    monkeypatch.setattr(module, 'SOURCE_SEGMENTS', 4)
    session = AdSession.__new__(AdSession)
    session._lock = RLock()
    session._source = {
        'needed': (SimpleNamespace(id='needed'), b'n' * 4),
        'old': (SimpleNamespace(id='old'), b'o' * 4),
    }
    session._source_by_id = {'needed': b'n' * 4, 'old': b'o' * 4}
    session._published_order = OrderedDict((('old', None), ('needed', None)))
    session._published = {'old', 'needed'}
    session.active_ids = lambda: set()

    session._make_source_room(6, {'needed', 'incoming'})

    assert session._source_by_id == {'needed': b'n' * 4}
    assert session._published == {'needed'}
    assert list(session._published_order) == ['needed']


def test_published_route_retention_is_limited_to_two_output_windows(monkeypatch):
    from collections import OrderedDict
    from types import SimpleNamespace
    import ad_session as module
    from ad_models import OutputSegment, Presentation
    monkeypatch.setattr(module, 'SOURCE_SEGMENTS', 192)
    session = AdSession.__new__(AdSession)
    session._lock = RLock()
    session._limit = 7
    session._resource_limit = 100
    session._published_order = OrderedDict()
    session._published = set()
    session._source = {}
    session._source_by_id = {}
    session.active_ids = lambda: set()
    session.timeline = SimpleNamespace()
    session._midroll = None
    session._prepared = []
    session.creative = SimpleNamespace(unpin=lambda _identity: None)
    session._initial = True
    from datetime import datetime, timezone
    session._program_date_origin = datetime(2026, 10, 27, tzinfo=timezone.utc)
    for batch in range(3):
        segments = tuple(OutputSegment(batch * 7 + index, 0,
                                       'resource-%d' % (batch * 7 + index),
                                       (batch * 7 + index) * 48000,
                                       48000, None) for index in range(7))
        presentation = Presentation(segments, (), False)
        session.timeline.update = lambda *_args, p=presentation: p
        session._compose_locked(SimpleNamespace(segments=()), lambda identity: '/' + identity, {})
    assert len(session._published_order) <= 14


@pytest.mark.parametrize('preparation_error,expected_code', [
    ('ad-error', 403), ('timeout', 402), ('transport', 405)])
def test_creative_preparation_failure_reports_resolved_vast_error_urls(
        preparation_error, expected_code):
    from ad_models import AdError, ResolvedAd
    urls = ('http://127.0.0.1/error?code=[ERRORCODE]',)
    ad = ResolvedAd('creative', 'http://127.0.0.1/media', 5, (), urls)
    events = []
    session = AdSession.__new__(AdSession)
    session.config = SimpleNamespace(vast_url='http://127.0.0.1/vast')
    session.cancel = Event()
    session._lock = RLock()
    session._closed = False
    session.resolver = SimpleNamespace(resolve=lambda *_args: ad)
    def prepare(*_args):
        if preparation_error == 'ad-error':
            raise AdError(403, 'Creative failed')
        if preparation_error == 'timeout':
            raise TimeoutError('slow creative')
        raise OSError('creative unavailable')
    session.creative = SimpleNamespace(prepare=prepare)
    session.on_error = events.append

    assert session._prepare() is None
    assert len(events) == 1
    assert events[0].code == expected_code
    assert events[0].error_urls == urls


def test_vast_resolution_failure_keeps_original_error_without_losing_its_urls():
    from ad_models import AdError
    original = AdError(301, 'Wrapper failed', ('http://127.0.0.1/error',))
    events = []
    session = AdSession.__new__(AdSession)
    session.config = SimpleNamespace(vast_url='http://127.0.0.1/vast')
    session.cancel = Event()
    session._lock = RLock()
    session._closed = False
    session.resolver = SimpleNamespace(resolve=lambda *_args: (_ for _ in ()).throw(original))
    session.creative = SimpleNamespace(prepare=lambda *_args: pytest.fail('prepare after resolver error'))
    session.on_error = events.append
    assert session._prepare() is None
    assert events == [original]


def test_closed_ad_session_suppresses_late_preparation_error_callback():
    from ad_models import AdError
    session = AdSession.__new__(AdSession)
    session.config = SimpleNamespace(vast_url='http://127.0.0.1/vast')
    session.cancel = Event()
    session._lock = RLock()
    session._closed = True
    session.resolver = SimpleNamespace(resolve=lambda *_args: (_ for _ in ()).throw(
        AdError(303, 'Wrapper failed')))
    events = []
    session.on_error = events.append

    assert session._prepare() is None
    assert events == []


def test_source_download_does_not_hold_ad_session_state_lock(monkeypatch):
    import ad_session as module
    from hls import Playlist, Segment

    entered = Event()
    release = Event()

    class Response:
        status = 200
        def __enter__(self): return self
        def __exit__(self, *_args): pass
        def read(self, _size): return b''

    class Client:
        def open(self, *_args, **_kwargs):
            assert not session._lock._is_owned(), 'source network I/O must not hold the state lock'
            entered.set()
            assert release.wait(2)
            return Response()

    session = AdSession.__new__(AdSession)
    session._lock = RLock()
    session._source, session._source_by_id, session._published = {}, {}, set()
    session._limit, session.active_ids = 4, lambda: set()
    session.client, session.cancel = Client(), Event()
    session._closed = session._terminal = False
    monkeypatch.setattr(module, 'inspect_ts', lambda _data: SimpleNamespace(samples=48000))
    playlist = Playlist('http://fixture.test/index.m3u8', (), (), 0, 0, 1,
                        (Segment(0, 0, 1, 0, 'http://fixture.test/segment.ts'),), (), False)

    result, error = [], []
    def load():
        try:
            result.append(session._cache_window(playlist))
        except Exception as exception:
            error.append(exception)
    worker = Thread(target=load)
    worker.start()
    try:
        assert entered.wait(.5), 'source fetch must not hold the state lock'
    finally:
        release.set()
        worker.join(2)
    assert not worker.is_alive() and not error
    assert len(result[0]) == 1


def test_source_transport_error_is_classified_as_retryable():
    from ad_session import SourceUnavailable
    from hls import Playlist, Segment
    from types import SimpleNamespace

    class Client:
        def open(self, *args, **kwargs):
            raise OSError('temporary source transport failure')

    session = AdSession.__new__(AdSession)
    session._lock = RLock()
    session.client, session.cancel = Client(), Event()
    session._source, session._source_by_id, session._published = {}, {}, set()
    session._limit, session.active_ids = 4, lambda: set()
    session._closed = session._terminal = False
    playlist = Playlist('http://fixture.test/index.m3u8', (), (), 0, 0, 6,
                        (Segment(0, 0, 6, 0, 'http://fixture.test/segment.ts'),), (), False)
    with pytest.raises(SourceUnavailable):
        session._cache_window(playlist)


def test_cross_origin_source_segment_is_permanent_without_network_fetch():
    from ad_session import CompositionFailure
    from hls import Playlist, Segment

    calls = []
    class Client:
        def open(self, *args, **kwargs):
            calls.append((args, kwargs))
            raise AssertionError('cross-origin source must be rejected before fetching')

    session = AdSession.__new__(AdSession)
    session._lock = RLock()
    session.client, session.cancel = Client(), Event()
    session._source, session._source_by_id, session._published = {}, {}, set()
    session._limit, session.active_ids = 4, lambda: set()
    session._closed = session._terminal = False
    playlist = Playlist('http://fixture.test/index.m3u8', (), (), 0, 0, 6,
                        (Segment(0, 0, 6, 0, 'http://other.test/segment.ts'),), (), False)
    with pytest.raises(CompositionFailure, match='source_origin_changed'):
        session._cache_window(playlist)
    assert calls == []


def test_source_segment_fetch_uses_one_total_deadline(monkeypatch):
    import ad_session as module
    from ad_session import SourceUnavailable
    from hls import Playlist, Segment

    observed = []
    class Response:
        status = 200
        def __enter__(self): return self
        def __exit__(self, *_args): pass
        def read(self, _size): return b''

    class Client:
        def open(self, url, headers, cancel, deadline=None, allowed_origin=None):
            observed.append((url, headers, deadline, allowed_origin))
            return Response()

    clock = iter((100.0, 100.0 + module.SOURCE_FETCH_SECONDS + .01))
    monkeypatch.setattr(module, 'time', SimpleNamespace(monotonic=lambda: next(clock)))
    session = AdSession.__new__(AdSession)
    session._lock = RLock()
    session._source, session._source_by_id, session._published = {}, {}, set()
    session._limit, session.active_ids = 4, lambda: set()
    session.client, session.cancel = Client(), Event()
    session._closed = session._terminal = False
    playlist = Playlist('http://fixture.test/index.m3u8', (), (), 0, 0, 1,
                        (Segment(0, 0, 1, 0, 'http://fixture.test/segment.ts'),), (), False)

    with pytest.raises(SourceUnavailable):
        session._cache_window(playlist)
    assert observed == [(playlist.segments[0].uri, {}, 100 + module.SOURCE_FETCH_SECONDS,
                         playlist.final_url)]
    assert not session._source


def test_midroll_completion_after_close_tolerates_cleared_cache_and_signals_done():
    from threading import Event, RLock

    session = AdSession.__new__(AdSession)
    session._prepare = lambda: SimpleNamespace(segments=[SimpleNamespace(id='creative')])
    session._lock = RLock()
    session._closed = True
    session._prepared = []
    session._midroll = None
    session._midroll_done = Event()

    def unpin(_resource_id):
        raise KeyError('cache entry already cleared by close')

    session.creative = SimpleNamespace(unpin=unpin)
    session._prepare_midroll()

    assert session._midroll_done.is_set()


def test_program_dates_follow_output_samples_across_discontinuities_and_reload():
    from collections import OrderedDict
    from datetime import datetime, timezone
    from ad_models import OutputSegment, Presentation

    session = AdSession.__new__(AdSession)
    session._program_date_origin = datetime(2026, 10, 27, tzinfo=timezone.utc)
    session._limit = 8
    session._resource_limit = 32
    session._published_order = OrderedDict()
    session._published = set()
    session._source = {}
    session._source_by_id = {}
    session.active_ids = lambda: set()
    session._midroll = None
    session._prepared = []
    session._initial = True
    segments = (OutputSegment(0, 0, 'ad', 0, 96256, 'placement'),
                OutputSegment(1, 1, 'programme', 96256, 96256, None))
    session.timeline = SimpleNamespace(update=lambda *_: Presentation(segments, (), False))
    playlist = SimpleNamespace(segments=())
    first = session._compose_locked(playlist, lambda value: '/' + value, {}).decode()
    session.timeline.update = lambda *_: Presentation(segments[1:], (), False)
    later = session._compose_locked(playlist, lambda value: '/' + value, {}).decode()

    dates = [line for line in first.splitlines() if line.startswith('#EXT-X-PROGRAM-DATE-TIME:')]
    assert dates == ['#EXT-X-PROGRAM-DATE-TIME:2026-10-27T00:00:00.000000Z',
                     '#EXT-X-PROGRAM-DATE-TIME:2026-10-27T00:00:02.005333Z']
    assert '#EXT-X-DISCONTINUITY\n' + dates[1] in first
    assert dates[1] in later
    assert dates[0] not in later


def test_program_date_epoch_starts_at_first_publication_not_session_preparation(tmp_path, monkeypatch):
    from datetime import datetime, timezone
    from ad_models import AdConfig, OutputSegment, Presentation
    import ad_session as module

    class Clock(datetime):
        current = datetime(2026, 10, 27, tzinfo=timezone.utc)

        @classmethod
        def now(cls, _zone):
            return cls.current

    monkeypatch.setattr(module, 'datetime', Clock)
    session = AdSession(AdConfig(True, False, False, '', 'http://127.0.0.1:8766'),
                        object(), tmp_path, Event(), 32, 'http://127.0.0.1:8766')
    try:
        Clock.current = datetime(2026, 10, 27, 0, 10, tzinfo=timezone.utc)
        segment = OutputSegment(0, 0, 'programme', 0, 48000, None)
        session.timeline.update = lambda *_: Presentation((segment,), (), False)
        first = session._compose_locked(SimpleNamespace(segments=()), lambda value: '/' + value, {})
        Clock.current = datetime(2026, 10, 27, 0, 20, tzinfo=timezone.utc)
        second = session._compose_locked(SimpleNamespace(segments=()), lambda value: '/' + value, {})
        expected = b'#EXT-X-PROGRAM-DATE-TIME:2026-10-27T00:10:00.000000Z'
        assert expected in first
        assert expected in second
    finally:
        session.close()
