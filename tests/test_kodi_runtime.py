# SPDX-License-Identifier: GPL-2.0-or-later
"""Narrow Kodi boundary fakes; relay/session behavior stays real where possible."""
import importlib
import json
import os
from pathlib import Path
import re
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


def station_payload(station):
    return json.dumps({key: getattr(station, key)
                       for key in ('uuid', 'name', 'url', 'codec', 'hls')})


@pytest.fixture
def kodi(monkeypatch, tmp_path):
    state = SimpleNamespace(items=[], resolved=[], ended=[], messages=[], notifications=[], logs=[], settings={},
                            input='jazz', cancel=False, abort=False, waits=0, players=[], action=None,
                            player_paused=False, player_caching=False, selections=[], confirmations=[],
                            dialog_choices=None, dialog_inputs=None, builtins=[], contents=[],
                            selection=-1, remember=False, memory_selection=None, on_select=None, on_confirm=None)
    class Item:
        def __init__(self, label='', path=''):
            self.label, self.path, self.props = label, path, {}
            self.label2, self.art, self.info, self.context = '', {}, {}, []
        def addContextMenuItems(self, values): self.context.extend(values)
        def setLabel2(self, value): self.label2 = value
        def setArt(self, value): self.art = value
        def setInfo(self, kind, value): self.info[kind] = value
        def setIsFolder(self, value): self.folder = value
        def setProperty(self, key, value): self.props[key] = value
        def setMimeType(self, value): self.mime = value
        def setContentLookup(self, value): self.lookup = value
    class Dialog:
        def input(self, heading):
            state.messages.append(heading)
            return state.dialog_inputs.pop(0) if state.dialog_inputs is not None else state.input
        def ok(self, heading, message): state.messages.append(message)
        def select(self, heading, options):
            state.selections.append((heading, options))
            if state.dialog_choices is not None:
                if state.on_select: state.on_select()
                return state.dialog_choices.pop(0)
            if len(state.selections) == 2:
                state.confirmations.append((heading, options))
                if state.on_confirm: state.on_confirm()
                if state.memory_selection is not None: return state.memory_selection
                return 0 if state.remember else 1
            if state.on_select: state.on_select()
            return state.selection
        def yesno(self, heading, message):
            state.confirmations.append((heading, message))
            if state.on_confirm: state.on_confirm()
            return state.remember
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
        def play(self, path, listitem): self.file = path; self.playing = True; self.item = listitem
    class Addon:
        def getAddonInfo(self, key): return {'profile': str(tmp_path), 'name': 'Kodi Donor Radio', 'path': str(ADDON)}[key]
        def getLocalizedString(self, key): return 'localized-%d' % key
        def getSettingBool(self, key): return state.settings.get(key, False)
        def getSetting(self, key): return state.settings.get(key, '')
        def openSettings(self): state.settings_opened = True
    modules = {
        'xbmc': SimpleNamespace(Monitor=Monitor, Player=Player, LOGDEBUG=0,
                                getCondVisibility=lambda condition: state.player_paused if condition == 'Player.Paused' else state.player_caching,
                                executebuiltin=lambda value: state.builtins.append(value),
                                log=lambda value, level: state.logs.append(value)),
        'xbmcaddon': SimpleNamespace(Addon=Addon),
        'xbmcgui': SimpleNamespace(ListItem=Item, Dialog=Dialog, DialogProgress=Progress,
                                  NOTIFICATION_ERROR='error'),
        'xbmcplugin': SimpleNamespace(
            addDirectoryItem=lambda handle, url, listitem, isFolder=False: state.items.append((url,listitem,isFolder)),
            endOfDirectory=lambda handle, **kwargs: state.ended.append(kwargs),
            setResolvedUrl=lambda handle, succeeded, listitem: state.resolved.append((succeeded,listitem)),
            setContent=lambda *args: state.contents.append(args)),
        'xbmcvfs': SimpleNamespace(translatePath=lambda value: value),
    }
    for name, module in modules.items(): monkeypatch.setitem(sys.modules, name, module)
    sys.modules.pop('kodi_runtime', None)
    runtime = importlib.import_module('kodi_runtime')
    yield runtime, state, tmp_path
    sys.modules.pop('kodi_runtime', None)


def route(runtime, **params):
    runtime.run_plugin(['plugin://plugin.audio.kodi_donor_radio', '7', '?' + urlencode(params)])


def install_map_window(monkeypatch):
    opened = []
    class MapWindow:
        def __init__(self, **kwargs): self.kwargs = kwargs; opened.append(self)
        def doModal(self): self.modal = True
    monkeypatch.setitem(sys.modules, 'kodi_radio_map', SimpleNamespace(RadioMapWindow=MapWindow))
    return opened


class Directory:
    def __init__(self, cancel=None): self.cancel=cancel; self.searches=[]; self.clicks=[]; self.lookups=[]
    def search(self, name, country, tag, offset, limit=50):
        self.searches.append((name,country,tag,offset,limit)); return [STATION] * 50
    def search_page(self, name, country, tag, offset, limit=50):
        stations = self.search(name, country, tag, offset, limit)
        return stations, len(stations) == limit
    def countries(self): return ['Canada', 'France']
    def tags(self, offset, limit=50): self.tag_page=(offset,limit); return ['jazz'] * 50
    def click(self, uuid): self.clicks.append(uuid)
    def by_uuid(self, uuid): self.lookups.append(uuid); return STATION
    def country_entries(self): return [('Canada', 'CA'), ('France', 'FR')]
    def search_country(self, code, offset, limit=50):
        self.country_page=(code,offset,limit); return [STATION] * 50
    def search_country_page(self, code, offset, limit=50):
        stations = self.search_country(code, offset, limit)
        return stations, len(stations) == limit
    def geographic(self, code, offset, limit=50): return [], False


def directory(monkeypatch, runtime):
    result=Directory(); monkeypatch.setattr(runtime,'RadioBrowser',lambda cancel=None: result); return result


def test_home_hides_test_streams_and_uses_localized_labels(kodi):
    runtime,state,_=kodi
    route(runtime)
    assert [item.label for _,item,_ in state.items] == ['localized-30111','localized-30003','localized-30002','localized-30001','localized-30112','localized-30004','localized-30050']
    assert all('tests' not in url for url,_,_ in state.items)
    assert state.ended == [{'succeeded': True, 'cacheToDisc': False}]


def test_settings_action_does_not_replace_home_listing_with_empty_folder(kodi):
    runtime, state, _ = kodi
    route(runtime, action='settings')
    assert state.settings_opened
    assert state.ended == [{'succeeded': False, 'cacheToDisc': False}]


def test_controlled_ad_sources_appear_in_test_menu(kodi):
    runtime, state, _ = kodi
    state.settings.update({'test_mode': True, 'test_origin': 'http://127.0.0.1:58416'})
    route(runtime, action='tests')
    stations = [runtime._fixture_station(parse_qs(urlsplit(url).query)['id'][0])
                for url, _, _ in state.items]
    paths = {urlsplit(station.url).path for station in stations}
    assert {'/source/normal/index.m3u8', '/source/mutation/index.m3u8',
            '/source/expired-window/index.m3u8', '/source/stall/index.m3u8'} <= paths
    assert all(station.hls and station.codec == 'AAC' for station in stations
               if '/source/' in station.url)
    translations = (ADDON / 'resources/language/resource.language.en_gb/strings.po').read_text()
    import re
    ids = re.findall(r'^msgctxt "#(\d+)"$', translations, re.MULTILINE)
    assert len(ids) == len(set(ids))


def test_icy_marker_scenarios_are_selectable_with_localized_labels_and_exact_origin(kodi):
    runtime, state, _ = kodi
    state.settings.update({'test_mode': True, 'test_origin': 'http://127.0.0.1:58416'})
    route(runtime, action='tests')
    expected = {
        'explicit': 30061, 'tildeats': 30062, 'wrapped': 30063,
        'return-to-track': 30064, 'paired-marker': 30065,
        'punctuation': 30066, 'unresolved': 30067,
    }
    stations = []
    for url, item, _ in state.items:
        params = parse_qs(urlsplit(url).query)
        station = runtime._fixture_station(params['id'][0])
        if '/icy/' in station.url:
            stations.append((item.label, json.loads(station_payload(station))))
    assert [(label, urlsplit(station['url']).path.rsplit('/', 1)[-1])
            for label, station in stations] == [
        ('localized-%d' % label, scenario + '.mp3')
        for scenario, label in expected.items()]
    assert all(urlsplit(station['url']).scheme == 'http'
               and urlsplit(station['url']).netloc == '127.0.0.1:58416'
               and station['codec'] == 'MP3' and station['hls'] is False
               and station['uuid'].startswith('fixture-')
               for _, station in stations)
    translations = (ADDON / 'resources/language/resource.language.en_gb/strings.po').read_text()
    ids = {int(value) for value in re.findall(r'^msgctxt "#(\d+)"$', translations, re.MULTILINE)}
    assert set(expected.values()) <= ids
    state.items.clear()
    state.settings['test_origin'] = 'http://127.0.0.2:58416'
    route(runtime, action='tests')
    assert not any('station=' in url for url, _, _ in state.items)


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


