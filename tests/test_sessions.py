# SPDX-License-Identifier: GPL-2.0-or-later
import threading
import time

import pytest

from models import Station
from station_resolver import ResolvedSource


class Probe:
    def __init__(self):
        self.closed = False
    def close(self):
        self.closed = True


def manager(**kwargs):
    from sessions import SessionManager
    return SessionManager(resolver=lambda *args: ResolvedSource('http://radio.example/live', 'continuous', Probe(), b'a'), **kwargs)


def station():
    return Station('station', 'Radio', 'http://radio.example/live', 'MP3', False)


def test_switch_cancels_previous_and_invalidates_opaque_urls():
    m = manager()
    first = m.create(station())
    second = m.create(station())
    assert first.cancel.is_set() and first.source.response.closed
    assert len(first.id) >= 32 and first.id != second.id
    assert m.get(first.id) is None
    assert m.get(second.id) is second
    m.close()
    assert second.cancel.is_set()


def test_resource_limit_evicts_inactive_but_keeps_active_and_batch_atomic():
    from sessions import ResourceLimit
    m = manager(resource_limit=4)
    s = m.create(station())
    a = s.register_many(['http://a/1'])[0]
    with s.acquire(a) as active:
        assert active.url == 'http://a/1'
        s.register_many(['http://a/2', 'http://a/3'])
        assert s.resource_count == 4
        assert s.lookup(a) is active
        with pytest.raises(ResourceLimit):
            s.register_many(['http://a/4', 'http://a/5', 'http://a/6'])
        assert s.resource_count == 4
    m.close()


def test_idle_expiry_closes_probe_and_closed_manager_rejects_create():
    m = manager(idle_timeout=.01)
    s = m.create(station())
    time.sleep(.02)
    m.expire()
    assert m.get(s.id) is None and s.source.response.closed
    m.close()
    with pytest.raises(RuntimeError):
        m.create(station())


def test_continuous_claim_only_once_and_unknown_resource_rejected():
    m = manager()
    s = m.create(station())
    assert s.claim_continuous()
    assert not s.claim_continuous()
    assert s.lookup('forged') is None
    m.close()


def test_close_cancels_inflight_resolution_without_waiting_for_create_lock():
    from sessions import SessionManager
    entered, stopped = threading.Event(), threading.Event()
    def resolve(url, client, cancel):
        entered.set()
        cancel.wait(2)
        stopped.set()
        raise OSError('cancelled')
    m = SessionManager(resolver=resolve)
    def create():
        with pytest.raises(OSError):
            m.create(station())
    worker = threading.Thread(target=create)
    worker.start()
    assert entered.wait(1)
    m.close()
    assert stopped.wait(1)
    worker.join(1)
    assert not worker.is_alive()


@pytest.mark.parametrize('selections', [2, 3])
@pytest.mark.parametrize('close_manager', [False, True])
def test_newest_selection_cancels_running_and_queued_creates(selections,close_manager):
    from sessions import SessionManager
    entered, cancelled, release = threading.Event(), threading.Event(), threading.Event()
    queued = {str(index): threading.Event() for index in range(selections)}
    calls, successes, failures, probes = [], {}, {}, []

    class ObservedLock:
        # Signal queue entry without depending on scheduler timing or FIFO locks.
        def __init__(self): self.lock = threading.Lock()
        def __enter__(self):
            queued[threading.current_thread().name].set()
            self.lock.acquire()
        def __exit__(self, *args): self.lock.release()

    def resolve(url, client, cancel):
        calls.append(url)
        probe = Probe(); probes.append(probe)
        if url.endswith('/0'):
            entered.set()
            if cancel.wait(2): cancelled.set()
            release.wait(2)
        # Simulate an upstream completion racing cancellation: it still owns a probe.
        return ResolvedSource(url, 'continuous', probe, b'a')

    m = SessionManager(resolver=resolve)
    m._create_lock = ObservedLock()
    def create(index):
        try:
            successes[index] = m.create(Station(str(index), 'Radio',
                'http://radio.example/%d' % index, 'MP3', False))
        except Exception as error:
            failures[index] = error
    workers = []
    try:
        for index in range(selections):
            worker = threading.Thread(target=create, args=(index,), name=str(index))
            workers.append(worker); worker.start()
            assert queued[str(index)].wait(1)
            if index == 0:
                assert entered.wait(1)
            elif index == 1:
                assert cancelled.wait(1), 'New selection did not cancel pending resolution'
        if close_manager:
            m.close()
        release.set()
        for worker in workers:
            worker.join(1)
            assert not worker.is_alive()
        assert probes[0].closed
        assert all(isinstance(error, RuntimeError) for error in failures.values())
        if close_manager:
            assert not successes
            assert set(failures) == set(range(selections))
            assert calls == ['http://radio.example/0']
        else:
            latest = selections - 1
            assert set(successes) == {latest}
            assert set(failures) == set(range(latest))
            assert calls == ['http://radio.example/0', 'http://radio.example/%d' % latest]
            assert m.get(successes[latest].id) is successes[latest]
            assert not probes[-1].closed
    finally:
        release.set()
        m.close()
        for worker in workers: worker.join(3)
    assert all(probe.closed for probe in probes)
