"""One native headless RDP session on the dev machine, with optional read-only observation."""
import argparse
import base64
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys
import tempfile
import time
import uuid

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE / 'lib'))
import config as ra_config  # noqa: E402
import devlocal  # noqa: E402

_config, _ = ra_config.load_config()
HOST_ID, HOST = ra_config.resolve_host(_config)
if HOST.get('os') != 'windows':
    sys.exit(f"desktop.py: host '{HOST_ID}' is not a Windows host")
DESKTOP = HOST.get('desktop') or {}
if not DESKTOP:
    sys.exit(f"desktop.py: host '{HOST_ID}' has no 'desktop' config block")

SSH_ALIAS = HOST['sshAlias']
WORK_ROOT = HOST['workRoot'].rstrip('/').rstrip('\\').replace('\\', '/')
WINDOWS_USER = DESKTOP.get('windowsUser', 'Administrator')
RDP_BOOKMARK = DESKTOP.get('rdpBookmark')
LOCAL_PORT = int(DESKTOP.get('localPort', 13389))
VIEWER_LOCAL_PORT = int(DESKTOP.get('viewerLocalPort', 16080))
VIEWER_REMOTE_PORT = int(DESKTOP.get('viewerRemotePort', 16081))
SIZE = DESKTOP.get('size', '1600x1000')
HELPER_NAME = DESKTOP.get('helperDir', 'desktop-helpers')
REMOTE_PYTHON = DESKTOP.get('remotePython')
TASK_PREFIX = DESKTOP.get('taskPrefix', 'RemoteAgents-Desktop')

RUNTIME = Path(tempfile.gettempdir()) / f'remote-agents-desktop-{HOST_ID}'
AUTH_ERRORS = ('LOGON_FAILURE', 'ACCOUNT_LOCKED', 'ACCOUNT_DISABLED', 'PASSWORD_EXPIRED',
               'PASSWORD_MUST_CHANGE', 'LOGON_DENIED', 'AUTHENTICATION_FAILED', 'ERRCONNECT_LOGON')


def run(argv, **kwargs):
    if str(argv[0]) in ('ssh', 'scp'):
        argv = [argv[0], '-o', 'ControlPath=none', '-o', 'ControlMaster=no', *argv[1:]]
    return subprocess.run(list(map(str, argv)), check=True, **kwargs)


def powershell(script):
    encoded = base64.b64encode(script.encode('utf-16-le')).decode()
    return run(['ssh', '-o', 'BatchMode=yes', SSH_ALIAS,
                'powershell -NoProfile -NonInteractive -EncodedCommand ' + encoded],
               capture_output=True, text=True, timeout=120).stdout.strip()


def write_json(name, value):
    temp = RUNTIME / (name + '.tmp')
    temp.write_text(json.dumps(value, indent=2))
    temp.replace(RUNTIME / name)


def alive(record):
    return bool(record and record.get('identity') and
                devlocal.process_identity(record['pid']) == record['identity'])


def read_record(name):
    path = RUNTIME / name
    return json.loads(path.read_text()) if path.exists() else {}


def launch(argv, name, stdin=None, env_extra=None):
    env = dict(os.environ)
    if env_extra:
        env.update(env_extra)
    with (RUNTIME / (name + '.log')).open('wb') as log:
        child = subprocess.Popen(list(map(str, argv)), stdin=subprocess.PIPE if stdin is not None
                                 else subprocess.DEVNULL, stdout=log, stderr=log, env=env,
                                 **devlocal.popen_detached_kwargs())
    if stdin is not None:
        try:
            child.stdin.write(stdin)
            child.stdin.close()
        except BrokenPipeError:
            pass
    record = dict(pid=child.pid, identity=devlocal.process_identity(child.pid))
    write_json(name + '.json', record)
    return child, record


def terminate(name):
    record = read_record(name + '.json')
    if alive(record):
        devlocal.terminate_tree(record['pid'])
    (RUNTIME / (name + '.json')).unlink(missing_ok=True)


