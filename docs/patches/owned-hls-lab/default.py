# SPDX-License-Identifier: GPL-2.0-or-later
import os
import sys

def validate_lab_manifest(path, owned=False, video=False, video_control=False):
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
    if video: required.update(('owned_player_mode','owned_player_core'))
    if video_control: required.add('owned_player_queue_tenths')
    if len(extensions) != 1 or not required.issubset(set(extensions[0].get('listitemprops', '').split('|'))):
        raise RuntimeError('Owned HLS lab requires terminal_eof and is_realtime_stream in kodi.inputstream listitemprops')
    dependencies = root.findall("requires/import[@addon='kodi.binary.global.audioengine']")
    if not dependencies or all(item.get('optional', 'false').lower() == 'true' for item in dependencies):
        raise RuntimeError('Owned HLS lab requires the audioengine binary dependency')


def player_contract(mode):
    modes={'paplayer_pcm_v1':'PAPlayer','videoplayer_pcm_v1':'VideoPlayer','videoplayer_video_v1':'VideoPlayer'}
    if mode not in modes:raise ValueError('explicit_player_mode_required')
    return mode,modes[mode]


def owned_open_request(nonce, mode):
    import re
    mode,core=player_contract(mode)
    if not isinstance(nonce,str) or not re.fullmatch('[0-9a-f]{32}',nonce):raise ValueError('invalid_launch_nonce')
    return {'jsonrpc':'2.0','id':1,'method':'Player.Open','params':{
        'item':{'file':'plugin://plugin.audio.kodi_donor_radio/?owned_resolve='+nonce+'&player_mode='+mode},
        'options':{'playername':core,'resume':False}}}


def owned_item_properties(binding):
    mode,core=player_contract(binding['player_mode'])
    if binding['requested_core']!=core or binding['selected_url']!='http://127.0.0.1:18789/live/index.m3u8':
        raise ValueError('resolved_player_binding')
    video=mode=='videoplayer_video_v1'
    queue=binding.get('video_queue_tenths')
    if (video and (type(queue) is not int or queue!=40)) or (not video and queue is not None):
        raise ValueError('resolved_video_queue_binding')
    properties={'inputstream':'inputstream.ffmpegdirect',
        'inputstream-player':'videodefaultplayer' if core=='VideoPlayer' else 'audiodefaultplayer',
        'inputstream.ffmpegdirect.stream_mode':'timeshift','inputstream.ffmpegdirect.is_realtime_stream':'false',
        'inputstream.ffmpegdirect.terminal_eof':'true','inputstream.ffmpegdirect.owned_clock_nonce':binding['nonce'],
        'inputstream.ffmpegdirect.owned_clock_timeline':binding['timeline_sha256'],
        'inputstream.ffmpegdirect.owned_clock_first_packet':binding['first_packet_sha256'],
        'inputstream.ffmpegdirect.owned_player_mode':mode,'inputstream.ffmpegdirect.owned_player_core':core}
    if video:properties['inputstream.ffmpegdirect.owned_player_queue_tenths']='40'
    return properties


def owned_item(binding):
    import xbmcgui
    item=xbmcgui.ListItem(label='Owned clock controlled beacons',path=binding['selected_url'])
    item.setMimeType('application/vnd.apple.mpegurl');item.setContentLookup(False)
    (item.getVideoInfoTag() if binding['player_mode']=='videoplayer_video_v1' else item.getMusicInfoTag()).setTitle('Owned clock controlled beacons')
    for name,value in owned_item_properties(binding).items():item.setProperty(name,value)
    return item


