# SPDX-License-Identifier: GPL-2.0-or-later
"""Kodi 21.3 WindowXML adapter for the shared radio discovery map."""
from urllib.parse import parse_qs, urlencode, urlsplit
import math
import os
import threading
import re
import time

import xbmc
import xbmcaddon
import xbmcgui

from radio_map import (MapView, cluster_cities, nearest_city_in_direction,
                       project, visible_cities)
from playback_arbiter import PlaybackStartArbiter
from radio_ui import station_identifier


PIN_FIRST_ID = 1000
PIN_LIMIT = 60
RAIL_ID = 2000
PREVIEW_ID = 3000
BACK_ID = 3001
STATUS_ID = 3002
MAP_LEFT, MAP_TOP, MAP_WIDTH, MAP_HEIGHT = 75, 145, 1030, 590
RAIL_LIMIT = 50
ACTION_IDS = {'left': 1, 'right': 2, 'up': 3, 'down': 4}
BACK_ACTIONS = (10, 92)
PENDING_PLAY_HANDOFF_SECONDS = 5.0
PENDING_PLAY_POLL_SECONDS = 0.05
_GRID_STEPS = (120, 90, 60, 45, 30, 20, 15, 10, 5, 2, 1, .5,
               .25, .125, .0625, .01, .001)


def _valid_coordinates(latitude, longitude):
    for value, low, high in ((latitude, -90, 90), (longitude, -180, 180)):
        if isinstance(value, bool):
            return False
        try:
            number = float(value)
        except (TypeError, ValueError, OverflowError):
            return False
        if not math.isfinite(number) or number < low or number > high:
            return False
    return True


def _grid_value_count(start, end, step):
    """Count strict-interior grid lines without allocating their coordinates."""
    first = math.floor(start / step) + 1
    last = math.ceil(end / step) - 1
    return max(0, last - first + 1)


def _grid_values(start, end):
    """Choose a bounded, readable set of evenly spaced graticule values."""
    candidates = []
    for step in _GRID_STEPS:
        first = math.floor(start / step) + 1
        last = math.ceil(end / step) - 1
        count = _grid_value_count(start, end, step)
        if not 1 <= count <= 5:
            continue
        values = [round(index * step, 8) for index in range(first, last + 1)
                  if start < index * step < end]
        candidates.append((abs(4 - count), -count, -step, values))
    return min(candidates)[3] if candidates else []


def _valid_origin(route, plugin_base):
    if not isinstance(route, str) or len(route) > 1024:
        raise ValueError('Invalid map origin')
    parts = urlsplit(route)
    base = urlsplit(plugin_base)
    if (base.scheme != 'plugin' or base.netloc != 'plugin.audio.kodi_donor_radio' or
            base.path not in ('', '/') or base.query or base.fragment or
            parts.scheme != 'plugin' or parts.netloc != base.netloc or
            parts.path not in ('', '/') or len(parts.query) > 512 or
            parts.fragment or parts.username or parts.password):
        raise ValueError('Invalid map origin')
    query = parse_qs(parts.query, keep_blank_values=True, strict_parsing=True)
    modes = {
        'home': set(), 'favorites': {'offset'}, 'place': {'id', 'offset'},
        'tag': {'tag', 'offset'}, 'country': {'code', 'offset'},
        'search': {'name', 'country', 'tag', 'offset'},
        'stations': {'name', 'country', 'tag', 'offset'},
    }
    if len(query.get('mode', ())) != 1:
        raise ValueError('Invalid map origin')
    mode = query['mode'][0]
    if mode not in modes or set(query) - ({'mode'} | modes[mode]):
        raise ValueError('Invalid map origin')
    for key, values in query.items():
        if len(values) != 1 or len(values[0]) > 256 or '://' in values[0]:
            raise ValueError('Invalid map origin')
        if any(ord(char) < 32 or ord(char) == 127 for char in values[0]):
            raise ValueError('Invalid map origin')
    if 'id' in query:
        station_identifier(query['id'][0])
    if 'offset' in query and (not query['offset'][0].isdigit() or int(query['offset'][0]) > 10000000):
        raise ValueError('Invalid map origin')
    if 'code' in query and not re.fullmatch(r'[A-Z]{2}', query['code'][0]):
        raise ValueError('Invalid map origin')
    return route


