#!/usr/bin/env python3
"""Developer-machine OS primitives for remote-agents.

This is the ONLY module that knows which OS the operator's machine runs.
Everything platform-dependent (file locks, process identity, the RDP client,
the saved RDP credential, the WireGuard tunnel, the SMB mount, service
management) lives behind the small function surface below. Callers must not
branch on sys.platform themselves.

DEV_OS is 'macos', 'linux', or 'windows'.
"""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time

if sys.platform == 'darwin':
    DEV_OS = 'macos'
elif sys.platform == 'win32':
    DEV_OS = 'windows'
else:
    DEV_OS = 'linux'

if DEV_OS == 'windows':
    import msvcrt
else:
    import fcntl


# ---------------------------------------------------------------- processes


def acquire_lock(path):
    """Open `path` and take a nonblocking exclusive lock. Returns the open file
    (keep it referenced for the lock's lifetime). Raises BlockingIOError."""
    handle = open(path, 'a+b')
    if DEV_OS == 'windows':
        # msvcrt locks a byte range; a 0-byte file would fail with EINVAL.
        if os.fstat(handle.fileno()).st_size == 0:
            handle.write(b'0')
            handle.flush()
        handle.seek(0)
        try:
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError as exc:
            handle.close()
            raise BlockingIOError(exc.errno, exc.strerror) from exc
    else:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            handle.close()
            raise
    return handle


def process_identity(pid):
    """Start-time + command for pid, or '' if the process is gone."""
    if DEV_OS == 'windows':
        script = (f"$p=Get-Process -Id {int(pid)} -ErrorAction SilentlyContinue;"
                  "if($p){$p.StartTime.ToString('o')+' '+$p.Path}")
        out = subprocess.run(['powershell', '-NoProfile', '-NonInteractive', '-Command', script],
                             capture_output=True, text=True).stdout.strip()
        return out
    result = subprocess.run(['ps', '-p', str(pid), '-o', 'lstart=', '-o', 'command='],
                            capture_output=True, text=True)
    return result.stdout.strip()


def popen_detached_kwargs():
    """Popen kwargs that detach the child into its own killable group."""
    if DEV_OS == 'windows':
        return {'creationflags': subprocess.CREATE_NEW_PROCESS_GROUP
                | getattr(subprocess, 'DETACHED_PROCESS', 0)}
    return {'start_new_session': True}


