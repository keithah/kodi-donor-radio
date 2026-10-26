import pytest

from models import Station
from tools.sample_hls_ad_markers import discover_candidates, host_is_cdnstream1


def test_host_filter_requires_cdnstream1_domain_boundary():
    assert host_is_cdnstream1('https://cdnstream1.com/live.m3u8') is True
    assert host_is_cdnstream1('http://edge.cdnstream1.com/live') is True
    assert host_is_cdnstream1('https://notcdnstream1.com/live') is False
    assert host_is_cdnstream1('https://cdnstream1.com.example.org/live') is False
    assert host_is_cdnstream1('://cdnstream1.com/live') is False
    assert host_is_cdnstream1('file://cdnstream1.com/live') is False


def _station(uuid, url='https://cdnstream1.com/live', hls=True):
    return Station(uuid, uuid, url, 'AAC', hls)


class _PagedBrowser:
    def __init__(self, pages):
        self.pages = pages
        self.calls = []

    def search(self, name, country, tag, offset, limit):
        self.calls.append((name, country, tag, offset, limit))
        return self.pages[offset]


def test_discovery_filters_hls_deduplicates_and_stops_at_bounds():
    first_page = [_station('one'), _station('one'), _station('non-hls', hls=False),
                  _station('lookalike', 'https://cdnstream1.com.example.org/live'),
                  _station('two')]
    first_page.extend(_station('other-%d' % index, hls=False)
                      for index in range(45))
    second_page = [_station('two'), _station('three')]
    browser = _PagedBrowser({0: first_page, 50: second_page})

    stations, pages_read = discover_candidates(browser, page_limit=2, candidate_limit=3)

    assert [station.uuid for station in stations] == ['one', 'two', 'three']
    assert pages_read == 2
    assert browser.calls == [('', '', '', 0, 50), ('', '', '', 50, 50)]

    capped = _PagedBrowser({0: first_page, 50: second_page})
    stations, pages_read = discover_candidates(capped, page_limit=1, candidate_limit=20)
    assert [station.uuid for station in stations] == ['one', 'two']
    assert pages_read == 1
    assert capped.calls == [('', '', '', 0, 50)]


@pytest.mark.parametrize('page_limit,candidate_limit', [(0, 1), (101, 1), (1, 0), (1, 21)])
def test_discovery_rejects_limits_outside_bounds(page_limit, candidate_limit):
    with pytest.raises(ValueError):
        discover_candidates(_PagedBrowser({}), page_limit, candidate_limit)
