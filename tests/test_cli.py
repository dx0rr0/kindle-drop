import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

from kindle_drop.app import App, ThreadingHTTPServer, atomic_json, fingerprint, handler_for
from kindle_drop.cli import inputs_from_make, main, state_lock
from kindle_drop import prerequisites as checks
from tests.test_app import SSHFixture, sample_epub
from tests.test_prerequisites import fake_kindle, write_passing_report


class CliTests(unittest.TestCase):
    def test_input_spaces_queries_and_windows_glob(self):
        with tempfile.TemporaryDirectory() as temporary:
            file = Path(temporary) / 'A sample book.epub'
            file.write_bytes(sample_epub())
            with patch.dict(os.environ, {'KD_INPUT': str(file), 'KD_GOALS': 'send ignored'}):
                self.assertEqual(inputs_from_make([]), [str(file)])
            with patch.dict(os.environ, {'KD_INPUT': 'https://example.org/read?a=1&b=2'}):
                self.assertEqual(inputs_from_make([]), ['https://example.org/read?a=1&b=2'])
            self.assertEqual(inputs_from_make([str(Path(temporary) / '*.epub')]), [str(file)])
            with self.assertRaisesRegex(ValueError, 'No files match'):
                inputs_from_make([str(Path(temporary) / 'missing*.epub')])

    @unittest.skipUnless(shutil.which('make'), 'GNU Make is not installed')
    def test_make_positional_url_glob_and_safe_environment(self):
        repo = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as temporary:
            recorder = Path(temporary) / 'record.py'
            output = Path(temporary) / 'goals.json'
            recorder.write_text('import json,os,pathlib,sys\nif sys.argv[1]=="send": pathlib.Path(os.environ["KD_RECORD"]).write_text(json.dumps({k:os.environ.get(k) for k in ("KD_INPUT","KD_GOALS")}))\n')
            env = dict(os.environ, KD_RECORD=str(output))
            run = f'"{sys.executable}" "{recorder}"'
            for args in (['send', 'https://example.org/read?a=1&b=2'], ['send', 'books/*.epub'], ['send', 'INPUT=books/A sample book.epub']):
                with self.subTest(args=args):
                    result = subprocess.run(['make', f'RUN={run}', *args], cwd=repo, env=env, capture_output=True, text=True)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    record = json.loads(output.read_text())
                    self.assertIn('send', record['KD_GOALS'])
                    if args[1].startswith('INPUT='):
                        self.assertEqual(record['KD_INPUT'], 'books/A sample book.epub')
                    else:
                        self.assertIn(args[1], record['KD_GOALS'])

    def test_process_lock_excludes_another_cli(self):
        with tempfile.TemporaryDirectory() as temporary:
            data = Path(temporary) / 'state'
            with state_lock(data):
                proc = subprocess.run([sys.executable, '-m', 'kindle_drop', '--data', str(data), 'test'], capture_output=True, text=True)
                self.assertEqual(proc.returncode, 1)
                self.assertIn('using this state', proc.stderr)

    def test_cli_uses_running_web_app_for_real_sftp_transfer(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            mount, data = root / 'kindle', root / 'state'
            fake_kindle(mount)
            checks.prepare(mount, data)
            write_passing_report(mount, data)
            checks.install_key(mount, data)
            app = App(data)
            # The fixture represents the actual Kindle filesystem for live SFTP checks.
            remote = root / 'remote'
            shutil.copytree(mount, remote / 'mnt/us')
            for name in ('otaupd.bck', 'otav3.bck'):
                file = remote / 'usr/bin' / name
                file.parent.mkdir(parents=True, exist_ok=True)
                file.write_bytes(b'disabled test updater')
            (remote / 'tmp').mkdir()
            (remote / 'tmp/dropbear_koreader.pid').write_text('123')
            (remote / 'proc/123').mkdir(parents=True)
            (remote / 'proc/123/cmdline').write_bytes(b'./dropbear\0-E\0-R\0-p2222\0')
            (remote / 'proc/123/comm').write_bytes(b'dropbear\n')
            fixture = SSHFixture(remote, app.key()[0])
            app.config['kindle'] = {'host': '127.0.0.1', 'port': fixture.port,
                'inbox': '/mnt/us/documents/KindleDrop', 'authorized_keys': '/mnt/us/koreader/settings/SSH/authorized_keys',
                'fingerprint': fingerprint(fixture.host_key)}
            atomic_json(data / 'config.json', app.config)
            server = ThreadingHTTPServer(('127.0.0.1', 0), handler_for(app))
            atomic_json(data / 'server.json', {'port': server.server_address[1]})
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            book = root / 'A book.epub'
            book.write_bytes(sample_epub())
            try:
                with patch('kindle_drop.app.validate_kindle', lambda cfg: cfg):
                    # The CLI process talks to HTTP; only the server uses this localhost fixture exception.
                    with state_lock(data):
                        result = subprocess.run([sys.executable, '-m', 'kindle_drop', '--data', str(data), 'send', str(book)], capture_output=True, text=True)
                        self.assertEqual(result.returncode, 0, result.stderr)
                    sent = list((remote / 'mnt/us/documents/KindleDrop').glob('*.epub'))
                    self.assertEqual(len(sent), 1)
                    self.assertEqual(sent[0].read_bytes(), sample_epub())
                    # An OTA regression blocks a subsequent send despite an existing key and pairing.
                    (remote / 'usr/bin/otaupd').write_bytes(b'active updater')
                    with self.assertRaisesRegex(ValueError, 'OTA protection'):
                        app.send(app.books[0]['id'])
                    (remote / 'usr/bin/otaupd').unlink()
                    (remote / 'proc/123/cmdline').write_bytes(b'./dropbear\0-n\0')
                    with self.assertRaisesRegex(ValueError, 'password bypass'):
                        app.send(app.books[0]['id'])
            finally:
                server.shutdown()
                server.server_close()
                fixture.close()
