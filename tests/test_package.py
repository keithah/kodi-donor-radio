# SPDX-License-Identifier: GPL-2.0-or-later
"""Exercise the distributable, including rejection of unsafe or incomplete ZIPs."""
import json
import os
from pathlib import Path
import re
import shutil
import stat
import struct
import subprocess
import sys
import xml.etree.ElementTree as ET
import zipfile
import zlib

import pytest

ROOT = Path(__file__).resolve().parents[1]
ADDON = 'plugin.audio.kodi_donor_radio'


def land_geometry(geometry, polygons):
    return json.dumps({**geometry, 'polygons': polygons}).encode()


def run_tool(name, *args):
    return subprocess.run([sys.executable, str(ROOT / 'tools' / name), *map(str, args)],
                          capture_output=True, text=True)


@pytest.fixture
def package(tmp_path):
    result = run_tool('package_addon.py', '--output', tmp_path)
    assert result.returncode == 0, result.stderr
    path = tmp_path / (ADDON + '-0.1.0.zip')
    assert path.is_file()
    return path


def test_package_is_installable_source_only(package):
    assert stat.S_IMODE(package.stat().st_mode) == 0o644
    with zipfile.ZipFile(package) as archive:
        names = archive.namelist()
        assert {name.split('/')[0] for name in names} == {ADDON}
        assert ADDON + '/default.py' in names
        assert ADDON + '/service.py' in names
        assert ADDON + '/resources/map/natural_earth_110m_land.json' in names
        assert ADDON + '/resources/map/SOURCES.md' in names
        assert ADDON + '/resources/station-monogram.png' in names
        assert ADDON + '/resources/skins/Default/media/white.png' in names
        assert ADDON + '/resources/skins/Default/media/radio-white.png' in names
        assert ADDON + '/resources/lib/radio_ui.py' in names
        assert ADDON + '/resources/lib/radio_map.py' in names
        assert ADDON + '/resources/lib/kodi_radio_map.py' in names
        map_xml = ET.fromstring(archive.read(ADDON + '/resources/skins/Default/1080i/map.xml'))
        assert len(map_xml.findall(".//control[@type='button']")) == 62
        assert {ADDON + '/resources/lib/' + module + '.py' for module in
                ('ad_models', 'ad_tracking', 'ad_media', 'ad_session', 'ad_timeline',
                 'vast', 'kodi_ad_player', 'icy_markers', 'icy_marker_store',
                 'playback_arbiter')} <= set(names)
        assert all(not any(part in ('tests', 'tools', '__pycache__') or part.startswith('.')
                           for part in name.split('/')) for name in names)
        assert not any(name.endswith(('.pyc', '.mp3', '.aac', '.ts', '.env')) for name in names)
        assert all(stat.S_IMODE(item.external_attr >> 16) == 0o644 for item in archive.infolist())
        metadata = ET.fromstring(archive.read(ADDON + '/addon.xml'))
        assert metadata.attrib['id'] == ADDON
        assert metadata.attrib['version'] == '0.1.0'
        assert {(item.attrib['point'], item.attrib.get('library')) for item in metadata.findall('extension')} >= {
            ('xbmc.python.pluginsource', 'default.py'), ('xbmc.service', 'service.py')}
        assert metadata.find('requires/import').attrib == {'addon': 'xbmc.python', 'version': '3.0.1'}
        assert metadata.find("extension[@point='xbmc.addon.metadata']/license").text == 'GPL-2.0-or-later'
        license_text = archive.read(ADDON + '/LICENSE.txt').decode()
        assert 'GNU GENERAL PUBLIC LICENSE' in license_text
        assert 'END OF TERMS AND CONDITIONS' in license_text
        assert len(license_text) > 17000
    result = run_tool('check_package.py', package)
    assert result.returncode == 0, result.stderr


