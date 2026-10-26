# SPDX-License-Identifier: GPL-2.0-or-later
"""Check an addon ZIP without extracting or importing its contents (stdlib only)."""
import argparse
import ast
from pathlib import Path
import re
import stat
import struct
import sys
import xml.etree.ElementTree as ET
import zipfile
import zlib

ADDON_ID = 'plugin.audio.kodi_donor_radio'
VERSION = '0.1.0'
# Explicit manifest: adding a distributable source requires a deliberate edit.
FILES = frozenset({
    'addon.xml', 'default.py', 'service.py', 'LICENSE.txt',
    'resources/settings.xml', 'resources/icon.png', 'resources/fanart.jpg',
    'resources/station-monogram.png',
    'resources/map/natural_earth_110m_land.json', 'resources/map/SOURCES.md',
    'resources/skins/Default/media/white.png',
    'resources/skins/Default/media/radio-white.png',
    'resources/language/resource.language.en_gb/strings.po',
    *('resources/lib/' + name + '.py' for name in (
        'control', 'cues', 'events', 'hls', 'http_client', 'icy', 'kodi_runtime',
        'icy_marker_store', 'icy_markers',
        'models', 'radio_browser', 'radio_ui', 'radio_map', 'kodi_radio_map',
        'playback_arbiter',
        'relay', 'sessions', 'station_resolver', 'url_policy',
        'ad_models', 'ad_tracking', 'ad_media', 'ad_session', 'ad_timeline',
        'vast', 'kodi_ad_player')),
    'resources/skins/Default/1080i/map.xml',
})


def require(condition, message):
    if not condition:
        raise ValueError(message)


def check_land_geometry(payload):
    """Validate bundled map geometry using the runtime decoder."""
    library_path = Path(__file__).resolve().parents[1] / ADDON_ID / 'resources' / 'lib'
    if str(library_path) not in sys.path:
        sys.path.insert(0, str(library_path))
    from radio_map import decode_land_polygons

    try:
        polygons = decode_land_polygons(payload)
    except ValueError as error:
        raise ValueError('Invalid bundled land geometry: ' + str(error)) from error
    require(bool(polygons), 'Bundled map land geometry must not be empty')


def parse_xml(data):
    text = data.decode('utf-8-sig')
    upper = text.upper()
    require('<!DOCTYPE' not in upper and '<!ENTITY' not in upper,
            'XML DTDs are not allowed')
    return ET.fromstring(text)


def png_info(data):
    """Validate the bundled non-interlaced 8-bit RGB/RGBA PNG assets."""
    if len(data) < 45 or data[:8] != b'\x89PNG\r\n\x1a\n':
        return None
    offset = 8
    dimensions = color_type = None
    image_data = bytearray()
    saw_header = saw_data = saw_end = False
    while offset + 12 <= len(data):
        length = struct.unpack('>I', data[offset:offset + 4])[0]
        kind = data[offset + 4:offset + 8]
        end = offset + 12 + length
        if end > len(data):
            return None
        payload = data[offset + 8:offset + 8 + length]
        checksum = struct.unpack('>I', data[offset + 8 + length:end])[0]
        if zlib.crc32(kind + payload) & 0xffffffff != checksum:
            return None
        if not saw_header:
            if kind != b'IHDR' or length != 13:
                return None
            width, height = struct.unpack('>II', payload[:8])
            bit_depth, color_type, compression, filtering, interlace = payload[8:]
            if (not width or not height or bit_depth != 8 or color_type not in (2, 6)
                    or compression != 0 or filtering != 0 or interlace != 0):
                return None
            # Keep malformed package assets from expanding into an unbounded
            # allocation when their compressed IDAT payload is small.
            channels = 3 if color_type == 2 else 4
            expected_size = height * (1 + width * channels)
            if expected_size > 8 * 1024 * 1024:
                return None
            dimensions = (width, height)
            saw_header = True
        elif kind == b'IDAT':
            image_data.extend(payload)
            saw_data = True
        elif kind == b'IEND':
            if length != 0 or not saw_data or end != len(data):
                return None
            saw_end = True
            break
        offset = end
    if not (saw_header and saw_data and saw_end):
        return None
    channels = 3 if color_type == 2 else 4
    width, height = dimensions
    expected_size = height * (1 + width * channels)
    try:
        decompressor = zlib.decompressobj()
        decoded = decompressor.decompress(bytes(image_data), expected_size + 1)
    except zlib.error:
        return None
    if (len(decoded) != expected_size or not decompressor.eof or
            decompressor.unused_data or decompressor.unconsumed_tail):
        return None
    return dimensions, color_type


