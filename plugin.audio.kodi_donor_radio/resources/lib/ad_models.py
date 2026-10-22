# SPDX-License-Identifier: GPL-2.0-or-later
"""Immutable records shared by controlled ad resolution and composition.

Samples and output sample positions are at 48 kHz. PTS values use the 90 kHz
MPEG clock; media parsers normalize wrap before constructing MediaInfo.
"""
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Tuple


class AdError(Exception):
    """Safe, typed VAST failure; URLs are data for a later beacon dispatcher."""
    def __init__(self, code: int, reason: str, error_urls: tuple = ()):
        self.code, self.reason, self.error_urls = code, reason, tuple(error_urls)
        super().__init__(reason)


@dataclass(frozen=True)
class AdConfig:
    enabled: bool
    preroll: bool
    midroll: bool
    vast_url: str
    fixture_origin: str


@dataclass(frozen=True)
class TrackingUrl:
    event: str
    url: str


@dataclass(frozen=True)
class ResolvedAd:
    id: str
    media_url: str
    duration_seconds: float
    tracking: Tuple[TrackingUrl, ...]
    error_urls: Tuple[str, ...]


@dataclass(frozen=True)
class MediaInfo:
    codec: str
    sample_rate: int
    channels: int
    samples: int
    first_pts: int
    last_pts: int


@dataclass(frozen=True)
class CachedSegment:
    id: str
    path: Path
    info: MediaInfo


@dataclass(frozen=True)
class PreparedAd:
    ad: ResolvedAd
    segments: Tuple[CachedSegment, ...]
    samples: int


@dataclass(frozen=True)
class Placement:
    id: str
    ad: ResolvedAd
    start_sample: int
    end_sample: int


@dataclass(frozen=True)
class OutputSegment:
    sequence: int
    discontinuity: int
    resource_id: str
    start_sample: int
    samples: int
    placement_id: Optional[str]


@dataclass(frozen=True)
class Presentation:
    segments: Tuple[OutputSegment, ...]
    placements: Tuple[Placement, ...]
    ended: bool


@dataclass(frozen=True)
class PlaybackSample:
    session_id: str
    position_seconds: float
    monotonic_seconds: float
    playing: bool
    paused: bool
    seeking: bool
    mapped: bool
