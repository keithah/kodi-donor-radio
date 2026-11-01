import importlib.util
from pathlib import Path
import tempfile
import unittest

spec = importlib.util.spec_from_file_location('owned_lab_launcher', Path(__file__).with_name('default.py'))
launcher = importlib.util.module_from_spec(spec)
spec.loader.exec_module(launcher)

class ManifestTests(unittest.TestCase):
    def validate(self, properties='terminal_eof|is_realtime_stream', dependency=True):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'addon.xml'
            dependency_xml='<import addon="kodi.binary.global.audioengine" version="1.1.1"/>' if dependency else ''
            path.write_text('<addon id="inputstream.ffmpegdirect"><requires>'+dependency_xml+
                            '</requires><extension point="kodi.inputstream" listitemprops="'+properties+'"/></addon>')
            launcher.validate_lab_manifest(path)
    def test_owned_properties_required_without_affecting_diagnostic(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'addon.xml'
            prefix='<addon id="inputstream.ffmpegdirect"><requires><import addon="kodi.binary.global.audioengine"/></requires><extension point="kodi.inputstream" listitemprops="'
            suffix='"/></addon>'
            path.write_text(prefix+'terminal_eof|is_realtime_stream'+suffix)
            launcher.validate_lab_manifest(path)
            with self.assertRaises(RuntimeError):launcher.validate_lab_manifest(path,owned=True)
            path.write_text(prefix+'terminal_eof|is_realtime_stream|owned_clock_nonce|owned_clock_timeline|owned_clock_first_packet'+suffix)
            launcher.validate_lab_manifest(path,owned=True)

    def test_video_control_queue_property_required(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'addon.xml'
            base='<addon id="inputstream.ffmpegdirect"><requires><import addon="kodi.binary.global.audioengine"/></requires><extension point="kodi.inputstream" listitemprops="terminal_eof|is_realtime_stream|owned_player_mode|owned_player_core'
            path.write_text(base+'"/></addon>')
            with self.assertRaises(RuntimeError):launcher.validate_lab_manifest(path,video=True,video_control=True)
            path.write_text(base+'|owned_player_queue_tenths"/></addon>')
            launcher.validate_lab_manifest(path,video=True,video_control=True)

    def test_present_passes(self):
        self.validate()
    def test_missing_each_required_property_fails(self):
        for properties in ('is_realtime_stream','terminal_eof','prefix_terminal_eof|is_realtime_stream'):
            with self.subTest(properties=properties), self.assertRaises(RuntimeError):
                self.validate(properties)

    def test_missing_owned_properties_are_named_in_preflight_error(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'addon.xml'
            path.write_text('<addon id="inputstream.ffmpegdirect"><requires><import addon="kodi.binary.global.audioengine"/></requires><extension point="kodi.inputstream" listitemprops="terminal_eof|is_realtime_stream"/></addon>')
            with self.assertRaisesRegex(RuntimeError, 'owned_clock_first_packet.*owned_clock_nonce.*owned_clock_timeline'):
                launcher.validate_lab_manifest(path,owned=True)
    def test_wrong_instance_audioengine_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'addon.xml'
            path.write_text('<addon id="inputstream.ffmpegdirect"><requires>'
                            '<import addon="kodi.binary.instance.audioengine" version="1.1.1"/>'
                            '</requires><extension point="kodi.inputstream" '
                            'listitemprops="terminal_eof|is_realtime_stream"/></addon>')
            with self.assertRaises(RuntimeError): launcher.validate_lab_manifest(path)
    def test_missing_audioengine_fails(self):
        with self.assertRaises(RuntimeError): self.validate(dependency=False)
    def test_unreadable_and_malformed_fail(self):
        with self.assertRaises(RuntimeError): launcher.validate_lab_manifest('/nonexistent/lab/addon.xml')
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'addon.xml'; path.write_text('<addon')
            with self.assertRaises(RuntimeError): launcher.validate_lab_manifest(path)

def test_hal_capabilities_required_before_native_open(tmp_path):
    path=tmp_path/'addon.xml'
    prefix='<addon id="inputstream.ffmpegdirect"><requires><import addon="kodi.binary.global.audioengine"/></requires><extension point="kodi.inputstream" listitemprops="terminal_eof|is_realtime_stream'
    path.write_text(prefix+'"/></addon>')
    with __import__('pytest').raises(RuntimeError):launcher.validate_lab_manifest(path,hal=True)
    path.write_text(prefix+'|owned_output_backend|owned_hal_contract_version"/></addon>')
    with __import__('pytest').raises(RuntimeError):launcher.validate_lab_manifest(path,hal=True)
    path.write_text(prefix+'|owned_output_backend|owned_hal_contract_version|owned_hal_phase|owned_hal_clock_version"/></addon>')
    launcher.validate_lab_manifest(path,hal=True)


def test_installed_addon_path_uses_current_vfs_translation_api():
    class Addon:
        def __init__(self, addon_id):
            assert addon_id == 'inputstream.ffmpegdirect'
        def getAddonInfo(self, name):
            assert name == 'path'
            return 'special://home/addons/inputstream.ffmpegdirect'
    AddonFactory = type('AddonFactory', (), {'Addon': Addon})
    class Vfs:
        @staticmethod
        def translatePath(path):
            assert path == 'special://home/addons/inputstream.ffmpegdirect'
            return '/profile/addons/inputstream.ffmpegdirect'
    assert str(launcher.installed_addon_path(Vfs, AddonFactory)) == '/profile/addons/inputstream.ffmpegdirect'


if __name__ == '__main__':
    import pytest
    raise SystemExit(pytest.main([__file__]))
