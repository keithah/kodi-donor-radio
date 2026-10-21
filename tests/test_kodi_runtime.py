# SPDX-License-Identifier: GPL-2.0-or-later
"""Narrow Kodi boundary fakes; relay/session behavior stays real where possible."""
import importlib
import json
import os
from pathlib import Path
import sys
import threading
import time
from types import SimpleNamespace
from urllib.parse import urlencode, urlsplit, parse_qs
import xml.etree.ElementTree as ET

import pytest

from control import ControlClient, ServiceDescriptor
from models import Station
from relay import RelayServer
from sessions import SessionManager

ADDON = Path(__file__).resolve().parents[1] / 'plugin.audio.kodi_donor_radio'
STATION = Station('station-1', 'Station One', 'https://radio.example/live', 'MP3', False)


@pytest.fixture
def kodi(monkeypatch, tmp_path):
    state = SimpleNamespace(items=[], resolved=[], ended=[], messages=[], notifications=[], logs=[], settings={},
                            input='jazz', cancel=False, abort=False, waits=0, players=[], action=None)
    class Item:
        def __init__(self, label='', path=''):
            self.label, self.path, self.props = label, path, {}
        def setProperty(self, key, value): self.props[key] = value
        def setMimeType(self, value): self.mime = value
        def setContentLookup(self, value): self.lookup = value
    class Dialog:
        def input(self, heading): state.messages.append(heading); return state.input
        def ok(self, heading, message): state.messages.append(message)
        def notification(self, heading, message, icon, time):
            state.notifications.append((heading, message, icon, time))
    class Progress:
        def create(self, heading, message): state.messages.append(message)
        def iscanceled(self): return state.cancel
        def close(self): pass
    class Monitor:
        def abortRequested(self): return state.abort
        def waitForAbort(self, timeout):
            state.waits += 1
            if state.action: state.action(self)
            time.sleep(min(timeout, .001))
            return state.abort
    class Player:
        def __init__(self): self.file = ''; self.playing = False; state.players.append(self)
        def getPlayingFile(self): return self.file
        def isPlaying(self): return self.playing
    class Addon:
        def getAddonInfo(self, key): return {'profile': str(tmp_path), 'name': 'Kodi Donor Radio'}[key]
        def getLocalizedString(self, key): return 'localized-%d' % key
        def getSettingBool(self, key): return state.settings.get(key, False)
        def getSetting(self, key): return state.settings.get(key, '')
        def openSettings(self): state.settings_opened = True
    modules = {
        'xbmc': SimpleNamespace(Monitor=Monitor, Player=Player, LOGDEBUG=0,
                                log=lambda value, level: state.logs.append(value)),
        'xbmcaddon': SimpleNamespace(Addon=Addon),
        'xbmcgui': SimpleNamespace(ListItem=Item, Dialog=Dialog, DialogProgress=Progress,
                                  NOTIFICATION_ERROR='error'),
        'xbmcplugin': SimpleNamespace(
            addDirectoryItem=lambda handle, url, listitem, isFolder=False: state.items.append((url,listitem,isFolder)),
            endOfDirectory=lambda handle, **kwargs: state.ended.append(kwargs),
            setResolvedUrl=lambda handle, succeeded, listitem: state.resolved.append((succeeded,listitem)),
            setContent=lambda *args: None),
        'xbmcvfs': SimpleNamespace(translatePath=lambda value: value),
    }
    for name, module in modules.items(): monkeypatch.setitem(sys.modules, name, module)
    sys.modules.pop('kodi_runtime', None)
    runtime = importlib.import_module('kodi_runtime')
    yield runtime, state, tmp_path
    sys.modules.pop('kodi_runtime', None)


def route(runtime, **params):
    runtime.run_plugin(['plugin://plugin.audio.kodi_donor_radio', '7', '?' + urlencode(params)])


class Directory:
    def __init__(self, cancel=None): self.cancel=cancel; self.searches=[]; self.clicks=[]
    def search(self, name, country, tag, offset, limit=50):
        self.searches.append((name,country,tag,offset,limit)); return [STATION] * 50
    def countries(self): return ['Canada', 'France']
    def tags(self, offset, limit=50): self.tag_page=(offset,limit); return ['jazz'] * 50
    def click(self, uuid): self.clicks.append(uuid)


def directory(monkeypatch, runtime):
    result=Directory(); monkeypatch.setattr(runtime,'RadioBrowser',lambda cancel=None: result); return result


