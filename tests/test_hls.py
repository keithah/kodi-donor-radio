# SPDX-License-Identifier: GPL-2.0-or-later
"""HLS behavior: opaque routing, faithful lines, and defensible cue timing."""
from pathlib import Path

import pytest


FIXTURES = Path(__file__).parent / 'fixtures' / 'hls'
FINAL_URL = 'https://cdn.example/redirected/live/index.m3u8?auth=manifest'


def parse(text):
    from hls import parse_playlist
    return parse_playlist(text, FINAL_URL)


def test_rewrites_every_supported_uri():
    from hls import rewrite_playlist
    master = parse((FIXTURES / 'master.m3u8').read_text())
    media = parse((FIXTURES / 'media.m3u8').read_text())
    routes = {}

    def register(url):
        routes.setdefault(url, '/opaque/{}'.format(len(routes)))
        return routes[url]

    rewritten = rewrite_playlist(master, register)
    audio = rewrite_playlist(media, register)
    assert list(routes) == [
        'https://cdn.example/audio/main.m3u8?sig=a%2Fb&x=1',
        'https://cdn.example/redirected/live/iframe.m3u8?token=a,b',
        'https://cdn.example/redirected/live/low/index.m3u8?sig=low',
        'https://cdn.example/redirected/live/high/index.m3u8?sig=high',
        'https://cdn.example/redirected/init.mp4?sig=init',
        'https://cdn.example/redirected/live/media.mp4?sig=abc%2Fdef&part=1',
        'https://cdn.example/shared/media.mp4?sig=second',
        'https://cdn.example/redirected/live/last.aac',
    ]
    assert 'NAME="Radio, \\"live\\"",URI="/opaque/0"' in rewritten
    assert 'URI="/opaque/1"' in rewritten
    assert '\n/opaque/2\n' in rewritten
    assert '\n/opaque/3\n' in rewritten
    assert 'URI="/opaque/4",BYTERANGE="400@0"' in audio
    assert '\n/opaque/5\n' in audio
    assert '\n/opaque/6\n' in audio
    assert '\n/opaque/7\n' in audio
    assert master.is_master and not media.is_master
    assert [ref.kind for ref in media.uri_references] == ['map', 'segment', 'segment', 'segment']


def test_preserves_non_uri_lines_and_line_endings():
    from hls import rewrite_playlist
    original = (FIXTURES / 'media.m3u8').read_text().replace('\n', '\r\n')
    playlist = parse(original)
    output = rewrite_playlist(playlist, lambda url: '/opaque/segment')
    original_lines = original.splitlines(keepends=True)
    changed = {ref.line_index for ref in playlist.uri_references}
    assert all(line == output.splitlines(keepends=True)[i]
               for i, line in enumerate(original_lines) if i not in changed)
    assert '#EXT-X-BYTERANGE:1000@0\r\n' in output
    assert playlist.lines == tuple(original_lines)


def test_observations_keep_sequence_discontinuity_and_relative_timing():
    playlist = parse((FIXTURES / 'media.m3u8').read_text())
    assert playlist.media_sequence == 100
    assert playlist.discontinuity_sequence == 4
    assert playlist.target_duration == 6
    assert [(s.sequence, s.discontinuity, s.duration, s.media_position) for s in playlist.segments] == [
        (100, 4, 5.5, 0), (101, 5, 6, 5.5), (102, 5, 1, 11.5)]
    assert [(c.source, c.sequence, c.discontinuity, c.media_position) for c in playlist.cues] == [
        ('EXT-X-DATERANGE', 100, 4, None), ('EXT-X-CUE-OUT', 100, 4, 0),
        ('EXT-X-CUE-OUT-CONT', 101, 5, 5.5), ('EXT-X-CUE-IN', 102, 5, 11.5)]
    assert playlist.cues[0].attributes['SCTE35-OUT'] == '0xFC00'
    assert playlist.cues[1].attributes == {'DURATION': '12'}
    assert playlist.cues[2].attributes['ElapsedTime'] == '5.5'