def jpeg_dimensions(data):
    require(data.startswith(b'\xff\xd8') and data.endswith(b'\xff\xd9'), 'Invalid JPEG framing')
    cursor = 2
    while cursor + 4 <= len(data):
        require(data[cursor] == 255, 'Invalid JPEG marker')
        while cursor < len(data) and data[cursor] == 255:
            cursor += 1
        marker = data[cursor]
        cursor += 1
        if marker in (0xD8, 0xD9) or 0xD0 <= marker <= 0xD7:
            continue
        require(cursor + 2 <= len(data), 'Truncated JPEG')
        length = int.from_bytes(data[cursor:cursor + 2], 'big')
        require(length >= 2 and cursor + length <= len(data), 'Invalid JPEG segment')
        if marker in (0xC0, 0xC1, 0xC2):
            require(length >= 8, 'Invalid JPEG frame')
            height, width = struct.unpack('>HH', data[cursor + 3:cursor + 7])
            return width, height
        cursor += length
    raise ValueError('JPEG frame missing')


def check_contents(contents):
    require(set(contents) == FILES, 'Distribution must contain exactly the approved source manifest')
    check_land_geometry(contents['resources/map/natural_earth_110m_land.json'])
    metadata = parse_xml(contents['addon.xml'])
    require(metadata.tag == 'addon' and metadata.get('id') == ADDON_ID
            and metadata.get('version') == VERSION, 'Incorrect addon identity/version')
    extensions = {(item.get('point'), item.get('library')) for item in metadata.findall('extension')}
    require(('xbmc.python.pluginsource', 'default.py') in extensions
            and ('xbmc.service', 'service.py') in extensions, 'Both addon entry points are required')
    require(metadata.find("extension[@point='xbmc.python.pluginsource']/provides").text == 'audio',
            'Music plugin must provide audio')
    imports = [item.attrib for item in metadata.findall('requires/import')]
    require(imports == [{'addon': 'xbmc.python', 'version': '3.0.1'}], 'Incorrect runtime dependency')
    info = metadata.find("extension[@point='xbmc.addon.metadata']")
    require(info is not None, 'Addon metadata missing')
    require(info.findtext('license') == 'GPL-2.0-or-later', 'Incorrect license metadata')
    require(info.findtext('assets/icon') == 'resources/icon.png'
            and info.findtext('assets/fanart') == 'resources/fanart.jpg', 'Incorrect artwork metadata')
    settings = parse_xml(contents['resources/settings.xml'])
    require(settings.tag == 'settings', 'Invalid settings root')
    map_xml = parse_xml(contents['resources/skins/Default/1080i/map.xml'])
    require(map_xml.tag == 'window', 'Invalid map skin root')
    map_textures = {element.text for element in map_xml.iter()
                    if element.tag in ('texture', 'texturefocus', 'texturenofocus') and element.text}
    addon_texture_prefix = 'special://home/addons/%s/' % ADDON_ID
    addon_media_dir = 'resources/skins/Default/media/'
    for texture in map_textures:
        if texture.startswith(addon_texture_prefix):
            package_path = texture[len(addon_texture_prefix):]
            require(package_path in contents,
                    'Map texture missing from package: ' + texture)
        elif not texture.startswith('$') and '://' not in texture:
            package_path = addon_media_dir + texture
            require(package_path in contents,
                    'Map texture missing from package: ' + texture)
    po = contents['resources/language/resource.language.en_gb/strings.po'].decode('utf-8')
    localized = set(re.findall(r'^msgctxt "#(\d+)"$', po, re.M))
    require('30000' in localized and 'Content-Type: text/plain; charset=UTF-8' in po,
            'English localization missing')
    for element in settings.iter():
        for attribute in ('label', 'help'):
            value = element.get(attribute, '')
            if value.isdigit() and int(value) >= 30000:
                require(value in localized, 'Settings reference missing localization')
    map_references = set(re.findall(r'\$LOCALIZE\[(\d+)\]', contents[
        'resources/skins/Default/1080i/map.xml'].decode('utf-8')))
    require(map_references <= localized, 'Map references missing localization')
    license_text = contents['LICENSE.txt'].decode('utf-8')
    require(len(license_text) > 17000 and 'GNU GENERAL PUBLIC LICENSE' in license_text
            and 'Version 2, June 1991' in license_text
            and 'END OF TERMS AND CONDITIONS' in license_text
            and 'How to Apply These Terms to Your New Programs' in license_text,
            'Full GPL version 2 license text required')
    icon = contents['resources/icon.png']
    icon_info = png_info(icon)
    require(icon_info is not None, 'Invalid PNG icon')
    require(icon_info[0] == (512, 512), 'Icon must be 512 x 512')
    require(icon_info[1] == 2 and b'tRNS' not in icon, 'Icon must be opaque RGB')
    for name, dimensions, color_type in (
            ('resources/station-monogram.png', (256, 256), 2),
            ('resources/skins/Default/media/white.png', (1, 1), 6),
            ('resources/skins/Default/media/radio-white.png', (1, 1), 6)):
        image_info = png_info(contents[name])
        require(image_info == (dimensions, color_type), 'Invalid PNG GUI resource: ' + name)
    require(jpeg_dimensions(contents['resources/fanart.jpg']) == (1280, 720),
            'Fanart must be 1280 x 720 JPEG')
    for name, content in contents.items():
        if name.endswith('.py'):
            source = content.decode('utf-8')
            ast.parse(source, filename=name, feature_version=(3, 8))
            compile(source, name, 'exec', dont_inherit=True)
            require(b'SPDX-License-Identifier: GPL-2.0-or-later' in content,
                    'Python source missing project license notice')