def _safe_station(station):
    station_uuid = station_identifier(getattr(station, 'uuid', getattr(station, 'station_uuid', '')))
    if station_uuid.startswith('fixture-'):
        raise ValueError('Fixtures cannot be opened in the public map')
    return station_uuid


class PreviewController:
    """Debounced, generation-guarded playback whose cleanup is session-owned."""

    def __init__(self, start, can_play, play, current_file, is_playing,
                 stop_playback, stop_session, timer_factory=None, thread_factory=None,
                 delay=0.4, pending_start_timeout=PENDING_PLAY_HANDOFF_SECONDS,
                 clock=None, sleep=None, playback_arbiter=None):
        self._start, self._can_play, self._play = start, can_play, play
        self._current_file, self._is_playing = current_file, is_playing
        self._stop_playback, self._stop_session = stop_playback, stop_session
        self._timer_factory = timer_factory or threading.Timer
        self._thread_factory = thread_factory or threading.Thread
        self._delay = delay
        self._pending_start_timeout = pending_start_timeout
        self._clock = clock or time.monotonic
        self._sleep = sleep or time.sleep
        self._playback_arbiter = playback_arbiter or PlaybackStartArbiter()
        self._lock = threading.RLock()
        # Serialize lifecycle changes with the final player handoff. A stale
        # resolution must not call Player.play after cancel/close has returned.
        self._handoff_lock = threading.RLock()
        self._generation = 0
        self._timer = None
        self._request_cancel = None
        self._closed = False
        self.owned = None
        self._pending_playback_url = ''

    def focus_station(self, station_uuid):
        station_uuid = station_identifier(station_uuid)
        with self._handoff_lock:
            with self._lock:
                self._generation += 1
                generation = self._generation
                timer, self._timer = self._timer, None
                request_cancel, self._request_cancel = self._request_cancel, None
                owned, self.owned = self.owned, None
                pending = bool(owned and self._pending_playback_url == owned.get('playback_url'))
                self._pending_playback_url = ''
            if timer is not None:
                timer.cancel()
            if request_cancel is not None:
                request_cancel.set()
            self._stop_owned(owned, pending=pending)
            with self._lock:
                if self._closed:
                    return
                request_cancel = threading.Event()
                self._request_cancel = request_cancel
                timer = self._timer_factory(
                    self._delay, lambda: self._launch(generation, station_uuid, request_cancel))
                self._timer = timer
                timer.daemon = True
                timer.start()

    def cancel(self):
        with self._handoff_lock:
            with self._lock:
                self._generation += 1
                timer, self._timer = self._timer, None
                request_cancel, self._request_cancel = self._request_cancel, None
                owned, self.owned = self.owned, None
                pending = bool(owned and self._pending_playback_url == owned.get('playback_url'))
                self._pending_playback_url = ''
            if timer is not None:
                timer.cancel()
            if request_cancel is not None:
                request_cancel.set()
            self._stop_owned(owned, pending=pending)

    def _launch(self, generation, station_uuid, request_cancel):
        with self._lock:
            if self._closed or generation != self._generation or request_cancel.is_set():
                return
        worker = self._thread_factory(
            target=lambda: self._resolve(generation, station_uuid, request_cancel), daemon=True)
        worker.start()

    def _resolve(self, generation, station_uuid, request_cancel):
        try:
            result = self._start(station_uuid, request_cancel)
            if not isinstance(result, dict):
                return
            if (not isinstance(result.get('session_id'), str) or not result['session_id']
                    or not isinstance(result.get('playback_url'), str) or not result['playback_url']):
                if isinstance(result.get('session_id'), str) and result['session_id']:
                    self._retire(result['session_id'])
                return
        except Exception:
            return
        with self._handoff_lock:
            with self._lock:
                current = (not self._closed and generation == self._generation
                           and not request_cancel.is_set())
            if not current or not self._can_play(result):
                stale = True
            else:
                with self._lock:
                    if self._closed or generation != self._generation or request_cancel.is_set():
                        stale = True
                    else:
                        self.owned = result
                        self._pending_playback_url = result['playback_url']
                        stale = False
                if not stale:
                    try:
                        self._play(result)
                    except Exception:
                        with self._lock:
                            if self.owned is result:
                                self.owned = None
                                self._pending_playback_url = ''
                        stale = True
        if stale:
            self._retire(result['session_id'])

    def _retire(self, session_id):
        try:
            self._stop_session(session_id)
        except Exception:
            pass

    def _stop_owned(self, result, pending=False):
        if result is None:
            return
        intent = result.get('_playback_intent')
        try:
            current_intent = self._playback_arbiter.is_current_intent(intent)
        except Exception:
            # Lock state is unknown: fail closed for Player.stop, but always
            # release this preview's relay session.
            self._retire(result['session_id'])
            return
        if not current_intent:
            self._retire(result['session_id'])
            return

        def can_stop():
            try:
                return (self._is_playing()
                        and self._current_file() == result.get('playback_url'))
            except Exception:
                return False

        try:
            stopped_playback = self._playback_arbiter.stop_if_current(
                intent, can_stop, self._stop_playback)
        except Exception:
            stopped_playback = False
        if pending and not stopped_playback:
            self._defer_pending_stop(result)
            return
        self._retire(result['session_id'])

    def _defer_pending_stop(self, result):
        """Give Kodi a bounded window to acknowledge an asynchronous Player.play."""
        def finish_handoff():
            try:
                deadline = self._clock() + self._pending_start_timeout
                while True:
                    try:
                        current_intent = self._playback_arbiter.is_current_intent(
                            result.get('_playback_intent'))
                    except Exception:
                        # The shared lock is unavailable; never stop based on
                        # an intent we could not verify.
                        break
                    if not current_intent:
                        break
                    try:
                        playing = self._is_playing()
                        current = self._current_file()
                    except Exception:
                        playing, current = False, ''
                    if playing and current == result.get('playback_url'):
                        try:
                            self._playback_arbiter.stop_if_current(
                                result.get('_playback_intent'),
                                lambda: self._is_playing() and
                                self._current_file() == result.get('playback_url'),
                                self._stop_playback)
                        except Exception:
                            pass
                        break
                    # A different URL is not an acknowledgment that Kodi dropped
                    # this queued request; wait for our URL or the bounded timeout.
                    remaining = deadline - self._clock()
                    if remaining <= 0:
                        break
                    self._sleep(min(PENDING_PLAY_POLL_SECONDS, remaining))
            finally:
                self._retire(result['session_id'])

        worker = self._thread_factory or threading.Thread
        worker(target=finish_handoff, daemon=True).start()

    def close(self):
        with self._handoff_lock:
            with self._lock:
                self._closed = True
                self._generation += 1
                timer, self._timer = self._timer, None
                request_cancel, self._request_cancel = self._request_cancel, None
                owned, self.owned = self.owned, None
                pending = bool(owned and self._pending_playback_url == owned.get('playback_url'))
                self._pending_playback_url = ''
            if timer is not None:
                timer.cancel()
            if request_cancel is not None:
                request_cancel.set()
            self._stop_owned(owned, pending=pending)