@pytest.mark.parametrize('tag', [
    '#EXT-X-KEY:METHOD=AES-128,URI="key"',
    '#EXT-X-KEY:METHOD=SAMPLE-AES,URI="key"',
    '#EXT-X-SESSION-KEY:METHOD=AES-128,URI="key"',
    '#EXT-X-PART:DURATION=1,URI="part"',
    '#EXT-X-PRELOAD-HINT:TYPE=PART,URI="part"',
    '#EXT-X-SERVER-CONTROL:CAN-BLOCK-RELOAD=YES',
    '#EXT-X-PART-INF:PART-TARGET=1',
    '#EXT-X-SKIP:SKIPPED-SEGMENTS=5',
    '#EXT-X-RENDITION-REPORT:URI="other"',
    '#EXT-X-SESSION-DATA:DATA-ID="test",URI="metadata"',
    '#EXT-X-CONTENT-STEERING:SERVER-URI="steering"',
    '#EXT-X-FUTURE:URI="direct"',
    '#EXT-X-FUTURE:X-ASSET-URI="direct"',
    '#EXT-X-DATERANGE:ID="interstitial",X-ASSET-URI="direct"',
    '#EXT-X-DEFINE:NAME="host",VALUE="example.com"',
])
def test_unsupported_features_fail_closed(tag):
    from hls import UnsupportedPlaylist
    with pytest.raises(UnsupportedPlaylist):
        parse('#EXTM3U\n#EXT-X-TARGETDURATION:6\n' + tag + '\n#EXTINF:6,\nseg.aac\n')


def test_bare_uri_bearing_cue_out_fails_closed():
    from hls import UnsupportedPlaylist
    with pytest.raises(UnsupportedPlaylist, match='URI-bearing cue'):
        parse('#EXTM3U\n#EXT-X-TARGETDURATION:6\n'
              '#EXT-X-CUE-OUT:https://upstream.example/cue\n#EXTINF:6,\nseg.aac\n')


@pytest.mark.parametrize('text', [
    '', 'seg.aac\n', '#EXTM3U\nseg.aac\n', '#EXTM3U\n#EXTINF:6,\nseg.aac\n',
    '#EXTM3U\n#EXT-X-TARGETDURATION:6\n#EXTINF:6,\n',
    '#EXTM3U\n#EXT-X-TARGETDURATION:6\n#EXTINF:nan,\nseg.aac\n',
    '#EXTM3U\n#EXT-X-TARGETDURATION:0\n',
    '#EXTM3U\n#EXT-X-MEDIA-SEQUENCE:-1\n#EXT-X-TARGETDURATION:6\n',
    '#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=128000\n',
    '#EXTM3U\n#EXT-X-MEDIA:TYPE=AUDIO,URI="unterminated\n',
    '#EXTM3U\n#EXT-X-MAP:URI="one",URI="two"\n',
    '#EXTM3U\n#EXT-X-MAP:URI=unquoted\n',
    '#EXTM3U\n#EXT-X-TARGETDURATION:6\n#EXTINF:6,\nfile:///tmp/private\n',
    '#EXTM3U\n#EXT-X-TARGETDURATION:6\n#EXTINF:6,\n{$host}/seg.aac\n',
    '#EXTM3U\n#EXT-X-TARGETDURATION:6\n\x00\n',
    '\ufeff#EXTM3U\n',
])
def test_malformed_playlists_rejected(text):
    from hls import InvalidPlaylist
    with pytest.raises(InvalidPlaylist):
        parse(text)


def test_empty_quoted_hls_metadata_values_are_valid_but_empty_uri_is_not():
    from hls import InvalidPlaylist, parse_playlist
    playlist = parse_playlist('#EXTM3U\n#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="audio",NAME="English",'
                               'LANGUAGE="",CHARACTERISTICS=""\n', 'https://radio.example/master.m3u8')
    assert playlist.is_master
    with pytest.raises(InvalidPlaylist, match='Invalid resource URI'):
        parse_playlist('#EXTM3U\n#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="audio",NAME="English",URI=""\n',
                       'https://radio.example/master.m3u8')
    with pytest.raises(InvalidPlaylist, match='Empty attribute'):
        parse_playlist('#EXTM3U\n#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="audio",NAME="English",LANGUAGE=\n',
                       'https://radio.example/master.m3u8')


def test_oversized_playlist_rejected_by_shared_byte_limit():
    from hls import InvalidPlaylist
    with pytest.raises(InvalidPlaylist):
        parse('#EXTM3U\n#' + 'é' * (512 * 1024))


def test_none_key_and_unknown_non_uri_tags_remain_unchanged():
    from hls import rewrite_playlist
    text = '#EXTM3U\n#EXT-X-TARGETDURATION:6\n#EXT-X-KEY:METHOD=NONE\n#EXT-X-VENDOR:MARKER=YES\n#EXTINF:6,\nseg.aac'
    assert '#EXT-X-KEY:METHOD=NONE\n#EXT-X-VENDOR:MARKER=YES\n' in rewrite_playlist(parse(text), lambda u: '/local')


