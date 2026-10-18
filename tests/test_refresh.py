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
            ('frontend/apps/filemanager/filemanager.lua', 'function FileManager:moveFile(from, to)'),
            ('frontend/docsettings.lua', 'function DocSettings.updateLocation(doc_path, new_doc_path, copy) function DocSettings:hasSidecarFile('),
            ('frontend/readhistory.lua', 'function ReadHistory:updateItem(file, new_filepath)'),
            ('frontend/readcollection.lua', 'function ReadCollection:updateItem(file, new_filepath)'),
            ('frontend/util.lua', 'function util.makePath(path)'),
        ]:
            p = reader / name
            p.parent.mkdir(parents=True, exist_ok=True)
            with p.open('a') as file:
                file.write(body + '\n')
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
        self.assertTrue(refresh.notify(self.sftp, '/mnt/us/books/Artículos', '/mnt/us/books/Artículos'))
        self.assertRegex(marker.read_text(encoding='utf-8'), r'^[a-f0-9]{32}\n/mnt/us/books/Artículos\n/mnt/us/books/Artículos\n$')
        refresh.queue_moves(self.sftp, [('/mnt/us/books/a.epub', '/mnt/us/books/Artículos/a.epub')])
        moves = self.root / refresh.MOVES.lstrip('/')
        self.assertEqual(moves.read_text(encoding='utf-8'), '/mnt/us/books/a.epub\t/mnt/us/books/Artículos/a.epub\n')
        with self.assertRaisesRegex(ValueError, 'line break'):
            refresh.queue_moves(self.sftp, [('/mnt/us/books/a\n.epub', '/mnt/us/books/Artículos/a.epub')])
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

    def test_organize_queues_only_earlier_web_articles(self):
        from kindle_drop.app import App
        app = App(self.root / 'local')
        app.config['kindle'] = {'host': '127.0.0.1', 'port': self.fixture.port, 'inbox': '/mnt/us/books', 'fingerprint': 'unused'}
        inbox = self.root / 'mnt/us/books'
        inbox.mkdir(parents=True)
        def record(id, name, **fields):
            (inbox / name).write_bytes(b'epub')
            return {'id': id, 'title': name, 'filename': name, 'status': 'Sent', 'remote_path': '/mnt/us/books/' + name, **fields}
        app.books = [record('a' * 16, 'Article.epub', source='https://example.org/a', images=2),
                     record('b' * 16, 'Paper.epub', source='https://example.org/paper.pdf', images=0, source_sha256='x'),
                     record('c' * 16, 'Book.epub', source='EPUB file'),
                     record('d' * 16, 'Gone.epub', source='https://example.org/gone', images=0)]
        (inbox / 'Gone.epub').unlink()  # already moved or deleted on the Kindle
        self.client.close = lambda: None  # organize closes its connection; this one is shared
        connect = patch.object(app, 'connect', side_effect=lambda: (self.client, app.config['kindle']))
        with connect, self.assertRaisesRegex(ValueError, 'companion'):
            app.organize()  # without the companion nothing would carry History and highlights along
        refresh.install(self.sftp)
        with connect:
            result = app.organize()
        self.assertEqual(result['moved'], ['a' * 16])
        moves = (self.root / refresh.MOVES.lstrip('/')).read_text(encoding='utf-8')
        self.assertEqual(moves, '/mnt/us/books/Article.epub\t/mnt/us/books/Artículos/Article.epub\n')
        self.assertTrue((inbox / 'Artículos').is_dir())
        self.assertRegex((self.root / refresh.MARKER.lstrip('/')).read_text(encoding='utf-8'), r'\n/mnt/us/books/Artículos\n$')
        self.assertEqual(app.books[0]['remote_path'], '/mnt/us/books/Artículos/Article.epub')
        self.assertEqual([b.get('shelf') for b in app.books], ['article', None, None, None])

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
        self.assertEqual((self.root / 'mnt/us/books/Artículos' / book['filename']).read_bytes(), sample_epub())