def test_root_and_station_context_open_same_shared_map_without_clicks(kodi, monkeypatch):
    runtime, state, _ = kodi
    browser = directory(monkeypatch, runtime)
    browser.geographic = lambda code, offset, limit: ([STATION], False)
    opened = install_map_window(monkeypatch)
    route(runtime, mode='map')
    assert opened[-1].kwargs['selected_uuid'] == '' and opened[-1].modal
    source = 'plugin://plugin.audio.kodi_donor_radio?mode=tag&tag=jazz'
    route(runtime, mode='map', uuid=STATION.uuid, origin=source)
    assert opened[-1].kwargs['selected_uuid'] == STATION.uuid and opened[-1].modal
    assert opened[-1].kwargs['stations'] == [STATION]
    assert opened[-1].kwargs['origin_route'] == source
    assert browser.clicks == []


def test_map_scan_is_bounded_and_keeps_a_selected_station(kodi):
    runtime, _, _ = kodi
    requests = []
    class Browser:
        def geographic(self, code, offset, limit):
            requests.append((code, offset, limit))
            return [STATION] * limit, True
    result = runtime._map_stations(Browser(), 'CA', None, threading.Event())
    assert len(result) == 200
    assert requests == [('CA', 0, 50), ('CA', 50, 50), ('CA', 100, 50), ('CA', 150, 50)]
    selected = Station('selected', 'Selected', 'https://radio.example/live', 'MP3', False)
    result = runtime._map_stations(type('Browser', (), {'geographic': lambda *args: ([], False)})(),
                                   '', selected, threading.Event())
    assert result == [selected]


def test_map_scan_reserves_one_of_200_slots_for_absent_context_station(kodi):
    runtime, _, _ = kodi
    class Browser:
        def geographic(self, code, offset, limit):
            page = [Station('scan-%03d' % index, 'Scanned %03d' % index,
                            'https://radio.example/live', 'MP3', False)
                    for index in range(offset, offset + limit)]
            return page, True

    selected = Station('context-selected', 'Context selected',
                       'https://radio.example/selected', 'MP3', False)
    result = runtime._map_stations(Browser(), 'CA', selected, threading.Event())
    assert len(result) == 200
    assert result[0] is selected
    assert selected in result
    assert result[-1].uuid == 'scan-198'
    assert all(station.uuid != 'scan-199' for station in result)


def test_map_view_centers_station_and_handles_antimeridian(kodi):
    runtime, _, _ = kodi
    from radio_map import project
    station = Station('geo', 'Geo station', 'https://radio.example/live', 'MP3', False,
                      latitude=0, longitude=179.8)
    view = runtime._map_view(station=station)
    assert view.west > view.east
    assert project(0, 179.8, view) == (500, 325)


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
        def create(self,station): created.append(station); return {'id':'c'*48,'playback_url':'http://127.0.0.1:32123/media/'+'c'*48+'/'+'d'*48,'mime_type':'audio/aac'}
    monkeypatch.setattr(runtime,'ControlClient',Client)
    route(runtime,action='play',uuid=STATION.uuid)
    assert created==[STATION] and browser.clicks==['station-1']
    assert len(state.resolved)==1 and state.resolved[0][0]
    assert click_states == [state.resolved]
    assert stopped == []
    assert 'localized-30011' not in state.messages
    assert state.resolved[0][1].path=='http://127.0.0.1:32123/media/'+'c'*48+'/'+'d'*48
    # Directory codec/HLS fields are hints; the relay's response is authoritative.
    assert state.resolved[0][1].mime == 'audio/aac'
    # Kodi's redirect lookup opens a GET before the decoder, consuming the relay.
    assert state.resolved[0][1].lookup is False


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
    route(runtime,action='play',uuid=STATION.uuid)
    assert state.resolved and not state.resolved[0][0] and not state.resolved[0][1].path
    assert state.messages[-1] in ('localized-30010','localized-30011')
    assert not state.logs


def test_directory_error_offers_localized_retry(kodi,monkeypatch):
    runtime,state,_=kodi; browser=directory(monkeypatch,runtime)
    browser.country_entries=lambda: (_ for _ in ()).throw(OSError('sensitive'))
    route(runtime,action='countries')
    assert state.messages[-1]=='localized-30012'
    assert state.items[-1][1].label=='localized-30006'
    assert 'mode=countries' in state.items[-1][0]
    assert state.ended == [{'succeeded': True, 'cacheToDisc': False}]


def test_startup_wait_is_abortable(kodi,monkeypatch):
    runtime,state,_=kodi; directory(monkeypatch,runtime); state.abort=True
    before=time.monotonic(); route(runtime,action='play',uuid=STATION.uuid)
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
            return {'id':'c'*48,'playback_url':'http://127.0.0.1:32123/media/'+'c'*48+'/'+'d'*48,'mime_type':'audio/aac'}
        def stop(self,session_id): assert session_id=='c'*48; stopped.set()
    monkeypatch.setattr(runtime,'ControlClient',Client)
    state.action=lambda monitor: setattr(state,'cancel',True) if entered.is_set() else None
    route(runtime,action='play',uuid=STATION.uuid)
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
    assert len(state.items)==19
    assert all(item.label.startswith('localized-') for _,item,_ in state.items)
    station=json.loads(station_payload(runtime._fixture_station(parse_qs(urlsplit(state.items[0][0]).query)['id'][0])))
    assert station['url']=='http://127.0.0.1:8765/continuous.mp3'
    paths=[runtime._fixture_station(parse_qs(urlsplit(url).query)['id'][0]).url for url,_,_ in state.items]
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


@pytest.mark.parametrize('setting_id,default', [
    ('guided_setup', 'false'),
    ('offer_setup_row', 'true'),
    ('preview_on_browse', 'true'),
])
def test_radio_interface_settings_are_visible_boolean_toggles(setting_id, default):
    settings = ET.parse(ADDON / 'resources/settings.xml')
    setting = settings.find(".//setting[@id='%s']" % setting_id)
    assert setting is not None, 'Missing radio setting: ' + setting_id
    assert setting.get('type') == 'boolean'
    assert setting.findtext('default') == default
    assert setting.findtext('level') == '0'
    assert setting.find('control').get('type') == 'toggle'
    assert setting.find('visible') is None
    assert setting.find('enable') is None
    assert setting.find('dependencies') is None


def test_radio_settings_preserve_fixture_controls_and_defer_theme_selectors():
    settings = ET.parse(ADDON / 'resources/settings.xml')
    controls = {node.get('id'): node for node in settings.findall('.//setting')}
    expected = {
        'diagnostics': ('boolean', 'false', 'toggle'),
        'test_mode': ('boolean', 'false', 'toggle'),
        'test_origin': ('string', 'http://127.0.0.1:8765', 'edit'),
        'ad_enabled': ('boolean', 'false', 'toggle'),
        'ad_preroll': ('boolean', 'false', 'toggle'),
        'ad_midroll': ('boolean', 'false', 'toggle'),
        'ad_vast_url': ('string', '', 'edit'),
    }
    assert set(controls) == set(expected) | {
        'guided_setup', 'offer_setup_row', 'preview_on_browse', 'run_setup'}
    for setting_id, (kind, default, control) in expected.items():
        setting = controls[setting_id]
        assert setting.get('type') == kind
        assert setting.findtext('default') == default
        assert setting.find('control').get('type') == control
    assert controls['ad_vast_url'].findtext('constraints/allowempty') == 'true'
    assert not settings.findall('.//options')  # No interface/theme enums yet.


