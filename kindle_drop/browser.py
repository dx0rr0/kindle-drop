"""Render web-page graphics (scripted charts, CSS charts) to images with a local headless browser.

Documents are loaded as data: URLs (an opaque origin that cannot open local files) with
networking disabled: a Content-Security-Policy that only allows inline content and data: URIs,
a dead proxy for anything the browser itself would fetch, and no WebRTC. Chromium-based
browsers (Edge, Chrome, Chromium) are driven over the DevTools protocol. The browser is
optional: without one, articles keep their text and say where a graphic was.
"""
import base64
import json
import os
from pathlib import Path
import shutil
import socket
import struct
import subprocess
import tempfile
import time

OFFLINE_CSP = ("default-src 'none'; script-src 'unsafe-inline' 'unsafe-eval'; "
               "style-src 'unsafe-inline'; img-src data: blob:; font-src data:; media-src data: blob:")
VIEWPORT = 540  # CSS pixels: about a phone, the width web charts are designed to stay legible at
SCALE = 2  # device pixels per CSS pixel: 1080 px wide images, the Kindle's screen width
SETTLE = 0.5  # seconds for charts to draw once in view (with reduced motion, without transitions)
MAX_HEIGHT = 6000  # CSS pixels; taller renders are cut


def find_browsers():
    """Installed Chromium-based browsers, most likely to work first: Edge ships with Windows;
    elsewhere Chrome and Chromium are the common installs."""
    candidates = []
    for base in (os.environ.get('PROGRAMFILES(X86)'), os.environ.get('PROGRAMFILES'), os.environ.get('LOCALAPPDATA')):
        if base:
            candidates += [Path(base) / 'Microsoft/Edge/Application/msedge.exe',
                           Path(base) / 'Google/Chrome/Application/chrome.exe']
    candidates += [Path('/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'),
                   Path('/Applications/Chromium.app/Contents/MacOS/Chromium'),
                   Path('/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge')]
    for name in ('google-chrome', 'google-chrome-stable', 'chromium', 'chromium-browser', 'chrome', 'msedge', 'microsoft-edge'):
        found = shutil.which(name)
        if found:
            candidates.append(Path(found))
    found = []
    for path in candidates:
        if path.is_file() and path.resolve() not in [p.resolve() for p in found]:
            found.append(path)
    return found


def find_browser():
    return next(iter(find_browsers()), None)