def test_renditions_keep_independent_urls_and_sequence_context():
    from hls import parse_playlist
    text = '#EXTM3U\n#EXT-X-TARGETDURATION:6\n#EXT-X-CUE-OUT:6\n#EXTINF:6,\nseg.aac\n'
    first = parse_playlist(text, 'https://cdn.example/a/index.m3u8')
    second = parse_playlist(text, 'https://cdn.example/b/index.m3u8')
    assert first.final_url != second.final_url
    assert first.segments[0].uri == 'https://cdn.example/a/seg.aac'
    assert second.segments[0].uri == 'https://cdn.example/b/seg.aac'
    assert first.cues[0].sequence == second.cues[0].sequence == 0


def test_in_band_closed_caption_rendition_has_no_uri_to_rewrite():
    from hls import rewrite_playlist
    text = '#EXTM3U\n#EXT-X-MEDIA:TYPE=CLOSED-CAPTIONS,GROUP-ID="cc",NAME="Captions",INSTREAM-ID="CC1"\n#EXT-X-STREAM-INF:BANDWIDTH=128000,CLOSED-CAPTIONS="cc"\nmain.m3u8\n'
    output = rewrite_playlist(parse(text), lambda u: '/opaque/main')
    assert 'INSTREAM-ID="CC1"\n' in output
    assert output.endswith('/opaque/main\n')


@pytest.mark.parametrize('tag', [
    '#EXT-X-CUE-IN:URI="upstream"',
    '#EXT-X-CUE-OUT-CONT:URI="upstream"',
    '#EXT-X-CUE-OUT-CONT:https://upstream.example/cue',
    '#EXT-X-DISCONTINUITY:URI="upstream"',
    '#EXT-X-ENDLIST:URI="upstream"',
    '#EXT-X-PROGRAM-DATE-TIME:URI="upstream"',
    '#EXT-X-BYTERANGE:12@bad',
])
def test_malformed_supported_tags_cannot_hide_references(tag):
    from hls import InvalidPlaylist
    with pytest.raises(InvalidPlaylist):
        parse('#EXTM3U\n#EXT-X-TARGETDURATION:6\n' + tag + '\n#EXTINF:6,\nseg.aac\n')


def test_preserves_malformed_cue_payload_for_tracker_validation():
    playlist = parse('#EXTM3U\n#EXT-X-TARGETDURATION:6\n#EXT-X-CUE-OUT:DURATION=bad,SCTE35=??\n#EXTINF:6,\nseg.aac\n')
    assert playlist.cues[0].attributes == {'DURATION': 'bad', 'SCTE35': '??'}


@pytest.mark.parametrize('route', ['https://upstream.example/segment', '//upstream/segment', '/route\nunsafe', '/route"unsafe'])
def test_rewrite_rejects_nonlocal_or_injected_registered_routes(route):
    from hls import InvalidPlaylist, rewrite_playlist
    playlist = parse('#EXTM3U\n#EXT-X-TARGETDURATION:6\n#EXTINF:6,\nseg.aac\n')
    with pytest.raises(InvalidPlaylist):
        rewrite_playlist(playlist, lambda u: route)


@pytest.mark.parametrize('attribute', ['X-ASSET-LIST', 'X-ASSET-URI'])
@pytest.mark.parametrize('resource', ['https://ads.example/assets.json', '../ads/assets.json'])
def test_interstitial_asset_references_fail_closed(attribute, resource):
    from hls import UnsupportedPlaylist
    text = '#EXTM3U\n#EXT-X-TARGETDURATION:6\n#EXT-X-DATERANGE:ID="ad",CLASS="com.apple.hls.interstitial",START-DATE="2026-10-21T12:00:00Z",{}="{}"\n#EXTINF:6,\nseg.aac\n'.format(attribute, resource)
    with pytest.raises(UnsupportedPlaylist):
        parse(text)


@pytest.mark.parametrize('range_value', ['bad', '0@0', '12@bad', '-1@0', '12@-1'])
def test_map_byte_range_must_be_valid(range_value):
    from hls import InvalidPlaylist
    text = '#EXTM3U\n#EXT-X-TARGETDURATION:6\n#EXT-X-MAP:URI="init.mp4",BYTERANGE="{}"\n#EXTINF:6,\nseg.aac\n'.format(range_value)
    with pytest.raises(InvalidPlaylist):
        parse(text)


