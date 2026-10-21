# SPDX-License-Identifier: GPL-2.0-or-later
"""Kodi-only UI and service boundary; all media passes through the embedded relay."""
from contextlib import contextmanager
from dataclasses import asdict
import json
import os
from pathlib import Path
import re
import tempfile
import threading
import time
from urllib.parse import parse_qs, urlencode, urlsplit

import xbmc
import xbmcaddon
import xbmcgui
import xbmcplugin
import xbmcvfs

from control import ControlClient, ServiceDescriptor
from events import JsonEventSink
from http_client import Cancelled, HttpClient
from models import Station
from radio_browser import RadioBrowser
from relay import RelayServer
from sessions import SessionManager
from url_policy import UrlPolicy

STARTUP_TIMEOUT = 5.0
PAGE_SIZE = 50
_HEX48 = re.compile(r'^[0-9a-f]{48}$')
_HEX64 = re.compile(r'^[0-9a-f]{64}$')


def text(identifier):
    return xbmcaddon.Addon().getLocalizedString(identifier)


def profile_path():
    return Path(xbmcvfs.translatePath(xbmcaddon.Addon().getAddonInfo('profile')))


def _private_profile(profile):
    profile = Path(profile)
    profile.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(str(profile), 0o700)
    return profile


@contextmanager
def _descriptor_lock(profile):
    """Serialize publish and compare/remove across Kodi's separate interpreters."""
    profile = _private_profile(profile)
    fd = os.open(str(profile / 'service.lock'), os.O_CREAT | os.O_RDWR, 0o600)
    try:
        os.chmod(str(profile / 'service.lock'), 0o600)
        if os.name == 'nt':
            import msvcrt
            if os.fstat(fd).st_size == 0:
                os.write(fd, b'\0')
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_LOCK, 1)
        else:
            import fcntl
            fcntl.flock(fd, fcntl.LOCK_EX)
        try:
            yield profile
        finally:
            if os.name == 'nt':
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def _descriptor(record):
    if (not isinstance(record, dict) or set(record) != {'port', 'instance_id', 'control_token'}
            or type(record['port']) is not int or not 1 <= record['port'] <= 65535
            or not isinstance(record['instance_id'], str) or not _HEX48.fullmatch(record['instance_id'])
            or not isinstance(record['control_token'], str) or not _HEX64.fullmatch(record['control_token'])):
        raise ValueError('Invalid service descriptor')
    return ServiceDescriptor(**record)


def read_descriptor(profile):
    with (Path(profile) / 'service.json').open('rb') as source:
        payload = source.read(2049)
    if len(payload) > 2048:
        raise ValueError('Invalid service descriptor')
    return _descriptor(json.loads(payload))


def publish_descriptor(profile, descriptor):
    _descriptor(asdict(descriptor))
    with _descriptor_lock(profile) as profile:
        fd, filename = tempfile.mkstemp(prefix='.service-', dir=str(profile))
        try:
            with os.fdopen(fd, 'w', encoding='utf-8') as output:
                json.dump(asdict(descriptor), output)
                output.flush()
                os.fsync(output.fileno())
            os.chmod(filename, 0o600)
            os.replace(filename, str(profile / 'service.json'))
        finally:
            if os.path.exists(filename):
                os.unlink(filename)


def remove_descriptor(profile, descriptor):
    with _descriptor_lock(profile) as profile:
        try:
            if read_descriptor(profile) == descriptor:
                (profile / 'service.json').unlink()
        except (OSError, ValueError):
            pass


def fixture_origin():
    """A numeric loopback host and explicit port are the only development exception."""
    addon = xbmcaddon.Addon()
    if not addon.getSettingBool('test_mode'):
        return None
    value = addon.getSetting('test_origin')
    parts = urlsplit(value)
    if (parts.scheme != 'http' or parts.hostname != '127.0.0.1'
            or parts.username is not None or parts.password is not None
            or parts.path or parts.query or parts.fragment or parts.port is None
            or not 1 <= parts.port <= 65535
            or value != 'http://127.0.0.1:%d' % parts.port):
        raise ValueError('Invalid fixture origin')
    return value


def runtime_policy():
    origin = fixture_origin()
    return UrlPolicy(test_origins={origin} if origin else frozenset())


def event_sink(profile):
    return JsonEventSink(profile, xbmcaddon.Addon().getSettingBool('diagnostics'),
                         lambda line: xbmc.log(line, xbmc.LOGDEBUG))


def _error(identifier):
    xbmcgui.Dialog().ok(text(30000), text(identifier))


