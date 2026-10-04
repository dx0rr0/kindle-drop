from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from kindle_drop import refresh
from tests.test_app import SSHFixture, paramiko


class RefreshTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        reader = self.root / 'mnt/us/koreader'
        for name, body in [
            ('frontend/apps/filemanager/filemanager.lua', 'function FileManager:onRefresh()'),
            ('frontend/ui/widget/filechooser.lua', 'function FileChooser:clearSortingCache()'),
            ('frontend/ui/uimanager.lua', 'function UIManager:getTopmostVisibleWidget() function UIManager:scheduleIn( function UIManager:unschedule('),
        ]:
            p = reader / name
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(body)
        (reader / 'settings').mkdir()
        (reader / 'plugins').mkdir()
        self.key = paramiko.ECDSAKey.generate()
        self.fixture = SSHFixture(self.root, self.key)
        self.client = paramiko.SSHClient()
        self.client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        self.client.connect('127.0.0.1', port=self.fixture.port, username='root', pkey=self.key, allow_agent=False, look_for_keys=False)
        self.sftp = self.client.open_sftp()

    def tearDown(self):
        self.sftp.close()
        self.client.close()
        self.fixture.close()
        self.tmp.cleanup()

    def test_install_signal_repeat_and_managed_upgrade(self):
        self.assertFalse(refresh.notify(self.sftp, '/mnt/us/books'))
        refresh.install(self.sftp)
        self.assertIn('already installed', refresh.install(self.sftp)['message'])
        self.assertTrue(refresh.notify(self.sftp, '/mnt/us/books'))
        marker = self.root / refresh.MARKER.lstrip('/')
        first = marker.read_text()
        self.assertRegex(first, r'^[a-f0-9]{32}\n/mnt/us/books\n$')
        self.assertTrue(refresh.notify(self.sftp, '/mnt/us/books'))
        self.assertNotEqual(marker.read_text(), first)
        self.assertEqual(list(marker.parent.glob('*.part-*')), [])
        main = self.root / refresh.PLUGIN.lstrip('/') / 'main.lua'
        old = main.read_bytes() + b'-- old version\n'
        main.write_bytes(old)
        refresh.install(self.sftp)
        backups = list(main.parent.parent.glob('.kindle-drop-backup-*/main.lua'))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_bytes(), old)
        self.assertEqual(main.read_bytes(), (refresh.ASSETS / 'main.lua').read_bytes().replace(b'\r\n', b'\n'))

    def test_incompatible_api_and_unknown_plugin_are_not_overwritten(self):
        file = self.root / 'mnt/us/koreader/frontend/ui/uimanager.lua'
        original = file.read_bytes()
        file.write_bytes(b'incompatible')
        with self.assertRaisesRegex(ValueError, 'does not support'):
            refresh.install(self.sftp)
        self.assertFalse((self.root / refresh.PLUGIN.lstrip('/')).exists())
        file.write_bytes(original)
        plugin = self.root / refresh.PLUGIN.lstrip('/')
        plugin.mkdir()
        (plugin / 'main.lua').write_bytes(b'an unrelated plugin')
        with self.assertRaisesRegex(ValueError, 'not overwritten'):
            refresh.install(self.sftp)
        self.assertEqual((plugin / 'main.lua').read_bytes(), b'an unrelated plugin')

    def test_refresh_failure_preserves_successful_delivery(self):
        from kindle_drop.app import App
        from tests.test_app import sample_epub
        app = App(self.root / 'local')
        app.config['kindle'] = {'host': '127.0.0.1', 'port': self.fixture.port,
            'inbox': '/mnt/us/books', 'fingerprint': 'unused'}
        book = app.add_epub(sample_epub())
        # Use this already authenticated fixture client; normal connections still run prerequisites.
        with patch.object(app, 'connect', return_value=(self.client, app.config['kindle'])), patch('kindle_drop.refresh.notify', side_effect=OSError('marker failed')):
            result = app.send(book['id'])
        self.assertFalse(result['refresh_requested'])
        self.assertIn('delivered', result['refresh_warning'])
        self.assertEqual(app.books[0]['status'], 'Sent')
        self.assertEqual((self.root / 'mnt/us/books' / book['filename']).read_bytes(), sample_epub())