def test_home_hides_test_streams_and_uses_localized_labels(kodi):
    runtime,state,_=kodi
    route(runtime)
    assert [item.label for _,item,_ in state.items] == ['localized-30001','localized-30002','localized-30003','localized-30004']
    assert all('tests' not in url for url,_,_ in state.items)
    assert state.ended == [{'cacheToDisc': False}]


@pytest.mark.parametrize('params,expected', [
    ({'action':'search'},('jazz','','',0,50)),
    ({'action':'stations','country':'France','offset':'50'},('','France','',50,50)),
    ({'action':'stations','tag':'jazz','offset':'100'},('','','jazz',100,50)),
])
def test_station_navigation_keeps_filters_and_pages(kodi,monkeypatch,params,expected):
    runtime,state,_=kodi; browser=directory(monkeypatch,runtime)
    route(runtime,**params)
    assert browser.searches == [expected]
    assert len(state.items)==51
    next_params=parse_qs(urlsplit(state.items[-1][0]).query)
    assert next_params['offset'] == [str(expected[3]+50)]
    assert state.items[0][1].props['IsPlayable']=='true'
    assert state.items[-1][1].label=='localized-30005'


def test_country_and_tag_navigation(kodi,monkeypatch):
    runtime,state,_=kodi; browser=directory(monkeypatch,runtime)
    route(runtime,action='countries')
    assert [item.label for _,item,_ in state.items]==['Canada','France']
    state.items.clear(); route(runtime,action='tags',offset=50)
    assert browser.tag_page==(50,50)
    assert parse_qs(urlsplit(state.items[-1][0]).query)['offset']==['100']


def test_cancelled_search_does_not_fetch(kodi,monkeypatch):
    runtime,state,_=kodi; browser=directory(monkeypatch,runtime); state.input=''
    route(runtime,action='search')
    assert browser.searches==[] and state.items==[]


@pytest.mark.parametrize('click_failure', [False, True])
def test_playback_resolves_only_service_url_reports_click_once(kodi,monkeypatch,click_failure):
    runtime,state,profile=kodi; browser=directory(monkeypatch,runtime)
    descriptor=ServiceDescriptor(32123,'a'*48,'b'*64); runtime.publish_descriptor(profile,descriptor)
    created=[]; stopped=[]; click_states=[]
    def click(uuid):
        browser.clicks.append(uuid)
        click_states.append(list(state.resolved))
        if click_failure:
            raise OSError('Directory unavailable')
    browser.click=click
    class Client:
        def __init__(self,descriptor,timeout=10): self.descriptor=descriptor
        def health(self): return {'status':'ok','instance_id':'a'*48}
        def stop(self,session_id): stopped.append(session_id)
        def create(self,station): created.append(station); return {'id':'c'*48,'playback_url':'http://127.0.0.1:32123/media/'+'c'*48+'/'+'d'*48}
    monkeypatch.setattr(runtime,'ControlClient',Client)
    route(runtime,action='play',station=json.dumps(STATION.__dict__))
    assert created==[STATION] and browser.clicks==['station-1']
    assert len(state.resolved)==1 and state.resolved[0][0]
    assert click_states == [state.resolved]
    assert stopped == []
    assert 'localized-30011' not in state.messages
    assert state.resolved[0][1].path=='http://127.0.0.1:32123/media/'+'c'*48+'/'+'d'*48
    # Directory codec/HLS fields are hints; the relay's response is authoritative.
    assert not hasattr(state.resolved[0][1], 'mime')
    assert getattr(state.resolved[0][1], 'lookup', True)


@pytest.mark.parametrize('failure', ['missing','invalid','upstream','malformed'])
def test_unavailable_service_localized_failure_no_direct_fallback(kodi,monkeypatch,failure):
    runtime,state,profile=kodi; directory(monkeypatch,runtime)
    monkeypatch.setattr(runtime,'STARTUP_TIMEOUT',.003)
    if failure!='missing':
        runtime.publish_descriptor(profile,ServiceDescriptor(32123,'a'*48,'b'*64))
        class Client:
            def __init__(self,descriptor,timeout=10): self.descriptor=descriptor
            def health(self): return {'status':'ok','instance_id':'a'*48}
            def create(self,station):
                if failure=='upstream': raise OSError('secret upstream url')
                if failure=='malformed': return None
                return {'id':'c'*48,'playback_url':STATION.url}
            def stop(self,session_id): pass
        monkeypatch.setattr(runtime,'ControlClient',Client)
    route(runtime,action='play',station=json.dumps(STATION.__dict__))
    assert state.resolved and not state.resolved[0][0] and not state.resolved[0][1].path
    assert state.messages[-1] in ('localized-30010','localized-30011')
    assert not state.logs


