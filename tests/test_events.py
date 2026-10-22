# SPDX-License-Identifier: GPL-2.0-or-later
"""Event serialization must preserve observations and exclude private metadata."""
import json
from dataclasses import FrozenInstanceError

import pytest

from events import JsonEventSink, sanitize
from models import Event, Station


def observation(**changes):
    fields = dict(
        kind="metadata_observed", session_id="session-1", station_uuid="station-1",
        protocol="icy", source="StreamTitle", evidence="Artist - Song",
        observed_at="2026-10-21T17:00:00Z", monotonic_at=123.5,
    )
    fields.update(changes)
    return Event(**fields)


def test_unknown_timing_is_omitted(tmp_path):
    debug = []
    sink = JsonEventSink(tmp_path, True, debug.append)
    sink.emit(observation(details={"duration": None, "media_position": None}))
    record = json.loads(debug[0])
    assert record == {
        "schema_version": 1, "kind": "metadata_observed", "session_id": "session-1",
        "station_uuid": "station-1", "protocol": "icy", "source": "StreamTitle",
        "evidence": "Artist - Song", "observed_at": "2026-10-21T17:00:00Z",
        "monotonic_at": 123.5,
    }
    assert json.loads((tmp_path / "events.jsonl").read_text()) == record


def test_known_context_is_preserved_and_extra_fields_are_omitted():
    record = observation(details={
        "cue_id": "break-3", "duration": 0.0, "media_sequence": 0,
        "discontinuity": 2, "rendition_id": "audio", "epoch": 1,
        "media_position": 12.5, "classification": "metadata", "reason": "observed",
        "upstream_request": "private", "headers": {"custom": "private"},
    }).to_dict()
    assert record["details"] == {
        "cue_id": "break-3", "duration": 0.0, "media_sequence": 0,
        "discontinuity": 2, "rendition_id": "audio", "epoch": 1,
        "media_position": 12.5, "classification": "metadata", "reason": "observed",
    }
    assert "private" not in json.dumps(record)


def test_log_redacts_nested_credentials(tmp_path):
    secrets = ["user123", "pass123", "query123", "fragment123", "auth123", "cookie123", "api123"]
    metadata = {
        "nested": [{"url": "https://user123:pass123@example.org/live?token=query123#fragment123",
                    "Authorization": "Bearer auth123", "Set-Cookie": "sid=cookie123",
                    "X-API-Key": "api123"}],
        "title": 'Artist https://user123:pass123@example.org?key=query123#fragment123',
        "malformed": 'cue Cookie: cookie123',
        "safe": "Artist - Song",
    }
    debug = []
    sink = JsonEventSink(tmp_path, True, debug.append)
    sink.emit(observation(evidence=metadata, details={"reason": "Authorization: Bearer auth123"}))
    output = (tmp_path / "events.jsonl").read_text() + "".join(debug)
    for secret in secrets:
        assert secret not in output
    assert "Artist - Song" in output
    assert "REDACTED" in output
    assert sanitize(metadata)["safe"] == "Artist - Song"
    assert metadata["nested"][0]["Authorization"] == "Bearer auth123"


def test_redaction_precedes_truncation(tmp_path):
    debug = []
    sink = JsonEventSink(tmp_path, True, debug.append)
    sink.emit(observation(evidence="é" * 1010 + " https://user:password@example.org?token=secret"))
    assert "password" not in debug[0]
    assert "secret" not in debug[0]
    assert "https" not in debug[0]
    assert len(json.loads(debug[0])["evidence"].encode("utf-8")) <= 2048


def test_rotation_is_bounded(tmp_path):
    sink = JsonEventSink(tmp_path, True, lambda line: None)
    event = observation(evidence="é" * 5000)
    for _ in range(1600):
        sink.emit(event)
    files = sorted(tmp_path.iterdir())
    assert len(files) == 3
    for path in files:
        assert path.stat().st_size <= 1024 * 1024
        for line in path.read_text().splitlines():
            assert len(json.loads(line)["evidence"].encode("utf-8")) <= 2048


def test_disabled_sink_does_not_create_profile_or_log(tmp_path):
    profile = tmp_path / "not-created"
    debug = []
    JsonEventSink(profile, False, debug.append).emit(observation())
    assert not profile.exists()
    assert debug == []


def test_station_is_frozen():
    station = Station("station-1", "Radio", "https://example.org/live", "MP3", False)
    with pytest.raises(FrozenInstanceError):
        station.url = "https://other.example/live"


def test_large_nested_metadata_preserves_required_fields(tmp_path):
    debug = []
    metadata = {str(i): list(range(32)) for i in range(32)}
    JsonEventSink(tmp_path, True, debug.append).emit(observation(evidence=metadata))
    record = json.loads(debug[0])
    assert record["observed_at"] == "2026-10-21T17:00:00Z"
    assert record["monotonic_at"] == 123.5
    evidence = record["evidence"]
    encoded = evidence if isinstance(evidence, str) else json.dumps(evidence, separators=(",", ":"))
    assert len(encoded.encode("utf-8")) <= 2048


def test_sanitize_hides_credential_aliases_and_unbounded_scalars():
    value = sanitize({"username": "user-secret", "auth": "auth-secret", "large": 2 ** 100000})
    assert value["username"] == "[REDACTED]"
    assert value["auth"] == "[REDACTED]"
    assert value["large"] == "[REDACTED]"


def test_oversized_context_cannot_exceed_one_log_file(tmp_path):
    context = {str(i): ["\x00" * 2048] * 32 for i in range(3)}
    JsonEventSink(tmp_path, True, lambda line: None).emit(observation(details={"reason": context}))
    path = tmp_path / "events.jsonl"
    assert path.stat().st_size <= 1024 * 1024
    record = json.loads(path.read_text())
    assert record["details"]["reason"]
    assert record["monotonic_at"] == 123.5


@pytest.mark.parametrize("reference", [
    "/live/audio.m3u8?signature=query123#fragment123",
    "segments/audio.aac?signature=query123#fragment123",
])
def test_log_redacts_nested_relative_url_credentials(tmp_path, reference):
    metadata = {"nested": [{"cue": reference}]}
    debug = []
    JsonEventSink(tmp_path, True, debug.append).emit(observation(evidence=metadata))
    output = (tmp_path / "events.jsonl").read_text() + "".join(debug)
    assert "query123" not in output
    assert "fragment123" not in output
    assert sanitize(metadata) == {"nested": [{"cue": "[REDACTED]"}]}
    assert sanitize("#EXT-X-CUE-OUT:DURATION=30") == "#EXT-X-CUE-OUT:DURATION=30"
