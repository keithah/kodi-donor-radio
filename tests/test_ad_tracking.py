# SPDX-License-Identifier: GPL-2.0-or-later
"""Tracking requires observed playback, never media preparation or playlist fetches."""
import threading
import time

import pytest

from ad_models import AdError, OutputSegment, Placement, Presentation, ResolvedAd, TrackingUrl
from ad_tracking import AdTracker, BeaconWorker, PlaybackSample, _CancelOrStop, enqueue_ad_error


URL = "http://fixture.test/track"
EVENTS = ("impression", "start", "firstQuartile", "midpoint", "thirdQuartile", "complete")


def test_beacon_cancel_adapter_waits_for_either_event_or_times_out():
    cancel, stop = threading.Event(), threading.Event()
    combined = _CancelOrStop(cancel, stop)
    assert not combined.wait(.001)
    cancel.set()
    assert combined.wait(0)
    cancel.clear()
    stop.set()
    assert combined.wait(0)


def presentation(start=0, duration=4, placement_id="p1", url=URL, ended=False):
    ad = ResolvedAd("creative-raw-id", "http://fixture.test/media", duration,
                    tuple(TrackingUrl(event, url + "/" + event) for event in EVENTS), ())
    placement = Placement(placement_id, ad, round(start * 48000), round((start + duration) * 48000))
    segment = OutputSegment(0, 0, "segment", 0, round((start + duration + 5) * 48000), None)
    return Presentation((segment,), (placement,), ended)


def sample(position, clock, **changes):
    data = dict(session_id="session", position_seconds=position, monotonic_seconds=clock,
                playing=True, paused=False, seeking=False, mapped=True)
    data.update(changes)
    return PlaybackSample(**data)


def tracker():
    diagnostics, queued = [], []
    instance = AdTracker("session", diagnostics.append,
                         lambda placement_id, event, url: queued.append((placement_id, event, url)) or True)
    return instance, diagnostics, queued


def test_prefetch_and_first_late_sample_do_not_count_as_playback():
    instance, _, queued = tracker()
    view = presentation()
    instance.observe(view, sample(1.0, 10))
    assert queued == []


def test_caching_sample_cannot_start_or_advance_an_ad():
    instance, _, queued = tracker()
    view = presentation()
    instance.observe(view, sample(0.0, 10.0, caching=True))
    instance.observe(view, sample(0.3, 10.3, caching=True))
    assert queued == []


def test_continuous_sample_slightly_late_across_ad_start_records_impression():
    instance, _, queued = tracker()
    view = presentation(start=1.0)
    instance.observe(view, sample(.98, 10.0))
    instance.observe(view, sample(1.28, 10.3))
    assert [event for _, event, _ in queued] == ['impression', 'start']


def test_first_good_sample_near_preroll_start_counts_after_unmapped_sample():
    instance, _, queued = tracker()
    view = presentation()
    instance.observe(view, sample(0, 0, mapped=False))
    instance.observe(view, sample(.1, .1))
    assert [event for _, event, _ in queued] == ['impression', 'start']


def test_250ms_observations_emit_once_at_each_played_milestone():
    instance, diagnostics, queued = tracker()
    view = presentation()
    for tick in range(18):
        instance.observe(view, sample(tick * .25, tick * .25))
    assert [event for _, event, _ in queued] == list(EVENTS)
    instance.observe(view, sample(4.25, 4.25))
    assert len(queued) == 6
    assert [item["event"] for item in diagnostics] == list(EVENTS)


def test_long_stationary_clock_taints_current_ad_even_if_small_steps_resume():
    instance, _, queued = tracker()
    view = presentation()
    positions = (0, .25, .25, .25, .25, .25, .25, .5, .75, 1, 1.25, 1.5,
                 1.75, 2, 2.25, 2.5, 2.75, 3, 3.25, 3.5, 3.75, 4)
    for tick, position in enumerate(positions):
        instance.observe(view, sample(position, tick * .25))
    assert [event for _, event, _ in queued] == ["impression", "start"]


def test_short_stationary_sample_does_not_taint_continuous_playback():
    instance, _, queued = tracker()
    view = presentation()
    positions = (0, .25, .25, .5, .75, 1, 1.25, 1.5, 1.75, 2, 2.25,
                 2.5, 2.75, 3, 3.25, 3.5, 3.75, 4)
    for tick, position in enumerate(positions):
        instance.observe(view, sample(position, tick * .25))
    assert [event for _, event, _ in queued] == list(EVENTS)