def test_directory_error_offers_localized_retry(kodi,monkeypatch):
    runtime,state,_=kodi; browser=directory(monkeypatch,runtime)
    browser.countries=lambda: (_ for _ in ()).throw(OSError('sensitive'))
    route(runtime,action='countries')
    assert state.messages[-1]=='localized-30012'
    assert state.items[-1][1].label=='localized-30006'
    assert 'action=countries' in state.items[-1][0]


def test_startup_wait_is_abortable(kodi,monkeypatch):
    runtime,state,_=kodi; directory(monkeypatch,runtime); state.abort=True
    before=time.monotonic(); route(runtime,action='play',station=json.dumps(STATION.__dict__))
    assert time.monotonic()-before < .1
    assert not state.resolved[0][0]


def test_cancelled_create_stops_late_session(kodi,monkeypatch):
    runtime,state,profile=kodi; browser=directory(monkeypatch,runtime)
    runtime.publish_descriptor(profile,ServiceDescriptor(32123,'a'*48,'b'*64))
    released=threading.Event(); stopped=threading.Event(); entered=threading.Event()
    class Client:
        def __init__(self,descriptor,timeout=10): self.descriptor=descriptor
        def health(self): return {'status':'ok','instance_id':'a'*48}
        def create(self,station):
            entered.set()
            released.wait(1)
            return {'id':'c'*48,'playback_url':'http://127.0.0.1:32123/media/'+'c'*48+'/'+'d'*48}
        def stop(self,session_id): assert session_id=='c'*48; stopped.set()
    monkeypatch.setattr(runtime,'ControlClient',Client)
    state.action=lambda monitor: setattr(state,'cancel',True) if entered.is_set() else None
    route(runtime,action='play',station=json.dumps(STATION.__dict__))
    released.set()
    assert stopped.wait(1) and not state.resolved[0][0] and browser.clicks==[]


def test_descriptor_atomic_private_and_old_service_cannot_remove_new(kodi):
    runtime,_,profile=kodi
    old=ServiceDescriptor(32123,'a'*48,'b'*64); new=ServiceDescriptor(32124,'c'*48,'d'*64)
    runtime.publish_descriptor(profile,old)
    assert runtime.read_descriptor(profile)==old
    if os.name!='nt':
        assert profile.stat().st_mode & 0o777==0o700
        assert (profile/'service.json').stat().st_mode & 0o777==0o600
    runtime.publish_descriptor(profile,new)
    runtime.remove_descriptor(profile,old)
    assert runtime.read_descriptor(profile)==new
    runtime.remove_descriptor(profile,new)
    assert not (profile/'service.json').exists()


@pytest.mark.parametrize('value',[{'port':True,'instance_id':'a'*48,'control_token':'b'*64},
    {'port':32123,'instance_id':'oops','control_token':'b'*64},
    {'port':32123,'instance_id':'a'*48,'control_token':'b'*64,'url':'http://evil'}])
def test_descriptor_rejects_malformed_records(kodi,value):
    runtime,_,profile=kodi; (profile/'service.json').write_text(json.dumps(value))
    with pytest.raises((ValueError,OSError)): runtime.read_descriptor(profile)


def test_test_origin_is_exact_loopback_and_off_by_default(kodi):
    runtime,state,_=kodi
    assert not runtime.runtime_policy().test_origins
    state.settings.update(test_mode=True,test_origin='http://127.0.0.1:8765')
    policy=runtime.runtime_policy()
    assert policy.resolve('http://127.0.0.1:8765/icy.mp3').port==8765
    for url in ('http://127.0.0.1:8766/icy.mp3','http://127.0.0.2:8765/x'):
        with pytest.raises(ValueError): policy.resolve(url)
    for origin in ('http://localhost:8765','https://127.0.0.1:8765','http://127.0.0.1:8765/path','http://192.168.1.1:8765'):
        state.settings['test_origin']=origin
        with pytest.raises(ValueError): runtime.runtime_policy()


