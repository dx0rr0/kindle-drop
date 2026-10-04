"""Integration tests with isolated storage and a real local SSH/SFTP server."""
import io
import json
import os
from pathlib import Path
import socket
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.request import Request, urlopen
from urllib.error import HTTPError
from xml.etree import ElementTree as ET
import zipfile
from kindle_drop.app import App, handler_for, ThreadingHTTPServer, paramiko, fingerprint, validate_kindle, validate_public_url, epub_metadata


def sample_epub():
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w') as z:
        z.writestr('mimetype', 'application/epub+zip', compress_type=zipfile.ZIP_STORED)
        z.writestr('META-INF/container.xml', '<container xmlns="urn:oasis:names:tc:opendocument:xmlns:container" version="1.0"><rootfiles><rootfile full-path="content.opf" media-type="application/oebps-package+xml"/></rootfiles></container>')
        z.writestr('content.opf', '<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="id"><metadata xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:identifier id="id">urn:uuid:test</dc:identifier><dc:title>Wireless test</dc:title><dc:creator>Kindle Drop</dc:creator><dc:language>en</dc:language><meta property="dcterms:modified">2026-10-03T12:00:00Z</meta></metadata><manifest><item id="text" href="book.xhtml" media-type="application/xhtml+xml"/><item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/></manifest><spine><itemref idref="text"/></spine></package>')
        z.writestr('book.xhtml', '<html xmlns="http://www.w3.org/1999/xhtml"><head><title>Wireless test</title></head><body><h1>Your inbox is ready</h1><p>If you can read this in KOReader, the test EPUB opened successfully.</p><p>Add articles and books from your PC, then download via OPDS or send through SSH.</p></body></html>')
        z.writestr('nav.xhtml', '<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops"><head><title>Contents</title></head><body><nav epub:type="toc"><ol><li><a href="book.xhtml">Wireless test</a></li></ol></nav></body></html>')
    return buffer.getvalue()


class Auth(paramiko.ServerInterface):
    def __init__(self, public):
        self.public = public

    def check_auth_publickey(self, username, key):
        return paramiko.AUTH_SUCCESSFUL if username == 'root' and key == self.public else paramiko.AUTH_FAILED

    def get_allowed_auths(self, username):
        return 'publickey'

    def check_channel_request(self, kind, chanid):
        return paramiko.OPEN_SUCCEEDED if kind == 'session' else paramiko.OPEN_FAILED_ADMINISTRATIVELY_PROHIBITED


class Files(paramiko.SFTPServerInterface):
    def __init__(self, server, *args, root, **kwargs):
        super().__init__(server, *args, **kwargs)
        self.root = Path(root)

    def path(self, remote):
        return self.root / remote.lstrip('/')

    def stat(self, path):
        try:
            return paramiko.SFTPAttributes.from_stat(self.path(path).stat())
        except OSError as e:
            return paramiko.SFTPServer.convert_errno(e.errno)

    lstat = stat

    def list_folder(self, path):
        try:
            result = []
            for child in self.path(path).iterdir():
                attributes = paramiko.SFTPAttributes.from_stat(child.stat())
                attributes.filename = child.name
                result.append(attributes)
            return result
        except OSError as error:
            return paramiko.SFTPServer.convert_errno(error.errno)

    def open(self, path, flags, attr):
        try:
            fd = os.open(self.path(path), flags | getattr(os, 'O_BINARY', 0), 0o600)
            file = os.fdopen(fd, 'r+b' if flags & os.O_RDWR else 'wb' if flags & os.O_WRONLY else 'rb')
            handle = paramiko.SFTPHandle(flags)
            handle.readfile = file
            handle.writefile = file
            handle.stat = lambda: paramiko.SFTPAttributes.from_stat(os.fstat(file.fileno()))
            return handle
        except OSError as e:
            return paramiko.SFTPServer.convert_errno(e.errno)

    def mkdir(self, path, attr):
        try:
            self.path(path).mkdir()
            return paramiko.SFTP_OK
        except OSError as e:
            return paramiko.SFTPServer.convert_errno(e.errno)

    def rename(self, old, new):
        try:
            self.path(old).rename(self.path(new))
            return paramiko.SFTP_OK
        except OSError as e:
            return paramiko.SFTPServer.convert_errno(e.errno)

    def remove(self, path):
        try:
            self.path(path).unlink()
            return paramiko.SFTP_OK
        except OSError as e:
            return paramiko.SFTPServer.convert_errno(e.errno)


class SSHFixture:
    def __init__(self, root, public):
        self.sock = socket.socket()
        self.sock.bind(('127.0.0.1', 0))
        self.port = self.sock.getsockname()[1]
        self.sock.listen(5)
        self.host_key = paramiko.ECDSAKey.generate()
        self.transports = []
        self.running = True
        def worker():
            while self.running:
                try:
                    client, _ = self.sock.accept()
                except OSError:
                    break
                transport = paramiko.Transport(client)
                transport.add_server_key(self.host_key)
                transport.set_subsystem_handler('sftp', paramiko.SFTPServer, Files, root=root)
                self.transports.append(transport)
                try:
                    transport.start_server(server=Auth(public))
                except (EOFError, OSError):
                    transport.close()
        self.thread = threading.Thread(target=worker, daemon=True)
        self.thread.start()

    def close(self):
        self.running = False
        self.sock.close()
        for t in self.transports:
            t.close()


