# SPDX-License-Identifier: GPL-2.0-or-later
"""Build the source-only addon ZIP with fixed order, timestamps and permissions."""
import argparse
from pathlib import Path
import stat
import sys
import tempfile
import zipfile

from check_package import ADDON_ID, FILES, VERSION, check_contents, check_package


def package_addon(source, output):
    contents = {}
    for name in sorted(FILES):
        path = source / name
        if any(parent.is_symlink() for parent in (path, *path.parents)):
            raise ValueError('Symlinks are not distributable')
        contents[name] = path.read_bytes()
    check_contents(contents)
    output.mkdir(parents=True, exist_ok=True)
    destination = output / (ADDON_ID + '-' + VERSION + '.zip')
    # ZIP_STORED makes output independent of zlib/runtime versions as well as mtimes.
    with tempfile.NamedTemporaryFile(dir=output, suffix='.zip', delete=False) as handle:
        temporary = Path(handle.name)
    try:
        with zipfile.ZipFile(temporary, 'w', compression=zipfile.ZIP_STORED) as archive:
            for name, data in contents.items():
                info = zipfile.ZipInfo(ADDON_ID + '/' + name, date_time=(1980, 1, 1, 0, 0, 0))
                info.create_system = 3
                info.external_attr = (stat.S_IFREG | 0o644) << 16
                archive.writestr(info, data)
        check_package(temporary)
        temporary.chmod(0o644)
        temporary.replace(destination)
    finally:
        if temporary.exists():
            temporary.unlink()
    return destination


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, default=Path(__file__).resolve().parents[1] / ADDON_ID)
    parser.add_argument('--output', type=Path, default=Path('dist'))
    args = parser.parse_args()
    try:
        destination = package_addon(args.source.resolve(), args.output.resolve())
    except (OSError, ValueError, AttributeError, SyntaxError) as error:
        print('Packaging failed: ' + str(error), file=sys.stderr)
        return 1
    print(destination)
    return 0


if __name__ == '__main__':
    sys.exit(main())