@pytest.mark.parametrize("recovery_cached", (False, True))
@pytest.mark.parametrize("gap_seconds, expected_events", (
    (.75, EVENTS),
    (1.0, ("impression", "start")),
))
def test_advancing_cache_gap_taints_only_intersected_ad_after_close_recovery(
        gap_seconds, expected_events, recovery_cached):
    instance, _, queued = tracker()
    first = presentation()
    later = presentation(start=5, placement_id="later")
    view = Presentation(first.segments, first.placements + later.placements, False)
    instance.observe(view, sample(.05, 0))
    assert [event for _, event, _ in queued] == ["impression", "start"]
    instance.observe(view, sample(.05, .25, caching=True))
    assert [event for _, event, _ in queued] == ["impression", "start"]
    # The retained clock advances only 50 ms after a gap. The played-sample
    # proximity guard alone would accept recovery even after a full second.
    instance.observe(view, sample(.10, .25 + gap_seconds, caching=recovery_cached))
    assert [event for _, event, _ in queued] == ["impression", "start"]
    instance.observe(view, sample(.15, .5 + gap_seconds))
    for tick in range(1, 37):
        instance.observe(view, sample(.15 + tick * .25, .5 + gap_seconds + tick * .25))
    assert [event for placement, event, _ in queued if placement == "p1"] == list(expected_events)
    assert [event for placement, event, _ in queued if placement == "later"] == list(EVENTS)


def test_pause_buffering_and_unmapped_samples_break_crossing_evidence():
    instance, _, queued = tracker()
    view = presentation()
    instance.observe(view, sample(0, 0))
    instance.observe(view, sample(.25, .25))
    instance.observe(view, sample(1, 1, paused=True))
    instance.observe(view, sample(2, 2, playing=False))
    instance.observe(view, sample(3, 3, mapped=False))
    instance.observe(view, sample(4, 4))
    assert [event for _, event, _ in queued] == ["impression", "start"]


@pytest.mark.parametrize("unavailable_count", (1, 3))
@pytest.mark.parametrize("gap_seconds, expected_events", (
    (.5, EVENTS),
    (.75, EVENTS),
    (1.0, ("impression", "start")),
))
def test_unavailable_clock_gap_taints_only_intersected_ad_after_close_recovery(
        gap_seconds, expected_events, unavailable_count):
    instance, _, queued = tracker()
    first = presentation()
    later = presentation(start=5, placement_id="later")
    view = Presentation(first.segments, first.placements + later.placements, False)
    instance.observe(view, sample(.05, 0))
    assert [event for _, event, _ in queued] == ["impression", "start"]
    for tick in range(1, unavailable_count + 1):
        instance.observe(view, sample(0, gap_seconds * tick / (unavailable_count + 1),
                                      playing=False, mapped=False, caching=True))
        assert [event for _, event, _ in queued] == ["impression", "start"]
    # Recovery advances only 50 ms, inside the played-position proximity guard.
    instance.observe(view, sample(.10, gap_seconds))
    assert [event for _, event, _ in queued] == ["impression", "start"]
    for tick in range(1, 37):
        instance.observe(view, sample(.10 + tick * .25, gap_seconds + tick * .25))
    assert [event for placement, event, _ in queued if placement == "p1"] == list(expected_events)
    assert [event for placement, event, _ in queued if placement == "later"] == list(EVENTS)


@pytest.mark.parametrize("transition, expected_events", (
    ({"paused": True}, EVENTS),
    ({"seeking": True}, EVENTS),
    ({"speed_changed": True}, ("impression", "start")),
))
def test_unmapped_explicit_transition_preserves_applicable_ambiguity(transition, expected_events):
    instance, _, queued = tracker()
    view = presentation()
    instance.observe(view, sample(.05, 0))
    instance.observe(view, sample(0, .25, playing=False, mapped=False, caching=True))
    instance.observe(view, sample(0, .5, playing=False, mapped=False, **transition))
    instance.observe(view, sample(0, .75, playing=False, mapped=False, caching=True))
    assert [event for _, event, _ in queued] == ["impression", "start"]
    instance.observe(view, sample(.10, 1.0))
    for tick in range(1, 17):
        instance.observe(view, sample(.10 + tick * .25, 1.0 + tick * .25))
    assert [event for _, event, _ in queued] == list(expected_events)


