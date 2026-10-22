# SPDX-License-Identifier: GPL-2.0-or-later
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), 'resources', 'lib'))
from kodi_runtime import run_plugin

if __name__ == '__main__':
    run_plugin(sys.argv)
