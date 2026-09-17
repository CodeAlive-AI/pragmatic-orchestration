"""Fetch exact QA PNGs from the Windows host for direct image inspection."""
import argparse
import hashlib
import json
import re
import struct
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE / 'lib'))
import config as ra_config  # noqa: E402

_config, _ = ra_config.load_config()
HOST_ID, HOST = ra_config.resolve_host(_config)
SSH_ALIAS = HOST['sshAlias']
WORK_ROOT = HOST.get('workRoot', 'C:/Work').rstrip('/')
QA = HOST.get('qa') or {}
ARTIFACT_DIR = QA.get('artifactDir', 'C:/Users/Administrator/AppData/Local/RemoteAgents-QA/screenshots').rstrip('/')

ARTIFACT = re.compile(re.escape(ARTIFACT_DIR) + r'/[0-9a-f]{32}\.png', re.I)
RUN = re.compile(re.escape(WORK_ROOT) + r'/runs/[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)?')


def screenshot_metadata(events):
    lines = events.splitlines()
    for index, line in enumerate(lines):
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            # A running writer may leave its final JSON line incomplete.
            if index == len(lines) - 1 and not events.endswith('\n'):
                break
            raise
        for block in event.get('message', {}).get('content', []):
            if block.get('type') != 'tool_result' or not isinstance(block.get('content'), str):
                continue
            try:
                result = json.loads(block['content'])
            except json.JSONDecodeError:
                continue  # Ordinary non-MCP text results have no image metadata.
            if result.get('type') != 'MCP' or result.get('tool_name') != 'qa_view_screenshot':
                continue
            payload = result.get('output', {}).get('OkayOutput')
            if payload is None:
                continue  # Failed capture, not an image artifact.
            metadata, _ = json.JSONDecoder().raw_decode(payload)
            if '[image content will be provided separately]' not in payload:
                raise ValueError('Screenshot result lacks native image delivery marker')
            yield metadata


def transfer(remote, local):
    subprocess.run(['scp', '-o', 'ControlPath=none', '-o', 'ControlMaster=no',
                    '-q', f'{SSH_ALIAS}:{remote}', str(local)], check=True, timeout=120)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--remote-run', action='append', required=True,
                        help=f'{WORK_ROOT}/runs/<run>[/<subdir>]; repeat to include resumed runs')
    parser.add_argument('--output-dir', type=Path, required=True)
    args = parser.parse_args()
    runs = [value.replace('\\', '/') for value in args.remote_run]
    if any(not RUN.fullmatch(value) for value in runs):
        parser.error(f'Each remote run must be a {WORK_ROOT}/runs directory with simple names')
    output = args.output_dir.resolve()
    output.mkdir(parents=True, exist_ok=True)
    images = {}
    with tempfile.TemporaryDirectory(prefix='qa-fetch-') as temporary:
        temporary = Path(temporary)
        for run in runs:
            events_path = temporary / 'events.jsonl'
            transfer(run + '/events.jsonl', events_path)
            for metadata in screenshot_metadata(events_path.read_text(encoding='utf-8')):
                remote = metadata['artifact_path'].replace('\\', '/')
                if not ARTIFACT.fullmatch(remote):
                    raise ValueError('Screenshot path is outside the dedicated QA screenshot directory')
                if remote in images:
                    continue
                downloaded = temporary / 'image.png'
                transfer(remote, downloaded)
                data = downloaded.read_bytes()
                if len(data) < 24 or data[:8] != b'\x89PNG\r\n\x1a\n' or data[12:16] != b'IHDR':
                    raise ValueError('Downloaded artifact is not a PNG')
                dimensions = list(struct.unpack('>II', data[16:24]))
                if dimensions != metadata['image_size']:
                    raise ValueError('PNG dimensions differ from capture metadata')
                target = output / remote.rsplit('/', 1)[1]
                if target.exists() and target.read_bytes() != data:
                    raise ValueError(f'Existing local artifact differs: {target}')
                downloaded.replace(target)
                images[remote] = dict(metadata, local_path=str(target),
                                      sha256=hashlib.sha256(data).hexdigest(), source_run=run)
    if not images:
        raise RuntimeError('No completed image-native screenshot results found in these runs')
    manifest = dict(images=list(images.values()), count=len(images))
    manifest_path = output / 'screenshots.json'
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(dict(count=len(images), manifest=str(manifest_path),
                         local_paths=[value['local_path'] for value in images.values()]), indent=2))


if __name__ == '__main__':
    main()