def test_radio_settings_labels_and_help_have_unique_nonempty_translations():
    settings = ET.parse(ADDON / 'resources/settings.xml')
    strings = (ADDON / 'resources/language/resource.language.en_gb/strings.po').read_text()
    entries = re.findall(r'^msgctxt "#(\d+)"\nmsgid (".*")(?:\n|$)', strings, re.MULTILINE)
    ids = [int(context) for context, _ in entries]
    assert len(ids) == len(set(ids)), 'Duplicate localization IDs'
    translations = {int(context): json.loads(message) for context, message in entries}
    references = {int(node.get(attribute)) for node in settings.iter()
                  for attribute in ('label', 'help')
                  if node.get(attribute, '').isdigit()}
    assert references <= translations.keys()
    assert all(translations[context].strip() for context in references)
    assert set(range(30100, 30107)) <= translations.keys()


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


def test_ad_settings_default_off_and_require_exact_fixture_origin(kodi):
    runtime, state, _ = kodi
    settings = ET.parse(ADDON / 'resources/settings.xml')
    assert settings.find(".//setting[@id='ad_vast_url']/constraints/allowempty").text == 'true'
    assert runtime.ad_settings().enabled is False
    state.settings.update(test_mode=True, test_origin='http://127.0.0.1:8765',
                          ad_enabled=True, ad_preroll=True,
                          ad_vast_url='http://127.0.0.1:8765/vast.xml')
    config = runtime.ad_settings()
    assert config.enabled and config.preroll and not config.midroll
    assert config.fixture_origin == 'http://127.0.0.1:8765'
    for url in ('http://127.0.0.1:8766/vast.xml', 'http://127.0.0.1:8765/',
                'http://127.0.0.1:8765/vast.xml#fragment', 'garbage'):
        state.settings['ad_vast_url'] = url
        with pytest.raises(ValueError): runtime.ad_settings()
    state.settings['test_mode'] = False
    assert runtime.ad_settings().enabled is False


def test_ad_only_setting_change_updates_next_session_without_service_restart(kodi):
    runtime, state, _ = kodi
    state.settings.update(test_mode=True, test_origin='http://127.0.0.1:8765')
    monitor = runtime.ServiceMonitor()
    state.settings.update(ad_enabled=True, ad_preroll=True,
                          ad_vast_url='http://127.0.0.1:8765/vast.xml')
    monitor.onSettingsChanged()
    assert monitor.ad_changed and not monitor.settings_changed
    state.settings['diagnostics'] = True
    monitor.onSettingsChanged()
    assert monitor.settings_changed
    monitor.settings_changed = False  # The service consumed the restart request.
    monitor.ad_changed = False
    state.settings['ad_midroll'] = True
    monitor.onSettingsChanged()
    assert monitor.ad_changed and not monitor.settings_changed


def test_ad_create_uses_bounded_extended_control_timeout(kodi, monkeypatch):
    runtime, state, profile = kodi
    state.settings.update(test_mode=True, test_origin='http://127.0.0.1:8765',
                          ad_enabled=True, ad_preroll=True,
                          ad_vast_url='http://127.0.0.1:8765/vast.xml')
    descriptor = ServiceDescriptor(32123, 'a'*48, 'b'*64)
    runtime.publish_descriptor(profile, descriptor)
    timeouts = []
    class Client:
        def __init__(self, descriptor, timeout=10):
            self.descriptor = descriptor
            timeouts.append(timeout)
        def health(self): return {'status': 'ok', 'instance_id': descriptor.instance_id}
        def create(self, station):
            return {'id':'c'*48, 'playback_url':'http://127.0.0.1:32123/media/'+'c'*48+'/'+'d'*48,
                    'mime_type':'audio/aac'}
        def stop(self, session_id): pass
    monkeypatch.setattr(runtime, 'ControlClient', Client)
    monkeypatch.setattr(runtime.RadioBrowser, 'click', lambda *args: None)
    route(runtime, mode='play_fixture', id='fixture-30023')
    assert timeouts[-1] == 30
    assert state.resolved[0][0]


def test_invalid_ad_url_keeps_extended_fixture_create_timeout(kodi, monkeypatch):
    runtime, state, profile = kodi
    state.settings.update(test_mode=True, test_origin='http://127.0.0.1:8765',
                          ad_enabled=True, ad_preroll=True, ad_vast_url='invalid')
    descriptor = ServiceDescriptor(32123, 'a'*48, 'b'*64)
    runtime.publish_descriptor(profile, descriptor)
    timeouts = []

    class Client:
        def __init__(self, descriptor, timeout=10):
            self.descriptor = descriptor
            timeouts.append(timeout)
        def health(self): return {'status': 'ok', 'instance_id': descriptor.instance_id}
        def create(self, station):
            return {'id':'c'*48, 'playback_url':'http://127.0.0.1:32123/media/'+'c'*48+'/'+'d'*48,
                    'mime_type':'audio/aac'}
        def stop(self, session_id): pass

    monkeypatch.setattr(runtime, 'ControlClient', Client)
    route(runtime, mode='play_fixture', id='fixture-30023')
    assert timeouts[-1] == 30
    assert state.resolved[0][0]


def test_ad_change_keeps_service_and_active_config_snapshot(kodi):
    runtime, state, profile = kodi
    state.settings.update(test_mode=True, test_origin='http://127.0.0.1:8765')
    seen = []
    def action(monitor):
        descriptor = runtime.read_descriptor(profile)
        if not seen:
            seen.append((descriptor, monitor.settings_changed))
            state.settings.update(ad_enabled=True, ad_preroll=True,
                                  ad_vast_url='http://127.0.0.1:8765/vast.xml')
            monitor.onSettingsChanged()
        else:
            seen.append((descriptor, monitor.settings_changed))
            state.abort = True
    state.action = action
    runtime.run_service()
    assert len(seen) == 2
    assert seen[0][0] == seen[1][0]
    assert not seen[1][1]


def test_startup_removes_only_abandoned_ad_cache_directories(kodi):
    runtime, state, profile = kodi
    namespace = profile / 'controlled-ad-creative-cache'
    abandoned = namespace / 'old-session'
    abandoned.mkdir(parents=True)
    (abandoned / 'media.ts').write_bytes(b'old')
    neighbor = profile / 'unrelated'
    neighbor.mkdir()
    state.action = lambda monitor: setattr(state, 'abort', True)
    runtime.run_service()
    assert not abandoned.exists()
    assert neighbor.exists()


def test_invalid_ad_only_edit_does_not_restart_running_service(kodi):
    runtime, state, profile = kodi
    state.settings.update(test_mode=True, test_origin='http://127.0.0.1:8765')
    descriptors = []
    def action(monitor):
        descriptors.append(runtime.read_descriptor(profile))
        if len(descriptors) == 1:
            state.settings.update(ad_enabled=True, ad_preroll=True, ad_vast_url='invalid')
            monitor.onSettingsChanged()
        else:
            state.abort = True
    state.action = action
    runtime.run_service()
    assert len(descriptors) == 2
    assert descriptors[0] == descriptors[1]
    assert not state.notifications


def test_startup_recovers_after_invalid_ad_setting_is_corrected(kodi):
    runtime, state, profile = kodi
    state.settings.update(test_mode=True, test_origin='http://127.0.0.1:8765',
                          ad_enabled=True, ad_preroll=True, ad_vast_url='invalid')
    descriptors = []
    waits = []

    def action(monitor):
        waits.append(True)
        if len(waits) == 1:
            state.settings['ad_vast_url'] = 'http://127.0.0.1:8765/vast.xml'
            monitor.onSettingsChanged()
        else:
            descriptors.append(runtime.read_descriptor(profile))
            state.abort = True

    state.action = action
    runtime.run_service()
    assert len(descriptors) == 1
    assert descriptors[0] is not None
    assert not (profile / 'service.json').exists()
    assert state.notifications == [('localized-30000', 'localized-30068', 'error', 5000)]


def test_player_refuses_ad_sample_when_file_has_switched_before_callback(kodi):
    runtime, _, _ = kodi
    current = SimpleNamespace(id='a'*48, cancel=threading.Event(), ad_enabled=False)
    manager = SimpleNamespace(get=lambda identity: current if identity == current.id else None,
                              stop=lambda identity: None)
    player = runtime.RelayPlayer(manager, 32123)
    player.owned = current.id
    player.file = 'http://127.0.0.1:32123/media/' + 'c'*48 + '/' + 'd'*48
    sampled = []
    player.ads.sample = lambda: sampled.append(True)
    player.sample_ads()
    assert sampled == []