def terminate_tree(pid):
    """SIGTERM-equivalent, then force-kill, the process tree rooted at pid."""
    if DEV_OS == 'windows':
        subprocess.run(['taskkill', '/PID', str(pid), '/T', '/F'],
                       capture_output=True)
        return
    import signal
    try:
        os.killpg(pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        return
    for _ in range(30):
        if not process_identity(pid):
            return
        time.sleep(.1)
    try:
        os.killpg(pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


def tcp_open(host, port, timeout=3):
    """True when a TCP connect succeeds — replaces `nc -z` portably."""
    import socket
    try:
        with socket.create_connection((host, int(port)), timeout=timeout):
            return True
    except OSError:
        return False


def bash():
    """A bash binary for invoking the repo's shell scripts."""
    found = shutil.which('bash') or shutil.which('sh')
    if not found:
        raise RuntimeError('bash is required (on Windows run under Git Bash or WSL)')
    return found


# ------------------------------------------------------------- RDP (client)


def rdp_client_binary(desktop):
    """Configured or auto-detected FreeRDP client binary."""
    configured = desktop.get('clientBinary')
    candidates = [configured] if configured else {
        'macos': ['sfreerdp', 'sfreerdp3', 'sdl-freerdp'],
        'linux': ['sfreerdp3', 'sfreerdp', 'sdl-freerdp', 'sdl3-freerdp',
                  'xfreerdp3', 'xfreerdp'],
        'windows': ['wfreerdp.exe', 'wfreerdp3.exe', 'wfreerdp'],
    }[DEV_OS]
    for name in candidates:
        if name and shutil.which(name):
            return shutil.which(name)
    raise RuntimeError(
        'no FreeRDP client found; install one or set desktop.clientBinary '
        '(macOS: brew freerdp; Linux: freerdp3-sdl/x11; Windows: wfreerdp)')


def rdp_environment():
    """Extra env for the session-holder process."""
    if DEV_OS == 'linux':
        # The SDL client opens a window; the dummy driver keeps it headless.
        return {'SDL_VIDEODRIVER': 'dummy'}
    return {}


def _single_line(raw):
    password = raw.removesuffix(b'\n')
    if not password or b'\n' in password or b'\r' in password:
        raise RuntimeError('Password cannot be passed through single-line stdin')
    return password


def _password_from_command(command):
    result = subprocess.run(command, shell=True, capture_output=True)
    if result.returncode:
        raise RuntimeError('desktop.passwordCommand failed; inspect it manually')
    return _single_line(result.stdout)


def _password_macos(desktop, address, user):
    import sqlite3
    bookmark = desktop.get('rdpBookmark')
    if not bookmark:
        raise RuntimeError('desktop.rdpBookmark is not configured for this host')
    database = (Path.home() / 'Library/Containers/com.microsoft.rdc.macos/Data/Library/'
                'Application Support/com.microsoft.rdc.macos/com.microsoft.rdc.application-data.sqlite')
    with sqlite3.connect(database.as_uri() + '?mode=ro', uri=True) as conn:
        rows = conn.execute('''select c.ZID,c.ZUSERNAME from ZBOOKMARKENTITY b
          join ZCREDENTIALENTITY c on b.ZCREDENTIAL=c.Z_PK
          where b.ZFRIENDLYNAME=? and b.ZHOSTNAME=?''',
          (bookmark, address)).fetchall()
    if len(rows) != 1 or rows[0][1].lower() != user.lower():
        raise RuntimeError(f'Expected exactly one saved {bookmark} {user} credential')
    result = subprocess.run(['security', 'find-generic-password', '-s', 'com.microsoft.rdc.macos',
                             '-a', rows[0][0], '-w'], capture_output=True)
    if result.returncode:
        raise RuntimeError('Keychain did not release the authorized RDP credential')
    return _single_line(result.stdout)


def _credread_windows(target):
    """CredReadW of a generic credential (e.g. TERMSRV/<addr>) — what mstsc and
    Windows App store when you save the password in the client."""
    import ctypes
    from ctypes import wintypes

    class CREDENTIAL(ctypes.Structure):
        _fields_ = [
            ('Flags', wintypes.DWORD), ('Type', wintypes.DWORD),
            ('TargetName', wintypes.LPWSTR), ('Comment', wintypes.LPWSTR),
            ('LastWritten', wintypes.FILETIME), ('CredentialBlobSize', wintypes.DWORD),
            ('CredentialBlob', ctypes.POINTER(ctypes.c_byte)),
            ('Persist', wintypes.DWORD), ('AttributeCount', wintypes.DWORD),
            ('Attributes', ctypes.c_void_p), ('TargetAlias', wintypes.LPWSTR),
            ('UserName', wintypes.LPWSTR)]

    CRED_TYPE_GENERIC = 1
    advapi32 = ctypes.windll.advapi32
    cred_ptr = ctypes.POINTER(CREDENTIAL)()
    try:
        if not advapi32.CredReadW(target, CRED_TYPE_GENERIC, 0, ctypes.byref(cred_ptr)):
            return None
        size = cred_ptr.contents.CredentialBlobSize
        blob = ctypes.string_at(cred_ptr.contents.CredentialBlob, size)
        return blob.decode('utf-16-le').encode('utf-8')
    finally:
        if cred_ptr:
            advapi32.CredFree(cred_ptr)


def _password_windows(desktop, address, user):
    targets = [desktop['credTarget']] if desktop.get('credTarget') else \
        [f'TERMSRV/{address}', f'TERMSRV/{address.split(":")[0]}']
    for target in targets:
        password = _credread_windows(target)
        if password:
            return _single_line(password)
    raise RuntimeError(
        f'no saved RDP credential for {targets[0]}; save the password in '
        'mstsc/Windows App once, or set desktop.passwordCommand')


def rdp_password(desktop, address, user):
    """The saved RDP credential for the configured bookmark/target, as bytes.

    Precedence: desktop.passwordCommand (any OS) > OS credential store:
    macOS = the Windows App bookmark's Keychain item, Windows = Credential
    Manager TERMSRV/<addr> generic credential, Linux = passwordCommand is
    required (e.g. `secret-tool lookup service remote-agents host <id>`).
    The operator authorized this specific credential; never fall back to
    manual entry or a different one — report the failure.
    """
    if desktop.get('passwordEnv'):
        value = os.environ.get(desktop['passwordEnv'], '')
        if not value:
            raise RuntimeError(f"desktop.passwordEnv '{desktop['passwordEnv']}' is unset")
        return _single_line(value.encode())
    if desktop.get('passwordCommand'):
        return _password_from_command(desktop['passwordCommand'])
    if DEV_OS == 'macos':
        return _password_macos(desktop, address, user)
    if DEV_OS == 'windows':
        return _password_windows(desktop, address, user)
    raise RuntimeError(
        'no automatic credential store on Linux; set desktop.passwordCommand '
        '(e.g. "secret-tool lookup service remote-agents host <id>" or '
        '"pass show remote-agents/<id>")')


def open_rdp_client(desktop, address, bookmark):
    """Launch the configured interactive RDP client (the manual viewing path)."""
    template = desktop.get('openCommand')
    if template:
        command = template.format(addr=address, bookmark=bookmark or '')
        subprocess.Popen(command, shell=True)
        return command
    if DEV_OS == 'macos':
        bundle = desktop.get('rdpAppBundleId', 'com.microsoft.rdc.macos')
        subprocess.run(['open', '-b', bundle], check=True)
        return f'open -b {bundle}'
    if DEV_OS == 'windows':
        subprocess.Popen(['mstsc', f'/v:{address}'],
                         creationflags=getattr(subprocess, 'DETACHED_PROCESS', 0))
        return f'mstsc /v:{address}'
    for client in ('remmina', 'xfreerdp', 'krdc', 'gnome-connections'):
        if shutil.which(client):
            if client == 'xfreerdp':
                subprocess.Popen([client, f'/v:{address}'])
            elif client == 'remmina':
                subprocess.Popen([client, '-c', f'rdp://{address}'])
            else:
                subprocess.Popen([client])
            return client
    raise RuntimeError(
        f'no RDP client found; connect to {address} manually or set '
        'desktop.openCommand')


# --------------------------------------------------------------- WireGuard


def wg_up(conf_path):
    """Bring the tunnel up. conf_path is the rendered .conf with a current
    Endpoint. macOS/Linux use wg-quick (sudo); Windows (re)installs the
    WireGuard tunnel service through an elevated Start-Process (one UAC
    prompt)."""
    conf_path = str(conf_path)
    if DEV_OS == 'windows':
        name = Path(conf_path).stem
        _elevated('wireguard', '/uninstalltunnelservice', name,
                  check=False)
        _elevated('wireguard', '/installtunnelservice', conf_path)
        return
    subprocess.run(['sudo', 'wg-quick', 'up', conf_path], check=True)


def wg_down(conf_path):
    conf_path = str(conf_path)
    if DEV_OS == 'windows':
        _elevated('wireguard', '/uninstalltunnelservice',
                  Path(conf_path).stem, check=False)
        return
    subprocess.run(['sudo', 'wg-quick', 'down', conf_path], check=True)


def _elevated(program, *args, check=True):
    """Run a command elevated (UAC) on Windows and wait for it."""
    arg_list = ','.join(f"'{a}'" for a in args)
    script = (f"$p=Start-Process -FilePath '{program}' "
              f"-ArgumentList {arg_list} -Verb RunAs -Wait -PassThru;"
              'exit $p.ExitCode')
    result = subprocess.run(['powershell', '-NoProfile', '-NonInteractive',
                             '-Command', script])
    if check and result.returncode:
        raise RuntimeError(f'elevated {program} {args[0]} failed (exit {result.returncode})')


def wg_service_restart(label):
    """Restart the fixed, passwordless bridge service (installed variant)."""
    if DEV_OS == 'macos':
        subprocess.run(['sudo', '-n', '/bin/launchctl', 'kickstart', '-k',
                        f'system/{label}'], check=True)
    elif DEV_OS == 'linux':
        subprocess.run(['sudo', '-n', 'systemctl', 'restart', label], check=True)
    else:
        raise RuntimeError('no fixed bridge service on Windows; wg_up manages the tunnel service')


def wg_service_stop(label):
    if DEV_OS == 'macos':
        subprocess.run(['sudo', '-n', '/bin/launchctl', 'kill', 'SIGTERM',
                        f'system/{label}'], check=True)
    elif DEV_OS == 'linux':
        subprocess.run(['sudo', '-n', 'systemctl', 'stop', label], check=True)


# -------------------------------------------------------------------- SMB


def smb_access_path(ip, share, mountpoint):
    """What the operator uses to reach the share: the mountpoint on Unix,
    the UNC path on Windows (no drive mapping needed)."""
    return f'\\\\{ip}\\{share}' if DEV_OS == 'windows' else str(mountpoint)


def smb_mounted(ip, share, mountpoint):
    if DEV_OS == 'windows':
        return Path(f'//{ip}/{share}').exists()
    return os.path.ismount(str(mountpoint))


def _credential_file(state_dir, credential):
    """Linux mount.cifs reads credentials from a file, not a prompt."""
    path = Path(state_dir) / 'smb.cred'
    path.write_text(f"username={credential['username']}\n"
                    f"password={credential['password']}\n")
    os.chmod(path, 0o600)
    return path


def smb_mount(ip, share, mountpoint, credential, state_dir):
    """Mount (or attach, on Windows) the work share. Returns the access path."""
    if DEV_OS == 'windows':
        # cmdkey stores the credential for this private tunnel IP; net use
        # then opens the session. We record that we created it so disconnect
        # only removes what it added.
        subprocess.run(['cmdkey', f'/add:{ip}',
                        f'/user:{credential["username"]}',
                        f'/pass:{credential["password"]}'],
                       check=True, capture_output=True)
        marker = Path(state_dir) / 'smb-cred-created.json'
        marker.write_text(json.dumps({'target': ip}))
        os.chmod(marker, 0o600)
        result = subprocess.run(['net', 'use', f'\\\\{ip}\\{share}'],
                                capture_output=True, text=True)
        if result.returncode:
            raise RuntimeError(f'SMB attach failed (no retry): {result.stderr.strip()}')
        return f'\\\\{ip}\\{share}'
    if DEV_OS == 'macos':
        return _smb_mount_macos(ip, share, mountpoint, credential)
    credfile = _credential_file(state_dir, credential)
    try:
        subprocess.run(['sudo', 'mount', '-t', 'cifs', f'//{ip}/{share}',
                        str(mountpoint),
                        '-o', f'credentials={credfile},uid={os.getuid()},vers=3.0,iocharset=utf8'],
                       check=True)
    finally:
        credfile.unlink(missing_ok=True)
    return str(mountpoint)


def _smb_mount_macos(ip, share, mountpoint, credential):
    import fcntl as _fcntl
    import pty
    import select
    import termios
    master, slave = pty.openpty()

    def controlling_terminal():
        os.setsid()
        _fcntl.ioctl(slave, termios.TIOCSCTTY, 0)

    process = subprocess.Popen(
        ['/sbin/mount_smbfs', f'//{credential["username"]}@{ip}/{share}', str(mountpoint)],
        stdin=slave, stdout=slave, stderr=slave, preexec_fn=controlling_terminal)
    os.close(slave)
    pending = b''
    sent = False
    deadline = time.monotonic() + 30
    try:
        while process.poll() is None and time.monotonic() < deadline:
            if select.select([master], [], [], 0.2)[0]:
                try:
                    pending += os.read(master, 4096)
                except OSError:
                    break
                if b'password' in pending.lower() and not sent:
                    os.write(master, (credential['password'] + '\n').encode())
                    sent = True
        if process.poll() is None:
            process.wait(timeout=2)
        if process.returncode != 0 or not os.path.ismount(str(mountpoint)):
            detail = pending.decode(errors='replace').replace(credential['password'], '[redacted]')
            raise RuntimeError(f'SMB mount failed (no retry): {detail}')
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()
        os.close(master)
    return str(mountpoint)


def smb_unmount(ip, share, mountpoint):
    if DEV_OS == 'windows':
        subprocess.run(['net', 'use', f'\\\\{ip}\\{share}', '/delete'],
                       capture_output=True)
        return
    if os.path.ismount(str(mountpoint)):
        if DEV_OS == 'macos':
            subprocess.run(['/sbin/umount', str(mountpoint)], check=True)
        else:
            subprocess.run(['sudo', 'umount', str(mountpoint)], check=True)


def smb_cred_cleanup(state_dir, ip):
    """Remove the cmdkey credential only if smb_mount created it."""
    marker = Path(state_dir) / 'smb-cred-created.json'
    if marker.exists():
        subprocess.run(['cmdkey', f'/delete:{ip}'], capture_output=True)
        marker.unlink()