def test_unmapped_speed_transition_taints_intersected_ads_on_cached_recovery():
    instance, _, queued = tracker()
    first = presentation(duration=1)
    crossed = presentation(start=1.2, duration=.1, placement_id='crossed')
    later = presentation(start=2, duration=1, placement_id='later')
    view = Presentation(first.segments, first.placements + crossed.placements + later.placements, False)
    instance.observe(view, sample(.05, 0))
    instance.observe(view, sample(0, .25, mapped=False, speed_changed=True))
    instance.observe(view, sample(0, .5, mapped=False, paused=True))
    assert [event for _, event, _ in queued] == ['impression', 'start']
    # Mapped cache evidence locates the unresolved interval without proving
    # playback. It must also taint an ad entirely crossed during the gap.
    instance.observe(view, sample(1.5, .75, caching=True))
    assert instance._progress['p1'].ambiguous is True
    assert instance._progress['crossed'].ambiguous is True
    assert [event for _, event, _ in queued] == ['impression', 'start']
    for tick in range(7):
        instance.observe(view, sample(1.5 + tick * .25, 1 + tick * .25))
    assert [event for placement, event, _ in queued if placement == 'p1'] == ['impression', 'start']
    assert [event for placement, event, _ in queued if placement == 'crossed'] == []
    assert [event for placement, event, _ in queued if placement == 'later'] == list(EVENTS)


def test_unmapped_clock_jump_taints_ad_before_tracking_resumes():
    instance, _, queued = tracker()
    view = presentation()
    instance.observe(view, sample(0, 0))
    instance.observe(view, sample(1, 1, mapped=False))
    instance.observe(view, sample(2, 2, playing=False, mapped=False))
    instance.observe(view, sample(3, 3, mapped=False))
    for tick in range(1, 5):
        position = 3 + tick * .25
        instance.observe(view, sample(position, 3 + tick * .25))
    assert [event for _, event, _ in queued] == ["impression", "start"]


def test_gap_seek_backwards_and_jump_over_ad_do_not_backfill():
    for interruption in (sample(1.5, 1.5), sample(1.5, .5, seeking=True),
                         sample(0, .5), sample(4.25, .5)):
        instance, _, queued = tracker()
        view = presentation()
        instance.observe(view, sample(0, 0))
        instance.observe(view, interruption)
        assert [event for _, event, _ in queued] == ["impression", "start"]


def test_clock_jump_taints_intersected_ad_but_not_later_placements():
    instance, _, queued = tracker()
    first = presentation(start=0, duration=4, placement_id="first")
    second_ad = ResolvedAd("later", "http://fixture.test/later", 4.0,
                           tuple(TrackingUrl(event, URL + "/later/" + event)
                                 for event in EVENTS), ())
    second = Placement("later", second_ad, 5 * 48000, 9 * 48000)
    view = Presentation(first.segments, (first.placements[0], second), False)
    instance.observe(view, sample(0, 0))
    instance.observe(view, sample(.25, .25))
    instance.observe(view, sample(2.25, .5))  # Ambiguous jump lands inside first placement.
    for tick in range(1, 28):
        position = 2.25 + tick * .25
        instance.observe(view, sample(position, .5 + tick * .25))
    first_events = [event for placement, event, _ in queued if placement == "first"]
    later_events = [event for placement, event, _ in queued if placement == "later"]
    assert first_events == ["impression", "start"]
    assert later_events == list(EVENTS)


def test_seek_to_near_start_after_unmapped_playback_cannot_create_new_impression():
    instance, _, queued = tracker()
    view = presentation()
    instance.observe(view, sample(3, 0, mapped=False))
    instance.observe(view, sample(.1, .25))
    assert queued == []


def test_midroll_uses_placement_relative_start_and_separate_anchors():
    instance, _, queued = tracker()
    view = presentation(start=20)
    instance.observe(view, sample(19.75, 0))
    instance.observe(view, sample(20, .25))
    instance.observe(view, sample(21, 1.25))  # gap cannot prove quartile
    assert [event for _, event, _ in queued] == ["impression", "start"]


