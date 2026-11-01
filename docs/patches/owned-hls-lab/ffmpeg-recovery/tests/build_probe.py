#!/usr/bin/env python3
"""Link the probe against the named private archive; never install libraries."""
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
root = Path(__file__).resolve().parent.parent
# Kodi depends tree: KODI_DEPENDS_TARGET is the target prefix, KODI_DEPENDS_NATIVE the host tools prefix.
try:
    prefix = Path(os.environ['KODI_DEPENDS_TARGET'])
    pkg = str(Path(os.environ['KODI_DEPENDS_NATIVE']) / 'bin/pkg-config')
except KeyError as error:
    raise SystemExit(f'Set {error.args[0]} to the Kodi depends prefix')
archive = Path(sys.argv[1]).resolve()
output = Path(sys.argv[2]).resolve()
if not archive.is_relative_to(root):
    raise ValueError('Archive must be inside the isolated experiment directory')
(root / 'logs').mkdir(parents=True, exist_ok=True)
output.parent.mkdir(parents=True, exist_ok=True)
flags = shlex.split(subprocess.check_output([pkg, '--static', '--libs', str(prefix/'lib/pkgconfig/libavformat.pc')], text=True, env={**os.environ, "PKG_CONFIG_PATH": str(prefix/"lib/pkgconfig")}))
# Pin the format library to this copied archive. Other immutable dependencies are read-only.
flags = [str(archive) if flag == '-lavformat' else flag for flag in flags]
command = ['/Applications/Xcode.app/Contents/Developer/Toolchains/XcodeDefault.xctoolchain/usr/bin/clang', '-std=c17', '-g', '-O1', '-arch', 'arm64', '-mmacosx-version-min=11.0', '-I'+str(prefix/'include'), str(root/'tests/hls_recovery_probe.c'), '-o', str(output), *flags]
(root/'logs'/f'{output.name}-link.json').write_text(json.dumps(command,indent=2)+'\n')
result = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
(root/'logs'/f'{output.name}-link.log').write_text(result.stdout)
if result.returncode:
    sys.stderr.write(result.stdout)
    raise SystemExit(result.returncode)
print(output)