def check_package(path):
    with zipfile.ZipFile(path) as archive:
        require(not archive.comment, 'ZIP archive comments are not allowed')
        infos = archive.infolist()
        names = [info.filename for info in infos]
        require(len(names) == len(set(names)), 'Duplicate ZIP members')
        expected = {ADDON_ID + '/' + name for name in FILES}
        require(set(names) == expected, 'Unexpected, missing, or unsafe ZIP paths')
        require(sum(info.file_size for info in infos) <= 16 * 1024 * 1024, 'Package exceeds size limit')
        for info in infos:
            require(not info.comment, 'ZIP member comments are not allowed')
            mode = info.external_attr >> 16
            require(stat.S_IFMT(mode) == stat.S_IFREG and stat.S_IMODE(mode) == 0o644,
                    'Only non-executable regular files with mode 0644 are allowed')
            require(not info.flag_bits & 1 and info.file_size <= 2 * 1024 * 1024,
                    'Encrypted or oversized ZIP member')
        contents = {name: archive.read(ADDON_ID + '/' + name) for name in FILES}
    check_contents(contents)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('zip', type=Path)
    args = parser.parse_args()
    try:
        check_package(args.zip)
    except (OSError, ValueError, KeyError, AttributeError, IndexError, SyntaxError,
            ET.ParseError, zipfile.BadZipFile) as error:
        print('Package check failed: ' + str(error), file=sys.stderr)
        return 1
    print('Package OK: ' + str(args.zip))
    return 0


if __name__ == '__main__':
    sys.exit(main())