def test_source_end_does_not_synthesize_ad_completion_without_crossing():
    instance, _, queued = tracker()
    view = presentation()
    for tick in range(16):
        instance.observe(view, sample(tick * .25, tick * .25))
    instance.finish("source_end")
    assert "complete" not in [event for _, event, _ in queued]


def test_seek_near_end_then_stationary_samples_cannot_complete_on_callback():
    instance, _, queued = tracker()
    view = presentation()
    instance.observe(view, sample(0, 0))
    instance.observe(view, sample(3.9, 1, seeking=True))
    instance.observe(view, sample(3.9, 1.25))
    instance.observe(view, sample(3.9, 1.5))
    instance.finish("source_end")
    assert "complete" not in [event for _, event, _ in queued]


def test_cancel_before_end_never_completes():
    instance, _, queued = tracker()
    view = presentation()
    instance.observe(view, sample(0, 0))
    instance.observe(view, sample(3.75, 3.75))
    instance.finish("cancelled")
    assert "complete" not in [event for _, event, _ in queued]


class FakeResponse:
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return None

    def read(self, *_):
        return b""


class FakeClient:
    def __init__(self, fail=False):
        self.calls = []
        self.fail = fail

    def open(self, url, headers, cancel, deadline=None, allowed_origin=None):
        self.calls.append((url, deadline, allowed_origin))
        if self.fail:
            raise TimeoutError("ambiguous delivery")
        return FakeResponse()


def test_worker_deduplicates_per_placement_and_does_not_retry_ambiguous_failure():
    client = FakeClient(fail=True)
    worker = BeaconWorker(client, threading.Event(), "http://fixture.test")
    assert worker.enqueue("p1", "start", URL)
    assert not worker.enqueue("p1", "start", URL)
    worker.close()
    assert len(client.calls) == 1
    assert client.calls[0][2] == "http://fixture.test"
    assert client.calls[0][1] <= time.monotonic() + 2
    assert worker.failures == 1


def test_worker_allows_same_url_for_later_placement():
    client = FakeClient()
    worker = BeaconWorker(client, threading.Event(), "http://fixture.test")
    assert worker.enqueue("p1", "start", URL)
    assert worker.enqueue("p2", "start", URL)
    worker.close()
    assert len(client.calls) == 2


def test_worker_rejects_credentials_even_on_fixture_origin():
    client = FakeClient()
    worker = BeaconWorker(client, threading.Event(), "http://fixture.test")
    assert not worker.enqueue("p1", "start", "http://userinfo@fixture.test/track")
    worker.close()
    assert client.calls == []


def test_error_beacons_inherit_endpoints_and_substitute_fixed_code_once():
    client = FakeClient()
    worker = BeaconWorker(client, threading.Event(), "http://fixture.test")
    error = AdError(302, "wrapper cycle", (URL + "?e=[ERRORCODE]", URL + "?e=[ERRORCODE]"))
    assert enqueue_ad_error(worker, "p1", error) == 1
    worker.close()
    assert [call[0] for call in client.calls] == [URL + "?e=302"]


def test_error_beacon_uses_spec_code_for_unsupported_ad_and_bad_media():
    client = FakeClient()
    worker = BeaconWorker(client, threading.Event(), "http://fixture.test")
    assert enqueue_ad_error(worker, "p1", AdError(
        403, "Unsupported VAST pod", (URL + "?e=[ERRORCODE]",))) == 1
    assert enqueue_ad_error(worker, "p2", AdError(
        403, "Unsupported AAC profile", (URL + "?e=[ERRORCODE]",))) == 1
    worker.close()
    assert sorted(call[0] for call in client.calls) == sorted((URL + "?e=200", URL + "?e=405"))


def test_error_beacon_uses_general_wrapper_code_when_following_disabled():
    client = FakeClient()
    worker = BeaconWorker(client, threading.Event(), "http://fixture.test")
    enqueue_ad_error(worker, "p1", AdError(302, "Wrapper following disabled",
                                          (URL + "?e=[ERRORCODE]",)))
    worker.close()
    assert [call[0] for call in client.calls] == [URL + "?e=300"]


def test_error_beacon_uses_undefined_code_when_upstream_reason_is_ambiguous():
    client = FakeClient()
    worker = BeaconWorker(client, threading.Event(), "http://fixture.test")
    enqueue_ad_error(worker, "p1", AdError(100, "Unsupported VAST document",
                                          (URL + "?e=[ERRORCODE]",)))
    worker.close()
    assert [call[0] for call in client.calls] == [URL + "?e=900"]


