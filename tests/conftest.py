# SPDX-License-Identifier: GPL-2.0-or-later
"""Load the addon library without importing Kodi integration modules."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugin.audio.kodi_donor_radio" / "resources" / "lib"))