def test_test_navigation_opt_in_localized_and_disabled_route_rejected(kodi):
    runtime,state,_=kodi
    route(runtime,action='tests'); assert not state.items
    state.settings.update(test_mode=True,test_origin='http://127.0.0.1:8765')
    route(runtime,action='tests')
    assert len(state.items)==8
    assert all(item.label.startswith('localized-') for _,item,_ in state.items)
    station=json.loads(parse_qs(urlsplit(state.items[0][0]).query)['station'][0])
    assert station['url']=='http://127.0.0.1:8765/continuous.mp3'
    paths=[json.loads(parse_qs(urlsplit(url).query)['station'][0])['url'] for url,_,_ in state.items]
    assert paths[5]=='http://127.0.0.1:8765/hls/missing-end/index.m3u8'


@pytest.mark.parametrize('callback',['onPlayBackStopped','onPlayBackEnded','onPlayBackError'])
def test_player_callbacks_release_owned_stream_not_new_session(kodi,callback):
    runtime,state,_=kodi
    manager=SimpleNamespace(stop=lambda value: stopped.append(value))
    stopped=[]; player=runtime.RelayPlayer(manager,32123)
    player.file='http://127.0.0.1:32123/media/'+'a'*48+'/'+'b'*48
    player.playing=True; player.onAVStarted()
    player.playing=False; getattr(player,callback)()
    assert stopped==['a'*48]
    getattr(player,callback)(); assert stopped==['a'*48]
    player.file=STATION.url; player.playing=True; player.onAVStarted()
    player.playing=False; getattr(player,callback)(); assert stopped==['a'*48]


def test_service_monitor_shutdown_removes_descriptor_and_expires(kodi,monkeypatch):
    runtime,state,profile=kodi
    seen=[]
    state.action=lambda monitor: (seen.append(runtime.read_descriptor(profile)),setattr(state,'abort',True))
    runtime.run_service()
    assert len(seen)==1 and seen[0].port>0
    assert not (profile/'service.json').exists() and not state.logs
    with pytest.raises(OSError): ControlClient(seen[0],timeout=.1).health()


def test_service_settings_restart_invalidates_stale_token(kodi,monkeypatch):
    runtime,state,profile=kodi; descriptors=[]
    def action(monitor):
        descriptors.append(runtime.read_descriptor(profile))
        if len(descriptors)==1:
            state.settings['diagnostics']=True; monitor.onSettingsChanged()
        else: state.abort=True
    state.action=action; runtime.run_service()
    assert len(descriptors)==2 and descriptors[0]!=descriptors[1]
    assert not (profile/'service.json').exists()


def test_debug_logging_is_opt_in(kodi):
    runtime,state,profile=kodi
    from models import Event
    event=Event('cue_observed','id','station','hls','cue','plain','2026-10-21T00:00:00+00:00',1)
    runtime.event_sink(profile).emit(event)
    assert not state.logs and not (profile/'events.jsonl').exists()
    state.settings['diagnostics']=True; runtime.event_sink(profile).emit(event)
    assert len(state.logs)==1 and (profile/'events.jsonl').is_file()


def test_xml_and_all_localized_references_have_translations(kodi):
    runtime,state,_=kodi
    addon=ET.parse(ADDON/'addon.xml').getroot(); settings=ET.parse(ADDON/'resources/settings.xml').getroot()
    assert addon.find("requires/import[@addon='xbmc.python']").get('version')=='3.0.1'
    assert addon.find("extension[@point='xbmc.python.pluginsource']/provides").text=='audio'
    assert addon.find("extension[@point='xbmc.service']").get('library')=='service.py'
    assert settings.find(".//setting[@id='test_mode']/default").text=='false'
    assert settings.find(".//setting[@id='diagnostics']/default").text=='false'
    import re
    strings=(ADDON/'resources/language/resource.language.en_gb/strings.po').read_text()
    ids=set(map(int,re.findall(r'msgctxt "#(\d+)"',strings)))
    source=Path(runtime.__file__).read_text()
    references=set(map(int,re.findall(r'\b30\d{3}\b',source)))
    references.update(int(node.get('label')) for node in settings.iter() if node.get('label','').isdigit())
    assert references <= ids


def test_early_player_error_releases_session_before_av_started(kodi):
    runtime,_,_=kodi; stopped=[]
    player=runtime.RelayPlayer(SimpleNamespace(stop=stopped.append),32123)
    player.file='http://127.0.0.1:32123/media/'+'a'*48+'/'+'b'*48
    player.playing=True; player.onPlayBackStarted()
    player.playing=False; player.onPlayBackError()
    assert stopped==['a'*48]