def listening(port):
    return devlocal.tcp_open('127.0.0.1', port, timeout=.3)


def tunnel():
    if listening(LOCAL_PORT):
        return  # An existing tunnel remains owned by its creator.
    terminate('tunnel')
    child, _ = launch([devlocal.bash(), HERE / 'host.sh', '--host', HOST_ID, 'desktop-tunnel'], 'tunnel')
    for _ in range(60):
        if child.poll() is not None:
            raise RuntimeError('SSM tunnel exited; inspect the private runtime log')
        if listening(LOCAL_PORT):
            return
        time.sleep(.5)
    raise RuntimeError('SSM tunnel did not become ready within 30 seconds')


def fingerprint():
    value = powershell(r"""
$ErrorActionPreference='Stop'
$setting=Get-CimInstance -Namespace root/cimv2/terminalservices -ClassName Win32_TSGeneralSetting -Filter "TerminalName='RDP-tcp'"
$cert=Get-Item ('Cert:\LocalMachine\Remote Desktop\'+$setting.SSLCertificateSHA1Hash)
$sha=[Security.Cryptography.SHA256]::Create()
([BitConverter]::ToString($sha.ComputeHash($cert.RawData))).Replace('-','').ToLowerInvariant()
""")
    import re
    if not re.fullmatch('[0-9a-f]{64}', value):
        raise RuntimeError('Cannot obtain RDP certificate fingerprint over authenticated SSH')
    return value


def windows_password():
    # The operator authorized this specific saved RDP credential. Do not switch
    # to manual password entry or another credential on failure — report it.
    return devlocal.rdp_password(DESKTOP, f'127.0.0.1:{LOCAL_PORT}', WINDOWS_USER)


def status():
    record = read_record('rdp.json')
    running = alive(record)
    log = RUNTIME / 'rdp.log'
    text = log.read_text(errors='replace').upper() if log.exists() else ''
    auth = next((value for value in AUTH_ERRORS if value in text), None)
    return dict(stage='auth_failed' if auth else 'transport_running' if running
                else 'connection_failed' if record else 'stopped',
                rdp_pid=record.get('pid'), auth_error=auth, tunnel_listening=listening(LOCAL_PORT),
                viewer_listening=listening(VIEWER_LOCAL_PORT),
                readiness='Run probe; a live RDP process does not prove an unlocked desktop')


def deploy_helpers():
    destination = WORK_ROOT + '/' + HELPER_NAME
    powershell("New-Item -ItemType Directory -Force -Path '" + destination + "' | Out-Null")
    for name in ('probe.py', 'observer.py'):
        run(['scp', '-q', HERE / 'desktop' / name, f'{SSH_ALIAS}:{destination}/' + name])
    return destination


def probe(output):
    if not REMOTE_PYTHON:
        raise RuntimeError('desktop.remotePython (a pythonw.exe with Pillow) is not configured')
    output.mkdir(parents=True, exist_ok=False)
    identifier = uuid.uuid4().hex
    helper = deploy_helpers()
    remote = f'{WORK_ROOT}/{HELPER_NAME}-probe-{identifier}'
    raw = powershell(r"""
$ErrorActionPreference='Stop'
$out='REMOTE'
$task='TASKPREFIX-Probe-ID'
$python='PYW'
$action=New-ScheduledTaskAction -Execute $python -Argument ('"HELPER/probe.py" "'+$out+'"')
$principal=New-ScheduledTaskPrincipal -UserId 'WUSER' -LogonType Interactive
$settings=New-ScheduledTaskSettingsSet -ExecutionTimeLimit (New-TimeSpan -Seconds 30)
try {
  Register-ScheduledTask -TaskName $task -Action $action -Principal $principal -Settings $settings | Out-Null
  Start-ScheduledTask -TaskName $task
  for($i=0;$i -lt 40;$i++) {
    if(Test-Path ($out+'/result.json')) { Get-Content ($out+'/result.json') -Raw; exit 0 }
    Start-Sleep -Milliseconds 500
  }
  throw 'Interactive desktop probe timed out; no unlocked session may exist'
} finally {
  Stop-ScheduledTask -TaskName $task -ErrorAction SilentlyContinue
  Unregister-ScheduledTask -TaskName $task -Confirm:$false -ErrorAction SilentlyContinue
}
""".replace('REMOTE', remote).replace('HELPER', helper).replace('TASKPREFIX', TASK_PREFIX)
       .replace('ID', identifier).replace('PYW', REMOTE_PYTHON).replace('WUSER', WINDOWS_USER))
    result = json.loads(raw)
    (output / 'result.json').write_text(json.dumps(result, indent=2))
    if result['ready']:
        run(['scp', '-q', f'{SSH_ALIAS}:{remote}/desktop.png', output / 'desktop.png'])
    powershell("Remove-Item -LiteralPath '" + remote + "' -Recurse -Force")
    print(json.dumps(dict(result, output=str(output)), indent=2))
    if not result['ready']:
        raise RuntimeError('Windows desktop is not ready; inspect the probe result')
    return result