def test_package_checker_rejects_malformed_map_xml(package):
    import runpy
    checker = runpy.run_path(str(ROOT / 'tools' / 'check_package.py'))['check_contents']
    with zipfile.ZipFile(package) as archive:
        prefix = ADDON + '/'
        contents = {name[len(prefix):]: archive.read(name)
                    for name in archive.namelist() if name.startswith(prefix)}
    contents['resources/skins/Default/1080i/map.xml'] = b'<window>'
    with pytest.raises(ET.ParseError):
        checker(contents)


def test_package_checker_rejects_missing_map_localization(package):
    import runpy
    checker = runpy.run_path(str(ROOT / 'tools' / 'check_package.py'))['check_contents']
    with zipfile.ZipFile(package) as archive:
        prefix = ADDON + '/'
        contents = {name[len(prefix):]: archive.read(name)
                    for name in archive.namelist() if name.startswith(prefix)}
    contents['resources/skins/Default/1080i/map.xml'] = contents[
        'resources/skins/Default/1080i/map.xml'].replace(b'$LOCALIZE[30148]', b'$LOCALIZE[39999]')
    with pytest.raises(ValueError, match='Map references missing localization'):
        checker(contents)


def test_package_checker_rejects_missing_relative_map_texture(package):
    import runpy
    checker = runpy.run_path(str(ROOT / 'tools' / 'check_package.py'))['check_contents']
    with zipfile.ZipFile(package) as archive:
        prefix = ADDON + '/'
        contents = {name[len(prefix):]: archive.read(name)
                    for name in archive.namelist() if name.startswith(prefix)}
    contents['resources/skins/Default/1080i/map.xml'] = contents[
        'resources/skins/Default/1080i/map.xml'].replace(b'radio-white.png', b'not-packaged.png')
    with pytest.raises(ValueError, match='Map texture missing from package'):
        checker(contents)


@pytest.mark.parametrize('change', [
    lambda geometry: b'{not json',
    lambda geometry: json.dumps({**geometry, 'schema_version': True}).encode(),
    lambda geometry: json.dumps({**geometry, 'source': 'wrong dataset'}).encode(),
    lambda geometry: json.dumps({**geometry, 'unexpected': 1}).encode(),
    lambda geometry: json.dumps({**geometry, 'polygons': []}).encode(),
    lambda geometry: land_geometry(geometry, [None]),
    lambda geometry: land_geometry(geometry, [None] * 25001),
    lambda geometry: land_geometry(geometry, [[None] * 25001]),
    lambda geometry: land_geometry(geometry, [
        [[[0, 0], [1, 0], [1, 1], [0, 0]]] * 25001]),
    lambda geometry: land_geometry(geometry, [[[[0, 0], [1, 0], [0, 0]]]]),
    lambda geometry: land_geometry(geometry, [[[[0, 0], [1, 0], [1, 1], [0, 1]]]]),
    lambda geometry: land_geometry(geometry, [[[[0, 0] for _ in range(100001)]]]),
    lambda geometry: land_geometry(geometry, [[[[0, 0], [1, 0], [float('nan'), 1], [0, 0]]]]),
    lambda geometry: land_geometry(geometry, [[[[True, 0], [1, 0], [1, 1], [True, 0]]]]),
    lambda geometry: land_geometry(geometry, [[[[181, 0], [1, 0], [1, 1], [181, 0]]]]),
    lambda geometry: land_geometry(geometry, [[[[0, 91], [1, 0], [1, 1], [0, 91]]]]),
    lambda geometry: land_geometry(geometry, [[[[0, 0, 1], [1, 0], [1, 1], [0, 0, 1]]]]),
    lambda geometry: land_geometry(geometry, [[[[0, 0], [1, 0], [1, 1], [0, 0]]]]) +
        b' ' * (2 * 1024 * 1024),
])
def test_package_checker_rejects_invalid_bundled_land_geometry(package, change):
    import runpy
    checker = runpy.run_path(str(ROOT / 'tools' / 'check_package.py'))['check_contents']
    with zipfile.ZipFile(package) as archive:
        prefix = ADDON + '/'
        contents = {name[len(prefix):]: archive.read(name)
                    for name in archive.namelist() if name.startswith(prefix)}
    geometry = {
        'schema_version': 1,
        'source': 'Natural Earth 1:110m land v4.1.0',
        'polygons': [[[[0, 0], [1, 0], [1, 1], [0, 0]]]],
    }
    contents['resources/map/natural_earth_110m_land.json'] = change(geometry)

    with pytest.raises(ValueError):
        checker(contents)


