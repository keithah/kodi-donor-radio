import unittest
import importlib.util
import json
from pathlib import Path
import pytest

spec=importlib.util.spec_from_file_location('player_launcher',Path(__file__).with_name('default.py'))
launcher=importlib.util.module_from_spec(spec);spec.loader.exec_module(launcher)

def test_explicit_mode_selection_and_request():
    assert launcher.player_contract('paplayer_pcm_v1')==('paplayer_pcm_v1','PAPlayer')
    assert launcher.player_contract('videoplayer_pcm_v1')==('videoplayer_pcm_v1','VideoPlayer')
    for bad in ('','VideoPlayer','auto',None):
        with pytest.raises(ValueError):launcher.player_contract(bad)
    request=launcher.owned_open_request('a'*32,'videoplayer_pcm_v1')
    assert request['params']['options']=={'playername':'VideoPlayer','resume':False}
    assert request['params']['item']['file']=='plugin://plugin.audio.kodi_donor_radio/?owned_resolve='+('a'*32)+'&player_mode=videoplayer_pcm_v1'
    with pytest.raises(ValueError):launcher.owned_open_request('../other','videoplayer_pcm_v1')

def test_resolved_properties_bind_core_mode_and_source():
    mode='videoplayer_pcm_v1'
    b=dict(nonce='a'*32,timeline_sha256='b'*64,first_packet_sha256='c'*64,selected_url='http://127.0.0.1:18789/live/index.m3u8',player_mode=mode,requested_core='VideoPlayer',video_queue_tenths=40 if mode=='videoplayer_video_v1' else None)
    p=launcher.owned_item_properties(b)
    assert p['inputstream.ffmpegdirect.owned_player_mode']=='videoplayer_pcm_v1'
    assert p['inputstream.ffmpegdirect.owned_player_core']=='VideoPlayer'
    assert p['inputstream.ffmpegdirect.owned_clock_first_packet']=='c'*64
    assert p['inputstream-player']=='videodefaultplayer'
    for key,value in [('requested_core','PAPlayer'),('selected_url','http://elsewhere/'),('player_mode','')]:
        with pytest.raises(ValueError):launcher.owned_item_properties(dict(b,**{key:value}))


@pytest.mark.parametrize("mode",["videoplayer_pcm_v1","videoplayer_video_v1"])
def test_real_session_resolution_passes_properties_once_and_rejects_mode_conflict(tmp_path,monkeypatch,mode):
    import sys,os,types
    from dataclasses import asdict
    sys.path.insert(0,str(Path(__file__).resolve().parents[3]/'plugin.audio.kodi_donor_radio/resources/lib'))
    import owned_clock
    monkeypatch.setattr(owned_clock,'SESSION_ROOT',tmp_path)
    monkeypatch.setattr(owned_clock,'mach_host_ns',lambda:100)
    binding=owned_clock.OwnedClockBinding('a'*32,None,'b'*64,'c'*64,owned_clock.URL,90,os.getpid(),player_mode=mode,requested_core='VideoPlayer',video_queue_tenths=40 if mode=='videoplayer_video_v1' else None)
    transport=owned_clock.OwnedClockTransport.create(tmp_path,binding)
    d=transport.directory
    ready={k:asdict(binding)[k] for k in ('nonce','timeline_sha256','native_pid','player_mode','requested_core')};ready['kind']='owned_monitor_ready'
    if binding.video_queue_tenths is not None:ready['video_queue_tenths']=binding.video_queue_tenths
    for name,value in [('launch.json',{'binding':asdict(binding)}),('monitor-ready.json',ready)]:
        p=d/name;p.write_text(json.dumps(value));p.chmod(0o600)
    resolved=[]
    class Item:
        def __init__(self,**kw):self.path=kw['path'];self.properties={}
        def setMimeType(self,v):pass
        def setContentLookup(self,v):pass
        def getMusicInfoTag(self):return self
        def getVideoInfoTag(self):return self
        def setTitle(self,v):pass
        def setProperty(self,k,v):self.properties[k]=v
    monkeypatch.setitem(sys.modules,'xbmcgui',types.SimpleNamespace(ListItem=Item))
    monkeypatch.setitem(sys.modules,'xbmcplugin',types.SimpleNamespace(setResolvedUrl=lambda *a:resolved.append(a)))
    monkeypatch.setattr(sys,'argv',['plugin://plugin.audio.kodi_donor_radio','5'])
    with pytest.raises(ValueError):launcher.resolve_owned(binding.nonce,'paplayer_pcm_v1')
    assert not resolved
    launcher.resolve_owned(binding.nonce,mode)
    handle,ok,item=resolved[0]
    assert handle==5 and ok and item.path==owned_clock.URL
    assert item.properties==launcher.owned_item_properties(asdict(binding))
    with pytest.raises(FileExistsError):launcher.resolve_owned(binding.nonce,mode)
    assert len(resolved)==1
    transport.close()

class VideoControlLaunchTests(unittest.TestCase):
    def test_video_control_requires_queue_binding(self):
        mode,core=launcher.player_contract('videoplayer_video_v1')
        self.assertEqual(core,'VideoPlayer')
        b=dict(nonce='a'*32,player_mode=mode,requested_core=core,selected_url='http://127.0.0.1:18789/live/index.m3u8',timeline_sha256='b'*64,first_packet_sha256='c'*64,video_queue_tenths=40)
        self.assertEqual(launcher.owned_item_properties(b)['inputstream.ffmpegdirect.owned_player_queue_tenths'],'40')
        self.assertEqual(launcher.owned_open_request(b['nonce'],mode)['params']['options']['playername'],'VideoPlayer')
        for queue in (None,39,41,'40',True):
            b['video_queue_tenths']=queue
            with self.assertRaises(ValueError):launcher.owned_item_properties(b)