def start():
    current = status()
    if current['auth_error']:
        raise RuntimeError('Previous authentication failed. Operator must verify the credential and acknowledge with clear-auth.')
    if current['stage'] == 'transport_running':
        return
    if read_record('rdp.json'):
        raise RuntimeError('Previous RDP connection exited. Inspect status, then explicitly stop/start; no blind retry.')
    binary = devlocal.rdp_client_binary(DESKTOP)
    tunnel()
    cert = fingerprint()
    password = windows_password()
    child, _ = launch([binary, f'/v:127.0.0.1:{LOCAL_PORT}', f'/u:{WINDOWS_USER}', '/from-stdin:force',
                       '/cert:fingerprint:sha256:' + cert, '/sec:nla', f'/size:{SIZE}',
                       '-clipboard', '-auto-reconnect', '/log-level:WARN'], 'rdp',
                    stdin=password + b'\n', env_extra=devlocal.rdp_environment())
    del password
    for _ in range(8):
        if child.poll() is not None:
            raise RuntimeError('RDP client exited. Inspect status; do not retry blindly.')
        time.sleep(.5)


def viewer():
    if not REMOTE_PYTHON:
        raise RuntimeError('desktop.remotePython (a pythonw.exe with Pillow) is not configured')
    from urllib.request import urlopen
    previous = read_record('viewer.json')
    if previous and alive(read_record('viewer-tunnel.json')):
        with urlopen(previous['url'], timeout=5) as response:
            if response.status == 200:
                print(previous['url'])
                return
    if listening(VIEWER_LOCAL_PORT):
        raise RuntimeError('Observer port belongs to another process; refusing to reuse it')
    if status()['stage'] != 'transport_running':
        raise RuntimeError('Start the RDP transport before opening the observer')
    helper = deploy_helpers()
    token = secrets.token_urlsafe(32)
    task = f'{TASK_PREFIX}-Observer-{uuid.uuid4().hex}'
    config = RUNTIME / 'observer-config.json'
    config.write_text(json.dumps(dict(token=token, port=VIEWER_REMOTE_PORT)))
    remote_config = helper + '/' + task + '.json'
    try:
        run(['scp', '-q', config, f'{SSH_ALIAS}:{remote_config}'])
    finally:
        config.unlink()
    powershell(r"""
$ErrorActionPreference='Stop'
$python='PYW'
$action=New-ScheduledTaskAction -Execute $python -Argument '"HELPER/observer.py" "CONFIG"'
$principal=New-ScheduledTaskPrincipal -UserId 'WUSER' -LogonType Interactive
$settings=New-ScheduledTaskSettingsSet -ExecutionTimeLimit (New-TimeSpan -Minutes 61)
Register-ScheduledTask -TaskName 'TASK' -Action $action -Principal $principal -Settings $settings | Out-Null
Start-ScheduledTask -TaskName 'TASK'
""".replace('HELPER', helper).replace('CONFIG', remote_config).replace('TASK', task)
       .replace('PYW', REMOTE_PYTHON).replace('WUSER', WINDOWS_USER))
    child, _ = launch(['ssh', '-T', '-N', '-S', 'none', '-o', 'ControlMaster=no',
                       '-o', 'RemoteCommand=none', '-o', 'BatchMode=yes', '-o', 'ExitOnForwardFailure=yes',
                       '-L', f'127.0.0.1:{VIEWER_LOCAL_PORT}:127.0.0.1:{VIEWER_REMOTE_PORT}', SSH_ALIAS],
                      'viewer-tunnel')
    url = f'http://127.0.0.1:{VIEWER_LOCAL_PORT}/?token=' + token
    write_json('viewer.json', dict(task=task, url=url))
    for _ in range(30):
        if child.poll() is not None:
            raise RuntimeError('Observer tunnel exited')
        try:
            with urlopen(url, timeout=2) as response:
                if response.status == 200:
                    print(url)
                    return
        except OSError:
            time.sleep(.5)
    raise RuntimeError('Observer did not become ready')