class RadioMapWindow(xbmcgui.WindowXML):
    """One skin-independent map window shared by root and station actions.

    Network discovery is completed through the runtime's cancellable
    ``_operation`` boundary before construction. The window only handles
    local projection, navigation, and UUID-safe playback requests.
    """

    def __new__(cls, xml_filename, script_path, default_skin='Default',
                default_res='720p', is_media=False, **state):
        # Kodi's SWIG WindowXML constructor is invoked from tp_new, before this
        # class's __init__. Consume add-on state here so only native parameters
        # reach Kodi's allocator.
        return super().__new__(cls, xml_filename, script_path, default_skin,
                               default_res, is_media)

    def __init__(self, xml_filename, script_path, default_skin, default_res, is_media,
                 origin_route, selected_uuid, stations, view, preview_enabled,
                 addon_path, plugin_base='plugin://plugin.audio.kodi_donor_radio',
                 execute=None, preview_start=None, preview_stop=None, player_factory=None,
                 timer_factory=None, thread_factory=None, status_message='',
                 playback_arbiter=None):
        self.plugin_base = plugin_base
        home_route = _valid_origin(plugin_base + '?mode=home', plugin_base)
        try:
            self.origin_route = _valid_origin(origin_route, plugin_base)
        except ValueError:
            # Origin is optional return context, never a playback target. A
            # long/encoded search query must not prevent opening the map.
            self.origin_route = home_route
        self.selected_uuid = station_identifier(selected_uuid) if selected_uuid else ''
        if self.selected_uuid.startswith('fixture-'):
            raise ValueError('Fixture station UUID is not valid for map')
        if not isinstance(view, MapView):
            raise TypeError('Map view is required')
        self.view = view
        incoming = list(stations)
        if self.selected_uuid:
            selected = next((station for station in incoming
                             if getattr(station, 'uuid', getattr(station, 'station_uuid', ''))
                             == self.selected_uuid), None)
            if selected is not None:
                incoming = [selected] + [station for station in incoming if station is not selected]
        self.station_by_uuid = {}
        for station in incoming:
            if len(self.station_by_uuid) >= 200:
                break
            try:
                station_uuid = _safe_station(station)
            except ValueError:
                continue
            # The map and its station rail are limited to stations with a
            # valid geographic position; locationless records have no map action.
            if not _valid_coordinates(getattr(station, 'latitude', None),
                                      getattr(station, 'longitude', None)):
                continue
            self.station_by_uuid.setdefault(station_uuid, station)
        self.stations = list(self.station_by_uuid.values())
        self.cities = cluster_cities(self.stations)
        preferred = next((city.city_id for city in self.cities
                          if self.selected_uuid and self.selected_uuid in city.station_uuids), None)
        self.pins = visible_cities(self.cities, view, selected_city=preferred, limit=PIN_LIMIT)
        self.city_by_id = {city.city_id: city for city in self.pins}
        self._unknown_location = xbmcaddon.Addon().getLocalizedString(30154)
        self.selected_city_id = ''
        if self.selected_uuid:
            self.selected_city_id = next((city.city_id for city in self.pins
                                          if self.selected_uuid in city.station_uuids), '')
        if not self.selected_city_id and self.pins:
            self.selected_city_id = self.pins[0].city_id
        self.execute = execute or xbmc.executebuiltin
        self.preview_enabled = bool(preview_enabled)
        self._preview_start_callback = preview_start
        self._preview_stop_callback = preview_stop
        self.status_message = status_message
        self._player_factory = player_factory or xbmc.Player
        self._timer_factory, self._thread_factory = timer_factory, thread_factory
        self._playback_arbiter = playback_arbiter or PlaybackStartArbiter()
        self._preview = None
        self._rail_position = 0
        super().__init__(xml_filename, script_path, default_skin, default_res, is_media)

    def onInit(self):
        addon = xbmcaddon.Addon()
        self.getControl(PREVIEW_ID).setLabel(xbmcaddon.Addon().getLocalizedString(30145))
        self.getControl(BACK_ID).setLabel(xbmcaddon.Addon().getLocalizedString(30146))
        self.getControl(11).setLabel(xbmcaddon.Addon().getLocalizedString(30149))
        whole_world = self.view.full_longitude and self.view.north == 90 and self.view.south == -90
        self.getControl(13).setLabel(addon.getLocalizedString(30148 if whole_world else 30162))
        self._render_graticule(whole_world)
        self._selected_prefix = xbmcaddon.Addon().getLocalizedString(30150)
        self.getControl(PREVIEW_ID).setVisible(self.preview_enabled)
        if self.status_message:
            self.getControl(STATUS_ID).setLabel(self.status_message)
        elif not self.pins:
            self.getControl(STATUS_ID).setLabel(xbmcaddon.Addon().getLocalizedString(30147))
        self._render_pins()
        self._render_rail()
        if self.selected_city_id:
            pin = self._pin_control(self.selected_city_id)
            if pin is not None:
                self.setFocusId(pin)
        else:
            self.setFocusId(BACK_ID)
        if self._preview is None and self.preview_enabled and self._preview_start_callback:
            self._preview = PreviewController(
                self._preview_start_callback,
                self._can_start_preview,
                self._play_preview,
                lambda: self._player_factory().getPlayingFile(),
                lambda: self._player_factory().isPlaying(),
                lambda: self._player_factory().stop(),
                self._stop_preview_session,
                timer_factory=self._timer_factory, thread_factory=self._thread_factory,
                playback_arbiter=self._playback_arbiter,
            )

    def _render_graticule(self, whole_world):
        """Fit the schematic grid to the map bounds; region names belong to world view."""
        for control_id in range(30, 37):
            self.getControl(control_id).setVisible(whole_world)

        latitude_values = sorted(_grid_values(self.view.south, self.view.north), reverse=True)
        for index in range(5):
            control = self.getControl(20 + index)
            if index >= len(latitude_values):
                control.setVisible(False)
                continue
            latitude = latitude_values[index]
            y = MAP_TOP + round((self.view.north - latitude) * MAP_HEIGHT /
                                (self.view.north - self.view.south))
            control.setPosition(MAP_LEFT, y)
            control.setVisible(True)

        west = -180 if self.view.full_longitude else self.view.west
        longitude_values = _grid_values(west, west + self.view.longitude_span)
        for index in range(5):
            control = self.getControl(25 + index)
            if index >= len(longitude_values):
                control.setVisible(False)
                continue
            longitude = longitude_values[index]
            ratio = ((longitude + 180) / 360 if self.view.full_longitude else
                     (longitude - self.view.west) / self.view.longitude_span)
            x = MAP_LEFT + round(ratio * MAP_WIDTH)
            control.setPosition(x, MAP_TOP)
            control.setVisible(True)

    def _pin_control(self, city_id):
        for index, city in enumerate(self.pins):
            if city.city_id == city_id:
                return PIN_FIRST_ID + index
        return None

    def _render_pins(self):
        for index in range(PIN_LIMIT):
            control = self.getControl(PIN_FIRST_ID + index)
            if index >= len(self.pins):
                control.setVisible(False)
                control.setEnabled(False)
                continue
            city = self.pins[index]
            point = project(city.latitude, city.longitude, self.view)
            x = MAP_LEFT + round(point[0] * MAP_WIDTH / self.view.width)
            y = MAP_TOP + round(point[1] * MAP_HEIGHT / self.view.height)
            selected = city.city_id == self.selected_city_id
            # A full city name on every button becomes unreadable where the
            # directory is dense. Keep compact dots for the map and promote
            # only the focused city to a label; the station rail carries detail.
            label = (city.city or self._unknown_location) if selected else '•'
            left = x - 150
            control.setPosition(min(MAP_LEFT + MAP_WIDTH - 300, max(MAP_LEFT, left)),
                                min(MAP_TOP + MAP_HEIGHT - 42, max(MAP_TOP, y)))
            control.setVisible(True)
            control.setEnabled(True)
            control.setLabel(label)

    def _render_rail(self):
        rail = self.getControl(RAIL_ID)
        rail.reset()
        city = self.city_by_id.get(self.selected_city_id)
        if city is None:
            self.getControl(STATUS_ID).setLabel(
                self.status_message or xbmcaddon.Addon().getLocalizedString(30147))
            return
        count_label = xbmcaddon.Addon().getLocalizedString(
            30166 if city.station_count == 1 else 30151)
        status = '%s · %s · %d %s' % (
            city.city or self._unknown_location, city.country or city.country_code,
            city.station_count, count_label)
        self.getControl(STATUS_ID).setLabel((self.status_message + ' · ' + status)
                                            if self.status_message else status)
        station_uuids = sorted(city.station_uuids, key=lambda uuid: (
            -getattr(self.station_by_uuid.get(uuid), 'click_count', 0),
            getattr(self.station_by_uuid.get(uuid), 'name', '').casefold(), uuid))
        rail_uuids = station_uuids[:RAIL_LIMIT]
        if self.selected_uuid in station_uuids and self.selected_uuid not in rail_uuids:
            rail_uuids = rail_uuids[:RAIL_LIMIT - 1] + [self.selected_uuid]
        for station_uuid in rail_uuids:
            station = self.station_by_uuid.get(station_uuid)
            if station is None:
                continue
            label = getattr(station, 'name', '') or station_uuid
            entry = xbmcgui.ListItem(label=label)
            code = getattr(station, 'codec', '')
            city_label = getattr(station, 'city', '') or getattr(station, 'state', '')
            if not city_label:
                city_label = self._unknown_location
            entry.setLabel2(' · '.join(value for value in (city_label, code) if value))
            # Kodi would fetch remote ListItem art outside UrlPolicy. Keep
            # directory-controlled image URLs away from the native loader.
            artwork = os.path.join(xbmcaddon.Addon().getAddonInfo('path'),
                                   'resources', 'station-monogram.png')
            entry.setArt({'thumb': artwork, 'icon': artwork})
            entry.getMusicInfoTag().setTitle(label)
            entry.setProperty('station_uuid', station_uuid)
            rail.addItems([entry])
        self._rail_position = rail_uuids.index(self.selected_uuid) if self.selected_uuid in rail_uuids else 0
        rail.selectItem(self._rail_position)

    def focus_city(self, city_id, request_focus=True):
        if city_id not in self.city_by_id:
            return False
        if self._preview:
            self._preview.cancel()
        if self.selected_city_id != city_id:
            self.selected_city_id = city_id
            self._render_pins()
            self._render_rail()
        pin = self._pin_control(city_id)
        if request_focus and pin is not None and self.getFocusId() != pin:
            self.setFocusId(pin)
        return True

    def _move_city(self, direction):
        city = nearest_city_in_direction(self.pins, self.selected_city_id, direction, self.view)
        if city is None:
            if direction == 'right':
                target = RAIL_ID
                self.setFocusId(target)
                self._rail_position = self.getControl(RAIL_ID).getSelectedPosition()
            elif direction == 'down':
                target = PREVIEW_ID if self.preview_enabled else BACK_ID
                self.setFocusId(target)
            elif direction in ('up', 'left'):
                self.setFocusId(BACK_ID)
            return False
        self.focus_city(city.city_id)
        return True

    def _focused_station_uuid(self):
        try:
            entry = self.getControl(RAIL_ID).getSelectedItem()
            return station_identifier(entry.getProperty('station_uuid')) if entry else ''
        except (AttributeError, ValueError, TypeError):
            return ''

    def onAction(self, action):
        try:
            action_id = action.getId()
        except (AttributeError, TypeError):
            return
        if action_id in BACK_ACTIONS:
            self.close()
            return
        direction = next((name for name, value in ACTION_IDS.items() if value == action_id), None)
        if not direction:
            return
        focus_id = self.getFocusId()
        if PIN_FIRST_ID <= focus_id < PIN_FIRST_ID + len(self.pins):
            self._move_city(direction)
        elif focus_id == RAIL_ID:
            if direction == 'left':
                self.setFocusId(self._pin_control(self.selected_city_id) or BACK_ID)
                if self._preview:
                    self._preview.cancel()
            elif direction == 'right':
                self.setFocusId(PREVIEW_ID if self.preview_enabled else BACK_ID)
                if self._preview:
                    self._preview.cancel()
            elif direction == 'down':
                rail = self.getControl(RAIL_ID)
                position = rail.getSelectedPosition()
                # Kodi moves the native list before onAction and wraps at its
                # endpoints. Restore the boundary row before leaving the rail.
                if self._rail_position + 1 >= rail.size():
                    rail.selectItem(self._rail_position)
                    self.setFocusId(PREVIEW_ID if self.preview_enabled else BACK_ID)
                    if self._preview:
                        self._preview.cancel()
                elif position != self._rail_position:
                    self._rail_position = position
                    self._preview_station_at(position)
            elif direction == 'up':
                rail = self.getControl(RAIL_ID)
                position = rail.getSelectedPosition()
                if self._rail_position == 0:
                    rail.selectItem(0)
                    self.setFocusId(self._pin_control(self.selected_city_id) or BACK_ID)
                    if self._preview:
                        self._preview.cancel()
                elif position != self._rail_position:
                    self._rail_position = position
                    self._preview_station_at(position)
        elif focus_id in (PREVIEW_ID, BACK_ID):
            if direction == 'up':
                self.setFocusId(RAIL_ID if focus_id == PREVIEW_ID else self._pin_control(self.selected_city_id) or RAIL_ID)
            elif direction == 'left':
                self.setFocusId(self._pin_control(self.selected_city_id) or RAIL_ID)
            elif direction == 'right' and focus_id == PREVIEW_ID:
                self.setFocusId(BACK_ID)

    def onFocus(self, control_id):
        if PIN_FIRST_ID <= control_id < PIN_FIRST_ID + len(self.pins):
            self.focus_city(self.pins[control_id - PIN_FIRST_ID].city_id, request_focus=False)
        elif control_id == RAIL_ID:
            self._rail_position = self.getControl(RAIL_ID).getSelectedPosition()
            if self._preview:
                station_uuid = self._focused_station_uuid()
                if station_uuid:
                    self._preview.focus_station(station_uuid)
        elif control_id != RAIL_ID and self._preview:
            self._preview.cancel()

    def _preview_station_at(self, position):
        if not self._preview:
            return
        try:
            entry = self.getControl(RAIL_ID).getListItem(position)
            station_uuid = station_identifier(entry.getProperty('station_uuid'))
        except (AttributeError, IndexError, ValueError, TypeError):
            return
        self._preview.focus_station(station_uuid)

    def onClick(self, control_id):
        if PIN_FIRST_ID <= control_id < PIN_FIRST_ID + len(self.pins):
            self.focus_city(self.pins[control_id - PIN_FIRST_ID].city_id)
            if self._preview:
                self._preview.cancel()
        elif control_id == RAIL_ID:
            station_uuid = self._focused_station_uuid()
            if station_uuid:
                if self._preview:
                    self._preview.cancel()
                self.execute('RunPlugin(%s)' % (self.plugin_base + '?' + urlencode({'mode': 'play', 'uuid': station_uuid})))
        elif control_id == PREVIEW_ID:
            station_uuid = self._focused_station_uuid()
            if station_uuid and self._preview:
                self._preview.focus_station(station_uuid)
        elif control_id == BACK_ID:
            self.close()

    def _can_start_preview(self, result):
        player = self._player_factory()
        try:
            if not player.isPlaying():
                return True
            current = player.getPlayingFile()
            owned = self._preview.owned if self._preview else None
            return bool(owned and current == owned.get('playback_url'))
        except Exception:
            return False

    def _play_preview(self, result):
        player = self._player_factory()
        entry = xbmcgui.ListItem(label=xbmcaddon.Addon().getLocalizedString(30152), path=result['playback_url'])
        entry.setProperty('IsPlayable', 'true')
        mime = result.get('mime_type', 'application/octet-stream')
        if isinstance(mime, str) and len(mime) <= 128:
            entry.setMimeType(mime)
            entry.setContentLookup(False)

        def can_start():
            try:
                if not player.isPlaying():
                    return True
                current = player.getPlayingFile()
                owned = self._preview.owned if self._preview else None
                return bool(owned and current == owned.get('playback_url'))
            except Exception:
                return False

        def start_preview():
            player.play(result['playback_url'], entry, windowed=True)

        started = self._playback_arbiter.start_if_available(can_start, start_preview)
        if not started:
            raise RuntimeError('Kodi is playing a stream not owned by this map preview')
        result['_playback_intent'] = started

    def _stop_preview_session(self, session_id):
        if self._preview_stop_callback:
            worker = self._thread_factory or threading.Thread
            worker(target=lambda: self._preview_stop_callback(session_id), daemon=True).start()

    def cleanup(self):
        """Release preview ownership even after Kodi has dismissed the window."""
        if self._preview:
            self._preview.close()
            self._preview = None

    def close(self):
        self.cleanup()
        super().close()
