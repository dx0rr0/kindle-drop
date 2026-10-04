"""Make-friendly command line interface; all setup remains explicit."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import glob
import importlib.metadata
import json
import os
from pathlib import Path
import socket
import sys
import threading
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
import webbrowser

from .app import App, ThreadingHTTPServer, atomic_json, handler_for, local_ips, validate_kindle
from . import prerequisites


@contextmanager
def state_lock(data):
    """Prevent concurrent processes from modifying the same queue or setup."""
    data.mkdir(parents=True, exist_ok=True)
    stream = (data / 'process.lock').open('a+b')
    stream.seek(0)
    if os.name == 'nt':
        import msvcrt
        if not os.fstat(stream.fileno()).st_size:
            stream.write(b'0')
            stream.flush()
        stream.seek(0)
        try:
            msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            stream.close()
            raise ValueError('Kindle Drop is using this state. Stop the web server before changing setup.')
    else:
        import fcntl
        try:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            stream.close()
            raise ValueError('Kindle Drop is using this state. Stop the web server before changing setup.')
    try:
        yield
    finally:
        stream.close()


def api(base, token, path, body=None, content_type='application/json'):
    headers = {'X-Kindle-Token': token}
    if body is not None:
        headers['Content-Type'] = content_type
    with urlopen(Request(base + path, data=body, headers=headers), timeout=150) as response:
        return json.load(response)


def running_server(data):
    meta = data / 'server.json'
    config = data / 'config.json'
    if not meta.is_file() or not config.is_file():
        return None
    record = json.loads(meta.read_text(encoding='utf-8'))
    port = int(record['port'])
    if not 1 <= port <= 65535:
        raise ValueError('Invalid saved web server port.')
    base = f'http://127.0.0.1:{port}'
    token = json.loads(config.read_text(encoding='utf-8'))['token']
    try:
        api(base, token, '/api/info')
    except (URLError, TimeoutError, OSError):
        return None
    return base, token


def inputs_from_make(args):
    if args:
        values = args
    elif os.environ.get('KD_INPUT'):
        # A single INPUT preserves spaces and URL query strings without shell interpolation.
        values = [os.environ['KD_INPUT']]
    else:
        goals = os.environ.get('KD_GOALS', '').split()
        values = goals[1:] if goals and goals[0] == 'send' else []
    if not values:
        raise ValueError('Provide a URL or EPUB: make send INPUT="https://example.org/article" or make send books/*.epub')
    expanded = []
    for value in values:
        if value.startswith(('http://', 'https://')):
            expanded.append(value)
            continue
        matches = sorted(glob.glob(value)) if glob.has_magic(value) else [value]
        if not matches:
            raise ValueError(f'No files match: {value}')
        for path in matches:
            file = Path(path)
            if file.suffix.lower() != '.epub' or not file.is_file():
                raise ValueError(f'Expected an existing .epub file: {path}')
            expanded.append(str(file))
    return expanded


def send(data, values):
    server = running_server(data)
    def transfer(app=None):
        for value in values:
            is_url = value.startswith(('http://', 'https://'))
            if server:
                base, token = server
                book = api(base, token, '/api/url', json.dumps({'url': value}).encode()) if is_url else api(
                    base, token, '/api/upload', prerequisites.read_small(Path(value), 40 * 1024 * 1024), 'application/epub+zip')
                result = api(base, token, f'/api/books/{book["id"]}/send', b'{}')
            else:
                book = app.add_url(value) if is_url else app.add_epub(prerequisites.read_small(Path(value), 40 * 1024 * 1024))
                result = app.send(book['id'])
            print(result['message'])
    if server:
        transfer()
    else:
        with state_lock(data):
            transfer(App(data))


def doctor():
    from .app import find_calibre
    checks = {'Python 3.12+': sys.version_info >= (3, 12), 'Calibre ebook-convert': find_calibre('ebook-convert').is_file()}
    for package in ('paramiko', 'trafilatura', 'Pillow'):
        try:
            checks[package + ' ' + importlib.metadata.version(package)] = True
        except importlib.metadata.PackageNotFoundError:
            checks[package] = False
    for name, ok in checks.items():
        print(f'{"PASS" if ok else "FAIL"}  {name}')
    if not all(checks.values()):
        raise ValueError('Install the missing PC prerequisites before Kindle setup. See docs/setup.md.')


def pair(app, host, port, expected, destination=None):
    options = {'host': host, 'port': port}
    chosen_destination = destination or app.config['kindle'].get('inbox')
    if chosen_destination:
        options['inbox'] = chosen_destination
    cfg = validate_kindle(options)
    if not (app.data / 'setup.json').is_file():
        raise ValueError('Complete make key with a verified USB diagnostic before pairing.')
    observed = app.probe(cfg)['fingerprint']
    if not expected:
        print('Observed SSH host fingerprint:', observed)
        print('This is not saved yet. Verify the IP on your Kindle on a trusted LAN, then run:')
        print(f'make pair HOST={host} SSH_PORT={port} FINGERPRINT={observed}')
        return
    if observed != expected:
        raise ValueError('The supplied fingerprint does not match the SSH server.')
    cfg['fingerprint'] = expected
    previous = app.config['kindle']
    app.config['kindle'] = cfg
    try:
        app.test_key()
    except Exception:
        app.config['kindle'] = previous
        raise
    app.save_kindle(cfg)
    print('Pairing saved. Public key authentication and live prerequisites passed.')


def open_web(data, port, lan, no_browser, alias_port=None):
    existing = running_server(data)
    if existing:
        if not no_browser:
            webbrowser.open(existing[0])
        print('Kindle Drop is already running:', existing[0])
        return
    with state_lock(data):
        app = App(data, port, lan)
        server = ThreadingHTTPServer(('0.0.0.0' if lan else '127.0.0.1', port), handler_for(app))
        alias = None
        if alias_port is not None:
            try:
                alias = ThreadingHTTPServer(('127.0.0.1', alias_port), handler_for(app))
            except Exception:
                server.server_close()
                raise
            threading.Thread(target=alias.serve_forever, daemon=True).start()
        atomic_json(data / 'server.json', {'port': port})
        base = f'http://127.0.0.1:{port}'
        print('Kindle Drop:', base, flush=True)
        if lan:
            print('Phone links (private; keep these to yourself):', flush=True)
            for host in local_ips():
                print(f'http://{host}:{port}/#{app.config["token"]}', flush=True)
        if not no_browser:
            threading.Timer(0.5, lambda: webbrowser.open(base)).start()
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            server.server_close()
            if alias is not None:
                alias.shutdown()
                alias.server_close()
            (data / 'server.json').unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(description='Send URLs and EPUBs to a Kindle running KOReader.')
    parser.add_argument('--data', type=Path, default=Path(os.environ.get('KINDLE_DROP_DATA', '.kindle-drop')))
    sub = parser.add_subparsers(dest='command', required=True)
    sub.add_parser('doctor')
    sub.add_parser('noop', help=argparse.SUPPRESS)
    for command in ('check', 'verify', 'key'):
        p = sub.add_parser(command)
        p.add_argument('--mount', type=Path, default=os.environ.get('KD_MOUNT'))
    p = sub.add_parser('pair')
    p.add_argument('--host', default=os.environ.get('KD_HOST'))
    p.add_argument('--port', type=int, default=int(os.environ.get('KD_SSH_PORT', '2222')))
    p.add_argument('--fingerprint', default=os.environ.get('KD_FINGERPRINT', ''))
    p.add_argument('--destination', default=os.environ.get('KD_DESTINATION') or None)
    sub.add_parser('install-refresh', help='Install the optional KOReader file browser auto-refresh companion')
    sub.add_parser('test')
    p = sub.add_parser('send')
    p.add_argument('inputs', nargs='*')
    p = sub.add_parser('open')
    p.add_argument('--port', type=int, default=int(os.environ.get('KD_WEB_PORT', '8765')))
    p.add_argument('--lan', action='store_true', default=os.environ.get('KD_LAN') == '1')
    p.add_argument('--no-browser', action='store_true')
    p.add_argument('--alias-port', type=int, help='Serve the same profile on a second loopback port')
    args = parser.parse_args()
    try:
        if args.command == 'noop':
            return 0
        if args.command == 'doctor':
            doctor()
        elif args.command == 'open':
            open_web(args.data, args.port, args.lan, args.no_browser, args.alias_port)
        elif args.command == 'install-refresh':
            server = running_server(args.data)
            if server:
                result = api(*server, '/api/kindle/install-refresh', b'{}')
            else:
                with state_lock(args.data):
                    result = App(args.data).install_refresh()
            print(result['message'])
        elif args.command == 'send':
            send(args.data, inputs_from_make(args.inputs))
        else:
            with state_lock(args.data):
                if args.command in ('check', 'verify', 'key'):
                    if not args.mount:
                        raise ValueError('Select the Kindle USB mount explicitly: MOUNT=D:/ or --mount /media/you/Kindle')
                    action = {'check': prerequisites.prepare, 'verify': prerequisites.verify, 'key': prerequisites.install_key}[args.command]
                    result = action(args.mount, args.data)
                    if args.command == 'verify':
                        print('PASS  All supported Kindle prerequisite checks. Key creation is now permitted for this device.')
                    else:
                        print(json.dumps(result, indent=2))
                elif args.command == 'pair':
                    pair(App(args.data), args.host, args.port, args.fingerprint, args.destination)
                elif args.command == 'test':
                    print(App(args.data).test_key()['message'])
        return 0
    except HTTPError as error:
        try:
            message = json.load(error)['error']
        except (ValueError, KeyError):
            message = f'HTTP {error.code}'
        print('Error:', message, file=sys.stderr)
    except (ValueError, OSError, KeyError, json.JSONDecodeError) as error:
        print('Error:', error, file=sys.stderr)
    except Exception as error:
        print(f'Error: {type(error).__name__}: {error}', file=sys.stderr)
    return 1