def test_png_checker_bounds_decompressed_gui_texture_data():
    import runpy
    checker = runpy.run_path(str(ROOT / 'tools' / 'check_package.py'))['png_info']

    def chunk(kind, payload):
        return (struct.pack('>I', len(payload)) + kind + payload +
                struct.pack('>I', zlib.crc32(kind + payload) & 0xffffffff))

    header = struct.pack('>IIBBBBB', 1, 1, 8, 6, 0, 0, 0)
    image = (b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', header) +
             chunk(b'IDAT', zlib.compress(b'\x00' * (1024 * 1024))) +
             chunk(b'IEND', b''))
    assert checker(image) is None


def test_package_checker_rejects_xml_dtd(package):
    import runpy
    checker = runpy.run_path(str(ROOT / 'tools' / 'check_package.py'))['check_contents']
    with zipfile.ZipFile(package) as archive:
        prefix = ADDON + '/'
        contents = {name[len(prefix):]: archive.read(name)
                    for name in archive.namelist() if name.startswith(prefix)}
    contents['resources/settings.xml'] = (
        b'<!DOCTYPE settings [<!ENTITY local "expanded">]><settings/>')
    with pytest.raises(ValueError, match='XML DTDs are not allowed'):
        checker(contents)


def test_package_checker_rejects_python_that_parses_but_cannot_compile(package):
    import runpy
    checker = runpy.run_path(str(ROOT / 'tools' / 'check_package.py'))['check_contents']
    with zipfile.ZipFile(package) as archive:
        prefix = ADDON + '/'
        contents = {name[len(prefix):]: archive.read(name)
                    for name in archive.namelist() if name.startswith(prefix)}
    contents['default.py'] = b'# SPDX-License-Identifier: GPL-2.0-or-later\nreturn\n'
    with pytest.raises(SyntaxError):
        checker(contents)


@pytest.mark.parametrize('comment_target', ['archive', 'member'])
def test_package_checker_rejects_zip_comments(package, tmp_path, comment_target):
    candidate = tmp_path / ('commented-' + comment_target + '.zip')
    with zipfile.ZipFile(package) as source, zipfile.ZipFile(candidate, 'w') as target:
        for index, item in enumerate(source.infolist()):
            if comment_target == 'member' and index == 0:
                item.comment = b'embedded-data'
            target.writestr(item, source.read(item.filename))
        if comment_target == 'archive':
            target.comment = b'embedded-data'
    result = run_tool('check_package.py', candidate)
    assert result.returncode != 0
    assert 'comments are not allowed' in result.stderr