class WebSocket:
    """The client half of RFC 6455, enough for the DevTools protocol on loopback."""

    def __init__(self, port, path, timeout):
        self.sock = socket.create_connection(('127.0.0.1', port), timeout=timeout)
        key = base64.b64encode(os.urandom(16)).decode()
        self.sock.sendall((f'GET {path} HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nUpgrade: websocket\r\n'
                           f'Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n').encode())
        response = b''
        while b'\r\n\r\n' not in response:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise ConnectionError('The browser closed the DevTools connection.')
            response += chunk
        if not response.startswith(b'HTTP/1.1 101'):
            raise ConnectionError('The browser refused the DevTools connection.')
        self.buffer = response.partition(b'\r\n\r\n')[2]

    def read(self, count):
        while len(self.buffer) < count:
            chunk = self.sock.recv(1 << 20)
            if not chunk:
                raise ConnectionError('The browser closed the DevTools connection.')
            self.buffer += chunk
        data, self.buffer = self.buffer[:count], self.buffer[count:]
        return data

    def send(self, text, opcode=1):
        payload = text.encode() if isinstance(text, str) else text
        length = len(payload)
        header = bytes([0x80 | opcode])
        if length < 126:
            header += bytes([0x80 | length])
        elif length < 1 << 16:
            header += bytes([0x80 | 126]) + struct.pack('>H', length)
        else:
            header += bytes([0x80 | 127]) + struct.pack('>Q', length)
        mask = os.urandom(4)
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload)) if length < 4096 else \
            (int.from_bytes(payload, 'big') ^ int.from_bytes((mask * (length // 4 + 1))[:length], 'big')).to_bytes(length, 'big')
        self.sock.sendall(header + mask + masked)

    def receive(self):
        message = b''
        while True:
            first, second = self.read(2)
            opcode, length = first & 0x0F, second & 0x7F
            if length == 126:
                length = struct.unpack('>H', self.read(2))[0]
            elif length == 127:
                length = struct.unpack('>Q', self.read(8))[0]
            payload = self.read(length)
            if opcode == 8:
                raise ConnectionError('The browser closed the DevTools connection.')
            if opcode == 9:
                self.send(payload, opcode=10)
                continue
            if opcode in (0, 1, 2):
                message += payload
                if first & 0x80:
                    return message.decode()

    def close(self):
        self.sock.close()


class Browser:
    """A headless browser process with one DevTools connection; use as a context manager."""

    def __init__(self, executable, timeout=20):
        self.timeout = timeout
        self.profile = tempfile.TemporaryDirectory(prefix='kindle-drop-browser-')
        self.log = tempfile.TemporaryFile()  # the browser's own error output, for failure messages
        self.process = subprocess.Popen(
            [str(executable), '--headless=new', '--remote-debugging-port=0', f'--user-data-dir={self.profile.name}',
             '--no-first-run', '--no-default-browser-check', '--disable-extensions', '--disable-sync',
             '--disable-background-networking', '--disable-component-update', '--disable-default-apps',
             '--disable-renderer-backgrounding', '--disable-background-timer-throttling',
             '--disable-backgrounding-occluded-windows',
             '--disable-features=Translate,OptimizationHints,MediaRouter', '--mute-audio', '--hide-scrollbars',
             '--proxy-server=http://127.0.0.1:9', '--proxy-bypass-list=<-loopback>',
             '--force-webrtc-ip-handling-policy=disable_non_proxied_udp', 'about:blank'],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=self.log)
        try:
            marker = Path(self.profile.name) / 'DevToolsActivePort'
            deadline = time.monotonic() + timeout
            while not marker.is_file() or len(marker.read_text().split()) < 2:
                if self.process.poll() is not None or time.monotonic() > deadline:
                    self.log.seek(0)
                    detail = self.log.read()[-600:].decode('utf-8', 'replace').strip()
                    raise RuntimeError('The browser did not start' + (f': {detail}' if detail else '.'))
                time.sleep(0.02)
            port, path = marker.read_text().split()[:2]
            self.socket = WebSocket(int(port), path, timeout)
        except BaseException:
            self.close()
            raise
        self.next_id = 0

    def calls(self, commands):
        """Send [(method, session, params)] at once; the browser works on them in parallel.

        Returns their results in order, with a RuntimeError in place of each failed command.
        """
        pending = {}
        for method, session, params in commands:
            self.next_id += 1
            message = {'id': self.next_id, 'method': method, 'params': params}
            if session:
                message['sessionId'] = session
            pending[self.next_id] = method
            self.socket.send(json.dumps(message))
        first, results = self.next_id - len(commands) + 1, {}
        while len(results) < len(commands):
            reply = json.loads(self.socket.receive())
            if reply.get('id') in pending:
                error = reply.get('error')
                results[reply['id']] = RuntimeError(f"{pending[reply['id']]}: {error.get('message')}") if error \
                    else reply.get('result', {})
        return [results[first + number] for number in range(len(commands))]  # events are ignored

    def call(self, method, session=None, **params):
        result = self.calls([(method, session, params)])[0]
        if isinstance(result, Exception):
            raise result
        return result

    def wait_loaded(self, sessions):
        """The sessions whose own data: document finished loading within the timeout.

        Polled, not taken from load events: a new window's about:blank fires one too, and on
        a busy machine it can arrive after navigation started.
        """
        deadline, waiting, ready = time.monotonic() + self.timeout, list(sessions), set()
        while waiting and time.monotonic() < deadline:
            states = self.evaluate([(session, READY) for session in waiting])
            ready.update(session for session, state in zip(waiting, states) if state)
            waiting = [session for session in waiting if session not in ready]
            if waiting:
                time.sleep(0.05)
        return [session for session in sessions if session in ready]

    def evaluate(self, scripts):
        """The JSON value of each (session, expression), all at once (None where it failed)."""
        values = []
        for result in self.calls([('Runtime.evaluate', session, {'expression': expression, 'returnByValue': True,
                                                                  'awaitPromise': True}) for session, expression in scripts]):
            try:
                values.append(json.loads(result['result']['value']) if 'exceptionDetails' not in result else None)
            except (TypeError, KeyError, ValueError):
                values.append(None)
        return values

    def open(self, urls, width=VIEWPORT):
        """Load each URL in its own window, all at once; returns their sessions."""
        # Separate windows: a background tab would not draw (no animation frames) or produce screenshots.
        targets = self.calls([('Target.createTarget', None, {'url': 'about:blank', 'newWindow': True})] * len(urls))
        sessions = [r['sessionId'] for r in self.calls([('Target.attachToTarget', None, {'targetId': t['targetId'], 'flatten': True})
                                                         for t in targets])]
        media = [{'name': 'prefers-color-scheme', 'value': 'light'}, {'name': 'prefers-reduced-motion', 'value': 'reduce'}]
        self.calls([command for session in sessions for command in (
            ('Emulation.setDeviceMetricsOverride', session, {'width': width, 'height': 800, 'deviceScaleFactor': SCALE, 'mobile': False}),
            ('Emulation.setEmulatedMedia', session, {'features': media}))])
        self.calls([('Page.navigate', session, {'url': url}) for session, url in zip(sessions, urls)])
        return sessions

    def close(self):
        if getattr(self, 'socket', None):
            self.socket.close()
        if self.process.poll() is None:
            self.process.kill()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass
        self.log.close()
        try:
            self.profile.cleanup()
        except OSError:
            pass  # a browser helper still holds a file; the temporary folder is left for the OS

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


def start(executables):
    """The first browser that starts. One may refuse to, for instance Linux Edge when its
    sandbox helper is not installed setuid root; it is never run without its sandbox."""
    errors = []
    for executable in executables:
        try:
            return Browser(executable)
        except RuntimeError as error:
            errors.append(f'{executable}: {error}')
    raise RuntimeError('; '.join(errors))


# Still frames, without the controls a page puts on its charts ("Download PNG") that mean nothing on paper.
STILL = ('<style>*, *::before, *::after { transition: none !important; animation: none !important; }'
         ' button[aria-label*="download" i], a[download] { visibility: hidden !important; }</style>')


def offline_document(markup):
    """Prepend the offline policy (a document's own policies can only restrict it further) and stop transitions."""
    return (f'<!doctype html><meta charset="utf-8"><meta http-equiv="Content-Security-Policy" content="{OFFLINE_CSP}">'
            + STILL + markup)


# Hide each target's own "Figure 3: ..." caption (it becomes text under the image) and return it.
CAPTIONS = r"""JSON.stringify(%s.map(selector => {
  const root = selector ? document.querySelector(selector) : document.body;
  if (!root) return null;
  const own = root.tagName === 'FIGURE' && root.querySelector(':scope > figcaption');
  if (own) {
    // innerText keeps the breaks the stylesheet puts between parts ("FIG 2", then the title).
    const text = (own.innerText || own.textContent).replace(/\s+/g, ' ').trim();
    own.style.setProperty('display', 'none', 'important');
    return text;
  }
  const pattern = /^\s*(figure|fig\.|table|chart|diagram)\s*\d+[a-z]?\s*[:.–—-]/i;
  for (const node of root.querySelectorAll('figcaption, caption, p, div, span, small')) {
    const text = node.textContent.replace(/\s+/g, ' ').trim();
    if (pattern.test(text) && text.length < 700 && ![...node.children].some(c => pattern.test(c.textContent.trim()))) {
      node.style.setProperty('display', 'none', 'important');
      return text;
    }
  }
  return '';
}))"""
RECTS = r"""JSON.stringify(%s.map(selector => {
  if (!selector) return [0, 0, document.documentElement.scrollWidth, document.documentElement.scrollHeight];
  const node = document.querySelector(selector);
  if (!node) return null;
  const box = node.getBoundingClientRect();
  return [box.left + scrollX, box.top + scrollY, box.width, box.height];
}))"""


def render(documents, executable=None):
    """Render [(markup, selectors)]: for each document, a (PNG bytes, caption) per selector,
    False for an element the page does not display, or None when it could not be rendered.

    A selector of None captures the whole document; otherwise the first element it matches.
    All documents load at once, each in its own window, and each step runs on all of them together.
    """
    executables = [executable] if executable else find_browsers()
    if not executables or not documents:
        return [[None] * len(selectors) for _, selectors in documents]
    with start(executables) as browser:
        sessions = browser.open(['data:text/html;charset=utf-8;base64,' + base64.b64encode(offline_document(markup).encode()).decode()
                                 for markup, _ in documents])
        loaded = browser.wait_loaded(sessions)
        wanted = {session: selectors for session, (_, selectors) in zip(sessions, documents)}
        captions = dict(zip(loaded, browser.evaluate([(session, CAPTIONS % json.dumps(wanted[session])) for session in loaded])))
        sizes = dict(zip(loaded, browser.evaluate([(session, PAGE_SIZE) for session in loaded])))
        # A viewport as tall as the page: charts often draw only once scrolled into view.
        browser.calls([('Emulation.setDeviceMetricsOverride', session, {
            'width': VIEWPORT, 'height': max(1, min(sizes[session][1], MAX_HEIGHT)) if sizes[session] else 800,
            'deviceScaleFactor': SCALE, 'mobile': False}) for session in loaded])
        time.sleep(SETTLE)
        # The page must still be this document: never capture a page it navigated to.
        protocols = browser.evaluate([(session, 'JSON.stringify(location.protocol)') for session in loaded])
        loaded = [session for session, protocol in zip(loaded, protocols) if protocol == 'data:' and captions[session] is not None]
        boxes = {session: box or [None] * len(wanted[session])
                 for session, box in zip(loaded, browser.evaluate([(session, RECTS % json.dumps(wanted[session])) for session in loaded]))}
        results = {session: [None] * len(wanted[session]) for session in sessions}
        pending = []
        for session in loaded:
            for number, box in enumerate(boxes[session]):
                if box and box[2] >= 8 and box[3] >= 8:
                    pending.append((session, number, box))
                elif box:
                    results[session][number] = False  # not displayed: the page's CSS hides this variant
        while pending:
            # Windows capture in parallel, each one capture at a time: concurrent captures of a
            # window would resize its viewport under each other.
            batch, later, busy = [], [], set()
            for shot in pending:
                (later if shot[0] in busy else batch).append(shot)
                busy.add(shot[0])
            images = browser.calls([('Page.captureScreenshot', session, {
                'format': 'png', 'optimizeForSpeed': True, 'captureBeyondViewport': True,
                'clip': {'x': x, 'y': y, 'width': width, 'height': min(height, MAX_HEIGHT), 'scale': 1}})
                for session, _, (x, y, width, height) in batch])
            for (session, number, _), image in zip(batch, images):
                if not isinstance(image, Exception):
                    results[session][number] = (base64.b64decode(image['data']), captions[session][number] or '')
            pending = later
        return [results[session] for session in sessions]


READY = "JSON.stringify(location.protocol === 'data:' && document.readyState === 'complete')"
PAGE_SIZE = 'JSON.stringify([document.documentElement.scrollWidth, document.documentElement.scrollHeight])'
