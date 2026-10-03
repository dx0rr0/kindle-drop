"""Safety gates use synthetic USB volumes, never a personal device or key."""
import json
import os
from pathlib import Path
import tempfile
import time
import unittest

from kindle_drop import prerequisites as checks
from kindle_drop.app import App, atomic_json


def fake_kindle(mount):
    for relative in checks.FILES:
        path = mount / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b'test fixture: ' + relative.encode())
    (mount / 'system/version.txt').write_text('Kindle 5.16.2.1.1 (TEST DEVICE ONLY)', encoding='ascii')
    (mount / 'koreader/plugins/SSH.koplugin/main.lua').write_text(
        '-- test fixture settings/SSH/authorized_keys SSH_allow_no_password', encoding='ascii')
    (mount / 'koreader/settings.reader.lua').write_text('return {}', encoding='ascii')


def write_passing_report(mount, data, **overrides):
    expected = json.loads((data / 'pending-check.json').read_text())
    report = {'schema': '1', 'challenge': expected['challenge'], 'uid': '0', 'ota': 'renamed',
              'ota_processes': 'none', 'pending_updates': 'none', 'dropbear_executable': 'yes',
              'sftp_executable': 'yes', 'ssh_bypass': 'off', 'ssh_autostart': 'off', 'ssh_runtime_bypass': 'off',
              'storage_writable': 'yes', 'free_kb': '100000', 'checker_sha': expected['checker'],
              'version_sha': expected['files']['system/version.txt'],
              'plugin_sha': expected['files']['koreader/plugins/SSH.koplugin/main.lua'],
              'dropbear_sha': expected['files']['koreader/dropbear'],
              'sftp_sha': expected['files']['koreader/sftp-server']}
    report.update(overrides)
    (mount / checks.REPORT).write_text(''.join(f'{k}={v}\n' for k, v in report.items()), encoding='ascii')


class PrerequisiteTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.mount, self.data = self.root / 'kindle', self.root / 'state'
        fake_kindle(self.mount)

    def tearDown(self):
        self.temp.cleanup()

    def assert_no_key(self):
        self.assertFalse((self.data / 'kindle_key').exists())
        self.assertFalse((self.mount / 'koreader/settings/SSH/authorized_keys').exists())

    def test_missing_runtime_report_blocks_key(self):
        checks.prepare(self.mount, self.data)
        with self.assertRaises(OSError):
            checks.install_key(self.mount, self.data)
        self.assert_no_key()
        self.assertEqual((self.mount / checks.EXTENSION / 'check.sh').read_bytes(), (checks.ASSETS / 'check.sh').read_bytes())

    def test_each_failed_field_blocks_key(self):
        checks.prepare(self.mount, self.data)
        failures = {'challenge': 'wrong-device', 'uid': '1000', 'ota': 'unknown',
                    'ota_processes': 'running', 'pending_updates': 'present', 'sftp_executable': 'no',
                    'dropbear_executable': 'no', 'ssh_bypass': 'on', 'ssh_autostart': 'on',
                    'ssh_runtime_bypass': 'on', 'storage_writable': 'no', 'free_kb': '2',
                    'version_sha': 'changed', 'plugin_sha': 'changed', 'dropbear_sha': 'changed',
                    'sftp_sha': 'changed', 'checker_sha': 'changed', 'schema': '0'}
        for field, value in failures.items():
            with self.subTest(field=field):
                write_passing_report(self.mount, self.data, **{field: value})
                with self.assertRaises(ValueError):
                    checks.install_key(self.mount, self.data)
                self.assert_no_key()

    def test_expired_challenge_and_changed_installation(self):
        checks.prepare(self.mount, self.data)
        write_passing_report(self.mount, self.data)
        pending = self.data / 'pending-check.json'
        original = json.loads(pending.read_text())
        expired = dict(original, prepared_at=time.time() - checks.MAX_AGE - 10)
        atomic_json(pending, expired)
        with self.assertRaisesRegex(ValueError, 'expired'):
            checks.install_key(self.mount, self.data)
        atomic_json(pending, original)
        (self.mount / 'koreader/dropbear').write_bytes(b'changed')
        with self.assertRaisesRegex(ValueError, 'changed'):
            checks.install_key(self.mount, self.data)
        self.assert_no_key()

    def test_previous_challenge_cannot_be_reused(self):
        checks.prepare(self.mount, self.data)
        write_passing_report(self.mount, self.data)
        checks.prepare(self.mount, self.data)
        with self.assertRaisesRegex(ValueError, 'challenge'):
            checks.install_key(self.mount, self.data)
        self.assert_no_key()

    def test_key_creation_preserves_existing_entries_and_is_idempotent(self):
        checks.prepare(self.mount, self.data)
        write_passing_report(self.mount, self.data)
        checks.verify(self.mount, self.data)
        self.assert_no_key()
        keys = self.mount / 'koreader/settings/SSH/authorized_keys'
        keys.parent.mkdir(parents=True)
        existing = b'# existing user entries\nssh-ed25519 TEST_FIXTURE other-client'
        keys.write_bytes(existing)
        checks.install_key(self.mount, self.data)
        first = keys.read_bytes()
        self.assertTrue(first.startswith(existing + b'\n'))
        self.assertIn(b'ecdsa-sha2-nistp256', first)
        self.assertNotIn(b'PRIVATE', first)
        private = (self.data / 'kindle_key').read_bytes()
        self.assertIn(b'PRIVATE', private)
        checks.install_key(self.mount, self.data)
        self.assertEqual(keys.read_bytes(), first)
        self.assertEqual((self.data / 'kindle_key').read_bytes(), private)
        self.assertEqual(len(list((self.data / 'backups').iterdir())), 1)
        self.assertEqual(next((self.data / 'backups').iterdir()).read_bytes(), existing)
        if os.name != 'nt':
            self.assertEqual((self.data / 'kindle_key').stat().st_mode & 0o777, 0o600)

    def test_browser_or_core_cannot_implicitly_create_key(self):
        app = App(self.data)
        with self.assertRaisesRegex(ValueError, 'No SSH key'):
            app.key()
        self.assert_no_key()

    def test_unknown_firmware_and_password_bypass_stop_usb_stage(self):
        settings = self.mount / 'koreader/settings.reader.lua'
        for entry in ('["SSH_allow_no_password"] = true', '["SSH_allow_no_password"] = strange_function()', '["SSH_autostart"] = true'):
            with self.subTest(entry=entry):
                settings.write_text('return {' + entry + '}', encoding='ascii')
                with self.assertRaises(ValueError):
                    checks.prepare(self.mount, self.data)
        settings.write_text('return {}')
        (self.mount / 'system/version.txt').write_text('Kindle 5.10.0')
        with self.assertRaisesRegex(ValueError, 'Older OTA'):
            checks.prepare(self.mount, self.data)
        self.assert_no_key()

    def test_malformed_and_duplicate_reports_are_rejected(self):
        for raw in (b'not a report', b'uid=0\nuid=0\n', b'uid=0\ninvalid field=1\n'):
            with self.assertRaises(ValueError):
                checks.parse_report(raw)

    def test_symlink_outside_volume_is_rejected(self):
        path = self.mount / 'koreader/settings/SSH'
        outside = self.root / 'outside'
        outside.mkdir()
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            path.symlink_to(outside, target_is_directory=True)
        except OSError:
            self.skipTest('Symlink creation requires privileges on this system.')
        checks.prepare(self.mount, self.data)
        write_passing_report(self.mount, self.data)
        with self.assertRaisesRegex(ValueError, 'outside'):
            checks.install_key(self.mount, self.data)
        self.assert_no_key()