def test_package_ignores_unlisted_files_and_filesystem_metadata(tmp_path):
    source = tmp_path / ADDON
    shutil.copytree(ROOT / ADDON, source, ignore=shutil.ignore_patterns('__pycache__'))
    for filename in ('tests/fixture.mp3', 'tests/fixtures/hls/media.m3u8',
                     'tools/fixture_server.py', '.superpowers/progress.md',
                     'resources/lib/secret.py', '.env', 'token.json'):
        path = source / filename
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('DO NOT DISTRIBUTE')
    first = tmp_path / 'first'
    second = tmp_path / 'second'
    result = run_tool('package_addon.py', '--source', source, '--output', first)
    assert result.returncode == 0, result.stderr
    for path in source.rglob('*'):
        if path.is_file():
            os.chmod(path, 0o755)
            os.utime(path, (1700000000, 1700000000))
    result = run_tool('package_addon.py', '--source', source, '--output', second)
    assert result.returncode == 0, result.stderr
    filename = ADDON + '-0.1.0.zip'
    assert (first / filename).read_bytes() == (second / filename).read_bytes()
    with zipfile.ZipFile(first / filename) as archive:
        assert not any(name.endswith(('secret.py', '.env', 'token.json', 'fixture.mp3')) for name in archive.namelist())
        assert not any(part in ('tests', 'fixtures', 'tools', '.superpowers')
                       for name in archive.namelist() for part in name.split('/'))


def test_packaged_gui_settings_and_map_have_localized_resources(package):
    """Catch incomplete GUI archives even when source-tree adapter tests pass."""
    with zipfile.ZipFile(package) as archive:
        root = ADDON + '/resources/'
        settings = ET.fromstring(archive.read(root + 'settings.xml'))
        po = archive.read(root + 'language/resource.language.en_gb/strings.po').decode()
        translations = dict(re.findall(r'msgctxt "#(\d+)"\nmsgid "([^"\n]+)"', po))
        assert translations.get('30167') == (
            'The action could not be completed. Return to the list and try again.')
        for setting_id, default in [('guided_setup', 'false'), ('offer_setup_row', 'true'),
                                    ('preview_on_browse', 'true')]:
            setting = settings.find(".//setting[@id='%s']" % setting_id)
            assert setting is not None
            assert setting.findtext('default') == default
        assert settings.findtext(".//setting[@id='run_setup']/data") == (
            'RunPlugin(plugin://plugin.audio.kodi_donor_radio/?mode=setup)')
        for node in settings.iter():
            for attribute in ('label', 'help'):
                reference = node.get(attribute, '')
                if reference.isdigit() and int(reference) >= 30000:
                    assert translations.get(reference), reference
        map_source = archive.read(root + 'skins/Default/1080i/map.xml').decode()
        window = ET.fromstring(map_source)
        pin_ids = {int(node.get('id')) for node in window.findall(".//control[@type='button']")
                   if 1000 <= int(node.get('id')) < 2000}
        assert pin_ids == set(range(1000, 1060))
        assert window.find(".//control[@type='list'][@id='2000']") is not None
        for reference in re.findall(r'\$LOCALIZE\[(30\d{3})\]', map_source):
            assert translations.get(reference), reference
        for module in ('radio_ui', 'radio_map', 'kodi_radio_map', 'playback_arbiter'):
            assert root + 'lib/' + module + '.py' in archive.namelist()
        assert archive.read(root + 'station-monogram.png').startswith(b'\x89PNG\r\n\x1a\n')


@pytest.mark.parametrize('resource', [
    'resources/settings.xml', 'resources/language/resource.language.en_gb/strings.po',
    'resources/skins/Default/1080i/map.xml', 'resources/skins/Default/media/white.png',
    'resources/skins/Default/media/radio-white.png',
    'resources/station-monogram.png',
    'resources/lib/radio_ui.py', 'resources/lib/radio_map.py',
    'resources/lib/kodi_radio_map.py', 'resources/lib/playback_arbiter.py',
])
def test_checker_rejects_missing_gui_resource(package, tmp_path, resource):
    path = tmp_path / 'missing-gui-resource.zip'
    with zipfile.ZipFile(package) as original, zipfile.ZipFile(path, 'w') as changed:
        for entry in original.infolist():
            if entry.filename != ADDON + '/' + resource:
                changed.writestr(entry, original.read(entry))
    result = run_tool('check_package.py', path)
    assert result.returncode != 0
    assert 'Unexpected, missing, or unsafe ZIP paths' in result.stderr