def test_old_native_callbacks_during_new_stream_load_cannot_change_or_retire_it(kodi):
    runtime, state, _ = kodi
    old_id, new_id = 'a'*48, 'c'*48
    stopped = []
    manager = SimpleNamespace(stop=stopped.append, get=lambda identity: None)
    player = runtime.RelayPlayer(manager, 32123)
    states = []
    player.ads.on_pause = lambda identity: states.append(('pause', identity))
    player.ads.on_resume = lambda identity: states.append(('resume', identity))
    player.ads.on_seek = lambda identity: states.append(('seek', identity))
    player.ads.on_end = lambda reason, identity: states.append(('end', identity))
    player.file = 'http://127.0.0.1:32123/media/' + old_id + '/' + 'b'*48
    player.playing = True
    player.onAVStarted()
    player.file = 'http://127.0.0.1:32123/media/' + new_id + '/' + 'd'*48
    player.playing = False
    player.onAVStarted()
    assert stopped == [old_id]
    states.clear()
    player.onPlayBackPaused()
    player.onPlayBackResumed()
    player.onPlayBackSeek(3000, 1000)
    player.onPlayBackSeekChapter(2)
    player.onPlayBackStopped()
    player.onPlayBackEnded()
    player.onPlayBackError()
    assert states == []
    assert stopped == [old_id]
    assert player.owned == new_id
    player.playing = True
    player.sample_ads()
    state.player_paused = True
    player.onPlayBackPaused()
    assert states == [('pause', new_id)]


def test_delayed_native_callbacks_after_arm_require_current_file_and_state(kodi):
    runtime, state, _ = kodi
    old_id, new_id = 'a'*48, 'c'*48
    stopped = []
    player = runtime.RelayPlayer(SimpleNamespace(stop=stopped.append, get=lambda identity: None), 32123)
    calls = []
    player.ads.on_pause = lambda identity: calls.append(('pause', identity))
    player.ads.on_resume = lambda identity: calls.append(('resume', identity))
    player.ads.on_seek = lambda identity: calls.append(('seek', identity))
    player.file = 'http://127.0.0.1:32123/media/' + old_id + '/' + 'b'*48
    player.playing = True
    player.onAVStarted()
    player.file = 'http://127.0.0.1:32123/media/' + new_id + '/' + 'd'*48
    player.onAVStarted()
    player.sample_ads()
    calls.clear()
    player.onPlayBackPaused()  # old pause; current player is not paused
    state.player_paused = True
    player.onPlayBackResumed()  # old resume; current player remains paused
    assert calls == []
    player.playing = False
    state.player_caching = True
    player.onPlayBackStopped()  # old stop while the new player buffers
    assert player.owned == new_id
    state.player_caching = False
    player.file = 'http://127.0.0.1:32123/media/' + old_id + '/' + 'b'*48
    player.onPlayBackSeek(1000, 1000)
    player.onPlayBackStopped()
    assert calls == [] and stopped == [old_id]
    assert player.owned == new_id


def test_shutdown_cancels_pending_demand_without_resuming_old_stream(kodi):
    runtime, _, _ = kodi
    entered, released = threading.Event(), threading.Event()
    def resolve(url, client, cancel):
        entered.set()
        cancel.wait(1)
        released.set()
        raise OSError('cancelled demand')
    manager = SessionManager(resolver=resolve)
    player = runtime.RelayPlayer(manager, 32123)
    outcomes = []
    def demand():
        try:
            manager.create(STATION)
        except OSError:
            outcomes.append('cancelled')
    worker = threading.Thread(target=demand)
    worker.start()
    assert entered.wait(1)
    player.ads.close()
    manager.close()
    assert released.wait(1)
    worker.join(1)
    assert outcomes == ['cancelled']
    assert manager.get('a'*48) is None
    assert player.owned is None


def test_service_loads_station_labels_from_its_private_profile(kodi, monkeypatch):
    from icy_marker_store import IcyMarkerStore
    from station_resolver import ResolvedSource
    runtime, state, profile = kodi
    IcyMarkerStore(profile).learn('station-1', '-', 'end')
    real_manager = SessionManager
    def manager(**kwargs):
        result = real_manager(**kwargs)
        result.resolver = lambda *args: ResolvedSource('https://radio.example/live', 'hls', None, b'')
        return result
    monkeypatch.setattr(runtime, 'SessionManager', manager)
    def action(monitor):
        client = ControlClient(runtime.read_descriptor(profile))
        result = client.create(STATION)
        # Access the actual service through its player, keeping relay and store real.
        session = state.players[-1].manager.get(result['id'])
        session.observe_icy_metadata({'StreamTitle': '-'}, '2026-10-23T00:00:00Z', time.monotonic())
        assert session.icy_tracker.learned_lookup('-') == 'end'
        state.abort = True
    state.action = action
    runtime.run_service()


@pytest.fixture
def icy_label_service(kodi):
    """Exercise UI writes through the real authenticated control and station store."""
    from icy_marker_store import IcyMarkerStore
    from station_resolver import ResolvedSource
    runtime, state, profile = kodi
    events = []
    manager = SessionManager(
        resolver=lambda *args: ResolvedSource(STATION.url, 'icy', None, b''),
        sink=SimpleNamespace(emit=events.append), icy_marker_store=IcyMarkerStore(profile))
    server = RelayServer(manager)
    runtime.publish_descriptor(profile, server.start())
    session = manager.create(STATION)
    def observe(marker):
        session.observe_icy_metadata({'StreamTitle': marker}, '2026-10-23T00:00:00Z', time.monotonic())
    try:
        yield SimpleNamespace(manager=manager, session=session, events=events, observe=observe,
                              store=manager.icy_marker_store, profile=profile)
    finally:
        server.close()


def test_icy_label_home_action_remains_reachable_during_audio(kodi):
    runtime, state, _ = kodi
    player = runtime.xbmc.Player()
    player.playing = True
    player.file = STATION.url
    route(runtime)
    action = [entry for entry in state.items
              if parse_qs(urlsplit(entry[0]).query).get('mode') == ['label_icy_marker']]
    assert len(action) == 1
    assert action[0][1].label == 'localized-30050'
    assert 'IsPlayable' not in action[0][1].props
    assert player.playing and player.file == STATION.url
    assert not state.resolved


@pytest.mark.parametrize('selection,action', [(0, 'start'), (1, 'end')])
@pytest.mark.parametrize('remember', [False, True])
def test_icy_label_displays_exact_candidate_applies_choice_and_optional_memory(
        kodi, icy_label_service, selection, action, remember):
    runtime, state, _ = kodi
    service = icy_label_service
    service.observe('#')
    state.selection, state.remember = selection, remember
    player = runtime.xbmc.Player()
    player.playing, player.file = True, STATION.url
    route(runtime, action='label_icy_marker')
    assert len(state.selections) == 2
    heading, options = state.selections[0]
    assert '#' in heading and 'localized-30051' in heading
    assert options == ['localized-30052', 'localized-30053', 'localized-30054']
    assert len(state.confirmations) == 1
    assert state.confirmations[0] == ('localized-30055',
                                      ['localized-30059', 'localized-30060', 'localized-30054'])
    labeled = [event for event in service.events if event.kind == 'icy_marker_labeled']
    assert len(labeled) == 1 and labeled[0].details['reason'] == action
    assert labeled[0].station_uuid == 'station-1'
    assert labeled[0].session_id == service.session.id
    assert service.store.lookup('station-1', '#') == (action if remember else None)
    assert (service.profile / 'icy-markers.json').exists() is remember
    assert service.store.lookup('another-station', '#') is None
    assert state.messages[-1] == 'localized-30058'
    assert service.manager.get(service.session.id) is service.session
    assert player.playing and player.file == STATION.url and not state.resolved
    assert state.ended == [{'succeeded': False, 'cacheToDisc': False}]


@pytest.mark.parametrize('selection', [-1, 2])
def test_icy_label_cancel_or_dismiss_does_not_apply_or_remember(kodi, icy_label_service, selection):
    runtime, state, _ = kodi
    service = icy_label_service
    service.observe('-')
    state.selection, state.remember = selection, True
    route(runtime, action='label_icy_marker')
    assert state.selections and not state.confirmations
    assert not [event for event in service.events if event.kind == 'icy_marker_labeled']
    assert not (service.profile / 'icy-markers.json').exists()
    assert 'localized-30058' not in state.messages