def _operation(function, monitor, cancel, cleanup=None):
    """Keep Kodi UI responsive; late create results are retired after cancellation."""
    progress = xbmcgui.DialogProgress()
    progress.create(text(30000), text(30013))
    done, lock, result = threading.Event(), threading.Lock(), {}
    def work():
        try:
            value = function()
            with lock:
                abandoned = cancel.is_set()
                if not abandoned:
                    result['value'] = value
            if abandoned and cleanup:
                cleanup(value)
        except Exception as error:
            with lock:
                result['error'] = error
        finally:
            done.set()
    thread = threading.Thread(target=work, daemon=True)
    thread.start()
    try:
        while True:
            if monitor.abortRequested() or progress.iscanceled():
                with lock:
                    cancel.set()
                    value = result.pop('value', None)
                if value is not None and cleanup:
                    cleanup(value)
                raise Cancelled('Request cancelled')
            if done.is_set():
                if 'error' in result:
                    raise result['error']
                return result.get('value')
            monitor.waitForAbort(.05)
    finally:
        progress.close()


def _service(profile, monitor, cancel):
    deadline = time.monotonic() + STARTUP_TIMEOUT
    while time.monotonic() < deadline:
        if monitor.abortRequested() or cancel.is_set():
            raise Cancelled('Request cancelled')
        try:
            descriptor = read_descriptor(profile)
            client = ControlClient(descriptor, timeout=min(.5, max(.01, deadline - time.monotonic())))
            health = client.health()
            if health == {'status': 'ok', 'instance_id': descriptor.instance_id}:
                return ControlClient(descriptor)
        except (OSError, ValueError):
            pass
        if monitor.waitForAbort(.1):
            raise Cancelled('Request cancelled')
    raise OSError('Service unavailable')


def _relay_path(value, port):
    try:
        parts = urlsplit(value)
        match = re.fullmatch(r'/media/([0-9a-f]{48})/[0-9a-f]{48}'
                             r'(?:\.(?:m3u8|ts|aac|mp3|mp4|m4s|m4a|ac3|ec3))?', parts.path)
        if (parts.scheme == 'http' and parts.netloc == '127.0.0.1:%d' % port
                and not parts.query and not parts.fragment and match):
            return match.group(1)
    except ValueError:
        pass
    return None


def _station(value):
    record = json.loads(value)
    if (not isinstance(record, dict) or set(record) != {'uuid','name','url','codec','hls'}
            or any(not isinstance(record[key], str) or not record[key] for key in ('uuid','name','url'))
            or not isinstance(record['codec'], str) or type(record['hls']) is not bool):
        raise ValueError('Invalid station')
    return Station(**record)


