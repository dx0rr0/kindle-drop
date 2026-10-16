"""Read-only reachability check for the paired Kindle: no key use, no state changes."""
from __future__ import annotations

import os
import socket
import subprocess
import time

from .app import CREATE_NO_WINDOW, validate_kindle


def icmp_ping(host, timeout=1.5):
    """True when the device answers an ICMP echo (the Kindle is awake and on Wi-Fi)."""
    wait = ['-n', '1', '-w', str(int(timeout * 1000))] if os.name == 'nt' else ['-c', '1', '-W', str(max(1, round(timeout)))]
    try:
        return subprocess.run(['ping', *wait, host], capture_output=True, timeout=timeout + 2,
                              creationflags=CREATE_NO_WINDOW).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def ping(config, timeout=3.0):
    """Probe the paired SSH endpoint. Returns (ready, message)."""
    cfg = validate_kindle(dict(config))  # private LAN addresses only, as for sending
    host, port = cfg['host'], cfg['port']
    started = time.monotonic()
    try:
        with socket.create_connection((host, port), timeout=timeout) as sock:
            sock.settimeout(timeout)
            banner = sock.recv(256).split(b'\r\n')[0].decode('ascii', 'replace')
    except ConnectionRefusedError:
        return False, (f'The Kindle at {host} answered, but nothing listens on port {port}. '
                       'Start KOReader → Tools → Network → SSH server.')
    except OSError:
        if icmp_ping(host):
            return False, (f'The Kindle at {host} is awake on Wi-Fi, but its SSH server on port {port} is not reachable. '
                           'Start KOReader → Tools → Network → SSH server.')
        return False, (f'No answer from {host}. The Kindle may be asleep, off Wi-Fi or on a different IP '
                       '(KOReader → Tools → Network → Network info).')
    elapsed = (time.monotonic() - started) * 1000
    if not banner.startswith('SSH-'):
        return False, f'{host}:{port} answered in {elapsed:.0f} ms, but not with SSH. Check the paired port.'
    return True, f'Kindle SSH server ready at {host}:{port} ({banner}, {elapsed:.0f} ms).'