def test_icy_label_without_candidate_reports_noop(kodi, icy_label_service):
    runtime, state, _ = kodi
    state.selection, state.remember = 0, True
    route(runtime, action='label_icy_marker')
    assert state.messages[-1] == 'localized-30056'
    assert not state.selections and not state.confirmations
    assert not (icy_label_service.profile / 'icy-markers.json').exists()
    assert not [event for event in icy_label_service.events if event.kind == 'icy_marker_labeled']


@pytest.mark.parametrize('change', ['candidate', 'session', 'stop'])
def test_icy_label_rejects_candidate_or_session_changed_during_dialog(kodi, icy_label_service, change):
    runtime, state, _ = kodi
    service = icy_label_service
    service.observe('-')
    state.selection, state.remember = 0, True
    def change_candidate():
        if change == 'candidate':
            service.observe('#')
        elif change == 'session':
            replacement = service.manager.create(Station('station-2', 'Second', STATION.url, 'MP3', False))
            replacement.observe_icy_metadata({'StreamTitle': '-'}, '2026-10-23T00:00:00Z', time.monotonic())
        else:
            service.manager.stop(service.session.id)
    state.on_confirm = change_candidate
    route(runtime, action='label_icy_marker')
    assert state.messages[-1] == 'localized-30057'
    assert 'localized-30058' not in state.messages
    assert not [event for event in service.events if event.kind == 'icy_marker_labeled']
    assert not (service.profile / 'icy-markers.json').exists()
    assert not state.resolved


@pytest.mark.parametrize('memory_selection', [-1, 2])
def test_icy_label_dismissing_or_canceling_memory_step_does_not_apply(kodi, icy_label_service, memory_selection):
    runtime, state, _ = kodi
    service = icy_label_service
    service.observe('-')
    state.selection = 0
    state.memory_selection = memory_selection
    # The old Yes/No prompt maps dismissal to False, reproducing its transient write.
    state.remember = False
    events_before = list(service.events)
    route(runtime, action='label_icy_marker')
    assert service.events == events_before
    assert not [event for event in service.events if event.kind == 'icy_marker_labeled']
    assert not (service.profile / 'icy-markers.json').exists()
    assert service.store.lookup('station-1', '-') is None
    assert 'localized-30058' not in state.messages
    assert service.manager.get(service.session.id) is service.session


def test_legacy_station_listing_emits_uuid_only_play_route(kodi, monkeypatch):
    runtime, state, _ = kodi
    directory(monkeypatch, runtime)
    route(runtime, action='stations')
    assert parse_qs(urlsplit(state.items[0][0]).query) == {'mode': ['play'], 'uuid': ['station-1']}


def test_legacy_station_decoder_rejects_extended_metadata(kodi):
    runtime, _, _ = kodi
    payload = {'uuid': 'station-1', 'name': 'Station One', 'url': 'https://radio.example/live',
               'codec': 'MP3', 'hls': False, 'country': 'France'}
    with pytest.raises(ValueError):
        runtime._station(json.dumps(payload))


def test_root_order_places_and_action_flags_without_network(kodi, monkeypatch):
    from radio_ui import PlaceStore, FavoriteStore, StationSummary
    runtime, state, profile = kodi
    browser = directory(monkeypatch, runtime)
    first = PlaceStore(profile).save('Home', 0, 0, 25)
    second = PlaceStore(profile).save('Work', 1, 1, 25)
    FavoriteStore(profile).add(StationSummary('saved', 'Offline Radio'))
    state.settings['offer_setup_row'] = True
    route(runtime)
    assert [i.label for _, i, _ in state.items] == [
        'localized-30110', 'Home', 'Work', 'localized-30111', 'localized-30003',
        'localized-30002', 'localized-30001', 'localized-30112', 'localized-30004', 'localized-30050']
    assert [folder for _, _, folder in state.items] == [False, True, True, True, True, True, True, False, False, False]
    assert parse_qs(urlsplit(state.items[1][0]).query) == {'mode': ['place'], 'id': [first.place_id]}
    assert parse_qs(urlsplit(state.items[2][0]).query)['id'] == [second.place_id]
    assert browser.clicks == browser.lookups == browser.searches == []
    assert all('IsPlayable' not in i.props for _, i, _ in state.items)


def test_station_display_metadata_safe_artwork_and_uuid_links(kodi, monkeypatch):
    runtime, state, _ = kodi
    browser = directory(monkeypatch, runtime)
    browser.search = lambda *a: [Station('a', 'FM 88', 'https://secret.example/token', 'AAC', False,
        city='San Mateo', bitrate=64, tags='jazz,news', favicon='https://art.example/a.png'),
        Station('b', 'Other', 'https://secret.example/other', '', False, favicon='file:///secret')]
    route(runtime, mode='tag', tag='jazz')
    assert len(state.items) == 2
    link, item, folder = state.items[0]
    assert parse_qs(urlsplit(link).query) == {'mode': ['play'], 'uuid': ['a']}
    assert item.label == 'FM 88' and item.label2 == 'San Mateo · AAC 64'
    assert item.art == {'thumb': 'https://art.example/a.png', 'icon': 'https://art.example/a.png'}
    assert item.info['music']['genre'] == 'jazz,news'
    assert item.info['music']['title'] == 'FM 88'
    assert item.props['IsPlayable'] == 'true' and folder is False and item.folder is False
    fallback = state.items[1][1].art['thumb']
    assert fallback.endswith('/resources/station-monogram.png') and Path(fallback).is_file()
    assert all('secret' not in url for url, _, _ in state.items)
    assert browser.clicks == browser.lookups == []


def test_fixture_routes_are_fixed_identifiers_and_require_enabled_exact_origin(kodi, monkeypatch):
    runtime, state, _ = kodi
    browser = directory(monkeypatch, runtime)
    state.settings.update(test_mode=True, test_origin='http://127.0.0.1:8765')
    route(runtime, mode='tests')
    assert len(state.items) == 19
    for link, item, folder in state.items:
        params = parse_qs(urlsplit(link).query)
        assert set(params) == {'mode', 'id'} and params['mode'] == ['play_fixture']
        assert not folder and item.props['IsPlayable'] == 'true'
        assert '127.0.0.1' not in link
    route(runtime, mode='play_fixture', id='not-known')
    assert state.resolved[-1][0] is False
    state.settings['test_mode'] = False
    route(runtime, mode='play_fixture', id='fixture-30020')
    assert state.resolved[-1][0] is False and browser.clicks == []


@pytest.mark.parametrize('uuid', ['', '../bad', 'a'*129, 'a,b', 'fixture-30020'])
def test_invalid_uuid_selection_never_resolves_or_counts(kodi, monkeypatch, uuid):
    runtime, state, _ = kodi
    browser = directory(monkeypatch, runtime)
    route(runtime, mode='play', uuid=uuid)
    assert state.resolved == [(False, state.resolved[0][1])]
    assert browser.lookups == browser.clicks == []


def test_uuid_play_resolves_transient_station_before_existing_relay(kodi, monkeypatch):
    runtime, state, _ = kodi
    browser = directory(monkeypatch, runtime)
    created=[]
    client = SimpleNamespace(descriptor=ServiceDescriptor(32123, 'a'*48, 'b'*64),
        create=lambda station: created.append(station) or {'id':'c'*48,
            'playback_url':'http://127.0.0.1:32123/media/'+'c'*48+'/'+'d'*48, 'mime_type':'audio/mpeg'})
    monkeypatch.setattr(runtime, '_service', lambda *a, **kw: client)
    route(runtime, mode='play', uuid='station-1', station=station_payload(Station('evil', 'Evil', 'https://evil', '', False)))
    assert browser.lookups == ['station-1'] and created == [STATION]
    assert browser.clicks == ['station-1'] and state.resolved[0][0]


def test_favorites_render_cached_order_without_directory_lookup(kodi, monkeypatch):
    from radio_ui import FavoriteStore, StationSummary
    runtime, state, profile = kodi
    browser = directory(monkeypatch, runtime)
    FavoriteStore(profile).add(StationSummary('z', 'Cached Z', city='Paris', codec='MP3', bitrate=128))
    FavoriteStore(profile).add(StationSummary('a', 'Cached A'))
    browser.by_uuid = lambda *a: (_ for _ in ()).throw(OSError('offline'))
    route(runtime, mode='favorites')
    assert [i.label for _,i,_ in state.items] == ['Cached Z', 'Cached A']
    assert state.items[0][1].label2 == 'Paris · MP3 128'
    assert browser.clicks == browser.searches == []


