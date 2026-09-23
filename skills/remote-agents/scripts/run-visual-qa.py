"""Run a bounded visual-QA worker inside the interactive Windows desktop.

The default worker is the Grok CLI (streaming-messages-json events, --resume
session ids). Other agent CLIs can be adapted by keeping the same contract:
prompt file in, JSONL events out, answer.md + status.json artifacts.
"""
import argparse
import json
import os
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_AGENT = str(Path.home() / '.grok' / 'bin' / 'grok.exe')


def run_worker(command, out, err, timeout, env):
    # Kill the owned process tree before the parent disappears; otherwise an
    # orphaned MCP child could retain the desktop-input lock after a timeout.
    with subprocess.Popen(command, stdout=out, stderr=err, env=env) as worker:
        try:
            return worker.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            stopped = subprocess.run(['taskkill', '/PID', str(worker.pid), '/T', '/F'],
                                     stdout=err, stderr=err, timeout=15)
            if stopped.returncode and worker.poll() is None:
                worker.kill()
                raise RuntimeError(f'QA timed out; tree cleanup failed for PID {worker.pid}; inspect remaining MCP children')
            worker.wait(timeout=10)
            raise RuntimeError('QA timed out; its agent/MCP process tree was stopped')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--prompt-file', type=Path, required=True)
    parser.add_argument('--output-dir', type=Path, required=True)
    parser.add_argument('--cwd', default=str(Path(os.environ['SystemDrive'] + '/') / 'Work' / 'workspaces' / 'default'))
    parser.add_argument('--timeout', type=int, default=1800)
    parser.add_argument('--max-turns', type=int, default=120)
    parser.add_argument('--resume-session')
    parser.add_argument('--agent-bin', default=os.environ.get('QA_AGENT_BIN', DEFAULT_AGENT))
    parser.add_argument('--model', default=os.environ.get('QA_AGENT_MODEL', 'grok-4.7'))
    parser.add_argument('--launcher', default=str(Path(os.environ['SystemDrive'] + '/') / 'Work' / 'desktop-helpers' / 'Start-Interactive.ps1'))
    args = parser.parse_args()
    if not 30 <= args.timeout <= 3600:
        parser.error('--timeout must be between 30 and 3600 seconds')
    if not 1 <= args.max_turns <= 500:
        parser.error('--max-turns must be between 1 and 500')
    args.output_dir.mkdir(parents=True, exist_ok=False)
    status_path = args.output_dir / 'status.json'
    def status(stage, **details):
        status_path.write_text(json.dumps(dict(stage=stage, at=datetime.now(timezone.utc).isoformat(),
                                              max_turns=args.max_turns, timeout_seconds=args.timeout,
                                              **details)), encoding='utf-8')
    status('running')
    qa_lock = None
    try:
        # UI focus belongs to one worker. Separate browser observers do not
        # change this; reject a second writer in the same Windows account.
        import msvcrt
        qa_lock = (Path(tempfile.gettempdir()) / 'remote-agents-visual-qa.lock').open('a+b')
        if qa_lock.tell() == 0:
            qa_lock.write(b'0')
            qa_lock.flush()
        qa_lock.seek(0)
        try:
            msvcrt.locking(qa_lock.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError as exc:
            raise RuntimeError('Another UI worker owns this desktop; wait for it to finish') from exc
        scenario = args.prompt_file.read_text(encoding='utf-8')
        if not scenario.strip():
            raise ValueError('Prompt is empty')
        effective_prompt = args.output_dir / 'effective-prompt.txt'
        effective_prompt.write_text(
            'GUI launch rule: inspect/reuse an existing application window first. If this scenario '
            'authorizes launching a GUI program, use the scheduled-task launcher '
            + args.launcher + ' . Never launch the interactive GUI with Start-Process or direct '
            'executable invocation inside a worker terminal: terminal job cleanup can kill the '
            'process. Record the returned task name and PID; verify survival after the launch '
            'command exits, then inspect the window with the interactive QA driver. '
            'MainWindowTitle/MainWindowHandle queried from SSH can be empty across sessions even '
            'when the window is visible. A process start does not prove UI readiness. Do not '
            'retry by launching duplicates. Headless CLI/MCP commands intentionally remain '
            'caller-owned and keep their normal launch path.\n\n'
            + scenario, encoding='utf-8')
        events_path = args.output_dir / 'events.jsonl'
        with events_path.open('w', encoding='utf-8') as out, (args.output_dir / 'stderr.txt').open('w', encoding='utf-8') as err:
            command = [args.agent_bin, '--cwd', args.cwd, '--model', args.model, '--no-subagents',
                       '--disable-web-search', '--always-approve', '--max-turns', str(args.max_turns),
                       '--prompt-file', str(effective_prompt), '--output-format', 'streaming-messages-json']
            if args.resume_session:
                command.extend(['--resume', args.resume_session])
            env = dict(os.environ, GROK_MCP_STARTUP_TIMEOUT_SECS='30')
            returncode = run_worker(command, out, err, args.timeout, env)
        if returncode:
            raise RuntimeError(f'Agent exited with {returncode}')
        events = [json.loads(line) for line in events_path.read_text(encoding='utf-8').splitlines() if line.strip()]
        final = next(event for event in reversed(events) if event.get('type') == 'result')
        if final.get('is_error') or final.get('stop_reason') != 'end_turn':
            raise RuntimeError(f'Agent did not finish: {final.get("stop_reason")}')
        (args.output_dir / 'answer.md').write_text(final['result'], encoding='utf-8')
        # An answer is not a QA pass. The manager must inspect images and assertions.
        status('awaiting_review', session_id=final['session_id'], duration_ms=final['duration_ms'])
    except Exception as exc:
        status('failed', error=str(exc))
        raise
    finally:
        if qa_lock is not None:
            qa_lock.close()


if __name__ == '__main__':
    main()
