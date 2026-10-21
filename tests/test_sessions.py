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
