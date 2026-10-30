# SPDX-License-Identifier: GPL-2.0-or-later
"""Load the addon library without importing Kodi integration modules."""
import importlib
import sys
import time
from pathlib import Path
import shutil
import subprocess
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugin.audio.kodi_donor_radio" / "resources" / "lib"))

ADDON = Path(__file__).resolve().parents[1] / 'plugin.audio.kodi_donor_radio'


@pytest.fixture
def scenario(monkeypatch):
    import threading
    from ad_models import OutputSegment, Placement, Presentation, ResolvedAd, TrackingUrl
    from kodi_ad_player import KodiAdPlayback
    from sessions import ContinuousAudioState

    state = SimpleNamespace(clock=.25, now=10.0, caching=False, playing=True,
                            audio=ContinuousAudioState(True, 480000, False))
    records, queued = [], []
    ad = ResolvedAd('creative', 'fixture.ts', 1, tuple(
        TrackingUrl(event, 'http://127.0.0.1:8765/track/' + event)
        for event in ('impression', 'start', 'firstQuartile', 'midpoint',
                      'thirdQuartile', 'complete')), ())
    state.presentation = Presentation(
        (OutputSegment(0, 0, 'source', 0, 480000, None),),
        (Placement('placement', ad, 24000, 72000),), False)
    session = SimpleNamespace(
        id='session', cancel=threading.Event(), ad_enabled=True,
        station=SimpleNamespace(uuid='fixture-continuous'),
        source=SimpleNamespace(protocol='hls'),
        presentation=lambda: state.presentation, take_ad_errors=lambda: [],
        playback_url='http://127.0.0.1:8765/media/session/root.m3u8',
        continuous_playback_url='http://127.0.0.1:8765/media/session/root.aac',
        continuous_audio_state=lambda: state.audio)
    state.path = session.continuous_playback_url
    player = SimpleNamespace(isPlaying=lambda: state.playing,
                             getTime=lambda: state.clock,
                             getPlayingFile=lambda: state.path)

    class Worker:
        def __init__(self, *_args): pass
        def enqueue(self, *args): queued.append(args); return True
        def close(self): pass

    monkeypatch.setattr('kodi_ad_player.BeaconWorker', Worker)
    monkeypatch.setattr('kodi_ad_player.time', SimpleNamespace(monotonic=lambda: state.now))
    manager = SimpleNamespace(get=lambda identity: session if identity == session.id else None,
                              sink=SimpleNamespace(emit=records.append))
    adapter = KodiAdPlayback(manager, player, client=SimpleNamespace(),
                             fixture_origin='http://127.0.0.1:8765',
                             is_caching=lambda: state.caching)

    def sample(clock, elapsed=.25):
        state.clock = clock
        state.now += elapsed
        adapter.sample()
        return [record.details for record in records if record.kind == 'ad_clock_sample'][-1]

    yield SimpleNamespace(state=state, session=session, player=player, adapter=adapter,
                          records=records, queued=queued, sample=sample)
    adapter.close()


@pytest.fixture
def kodi(monkeypatch, tmp_path):
    state = SimpleNamespace(items=[], resolved=[], ended=[], messages=[], notifications=[], logs=[], settings={},
                            input='jazz', cancel=False, abort=False, waits=0, players=[], action=None,
                            player_paused=False, player_caching=False, selections=[], confirmations=[],
                            dialog_choices=None, dialog_inputs=None, builtins=[], contents=[],
                            selection=-1, remember=False, memory_selection=None, on_select=None, on_confirm=None,
                            window_properties={})
    class Item:
        def __init__(self, label='', path=''):
            self.label, self.path, self.props = label, path, {}
            self.label2, self.art, self.info, self.context = '', {}, {}, []
            self.music_info = MusicInfoTag()
        def addContextMenuItems(self, values): self.context.extend(values)
        def getMusicInfoTag(self): return self.music_info
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
        def notification(self, heading, message, icon, duration):
            state.notifications.append((heading, message, icon, duration))
    class MusicInfoTag:
        def __init__(self): self.title, self.genres, self.comment = '', [], ''
        def setTitle(self, value): self.title = value
        def setGenres(self, value): self.genres = value
        def setComment(self, value): self.comment = value
    class Progress:
        def __init__(self): self.created = False
        def create(self, heading, message):
            self.created = True
            state.messages.append(message)
        def iscanceled(self):
            if not self.created: raise RuntimeError('Dialog not created.')
            return state.cancel
        def close(self):
            if not self.created: raise RuntimeError('Dialog not created.')
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
    class Window:
        def __init__(self, window_id): self.window_id = window_id
        def getProperty(self, key): return state.window_properties.get(key, '')
        def setProperty(self, key, value): state.window_properties[key] = value
        def clearProperty(self, key): state.window_properties.pop(key, None)
    modules = {
        'xbmc': SimpleNamespace(Monitor=Monitor, Player=Player, LOGDEBUG=0,
                                getCondVisibility=lambda condition: state.player_paused if condition == 'Player.Paused' else state.player_caching,
                                executebuiltin=lambda value: state.builtins.append(value),
                                log=lambda value, level: state.logs.append(value)),
        'xbmcaddon': SimpleNamespace(Addon=Addon),
        'xbmcgui': SimpleNamespace(ListItem=Item, Dialog=Dialog, DialogProgress=Progress,
                                  Window=Window, NOTIFICATION_ERROR='error'),
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
