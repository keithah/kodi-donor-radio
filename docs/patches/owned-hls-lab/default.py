# SPDX-License-Identifier: GPL-2.0-or-later
import os
import sys

def validate_lab_manifest(path, owned=False):
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
    if owned: required.update(('owned_clock_nonce','owned_clock_timeline','owned_clock_first_packet'))
    if len(extensions) != 1 or not required.issubset(set(extensions[0].get('listitemprops', '').split('|'))):
        raise RuntimeError('Owned HLS lab requires terminal_eof and is_realtime_stream in kodi.inputstream listitemprops')
    dependencies = root.findall("requires/import[@addon='kodi.binary.global.audioengine']")
    if not dependencies or all(item.get('optional', 'false').lower() == 'true' for item in dependencies):
        raise RuntimeError('Owned HLS lab requires the audioengine binary dependency')


def launch_owned(fixture):
    import json
    import time
    import uuid
    from dataclasses import asdict
    from pathlib import Path
    import xbmc, xbmcgui, xbmcplugin, xbmcaddon
    lab=Path('/private/tmp/kodi-audio-validation-20261027/native-pause-repair/owned-renderer')
    library=Path(__file__).resolve().parent/'resources/lib'
    sys.path[:0]=[str(lab),str(library)]
    import fixture_beacons as fixture_api
    from owned_clock import OwnedClockBinding,OwnedClockTransport,SESSION_ROOT,mach_host_ns
    from owned_ad_playback import canonical_timeline
    import runtime_clock
    validate_lab_manifest(Path(xbmcaddon.Addon('inputstream.ffmpegdirect').getAddonInfo('path'))/'addon.xml',owned=True)
    runtime_clock.frozen_module()  # Refuse altered estimator before native Open.
    settings={}
    for setting in ('audiodevice','lowlatency','channels','config','processquality','stereoupmix','passthrough'):
        name='audiooutput.'+setting
        response=json.loads(xbmc.executeJSONRPC(json.dumps({'jsonrpc':'2.0','id':1,'method':'Settings.GetSettingValue','params':{'setting':name}})))
        if 'error' in response:raise RuntimeError('Cannot verify owned output setting '+name)
        settings[name]=response['result']['value']
    fixture_api.validate_profile(settings)
    nonce=uuid.uuid4().hex
    presentation,proof=fixture_api.resolve_presentation(Path(fixture),nonce)
    timeline=canonical_timeline(presentation,fixture_api.FIRST_PACKET)
    binding=OwnedClockBinding(nonce,None,fixture_api.sha(timeline),fixture_api.FIRST_PACKET,
                              fixture_api.URL,mach_host_ns(),os.getpid())
    transport=OwnedClockTransport.create(SESSION_ROOT,binding)
    directory=transport.directory
    def private_write(name,raw):
        descriptor=os.open(name,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600,dir_fd=transport.fd)
        try:
            os.fchmod(descriptor,0o600)
            with os.fdopen(descriptor,'wb',closefd=False) as stream:stream.write(raw)
        finally:os.close(descriptor)
    private_write('timeline.json',timeline)
    private_write('launch.json',fixture_api.canonical({'binding':asdict(binding),'fixture':str(Path(fixture).resolve()),
                  'proof':proof,'settings':settings,'estimator_sha256':runtime_clock.SHA256}))
    try:
        xbmc.executebuiltin('RunScript(%s,--owned-beacons,%s,%s)'%(lab/'monitor.py',directory,library))
        deadline=time.monotonic()+10
        while time.monotonic()<deadline:
            try:
                ready,_=transport._read_immutable('monitor-ready.json',2048)
                fixture_api.validate_ready(json.loads(ready),asdict(binding));break
            except FileNotFoundError:xbmc.sleep(20)
        else:raise RuntimeError('Owned monitor did not become ready')
        item=xbmcgui.ListItem(label='Owned clock controlled beacons',path=fixture_api.URL)
        item.setMimeType('application/vnd.apple.mpegurl');item.setContentLookup(False)
        item.getMusicInfoTag().setTitle('Owned clock controlled beacons')
        for name,value in {'inputstream':'inputstream.ffmpegdirect','inputstream-player':'audiodefaultplayer',
             'inputstream.ffmpegdirect.stream_mode':'timeshift','inputstream.ffmpegdirect.is_realtime_stream':'false',
             'inputstream.ffmpegdirect.terminal_eof':'true','inputstream.ffmpegdirect.owned_clock_nonce':nonce,
             'inputstream.ffmpegdirect.owned_clock_timeline':binding.timeline_sha256,
             'inputstream.ffmpegdirect.owned_clock_first_packet':binding.first_packet_sha256}.items():item.setProperty(name,value)
        xbmc.Player().play(fixture_api.URL,item)
        xbmcplugin.endOfDirectory(int(sys.argv[1]))
    finally:transport.close()


if __name__ == '__main__':
    from urllib.parse import parse_qs
    query=parse_qs(sys.argv[2].lstrip('?')) if len(sys.argv)>2 else {}
    if query.get('owned_beacons')==['1']:
        if set(query)!={'owned_beacons','fixture'} or len(query['fixture'])!=1:raise RuntimeError('Explicit fixture required')
        launch_owned(query['fixture'][0])
    elif len(sys.argv)>2 and sys.argv[2]=='?diagnostic_owned_hls=1':
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
