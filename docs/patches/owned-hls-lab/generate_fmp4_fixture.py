#!/usr/bin/env python3
"""Generate and verify a raw-AAC fMP4 HLS fixture without re-encoding.

The destination must be absent or an empty directory. All work is staged beside
it and published only after packet hashes and decoded PCM validate.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

SAMPLE_RATE = 48_000
CHANNELS = 2
SAMPLES_PER_AU = 1_024
ROLLING_WINDOW_SEGMENTS = 6


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def run_json(command: list[str]) -> dict[str, Any]:
    result = subprocess.run(command, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    return json.loads(result.stdout)


def canonical_identity(metadata: dict[str, Any]) -> str:
    identity_payload = dict(metadata)
    identity_payload.pop('metadata_identity', None)
    canonical = json.dumps(identity_payload, sort_keys=True, separators=(',', ':'), ensure_ascii=False)
    return hashlib.sha256(canonical.encode('utf-8')).hexdigest()


def parse_playlist(path: Path) -> tuple[str, list[tuple[float, str]]]:
    lines = path.read_text(encoding='utf-8').splitlines()
    init_name = None
    items: list[tuple[float, str]] = []
    for index, line in enumerate(lines):
        if line.startswith('#EXT-X-MAP:'):
            attrs = line.partition(':')[2]
            for attr in attrs.split(','):
                key, equals, value = attr.partition('=')
                if equals and key == 'URI':
                    init_name = value.strip().strip('"')
        elif line.startswith('#EXTINF:'):
            if index + 1 >= len(lines):
                raise ValueError('playlist has EXTINF without a following fragment')
            duration = float(line.partition(':')[2].split(',', 1)[0])
            name = lines[index + 1].strip()
            if not name or name.startswith('#'):
                raise ValueError('playlist fragment URI is missing or malformed')
            items.append((duration, name))
    if init_name != 'init.mp4' or not items:
        raise ValueError('expected one init.mp4 map and at least one media fragment')
    return init_name, items


def probe_packets(ffprobe: str, media: Path, force_mov: bool = False) -> dict[str, Any]:
    command = [ffprobe, '-v', 'error']
    if force_mov:
        command += ['-f', 'mov']
    command += [
        '-select_streams', 'a:0', '-show_streams', '-show_packets', '-show_data_hash', 'sha256',
        '-show_entries',
        'stream=codec_name,profile,codec_type,sample_rate,channels,channel_layout,time_base,extradata_size,initial_padding:'
        'packet=pts,dts,duration,size,flags,data_hash:packet_side_data=side_data_type,skip_samples,discard_padding',
        '-of', 'json', str(media),
    ]
    return run_json(command)


def assert_no_trim_side_data(packets: list[dict[str, Any]], where: str) -> None:
    for index, packet in enumerate(packets):
        for side in packet.get('side_data_list', []):
            if side.get('side_data_type') == 'Skip Samples' or int(side.get('skip_samples', 0)) or int(side.get('discard_padding', 0)):
                raise ValueError(f'{where} packet {index} carries AAC trim/skip side data: {side}')


def validate_packet_timestamp(packet: dict[str, Any], index: int) -> None:
    expected = index * SAMPLES_PER_AU
    if (int(packet['pts']) != expected or int(packet['dts']) != expected or
            int(packet['duration']) != SAMPLES_PER_AU):
        raise ValueError(f'packet {index} has discontinuous timestamp or duration')


def empty_or_missing_target(path: Path) -> None:
    if path.exists() and (not path.is_dir() or any(path.iterdir())):
        raise FileExistsError(f'destination already contains data; refusing to replace it: {path}')


def generate(args: argparse.Namespace) -> dict[str, Any]:
    source_root = args.source_root.resolve()
    target = args.root.resolve()
    source_aac = source_root / 'encoded-once.aac'
    source_map_path = source_root / 'packet-map.jsonl'
    source_pcm = source_root / 'reference-from-served-ts.f32'
    source_metadata = source_root / 'metadata.json'
    for required in (source_aac, source_map_path, source_pcm):
        if not required.is_file():
            raise FileNotFoundError(required)
    empty_or_missing_target(target)
    target.parent.mkdir(parents=True, exist_ok=True)

    source_map = [json.loads(line) for line in source_map_path.read_text(encoding='utf-8').splitlines() if line.strip()]
    if not source_map:
        raise ValueError('source packet map is empty')
    for expected_index, row in enumerate(source_map):
        if int(row['packet']) != expected_index or int(row['samples']) != SAMPLES_PER_AU:
            raise ValueError(f'source packet map is not a contiguous 1024-sample ledger at packet {expected_index}')

    stage_parent = target.parent
    with tempfile.TemporaryDirectory(prefix=f'.{target.name}.stage-', dir=stage_parent) as temp_name:
        stage = Path(temp_name)
        assets = stage / 'assets'
        assets.mkdir()
        playlist = assets / 'reference-vod.m3u8'
        mux_command = [
            args.ffmpeg, '-hide_banner', '-loglevel', 'error', '-y', '-i', str(source_aac),
            '-map', '0:a:0', '-c:a', 'copy', '-bsf:a', 'aac_adtstoasc',
            '-f', 'hls', '-hls_time', '2', '-hls_list_size', '0', '-hls_playlist_type', 'vod',
            '-hls_segment_type', 'fmp4', '-hls_fmp4_init_filename', 'init.mp4',
            '-hls_segment_filename', str(assets / 'segment%03d.m4s'), str(playlist),
        ]
        subprocess.run(mux_command, check=True)
        init_name, segment_uris = parse_playlist(playlist)
        init_path = assets / init_name
        if not init_path.is_file():
            raise FileNotFoundError(init_path)

        full_probe = probe_packets(args.ffprobe, playlist)
        streams = full_probe.get('streams', [])
        packets = full_probe.get('packets', [])
        if len(streams) != 1 or len(packets) != len(source_map):
            raise ValueError(f'expected one audio stream and {len(source_map)} packets; got {len(streams)} streams/{len(packets)} packets')
        stream = streams[0]
        if (stream.get('codec_name') != 'aac' or stream.get('profile') != 'LC' or
                int(stream.get('sample_rate', 0)) != SAMPLE_RATE or int(stream.get('channels', 0)) != CHANNELS):
            raise ValueError(f'unsupported fMP4 audio stream: {stream}')
        if stream.get('time_base') != '1/48000':
            raise ValueError(f'expected 48 kHz fMP4 packet time base, got {stream.get("time_base")}')
        if int(stream.get('extradata_size', 0)) <= 0:
            raise ValueError('fMP4 init segment did not provide AAC AudioSpecificConfig')
        if int(stream.get('initial_padding', 0)) != 0:
            raise ValueError('fMP4 stream requires initial padding trim')
        assert_no_trim_side_data(packets, 'full playlist')

        derived_segments: list[dict[str, Any]] = []
        packet_rows: list[dict[str, Any]] = []
        packet_cursor = 0
        for sequence, (declared_duration, uri) in enumerate(segment_uris):
            if Path(uri).name != uri or not uri.endswith('.m4s'):
                raise ValueError(f'unsafe or non-fMP4 fragment URI: {uri}')
            fragment = assets / uri
            if not fragment.is_file():
                raise FileNotFoundError(fragment)
            joined = stage / f'probe-{sequence:03d}.mp4'
            with joined.open('wb') as output:
                for part in (init_path, fragment):
                    with part.open('rb') as source:
                        shutil.copyfileobj(source, output)
            fragment_probe = probe_packets(args.ffprobe, joined, force_mov=True)
            fragment_streams = fragment_probe.get('streams', [])
            fragment_packets = fragment_probe.get('packets', [])
            if len(fragment_streams) != 1 or not fragment_packets:
                raise ValueError(f'fragment {sequence} did not probe as one AAC stream with packets')
            if fragment_streams[0].get('time_base') != '1/48000':
                raise ValueError(f'fragment {sequence} packet time base changed: {fragment_streams[0].get("time_base")}')
            assert_no_trim_side_data(fragment_packets, f'fragment {sequence}')
            first_pts = int(fragment_packets[0]['pts'])
            start_sample = packet_cursor * SAMPLES_PER_AU
            if first_pts != start_sample:
                raise ValueError(f'fragment {sequence} PTS {first_pts} does not match sample offset {start_sample}')
            fragment_sample_count = 0
            first_packet = packet_cursor
            for packet in fragment_packets:
                duration = int(packet.get('duration', 0))
                if duration != SAMPLES_PER_AU:
                    raise ValueError(f'fragment {sequence} packet duration is not one AAC AU: {duration}')
                global_index = packet_cursor
                if global_index >= len(source_map):
                    raise ValueError('fMP4 fragment sequence has extra packets')
                payload_hash = str(packet['data_hash']).removeprefix('SHA256:').lower()
                source_hash = str(source_map[global_index]['aac_payload_sha256']).lower()
                if payload_hash != source_hash:
                    raise ValueError(f'AAC payload changed/reordered at packet {global_index}')
                validate_packet_timestamp(packet, global_index)
                full_packet = packets[global_index]
                validate_packet_timestamp(full_packet, global_index)
                if str(full_packet['data_hash']).removeprefix('SHA256:').lower() != payload_hash:
                    raise ValueError(f'fragment and playlist demux disagree at packet {global_index}')
                packet_rows.append({
                    'packet': global_index,
                    'aac_payload_sha256': payload_hash,
                    'payload_bytes': int(packet['size']),
                    'source_adts_payload_sha256': source_hash,
                    'source_adts_pts': int(source_map[global_index]['pts']),
                    'source_adts_timebase': source_map[global_index]['timebase'],
                    'fmp4_pts': int(packet['pts']),
                    'fmp4_dts': int(packet['dts']),
                    'fmp4_duration': duration,
                    'fmp4_timebase': [1, SAMPLE_RATE],
                    'reference_start_sample': global_index * SAMPLES_PER_AU,
                    'samples': SAMPLES_PER_AU,
                    'segment_sequence': sequence,
                })
                packet_cursor += 1
                fragment_sample_count += duration
            end_sample = start_sample + fragment_sample_count
            actual_duration = fragment_sample_count / SAMPLE_RATE
            if abs(actual_duration - declared_duration) > (1.0 / SAMPLE_RATE):
                raise ValueError(f'playlist duration mismatch in fragment {sequence}: {declared_duration} vs {actual_duration}')
            derived_segments.append({
                'sequence': sequence,
                'filename': uri,
                'bytes': fragment.stat().st_size,
                'sha256': sha256_file(fragment),
                'duration_seconds': declared_duration,
                'first_packet': first_packet,
                'packet_count': len(fragment_packets),
                'first_pts': first_pts,
                'last_pts': int(fragment_packets[-1]['pts']),
                'start_sample': start_sample,
                'end_sample': end_sample,
                'samples': fragment_sample_count,
            })
            joined.unlink()

        if packet_cursor != len(source_map):
            raise ValueError(f'fMP4 fragments cover {packet_cursor} packets, expected {len(source_map)}')

        decoded_pcm = stage / 'reference-from-fmp4.f32'
        subprocess.run([
            args.ffmpeg, '-hide_banner', '-loglevel', 'error', '-y', '-i', str(playlist),
            '-map', '0:a:0', '-ar', str(SAMPLE_RATE), '-ac', str(CHANNELS), '-f', 'f32le', str(decoded_pcm),
        ], check=True)
        expected_bytes = packet_cursor * SAMPLES_PER_AU * CHANNELS * 4
        if decoded_pcm.stat().st_size != expected_bytes:
            raise ValueError('decoded frame count differs from untrimmed AAC packet count')
        if decoded_pcm.stat().st_size != source_pcm.stat().st_size or sha256_file(decoded_pcm) != sha256_file(source_pcm):
            raise ValueError('decoded fMP4 PCM differs from the existing served-TS reference')
        if not file_bytes_equal(decoded_pcm, source_pcm):
            raise ValueError('PCM hashes match but bytewise comparison did not')

        metadata: dict[str, Any] = {
            'schema_version': 1,
            'kind': 'single-encode-unique-aac-fmp4-hls',
            'source': str(source_aac),
            'source_sha256': sha256_file(source_aac),
            'source_packet_map': str(source_map_path),
            'codec': 'aac',
            'profile': 'LC',
            'sample_rate': SAMPLE_RATE,
            'channels': CHANNELS,
            'channel_layout': 'stereo',
            'packet_count': len(packets),
            'samples_per_packet': SAMPLES_PER_AU,
            'total_samples': packet_cursor * SAMPLES_PER_AU,
            'duration_seconds': (packet_cursor * SAMPLES_PER_AU) / SAMPLE_RATE,
            'container': 'fragmented MP4 HLS',
            'playlist': 'assets/reference-vod.m3u8',
            'init_segment': init_name,
            'init_segment_info': {
                'filename': init_name,
                'bytes': init_path.stat().st_size,
                'sha256': sha256_file(init_path),
                'aac_extradata_size': int(stream['extradata_size']),
            },
            'first_pts': int(packets[0]['pts']),
            'pts_timebase': [1, SAMPLE_RATE],
            'media_sequence': 0,
            'rolling_window_segments': ROLLING_WINDOW_SEGMENTS,
            'segments': derived_segments,
            'packet_map': 'packet-map.jsonl',
            'reference_pcm': 'reference-from-fmp4.f32',
            'reference_pcm_sha256': sha256_file(decoded_pcm),
            'reference_pcm_frames': packet_cursor * SAMPLES_PER_AU,
            'reference_pcm_matches_served_ts': True,
            'trim_side_data_packets': 0,
            'source_pts_origin': {
                'first_source_ts_pts': int(source_map[0]['pts']),
                'source_ts_timebase': source_map[0]['timebase'],
                'first_fmp4_pts': int(packets[0]['pts']),
                'fmp4_timebase': [1, SAMPLE_RATE],
            },
        }
        metadata['metadata_identity'] = canonical_identity(metadata)
        (stage / 'metadata.json').write_text(json.dumps(metadata, indent=2) + '\n', encoding='utf-8')
        with (stage / 'packet-map.jsonl').open('w', encoding='utf-8') as output:
            for row in packet_rows:
                output.write(json.dumps(row, separators=(',', ':')) + '\n')
        verification = {
            'status': 'pass',
            'source_payload_hashes_matched_in_order': packet_cursor,
            'segment_count': len(derived_segments),
            'segment_packet_total': sum(row['packet_count'] for row in derived_segments),
            'decoded_frames': packet_cursor * SAMPLES_PER_AU,
            'decoded_pcm_sha256': metadata['reference_pcm_sha256'],
            'decoded_pcm_byte_identical_to_served_ts_reference': True,
            'trim_side_data_packets': 0,
            'metadata_identity': metadata['metadata_identity'],
        }
        (stage / 'verification.json').write_text(json.dumps(verification, indent=2) + '\n', encoding='utf-8')
        (stage / 'fmp4-packets.json').write_text(json.dumps(full_probe, indent=2) + '\n', encoding='utf-8')
        if source_metadata.is_file():
            shutil.copy2(source_metadata, stage / 'fixture-metadata-original.json')
        generator_readme = Path(__file__).with_name('README.md')
        if generator_readme.is_file():
            shutil.copy2(generator_readme, stage / 'README.md')

        # Recheck before publish so a concurrent fixture is never overwritten.
        empty_or_missing_target(target)
        if target.exists():
            target.rmdir()
        os.replace(stage, target)
        return verification


def file_bytes_equal(left: Path, right: Path) -> bool:
    if left.stat().st_size != right.stat().st_size:
        return False
    with left.open('rb') as a, right.open('rb') as b:
        while True:
            ca, cb = a.read(1024 * 1024), b.read(1024 * 1024)
            if ca != cb:
                return False
            if not ca:
                return True


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source-root', type=Path, required=True, help='Existing owned-hls-validation directory')
    parser.add_argument('--root', type=Path, required=True, help='New or empty destination directory')
    parser.add_argument('--ffmpeg', default='ffmpeg')
    parser.add_argument('--ffprobe', default='ffprobe')
    args = parser.parse_args()
    print(json.dumps(generate(args), indent=2))


if __name__ == '__main__':
    main()
