# SPDX-License-Identifier: GPL-2.0-or-later
"""Pure records shared by the discovery, relay, and observation layers."""
from dataclasses import dataclass, field
from typing import Any, Dict, Mapping, Protocol


_DETAIL_FIELDS = frozenset({
    "cue_id", "duration", "media_sequence", "discontinuity", "rendition_id",
    "epoch", "media_position", "classification", "reason",
    "placement_id", "creative_id", "marker_format", "confidence",
    "insertion_type", "raw_clock", "mapped_position", "mapped",
    "tracking_enabled", "playing", "candidate_only",
    "paused", "seeking", "caching",
})


@dataclass(frozen=True)
class Station:
    uuid: str
    name: str
    url: str
    codec: str
    hls: bool
    country: str = ''
    country_code: str = ''
    state: str = ''
    city: str = ''
    language: str = ''
    tags: str = ''
    favicon: str = ''
    bitrate: int = 0
    latitude: Any = None
    longitude: Any = None
    click_count: int = 0
    votes: int = 0


@dataclass(frozen=True)
class Event:
    kind: str
    session_id: str
    station_uuid: str
    protocol: str
    source: str
    evidence: Any
    observed_at: str
    monotonic_at: float
    details: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        """Select schema fields; sanitation is applied by the diagnostic sink."""
        record = {"schema_version": 1}
        for name in ("kind", "session_id", "station_uuid", "protocol", "source",
                     "evidence", "observed_at", "monotonic_at"):
            value = getattr(self, name)
            if value is not None:
                record[name] = value
        details = {key: value for key, value in self.details.items()
                   if key in _DETAIL_FIELDS and value is not None}
        if details:
            record["details"] = details
        return record


class EventSink(Protocol):
    def emit(self, event: Event) -> None:
        """Receive one upstream observation."""
        ...