@pytest.mark.parametrize('mode', ['favorites', 'tag', 'countries', 'tags'])
def test_empty_listings_have_localized_message(kodi, monkeypatch, mode):
    runtime, state, _ = kodi
    browser = directory(monkeypatch, runtime)
    browser.search = lambda *a: []
    browser.tags = lambda *a: []
    browser.country_entries = lambda: []
    route(runtime, mode=mode, tag='jazz')
    assert 'localized-30113' in state.messages
    assert state.items == [] and state.ended[-1]['succeeded']


@pytest.mark.parametrize('mode', ['setup', 'map'])
def test_future_ui_routes_return_to_listing_without_playback(kodi, monkeypatch, mode):
    runtime, state, _ = kodi
    browser = directory(monkeypatch, runtime)
    if mode == 'map': install_map_window(monkeypatch)
    route(runtime, mode=mode, uuid='station-1')
    assert state.ended == [{'succeeded': False, 'cacheToDisc': False}]
    assert state.items == state.resolved == browser.clicks == []
    assert browser.lookups == (['station-1'] if mode == 'map' else [])


def test_new_country_routes_use_iso_codes_and_page(kodi, monkeypatch):
    runtime, state, _ = kodi
    browser = directory(monkeypatch, runtime)
    browser.country_entries = lambda: [('Country %d'%i, 'US') for i in range(60)]
    route(runtime, mode='countries', offset=50)
    assert len(state.items) == 10
    assert parse_qs(urlsplit(state.items[0][0]).query) == {'mode':['country'], 'code':['US']}
    state.items.clear()
    route(runtime, mode='country', code='FR', offset=50)
    assert browser.country_page == ('FR', 50, 50)
    assert parse_qs(urlsplit(state.items[-1][0]).query) == {'mode':['country'], 'code':['FR'], 'offset':['100']}


@pytest.mark.parametrize('params', [dict(mode='tag',tag='a'*257),dict(mode='tag',tag='bad\n'),
    dict(mode='countries',offset=-1),dict(mode='countries',offset=10000001),
    dict(mode='country',code='France'),dict(mode='place',id='../bad')])
def test_invalid_routes_do_not_copy_untrusted_payload_to_retry(kodi, monkeypatch, params):
    runtime, state, _ = kodi
    browser = directory(monkeypatch, runtime)
    route(runtime, **params)
    assert 'localized-30012' in state.messages
    assert browser.searches == []
    assert all('station=' not in link and len(link)<1024 and '%0A' not in link for link,_,_ in state.items)


def test_place_local_radius_ranks_click_count_and_uses_saved_country(kodi, monkeypatch):
    from radio_ui import PlaceStore
    runtime, state, profile = kodi
    browser = directory(monkeypatch, runtime)
    place = PlaceStore(profile).save('Home', 0, 0, 25, country_code='FR')
    calls=[]
    stations = [Station(uuid, name, 'https://x.example/live', '', False, latitude=lat, longitude=0, click_count=clicks)
        for uuid,name,lat,clicks in [('z','Z',0,1),('b','A',0,5),('a','A',.1,5),('far','Far',40,99)]]
    browser.geographic = lambda code, offset, limit: calls.append((code,offset,limit)) or (stations, False)
    route(runtime, mode='place', id=place.place_id)
    assert [parse_qs(urlsplit(link).query)['uuid'][0] for link,_,_ in state.items] == ['a','b','z']
    assert calls == [('FR',0,50)] and browser.clicks == []


def test_place_scan_budget_and_continuation_do_not_claim_complete_empty(kodi, monkeypatch):
    from radio_ui import PlaceStore
    runtime, state, profile = kodi
    browser = directory(monkeypatch, runtime)
    place = PlaceStore(profile).save('Home', 0, 0, 25)
    calls=[]
    browser.geographic = lambda code, offset, limit: calls.append((code,offset,limit)) or ([], True)
    route(runtime, mode='place', id=place.place_id)
    assert calls == [('',0,50),('',50,50),('',100,50),('',150,50)]
    assert 'localized-30116' in state.messages and 'localized-30113' not in state.messages
    assert parse_qs(urlsplit(state.items[-1][0]).query) == {'mode':['place'], 'id':[place.place_id], 'offset':['200']}
    assert browser.clicks == []


def test_place_no_coordinates_reports_clear_message(kodi, monkeypatch):
    runtime, state, _ = kodi
    directory(monkeypatch, runtime)
    monkeypatch.setattr(runtime, 'PlaceStore', lambda profile: SimpleNamespace(list=lambda:[
        SimpleNamespace(place_id='old', latitude=None, longitude=None, country_code='')]))
    route(runtime, mode='place', id='old')
    assert 'localized-30114' in state.messages and state.items == []


def test_mode_precedes_legacy_action_and_cancellation_closes_listing(kodi, monkeypatch):
    from http_client import Cancelled
    runtime, state, _ = kodi
    browser = directory(monkeypatch,runtime)
    browser.tags=lambda *a: (_ for _ in ()).throw(Cancelled('cancelled'))
    route(runtime, mode='tags', action='settings')
    assert not getattr(state, 'settings_opened', False)
    assert state.items == [] and browser.clicks == []
    assert state.ended[-1] == {'succeeded':False,'cacheToDisc':False}
    assert 'localized-30013' in state.messages


@pytest.mark.parametrize('params', [dict(mode='tag', tag='jazz'), dict(mode='country', code='FR')])
def test_full_raw_page_with_malformed_station_retains_next_page(kodi, monkeypatch, params):
    from radio_browser import RadioBrowser
    runtime, state, _ = kodi
    browser = RadioBrowser()
    rows = [None] + [{'stationuuid': 'station-%d' % i, 'name': 'Radio %d' % i,
                      'url': 'https://radio.example/live'} for i in range(49)]
    monkeypatch.setattr(browser, '_json', lambda path, query: rows)
    monkeypatch.setattr(runtime, 'RadioBrowser', lambda cancel=None: browser)
    route(runtime, **params)
    assert len([entry for entry in state.items if not entry[2]]) == 49
    assert state.items[-1][1].label == 'localized-30005'
    continuation = parse_qs(urlsplit(state.items[-1][0]).query)
    assert continuation['offset'] == ['50'] and continuation['mode'] == [params['mode']]
    if params['mode'] == 'country':
        assert continuation['code'] == ['FR']
    else:
        assert continuation['tag'] == ['jazz']


def context_params(command):
    assert command.startswith('RunPlugin(plugin://plugin.audio.kodi_donor_radio?') and command.endswith(')')
    return {key: value[0] for key, value in parse_qs(urlsplit(command[10:-1]).query).items()}


def test_station_context_actions_are_uuid_only_and_favorite_state_is_current(kodi, monkeypatch):
    from radio_ui import FavoriteStore, StationSummary
    runtime, state, profile = kodi
    browser = directory(monkeypatch, runtime)
    browser.search = lambda *args: [STATION]
    route(runtime, mode='tag', tag='jazz')
    menus = state.items[0][1].context
    map_origin = 'plugin://plugin.audio.kodi_donor_radio?mode=tag&tag=jazz'
    assert [context_params(command) for _, command in menus] == [
        {'mode': 'play', 'uuid': 'station-1'}, {'mode': 'favorite_add', 'uuid': 'station-1'},
        {'mode': 'map', 'uuid': 'station-1', 'origin': map_origin},
        {'mode': 'station_info', 'uuid': 'station-1'}]
    assert all(STATION.url not in command for _, command in menus)
    assert [label for label, _ in menus] == ['localized-30120', 'localized-30121', 'localized-30123', 'localized-30124']
    FavoriteStore(profile).add(StationSummary.from_station(STATION))
    state.items.clear()
    route(runtime, mode='favorites')
    assert context_params(state.items[0][1].context[1][1]) == {'mode':'favorite_remove','uuid':'station-1'}
    assert state.items[0][1].context[1][0] == 'localized-30122'
    assert browser.clicks == browser.lookups == []


