"""Kindle Drop: local inbox, article conversion, OPDS and pinned SSH/SFTP."""
from __future__ import annotations

import argparse
import base64
import hashlib
import io
import ipaddress
import http.client
import json
import mimetypes
import os
from pathlib import Path, PurePosixPath
import re
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit, urljoin, unquote
from urllib.request import Request, build_opener, HTTPRedirectHandler, ProxyHandler, HTTPHandler, HTTPSHandler
from urllib.error import HTTPError, URLError
from xml.etree import ElementTree as ET
from xml.sax.saxutils import escape
import zipfile

ROOT = Path(__file__).resolve().parent

import paramiko
import trafilatura

MAX_FILE = 40 * 1024 * 1024
MAX_HTML = 8 * 1024 * 1024
MAX_UNZIP = 200 * 1024 * 1024
ATOM = 'http://www.w3.org/2005/Atom'
DC = 'http://purl.org/dc/elements/1.1/'
CREATE_NO_WINDOW = getattr(subprocess, 'CREATE_NO_WINDOW', 0)


def find_calibre(name):
    """Discover Calibre on PATH, with the standard Windows/macOS locations."""
    override = os.environ.get('EBOOK_CONVERT' if name == 'ebook-convert' else 'CALIBRE')
    if override:
        return Path(override)
    found = shutil.which(name)
    if found:
        return Path(found)
    candidates = [
        Path(os.environ.get('PROGRAMFILES', r'C:\Program Files')) / 'Calibre2' / (name + '.exe'),
        Path('/Applications/calibre.app/Contents/MacOS') / name,
    ]
    return next((p for p in candidates if p.is_file()), Path('__calibre_not_found__'))


def now():
    return datetime.now(timezone.utc).isoformat(timespec='seconds').replace('+00:00', 'Z')


def atomic_json(path, data):
    tmp = path.with_suffix('.tmp')
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
    tmp.replace(path)


def safe_text(text):
    return re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f]', '', str(text))


def filename(title):
    title = re.sub(r'[^\w .-]', '', safe_text(title), flags=re.UNICODE).strip(' .')
    return (title[:80] or 'Reading')


def epub_metadata(data):
    if len(data) > MAX_FILE:
        raise ValueError('The EPUB exceeds the 40 MB limit.')
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            infos = z.infolist()
            if len(infos) > 10000 or sum(i.file_size for i in infos) > MAX_UNZIP:
                raise ValueError('The uncompressed EPUB is too large.')
            if len({i.filename for i in infos}) != len(infos):
                raise ValueError('The EPUB contains duplicate ZIP entries.')
            if z.read('mimetype').strip() != b'application/epub+zip':
                raise ValueError('The file is not a valid EPUB.')
            if z.getinfo('META-INF/container.xml').file_size > 1024 * 1024:
                raise ValueError('EPUB metadata is too large.')
            container = ET.fromstring(z.read('META-INF/container.xml'))
            rootfile = container.find('.//{*}rootfile')
            if rootfile is None:
                raise ValueError('The EPUB package document is missing.')
            name = rootfile.attrib['full-path']
            if z.getinfo(name).file_size > 2 * 1024 * 1024:
                raise ValueError('EPUB metadata is too large.')
            package = ET.fromstring(z.read(name))
            if package.tag != '{http://www.idpf.org/2007/opf}package':
                raise ValueError('The package document is not OPF.')
            title = package.findtext(f'.//{{{DC}}}title') or 'Reading'
            author = package.findtext(f'.//{{{DC}}}creator') or 'Unknown author'
            return safe_text(title), safe_text(author)
    except (zipfile.BadZipFile, KeyError, ET.ParseError) as e:
        raise ValueError('The file does not have a valid EPUB structure.') from e


