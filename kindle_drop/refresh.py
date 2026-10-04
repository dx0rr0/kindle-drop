"""Optional KOReader companion; transfer completion is independent of UI refresh."""
from pathlib import Path
import secrets

READER = '/mnt/us/koreader'
PLUGIN = READER + '/plugins/kindle_drop_refresh.koplugin'
MARKER = READER + '/settings/kindle-drop.refresh'
ASSETS = Path(__file__).resolve().parent / 'reader_plugin'
SIGNATURE = b'-- Kindle Drop managed auto-refresh plugin v1\n'


def remote_read(sftp, path, limit=1024 * 1024):
    with sftp.open(path, 'rb') as file:
        body = file.read(limit + 1)
    if len(body) > limit:
        raise ValueError('KOReader companion file exceeds its size limit.')
    return body


def install(sftp):
    # Verify the installed APIs before installing executable reader code.
    for path, names in [
        ('frontend/apps/filemanager/filemanager.lua', [b'function FileManager:onRefresh()']),
        ('frontend/ui/widget/filechooser.lua', [b'function FileChooser:clearSortingCache()']),
        ('frontend/ui/uimanager.lua', [b'function UIManager:getTopmostVisibleWidget()', b'function UIManager:scheduleIn(', b'function UIManager:unschedule(']),
    ]:
        content = remote_read(sftp, READER + '/' + path)
        if not all(name in content for name in names):
            raise ValueError('This KOReader version does not support the companion refresh APIs.')
    assets = {name: (ASSETS / name).read_bytes().replace(b'\r\n', b'\n') for name in ('main.lua', '_meta.lua')}
    try:
        existing = remote_read(sftp, PLUGIN + '/main.lua')
    except FileNotFoundError:
        existing = None
        try:
            sftp.stat(PLUGIN)
        except FileNotFoundError:
            pass
        else:
            raise ValueError('An unrecognized plugin folder exists; it was not overwritten.')
    if existing is not None:
        if not existing.startswith(SIGNATURE):
            raise ValueError('An unrecognized plugin exists; it was not overwritten.')
        if all(remote_read(sftp, PLUGIN + '/' + name) == body for name, body in assets.items()):
            return {'message': 'Auto-refresh companion is already installed. Restart KOReader if it has not been loaded yet.'}
    suffix = secrets.token_hex(8)
    staging = READER + '/plugins/.kindle-drop-staging-' + suffix
    backup = READER + '/plugins/.kindle-drop-backup-' + suffix
    sftp.mkdir(staging)
    backed_up = False
    try:
        for name, body in assets.items():
            with sftp.open(staging + '/' + name, 'wb') as file:
                file.write(body)
            if remote_read(sftp, staging + '/' + name) != body:
                raise ValueError('Auto-refresh plugin checksum verification failed.')
        if existing is not None:
            sftp.rename(PLUGIN, backup)
            backed_up = True
        try:
            sftp.rename(staging, PLUGIN)
        except Exception:
            if backed_up:
                sftp.rename(backup, PLUGIN)
            raise
    finally:
        for name in assets:
            try:
                sftp.remove(staging + '/' + name)
            except OSError:
                pass
        try:
            sftp.rmdir(staging)
        except OSError:
            pass
    return {'message': 'Auto-refresh companion installed and verified. Restart KOReader once, then enable SSH again. Home will refresh within about five seconds of a new delivery.'}


def notify(sftp, folder):
    """Write a bounded, atomic completion marker only for our installed plugin."""
    try:
        if not remote_read(sftp, PLUGIN + '/main.lua').startswith(SIGNATURE):
            return False
    except FileNotFoundError:
        return False
    body = (secrets.token_hex(16) + '\n' + folder + '\n').encode('utf-8')
    if len(body) > 4096:
        raise ValueError('The delivery folder name is too long for auto refresh.')
    partial = MARKER + '.part-' + secrets.token_hex(8)
    try:
        with sftp.open(partial, 'wb') as file:
            file.write(body)
        # Dropbear SFTP supports the POSIX rename extension (atomic replacement).
        sftp.posix_rename(partial, MARKER)
    finally:
        try:
            sftp.remove(partial)
        except OSError:
            pass
    return True