@pytest.mark.parametrize('mutation, message', [
    ('missing_entry', 'Unexpected, missing, or unsafe ZIP paths'),
    ('invalid_xml', 'Package check failed'),
    ('wrong_version', 'Incorrect addon identity/version'),
    ('wrong_license', 'Incorrect license metadata'),
    ('short_license', 'Full GPL version 2 license text required'),
    ('missing_localization', 'English localization missing'),
    ('bad_artwork', 'Invalid PNG icon'),
    ('bad_gui_texture', 'Invalid PNG GUI resource'),
    ('missing_map_texture', 'Map texture missing from package'),
    ('extra_secret', 'Unexpected, missing, or unsafe ZIP paths'),
    ('extra_root', 'Unexpected, missing, or unsafe ZIP paths'),
    ('traversal', 'Unexpected, missing, or unsafe ZIP paths'),
    ('executable', 'Only non-executable regular files with mode 0644 are allowed'),
    ('symlink', 'Only non-executable regular files with mode 0644 are allowed'),
    ('duplicate', 'Duplicate ZIP members'),
    ('missing_map_localization', 'Map references missing localization'),
])
def test_checker_rejects_invalid_distribution(package, tmp_path, mutation, message):
    with zipfile.ZipFile(package) as archive:
        contents = {info.filename: archive.read(info) for info in archive.infolist()}
    root = ADDON + '/'
    if mutation == 'missing_entry':
        del contents[root + 'service.py']
    elif mutation == 'invalid_xml':
        contents[root + 'resources/settings.xml'] = b'<settings'
    elif mutation == 'wrong_version':
        contents[root + 'addon.xml'] = contents[root + 'addon.xml'].replace(b'version="0.1.0"', b'version="9.0.0"')
    elif mutation == 'wrong_license':
        contents[root + 'addon.xml'] = contents[root + 'addon.xml'].replace(b'GPL-2.0-or-later', b'MIT')
    elif mutation == 'short_license':
        contents[root + 'LICENSE.txt'] = b'GNU GENERAL PUBLIC LICENSE\nEND OF TERMS AND CONDITIONS'
    elif mutation == 'missing_localization':
        contents[root + 'resources/language/resource.language.en_gb/strings.po'] = b''
    elif mutation == 'missing_map_localization':
        map_path = root + 'resources/skins/Default/1080i/map.xml'
        contents[map_path] = contents[map_path].replace(b'$LOCALIZE[30148]', b'$LOCALIZE[39999]')
    elif mutation == 'bad_artwork':
        contents[root + 'resources/icon.png'] = b'not a picture'
    elif mutation == 'bad_gui_texture':
        contents[root + 'resources/skins/Default/media/radio-white.png'] = b'not a picture'
    elif mutation == 'missing_map_texture':
        map_path = root + 'resources/skins/Default/1080i/map.xml'
        contents[map_path] = contents[map_path].replace(
                b'radio-white.png', b'not-packaged.png')
    elif mutation == 'extra_secret':
        contents[root + '.env'] = b'token=secret'
    elif mutation == 'extra_root':
        contents['extra/file.py'] = b''
    elif mutation == 'traversal':
        contents[root + '../outside.py'] = b''
    path = tmp_path / 'invalid.zip'
    with zipfile.ZipFile(path, 'w') as archive:
        for name, data in contents.items():
            info = zipfile.ZipInfo(name)
            info.create_system = 3
            mode = stat.S_IFREG | 0o644
            if name == root + 'default.py' and mutation == 'executable':
                mode |= 0o111
            if name == root + 'default.py' and mutation == 'symlink':
                mode = stat.S_IFLNK | 0o644
            info.external_attr = mode << 16
            archive.writestr(info, data)
        if mutation == 'duplicate':
            with pytest.warns(UserWarning):
                archive.writestr(root + 'default.py', b'')
    result = run_tool('check_package.py', path)
    assert result.returncode != 0
    assert message in result.stderr
