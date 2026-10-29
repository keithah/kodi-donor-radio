import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

HERE=Path(__file__).resolve().parent
sys.path.insert(0,str(HERE))
import fixture_beacons as fixture


class HalActivationTests(unittest.TestCase):
    def load_hal_clock(self):
        with mock.patch.object(fixture.sys,'platform','darwin'):
            return fixture.load_hal_clock(self.library,self.installed_addon)

    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        self.library=self.root/'library';self.library.mkdir()
        freeze=json.loads((HERE/'hal-clock-v1/freeze.json').read_text())
        for name,key in (('owned_hal_clock.py','provider_sha256'),('owned_hal_authorization.py','authorization_provider_sha256')):
            # Use known bytes from the repository rather than inventing a digest.
            source=HERE.parents[2]/'plugin.audio.kodi_donor_radio/resources/lib'/name;target=self.library/name
            target.write_bytes(source.read_bytes())
            self.assertEqual(fixture.file_sha(target),freeze[key])
        self.installed_addon=self.root/'inputstream.ffmpegdirect';self.installed_addon.mkdir()
        self.dylib=self.installed_addon/'inputstream.ffmpegdirect.22.2.10.dylib';self.dylib.write_bytes(b'native-candidate')
        self.addon=self.installed_addon/'addon.xml'
        self.addon.write_text('<addon><extension point="kodi.inputstream" library_osx="inputstream.ffmpegdirect.22.2.10.dylib"/></addon>')
        self.manifest=self.root/'native-manifest.json'
        self.freeze=freeze
        self.write_manifest()
        self.previous=os.environ.get('KODI_DONOR_HAL_NATIVE_MANIFEST')
        os.environ['KODI_DONOR_HAL_NATIVE_MANIFEST']=str(self.manifest)

    def tearDown(self):
        if self.previous is None:os.environ.pop('KODI_DONOR_HAL_NATIVE_MANIFEST',None)
        else:os.environ['KODI_DONOR_HAL_NATIVE_MANIFEST']=self.previous
        self.temp.cleanup()

    def write_manifest(self):
        native={'src/OwnedHalClock.h':self.freeze['native_clock_sha256'],
                'src/OwnedHalAuthorization.h':self.freeze['native_authorization_sha256'],
                'src/OwnedClockTransport.h':self.freeze['native_transport_sha256']}
        value={'schema':'owned-hal-native-manifest-v1','native_source_sha256':native,
               'dylib':{'path':str(self.dylib),'sha256':fixture.file_sha(self.dylib)},
               'addon_manifest':{'path':str(self.addon),'sha256':fixture.file_sha(self.addon)}}
        self.manifest.write_text(json.dumps(value));self.manifest.chmod(0o600)

    def test_exact_installed_native_candidate_is_required(self):
        self.assertEqual(self.load_hal_clock(),self.freeze)

    def test_source_digest_or_installed_artifact_mismatch_fails_closed(self):
        for mutation in ('clock','authorization','transport','dylib','addon'):
            self.write_manifest()
            value=json.loads(self.manifest.read_text())
            if mutation in ('clock','authorization','transport'):
                value['native_source_sha256'][{'clock':'src/OwnedHalClock.h','authorization':'src/OwnedHalAuthorization.h','transport':'src/OwnedClockTransport.h'}[mutation]]='0'*64
                self.manifest.write_text(json.dumps(value));self.manifest.chmod(0o600)
            elif mutation=='dylib':self.dylib.write_bytes(b'swapped')
            else:self.addon.write_bytes(b'<other/>')
            with self.subTest(mutation=mutation),self.assertRaises(ValueError):self.load_hal_clock()

    def test_manifest_cannot_attest_a_different_installed_binary(self):
        external=self.root/'archived-inputstream.dylib';external.write_bytes(b'native-candidate')
        value=json.loads(self.manifest.read_text())
        value['dylib']={'path':str(external),'sha256':fixture.file_sha(external)}
        self.manifest.write_text(json.dumps(value));self.manifest.chmod(0o600)

        with self.assertRaisesRegex(ValueError,'hal_native_artifact_not_installed'):
            self.load_hal_clock()

    def test_untrusted_manifest_permissions_fail_closed(self):
        self.manifest.chmod(0o666)
        with self.assertRaisesRegex(ValueError,'hal_native_manifest_trust'):
            self.load_hal_clock()

    def test_hal_clock_fixture_rejects_non_macos_hosts(self):
        with mock.patch.object(fixture.sys,'platform','linux'):
            with self.assertRaisesRegex(ValueError,'hal_native_platform_unsupported'):
                fixture.load_hal_clock(self.library,self.installed_addon)