def resolve_owned(nonce,mode):
    """Resolve only a pre-created, monitor-claimed, fresh one-use launch."""
    import json
    from dataclasses import asdict
    from pathlib import Path
    import xbmcplugin
    sys.path.insert(0,str(Path(__file__).resolve().parent/'resources/lib'))
    from owned_clock import OwnedClockBinding,OwnedClockTransport,SESSION_ROOT,mach_host_ns
    owned_open_request(nonce,mode)  # validate before constructing a path
    transport=OwnedClockTransport(SESSION_ROOT/nonce)
    try:
        launch=json.loads(transport._read_immutable('launch.json',128*1024)[0])
        binding=OwnedClockBinding(**launch['binding'])
        if (binding.nonce!=nonce or binding.player_mode!=mode or binding.incarnation is not None
                or binding.native_pid!=os.getpid() or not 0<=mach_host_ns()-binding.launch_mach_ns<=30_000_000_000):
            raise ValueError('resolved_launch_binding')
        ready=json.loads(transport._read_immutable('monitor-ready.json',2048)[0])
        expected={k:asdict(binding)[k] for k in ('nonce','timeline_sha256','native_pid','player_mode','requested_core')}
        if binding.video_queue_tenths is not None:expected['video_queue_tenths']=binding.video_queue_tenths
        expected['kind']='owned_monitor_ready'
        if ready!=expected:raise ValueError('resolved_monitor_binding')
        if (transport.directory/'hello.json').exists() or (transport.directory/'native.jsonl').exists():
            raise ValueError('resolved_launch_reuse')
        fd=os.open('resolved-claimed',os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600,dir_fd=transport.fd)
        os.close(fd)
        xbmcplugin.setResolvedUrl(int(sys.argv[1]),True,owned_item(asdict(binding)))
    finally:transport.close()


def launch_owned(fixture, mode="paplayer_pcm_v1"):
    mode,core=player_contract(mode)
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
    validate_lab_manifest(Path(xbmcaddon.Addon('inputstream.ffmpegdirect').getAddonInfo('path'))/'addon.xml',owned=True,video=core=='VideoPlayer',video_control=mode=='videoplayer_video_v1')
    runtime_clock.frozen_module()  # Refuse altered estimator before native Open.
    settings={}
    for setting in ('audiodevice','lowlatency','channels','config','processquality','stereoupmix','passthrough'):
        name='audiooutput.'+setting
        response=json.loads(xbmc.executeJSONRPC(json.dumps({'jsonrpc':'2.0','id':1,'method':'Settings.GetSettingValue','params':{'setting':name}})))
        if 'error' in response:raise RuntimeError('Cannot verify owned output setting '+name)
        settings[name]=response['result']['value']
    response=json.loads(xbmc.executeJSONRPC(json.dumps({'jsonrpc':'2.0','id':1,'method':'Settings.GetSettingValue','params':{'setting':'videoplayer.queuetimesize'}})))
    if 'error' in response:raise RuntimeError('Cannot verify VideoPlayer queue setting')
    settings['videoplayer.queuetimesize']=response['result']['value']
    fixture_api.validate_profile(settings)
    if mode=='videoplayer_video_v1' and (type(settings['videoplayer.queuetimesize']) is not int or settings['videoplayer.queuetimesize']!=40):
        raise RuntimeError('Video control requires observed queue40')
    nonce=uuid.uuid4().hex
    presentation,proof=fixture_api.resolve_presentation(Path(fixture),nonce)
    timeline=canonical_timeline(presentation,fixture_api.FIRST_PACKET)
    binding=OwnedClockBinding(nonce,None,fixture_api.sha(timeline),fixture_api.FIRST_PACKET,
                              fixture_api.URL,mach_host_ns(),os.getpid(),player_mode=mode,requested_core=core,video_queue_tenths=40 if mode=="videoplayer_video_v1" else None)
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
        if core=='VideoPlayer':
            request=owned_open_request(nonce,mode)
            private_write('player-open-request.json',fixture_api.canonical(request))
            response=json.loads(xbmc.executeJSONRPC(json.dumps(request)))
            if response.get('result')!='OK' or 'error' in response:raise RuntimeError('Named player launch rejected')
            private_write('player-open-response.json',fixture_api.canonical(response))
        else:
            xbmc.Player().play(fixture_api.URL,owned_item(asdict(binding)))
        xbmcplugin.endOfDirectory(int(sys.argv[1]))
    finally:transport.close()


if __name__ == '__main__':
    from urllib.parse import parse_qs
    query=parse_qs(sys.argv[2].lstrip('?'),keep_blank_values=True) if len(sys.argv)>2 else {}
    if 'owned_resolve' in query:
        if set(query)!={'owned_resolve','player_mode'} or any(len(v)!=1 for v in query.values()):raise ValueError('resolved_arguments')
        resolve_owned(query['owned_resolve'][0],query['player_mode'][0])
    elif query.get('owned_beacons')==['1']:
        if not {'owned_beacons','fixture'}.issubset(query) or set(query)-{'owned_beacons','fixture','player_mode'} or any(len(v)!=1 for v in query.values()):raise RuntimeError('Explicit fixture required')
        launch_owned(query['fixture'][0],query.get('player_mode',['paplayer_pcm_v1'])[0])
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
