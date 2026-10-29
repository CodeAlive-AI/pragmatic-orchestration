"""Unpaid regression probe; signals only a fresh process group created here."""
import json
import os
from pathlib import Path
import signal
import subprocess
import sys

repo = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(repo / 'skills/pragmatic-orchestration/scripts/lib'))
from steer.jsonrpc import JsonRpcProcess
from steer.util import kill_process_group

child_code = "import signal,time; signal.signal(signal.SIGTERM,signal.SIG_IGN); print('ready',flush=True); time.sleep(60)"
leader_code = "import subprocess,sys,time; p=subprocess.Popen([sys.executable,'-u','-c',sys.argv[1]],stdout=subprocess.PIPE,text=True); assert p.stdout.readline().strip()=='ready'; print(p.pid,flush=True); time.sleep(60)"
leader = subprocess.Popen([sys.executable, '-u', '-c', leader_code, child_code], stdout=subprocess.PIPE, text=True, start_new_session=True)
group = os.getpgid(leader.pid)
assert group == leader.pid and group != os.getpgrp()
try:
    child = int(leader.stdout.readline())
    rpc = JsonRpcProcess([])
    rpc.proc = leader
    rpc.terminate()
    kill_process_group(leader.pid, timeout=0.1)
    status = subprocess.run(['ps', '-o', 'stat=', '-p', str(child)], capture_output=True, text=True, check=False).stdout.strip()
    source_commit = subprocess.run(['git', 'rev-parse', 'HEAD'], cwd=repo, capture_output=True, text=True, check=True).stdout.strip()
    result = {'source_commit': source_commit, 'platform': sys.platform,
              'leader_reaped': leader.poll() is not None,
              'descendant_survived': bool(status) and not status.startswith('Z'),
              'scope': 'synthetic own process group; no model/cloud/container',
              'test_process_cleanup': 'SIGKILL sent to this probe group in finally'}
finally:
    try: os.killpg(group, signal.SIGKILL)
    except ProcessLookupError: pass
    leader.wait(timeout=5)
    leader.stdout.close()
print(json.dumps(result))