def test_late_stop_callback_does_not_stop_current_playback(kodi):
    runtime,_,_=kodi; stopped=[]
    player=runtime.RelayPlayer(SimpleNamespace(stop=stopped.append),32123)
    player.file='http://127.0.0.1:32123/media/'+'a'*48+'/'+'b'*48
    player.playing=True; player.onAVStarted(); player.onPlayBackStopped()
    assert stopped==[]
    player.file='http://127.0.0.1:32123/media/'+'c'*48+'/'+'d'*48
    player.onAVStarted(); assert stopped==['a'*48]
    player.playing=False; player.onPlayBackStopped(); assert stopped==['a'*48,'c'*48]


@pytest.mark.parametrize('recover', [True, False])
def test_service_error_remains_responsive_without_modal_dialog(kodi,monkeypatch,recover):
    runtime,state,profile=kodi
    state.settings.update(test_mode=True,test_origin='http://private.example')
    def modal(*args):
        pytest.fail('Background service must not wait for a modal dialog')
    monkeypatch.setattr(runtime.xbmcgui.Dialog, 'ok', modal)
    visits=[]
    def action(monitor):
        if not visits:
            visits.append('invalid')
            if recover:
                state.settings['test_mode']=False; monitor.onSettingsChanged()
            else:
                state.abort=True
        else:
            visits.append(runtime.read_descriptor(profile)); state.abort=True
    state.action=action; runtime.run_service()
    if recover:
        assert len(visits)==2 and visits[1].port>0
    else:
        assert visits==['invalid']
    assert not (profile/'service.json').exists()
    assert state.notifications==[('localized-30000','localized-30014','error',5000)]


def test_actual_relay_switch_and_callback_cancel_old_sources(kodi):
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from http_client import HttpClient
    from url_policy import UrlPolicy
    runtime,_,_=kodi
    class Origin(BaseHTTPRequestHandler):
        def do_GET(self):
            payload=b'#EXTM3U\n#EXT-X-TARGETDURATION:2\n#EXTINF:2,\n/segment.ts\n'
            self.send_response(200); self.send_header('Content-Type','application/vnd.apple.mpegurl')
            self.send_header('Content-Length',str(len(payload))); self.end_headers(); self.wfile.write(payload)
        def log_message(self,*args): pass
    origin=ThreadingHTTPServer(('127.0.0.1',0),Origin)
    worker=threading.Thread(target=origin.serve_forever,daemon=True); worker.start()
    base='http://127.0.0.1:%d'%origin.server_port
    manager=SessionManager(client=HttpClient(policy=UrlPolicy(test_origins={base})))
    server=RelayServer(manager); descriptor=server.start()
    try:
        client=ControlClient(descriptor)
        first=client.create(Station('1','First',base+'/one','MP3',False))
        old=manager.get(first['id'])
        assert old.source.protocol=='hls'  # Content overrides wrong directory flags.
        second=client.create(Station('2','Second',base+'/two','AAC',True))
        assert old.cancel.is_set() and manager.get(first['id']) is None
        assert manager.get(second['id']) is not None
        player=runtime.RelayPlayer(manager,descriptor.port)
        player.file=second['playback_url']; player.playing=True; player.onAVStarted()
        player.playing=False; player.onPlayBackStopped()
        assert manager.get(second['id']) is None
        # A restarted instance cannot accept a token persisted by the old instance.
        server.close(); server=RelayServer(); fresh=server.start()
        with pytest.raises(OSError):
            ControlClient(ServiceDescriptor(fresh.port,descriptor.instance_id,descriptor.control_token)).stop('a'*48)
    finally:
        server.close(); origin.shutdown(); origin.server_close(); worker.join(1)


@pytest.mark.parametrize('suffix', ['.m3u8', '.ts', '.mp4', '.m4s', '.aac', '.mp3', '.m4a', '.ac3', '.ec3'])
def test_adapter_recognizes_safe_suffix_playback_routes(kodi, suffix):
    runtime, _, _ = kodi
    url = 'http://127.0.0.1:32123/media/' + 'a' * 48 + '/' + 'b' * 48
    assert runtime._relay_path(url + suffix, 32123) == 'a' * 48
    assert runtime._relay_path(url + '.exe', 32123) is None
    assert runtime._relay_path(url + suffix + '?upstream=secret', 32123) is None