def validate_public_url(url):
    p = urlsplit(url)
    if p.scheme not in ('http', 'https') or not p.hostname or p.username or p.password:
        raise ValueError('Use a public HTTP or HTTPS URL without credentials.')
    if p.port not in (None, 80, 443):
        raise ValueError('URLs must use the standard HTTP or HTTPS port.')
    addresses = socket.getaddrinfo(p.hostname, p.port or (443 if p.scheme == 'https' else 80), type=socket.SOCK_STREAM)
    if not addresses or any(not ipaddress.ip_address(a[4][0]).is_global for a in addresses):
        raise ValueError('URL import only supports public Internet addresses.')
    return p


def public_connection(address, timeout=25, source_address=None, **kwargs):
    # Resolve once and connect to a checked numeric IP, preventing DNS rebinding.
    host, port = address
    addresses = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    if not addresses or any(not ipaddress.ip_address(a[4][0]).is_global for a in addresses):
        raise ValueError('The destination address is not public.')
    last_error = None
    for family, type_, proto, _, sockaddr in addresses:
        sock = socket.socket(family, type_, proto)
        try:
            sock.settimeout(timeout)
            if source_address:
                sock.bind(source_address)
            sock.connect(sockaddr)
            return sock
        except OSError as e:
            sock.close()
            last_error = e
    raise last_error or OSError('Could not connect.')


class PublicHTTPConnection(http.client.HTTPConnection):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._create_connection = public_connection


class PublicHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._create_connection = public_connection


class PublicHTTPHandler(HTTPHandler):
    def http_open(self, req):
        return self.do_open(PublicHTTPConnection, req)


class PublicHTTPSHandler(HTTPSHandler):
    def https_open(self, req):
        return self.do_open(PublicHTTPSConnection, req, context=self._context)


class PublicRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        validate_public_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def fetch_public(url):
    validate_public_url(url)
    opener = build_opener(ProxyHandler({}), PublicRedirect(), PublicHTTPHandler(), PublicHTTPSHandler())
    request = Request(url, headers={'User-Agent': 'Mozilla/5.0 KindleDrop/0.1', 'Accept-Encoding': 'identity'})
    try:
        with opener.open(request, timeout=25) as response:
            # Check each redirect through PublicRedirect, then bound the download.
            body = response.read(MAX_FILE + 1)
            if len(body) > MAX_FILE:
                raise ValueError('The download exceeds 40 MB.')
            return body, response.headers.get_content_type(), response.geturl()
    except HTTPError as e:
        raise ValueError(f'The website rejected the download (HTTP {e.code}). It may require a browser or login.') from e
    except URLError as e:
        raise ValueError('Could not download the URL. Check your connection; some websites reject automated downloads.') from e


def local_ips():
    result = set()
    try:
        for a in socket.getaddrinfo(socket.gethostname(), None, family=socket.AF_INET):
            address = ipaddress.ip_address(a[4][0])
            if address.is_private and not address.is_loopback and not address.is_link_local:
                result.add(str(address))
    except OSError:
        pass
    return sorted(result)


def validate_kindle(config):
    try:
        address = ipaddress.ip_address(config.get('host', ''))
    except ValueError as e:
        raise ValueError('Enter the Kindle private IPv4 address, for example 192.168.1.25.') from e
    if address.version != 4 or not any(address in ipaddress.ip_network(n) for n in ('10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16')):
        raise ValueError('Only private LAN IPv4 addresses are supported.')
    port = int(config.get('port', 2222))
    if not 1 <= port <= 65535:
        raise ValueError('Invalid SSH port.')
    inbox = config.get('inbox', '/mnt/us/documents/KindleDrop').rstrip('/')
    authorized = config.get('authorized_keys', '/mnt/us/koreader/settings/SSH/authorized_keys')
    for path in (inbox, authorized):
        if not path.startswith('/mnt/us/') or '..' in PurePosixPath(path).parts or re.search(r'[\x00-\x1f]', path):
            raise ValueError('Kindle paths must stay inside /mnt/us/.')
    if not authorized.endswith('/authorized_keys'):
        raise ValueError('The public key path must end in /authorized_keys.')
    return {'host': str(address), 'port': port, 'inbox': inbox, 'authorized_keys': authorized,
            'fingerprint': str(config.get('fingerprint', ''))}


