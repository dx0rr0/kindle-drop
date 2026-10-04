"""USB checks, a challenge-bound KUAL report, and guarded public key setup."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import time

from .app import atomic_json, fingerprint, paramiko

ASSETS = Path(__file__).parent / 'diagnostic'
EXTENSION = 'extensions/kindle-drop-check'
REPORT = 'kindle-drop-report.txt'
MAX_AGE = 24 * 60 * 60
FILES = ('system/version.txt', 'koreader/git-rev', 'koreader/reader.lua',
         'koreader/dropbear', 'koreader/sftp-server', 'koreader/plugins/SSH.koplugin/main.lua')


def digest(data):
    return hashlib.sha256(data).hexdigest()


def child(mount, relative):
    root = Path(mount).resolve()
    path = (root / relative).resolve()
    if path == root or not path.is_relative_to(root):
        raise ValueError('A Kindle path resolves outside the selected USB mount.')
    return path


def read_small(path, limit=1024 * 1024):
    with path.open('rb') as stream:
        data = stream.read(limit + 1)
    if len(data) > limit:
        raise ValueError(f'Unexpectedly large file: {path.name}')
    return data


def enabled_setting(text, name):
    if name not in text:
        return False  # KOReader's documented default is disabled.
    matches = re.findall(r'\[\s*["\']' + re.escape(name) + r'["\']\s*\]\s*=\s*(true|false)\b', text)
    if len(matches) != 1:
        raise ValueError(f'Cannot determine {name}. Disable it in KOReader and retry.')
    return matches[0] == 'true'


def inspect_usb(mount):
    hashes = {}
    for relative in FILES:
        path = child(mount, relative)
        if not path.is_file():
            raise ValueError(f'Missing prerequisite: {relative}. See docs/setup.md.')
        # Binary files are bounded too; hash without executing Kindle code on the PC.
        hashes[relative] = digest(read_small(path, 32 * 1024 * 1024))
    version = read_small(child(mount, 'system/version.txt')).decode('utf-8', 'replace')
    match = re.search(r'Kindle\s+(5\.[\d.]+)', version)
    if not match:
        raise ValueError('The selected mount is not a recognized Kindle USB volume.')
    firmware = match[1].rstrip('.')
    if tuple(map(int, firmware.split('.')[:2])) < (5, 11):
        raise ValueError('This MVP verifies renamed OTA binaries on firmware 5.11+. Older OTA methods are unsupported.')
    plugin = read_small(child(mount, 'koreader/plugins/SSH.koplugin/main.lua')).decode('utf-8', 'replace')
    if 'settings/SSH/authorized_keys' not in plugin or 'SSH_allow_no_password' not in plugin:
        raise ValueError('The KOReader SSH plugin is incompatible with this setup method.')
    settings = child(mount, 'koreader/settings.reader.lua')
    if settings.is_file():
        text = read_small(settings).decode('utf-8', 'replace')
        if enabled_setting(text, 'SSH_allow_no_password'):
            raise ValueError('Disable Login without password in KOReader before setup.')
        if enabled_setting(text, 'SSH_autostart'):
            raise ValueError('Disable Start SSH server automatically in KOReader before setup.')
    return {'firmware': firmware, 'files': hashes}


def prepare(mount, data):
    """Write only the KUAL diagnostic and its nonce; never create a key."""
    snapshot = inspect_usb(mount)
    target = child(mount, EXTENSION)
    challenge = secrets.token_hex(32)
    target.mkdir(parents=True, exist_ok=True)
    for name in ('config.xml', 'menu.json', 'check.sh'):
        content = (ASSETS / name).read_bytes()
        child(mount, EXTENSION + '/' + name).write_bytes(content)
    script = child(mount, EXTENSION + '/check.sh')
    script.chmod(0o755)
    child(mount, EXTENSION + '/challenge.txt').write_text(challenge + '\n', encoding='ascii', newline='\n')
    snapshot.update(challenge=challenge, prepared_at=time.time(), checker=digest((ASSETS / 'check.sh').read_bytes()))
    data = Path(data)
    data.mkdir(parents=True, exist_ok=True)
    atomic_json(data / 'pending-check.json', snapshot)
    return {'message': 'Diagnostic installed. Safely eject USB, run KUAL > Kindle Drop > Check prerequisites, reconnect USB, then run make verify.',
            'key_created': False}


def parse_report(raw):
    result = {}
    for line in raw.decode('ascii', 'strict').splitlines():
        key, separator, value = line.partition('=')
        if not separator or not re.fullmatch(r'[a-z_]+', key) or key in result:
            raise ValueError('Malformed or duplicate diagnostic report fields.')
        result[key] = value
    return result


def verify(mount, data):
    data = Path(data)
    pending = data / 'pending-check.json'
    if not pending.is_file():
        raise ValueError('Run make check MOUNT=... first. A previous personal OTA report is not sufficient.')
    expected = json.loads(read_small(pending))
    age = time.time() - expected['prepared_at']
    if not 0 <= age <= MAX_AGE:
        raise ValueError('The diagnostic challenge expired. Run make check again.')
    current = inspect_usb(mount)
    if current['files'] != expected['files'] or current['firmware'] != expected['firmware']:
        raise ValueError('The Kindle firmware or KOReader files changed. Run make check again.')
    raw = read_small(child(mount, REPORT), 16 * 1024)
    report = parse_report(raw)
    required = {'schema': '1', 'challenge': expected['challenge'], 'uid': '0', 'ota': 'renamed',
                'ota_processes': 'none', 'pending_updates': 'none', 'dropbear_executable': 'yes',
                'sftp_executable': 'yes', 'ssh_bypass': 'off', 'ssh_autostart': 'off', 'storage_writable': 'yes',
                'ssh_runtime_bypass': 'off', 'version_sha': expected['files']['system/version.txt'],
                'plugin_sha': expected['files']['koreader/plugins/SSH.koplugin/main.lua'],
                'dropbear_sha': expected['files']['koreader/dropbear'],
                'sftp_sha': expected['files']['koreader/sftp-server'], 'checker_sha': expected['checker']}
    failures = [f'{key}: expected {value}, found {report.get(key, "missing")}' for key, value in required.items()
                if report.get(key) != value]
    if not report.get('free_kb', '').isdigit() or int(report['free_kb']) < 65536:
        failures.append('At least 64 MB of free Kindle storage is required.')
    if failures:
        raise ValueError('Prerequisites failed:\n' + '\n'.join(failures))
    evidence = {**expected, 'verified_at': time.time(), 'report_sha': digest(raw), 'checks': report}
    atomic_json(data / 'verified-check.json', evidence)
    return evidence


def install_key(mount, data):
    """Reverify immediately before key creation. Append only the public portion."""
    data = Path(data)
    evidence = verify(mount, data)
    keys = child(mount, 'koreader/settings/SSH/authorized_keys')
    existing = read_small(keys) if keys.exists() else b''
    # Validate every write target before generating a private key.
    keys.parent.mkdir(parents=True, exist_ok=True)
    private = data / 'kindle_key'
    if private.exists():
        key = paramiko.ECDSAKey.from_private_key_file(str(private))
    else:
        key = paramiko.ECDSAKey.generate(bits=256)
        # Exclusive creation and 0600 on Unix; Windows inherits the user's directory ACL.
        fd = os.open(private, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'w', encoding='ascii', newline='\n') as stream:
            key.write_private_key(stream)
    public = f'{key.get_name()} {key.get_base64()} kindle-drop\n'.encode('ascii')
    (data / 'kindle_key.pub').write_bytes(public)
    identity = public.split()[:2]
    present = any(line.split()[:2] == identity for line in existing.splitlines())
    if not present:
        backup = data / 'backups'
        backup.mkdir(exist_ok=True)
        (backup / f'authorized_keys-{time.time_ns()}.before').write_bytes(existing)
        if (read_small(keys) if keys.exists() else b'') != existing:
            raise ValueError('authorized_keys changed during setup. Retry; existing keys were not replaced.')
        # Appending preserves every existing key and avoids replacing symlinks/files.
        with keys.open('ab') as stream:
            stream.write((b'\n' if existing and not existing.endswith(b'\n') else b'') + public)
    installed = read_small(keys)
    if not installed.startswith(existing) or not any(line.split()[:2] == identity for line in installed.splitlines()):
        raise ValueError('Public key installation could not be verified.')
    atomic_json(data / 'setup.json', {**evidence, 'key_fingerprint': fingerprint(key), 'installed_at': time.time()})
    return {'message': 'Public key installed; existing keys preserved. The private key stays on this PC.',
            'public_key_fingerprint': fingerprint(key), 'next': 'Safely eject USB. Start KOReader SSH with Login without password disabled, then run make pair HOST=...'}


def remote_bytes(sftp, path, maximum=1024 * 1024):
    with sftp.open(path, 'rb') as stream:
        raw = stream.read(maximum + 1)
    if len(raw) > maximum:
        raise ValueError('Unexpectedly large remote check file.')
    return raw


def remote_exists(sftp, path):
    try:
        sftp.stat(path)
        return True
    except FileNotFoundError:
        return False


def check_remote(client, data, evidence=None):
    """Recheck firmware, executables, OTA state and actual SSH arguments before each send."""
    if evidence is None:
        setup = Path(data) / 'setup.json'
        if not setup.is_file():
            raise ValueError('The verified key setup is missing. Run make key after make verify.')
        evidence = json.loads(read_small(setup))
    with client.open_sftp() as sftp:
        sftp.get_channel().settimeout(15)
        for relative in ('system/version.txt', 'koreader/dropbear', 'koreader/sftp-server', 'koreader/plugins/SSH.koplugin/main.lua'):
            if digest(remote_bytes(sftp, '/mnt/us/' + relative, 32 * 1024 * 1024)) != evidence['files'][relative]:
                raise ValueError('The Kindle firmware or SSH installation changed. Repeat the USB diagnostic.')
        for name in ('otaupd', 'otav3'):
            path = '/usr/bin/' + name
            if remote_exists(sftp, path) or not remote_exists(sftp, path + '.bck'):
                raise ValueError('Live OTA protection is missing or inconclusive. Stop and repeat the diagnostic.')
        pending = [name for name in sftp.listdir('/mnt/us') if
                   (name.startswith('update') and name.endswith('.bin')) or name == 'update.bin.tmp.partial']
        if pending:
            raise ValueError('A pending Kindle update was found. Stop and inspect it before sending.')
        pid = remote_bytes(sftp, '/tmp/dropbear_koreader.pid', 32).strip().decode('ascii')
        if not pid.isdigit():
            raise ValueError('Cannot identify the running KOReader SSH server.')
        args = remote_bytes(sftp, f'/proc/{pid}/cmdline', 4096).split(b'\0')
        if not args or b'dropbear' not in args[0] or any(arg.startswith(b'-') and b'n' in arg[1:] for arg in args):
            raise ValueError('The SSH server is running with password bypass or unknown arguments.')
        # Read process names to confirm the updater is not already running.
        for entry in sftp.listdir('/proc'):
            if entry.isdigit():
                try:
                    name = remote_bytes(sftp, f'/proc/{entry}/comm', 256).strip()
                except FileNotFoundError:
                    continue  # Processes can disappear while being inspected.
                if name in (b'otaupd', b'otav3'):
                    raise ValueError('A Kindle OTA updater is running.')
