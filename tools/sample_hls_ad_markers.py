# SPDX-License-Identifier: GPL-2.0-or-later
"""Discover bounded Radio Browser HLS candidates on cdnstream1.com."""
from typing import List, Tuple
from urllib.parse import urlsplit

from models import Station
from radio_browser import RadioBrowser


PAGE_SIZE = 50


def host_is_cdnstream1(url: str) -> bool:
    """Return whether an HTTP(S) URL has cdnstream1.com as its host boundary."""
    if not isinstance(url, str):
        return False
    try:
        parsed = urlsplit(url)
        hostname = parsed.hostname
        _ = parsed.port  # Access validates the port syntax and range.
        authority = parsed.netloc.rsplit('@', 1)[-1]
        if authority.endswith(':'):
            return False
    except ValueError:
        return False
    if parsed.scheme.lower() not in ('http', 'https') or not hostname:
        return False
    hostname = hostname.lower()
    return hostname == 'cdnstream1.com' or hostname.endswith('.cdnstream1.com')


def discover_candidates(browser: RadioBrowser, page_limit: int,
                        candidate_limit: int) -> Tuple[List[Station], int]:
    """Read bounded directory pages and retain unique cdnstream1 HLS stations."""
    if (type(page_limit) is not int or not 1 <= page_limit <= 100 or
            type(candidate_limit) is not int or not 1 <= candidate_limit <= 20):
        raise ValueError('Invalid discovery limits')

    candidates = []
    seen_uuids = set()
    pages_read = 0
    for page_number in range(page_limit):
        page = browser.search('', '', '', page_number * PAGE_SIZE, PAGE_SIZE)
        pages_read += 1
        for station in page:
            if (station.hls and station.uuid not in seen_uuids and
                    host_is_cdnstream1(station.url)):
                seen_uuids.add(station.uuid)
                candidates.append(station)
                if len(candidates) >= candidate_limit:
                    return candidates, pages_read
        if len(page) < PAGE_SIZE:
            break
    return candidates, pages_read