def fingerprint(key):
    return 'SHA256:' + base64.b64encode(hashlib.sha256(key.asbytes()).digest()).decode().rstrip('=')


class PinnedKey(paramiko.MissingHostKeyPolicy):
    def __init__(self, expected):
        self.expected = expected

    def missing_host_key(self, client, hostname, key):
        if not self.expected or not secrets.compare_digest(fingerprint(key), self.expected):
            raise ValueError('The SSH host fingerprint changed. Verify the device before reconnecting.')


def mkdirs(sftp, path):
    parts = PurePosixPath(path).parts
    current = ''
    for part in parts[1:]:
        current += '/' + part
        try:
            sftp.stat(current)
        except FileNotFoundError:
            sftp.mkdir(current)


class App:
    def __init__(self, data_dir=None, port=8765, lan=False):
        self.data = Path(data_dir or Path(os.environ.get('KINDLE_DROP_DATA', '.kindle-drop')))
        self.data.mkdir(parents=True, exist_ok=True)
        (self.data / 'books').mkdir(exist_ok=True)
        self.port, self.lan = port, lan
        self.lock = threading.RLock()
        config_file = self.data / 'config.json'
        self.config = json.loads(config_file.read_text(encoding='utf-8')) if config_file.exists() else {
            'token': secrets.token_urlsafe(24), 'catalog_token': secrets.token_urlsafe(18), 'kindle': {}}
        atomic_json(config_file, self.config)
        ledger = self.data / 'books.json'
        self.books = json.loads(ledger.read_text(encoding='utf-8')) if ledger.exists() else []
        self.converter = find_calibre('ebook-convert')
        self.calibre = find_calibre('calibre')

    def info(self):
        hosts = local_ips()
        return {'lan': self.lan, 'ips': hosts, 'port': self.port,
                'catalog_path': f'/opds/{self.config["catalog_token"]}',
                'download_path': f'/download/{self.config["catalog_token"]}',
                'opds': [f'http://{h}:{self.port}/opds/{self.config["catalog_token"]}' for h in hosts],
                'mobile': [f'http://{h}:{self.port}/#{self.config["token"]}' for h in hosts],
                'kindle': self.config['kindle'], 'calibre': self.converter.is_file(),
                'key_exists': (self.data / 'kindle_key').is_file(),
                'setup_complete': (self.data / 'setup.json').is_file()}

    def save_books(self):
        atomic_json(self.data / 'books.json', self.books)

    def get_book(self, id):
        with self.lock:
            book = next((dict(b) for b in self.books if b['id'] == id), None)
        if book is None:
            raise ValueError('Reading no encontrada.')
        return book

    def add_epub(self, data, source='EPUB file'):
        title, author = epub_metadata(data)
        digest = hashlib.sha256(data).hexdigest()
        with self.lock:
            for b in self.books:
                if b['sha256'] == digest:
                    return dict(b)
            id = secrets.token_hex(8)
            name = f'{filename(title)}-{id[:6]}.epub'
            path = self.data / 'books' / name
            path.write_bytes(data)
            book = {'id': id, 'title': title, 'author': author, 'filename': name,
                    'source': source, 'created': now(), 'size': len(data), 'sha256': digest,
                    'status': 'Ready', 'sent_at': None}
            self.books.insert(0, book)
            self.save_books()
            return dict(book)

    def article(self, url, data):
        if not self.converter.is_file():
            raise ValueError('Calibre ebook-convert was not found. Install Calibre or set EBOOK_CONVERT.')
        if len(data) > MAX_HTML:
            raise ValueError('The page exceeds the 8 MB HTML limit.')
        raw = trafilatura.extract(data, url=url, output_format='json', with_metadata=True,
                                 include_comments=False, include_tables=True)
        if not raw:
            raise ValueError('Could not extract the article. It may require login or JavaScript.')
        article = json.loads(raw)
        text = safe_text(article.get('text') or '')
        if len(text.strip()) < 120:
            raise ValueError('The page has too little readable text. Try the full article URL.')
        title = safe_text(article.get('title') or urlsplit(url).hostname or 'Article')
        author = safe_text(article.get('author') or urlsplit(url).hostname or 'Web')
        paragraphs = '\n'.join('<p>' + escape(p) + '</p>' for p in text.splitlines() if p.strip())
        html = ('<!DOCTYPE html><html xmlns="http://www.w3.org/1999/xhtml"><head><meta charset="utf-8"/>'
                f'<title>{escape(title)}</title></head><body><h1>{escape(title)}</h1>'
                f'<p>{escape(author)}</p><p>Source: <a href="{escape(url, {chr(34): "&quot;"})}">{escape(url)}</a></p>'
                + paragraphs + '</body></html>')
        with tempfile.TemporaryDirectory(dir=self.data) as folder:
            src, dst = Path(folder) / 'article.html', Path(folder) / 'article.epub'
            src.write_text(html, encoding='utf-8')
            env = os.environ.copy()
            env['CALIBRE_CONFIG_DIRECTORY'] = str(self.data / 'calibre-config')
            proc = subprocess.run([str(self.converter), str(src), str(dst), '--title', title, '--authors', author,
                                   '--input-encoding', 'utf-8', '--epub-version', '3'],
                                  env=env, capture_output=True, timeout=100, creationflags=CREATE_NO_WINDOW)
            if proc.returncode or not dst.is_file():
                raise ValueError('Calibre could not convert the article to EPUB.')
            return self.add_epub(dst.read_bytes(), url)

    def add_url(self, url):
        url = url.strip()
        data, content_type, final_url = fetch_public(url)
        if content_type == 'application/epub+zip' or data[:4] == b'PK\x03\x04':
            return self.add_epub(data, final_url)
        if content_type not in ('text/html', 'application/xhtml+xml'):
            raise ValueError('The URL must point to an EPUB or an HTML article.')
        return self.article(final_url, data)

    def key(self):
        path = self.data / 'kindle_key'
        if not path.is_file():
            raise ValueError('No SSH key exists. Complete make check, make verify and make key first.')
        key = paramiko.ECDSAKey.from_private_key_file(str(path))
        return key, f'{key.get_name()} {key.get_base64()} kindle-drop\n'

    def probe(self, cfg):
        cfg = validate_kindle(cfg)
        with socket.create_connection((cfg['host'], cfg['port']), timeout=8) as sock:
            transport = paramiko.Transport(sock)
            try:
                transport.start_client(timeout=8)
                return {'fingerprint': fingerprint(transport.get_remote_server_key())}
            finally:
                transport.close()

    def save_kindle(self, cfg):
        if not (self.data / 'setup.json').is_file():
            raise ValueError('Complete the verified USB key setup first.')
        cfg = validate_kindle(cfg)
        if not re.fullmatch(r'SHA256:[A-Za-z0-9+/]{43}', cfg['fingerprint']):
            raise ValueError('First inspect and verify the Kindle SSH host fingerprint.')
        with self.lock:
            self.config['kindle'] = cfg
            atomic_json(self.data / 'config.json', self.config)
        return cfg

    def connect(self):
        with self.lock:
            cfg = validate_kindle(dict(self.config['kindle']))
        if not cfg['fingerprint']:
            raise ValueError('First configure and verify the Kindle SSH host fingerprint.')
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(PinnedKey(cfg['fingerprint']))
        args = {'hostname': cfg['host'], 'port': cfg['port'], 'username': 'root', 'timeout': 8,
                'banner_timeout': 8, 'auth_timeout': 8, 'allow_agent': False, 'look_for_keys': False}
        args['pkey'] = self.key()[0]
        try:
            client.connect(**args)
            client.get_transport().set_keepalive(15)
            self.check_runtime(client)
            return client, cfg
        except Exception:
            client.close()
            raise

    def check_runtime(self, client):
        from .prerequisites import check_remote
        check_remote(client, self.data)

    def test_key(self):
        client, _ = self.connect()
        try:
            return {'message': 'Public key authentication and live Kindle checks passed.'}
        finally:
            client.close()

    def send(self, id):
        book = self.get_book(id)
        client, cfg = self.connect()
        remote = cfg['inbox'] + '/' + book['filename']
        partial = remote + '.part-' + secrets.token_hex(4)
        try:
            with client.open_sftp() as sftp:
                sftp.get_channel().settimeout(30)
                mkdirs(sftp, cfg['inbox'])
                try:
                    with sftp.open(remote, 'rb') as file:
                        if hashlib.sha256(file.read(MAX_FILE + 1)).hexdigest() != book['sha256']:
                            raise ValueError('A different file with the same name already exists on the Kindle.')
                except FileNotFoundError:
                    try:
                        sftp.put(str(self.data / 'books' / book['filename']), partial)
                        with sftp.open(partial, 'rb') as file:
                            if hashlib.sha256(file.read(MAX_FILE + 1)).hexdigest() != book['sha256']:
                                raise ValueError('The transferred EPUB failed checksum verification.')
                        sftp.rename(partial, remote)
                    finally:
                        try:
                            sftp.remove(partial)
                        except OSError:
                            pass
            with self.lock:
                b = next(b for b in self.books if b['id'] == id)
                b.update(status='Sent', sent_at=now())
                self.save_books()
            return {'message': f'EPUB verified on the Kindle: {remote}. Open it in the KOReader file browser.'}
        finally:
            client.close()

    def feed(self):
        ET.register_namespace('', ATOM)
        ET.register_namespace('dc', 'http://purl.org/dc/terms/')
        root = ET.Element(f'{{{ATOM}}}feed')
        for tag, text in [('id', 'urn:kindle-drop:inbox'), ('title', 'Kindle Drop inbox'), ('updated', now())]:
            ET.SubElement(root, f'{{{ATOM}}}{tag}').text = text
        ET.SubElement(root, f'{{{ATOM}}}link', {'rel': 'self', 'href': f'/opds/{self.config["catalog_token"]}',
                                             'type': 'application/atom+xml;profile=opds-catalog;kind=acquisition'})
        with self.lock:
            books = [dict(b) for b in self.books]
        for b in books:
            entry = ET.SubElement(root, f'{{{ATOM}}}entry')
            for tag, text in [('id', 'urn:kindle-drop:' + b['id']), ('title', b['title']), ('updated', b['created']),
                              ('summary', 'EPUB ready to download in KOReader.')]:
                ET.SubElement(entry, f'{{{ATOM}}}{tag}').text = text
            author = ET.SubElement(entry, f'{{{ATOM}}}author')
            ET.SubElement(author, f'{{{ATOM}}}name').text = b['author']
            ET.SubElement(entry, f'{{{ATOM}}}link', {'rel': 'http://opds-spec.org/acquisition',
                'href': f'/download/{self.config["catalog_token"]}/{b["id"]}', 'type': 'application/epub+zip'})
        return ET.tostring(root, encoding='utf-8', xml_declaration=True)