def test_favorite_context_routes_save_safe_metadata_keep_order_and_remove_offline(kodi, monkeypatch):
    from radio_ui import FavoriteStore, StationSummary
    runtime, state, profile = kodi
    browser = directory(monkeypatch, runtime)
    store = FavoriteStore(profile)
    store.add(StationSummary('other', 'Other'))
    route(runtime, mode='favorite_add', uuid='station-1')
    route(runtime, mode='favorite_add', uuid='station-1')
    assert [s.station_uuid for s in store.list()] == ['other', 'station-1']
    assert 'radio.example' not in store.path.read_text()
    browser.by_uuid = lambda uuid: (_ for _ in ()).throw(OSError('offline'))
    route(runtime, mode='favorite_remove', uuid='station-1')
    assert [s.station_uuid for s in store.list()] == ['other']
    assert state.builtins == ['Container.Refresh'] * 3
    assert all(not ended['succeeded'] for ended in state.ended)
    assert state.items == state.resolved == browser.clicks == []


def test_place_context_routes_validate_confirm_and_refresh(kodi):
    from radio_ui import PlaceStore
    runtime, state, profile = kodi
    store = PlaceStore(profile)
    first = store.save('First', 1, 2, 25, make_default=True)
    second = store.save('Second', 3, 4, 50)
    route(runtime)
    assert [context_params(command) for _, command in state.items[0][1].context] == [
        {'mode':'place_default','id':first.place_id}, {'mode':'place_radius','id':first.place_id},
        {'mode':'place_remove','id':first.place_id}]
    state.items.clear()
    route(runtime, mode='place_default', id=second.place_id)
    assert [p.is_default for p in store.list()] == [False, True]
    state.dialog_choices = [2]
    route(runtime, mode='place_radius', id=second.place_id)
    assert store.list()[1].radius_km == pytest.approx(96.56064)
    before = store.path.read_bytes()
    state.dialog_choices = [-1]
    route(runtime, mode='place_radius', id=second.place_id)
    route(runtime, mode='place_remove', id=second.place_id)
    assert store.path.read_bytes() == before
    assert len(state.builtins) == 2
    state.remember = True
    route(runtime, mode='place_remove', id=second.place_id)
    assert [(p.name, p.is_default) for p in store.list()] == [('First', True)]
    assert state.builtins == ['Container.Refresh'] * 3
    assert len(state.confirmations) == 2
    assert state.items == state.resolved == []


def setup_directory(monkeypatch, runtime):
    browser = directory(monkeypatch, runtime)
    browser.geo_calls = []
    browser.country_entries = lambda: [('France', 'FR'), ('Japan', 'JP')]
    def geographic(code, offset, limit):
        browser.geo_calls.append((code, offset, limit))
        return [Station('paris', 'Paris Radio', 'https://secret.example/stream', 'MP3', False,
                        city='Paris', country='France', country_code='FR', latitude=48.8, longitude=2.3),
                Station('tokyo', 'Tokyo Radio', 'https://secret.example/stream', 'MP3', False,
                        city='Tokyo', country='Japan', country_code='JP', latitude=35.7, longitude=139.7)], False
    browser.geographic = geographic
    return browser


@pytest.mark.parametrize('choices,inputs', [
    ([-1], []), ([0, -1], []), ([1], ['']), ([0, 0, -1], []),
    ([0, 0, 0, -1], []), ([0, 0, 0, 1, -1], []),
    ([0, 0, 0, 1, 0, -1], []),
])
def test_setup_cancel_at_every_dialog_has_no_writes_refresh_or_playback(kodi, monkeypatch, choices, inputs):
    from radio_ui import PlaceStore
    runtime, state, profile = kodi
    store = PlaceStore(profile)
    store.save('Existing', 1, 2, 25, make_default=True)
    before = store.path.read_bytes()
    browser = setup_directory(monkeypatch, runtime)
    state.dialog_choices, state.dialog_inputs = choices, inputs
    player = runtime.xbmc.Player(); player.file = 'already-playing'; player.playing = True
    route(runtime, mode='setup')
    assert state.dialog_choices == [] and state.dialog_inputs == []
    assert store.path.read_bytes() == before
    assert state.builtins == state.items == state.resolved == browser.clicks == []
    assert player.file == 'already-playing' and player.playing
    assert state.ended == [{'succeeded':False, 'cacheToDisc':False}]


def test_setup_saves_multiple_valid_city_candidates_once_then_refreshes(kodi, monkeypatch):
    import radio_ui
    runtime, state, profile = kodi
    browser = setup_directory(monkeypatch, runtime)
    state.dialog_choices = [0, 0, 0, 1, 0, 1, 0, 3, 1]
    state.dialog_inputs = ['Tokyo']
    calls = []
    real_replace = os.replace
    def replace(source, destination): calls.append(destination); real_replace(source, destination)
    monkeypatch.setattr(os, 'replace', replace)
    route(runtime, mode='setup')
    places = radio_ui.PlaceStore(profile).list()
    assert [(p.name, p.radius_km, p.country_code, p.is_default) for p in places] == [
        ('Paris', 40.2336, 'FR', True), ('Tokyo', 160.9344, 'JP', False)]
    assert len(calls) == 1
    assert 'secret.example' not in (profile / 'places.json').read_text()
    assert state.builtins == ['Container.Refresh']
    assert browser.geo_calls == [('FR', 0, 50), ('', 0, 50)]
    assert browser.clicks == browser.lookups == state.resolved == []


@pytest.mark.parametrize('station', [
    Station('none', 'Missing', 'https://x', '', False, city='City'),
    Station('lat', 'Invalid', 'https://x', '', False, city='City', latitude=91, longitude=0),
    Station('lon', 'Invalid', 'https://x', '', False, city='City', latitude=0, longitude=float('nan')),
    Station('nameless', 'No city', 'https://x', '', False, latitude=0, longitude=0),
])
def test_setup_rejects_city_candidates_without_valid_coordinates_or_city(kodi, monkeypatch, station):
    runtime, state, profile = kodi
    browser = setup_directory(monkeypatch, runtime)
    browser.geographic = lambda *args: ([station], False)
    state.dialog_choices = [1]
    state.dialog_inputs = ['City']
    route(runtime, mode='setup')
    assert not (profile / 'places.json').exists()
    assert 'localized-30139' in state.messages
    assert state.builtins == browser.clicks == state.items == state.resolved == []


def test_setup_cities_are_deduplicated_matched_and_scan_is_bounded(kodi, monkeypatch):
    runtime, state, profile = kodi
    browser = setup_directory(monkeypatch, runtime)
    raw = browser.geographic('', 0, 50)[0]
    browser.geo_calls.clear()
    def geographic(code, offset, limit):
        browser.geo_calls.append((code, offset, limit)); return raw * 25, True
    browser.geographic = geographic
    state.dialog_choices, state.dialog_inputs = [1, -1], ['Paris']
    route(runtime, mode='setup')
    assert len(browser.geo_calls) == 4
    assert state.selections[-1][1] == ['Paris · France (48.800, 2.300)']
    assert not (profile / 'places.json').exists()


def test_setup_failure_after_pending_place_does_not_write_and_abort_is_respected(kodi, monkeypatch):
    runtime, state, profile = kodi
    browser = setup_directory(monkeypatch, runtime)
    real_geo = browser.geographic
    def geographic(*args):
        if browser.geo_calls: raise OSError('offline')
        return real_geo(*args)
    browser.geographic = geographic
    state.dialog_choices = [0, 0, 0, 1, 0, 1]
    state.dialog_inputs = ['Tokyo']
    route(runtime, mode='setup')
    assert not (profile / 'places.json').exists()
    assert state.builtins == state.items == state.resolved == browser.clicks == []
    assert 'localized-30012' in state.messages


def test_settings_setup_action_and_guided_first_run_use_same_flow(kodi, monkeypatch):
    runtime, state, profile = kodi
    setup_directory(monkeypatch, runtime)
    settings = ET.parse(ADDON / 'resources/settings.xml')
    setting = settings.find(".//setting[@id='run_setup']")
    assert setting is not None and setting.get('type') == 'action'
    assert setting.findtext('data') == 'RunPlugin(plugin://plugin.audio.kodi_donor_radio/?mode=setup)'
    assert setting.find('control').attrib == {'type':'button','format':'action'}
    state.settings['guided_setup'] = True
    state.dialog_choices = [0, 0, 0, 1, 1]
    route(runtime)
    assert (profile / 'places.json').exists()
    assert state.builtins == ['Container.Refresh']
    assert state.items == []
    state.dialog_choices = []
    route(runtime)
    assert state.items[0][1].label == 'Paris'