def run_plugin(argv):
    base, handle = argv[0], int(argv[1])
    params = {key: values[0] for key, values in parse_qs(argv[2].lstrip('?') if len(argv)>2 else '').items()}
    action = params.get('action', 'home')
    monitor, cancel = xbmc.Monitor(), threading.Event()
    browser = RadioBrowser(cancel=cancel)
    def url(**values): return base + '?' + urlencode(values)
    def item(label, folder=True, **values):
        entry = xbmcgui.ListItem(label=label)
        if not folder: entry.setProperty('IsPlayable', 'true')
        xbmcplugin.addDirectoryItem(handle, url(**values), entry, isFolder=folder)
    def station_item(station):
        item(station.name, folder=False, action='play', station=json.dumps(asdict(station)))
    if action == 'play':
        client, session = None, None
        def stop(value):
            if client is not None and isinstance(value, dict) and _HEX48.fullmatch(str(value.get('id',''))):
                try: client.stop(value['id'])
                except (OSError, ValueError): pass
        try:
            station = _station(params.get('station',''))
            # Fixture selection cannot activate the exception while test mode is disabled.
            if station.uuid.startswith('fixture-'):
                origin = fixture_origin()
                if origin is None or not station.url.startswith(origin + '/'):
                    raise ValueError('Test mode disabled')
            try:
                client = _operation(lambda: _service(profile_path(), monitor, cancel), monitor, cancel)
            except OSError as error:
                if isinstance(error, Cancelled): raise
                _error(30010)
                xbmcplugin.setResolvedUrl(handle, False, xbmcgui.ListItem())
                return
            session = _operation(lambda: client.create(station), monitor, cancel, stop)
            if not isinstance(session, dict):
                raise ValueError('Invalid relay response')
            session_id = _relay_path(session.get('playback_url',''), client.descriptor.port)
            if session_id is None or session.get('id') != session_id:
                raise ValueError('Invalid relay response')
            if not station.uuid.startswith('fixture-'):
                _operation(lambda: browser.click(station.uuid), monitor, cancel)
            entry = xbmcgui.ListItem(label=station.name, path=session['playback_url'])
            entry.setProperty('IsPlayable', 'true')
            # Let Kodi discover Content-Type from the authoritative relay response;
            # directory codec/HLS fields can be wrong after playlist indirection.
            xbmcplugin.setResolvedUrl(handle, True, entry)
        except (OSError, ValueError, KeyError, TypeError):
            if session: stop(session)
            if not cancel.is_set(): _error(30011)
            xbmcplugin.setResolvedUrl(handle, False, xbmcgui.ListItem())
        return
    xbmcplugin.setContent(handle, 'songs')
    try:
        offset = int(params.get('offset', '0'))
        if offset < 0: raise ValueError('Invalid page')
        if action == 'home':
            for label, target in ((30001,'search'),(30002,'countries'),(30003,'tags'),(30004,'settings')):
                item(text(label), action=target)
            if xbmcaddon.Addon().getSettingBool('test_mode'):
                item(text(30007), action='tests')
        elif action == 'settings':
            xbmcaddon.Addon().openSettings()
        elif action in ('search','stations'):
            name = params.get('name','')
            if action == 'search':
                name = xbmcgui.Dialog().input(text(30001))
                if not name: return
            country, tag = params.get('country',''), params.get('tag','')
            stations = _operation(lambda: browser.search(name,country,tag,offset,PAGE_SIZE), monitor,cancel)
            for station in stations: station_item(station)
            if len(stations)==PAGE_SIZE:
                item(text(30005),action='stations',name=name,country=country,tag=tag,offset=offset+PAGE_SIZE)
        elif action in ('countries','tags'):
            names = _operation(lambda: browser.countries() if action=='countries' else browser.tags(offset,PAGE_SIZE),monitor,cancel)
            page = names[offset:offset+PAGE_SIZE] if action=='countries' else names
            for name in page:
                item(name, action='stations', **{'country' if action=='countries' else 'tag': name})
            if (action=='countries' and offset+PAGE_SIZE<len(names)) or (action=='tags' and len(page)==PAGE_SIZE):
                item(text(30005),action=action,offset=offset+PAGE_SIZE)
        elif action == 'tests':
            origin = fixture_origin()
            if origin:
                fixtures = [(30020,'continuous.mp3','MP3',False),(30021,'continuous.aac','AAC',False),
                            (30022,'icy.mp3','MP3',False),(30023,'hls/normal/index.m3u8','AAC',True),
                            (30024,'hls/mid-break/index.m3u8','AAC',True),(30025,'hls/missing-end/index.m3u8','AAC',True),
                            (30026,'hls/no-cue/index.m3u8','AAC',True),(30027,'hls/malformed-cue/index.m3u8','AAC',True)]
                for label,path,codec,hls in fixtures:
                    station_item(Station('fixture-%d'%label,text(label),origin+'/'+path,codec,hls))
        else:
            raise ValueError('Unknown navigation action')
    except Cancelled:
        pass
    except (OSError,ValueError,TypeError):
        _error(30012)
        item(text(30006), **params)
    finally:
        xbmcplugin.endOfDirectory(handle, cacheToDisc=False)


class RelayPlayer(xbmc.Player):
    def __init__(self, manager, port):
        super().__init__()
        self.manager, self.port, self.owned = manager, port, None

    def onAVStarted(self):
        try: current = _relay_path(self.getPlayingFile(), self.port)
        except RuntimeError: current = None
        old, self.owned = self.owned, current
        if old is not None and old != current:
            self.manager.stop(old)

    def onPlayBackStarted(self):
        self.onAVStarted()

    def _finished(self):
        # A stop callback for the old stream can arrive after a rapid switch.
        if self.isPlaying():
            return
        old, self.owned = self.owned, None
        if old is not None:
            self.manager.stop(old)

    def onPlayBackStopped(self): self._finished()
    def onPlayBackEnded(self): self._finished()
    def onPlayBackError(self): self._finished()


class ServiceMonitor(xbmc.Monitor):
    def __init__(self):
        super().__init__()
        self.settings_changed = False

    def onSettingsChanged(self):
        self.settings_changed = True


def run_service():
    monitor = ServiceMonitor()
    while not monitor.abortRequested():
        server, descriptor = None, None
        try:
            profile = _private_profile(profile_path())
            monitor.settings_changed = False
            manager = SessionManager(client=HttpClient(policy=runtime_policy()), sink=event_sink(profile))
            server = RelayServer(manager=manager)
            descriptor = server.start()
            player = RelayPlayer(manager, descriptor.port)
            publish_descriptor(profile, descriptor)
            while not monitor.abortRequested() and not monitor.settings_changed:
                manager.expire()
                if monitor.waitForAbort(.5): break
        except (OSError, ValueError):
            _error(30014)
            # Stay subscribed so correcting settings can restart a failed service.
            while not monitor.abortRequested() and not monitor.settings_changed:
                if monitor.waitForAbort(.5): break
        finally:
            if server is not None: server.close()
            if descriptor is not None: remove_descriptor(profile, descriptor)