@pytest.mark.parametrize('preceding', [
    '',
    '#EXTINF:6,\nmedia.mp4\n',
    '#EXTINF:6,\n#EXT-X-BYTERANGE:10@0\nother.mp4\n',
    '#EXTINF:6,\n#EXT-X-BYTERANGE:10@0\nmedia.mp4?sig=other\n',
    '#EXTINF:6,\n#EXT-X-BYTERANGE:10@0\nmedia.mp4\n#EXTINF:6,\nmedia.mp4\n',
])
def test_implicit_segment_range_requires_preceding_subrange_of_same_resource(preceding):
    from hls import InvalidPlaylist
    text = '#EXTM3U\n#EXT-X-TARGETDURATION:6\n' + preceding + '#EXTINF:6,\n#EXT-X-BYTERANGE:10\nmedia.mp4\n'
    with pytest.raises(InvalidPlaylist):
        parse(text)


def test_valid_implicit_ranges_and_map_ranges_preserved():
    from hls import rewrite_playlist
    text = '#EXTM3U\n#EXT-X-TARGETDURATION:6\n#EXT-X-MAP:URI="init.mp4",BYTERANGE="400@0"\n#EXTINF:6,\n#EXT-X-BYTERANGE:10@400\nmedia.mp4\n#EXTINF:6,\n#EXT-X-BYTERANGE:20\n./media.mp4\n#EXTINF:6,\n#EXT-X-BYTERANGE:30\nmedia.mp4\n'
    output = rewrite_playlist(parse(text), lambda u: '/opaque/resource')
    assert 'BYTERANGE="400@0"' in output
    assert '#EXT-X-BYTERANGE:10@400\n/opaque/resource\n' in output
    assert '#EXT-X-BYTERANGE:20\n/opaque/resource\n' in output
    assert '#EXT-X-BYTERANGE:30\n/opaque/resource\n' in output


@pytest.mark.parametrize('value', ['URI="https://upstream.example/raw"', 'LIVE', 'vod', ''])
def test_playlist_type_rejects_invalid_values(value):
    from hls import InvalidPlaylist
    with pytest.raises(InvalidPlaylist):
        parse('#EXTM3U\n#EXT-X-TARGETDURATION:6\n#EXT-X-PLAYLIST-TYPE:' + value + '\n#EXTINF:6,\nseg.aac\n')


@pytest.mark.parametrize('value', ['EVENT', 'VOD'])
def test_playlist_type_preserves_supported_values(value):
    from hls import rewrite_playlist
    text = '#EXTM3U\n#EXT-X-TARGETDURATION:6\n#EXT-X-PLAYLIST-TYPE:' + value + '\n#EXTINF:6,\nseg.aac\n'
    assert '#EXT-X-PLAYLIST-TYPE:' + value + '\n' in rewrite_playlist(parse(text), lambda u: '/opaque/resource')


@pytest.mark.parametrize('timestamp', [
    '2026-10-21T17:58:58+0000',
    '2026-10-21T17:58:58.125+0530',
    '2026-10-21T17:58:58-0700',
])
def test_program_date_time_accepts_iso_basic_timezone_on_older_python(timestamp):
    from hls import rewrite_playlist
    text = '#EXTM3U\n#EXT-X-TARGETDURATION:6\n#EXT-X-PROGRAM-DATE-TIME:' + timestamp + '\n#EXTINF:6,\nseg.aac\n'
    parsed = parse(text)
    assert len(parsed.segments) == 1
    assert '#EXT-X-PROGRAM-DATE-TIME:' + timestamp in rewrite_playlist(parsed, lambda url: '/opaque/segment')


@pytest.mark.parametrize('timestamp', [
    '2026-10-21T17:58:58+2400',
    '2026-10-21T17:58:58+1260',
    '2026-10-21T17:58:58-9999',
    '2026-99-21T17:58:58+0000',
    '2026-10-21T17:58:58+0000junk',
])
def test_program_date_time_rejects_malformed_basic_timezone(timestamp):
    from hls import InvalidPlaylist
    with pytest.raises(InvalidPlaylist):
        parse('#EXTM3U\n#EXT-X-TARGETDURATION:6\n#EXT-X-PROGRAM-DATE-TIME:' + timestamp + '\n#EXTINF:6,\nseg.aac\n')


@pytest.mark.parametrize('fraction,microsecond', [('1', 100000), ('12', 120000), ('123', 123000),
                                                ('1234', 123400), ('12345', 123450),
                                                ('123456', 123456), ('123456789', 123456)])
