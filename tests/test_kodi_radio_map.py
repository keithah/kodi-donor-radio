"""Kodi boundary tests for the shared station map window."""
import importlib
import sys
import threading
from types import SimpleNamespace

import pytest

from models import Station
from radio_map import MapView


class Action:
    def __init__(self, action_id): self._id = action_id
    def getId(self): return self._id


class Control:
    def __init__(self, control_id):
        self.control_id = control_id
        self.visible, self.position, self.label = True, (0, 0), ''
        self.enabled = True
        self.width, self.height = 300, 42
        self.items, self.selected = [], 0
    def setVisible(self, value): self.visible = value
    def setPosition(self, x, y): self.position = (x, y)
    def setWidth(self, value): self.width = value
    def setLabel(self, value): self.label = value
    def setEnabled(self, value): self.enabled = value
    def reset(self): self.items, self.selected = [], 0
    def addItems(self, values): self.items.extend(values)
    def selectItem(self, value): self.selected = value
    def getSelectedPosition(self): return self.selected
    def getSelectedItem(self): return self.items[self.selected] if self.items else None
    def getListItem(self, position): return self.items[position]
    def size(self): return len(self.items)


class Item:
    def __init__(self, label='', path=''):
        self.label, self.path, self.props = label, path, {}
        self.music_info = MusicInfoTag()
        self.set_info_calls = []
    def getMusicInfoTag(self): return self.music_info
    def setProperty(self, key, value): self.props[key] = value
    def getProperty(self, key): return self.props.get(key, '')
    def setLabel2(self, value): self.label2 = value
    def setArt(self, value): self.art = value
    def setInfo(self, kind, value): self.set_info_calls.append((kind, value))
    def setMimeType(self, value): self.mime = value
    def setContentLookup(self, value): self.lookup = value


class MusicInfoTag:
    def __init__(self): self.title = ''
    def setTitle(self, value): self.title = value


def native_rail_action(window, action_id):
    """Kodi 21.3's default list wraps before delivering WindowXML.onAction."""
    rail = window.controls[2000]
    moved_position = (rail.getSelectedPosition() + (1 if action_id == 4 else -1)) % rail.size()
    rail.selectItem(moved_position)
    window.onAction(Action(action_id))
    return moved_position


class WindowXML:
    def __new__(cls, xml_filename, script_path, default_skin, default_res, is_media):
        assert xml_filename and script_path
        return object.__new__(cls)
    def __init__(self, *args):
        self.xml_args = args
        self.controls = {control_id: Control(control_id) for control_id in range(1000, 1060)}
        self.controls.update({key: Control(key) for key in (
            10, 11, 12, 13, 2000, 3000, 3001, 3002, *range(20, 37))})
        self.focus_id = 1000
        self.closed = False
    def getControl(self, control_id): return self.controls[control_id]
    def getFocusId(self): return self.focus_id
    def setFocusId(self, control_id): self.focus_id = control_id
    def close(self): self.closed = True
    def doModal(self): self.onInit()


def load_window(monkeypatch):
    gui = SimpleNamespace(WindowXML=WindowXML, ListItem=Item,
                          ACTION_MOVE_LEFT=1, ACTION_MOVE_RIGHT=2,
                          ACTION_MOVE_UP=3, ACTION_MOVE_DOWN=4,
                          ACTION_SELECT_ITEM=7, ACTION_PREVIOUS_MENU=10,
                          ACTION_NAV_BACK=92)
    monkeypatch.setitem(sys.modules, 'xbmcgui', gui)
    monkeypatch.setitem(sys.modules, 'xbmc', SimpleNamespace(
        executebuiltin=lambda value: None,
        Player=lambda: SimpleNamespace(isPlaying=lambda: False, getPlayingFile=lambda: '',
                                       play=lambda *args: None, stop=lambda: None)))
    monkeypatch.setitem(sys.modules, 'xbmcaddon', SimpleNamespace(
        Addon=lambda: SimpleNamespace(getLocalizedString=lambda identifier: 'localized-%d' % identifier,
                                      getAddonInfo=lambda key: '/addon')))
    sys.modules.pop('kodi_radio_map', None)
    return importlib.import_module('kodi_radio_map')


def station(uuid, name, latitude, longitude, city='Town', country='Canada', country_code='CA'):
    return Station(uuid, name, 'https://stream.invalid/live', 'MP3', False,
                   city=city, country=country, country_code=country_code,
                   latitude=latitude, longitude=longitude)


def make_window(module, **kwargs):
    defaults = dict(origin_route='plugin://plugin.audio.kodi_donor_radio?mode=home',
                    selected_uuid='', stations=[station('one', 'One', 0, 0),
                                                station('two', 'Two', 0, 0.1),
                                                station('three', 'Three', 0.1, 0)],
                    view=MapView(1, -1, -1, 1, 1000, 650), preview_enabled=False,
                    addon_path='/addon', plugin_base='plugin://plugin.audio.kodi_donor_radio')
    defaults.update(kwargs)
    native = ('map.xml', '/addon', 'Default', '1080i', False)
    return module.RadioMapWindow(*native, **defaults)


def test_window_populates_bounded_pin_pool_and_station_rail(monkeypatch):
    module = load_window(monkeypatch)
    stations = [station('station-%02d' % i, 'Station %02d' % i, 0, 0)
                for i in range(65)]
    window = make_window(module, stations=stations, selected_uuid='station-00')
    window.onInit()
    assert sum(window.controls[1000 + i].visible for i in range(60)) == len(window.pins)
    assert len(window.pins) <= 60
    window.onClick(window._pin_control(window.selected_city_id))
    rail = window.controls[2000]
    assert [item.label for item in rail.items] == ['Station %02d' % i for i in range(50)]
    assert rail.items[0].getProperty('station_uuid') == 'station-00'
    assert rail.items[0].music_info.title == 'Station 00'
    assert rail.items[0].set_info_calls == []
    assert rail.items[0].art == {'thumb': '/addon/resources/station-monogram.png',
                                 'icon': '/addon/resources/station-monogram.png'}
    assert 'Town' in rail.items[0].label2 and 'MP3' in rail.items[0].label2
    assert window.xml_args == ('map.xml', '/addon', 'Default', '1080i', False)