def viewer_stop():
    info = read_record('viewer.json')
    try:
        if info:
            task = info['task']
            powershell("Stop-ScheduledTask -TaskName '" + task + "' -ErrorAction SilentlyContinue; "
                       "Unregister-ScheduledTask -TaskName '" + task + "' -Confirm:$false -ErrorAction SilentlyContinue")
    finally:
        # Release local resources even if Windows is unreachable. Preserve the
        # remote task record on failure so explicit cleanup can be retried.
        terminate('viewer-tunnel')
    (RUNTIME / 'viewer.json').unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['start', 'status', 'probe', 'viewer', 'viewer-stop',
                                          'open', 'stop', 'clear-auth'])
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    os.umask(0o077)
    RUNTIME.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        lock = devlocal.acquire_lock(RUNTIME / 'controller.lock')
    except BlockingIOError:
        raise RuntimeError('Another desktop operation is in progress; do not start concurrent controllers')
    if args.command == 'start':
        start()
        probe(args.output or RUNTIME / ('probe-' + uuid.uuid4().hex))
    elif args.command == 'status':
        print(json.dumps(status(), indent=2))
    elif args.command == 'probe':
        probe(args.output or RUNTIME / ('probe-' + uuid.uuid4().hex))
    elif args.command == 'viewer':
        viewer()
    elif args.command == 'viewer-stop':
        viewer_stop()
    elif args.command == 'open':
        if not listening(LOCAL_PORT):
            raise RuntimeError(
                f'SSM desktop tunnel is not listening on 127.0.0.1:{LOCAL_PORT}. '
                'Start desktop-tunnel first.')
        how = devlocal.open_rdp_client(DESKTOP, f'127.0.0.1:{LOCAL_PORT}', RDP_BOOKMARK)
        print(f'Opened {how}. Connect to 127.0.0.1:{LOCAL_PORT}'
              + (f" via saved device '{RDP_BOOKMARK}'." if RDP_BOOKMARK else '.'))
    elif args.command == 'stop':
        try:
            viewer_stop()
        finally:
            terminate('rdp')
            terminate('tunnel')
        print('Desktop transport stopped; Windows is not logged off or powered off.')
    elif args.command == 'clear-auth':
        if alive(read_record('rdp.json')):
            raise RuntimeError('Stop the RDP client before clearing a recorded authentication failure')
        (RUNTIME / 'rdp.log').unlink(missing_ok=True)
        (RUNTIME / 'rdp.json').unlink(missing_ok=True)
        print('Authentication failure acknowledged by operator; one new start is permitted.')


if __name__ == '__main__':
    try:
        main()
    except (RuntimeError, subprocess.CalledProcessError) as exc:
        sys.exit(str(exc))