def test_fractional_timestamp_accepts_valid_precision_preserves_manifest(fraction, microsecond):
    from hls import parse_iso_datetime, rewrite_playlist
    timestamp = '2026-10-21T17:00:00.' + fraction + '+0530'
    parsed_date = parse_iso_datetime(timestamp)
    assert parsed_date.microsecond == microsecond
    assert parsed_date.utcoffset().total_seconds() == 19800
    text = '#EXTM3U\n#EXT-X-TARGETDURATION:6\n#EXT-X-PROGRAM-DATE-TIME:' + timestamp + '\n#EXTINF:6,\nseg.aac\n'
    assert '#EXT-X-PROGRAM-DATE-TIME:' + timestamp + '\n' in rewrite_playlist(parse(text), lambda url: '/opaque/segment')


@pytest.mark.parametrize('timestamp', ['2026-02-30T17:00:00.1Z', '2026-10-21T25:00:00.12Z',
    '2026-10-21T17:00:00.Z', '2026-10-21T17:00:00.12junkZ', '2026-10-21T17:00:00.12.3Z',
    '2026-10-21T17:00:00.12+12:60'])
def test_fractional_timestamp_does_not_hide_invalid_date_or_syntax(timestamp):
    from hls import InvalidPlaylist
    with pytest.raises(InvalidPlaylist):
        parse('#EXTM3U\n#EXT-X-TARGETDURATION:6\n#EXT-X-PROGRAM-DATE-TIME:' + timestamp + '\n#EXTINF:6,\nseg.aac\n')


@pytest.mark.parametrize('tag', [
    '#EXT-X-VENDOR:Security information from Curl',
    '#EXT-X-SCTE35:CUE="/DAuRl=="',
    '#EXT-X-VENDOR:NOTE="URI=ordinary text",MARKER=YES',
    '#EXT-X-CUE-OUT-CONT:Security',
])
def test_non_reference_vendor_text_is_preserved(tag):
    from hls import rewrite_playlist
    text = '#EXTM3U\n#EXT-X-TARGETDURATION:6\n' + tag + '\n#EXTINF:6,\nseg.aac\n'
    assert tag in rewrite_playlist(parse(text), lambda uri: '/opaque/media')



def test_url_substring_in_metadata_key_is_allowed_without_uri_value():
    text = ('#EXTM3U\n#EXT-X-TARGETDURATION:6\n'
            '#EXT-X-DATERANGE:ID="x",START-DATE="2026-10-21T12:00:00Z",'
            'X-MATURITY-RATING="G"\n#EXTINF:6,\nseg.aac\n')
    assert len(parse(text).segments) == 1


def test_undelimited_url_key_with_absolute_value_still_fails_closed():
    from hls import UnsupportedPlaylist
    text = ('#EXTM3U\n#EXT-X-TARGETDURATION:6\n'
            '#EXT-X-DATERANGE:ID="x",START-DATE="2026-10-21T12:00:00Z",'
            'X-SERVERURL="https://upstream.example/x"\n#EXTINF:6,\nseg.aac\n')
    with pytest.raises(UnsupportedPlaylist):
        parse(text)


@pytest.mark.parametrize('value', [
    'URI="../hidden"', 'X-URI-SUFFIX="../hidden"', 'OTHER=YES,URL="../hidden"',
    'OTHER=YES, X_URL_SUFFIX = "../hidden"', 'X.URL="../hidden"',
    'X-SERVERURL=https://upstream.example/hidden',
    'NOTE="https://upstream.example/hidden"', 'NOTE="{$host}/hidden"',
])
def test_unknown_tag_actual_references_still_fail_closed(value):
    from hls import UnsupportedPlaylist
    with pytest.raises(UnsupportedPlaylist):
        parse('#EXTM3U\n#EXT-X-TARGETDURATION:6\n#EXT-X-VENDOR:' + value + '\n#EXTINF:6,\nseg.aac\n')


def test_many_attributes_parse_with_bounded_slice_copying():
    from hls import _attributes
    class CountedString(str):
        copied = 0
        def __getitem__(self, key):
            result = super().__getitem__(key)
            if isinstance(key, slice): self.copied += len(result)
            return result
    value = CountedString(','.join('X-%d=value%d' % (index, index) for index in range(2000)))
    attrs, spans = _attributes(value)
    assert len(attrs) == 2000
    assert attrs['X-0'] == 'value0' and attrs['X-1999'] == 'value1999'
    start, end, quoted = spans['X-1999']
    assert value[start:end] == 'value1999' and not quoted
    # Count copied characters, not elapsed time: repeated suffix copies are quadratic.
    assert value.copied < len(value) * 2