class Tests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.app = App(Path(self.tmp.name) / 'app')

    def tearDown(self):
        self.tmp.cleanup()

    def test_send_readiness_identifies_missing_profile_configuration(self):
        info = self.app.info()
        self.assertFalse(info['send_ready'])
        self.assertIn('verified Kindle setup', info['send_blocked_reason'])
        (self.app.data / 'setup.json').write_text('{}')
        self.assertIn('private key', self.app.info()['send_blocked_reason'])
        (self.app.data / 'kindle_key').write_text('TEST ONLY: placeholder')
        self.assertIn('Pair this profile', self.app.info()['send_blocked_reason'])
        self.app.config['kindle'] = {'host': '192.168.1.25', 'fingerprint': 'SHA256:' + 'A' * 43}
        self.assertTrue(self.app.info()['send_ready'])
        self.assertIsNone(self.app.info()['send_blocked_reason'])

    def test_epub_duplicate_and_restart(self):
        data = sample_epub()
        b = self.app.add_epub(data)
        self.assertEqual(self.app.add_epub(data)['id'], b['id'])
        self.assertEqual(b['status'], 'Ready')
        self.assertEqual(App(self.app.data).books[0]['id'], b['id'])
        with self.assertRaises(ValueError):
            self.app.add_epub(b'not epub')

    def test_article_calibre_and_plain_text(self):
        paragraphs = ''.join(f'<p>This is paragraph {i}. Wireless reading lets you prepare an article on your computer and read it later on your reader. We keep the main text and remove website navigation.</p>' for i in range(7))
        html = f'<html lang="en"><head><title>A sample article</title></head><body><nav>Menu</nav><article><h1>A sample article</h1>{paragraphs}</article></body></html>'
        b = self.app.article('https://example.com/article', html.encode())
        data = (self.app.data / 'books' / b['filename']).read_bytes()
        self.assertIn('sample article', epub_metadata(data)[0])
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            chapters = ''.join(z.read(n).decode() for n in z.namelist() if n.endswith(('.xhtml', '.html')))
            self.assertIn('paragraph 6', chapters)
            self.assertIn('https://example.com/article', chapters)

    def test_network_and_paths_are_restricted(self):
        for host in ['8.8.8.8', '127.0.0.1', '169.254.1.1', '0.0.0.0', 'example.com']:
            with self.assertRaises(ValueError):
                validate_kindle({'host': host})
        with self.assertRaises(ValueError):
            validate_kindle({'host': '192.168.1.25', 'inbox': '/mnt/us/../../etc'})
        for url in ['file:///etc/passwd', 'http://127.0.0.1/', 'http://192.168.1.1/', 'https://user:password@example.com/']:
            with self.assertRaises(ValueError):
                validate_public_url(url)

    def test_http_opds_download_and_auth(self):
        b = self.app.add_epub(sample_epub())
        server = ThreadingHTTPServer(('127.0.0.1', 0), handler_for(self.app))
        port = server.server_address[1]
        threading.Thread(target=server.serve_forever, daemon=True).start()
        base = f'http://127.0.0.1:{port}'
        try:
            with self.assertRaises(HTTPError) as e:
                urlopen(base + '/api/books')
            self.assertEqual(e.exception.code, 401)
            req = Request(base + '/api/books', headers={'X-Kindle-Token': self.app.config['token']})
            self.assertEqual(json.load(urlopen(req))[0]['id'], b['id'])
            feed = ET.fromstring(urlopen(base + '/opds/' + self.app.config['catalog_token']).read())
            href = feed.find('.//{*}entry/{*}link').attrib['href']
            self.assertEqual(urlopen(base + href).read(), sample_epub())
            self.assertEqual(self.app.books[0]['status'], 'Ready')
            with self.assertRaises(HTTPError) as e:
                urlopen(Request(base + '/api/bootstrap', headers={'Host': 'evil.example'}))
            self.assertEqual(e.exception.code, 403)
            with self.assertRaises(HTTPError) as e:
                urlopen(Request(base + '/api/upload', data=sample_epub(), headers={'Content-Type': 'application/epub+zip'}))
            self.assertEqual(e.exception.code, 403)
        finally:
            server.shutdown()
            server.server_close()

    def test_real_ssh_transfer_checksum_retry_and_wrong_host_key(self):
        b = self.app.add_epub(sample_epub())
        key = paramiko.ECDSAKey.generate()
        key.write_private_key_file(str(self.app.data / 'kindle_key'))
        fake_root = Path(self.tmp.name) / 'remote'
        fake_root.mkdir()
        fixture = SSHFixture(fake_root, key)
        # Production validator never accepts localhost. Substitute only for this fixture.
        cfg = {'host': '127.0.0.1', 'port': fixture.port, 'inbox': '/mnt/us/koreader/libros/Inbox',
               'authorized_keys': '/mnt/us/koreader/settings/SSH/authorized_keys', 'fingerprint': fingerprint(fixture.host_key)}
        self.app.config['kindle'] = cfg
        try:
            with patch('kindle_drop.app.validate_kindle', lambda x: x), patch.object(self.app, 'check_runtime'):
                self.app.send(b['id'])
                remote = fake_root / cfg['inbox'].lstrip('/') / b['filename']
                self.assertEqual(remote.read_bytes(), sample_epub())
                self.app.send(b['id'])
                self.assertEqual(len(list(remote.parent.iterdir())), 1)
                self.assertEqual(self.app.books[0]['status'], 'Sent')
                cfg['fingerprint'] = 'SHA256:' + 'A' * 43
                with self.assertRaises(ValueError):
                    self.app.test_key()
        finally:
            fixture.close()


if __name__ == '__main__':
    unittest.main(verbosity=2)
