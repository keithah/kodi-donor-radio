# SPDX-License-Identifier: GPL-2.0-or-later
"""Exercise the distributable, including rejection of unsafe or incomplete ZIPs."""
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import xml.etree.ElementTree as ET
import zipfile

import pytest

ROOT = Path(__file__).resolve().parents[1]
ADDON = 'plugin.audio.kodi_donor_radio'


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
    with zipfile.ZipFile(package) as archive:
        names = archive.namelist()
        assert {name.split('/')[0] for name in names} == {ADDON}
        assert ADDON + '/default.py' in names
        assert ADDON + '/service.py' in names
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


def test_package_ignores_unlisted_files_and_filesystem_metadata(tmp_path):
    source = tmp_path / ADDON
    shutil.copytree(ROOT / ADDON, source, ignore=shutil.ignore_patterns('__pycache__'))
    for filename in ('tests/fixture.mp3', 'resources/lib/secret.py', '.env', 'token.json'):
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


@pytest.mark.parametrize('mutation', [
    'missing_entry', 'invalid_xml', 'wrong_version', 'wrong_license', 'short_license',
    'missing_localization', 'bad_artwork', 'extra_secret', 'extra_root', 'traversal',
    'executable', 'symlink', 'duplicate',
])
def test_checker_rejects_invalid_distribution(package, tmp_path, mutation):
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
    elif mutation == 'bad_artwork':
        contents[root + 'resources/icon.png'] = b'not a picture'
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
    assert result.stderr.strip()
