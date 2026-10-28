# SPDX-License-Identifier: GPL-2.0-or-later
import os
import sys

def validate_lab_manifest(path):
    """Refuse a misinstalled diagnostic before monitor startup or playback."""
    import xml.etree.ElementTree as ET
    try:
        root = ET.parse(str(path)).getroot()
    except (OSError, ET.ParseError) as exc:
        raise RuntimeError('Owned HLS lab manifest is unreadable or malformed: '+str(path)) from exc
    if root.tag != 'addon' or root.get('id') != 'inputstream.ffmpegdirect':
        raise RuntimeError('Owned HLS lab requires the ffmpegdirect manifest')
    extensions = root.findall("extension[@point='kodi.inputstream']")
    required = {'terminal_eof', 'is_realtime_stream'}
    if len(extensions) != 1 or not required.issubset(set(extensions[0].get('listitemprops', '').split('|'))):
        raise RuntimeError('Owned HLS lab requires terminal_eof and is_realtime_stream in kodi.inputstream listitemprops')
    dependencies = root.findall("requires/import[@addon='kodi.binary.global.audioengine']")
    if not dependencies or all(item.get('optional', 'false').lower() == 'true' for item in dependencies):
        raise RuntimeError('Owned HLS lab requires the audioengine binary dependency')


if __name__ == '__main__':
    if len(sys.argv)>2 and sys.argv[2]=='?diagnostic_owned_hls=1':
        import xbmc, xbmcgui, xbmcplugin, xbmcaddon
        validate_lab_manifest(os.path.join(xbmcaddon.Addon('inputstream.ffmpegdirect').getAddonInfo('path'), 'addon.xml'))
        url='http://127.0.0.1:18789/live/index.m3u8'
        item=xbmcgui.ListItem(label='Native pause unique diagnostic',path=url)
        item.setMimeType('application/vnd.apple.mpegurl'); item.setContentLookup(False)
        item.getMusicInfoTag().setTitle('Native pause unique diagnostic')
        item.setProperty('inputstream','inputstream.ffmpegdirect')
        item.setProperty('inputstream-player','audiodefaultplayer')
        item.setProperty('inputstream.ffmpegdirect.stream_mode','timeshift')
        item.setProperty('inputstream.ffmpegdirect.is_realtime_stream','false')
        item.setProperty('inputstream.ffmpegdirect.terminal_eof','true')
        xbmc.executebuiltin('RunScript(/private/tmp/kodi-audio-validation-20261027/native-pause-repair/owned-renderer/monitor.py)')
        import time
        from pathlib import Path
        ready=Path('/private/tmp/kodi-audio-validation-20261027/native-pause-repair/owned-renderer/monitor-ready')
        deadline=time.monotonic()+5
        while not ready.exists() and time.monotonic()<deadline: xbmc.sleep(20)
        if not ready.exists(): raise RuntimeError('Pause monitor did not become ready')
        xbmc.Player().play(url,item)
        xbmcplugin.endOfDirectory(int(sys.argv[1]))
    else:
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), 'resources', 'lib'))
        from kodi_runtime import run_plugin
        run_plugin(sys.argv)