def test_station_info_uses_safe_cached_metadata_offline_without_clicks(kodi, monkeypatch):
    from radio_ui import FavoriteStore, StationSummary
    runtime, state, profile = kodi
    browser = directory(monkeypatch, runtime)
    FavoriteStore(profile).add(StationSummary('station-1','Station One',city='Paris',country='France',
                                           codec='MP3',tags='jazz',bitrate=128))
    browser.by_uuid = lambda *args: (_ for _ in ()).throw(OSError('offline'))
    route(runtime, mode='station_info', uuid='station-1')
    assert any('Station One' in message and 'Paris' in message and '128' in message for message in state.messages)
    assert state.items == state.resolved == browser.clicks == state.builtins == []


def test_station_info_lookup_is_cancellable_safe_and_not_playback(kodi, monkeypatch):
    runtime, state, _ = kodi
    browser = directory(monkeypatch, runtime)
    route(runtime, mode='station_info', uuid='station-1')
    assert browser.lookups == ['station-1']
    assert any('Station One' in message for message in state.messages)
    assert all('radio.example' not in message for message in state.messages)
    assert browser.clicks == state.resolved == []


@pytest.mark.parametrize('mode,params', [
    ('favorite_add', {'uuid':'fixture-30020'}), ('favorite_remove', {'uuid':'fixture-30020'}),
    ('station_info', {'uuid':'fixture-30020'}), ('map', {'uuid':'fixture-30020'}),
    ('favorite_add', {'uuid':'../bad'}), ('favorite_remove', {'uuid':'../bad'}),
    ('place_default', {'id':'../bad'}), ('place_radius', {'id':'missing'}), ('place_remove', {'id':'missing'})])
def test_action_routes_reject_unsafe_or_fixture_identity_without_listing_replacement(kodi, monkeypatch, mode, params):
    runtime, state, _ = kodi
    browser = directory(monkeypatch, runtime)
    route(runtime, mode=mode, **params)
    assert state.items == state.resolved == state.builtins == browser.lookups == browser.clicks == []
    assert state.ended == [{'succeeded':False,'cacheToDisc':False}]
    assert 'localized-30012' in state.messages


def playback_client(runtime, monkeypatch):
    class Client:
        descriptor = ServiceDescriptor(12345, 'a'*48, 'b'*64)
        def create(self, station):
            return {'id':'c'*48,'playback_url':'http://127.0.0.1:12345/media/'+'c'*48+'/'+'d'*48,
                    'mime_type':'audio/mpeg'}
    monkeypatch.setattr(runtime, '_service', lambda *args: Client())


def test_deliberate_play_marks_favorite_timestamp_and_context_play_works_without_handle(kodi, monkeypatch):
    from radio_ui import FavoriteStore, StationSummary
    runtime, state, profile = kodi
    browser = directory(monkeypatch, runtime)
    store = FavoriteStore(profile)
    store.add(StationSummary.from_station(STATION))
    playback_client(runtime, monkeypatch)
    runtime.run_plugin(['plugin://plugin.audio.kodi_donor_radio','-1','?mode=play&uuid=station-1'])
    assert len(state.players) == 1 and state.players[0].playing
    assert state.players[0].file.startswith('http://127.0.0.1:12345/media/')
    assert store.list()[0].last_played
    assert state.resolved == state.ended == []
    assert browser.clicks == ['station-1']


def test_failed_play_and_map_selection_do_not_mark_favorite_played(kodi, monkeypatch):
    from radio_ui import FavoriteStore, StationSummary
    runtime, state, profile = kodi
    browser = directory(monkeypatch, runtime)
    install_map_window(monkeypatch)
    store = FavoriteStore(profile)
    store.add(StationSummary.from_station(STATION))
    route(runtime, mode='map', uuid='station-1')
    assert store.list()[0].last_played == ''
    monkeypatch.setattr(runtime,'_service',lambda *args: (_ for _ in ()).throw(OSError('offline')))
    route(runtime, mode='play', uuid='station-1')
    assert store.list()[0].last_played == ''
    assert browser.clicks == []


def test_setup_abort_at_final_save_and_progress_cancel_leave_existing_file_untouched(kodi, monkeypatch):
    from radio_ui import PlaceStore
    runtime, state, profile = kodi
    store = PlaceStore(profile)
    store.save('Existing', 0, 0, 10)
    before = store.path.read_bytes()
    setup_directory(monkeypatch, runtime)
    state.dialog_choices = [0, 0, 0, 1, 1]
    def abort_on_save():
        if len(state.selections) == 5: state.abort = True
    state.on_select = abort_on_save
    route(runtime, mode='setup')
    assert state.dialog_choices == []
    assert store.path.read_bytes() == before
    assert state.builtins == state.items == state.resolved == []
    state.abort, state.cancel, state.on_select = False, True, None
    state.dialog_choices = [0]
    route(runtime, mode='setup')
    assert store.path.read_bytes() == before
    assert state.builtins == state.items == state.resolved == []


def test_favorite_lookup_identity_mismatch_never_saves_or_shows_wrong_station(kodi, monkeypatch):
    runtime, state, profile = kodi
    browser = directory(monkeypatch, runtime)
    browser.by_uuid = lambda uuid: Station('wrong', 'Wrong', 'https://x', '', False)
    route(runtime, mode='favorite_add', uuid='station-1')
    route(runtime, mode='station_info', uuid='station-1')
    assert not (profile / 'favorites.json').exists()
    assert state.items == state.resolved == state.builtins == browser.clicks == []
    assert not any('Wrong' in message for message in state.messages)


def test_settings_setup_button_closes_settings_before_native_dialogs():
    settings = ET.parse(ADDON / 'resources/settings.xml')
    assert settings.findtext(".//setting[@id='run_setup']/control/close") == 'true'


def test_ordinary_play_timestamps_favorite_and_profile_failure_cannot_revoke_play(kodi, monkeypatch):
    from radio_ui import FavoriteStore, StationSummary
    runtime, state, profile = kodi
    browser = directory(monkeypatch, runtime)
    store = FavoriteStore(profile)
    store.add(StationSummary.from_station(STATION))
    playback_client(runtime, monkeypatch)
    route(runtime, mode='play', uuid='station-1')
    stamp = store.list()[0].last_played
    assert stamp and state.resolved[-1][0]
    def fail(*args): raise OSError('disk unavailable')
    monkeypatch.setattr(FavoriteStore, 'mark_played', fail)
    route(runtime, mode='play', uuid='station-1')
    assert state.resolved[-1][0] and store.list()[0].last_played == stamp
    assert browser.clicks == ['station-1', 'station-1']
    assert 'localized-30011' not in state.messages


def test_setup_from_runplugin_without_directory_handle_saves_and_only_refreshes(kodi, monkeypatch):
    from radio_ui import PlaceStore
    runtime, state, profile = kodi
    setup_directory(monkeypatch, runtime)
    state.dialog_choices = [0, 0, 0, 1, 1]
    runtime.run_plugin(['plugin://plugin.audio.kodi_donor_radio/', '-1', '?mode=setup'])
    assert [p.name for p in PlaceStore(profile).list()] == ['Paris']
    assert state.builtins == ['Container.Refresh']
    assert state.contents == state.ended == state.resolved == state.items == []


@pytest.mark.parametrize('choice,expected_km', [
    (0, 16.09344), (1, 40.2336), (2, 96.56064), (3, 160.9344), (4, 402.336),
])
def test_place_radius_displays_miles_and_converts_selection_for_store(kodi, choice, expected_km):
    from radio_ui import PlaceStore
    runtime, state, profile = kodi
    store = PlaceStore(profile)
    place = store.save('Home', 0, 0, 10)
    state.dialog_choices = [choice]
    route(runtime, mode='place_radius', id=place.place_id)
    assert state.selections == [('localized-30135', [
        '10 localized-30136', '25 localized-30136', '60 localized-30136',
        '100 localized-30136', '250 localized-30136'])]
    assert store.list()[0].radius_km == pytest.approx(expected_km)
    assert state.builtins == ['Container.Refresh']


def test_radius_unit_translation_is_miles():
    strings = (ADDON / 'resources/language/resource.language.en_gb/strings.po').read_text()
    entries = dict(re.findall(r'^msgctxt "#(\d+)"\nmsgid (".*")(?:\n|$)', strings, re.MULTILINE))
    assert json.loads(entries['30136']) == 'miles'