def test_worker_queue_is_bounded_and_stop_interrupts_delivery():
    entered = threading.Event()
    release = threading.Event()

    class BlockingClient(FakeClient):
        def open(self, url, headers, cancel, deadline=None, allowed_origin=None):
            self.calls.append((url, deadline, allowed_origin))
            entered.set()
            while not cancel.is_set() and not release.is_set():
                time.sleep(.001)
            raise TimeoutError("stopped")

    client = BlockingClient()
    cancel = threading.Event()
    worker = BeaconWorker(client, cancel, "http://fixture.test")
    assert worker.enqueue("p0", "start", URL + "/0")
    assert entered.wait(1)
    for index in range(128):
        assert worker.enqueue("p1", "start", URL + "/" + str(index))
    assert not worker.enqueue("p1", "start", URL + "/overflow")
    assert worker.overflow == 1
    cancel.set()
    worker.close()
    release.set()
    assert len(client.calls) == 1


def test_close_stops_in_flight_delivery_without_waiting_for_deadline():
    entered = threading.Event()

    class BlockingClient(FakeClient):
        def open(self, url, headers, cancel, deadline=None, allowed_origin=None):
            entered.set()
            while not cancel.is_set():
                time.sleep(.001)
            raise TimeoutError("stopped")

    worker = BeaconWorker(BlockingClient(), threading.Event(), "http://fixture.test")
    worker.enqueue("p1", "start", URL)
    assert entered.wait(1)
    before = time.monotonic()
    worker.close()
    assert time.monotonic() - before < 1


def test_tracker_diagnostics_contain_only_opaque_ids_and_fixed_reason():
    instance, diagnostics, _ = tracker()
    view = presentation(placement_id="https://private.test/?token=secret")
    instance.observe(view, sample(0, 0))
    assert len(diagnostics) == 2
    for diagnostic in diagnostics:
        assert diagnostic["reason"] == "queued"
        assert len(diagnostic["placement_id"]) == 16
        assert len(diagnostic["creative_id"]) == 16
        assert "secret" not in repr(diagnostic)


# Removing the speed gate would allow these plausible clock advances to succeed.
def test_non_normal_speed_cannot_start_advance_or_complete():
    for speed in (0, .5, 1.000001, 2, -1, float('nan'), float('inf'), None):
        instance, _, queued = tracker()
        view = presentation(duration=1)
        for tick in range(5):
            instance.observe(view, sample(tick * .25, tick * .25, speed=speed))
        assert queued == [], speed

        instance, _, queued = tracker()
        instance.observe(view, sample(0, 0))
        for tick in range(1, 5):
            instance.observe(view, sample(tick * .25, tick * .25, speed=speed))
        assert [event for _, event, _ in queued] == ['impression', 'start'], speed


def test_speed_round_trip_breaks_continuity_without_backfilling():
    instance, _, queued = tracker()
    view = presentation(duration=1)
    instance.observe(view, sample(0, 0))
    # Both callbacks occurred between polls; observed rate alone is normal.
    instance.observe(view, sample(.25, .25, speed=1, speed_changed=True))
    for tick in range(2, 5):
        instance.observe(view, sample(tick * .25, tick * .25))
    assert [event for _, event, _ in queued] == ['impression', 'start']


def test_speed_transition_does_not_taint_later_normal_speed_ad():
    instance, _, queued = tracker()
    view = presentation(start=1, duration=1)
    instance.observe(view, sample(0, 0, speed=.5))
    instance.observe(view, sample(.25, .25, speed_changed=True))
    for tick in range(2, 9):
        instance.observe(view, sample(tick * .25, tick * .25))
    assert [event for _, event, _ in queued] == list(EVENTS)


def test_first_normal_rate_after_non_normal_sample_cannot_backfill():
    instance, _, queued = tracker()
    view = presentation(duration=1)
    instance.observe(view, sample(0, 0))
    instance.observe(view, sample(.125, .25, speed=.5))
    instance.observe(view, sample(.25, .5))
    assert [event for _, event, _ in queued] == ['impression', 'start']
    for tick in range(2, 5):
        instance.observe(view, sample(tick * .25, .5 + tick * .25))
    assert [event for _, event, _ in queued] == ['impression', 'start']
