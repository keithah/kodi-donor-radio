# SPDX-License-Identifier: GPL-2.0-or-later
"""Ad-session segment reads must remain independent of playlist fetches."""
from threading import Event, Lock, RLock, Thread
from types import SimpleNamespace

import pytest

from ad_session import AdSession


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