def handler_for(app):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            # URLs carry OPDS capabilities: never log them.
            pass

        def response(self, status, body, content_type='application/json; charset=utf-8', headers=None):
            if isinstance(body, (dict, list)):
                body = json.dumps(body, ensure_ascii=False).encode('utf-8')
            elif isinstance(body, str):
                body = body.encode('utf-8')
            self.send_response(status)
            self.send_header('Content-Type', content_type)
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('Referrer-Policy', 'no-referrer')
            self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; base-uri 'none'; frame-ancestors 'none'; form-action 'self'")
            for key, value in (headers or {}).items():
                self.send_header(key, value)
            self.end_headers()
            self.wfile.write(body)

        def host_ok(self):
            host = self.headers.get('Host', '').split(':')[0].lower()
            return host in ['localhost', '127.0.0.1'] + local_ips()

        def authorized(self):
            token = self.headers.get('X-Kindle-Token', '')
            return bool(token) and secrets.compare_digest(token, app.config['token'])

        def read_body(self, max_bytes=MAX_FILE):
            self.connection.settimeout(35)
            length = int(self.headers.get('Content-Length', '0'))
            if not 0 < length <= max_bytes:
                raise ValueError('The request is empty or too large.')
            body = self.rfile.read(length)
            if len(body) != length:
                raise ValueError('The file upload was interrupted.')
            return body

        def do_GET(self):
            if not self.host_ok():
                return self.response(403, {'error': 'Host not allowed.'})
            path = urlsplit(self.path).path
            if path == '/api/bootstrap':
                if self.client_address[0] != '127.0.0.1':
                    return self.response(403, {'error': 'Open the phone link shown on the PC.'})
                return self.response(200, {'token': app.config['token']})
            if path in ('/', '/index.html', '/style.css', '/ui.js'):
                target = ROOT / 'static' / ('index.html' if path == '/' else path.lstrip('/'))
                return self.response(200, target.read_bytes(), mimetypes.guess_type(target)[0] or 'text/plain')
            if path == f'/opds/{app.config["catalog_token"]}':
                return self.response(200, app.feed(), 'application/atom+xml;profile=opds-catalog;kind=acquisition')
            match = re.fullmatch(r'/download/([^/]+)/([a-f0-9]{16})', path)
            if match and secrets.compare_digest(match[1], app.config['catalog_token']):
                try:
                    b = app.get_book(match[2])
                    body = (app.data / 'books' / b['filename']).read_bytes()
                    return self.response(200, body, 'application/epub+zip', {'Content-Disposition': 'attachment; filename="book.epub"'})
                except ValueError:
                    return self.response(404, {'error': 'Reading no encontrada.'})
            if path.startswith('/api/'):
                if not self.authorized():
                    return self.response(401, {'error': 'Unauthorized. Open the app from the PC.'})
                if path == '/api/info':
                    return self.response(200, app.info())
                if path == '/api/books':
                    with app.lock:
                        return self.response(200, [dict(b) for b in app.books])
            return self.response(404, {'error': 'Route not found.'})

        def do_POST(self):
            if not self.host_ok() or not self.authorized():
                return self.response(403, {'error': 'Unauthorized.'})
            path = urlsplit(self.path).path
            # Restrict device setup to the PC. Phone can prepare and send readings.
            if path.startswith('/api/kindle/') and self.client_address[0] != '127.0.0.1':
                return self.response(403, {'error': 'Configure the Kindle from the PC.'})
            try:
                if path == '/api/upload':
                    result = app.add_epub(self.read_body())
                else:
                    data = json.loads(self.read_body(16384))
                    if path == '/api/url':
                        result = app.add_url(str(data.get('url', '')))
                    elif path == '/api/kindle/test':
                        result = app.test_key()
                    elif re.fullmatch(r'/api/books/[a-f0-9]{16}/send', path):
                        result = app.send(path.split('/')[3])
                    elif re.fullmatch(r'/api/books/[a-f0-9]{16}/calibre', path):
                        if self.client_address[0] != '127.0.0.1':
                            raise ValueError('Open Calibre from the PC.')
                        b = app.get_book(path.split('/')[3])
                        if not app.calibre.is_file():
                            raise ValueError('Calibre was not found.')
                        subprocess.Popen([str(app.calibre), str(app.data / 'books' / b['filename'])])
                        result = {'message': 'EPUB opened in Calibre to add it to your library.'}
                    else:
                        return self.response(404, {'error': 'Route not found.'})
                return self.response(200, result)
            except paramiko.AuthenticationException:
                return self.response(400, {'error': 'KOReader rejected authentication. Check the installed public key and SSH settings.'})
            except (socket.timeout, TimeoutError):
                return self.response(400, {'error': 'Connection timed out. Check Wi-Fi, IP, port and that the Kindle is awake.'})
            except Exception as e:
                return self.response(400, {'error': str(e)[:600]})
    return Handler