def test_map_keeps_only_the_focused_city_name_on_the_pin_layer(monkeypatch):
    module = load_window(monkeypatch)
    stations = [station('newark', 'Newark Radio', 40.7, -74.2, city='Newark', country='United States', country_code='US'),
                station('lagos', 'Lagos Radio', 6.5, 3.4, city='Lagos', country='Nigeria', country_code='NG')]
    window = make_window(module, stations=stations, selected_uuid='newark',
                         view=MapView(90, -90, -180, 180, 1030, 590))
    window.onInit()
    by_city = {city.city: city for city in window.pins}

    newark_control = window.controls[window._pin_control(by_city['Newark'].city_id)]
    lagos_control = window.controls[window._pin_control(by_city['Lagos'].city_id)]
    assert newark_control.label == 'Newark'
    assert lagos_control.label == '•'

    window.focus_city(by_city['Lagos'].city_id)
    assert lagos_control.label == 'Lagos'
    assert newark_control.label == '•'


def test_map_pin_dot_is_centered_and_edge_label_only_is_clamped(monkeypatch):
    module = load_window(monkeypatch)
    west = station('west', 'West Radio', 0, -180, city='West')
    east = station('east', 'East Radio', 0, 180, city='East')
    window = make_window(module, stations=[west, east], selected_uuid='west',
                         view=MapView(90, -90, -180, 180, 1030, 590))
    window.onInit()

    selected = window.controls[window._pin_control(window.selected_city_id)]
    east_city = next(city for city in window.pins if city.city == 'East')
    unselected = window.controls[window._pin_control(east_city.city_id)]
    assert selected.width == 300
    assert selected.position[0] == module.MAP_LEFT
    assert selected.position[1] == module.MAP_TOP + module.MAP_HEIGHT // 2 - 21
    assert unselected.width == 42
    east_x = module.MAP_LEFT + module.MAP_WIDTH
    assert unselected.position == (east_x - 21,
                                   module.MAP_TOP + module.MAP_HEIGHT // 2 - 21)
    assert unselected.position[0] + unselected.width // 2 == east_x


@pytest.mark.parametrize(('stations', 'expected'), [
    ([station('one', 'One', 0, 0)], 'Town · Canada · 1 station'),
    ([station('one', 'One', 0, 0), station('two', 'Two', 0, 0)],
     'Town · Canada · 2 stations'),
])
def test_map_status_uses_singular_and_plural_station_labels(monkeypatch, stations, expected):
    strings = {30151: 'stations', 30166: 'station'}
    module = load_window(monkeypatch)
    module.xbmcaddon = SimpleNamespace(
        Addon=lambda: SimpleNamespace(
            getLocalizedString=lambda identifier: strings.get(identifier, 'localized-%d' % identifier),
            getAddonInfo=lambda key: '/addon'))
    window = make_window(module, stations=stations)
    window.onInit()
    assert window.controls[3002].label == expected


@pytest.mark.parametrize('base', ['plugin://plugin.audio.kodi_donor_radio',
                                  'plugin://plugin.audio.kodi_donor_radio/'])
def test_validates_and_canonicalizes_both_kodi_plugin_root_forms(monkeypatch, base):
    module = load_window(monkeypatch)
    route = base + '?mode=home'
    window = make_window(module, origin_route=route, plugin_base=base)
    assert window.origin_route == route
    fallback = make_window(module, origin_route=base + 'other?mode=home', plugin_base=base)
    assert fallback.origin_route == route


def test_directional_city_navigation_uses_radio_map_and_does_not_wrap(monkeypatch):
    module = load_window(monkeypatch)
    window = make_window(module)
    window.onInit()
    selected = window.selected_city_id
    window.focus_city(selected)
    window.onAction(Action(2))
    assert window.selected_city_id != selected
    last = window.selected_city_id
    window.onAction(Action(2))
    assert window.selected_city_id == last


def test_queued_kodi_pin_focus_notifications_drain_without_rebuilding_rail(monkeypatch):
    module = load_window(monkeypatch)
    pending = []
    def set_focus(self, control_id):
        self.focus_id = control_id
        pending.append(control_id)  # Kodi also notifies when focus is unchanged.
    monkeypatch.setattr(WindowXML, 'setFocusId', set_focus)
    window = make_window(module)
    window.onInit()
    rows = list(window.controls[2000].items)
    for _ in range(8):
        if not pending:
            break
        window.onFocus(pending.pop(0))
    assert pending == [], 'setFocusId notifications must not request focus again'
    assert window.controls[2000].items == rows
    # A native focus change still selects the new city and renders its rail.
    window.setFocusId(1001)
    window.onFocus(pending.pop(0))
    assert window.selected_city_id == window.pins[1].city_id
    assert window.controls[2000].items[0].getProperty('station_uuid') in window.pins[1].station_uuids
    assert pending == []


@pytest.mark.parametrize('preview_enabled', [False, True])
@pytest.mark.parametrize('direction,positions,exit_id', [
    (4, [1, 2, 0], 3000), (3, [1, 0, 2], 1000),
])
def test_rail_exits_edges_after_native_wrapping_movement(
        monkeypatch, preview_enabled, direction, positions, exit_id):
    module = load_window(monkeypatch)
    window = make_window(module, preview_enabled=preview_enabled, stations=[
        station('one', 'One', 0, 0), station('two', 'Two', 0, 0),
        station('three', 'Three', 0, 0)])
    window.onInit()
    rail = window.controls[2000]
    rail.selectItem(0 if direction == 4 else 2)
    window.setFocusId(2000)
    window.onFocus(2000)
    seen = []
    window._preview = SimpleNamespace(focus_station=seen.append, cancel=lambda: None) if preview_enabled else None
    for position in positions[:2]:
        native_rail_action(window, direction)
        assert rail.getSelectedPosition() == position
        assert window.getFocusId() == 2000
    expected_seen = (['three', 'two'] if direction == 4 else ['three', 'one']) if preview_enabled else []
    assert seen == expected_seen
    assert native_rail_action(window, direction) == positions[2]
    assert rail.getSelectedPosition() == (2 if direction == 4 else 0)
    assert window.getFocusId() == (3001 if exit_id == 3000 and not preview_enabled else exit_id)
    assert seen == expected_seen, 'the wrapped row must never trigger a preview'


@pytest.mark.parametrize('selected_uuid,expected_position', [('station-59', 49), ('station-17', 17)])
def test_context_station_is_reserved_and_selected_in_bounded_rail(monkeypatch, selected_uuid, expected_position):
    module = load_window(monkeypatch)
    stations = [station('station-%02d' % i, 'Station %02d' % i, 0, 0) for i in range(60)]
    window = make_window(module, stations=stations, selected_uuid=selected_uuid)
    window.onInit()
    window.onAction(Action(2))  # Enter the rail from the selected map pin.
    rail = window.controls[2000]
    assert rail.size() == 50
    assert rail.getSelectedPosition() == expected_position
    assert rail.getSelectedItem().getProperty('station_uuid') == selected_uuid
    expected = ['station-%02d' % i for i in range(49)] + ['station-59' if selected_uuid == 'station-59' else 'station-49']
    assert [entry.getProperty('station_uuid') for entry in rail.items] == expected


def test_cached_selected_summary_is_reserved_in_seed_budget_and_rendered(monkeypatch):
    from radio_ui import StationSummary
    module = load_window(monkeypatch)
    cached = StationSummary.from_station(station('cached', 'Cached station', 0, 0))
    stations = [station('station-%03d' % i, 'Station %03d' % i, .5, .5) for i in range(200)]
    window = make_window(module, stations=stations + [cached], selected_uuid='cached')
    window.onInit()
    assert len(window.stations) == 200
    window.setFocusId(2000)
    window.onFocus(2000)
    assert window.controls[2000].getSelectedItem().getProperty('station_uuid') == 'cached'


def test_rail_previews_native_focused_row_and_only_exits_after_edge_is_reached(monkeypatch):
    module = load_window(monkeypatch)
    window = make_window(module, preview_enabled=True, stations=[
        station('one', 'One', 0, 0), station('two', 'Two', 0, 0), station('three', 'Three', 0, 0)])
    window.onInit()
    rail = window.controls[2000]
    assert rail.size() == 3
    window._preview = SimpleNamespace(focus_station=lambda uuid: seen.append(uuid), cancel=lambda: None)
    seen = []
    window._rail_position = 0
    window.setFocusId(2000)
    rail.selectItem(1)  # Kodi changes selection before Python receives onAction.
    second_uuid = rail.items[1].getProperty('station_uuid')
    window.onAction(Action(4))
    assert seen == [second_uuid] and window.getFocusId() == 2000
    rail.selectItem(2)
    last_uuid = rail.items[2].getProperty('station_uuid')
    window.onAction(Action(4))
    assert seen == [second_uuid, last_uuid] and window.getFocusId() == 2000
    window.onAction(Action(4))
    assert window.getFocusId() == module.PREVIEW_ID


def test_station_rail_skin_has_visible_focus_and_metadata_fallback():
    from pathlib import Path
    import xml.etree.ElementTree as ET
    root = Path(__file__).parents[1] / 'plugin.audio.kodi_donor_radio/resources/skins/Default/1080i/map.xml'
    tree = ET.parse(root)
    rail = tree.find(".//control[@id='2000']")
    assert rail is not None
    focused = ET.tostring(rail.find('focusedlayout'), encoding='unicode')
    ordinary = ET.tostring(rail.find('itemlayout'), encoding='unicode')
    assert '703D9BE9' in focused and focused != ordinary
    assert 'ListItem.Label2' in focused and 'ListItem.Art(thumb)' in focused


def test_map_pins_and_actions_have_visible_distinct_focus_styles():
    from pathlib import Path
    import xml.etree.ElementTree as ET
    root = Path(__file__).parents[1] / 'plugin.audio.kodi_donor_radio/resources/skins/Default/1080i/map.xml'
    controls = {int(node.get('id')): node for node in ET.parse(root).findall(".//control[@type='button']")}
    pins = [controls[index] for index in range(1000, 1060)]
    for pin in pins:
        assert pin.findtext('texturefocus', '').endswith('/resources/skins/Default/media/radio-white.png')
        assert pin.findtext('texturenofocus') == pin.findtext('texturefocus')
        assert pin.find('texturenofocus').get('colordiffuse') == '00FFFFFF'
        assert pin.findtext('colordiffuse') == '7044A8D8'
        assert pin.findtext('focusedcolor') != pin.findtext('textcolor')
        assert int(pin.findtext('width')) == 300
    preview, back = controls[3000], controls[3001]
    assert preview.findtext('texturefocus') == back.findtext('texturefocus')
    assert preview.findtext('colordiffuse') != back.findtext('colordiffuse')
    assert preview.findtext('focusedcolor') != back.findtext('focusedcolor')
    title = ET.parse(root).find(".//control[@id='11']/label")
    assert title is not None and title.text == '$LOCALIZE[30149]'


def test_map_schematic_has_geographic_grid_and_region_labels():
    from pathlib import Path
    import xml.etree.ElementTree as ET
    root = Path(__file__).parents[1] / 'plugin.audio.kodi_donor_radio/resources/skins/Default/1080i/map.xml'
    xml = ET.parse(root)
    images = {int(node.get('id')): node for node in xml.findall(".//control[@type='image']")
              if node.get('id') is not None}
    assert set(range(20, 30)) <= set(images)
    horizontal = [images[index] for index in range(20, 25)]
    vertical = [images[index] for index in range(25, 30)]
    assert all(int(node.findtext('width')) == 1030 and int(node.findtext('height')) == 1
               for node in horizontal)
    assert all(int(node.findtext('width')) == 1 and int(node.findtext('height')) == 590
               for node in vertical)
    assert [int(node.findtext('top')) for node in horizontal] == [243, 341, 440, 538, 636]
    assert [int(node.findtext('left')) for node in vertical] == [247, 419, 590, 762, 933]
    labels = {int(node.get('id')): node.findtext('label')
              for node in xml.findall(".//control[@type='label']")
              if node.get('id') is not None}
    assert [labels[index] for index in range(30, 37)] == [
        '$LOCALIZE[%d]' % value for value in range(30155, 30162)]
    assert '$LOCALIZE[30148]' in [node.findtext('label') for node in xml.findall(".//control[@type='label']")]


def test_map_schematic_has_visible_panel_and_grid_contrast():
    import xml.etree.ElementTree as ET
    from pathlib import Path
    root = Path(__file__).parents[1] / 'plugin.audio.kodi_donor_radio/resources/skins/Default/1080i/map.xml'
    xml = ET.parse(root)
    panel = xml.find(".//control[@id='12']/colordiffuse").text
    grid = xml.find(".//control[@id='20']/colordiffuse").text
    assert int(panel[:2], 16) >= 0xF0
    assert int(grid[:2], 16) >= 0xC0


def test_map_texture_paths_resolve_from_window_directory():
    from pathlib import Path
    import xml.etree.ElementTree as ET
    root = Path(__file__).parents[1] / 'plugin.audio.kodi_donor_radio/resources/skins/Default/1080i'
    xml = ET.parse(root / 'map.xml')
    textures = {node.text for node in xml.iter()
                if node.tag in ('texture', 'texturefocus', 'texturenofocus') and node.text}
    for texture in textures:
        if texture.endswith('/resources/skins/Default/media/radio-white.png'):
            assert texture.startswith('special://home/addons/plugin.audio.kodi_donor_radio/')
            assert (root.parent / 'media/radio-white.png').is_file()


def test_map_graticule_follows_local_station_bounds_and_hides_world_regions(monkeypatch):
    module = load_window(monkeypatch)
    view = MapView(1, -1, -1, 1, 1000, 650)
    window = make_window(module, view=view)
    window.onInit()

    horizontal = [window.controls[index] for index in range(20, 25)]
    vertical = [window.controls[index] for index in range(25, 30)]
    assert [control.visible for control in horizontal] == [True, True, True, False, False]
    assert [control.position[1] for control in horizontal[:3]] == [293, 440, 587]
    assert [control.visible for control in vertical] == [True, True, True, False, False]
    assert [control.position[0] for control in vertical[:3]] == [333, 590, 847]
    assert all(not window.controls[index].visible for index in range(30, 37))
    assert window.controls[13].label == 'localized-30162'


def test_world_map_keeps_world_regions_and_expected_graticule(monkeypatch):
    module = load_window(monkeypatch)
    view = MapView(90, -90, -180, 180, 1000, 650)
    window = make_window(module, view=view)
    window.onInit()

    assert all(window.controls[index].visible for index in range(30, 37))
    assert [window.controls[index].position[1] for index in range(20, 25)] == [243, 342, 440, 538, 637]
    assert [window.controls[index].position[0] for index in range(25, 30)] == [247, 418, 590, 762, 933]
    assert window.controls[13].label == 'localized-30148'


def test_reinitializing_window_reuses_the_existing_preview_controller(monkeypatch):
    module = load_window(monkeypatch)
    window = make_window(module, preview_enabled=True,
                         preview_start=lambda station_uuid, cancel: None)
    window.onInit()
    controller = window._preview
    window.onInit()
    assert window._preview is controller


def test_antimeridian_grid_uses_unwrapped_longitude_and_narrow_views_get_grid(monkeypatch):
    module = load_window(monkeypatch)
    across_dateline = make_window(module, view=MapView(2, -2, 170, -170, 1000, 650))
    across_dateline.onInit()
    longitudes = [across_dateline.controls[index] for index in range(25, 30)]
    visible_longitudes = [control.position[0] for control in longitudes if control.visible]
    assert visible_longitudes == [333, 590, 847]

    narrow = make_window(module, view=MapView(.02, -.02, -.02, .02, 1000, 650))
    narrow.onInit()
    assert any(narrow.controls[index].visible for index in range(20, 25))
    assert any(narrow.controls[index].visible for index in range(25, 30))


def test_grid_value_count_skips_dense_candidates_without_materializing_them(monkeypatch):
    module = load_window(monkeypatch)
    assert module._grid_value_count(-90, 90, .001) == 179999
    assert module._grid_values(-90, 90) == [-60, -30, 0, 30, 60]


def test_untrusted_remote_favicon_is_never_passed_to_kodi_art_loader(monkeypatch):
    from dataclasses import replace
    module = load_window(monkeypatch)
    hostile = replace(station('local-art', 'Local Art', 0, 0),
                      favicon='http://127.0.0.1:8080/admin')
    window = make_window(module, stations=[hostile])
    window.onInit()
    window._render_rail()
    art = window.controls[2000].items[0].art
    assert art == {'thumb': '/addon/resources/station-monogram.png',
                   'icon': '/addon/resources/station-monogram.png'}
    assert '127.0.0.1' not in repr(art)


def test_rejected_stations_do_not_create_or_inflate_map_pins(monkeypatch):
    from dataclasses import replace
    module = load_window(monkeypatch)
    accepted = station('accepted', 'Accepted', 0, 0)
    unsafe = replace(station('fixture-hidden', 'Fixture hidden', 0, 0), city='Other town')
    window = make_window(module, stations=[unsafe, accepted])
    window.onInit()
    assert window.stations == [accepted]
    assert len(window.pins) == 1
    assert window.pins[0].station_uuids == ('accepted',)
    assert window.pins[0].station_count == 1


def test_stations_without_valid_coordinates_are_ignored_by_map(monkeypatch):
    from dataclasses import replace
    module = load_window(monkeypatch)
    located = station('located', 'Located', 10, 20)
    no_coordinates = replace(station('no-coordinates', 'No coordinates', None, None),
                             city='Unknown', country='')
    invalid_latitude = replace(station('invalid-latitude', 'Invalid latitude', 91, 20),
                               city='Unknown', country='')
    invalid_longitude = replace(station('invalid-longitude', 'Invalid longitude', 10, 181),
                                city='Unknown', country='')
    window = make_window(module, stations=[no_coordinates, invalid_latitude,
                                           invalid_longitude, located],
                         view=MapView(90, -90, -180, 180, 1030, 590))
    window.onInit()
    assert window.stations == [located]
    assert [pin.station_uuids for pin in window.pins] == [('located',)]
    assert [item.getProperty('station_uuid') for item in window.controls[2000].items] == ['located']


def test_station_rail_play_uses_uuid_route_and_never_reports_click(monkeypatch):
    module = load_window(monkeypatch)
    calls = []
    stations = [station(uuid, label, 0, 0) for uuid, label in
                (('one', 'One'), ('two', 'Two'), ('three', 'Three'))]
    window = make_window(module, stations=stations, selected_uuid='one',
                         execute=lambda value: calls.append(value))
    window.onInit()
    window.onClick(window._pin_control(window.selected_city_id))
    window.controls[2000].selectItem(1)
    window.onClick(2000)
    assert calls == ['RunPlugin(plugin://plugin.audio.kodi_donor_radio?mode=play&uuid=three)']


def test_station_rail_keeps_dpad_navigation_inside_rows_until_edge(monkeypatch):
    module = load_window(monkeypatch)
    stations = [station(uuid, label, 0, 0) for uuid, label in
                (('one', 'One'), ('two', 'Two'), ('three', 'Three'))]
    window = make_window(module, stations=stations, selected_uuid='one', preview_enabled=True)
    window.onInit()
    window.setFocusId(2000)
    window.controls[2000].selectItem(1)
    window.onAction(Action(4))
    assert window.getFocusId() == 2000
    window.controls[2000].selectItem(2)
    window.onAction(Action(4))
    assert window.getFocusId() == 2000
    window.onAction(Action(4))
    assert window.getFocusId() == 3000


def test_window_rejects_invalid_selected_uuid(monkeypatch):
    module = load_window(monkeypatch)
    with pytest.raises(ValueError):
        make_window(module, selected_uuid='https://stream.invalid/live')


def test_window_discards_untrusted_optional_origin_route(monkeypatch):
    module = load_window(monkeypatch)
    window = make_window(module, origin_route='plugin://plugin.audio.kodi_donor_radio?url=https://stream.invalid')
    assert window.origin_route == 'plugin://plugin.audio.kodi_donor_radio?mode=home'


@pytest.mark.parametrize('name', ['https://radio.example/live', 'a' * 300, '東京' * 80])
def test_unusable_optional_search_origin_falls_back_to_home(monkeypatch, name):
    from urllib.parse import urlencode
    module = load_window(monkeypatch)
    window = make_window(module, origin_route='plugin://plugin.audio.kodi_donor_radio?' +
                         urlencode({'mode': 'search', 'name': name}))
    window.onInit()
    assert window.origin_route == 'plugin://plugin.audio.kodi_donor_radio?mode=home'
    assert window.getFocusId() == 1000


@pytest.mark.parametrize('base', ['https://plugin.audio.kodi_donor_radio',
    'plugin://evil.example', 'plugin://plugin.audio.kodi_donor_radio/path',
    'plugin://plugin.audio.kodi_donor_radio?mode=home',
    'plugin://user@plugin.audio.kodi_donor_radio'])
def test_optional_origin_fallback_never_accepts_unsafe_plugin_root(monkeypatch, base):
    module = load_window(monkeypatch)
    with pytest.raises(ValueError):
        make_window(module, origin_route='plugin://plugin.audio.kodi_donor_radio?mode=search&name=' + 'a' * 300,
                    plugin_base=base)


def test_empty_map_has_localized_status_and_hides_disabled_preview(monkeypatch):
    module = load_window(monkeypatch)
    window = make_window(module, stations=[], preview_enabled=False)
    window.onInit()
    assert window.controls[3002].label == 'localized-30147'
    assert not window.controls[3000].visible
    assert window.getFocusId() == 3001


def test_preview_waits_for_stable_focus_and_cancels_stale_result(monkeypatch):
    module = load_window(monkeypatch)
    timers, workers, started, stopped = [], [], [], []
    class Timer:
        def __init__(self, delay, callback): self.delay, self.callback, self.cancelled = delay, callback, False
        def start(self): timers.append(self)
        def cancel(self): self.cancelled = True
        def fire(self):
            if not self.cancelled: self.callback()
    def thread(target, daemon=True):
        worker = SimpleNamespace(target=target, start=lambda: workers.append(worker))
        return worker
    requests = []
    def start_preview(uuid, cancel):
        requests.append((uuid, cancel))
        if uuid == 'one':
            return {'session_id': 'late-one', 'playback_url': 'http://127.0.0.1:1/media/one'}
        return {'session_id': 'owned-two', 'playback_url': 'http://127.0.0.1:1/media/two'}
    window = make_window(module, selected_uuid='one', preview_enabled=True, timer_factory=Timer,
                         thread_factory=thread, preview_start=start_preview,
                         preview_stop=lambda session: stopped.append(session),
                         player_factory=lambda: SimpleNamespace(
                             isPlaying=lambda: not started or started[-1] is not None,
                             getPlayingFile=lambda: started[-1][1] if started and started[-1] else '',
                                 play=lambda url, item, windowed=False: started.append(('play', url, windowed)),
                             stop=lambda: started.append(None)))
    window.onInit()
    window.onFocus(2000)
    assert timers[-1].delay == 0.4
    timers[-1].fire()
    assert len(workers) == 1
    # A newer focus generation invalidates the late result before its worker runs.
    window._preview.focus_station('two')
    workers[0].target()
    assert requests[0][1].is_set()
    workers[-1].target()  # Session cleanup is also kept off the Kodi UI thread.
    assert not any(entry[0] == 'play' for entry in started if entry)
    assert stopped


def test_preview_starts_after_debounce_and_closes_only_its_session(monkeypatch):
    module = load_window(monkeypatch)
    timers, workers, plays, stops = [], [], [], []
    class Timer:
        def __init__(self, delay, callback): self.delay, self.callback, self.cancelled = delay, callback, False
        def start(self): timers.append(self)
        def cancel(self): self.cancelled = True
        def fire(self):
            if not self.cancelled: self.callback()
    def thread(target, daemon=True):
        worker = SimpleNamespace(target=target, start=lambda: workers.append(worker))
        return worker
    player_state = {'url': '', 'playing': False}
    player = SimpleNamespace(
        isPlaying=lambda: player_state['playing'], getPlayingFile=lambda: player_state['url'],
        play=lambda url, item, windowed=False: (plays.append((url, windowed)), player_state.update(url=url, playing=True)),
        stop=lambda: player_state.update(url='', playing=False))
    window = make_window(
        module, selected_uuid='one', preview_enabled=True, timer_factory=Timer,
        thread_factory=thread, preview_start=lambda uuid, cancel: {
            'session_id': 'owned-preview', 'playback_url': 'http://127.0.0.1:1234/media/preview'},
        preview_stop=lambda session: stops.append(session), player_factory=lambda: player)
    window.onInit()
    window.onFocus(2000)
    assert timers[-1].delay == 0.4 and not plays
    timers[-1].fire()
    assert not plays
    workers[0].target()
    assert plays == [('http://127.0.0.1:1234/media/preview', True)]
    window.close()
    workers[-1].target()
    assert stops == ['owned-preview'] and not player_state['playing']


@pytest.mark.parametrize('lifecycle_method', ['cancel', 'close'])
def test_cancel_or_close_serializes_with_preview_play_handoff(monkeypatch, lifecycle_method):
    module = load_window(monkeypatch)
    entered_play, release_play = threading.Event(), threading.Event()
    close_started, close_done = threading.Event(), threading.Event()
    player_state = {'url': '', 'playing': False}

    def play(result):
        entered_play.set()
        assert release_play.wait(2)
        player_state.update(url=result['playback_url'], playing=True)

    controller = module.PreviewController(
        lambda *_: {'session_id': 'late-preview', 'playback_url': 'http://127.0.0.1:1/preview'},
        lambda _: True, play, lambda: player_state['url'], lambda: player_state['playing'],
        lambda: player_state.update(url='', playing=False), lambda _: None)
    cancel = threading.Event()
    resolving = threading.Thread(target=controller._resolve, args=(0, 'one', cancel))
    resolving.start()
    assert entered_play.wait(2)

    def close():
        close_started.set()
        getattr(controller, lifecycle_method)()
        close_done.set()

    closing = threading.Thread(target=close)
    closing.start()
    assert close_started.wait(2)
    closing.join(0.05)
    assert not close_done.is_set(), '%s must wait for an in-flight Player.play handoff' % lifecycle_method
    release_play.set()
    resolving.join(2)
    closing.join(2)
    assert not resolving.is_alive() and not closing.is_alive()
    assert close_done.is_set()
    assert player_state == {'url': '', 'playing': False}


def test_close_stops_only_preview_owned_by_window(monkeypatch):
    module = load_window(monkeypatch)
    stopped, played = [], []
    player = SimpleNamespace(isPlaying=lambda: True,
                              getPlayingFile=lambda: 'http://127.0.0.1:1/media/preview',
                              stop=lambda: played.append('stop'))
    window = make_window(module, preview_enabled=False,
                         preview_stop=lambda session: stopped.append(session),
                         player_factory=lambda: player)
    window._preview = module.PreviewController(
        lambda *_: None, lambda _: True, lambda _: None, player.getPlayingFile,
        player.isPlaying, player.stop, lambda session: stopped.append(session))
    window._preview.owned = {'session_id': 'owned', 'playback_url': 'http://127.0.0.1:1/media/preview'}
    window.close()
    assert played == ['stop'] and stopped == ['owned']


@pytest.mark.parametrize('lifecycle_method', ['cancel', 'close'])
def test_cancel_or_close_retires_owned_session_when_intent_lock_fails(monkeypatch, lifecycle_method):
    module = load_window(monkeypatch)
    events = []
    player = SimpleNamespace(isPlaying=lambda: True,
                              getPlayingFile=lambda: 'http://127.0.0.1:1/preview',
                              stop=lambda: events.append('stop-player'))
    arbiter = module.PlaybackStartArbiter()
    arbiter.is_current_intent = lambda _token: (_ for _ in ()).throw(OSError('lock failed'))
    window = make_window(module, preview_enabled=False, player_factory=lambda: player,
                         playback_arbiter=arbiter)
    controller = module.PreviewController(
        lambda *_: None, lambda _: True, lambda _: None, player.getPlayingFile,
        player.isPlaying, player.stop,
        lambda session: events.append(('stop-session', session)),
        playback_arbiter=arbiter)
    controller.owned = {'session_id': 'owned', 'playback_url': player.getPlayingFile(),
                        '_playback_intent': 'unknown-intent'}
    window._preview = controller

    getattr(controller, lifecycle_method)()

    assert events == [('stop-session', 'owned')]


def test_deferred_stop_failure_retires_session_after_exact_url_ack(monkeypatch):
    module = load_window(monkeypatch)
    events, workers = [], []
    player = SimpleNamespace(isPlaying=lambda: True,
                              getPlayingFile=lambda: 'http://127.0.0.1:1/preview',
                              stop=lambda: events.append('stop-player'))
    arbiter = module.PlaybackStartArbiter()
    arbiter.stop_if_current = lambda *_: (_ for _ in ()).throw(OSError('lock failed'))
    def thread(target, daemon=True):
        worker = SimpleNamespace(target=target, start=lambda: workers.append(worker))
        return worker
    controller = module.PreviewController(
        lambda *_: None, lambda _: True, lambda _: None, player.getPlayingFile,
        player.isPlaying, player.stop,
        lambda session: events.append(('stop-session', session)),
        thread_factory=thread, playback_arbiter=arbiter)
    controller.owned = {'session_id': 'acknowledged', 'playback_url': player.getPlayingFile(),
                        '_playback_intent': None}
    controller._pending_playback_url = controller.owned['playback_url']
    controller.cancel()
    assert len(workers) == 1 and events == []
    workers[0].target()
    assert events == [('stop-session', 'acknowledged')]


def test_pending_worker_retires_session_when_intent_lock_fails(monkeypatch):
    module = load_window(monkeypatch)
    events, workers = [], []
    player = SimpleNamespace(isPlaying=lambda: True,
                              getPlayingFile=lambda: 'https://old.example/live',
                              stop=lambda: events.append('stop-player'))
    arbiter = module.PlaybackStartArbiter()
    checks = iter([True, OSError('lock failed')])

    def current_intent(_token):
        result = next(checks)
        if isinstance(result, Exception):
            raise result
        return result

    arbiter.is_current_intent = current_intent
    arbiter.stop_if_current = lambda *_: (_ for _ in ()).throw(OSError('lock failed'))

    def thread(target, daemon=True):
        worker = SimpleNamespace(target=target, start=lambda: workers.append(worker))
        return worker

    controller = module.PreviewController(
        lambda *_: None, lambda _: True, lambda _: None, player.getPlayingFile,
        player.isPlaying, player.stop,
        lambda session: events.append(('stop-session', session)),
        thread_factory=thread, playback_arbiter=arbiter)
    controller.owned = {'session_id': 'pending',
                        'playback_url': 'http://127.0.0.1:1/preview',
                        '_playback_intent': 'preview-intent'}
    controller._pending_playback_url = controller.owned['playback_url']

    controller.close()
    assert len(workers) == 1 and events == []
    workers[0].target()

    assert events == [('stop-session', 'pending')]
    assert player.getPlayingFile() == 'https://old.example/live'


def test_window_does_not_interrupt_separately_selected_playback(monkeypatch):
    module = load_window(monkeypatch)
    calls = []
    player = SimpleNamespace(isPlaying=lambda: True,
                              getPlayingFile=lambda: 'https://external.example/live',
                              stop=lambda: calls.append('stop'))
    window = make_window(module, player_factory=lambda: player)
    window._preview = module.PreviewController(
        lambda *_: None, window._can_start_preview, lambda _: calls.append('play'),
        player.getPlayingFile, player.isPlaying, player.stop,
        lambda session: calls.append(('session-stop', session)))
    assert not window._can_start_preview({'session_id': 'candidate'})
    assert calls == []


def test_preview_rechecks_kodi_state_under_shared_start_arbiter(monkeypatch, tmp_path):
    module = load_window(monkeypatch)
    calls = []
    state = {'url': '', 'playing': False}
    player = SimpleNamespace(
        isPlaying=lambda: state['playing'], getPlayingFile=lambda: state['url'],
        play=lambda url, item, windowed=False: (calls.append((url, windowed)), state.update(url=url, playing=True)))
    window = make_window(module, player_factory=lambda: player,
                         playback_arbiter=module.PlaybackStartArbiter(tmp_path / 'play.lock'))
    result = {'session_id': 'preview', 'playback_url': 'http://127.0.0.1:1/preview'}
    window._preview = SimpleNamespace(owned=result)
    assert window._can_start_preview(result)
    # Kodi or another addon can change playback after the earlier preflight.
    state.update(url='https://external.example/live', playing=True)
    with pytest.raises(RuntimeError, match='not owned by this map preview'):
        window._play_preview(result)
    assert calls == []
    assert state['url'] == 'https://external.example/live'


def test_preview_keeps_session_when_player_play_is_still_pending(monkeypatch, tmp_path):
    module = load_window(monkeypatch)
    calls, stops, workers = [], [], []
    state = {'url': 'https://previous.example/live', 'playing': False}

    def play(url, item, windowed=False):
        calls.append((url, windowed))
        # Kodi accepted the request but has not switched getPlayingFile yet.
        state['playing'] = True

    player = SimpleNamespace(isPlaying=lambda: state['playing'],
                              getPlayingFile=lambda: state['url'], play=play,
                              stop=lambda: (stops.append('stop-playback'),
                                            state.update(url='', playing=False)))

    def thread(target, daemon=True):
        worker = SimpleNamespace(target=target, start=lambda: workers.append(worker))
        return worker

    arbiter = module.PlaybackStartArbiter(tmp_path / 'play.lock')
    window = make_window(module, player_factory=lambda: player,
                         playback_arbiter=arbiter,
                         thread_factory=thread, preview_stop=lambda session: stops.append(session))
    result = {'session_id': 'preview-session',
              'playback_url': 'http://127.0.0.1:1/preview'}
    controller = module.PreviewController(
        lambda *_: result, lambda _: True, window._play_preview,
        player.getPlayingFile, player.isPlaying, player.stop,
        lambda session: stops.append(session), thread_factory=thread,
        playback_arbiter=arbiter)
    window._preview = controller
    controller._resolve(0, 'station', threading.Event())
    assert calls == [(result['playback_url'], True)]
    assert state['url'] == 'https://previous.example/live'
    assert controller.owned is result
    assert stops == []
    window.close()
    assert stops == [] and len(workers) == 1
    state.update(url=result['playback_url'], playing=True)
    workers[0].target()
    assert stops == ['stop-playback', 'preview-session']


def test_pending_preview_survives_other_url_until_preview_is_acknowledged(monkeypatch, tmp_path):
    module = load_window(monkeypatch)
    workers, stops = [], []
    state = {'url': 'https://old.example/live', 'playing': False}
    sleep_entered, release_sleep = threading.Event(), threading.Event()
    player = SimpleNamespace(
        isPlaying=lambda: state['playing'], getPlayingFile=lambda: state['url'],
        play=lambda *_, **__: None,
        stop=lambda: (stops.append('stop-playback'), state.update(playing=False, url='')))

    def thread(target, daemon=True):
        worker = SimpleNamespace(target=target, start=lambda: workers.append(worker))
        return worker

    def injected_sleep(_seconds):
        sleep_entered.set()
        assert release_sleep.wait(2)

    arbiter = module.PlaybackStartArbiter(tmp_path / 'play.lock')
    window = make_window(module, player_factory=lambda: player,
                         playback_arbiter=arbiter)
    result = {'session_id': 'pending-preview',
              'playback_url': 'http://127.0.0.1:1/pending-preview'}
    controller = module.PreviewController(
        lambda *_: result, lambda _: True, window._play_preview,
        player.getPlayingFile, player.isPlaying, player.stop,
        lambda session: stops.append(session), thread_factory=thread,
        clock=lambda: 0.0, sleep=injected_sleep, playback_arbiter=arbiter)
    window._preview = controller
    controller._resolve(0, 'station', threading.Event())
    assert state['url'] == 'https://old.example/live'

    # A different Kodi action wins temporarily while this preview is still queued.
    state.update(url='https://external.example/live', playing=True)
    controller.close()
    watcher = threading.Thread(target=workers[0].target)
    watcher.start()
    assert sleep_entered.wait(2)
    assert stops == []

    # Kodi later acknowledges the previously queued preview; cleanup owns only it.
    state.update(url=result['playback_url'], playing=True)
    release_sleep.set()
    watcher.join(2)
    assert not watcher.is_alive()
    assert stops == ['stop-playback', 'pending-preview']


def test_pending_preview_timeout_retires_session_without_stopping_other_url(monkeypatch, tmp_path):
    module = load_window(monkeypatch)
    workers, stops = [], []
    clock = [0.0]
    state = {'url': 'https://external.example/live', 'playing': True}
    player = SimpleNamespace(isPlaying=lambda: state['playing'],
                              getPlayingFile=lambda: state['url'], play=lambda *_: None,
                              stop=lambda: stops.append('stop-playback'))

    def thread(target, daemon=True):
        worker = SimpleNamespace(target=target, start=lambda: workers.append(worker))
        return worker

    window = make_window(module, player_factory=lambda: player,
                         playback_arbiter=module.PlaybackStartArbiter(tmp_path / 'play.lock'))
    result = {'session_id': 'timed-out-preview',
              'playback_url': 'http://127.0.0.1:1/timed-out-preview'}
    controller = module.PreviewController(
        lambda *_: result, lambda _: True, lambda _: None,
        player.getPlayingFile, player.isPlaying, player.stop,
        lambda session: stops.append(session), thread_factory=thread,
        pending_start_timeout=0.1, clock=lambda: clock[0],
        sleep=lambda duration: clock.__setitem__(0, clock[0] + duration))
    controller.owned = result
    controller._pending_playback_url = result['playback_url']
    controller.close()
    workers[0].target()
    assert clock[0] >= 0.1
    assert stops == ['timed-out-preview']
    assert state['url'] == 'https://external.example/live'


def test_newer_queued_direct_play_prevents_late_preview_stop(monkeypatch, tmp_path):
    module = load_window(monkeypatch)
    lock_path = tmp_path / 'play.lock'
    preview_arbiter = module.PlaybackStartArbiter(lock_path)
    direct_arbiter = module.PlaybackStartArbiter(lock_path)
    workers, events = [], []
    state = {'url': 'https://old.example/live', 'playing': False}
    player = SimpleNamespace(
        isPlaying=lambda: state['playing'], getPlayingFile=lambda: state['url'],
        play=lambda *_, **__: None,
        stop=lambda: (events.append('stop-preview'), state.update(playing=False, url='')))

    def thread(target, daemon=True):
        worker = SimpleNamespace(target=target, start=lambda: workers.append(worker))
        return worker

    window = make_window(module, player_factory=lambda: player,
                         playback_arbiter=preview_arbiter)
    result = {'session_id': 'queued-preview',
              'playback_url': 'http://127.0.0.1:1/queued-preview'}
    controller = module.PreviewController(
        lambda *_: result, lambda _: True, window._play_preview,
        player.getPlayingFile, player.isPlaying, player.stop,
        lambda session: events.append(('stop-session', session)),
        thread_factory=thread, playback_arbiter=preview_arbiter)
    window._preview = controller
    controller._resolve(0, 'station', threading.Event())
    preview_intent = result['_playback_intent']

    # Closing defers cleanup because Kodi has not acknowledged the preview yet.
    controller.close()
    assert len(workers) == 1 and events == []
    direct_intent = direct_arbiter.start(lambda: events.append('queue-direct-play'))
    assert direct_intent != preview_intent

    # The old queued preview is acknowledged after the newer direct request.
    state.update(url=result['playback_url'], playing=True)
    workers[0].target()
    assert events == ['queue-direct-play', ('stop-session', 'queued-preview')]
    assert state['url'] == result['playback_url']


def test_station_rail_cancels_preview_before_queuing_direct_play(monkeypatch):
    module = load_window(monkeypatch)
    events = []
    state = {'url': 'http://127.0.0.1:1/preview', 'playing': True}
    player = SimpleNamespace(isPlaying=lambda: state['playing'],
                              getPlayingFile=lambda: state['url'],
                              stop=lambda: (events.append('stop-preview'),
                                            state.update(url='', playing=False)))
    window = make_window(module, player_factory=lambda: player,
                         execute=lambda command: events.append(('run-plugin', command)))
    window.onInit()
    result = {'session_id': 'owned-preview', 'playback_url': state['url']}
    window._preview = module.PreviewController(
        lambda *_: None, lambda _: True, lambda _: None,
        player.getPlayingFile, player.isPlaying, player.stop,
        lambda session: events.append(('stop-session', session)))
    window._preview.owned = result
    window.onClick(module.RAIL_ID)
    assert events[0] == 'stop-preview'
    assert events[1] == ('stop-session', 'owned-preview')
    assert events[2][0] == 'run-plugin'
