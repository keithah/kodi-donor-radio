# SPDX-License-Identifier: GPL-2.0-or-later
"""Load the addon library without importing Kodi integration modules."""
import sys
from pathlib import Path
import shutil
import subprocess

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugin.audio.kodi_donor_radio" / "resources" / "lib"))


def generate_ad_assets(root, target):
    try:
        subprocess.run([sys.executable, str(root / 'tools' / 'generate_ad_audio.py'),
                        '--assets', str(target)], check=True, timeout=300)
    except subprocess.TimeoutExpired:
        pytest.fail('Ad fixture generation timed out after 300 seconds', pytrace=False)


@pytest.fixture(scope='session')
def ad_assets(tmp_path_factory):
    required = ('ffmpeg', 'ffprobe', 'espeak')
    missing = [name for name in required if shutil.which(name) is None]
    if missing:
        pytest.skip('Ad fixture generation requires: ' + ', '.join(missing))
    root = Path(__file__).resolve().parents[1]
    target = tmp_path_factory.mktemp('generated-ad-assets')
    generate_ad_assets(root, target)
    return target
