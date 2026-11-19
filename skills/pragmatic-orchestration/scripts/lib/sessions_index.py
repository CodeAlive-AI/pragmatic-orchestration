"""Private session-index storage and incremental synchronization.

The sync registry covers Claude, Codex, Devin, OpenCode, Porch, agy and
Cursor CLI stores.
"""
from __future__ import annotations

import contextlib
import collections
import functools
import hashlib
import json
import math
import os
import re
import shlex
import sqlite3
import stat
import sys
import time
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator

import sessions
import sessions_facts

SCHEMA_VERSION = '1'
NORMALIZATION_VERSION = '1'

# Storage, normalization and atomic replacement
SCHEMA = """
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE sources (
 source_key TEXT PRIMARY KEY, harness TEXT NOT NULL, store TEXT NOT NULL,
 kind TEXT NOT NULL CHECK(kind IN ('file','sqlite')), stat_json TEXT,
 state TEXT NOT NULL CHECK(state IN ('ok','pending','error','offline')),
 error TEXT, extractor_version TEXT, verified_at TEXT
);
CREATE TABLE sessions (
 session_key TEXT PRIMARY KEY, harness TEXT NOT NULL, native_id TEXT NOT NULL,
 store TEXT NOT NULL, title TEXT, cwd TEXT, models_json TEXT, created TEXT,
 updated TEXT, surface TEXT, class TEXT, class_basis TEXT, status_json TEXT,
 content_state TEXT NOT NULL CHECK(content_state IN ('ok','unavailable','stale'))
);
CREATE TABLE session_sources (
 session_key TEXT NOT NULL REFERENCES sessions(session_key) ON DELETE CASCADE,
 source_key TEXT NOT NULL REFERENCES sources(source_key) ON DELETE CASCADE,
 role TEXT NOT NULL CHECK(role IN ('content','metadata','lineage')),
 fingerprint TEXT, extractor_version TEXT,
 PRIMARY KEY(session_key, source_key, role)
);
CREATE TABLE unit_errors (
 session_key TEXT PRIMARY KEY, harness TEXT NOT NULL, extractor_version TEXT NOT NULL,
 sources_json TEXT NOT NULL, details_json TEXT NOT NULL, reason_class TEXT NOT NULL, reason TEXT NOT NULL
);
CREATE TABLE aliases (
 alias TEXT PRIMARY KEY, session_key TEXT NOT NULL REFERENCES sessions(session_key) ON DELETE CASCADE,
 kind TEXT NOT NULL
);
CREATE TABLE edges (
 session_key TEXT NOT NULL REFERENCES sessions(session_key) ON DELETE CASCADE,
 type TEXT NOT NULL, target_key TEXT, unresolved TEXT, basis TEXT, locator_json TEXT
);
CREATE TABLE docs (
 doc_id INTEGER PRIMARY KEY, session_key TEXT NOT NULL REFERENCES sessions(session_key) ON DELETE CASCADE,
 kind TEXT NOT NULL CHECK(kind IN ('title','prompt','answer')), origin TEXT,
 ts TEXT, model TEXT, locator_json TEXT, source_key TEXT, source_rev TEXT, text TEXT NOT NULL
);
CREATE INDEX docs_session ON docs(session_key);
CREATE INDEX session_sources_source ON session_sources(source_key);
CREATE VIRTUAL TABLE fts USING fts5(text, tokenize='trigram case_sensitive 1',
 detail=none, content='', contentless_delete=1);
"""


class SessionIndexError(RuntimeError):
    """An index is unavailable or cannot be used safely."""


class UnsupportedStore(SessionIndexError):
    """A reachable store has no supported session schema."""


def normalize(text: str) -> str:
    return unicodedata.normalize('NFC', text).lower().replace('ё', 'е')


@functools.lru_cache(maxsize=1)
def sqlite_capability() -> tuple[bool, str]:
    """Test the actual SQLite/FTS build, once per process."""
    version = sqlite3.sqlite_version
    if sqlite3.sqlite_version_info < (3, 43, 0):
        return False, f'SQLite {version} is too old'
    try:
        conn = sqlite3.connect(':memory:')
        try:
            conn.execute("CREATE VIRTUAL TABLE f USING fts5(text, tokenize='trigram case_sensitive 1', detail=none, content='', contentless_delete=1)")
        finally:
            conn.close()
    except sqlite3.Error as exc:
        return False, f'SQLite {version} lacks the required FTS5 trigram/contentless-delete support: {exc}'
    return True, version


def require_sqlite() -> None:
    available, detail = sqlite_capability()
    if not available:
        raise SessionIndexError(f'porch sessions index requires SQLite >= 3.43 with FTS5 contentless-delete; {detail}')


def index_dir() -> Path:
    override = os.environ.get('PORCH_SESSIONS_INDEX_DIR')
    if override:
        return Path(override)
    return sessions._steer_root().parent / 'sessions-index'


def _private_dir(path: Path) -> None:
    path.mkdir(parents=True, mode=0o700, exist_ok=True)
    if os.name != 'nt':
        path.chmod(0o700)


@contextlib.contextmanager
def writer_lock(directory: Path, deadline: float | None = None) -> Iterator[bool]:
    """Acquire the persistent advisory lock until monotonic deadline, if supplied."""
    _private_dir(directory)
    api = sessions._lock_api()
    if api is None:
        raise SessionIndexError('porch sessions index cannot obtain a process lock on this platform')
    fd = os.open(directory / 'index.lock', os.O_RDWR | os.O_CREAT, 0o600)
    with os.fdopen(fd, 'r+b') as handle:
        if os.name != 'nt':
            os.fchmod(handle.fileno(), 0o600)
        acquire, release = api
        acquired = False
        try:
            while True:
                if acquire(handle, False):
                    acquired = True
                    break
                if deadline is not None and time.monotonic() >= deadline:
                    break
                time.sleep(min(0.05, max(0, deadline - time.monotonic())) if deadline is not None else 0.05)
            yield acquired
        finally:
            if acquired:
                release(handle)


def _open_writer(directory: Path) -> sqlite3.Connection:
    """Open the index after acquiring writer_lock; initialize an empty index."""
    require_sqlite()
    _private_dir(directory)
    path = directory / 'index-v1.sqlite'
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    os.close(fd)
    if os.name != 'nt':
        path.chmod(0o600)
    conn = sqlite3.connect(path, timeout=2)
    try:
        conn.execute('PRAGMA foreign_keys=ON')
        conn.execute('PRAGMA journal_mode=WAL')
        if not sessions.table_exists(conn, 'meta'):
            # executescript owns the transaction; a failure leaves no half-schema.
            conn.executescript('BEGIN IMMEDIATE;\n' + SCHEMA +
                f"\nINSERT INTO meta VALUES ('schema_version','{SCHEMA_VERSION}');" +
                f"\nINSERT INTO meta VALUES ('normalization_version','{NORMALIZATION_VERSION}');" +
                f"\nINSERT INTO meta VALUES ('sqlite_version','{sqlite3.sqlite_version}');\nCOMMIT;")
        else:
            version = conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
            if version != (SCHEMA_VERSION,):
                raise SessionIndexError('unsupported session index schema; run porch sessions index drop then porch sessions index build')
            # This additive table also upgrades indexes created before error memoization.
            conn.execute('''CREATE TABLE IF NOT EXISTS unit_errors (
                session_key TEXT PRIMARY KEY, harness TEXT NOT NULL, extractor_version TEXT NOT NULL,
                sources_json TEXT NOT NULL, details_json TEXT NOT NULL, reason_class TEXT NOT NULL, reason TEXT NOT NULL)''')
            conn.commit()
        conn.execute('''CREATE TABLE IF NOT EXISTS facts (
            session_key TEXT NOT NULL REFERENCES sessions(session_key) ON DELETE CASCADE,
            kind TEXT NOT NULL, ts TEXT, target TEXT, locator TEXT, short_text TEXT, data TEXT)''')
        conn.execute('CREATE INDEX IF NOT EXISTS facts_session ON facts(session_key)')
        conn.execute('CREATE INDEX IF NOT EXISTS facts_kind_target ON facts(kind,target)')
        conn.commit()
        return conn
    except BaseException:
        conn.close()
        raise


@contextlib.contextmanager
def writer(directory: Path, deadline: float | None = None) -> Iterator[sqlite3.Connection | None]:
    """Open the only read-write connection while holding the process lock."""
    with writer_lock(directory, deadline) as acquired:
        if not acquired:
            yield None
            return
        conn = _open_writer(directory)
        try:
            yield conn
        finally:
            conn.close()


def check_normalization(conn: sqlite3.Connection) -> None:
    row = conn.execute("SELECT value FROM meta WHERE key='normalization_version'").fetchone()
    if row != (NORMALIZATION_VERSION,):
        raise SessionIndexError('session index normalization changed; run porch sessions index build')


def _rebuild_normalization(conn: sqlite3.Connection) -> None:
    """Publish the new normalization and the entire FTS generation atomically."""
    conn.execute('BEGIN IMMEDIATE')
    try:
        conn.execute('DELETE FROM fts')
        rows = conn.execute('SELECT doc_id,text FROM docs')
        conn.executemany('INSERT INTO fts(rowid,text) VALUES(?,?)',
                         ((doc_id, normalize(text)) for doc_id, text in rows))
        conn.execute("UPDATE meta SET value=? WHERE key='normalization_version'", (NORMALIZATION_VERSION,))
        conn.commit()
    except BaseException:
        conn.rollback()
        raise


def _delete_session(conn, key):
    conn.execute('DELETE FROM fts WHERE rowid IN (SELECT doc_id FROM docs WHERE session_key=?)', (key,))
    conn.execute('DELETE FROM sessions WHERE session_key=?', (key,))


def _gc_sources(conn, keys):
    conn.executemany('DELETE FROM sources WHERE source_key=? AND NOT EXISTS '
                     '(SELECT 1 FROM session_sources WHERE source_key=?)', ((key, key) for key in keys))


def replace_session(conn: sqlite3.Connection, session: dict, documents: list[dict],
                    source_links: list[dict], aliases: list[dict], edges: list[dict]) -> None:
    """Atomically replace one extracted session and all of its dependent rows.

    Sources must already exist. All values are bound parameters; a failure
    rolls back the old session, its documents and its FTS entries together.
    """
    check_normalization(conn)
    session_columns = ('session_key', 'harness', 'native_id', 'store', 'title', 'cwd',
                       'models_json', 'created', 'updated', 'surface', 'class',
                       'class_basis', 'status_json', 'content_state')
    doc_columns = ('session_key', 'kind', 'origin', 'ts', 'model', 'locator_json',
                   'source_key', 'source_rev', 'text')
    key = session['session_key']
    conn.execute('BEGIN IMMEDIATE')
    try:
        _delete_session(conn, key)
        conn.execute(f"INSERT INTO sessions ({','.join(session_columns)}) VALUES ({','.join('?' for _ in session_columns)})",
                     tuple(session.get(c) for c in session_columns))
        for doc in documents:
            row = dict(doc, session_key=key)
            cursor = conn.execute(f"INSERT INTO docs ({','.join(doc_columns)}) VALUES ({','.join('?' for _ in doc_columns)})",
                                  tuple(row.get(c) for c in doc_columns))
            conn.execute('INSERT INTO fts(rowid,text) VALUES(?,?)', (cursor.lastrowid, normalize(row['text'])))
        conn.executemany('INSERT INTO session_sources(session_key,source_key,role,fingerprint,extractor_version) VALUES(?,?,?,?,?)',
                         [(key, item['source_key'], item['role'], item.get('fingerprint'), item.get('extractor_version')) for item in source_links])
        for item in aliases:
            if item['kind'] == 'desktop':
                conn.execute('DELETE FROM aliases WHERE alias=? AND session_key<>?', (item['alias'], key))
        conn.executemany('INSERT INTO aliases(alias,session_key,kind) VALUES(?,?,?)',
                         [(item['alias'], key, item['kind']) for item in aliases])
        conn.executemany('INSERT INTO edges(session_key,type,target_key,unresolved,basis,locator_json) VALUES(?,?,?,?,?,?)',
                         [(key, item['type'], item.get('target_key'), item.get('unresolved'), item.get('basis'),
                           json.dumps(item.get('locator')) if item.get('locator') is not None else None) for item in edges])
        conn.executemany('INSERT INTO facts(session_key,kind,ts,target,locator,short_text,data) VALUES(?,?,?,?,?,?,?)',
                         [(key, f['kind'], f.get('ts'), f.get('target'), json.dumps(f.get('locator'), ensure_ascii=False),
                           f.get('short_text'), json.dumps(f.get('data'), ensure_ascii=False)) for f in session.get('_facts', [])])
        conn.execute('DELETE FROM unit_errors WHERE session_key=?', (key,))
        conn.commit()
    except BaseException:
        conn.rollback()
        raise

def _task_text(text):
    """Remove the recognizable executor contract, keeping only the task."""
    if not isinstance(text, str): return ''
    text = text.strip()
    if sessions.is_delegated_brief(text):
        marker = re.search(r'(?m)^---[ \t]*\r?$', text)
        return text[marker.end():].strip() if marker else ''
    return text


def session_payload(store: sessions.Store, ref: sessions.SessionRef,
                    fragments: list[dict], source_revisions: dict[str, str]) -> tuple[dict, list[dict], list[dict], list[dict]]:
    """Interpret a fully read session into index rows; caller verifies source revisions.

    source_revisions maps file paths or database paths to their revision JSON.
    This function performs no I/O apart from the agy stale check's existing
    metadata comparison and does not publish a session.
    """
    turns = sessions.interpret_session(store, ref, fragments)
    sessions.refine_class(ref, turns)
    title_fragment = None
    if sessions.is_delegated_brief(ref.title):
        ref.title = None
        for turn in turns:
            for prompt in turn.get('prompts') or []:
                if (prompt.get('origin') or 'unknown') not in ('human', 'delegated', 'unknown'): continue
                text = _task_text(prompt.get('text'))
                if text:
                    ref.title = text.splitlines()[0][:120]
                    title_fragment = prompt
                    break
            if ref.title: break
    extra = ref.extra
    native_id = str(extra.get('native_id') or ref.session_id)
    key = ref.harness + ':' + native_id
    ref_dict = ref.as_dict()
    status = dict(extra.get('native_status') or {})
    status['open_turn'] = any(turn.get('open', False) for turn in turns)
    status['aborted_count'] = sum(bool(turn.get('aborted')) for turn in turns)
    status['final_basis'] = {}
    all_finals = []
    for turn in turns:
        finals = turn.get('finals') or []
        indexed_finals = [final for final in finals if isinstance(final.get('text'), str) and final['text']]
        if indexed_finals:
            basis = turn.get('final_basis') or 'none'
            status['final_basis'][basis] = status['final_basis'].get(basis, 0) + len(indexed_finals)
            all_finals.extend(indexed_finals)
    if all_finals:
        last = all_finals[-1]
        status['last_final'] = {'ts': last.get('ts'), 'locator': last.get('locator')}
    state = 'unavailable' if status.get('content_unavailable') else 'ok'
    if store.harness == 'agy' and sessions.agy_content_stale(ref, fragments):
        status['content_stale'] = True
    if status.get('content_stale'):
        state = 'stale'
    row = {'session_key': key, 'harness': ref.harness, 'native_id': native_id,
           'store': ref.store, 'title': ref.title, 'cwd': ref.cwd,
           'models_json': json.dumps(ref_dict.get('models', []), ensure_ascii=False),
           'created': ref_dict.get('created'), 'updated': ref_dict.get('updated'),
           'surface': extra.get('surface'), 'class': extra.get('session_class', 'unknown'),
           'class_basis': extra.get('class_basis'),
           'status_json': json.dumps(status, ensure_ascii=False), 'content_state': state}
    row['_facts'] = []
    if turns:
        turn = turns[-1]
        prompt = (turn.get('prompts') or [None])[-1]
        final = turn.get('final')
        anchor = prompt or final or {}
        row['_facts'].append({'kind':'last_turn', 'ts': turn.get('ts_end') or turn.get('ts_start'),
                              'locator':anchor.get('locator'),
                              'data':{'state':'aborted' if turn.get('aborted') else 'open' if turn.get('open') else 'closed',
                                      'prompt_ts':prompt.get('ts') if prompt else None,
                                      'final_ts':final.get('ts') if final else None,
                                      'abort_reason':turn.get('abort_reason')}})
    docs = []
    def add(kind: str, fragment: dict, origin: str | None = None) -> None:
        text = fragment.get('text')
        if not isinstance(text, str) or not text:
            return
        locator = dict(fragment.get('locator') or {})
        rel = fragment.get('rel') or {}
        native = fragment.get('native') or {}
        if rel.get('uuid'):
            locator['message_anchor'] = {'field': 'uuid', 'value': rel['uuid']}
        elif native.get('message_id') or rel.get('message_id'):
            locator['message_anchor'] = {'field': 'message_id', 'value': native.get('message_id') or rel.get('message_id')}
        elif rel.get('item_id'):
            locator['message_anchor'] = {'field': 'item_id', 'value': rel['item_id']}
        elif locator.get('table') in ('session_message', 'part') and locator.get('key') is not None:
            locator['message_anchor'] = {'field': 'locator_key', 'value': locator['key']}
        source = locator.get('file') or locator.get('db')
        docs.append({'kind': kind, 'origin': origin, 'ts': fragment.get('ts'),
                     'model': fragment.get('model'),
                     'locator_json': json.dumps(locator, ensure_ascii=False),
                     'source_key': source_key(source) if source else None, 'source_rev': source_revisions.get(source), 'text': text})
    if ref.title:
        matching_titles = [f for f in fragments if f.get('text') == ref.title and f.get('locator')]
        title_fragment = title_fragment or (matching_titles[-1] if matching_titles else {})
        add('title', {**title_fragment, 'text': ref.title,
                      'locator': title_fragment.get('locator') or ref.locator, 'ts': ref.updated})
    for turn in turns:
        for prompt in turn.get('prompts') or []:
            origin = prompt.get('origin') or 'unknown'
            if origin in ('human', 'delegated', 'unknown') and not (ref.harness == 'porch' and prompt.get('native_type') == 'meta.task'):
                add('prompt', prompt, origin)
        for answer in turn.get('finals') or []:
            add('answer', answer)
    aliases = []
    launcher = extra.get('launcher_key')
    if launcher:
        aliases.append({'alias': launcher, 'kind': 'launcher_key'})
    if ref.session_id != native_id:
        aliases.append({'alias': ref.harness + ':' + ref.session_id, 'kind': 'porch_ref'})
    edges = []
    for edge in extra.get('edges') or []:
        target_harness, target_id = edge.get('target_harness'), edge.get('target_id')
        target = f'{target_harness}:{target_id}' if target_harness and target_id else None
        edges.append({'type': edge['type'], 'target_key': target, 'unresolved': edge.get('unresolved'),
                      'basis': edge.get('basis'), 'locator': edge.get('locator')})
    return row, docs, aliases, edges
class SourceChanged(SessionIndexError):
    """A source changed during extraction; retry without publishing it."""

class FileSnapshot:
    """Hold file descriptors while a session is built and reject changed paths."""
    def __init__(self):
        self._files: dict[str, tuple[object, tuple]] = {}

    @staticmethod
    def _tuple(st: os.stat_result) -> tuple:
        return st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns

    def add(self, path: Path) -> None:
        name = str(path)
        if name in self._files:
            return
        if not stat.S_ISREG(path.stat().st_mode):
            raise SessionIndexError(f'FileSnapshot requires a plain non-SQLite file: {path}')
        fh = path.open('rb')
        try:
            if not stat.S_ISREG(os.fstat(fh.fileno()).st_mode) or fh.read(16) == b'SQLite format 3\x00':
                raise SessionIndexError(f'FileSnapshot requires a plain non-SQLite file: {path}')
            fh.seek(0)
            opened = self._tuple(os.fstat(fh.fileno()))
            if self._tuple(path.stat()) != opened:
                raise SourceChanged(f'source changed while opening: {path}')
            self._files[name] = (fh, opened)
        except BaseException:
            fh.close()
            raise

    def verify(self) -> None:
        for name, (fh, before) in self._files.items():
            try:
                current_fd = self._tuple(os.fstat(fh.fileno()))
                current_path = self._tuple(Path(name).stat())
            except OSError as exc:
                raise SourceChanged(f'source unavailable while reading: {name}') from exc
            if before != current_fd or before != current_path:
                raise SourceChanged(f'source changed during read: {name}')

    def revisions(self) -> dict[str, str]:
        return {name: json.dumps([stat, None], separators=(',', ':')) for name, (_, stat) in self._files.items()}

    def close(self) -> None:
        for fh, _ in self._files.values():
            fh.close()
        self._files.clear()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()


def extract_session(store: sessions.Store, ref: sessions.SessionRef,
                    source_revisions: dict[str, str],
                    source_conn: sqlite3.Connection | None = None) -> tuple[dict, list[dict], list[dict], list[dict]]:
    """Extract rows for one ref; the caller owns snapshot validation and commit.

    SQLite callers must hold their source read transaction for both the
    fingerprint and this extraction. No index rows are changed here.
    """
    stats: dict[str, int] = {}
    if store.kind == 'sqlite' and store.harness in ('devin', 'opencode'):
        if source_conn is None or not source_conn.in_transaction:
            raise SessionIndexError('SQLite session extraction requires the fingerprint read transaction')
        iterator = (sessions.devin_iter_fragments if store.harness == 'devin' else sessions.opencode_iter_fragments)
        stream = iterator(store, ref, 0, stats, source_conn)
        fragments = list(_devin_index_fragments(stream) if store.harness == 'devin' else stream)
    else:
        fragments = list(sessions.iter_all_fragments(store, ref, 0, stats))
    _check_extraction_stats(stats, store)
    return session_payload(store, ref, fragments, source_revisions)


def _check_extraction_stats(stats, store):
    if stats.get('changed_during_read'):
        raise SourceChanged('source changed during read; extraction remains pending')
    failures = {name: stats[name] for name in ('unreadable', 'malformed', 'truncated_tail', 'compressed_errors') if stats.get(name)}
    if failures or store.status == 'unavailable':
        raise SessionIndexError(f'session extraction incomplete: {failures or store.error}')


def _devin_index_fragments(fragments):
    """Keep turn and lineage evidence while releasing non-indexed text."""
    for fragment in fragments:
        if fragment.get('kind') not in ('prompt', 'assistant'):
            fragment.pop('text', None)
        yield fragment


def source_key(path: str) -> str:
    """Canonical key of a physical file or SQLite database."""
    return json.dumps([path], ensure_ascii=False, separators=(',', ':'))


def _db_revision(path: Path) -> str:
    errors: list[str] = []
    main = sessions._source_stat(path, errors)
    wal = sessions._source_stat(Path(str(path) + '-wal'), errors)
    if errors:
        raise SessionIndexError('; '.join(errors))
    if main is None:
        raise SessionIndexError(f'database unavailable: {path}')
    return json.dumps([main, wal], separators=(',', ':'))


def open_reader(directory: Path) -> sqlite3.Connection:
    """Read only; read-only connections never checkpoint the index."""
    require_sqlite()
    path = directory / 'index-v1.sqlite'
    conn = sessions.open_sqlite_ro(path)
    conn.execute('PRAGMA foreign_keys=ON')
    return conn


def _same_index(path: Path, identity) -> bool:
    try:
        current = path.stat()
    except FileNotFoundError:
        return False
    return identity is not None and (current.st_dev, current.st_ino) == (identity.st_dev, identity.st_ino)


@contextlib.contextmanager
def _checked_reader(directory: Path):
    """Translate a lost index into an actionable diagnostic for snapshot readers."""
    path = directory / 'index-v1.sqlite'
    try:
        identity = path.stat()
    except FileNotFoundError as exc:
        raise SessionIndexError('index dropped, run find again') from exc
    try:
        with contextlib.closing(open_reader(directory)) as conn:
            yield conn
            if not _same_index(path, identity):
                raise SessionIndexError('index dropped, run find again')
    except (sqlite3.Error, FileNotFoundError) as exc:
        if not _same_index(path, identity):
            raise SessionIndexError('index dropped, run find again') from exc
        raise


@dataclass
class Unit:
    session_key: str
    harness: str
    store: str
    sources: dict[str, str] = field(default_factory=dict)
    roles: dict[str, str] = field(default_factory=dict)
    newest_mtime: int = 0
    stores: dict[str, sessions.Store] = field(default_factory=dict, repr=False)
    discovery: object = field(default=None, repr=False)
    extra_databases: set[str] = field(default_factory=set, repr=False)
    extra_directories: set[str] = field(default_factory=set, repr=False)
    stat_only: set[str] = field(default_factory=set, repr=False)


# Cheap per-harness discovery and SQLite snapshot state
# Version 2 records stable native message anchors for revision-aware navigation.
EXTRACTOR_VERSIONS = {'claude-code': '9', 'claude-desktop': '9', 'codex': '11', 'devin': '4', 'opencode': '5', 'porch': '8', 'agy': '6', 'cursor': '3'}


def _revision(stat: tuple) -> str:
    return json.dumps([stat, None], separators=(',', ':'))


def _inaccessible_source(store: sessions.Store, source: sessions.Source) -> bool:
    if not source.error and source.stat is not None:
        return False
    path = Path(source.key[0])
    # The walker has already counted broken links and stat errors.
    if not source.error and not (path.is_symlink() and not path.exists()):
        store.walk_stats['unreadable_dirs'] = store.walk_stats.get('unreadable_dirs', 0) + 1
    return True


def units_claude(store: sessions.Store, prev: dict) -> Iterator[Unit]:
    for source in sessions.SOURCES['claude-code'](store):
        if _inaccessible_source(store, source):
            continue
        key = source_key(str(source.key[0]))
        yield Unit('claude-code:' + source.session_id, 'claude-code', store.name,
                   {key: _revision(source.stat)}, {key: 'content'}, source.stat[3], {key: store})


def units_desktop_code(store: sessions.Store, prev: dict) -> Iterator[Unit]:
    for source in sessions.SOURCES['claude-desktop'](store):
        if source.role != 'metadata':
            continue
        if _inaccessible_source(store, source):
            continue
        key = source_key(str(source.key[0]))
        revision = _revision(source.stat)
        cached = prev.get(key) if not getattr(prev, 'full', False) else None
        if cached and cached[1] == revision:
            session_key = cached[0]
        else:
            try:
                with FileSnapshot() as snapshot:
                    snapshot.add(Path(source.key[0]))
                    if snapshot.revisions()[str(source.key[0])] != revision:
                        raise SourceChanged('manifest changed during discovery')
                    with open(source.key[0], encoding='utf-8') as handle:
                        data = json.load(handle)
                    snapshot.verify()
                if not isinstance(data, dict):
                    raise ValueError('manifest is not an object')
                sid = data.get('cliSessionId')
                if not sid:
                    continue
                session_key = 'claude-code:' + str(sid)
            except (OSError, ValueError, SourceChanged) as exc:
                store.walk_stats['unreadable_dirs'] = store.walk_stats.get('unreadable_dirs', 0) + 1
                store.error = str(exc)
                continue
        yield Unit(session_key, 'claude-code', store.name, {key: revision},
                   {key: 'metadata'}, source.stat[3], {key: store})


def units_cowork(store: sessions.Store, prev: dict) -> Iterator[Unit]:
    for source in sessions.SOURCES['claude-desktop'](store):
        if source.role != 'metadata':
            continue
        if _inaccessible_source(store, source):
            continue
        path = Path(source.key[0])
        key = source_key(str(path))
        revision = _revision(source.stat)
        cached = prev.get(key) if not getattr(prev, 'full', False) else None
        if cached and cached[1] == revision:
            session_key = cached[0]
        else:
            with FileSnapshot() as snapshot:
                snapshot.add(path)
                if snapshot.revisions()[str(path)] != revision:
                    raise SourceChanged('Cowork manifest changed during discovery')
                with path.open(encoding='utf-8') as handle:
                    data = json.load(handle)
                snapshot.verify()
            if not isinstance(data, dict):
                raise SessionIndexError('Cowork manifest is not an object')
            session_key = 'claude-desktop:' + str(data.get('sessionId') or data.get('session_id') or path.stem)
        sources, roles, stores = {key: revision}, {key: 'metadata'}, {key: store}
        audit = path.with_suffix('') / 'audit.jsonl'
        errors = []
        stat = sessions._source_stat(audit, errors)
        if errors:
            raise SessionIndexError('; '.join(errors))
        newest = source.stat[3]
        if stat is not None:
            audit_key = source_key(str(audit))
            sources[audit_key], roles[audit_key], stores[audit_key] = _revision(stat), 'content', store
            newest = max(newest, stat[3])
        unit = Unit(session_key, 'claude-desktop', store.name, sources, roles, newest, stores)
        directory = path.with_suffix('')
        directory_stat = sessions._source_stat(directory, errors)
        if errors:
            raise SessionIndexError('; '.join(errors))
        if directory_stat is not None:
            directory_key = source_key(str(directory))
            unit.sources[directory_key] = _revision(directory_stat)
            unit.roles[directory_key] = 'metadata'
            unit.stores[directory_key] = store
            unit.extra_directories.add(directory_key)
            unit.newest_mtime = max(unit.newest_mtime, directory_stat[3])
        yield unit


def _porch_discovery_issue(prev, store, key, state, reason, revision):
    issues = getattr(prev, 'discovery_issues', None)
    if issues is None:
        raise (SourceChanged(reason) if state == 'pending' else SessionIndexError(reason))
    issues[(store.harness, store.name, key)] = (state, reason, revision)


def units_porch(store: sessions.Store, prev: dict) -> Iterator[Unit]:
    units = {}
    for source in sessions.SOURCES['porch'](store):
        if source.stat is None and not Path(source.key[0]).is_symlink():
            key = source_key(str(source.key[0]))
            state = 'error' if source.error else 'pending'
            if source.error:
                store.walk_stats['unreadable_dirs'] -= 1  # the enumerator counted this file, not a directory
            _porch_discovery_issue(prev, store, key, state,
                                   source.error or 'source disappeared during discovery', None)
            continue
        if _inaccessible_source(store, source):
            continue
        key = source_key(str(source.key[0]))
        sid = source.session_id
        unit = units.setdefault(sid, Unit('porch:' + sid, 'porch', store.name))
        unit.sources[key] = _revision(source.stat)
        unit.roles[key] = source.role
        unit.stores[key] = store
        unit.newest_mtime = max(unit.newest_mtime, source.stat[3])
    for sid, unit in units.items():
        run_dir = store.path / sid
        meta = run_dir / 'meta.json'
        key = source_key(str(meta))
        if key not in unit.sources:
            store.walk_stats['unreadable_dirs'] = store.walk_stats.get('unreadable_dirs', 0) + 1
            continue
        cached = prev.get(key) if not getattr(prev, 'full', False) else None
        if cached and cached[1] == unit.sources[key]:
            unit.session_key = cached[0]
        else:
            try:
                with FileSnapshot() as snapshot:
                    try:
                        snapshot.add(meta)
                    except FileNotFoundError as exc:
                        raise SourceChanged('run metadata disappeared during discovery') from exc
                    if snapshot.revisions()[str(meta)] != unit.sources[key]:
                        raise SourceChanged('run metadata changed during discovery')
                    try:
                        with meta.open(encoding='utf-8') as handle:
                            data = json.load(handle)
                    except (OSError, ValueError):
                        snapshot.verify()
                        raise
                    snapshot.verify()
                if not isinstance(data, dict):
                    raise ValueError('run metadata is not an object')
            except (SourceChanged, OSError, ValueError) as exc:
                state = 'pending' if isinstance(exc, SourceChanged) else 'error'
                reason = 'run metadata parse error: ' + str(exc) if isinstance(exc, ValueError) else str(exc)
                _porch_discovery_issue(prev, store, key, state, reason, unit.sources[key])
                continue
            unit.session_key = 'porch:' + str(data.get('run_id') or sid)
        for directory in (run_dir, run_dir / 'normalized'):
            errors = []
            stat = sessions._source_stat(directory, errors)
            if errors:
                raise SessionIndexError('; '.join(errors))
            if stat is not None:
                directory_key = source_key(str(directory))
                unit.sources[directory_key] = _revision(stat)
                unit.roles[directory_key] = 'metadata'
                unit.stores[directory_key] = store
                unit.extra_directories.add(directory_key)
                unit.newest_mtime = max(unit.newest_mtime, stat[3])
        delivery = run_dir / 'deliveries.jsonl'
        errors = []
        stat = sessions._source_stat(delivery, errors)
        if errors:
            raise SessionIndexError('; '.join(errors))
        if stat is None and delivery.is_symlink():
            _porch_discovery_issue(prev, store, source_key(str(delivery)), 'offline',
                                   'delivery source unavailable', None)
        if stat is not None:
            delivery_key = source_key(str(delivery))
            unit.sources[delivery_key], unit.roles[delivery_key], unit.stores[delivery_key] = _revision(stat), 'metadata', store
            unit.newest_mtime = max(unit.newest_mtime, stat[3])
        yield unit


class DiscoveryState(dict):
    def __init__(self, previous, gates, stack):
        super().__init__(previous)
        self.gates = gates
        self.stack = stack
        self.connections = {}
        self.revisions = {}
        self.rows = {}
        self.codex_shared = {}
        self.fingerprints = {}
        self.db_stores = {}
        self.agy_revisions = {}
        self.agy_shared_cache = {}
        self.discovery_issues = {}

    def database(self, path):
        if path not in self.connections:
            conn = sessions.open_sqlite_ro(path)
            self.stack.callback(conn.close)
            conn.execute('BEGIN')
            self.connections[path] = conn
        return self.connections[path]


def _codex_state_key(path: Path, sid: str) -> str:
    return 'state:' + str(path) + '#' + sid


def _codex_state_revision(row, edges) -> str:
    payload = json.dumps([row, sorted(edges, key=str)], sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(payload.encode('utf-8')).hexdigest()


def _codex_rows(conn):
    conn.row_factory = sqlite3.Row
    if not sessions.table_exists(conn, 'threads'):
        raise SessionIndexError('Codex state database has no threads table')
    rows = {str(row['id']): dict(row) for row in conn.execute('SELECT * FROM threads')}
    edges = {}
    if sessions.table_exists(conn, 'thread_spawn_edges'):
        for row in conn.execute('SELECT * FROM thread_spawn_edges'):
            edge = dict(row)
            edges.setdefault(str(edge['child_thread_id']), []).append(edge)
    return rows, edges


def units_codex(store: sessions.Store, prev: DiscoveryState) -> Iterator[Unit]:
    if store.kind != 'sqlite':
        for source in sessions.SOURCES['codex'](store):
            if _inaccessible_source(store, source):
                continue
            key = source_key(str(source.key[0]))
            yield Unit('codex:' + source.session_id, 'codex', store.name,
                       {key: _revision(source.stat)}, {key: 'content'}, source.stat[3], {key: store})
        return
    revision = _db_revision(store.path)
    prev.revisions[store.path] = revision
    prev.db_stores[store.path] = store
    newest_mtime = store.path.stat().st_mtime_ns
    gate = source_key(str(store.path))
    if prev.gates.get(gate) == revision:
        prefix = 'state:' + str(store.path) + '#'
        for key, (session_key, fingerprint) in prev.items():
            if key.startswith(prefix):
                yield Unit(session_key, 'codex', store.name, {key: fingerprint},
                           {key: 'metadata'}, newest_mtime, {key: store})
        return
    rows, edges = _codex_rows(prev.database(store.path))
    prev.rows[store.path] = (rows, edges)
    for sid, row in rows.items():
        key = _codex_state_key(store.path, sid)
        fingerprint = _codex_state_revision(row, edges.get(sid, []))
        yield Unit('codex:' + sid, 'codex', store.name, {key: fingerprint},
                   {key: 'metadata'}, newest_mtime, {key: store})


def _session_database_key(path: Path, sid: str) -> str:
    return 'session:' + str(path) + '#' + sid


def _database_fingerprints(store: sessions.Store, prev: DiscoveryState) -> dict[str, str]:
    if store.path not in prev.fingerprints:
        conn = prev.database(store.path)
        if store.harness == 'opencode' and not sessions.table_exists(conn, 'session_v2'):
            if not sessions.table_exists(conn, 'session'):
                raise UnsupportedStore('OpenCode database has no supported session table')
            fingerprints = {}
        else:
            fingerprints = sessions.session_fingerprints(store, conn)
        prev.fingerprints[store.path] = {
            str(sid): json.dumps(value, ensure_ascii=False, separators=(',', ':'), default=str)
            for sid, value in fingerprints.items()}
        if store.harness == 'opencode' and sessions.table_exists(conn, 'session'):
            legacy = {}
            for table in ('session', 'message', 'part'):
                if not sessions.table_exists(conn, table):
                    continue
                cursor = conn.execute('SELECT * FROM ' + table)
                names = [column[0] for column in cursor.description]
                for values in cursor:
                    row = dict(zip(names, values))
                    sid = str(row.get('id') if table == 'session' else row.get('session_id'))
                    legacy.setdefault(sid, []).append((table, row))
            for sid, rows in legacy.items():
                if sid not in fingerprints and any(table == 'session' for table, _ in rows):
                    prev.fingerprints[store.path]['legacy#' + sid] = hashlib.sha256(
                        json.dumps(rows, sort_keys=True, ensure_ascii=False, default=str).encode()).hexdigest()
    return prev.fingerprints[store.path]


def _units_session_database(store: sessions.Store, prev: DiscoveryState) -> Iterator[Unit]:
    revision = _db_revision(store.path)
    prev.revisions[store.path] = revision
    prev.db_stores[store.path] = store
    newest = store.path.stat().st_mtime_ns
    prefix = 'session:' + str(store.path) + '#'
    if prev.gates.get(source_key(str(store.path))) == revision:
        fingerprints = {key[len(prefix):]: fingerprint
                        for key, (session_key, fingerprint) in prev.items() if key.startswith(prefix)}
    else:
        fingerprints = _database_fingerprints(store, prev)
    for label, fingerprint in fingerprints.items():
        sid = label.removeprefix('legacy#')
        key = _session_database_key(store.path, label)
        yield Unit(store.harness + ':' + sid, store.harness, store.name,
                   {key: fingerprint}, {key: 'content'}, newest, {key: store})


def units_devin(store: sessions.Store, prev: DiscoveryState) -> Iterator[Unit]:
    yield from _units_session_database(store, prev)


def units_opencode_v2(store: sessions.Store, prev: DiscoveryState) -> Iterator[Unit]:
    yield from _units_session_database(store, prev)


def units_opencode_files(store: sessions.Store, prev: DiscoveryState) -> Iterator[Unit]:
    units, message_sessions = {}, {}
    sources = list(sessions.SOURCES['opencode'](store))
    for source in sources:
        path = Path(source.key[0])
        rel = path.relative_to(store.path).parts
        if _inaccessible_source(store, source):
            continue
        if rel[0] == 'session':
            sid = path.stem
            units.setdefault(sid, Unit('opencode:' + sid, 'opencode', store.name))
        elif rel[0] == 'message' and len(rel) > 2:
            message_sessions[path.stem] = rel[1]
    for source in sources:
        if source.stat is None or source.error:
            continue
        path = Path(source.key[0])
        rel = path.relative_to(store.path).parts
        sid = (path.stem if rel[0] == 'session' else rel[1] if rel[0] == 'message' and len(rel) > 2
               else message_sessions.get(rel[1]) if rel[0] == 'part' and len(rel) > 2 else None)
        unit = units.get(sid)
        if unit is None:
            continue
        key = source_key(str(path))
        unit.sources[key] = _revision(source.stat)
        unit.roles[key] = 'metadata' if rel[0] == 'session' else 'content'
        unit.stores[key] = store
        unit.newest_mtime = max(unit.newest_mtime, source.stat[3])
    for sid, unit in units.items():
        directories = {store.path / 'message' / sid, store.path / 'part'}
        directories.update(store.path / 'part' / mid for mid, owner in message_sessions.items() if owner == sid)
        for directory in directories:
            errors = []
            stat = sessions._source_stat(directory, errors)
            if errors:
                raise SessionIndexError('; '.join(errors))
            key = source_key(str(directory))
            unit.sources[key] = _revision(stat) if stat else 'missing'
            unit.roles[key] = 'content' if directory in (store.path / 'message' / sid, store.path / 'part') else 'metadata'
            unit.stores[key] = store
            unit.extra_directories.add(key)
            if stat:
                unit.newest_mtime = max(unit.newest_mtime, stat[3])
    yield from units.values()


def _agy_metadata_key(path: Path, sid: str) -> str:
    return 'agy-meta:' + str(path) + '#' + sid


def _agy_entry_fingerprint(entry) -> str:
    return hashlib.sha256(json.dumps(entry, sort_keys=True, ensure_ascii=False, default=str).encode()).hexdigest()


def units_agy(store: sessions.Store, prev: DiscoveryState) -> Iterator[Unit]:
    metadata = store.path / 'cache' / 'conversation_metadata.json'
    errors = []
    stat = sessions._source_stat(metadata, errors)
    if errors:
        raise SessionIndexError('; '.join(errors))
    units = {}
    if stat is not None:
        revision = _revision(stat)
        gate = 'agy-gate:' + str(metadata)
        prev.agy_revisions[metadata] = (revision, store)
        prefix = 'agy-meta:' + str(metadata) + '#'
        if prev.gates.get(gate) == revision:
            fingerprints = {key[len(prefix):]: fingerprint for key, (_, fingerprint) in prev.items() if key.startswith(prefix)}
        else:
            with FileSnapshot() as snapshot:
                snapshot.add(metadata)
                if snapshot.revisions()[str(metadata)] != revision:
                    raise SourceChanged('agy metadata changed during discovery')
                with metadata.open(encoding='utf-8') as handle:
                    data = json.load(handle)
                snapshot.verify()
            if not isinstance(data, dict) or not isinstance(data.get('conversations'), dict):
                raise SessionIndexError('invalid agy conversation metadata')
            fingerprints = {str(sid): _agy_entry_fingerprint(entry) for sid, entry in data['conversations'].items()}
        for sid, fingerprint in fingerprints.items():
            key = _agy_metadata_key(metadata, sid)
            units[sid] = Unit('agy:' + sid, 'agy', store.name, {key:fingerprint}, {key:'metadata'}, stat[3], {key:store})
    for source in sessions.SOURCES['agy'](store):
        if source.session_id is None:
            continue
        if _inaccessible_source(store, source):
            continue
        sid = source.session_id
        path = Path(source.key[0])
        key = source_key(str(path))
        if source.role == 'lineage':
            cached = prev.get(key) if not getattr(prev, 'full', False) else None
            revision = _revision(source.stat)
            if cached and cached[1] == revision:
                sid = cached[0].split(':', 1)[1]
            else:
                with FileSnapshot() as snapshot:
                    snapshot.add(path)
                    if snapshot.revisions()[str(path)] != revision:
                        raise SourceChanged('agy descriptor changed during discovery')
                    with path.open(encoding='utf-8') as handle:
                        descriptor = json.load(handle)
                    snapshot.verify()
                if not isinstance(descriptor, dict):
                    raise SessionIndexError('agy descriptor is not an object')
                sid = str(descriptor.get('conversationId') or sid)
        unit = units.setdefault(sid, Unit('agy:' + sid, 'agy', store.name))
        unit.sources[key], unit.roles[key], unit.stores[key] = _revision(source.stat), source.role, store
        unit.newest_mtime = max(unit.newest_mtime, source.stat[3])
        if path.suffix == '.db':
            unit.stat_only.add(key)
    brain = store.path / 'brain'
    if brain.is_dir():
        for path in brain.iterdir():
            if not path.is_dir():
                continue
            errors = []
            directory_stat = sessions._source_stat(path, errors)
            if errors or directory_stat is None:
                raise SessionIndexError('; '.join(errors) or 'agy conversation directory unavailable')
            unit = units.setdefault(path.name, Unit('agy:' + path.name, 'agy', store.name))
            key = source_key(str(path))
            unit.sources[key], unit.roles[key], unit.stores[key] = _revision(directory_stat), 'metadata', store
            unit.stat_only.add(key)
            unit.newest_mtime = max(unit.newest_mtime, directory_stat[3])
    yield from units.values()


def units_cursor(store: sessions.Store, prev: dict) -> Iterator[Unit]:
    """CLI chats and ACP sessions: one unit per session directory."""
    units: dict[str, Unit] = {}
    for source in sessions.SOURCES['cursor'](store):
        if not source.session_id or _inaccessible_source(store, source):
            continue
        path = str(source.key[0])
        if Path(path).name.endswith(('-wal', '-shm', '-journal')):
            # Volatile sqlite companions of store.db: -shm churns even on
            # read-only access, so _db_revision (main + WAL) is the only
            # stable fingerprint and these files are never tracked.
            continue
        key = source_key(path)
        sid = source.session_id  # 'cursor-cli:<uuid>' | 'cursor-acp:<uuid>'
        unit = units.setdefault(sid, Unit('cursor:' + sid, 'cursor', store.name))
        if Path(path).suffix in ('.db', '.sqlite', '.sqlite3'):
            # SQLite stores are fingerprinted by the _db_revision pair
            # (main + WAL) and verified at the end of extraction.
            unit.extra_databases.add(key)
            unit.sources[key] = 'missing' if source.stat is None else _db_revision(Path(path))
        else:
            unit.sources[key] = _revision(source.stat)
        unit.roles[key] = source.role
        unit.stores[key] = store
        unit.newest_mtime = max(unit.newest_mtime, source.stat[3])
    yield from units.values()


UNIT_BUILDERS = {'claude-code': units_claude, 'claude-desktop': units_desktop_code, 'codex': units_codex,
                 'devin': units_devin, 'opencode': units_opencode_v2, 'porch': units_porch, 'agy': units_agy,
                 'cursor': units_cursor}


# Whole-session extraction and source snapshot verification
def _select_codex_rollout(unit, snapshot, state_rows, paths, sid, stored):
    """Choose the reader-selected rollout while preserving its source snapshot."""
    state = unit.discovery
    paths.sort(key=lambda pair: (pair[0].stat().st_mtime_ns, str(pair[0])), reverse=True)
    selected = None
    if stored:
        wanted = Path(stored)
        names = [str(wanted), str(wanted) + '.zst' if not str(wanted).endswith('.zst') else str(wanted)[:-4]]
        for name in names:
            candidate = Path(name)
            stat_errors: list[str] = []
            candidate_stat = sessions._source_stat(candidate, stat_errors)
            if stat_errors:
                raise SessionIndexError('; '.join(stat_errors))
            if candidate_stat is not None and all(path != candidate for path, _ in paths):
                snapshot.add(candidate)
                key = source_key(str(candidate))
                owner = paths[0][1] if paths else sessions.Store('codex', 'sessions', state_rows[0][0].path.parent / 'sessions', 'jsonl_dir')
                unit.sources[key] = snapshot.revisions()[str(candidate)]
                unit.roles[key] = 'content'
                unit.stores[key] = owner
                paths.append((candidate, owner))
                break
        selected = next((pair for name in names for pair in paths if str(pair[0]) == name), None)
        if selected is None:
            selected = next((pair for pair in paths if pair[0].name in {Path(name).name for name in names}), None)
    if selected is None and paths:
        selected = next((pair for pair in paths if not str(pair[0]).endswith('.zst') or not pair[0].with_suffix('').is_file()), paths[0])
    if paths:
        store = paths[0][1]
        home = store.path.parent if store.path.name in ('sessions', 'archived_sessions') else store.path
        if home not in state.codex_shared:
            state_paths, state_edges, state_sources = {}, {}, {}
            for db_path in state.revisions:
                if state.db_stores[db_path].harness != 'codex':
                    continue
                if db_path not in state.rows:
                    state.rows[db_path] = _codex_rows(state.database(db_path))
                rows, all_edges = state.rows[db_path]
                for thread_id, thread in rows.items():
                    if thread.get('rollout_path'):
                        state_paths.setdefault(thread_id, thread['rollout_path'])
                        state_sources.setdefault(thread_id, db_path)
                for thread_id, thread_edges in all_edges.items():
                    if thread_edges and thread_edges[0].get('parent_thread_id'):
                        state_edges.setdefault(thread_id, (thread_edges[0]['parent_thread_id'], db_path))
            state.codex_shared[home] = sessions.codex_rollout_shared(
                store, state_snapshot=(state_paths, state_edges, state_sources))
        choice = state.codex_shared[home][0].get(sid)
        if choice:
            selected = next((pair for pair in paths if pair[0] == choice[0]), selected)
    return selected


def _codex_index_records(records):
    """Avoid serializing compaction history that cannot affect indexed turns."""
    for line, record in records:
        if record.get('type') == 'compacted':
            record['payload'] = {}
        yield line, record


def extract_codex_unit(unit: Unit) -> tuple[dict, list[dict], list[dict], list[dict]]:
    sid = unit.session_key.split(':', 1)[1]
    state = unit.discovery
    state_rows = []
    for key in unit.sources:
        if not key.startswith('state:'):
            continue
        store = unit.stores[key]
        if store.path not in state.rows:
            state.rows[store.path] = _codex_rows(state.database(store.path))
        rows, edges = state.rows[store.path]
        row = rows.get(sid)
        if row is None or _codex_state_revision(row, edges.get(sid, [])) != unit.sources[key]:
            raise SourceChanged('Codex state changed after discovery')
        state_rows.append((store, row, edges.get(sid, [])))
    collector = sessions_facts.FactCollector()
    with FileSnapshot() as snapshot:
        paths = []
        for key in unit.sources:
            if key.startswith('state:'):
                continue
            if key in unit.extra_databases:
                continue
            if unit.sources[key] == 'missing':
                continue
            path = Path(json.loads(key)[0])
            try:
                snapshot.add(path)
            except FileNotFoundError as exc:
                raise SourceChanged('rollout disappeared after discovery') from exc
            if snapshot.revisions()[str(path)] != unit.sources[key]:
                raise SourceChanged('rollout changed after discovery')
            paths.append((path, unit.stores[key]))
        row = state_rows[0][1] if state_rows else {}
        parents = state_rows[0][2] if state_rows else []
        parent = parents[0].get('parent_thread_id') if parents else None
        stored = row.get('rollout_path')
        selected = _select_codex_rollout(unit, snapshot, state_rows, paths, sid, stored)
        if selected:
            path, store = selected
            alternatives = [str(p) for p, _ in paths if p != path]
            state_db = state_rows[0][0].path if state_rows else None
            shared = ({sid:(path, alternatives, 'ambiguous' if not stored and alternatives else None)},
                      {sid:(parent, state_db)} if parent else {}, {sid:state_db} if state_db else {})
            ref = sessions.codex_ref_for_rollout(store, path, shared)
            if ref is None or ref.session_id != sid:
                raise SessionIndexError('cannot build Codex rollout reference')
            _register_ref_sources(unit, ref, snapshot, store)
            stats = {}
            records = _codex_index_records(sessions.iter_jsonl(path, stats))
            fragments = list(collector.stream(sessions._codex_record_fragments(store, ref, path, records, 0)))
            snapshot.verify()
            _check_extraction_stats(stats, store)
        else:
            store = state_rows[0][0]
            ref = sessions.SessionRef('codex', store.name, sid, extra={'native_id':sid})
            sessions._codex_enrich_ref(ref, state_parent=parent, state_db=store.path,
                                       meta={'source':row.get('source'), 'originator':row.get('originator')})
            fragments = []
        native_title = row.get('title') or row.get('name') or row.get('preview') or row.get('first_user_message') or ''
        ref.title = (native_title if sessions.is_delegated_brief(native_title) else native_title[:160]) or ref.title
        ref.cwd = row.get('cwd') or ref.cwd
        ref.created = ref.created or row.get('created_at')
        ref.updated = row.get('updated_at_ms') or ref.updated
        native_title_prefixed = sessions.is_delegated_brief(ref.title)
        result = session_payload(store, ref, fragments, snapshot.revisions())
        result[0]['_facts'].extend(collector.facts())
        if state_rows and not native_title_prefixed and (row.get('title') or row.get('name') or row.get('preview') or row.get('first_user_message')):
            state_key = _codex_state_key(state_rows[0][0].path, sid)
            for document in result[1]:
                if document['kind'] == 'title':
                    document['locator_json'] = json.dumps({'db': str(state_rows[0][0].path), 'table': 'threads', 'key': sid})
                    document['source_key'] = state_key
                    document['source_rev'] = unit.sources[state_key]
        if not paths:
            result[0]['content_state'] = 'unavailable'
            result[1].clear()
        snapshot.verify()
        _verify_extra_databases(unit)
        return result


def _register_ref_sources(unit: Unit, ref: sessions.SessionRef, snapshot: FileSnapshot, store: sessions.Store) -> None:
    sources = list(ref.extra.get('sources') or [])
    if ref.extra.get('selected_rollout'):
        sources.append({'path': ref.extra['selected_rollout'], 'role': 'content'})
    for path in (ref.extra.get('native_status') or {}).get('alternates') or []:
        sources.append({'path': path, 'role': 'content'})
    for source in sources:
        path = source.get('path')
        db = source.get('db')
        if db and any(unit.stores[key].path == Path(db) and key.startswith(('state:', 'session:')) for key in unit.sources):
            continue
        path = path or db
        if not path:
            continue
        key = source_key(str(path))
        if key in unit.sources:
            continue
        if db:
            fingerprint = _db_revision(Path(path))
            unit.extra_databases.add(key)
        elif Path(path).is_dir():
            stat = sessions._source_stat(Path(path))
            if stat is None:
                raise SourceChanged('source directory disappeared during extraction')
            fingerprint = _revision(stat)
            unit.extra_directories.add(key)
        else:
            try:
                snapshot.add(Path(path))
                fingerprint = snapshot.revisions()[str(path)]
            except FileNotFoundError:
                fingerprint = 'missing'
        unit.sources[key] = fingerprint
        unit.roles[key] = source.get('role') or 'metadata'
        unit.stores[key] = store


def _verify_extra_databases(unit: Unit) -> None:
    for key in unit.extra_databases:
        if unit.sources[key] != 'missing' and _db_revision(Path(json.loads(key)[0])) != unit.sources[key]:
            raise SourceChanged('additional database changed during extraction')


def _unit_source_kind(unit: Unit, key: str) -> str:
    return 'sqlite' if key.startswith(('state:', 'session:')) or key in unit.extra_databases else 'file'


def extract_agy_unit(unit: Unit) -> tuple[dict, list[dict], list[dict], list[dict]]:
    store = next(iter(unit.stores.values()))
    sid = unit.session_key.split(':', 1)[1]
    metadata = store.path / 'cache' / 'conversation_metadata.json'
    meta_key = _agy_metadata_key(metadata, sid)
    with FileSnapshot() as snapshot:
        for key, fingerprint in unit.sources.items():
            if key.startswith('agy-meta:') or key in unit.stat_only or fingerprint == 'missing':
                continue
            path = json.loads(key)[0]
            snapshot.add(Path(path))
            if snapshot.revisions()[path] != fingerprint:
                raise SourceChanged('agy source changed after discovery')
        if meta_key in unit.sources:
            snapshot.add(metadata)
            revision = unit.discovery.agy_revisions[metadata][0]
            if snapshot.revisions()[str(metadata)] != revision:
                raise SourceChanged('agy metadata changed after discovery')
        if store.path not in unit.discovery.agy_shared_cache:
            unit.discovery.agy_shared_cache[store.path] = sessions.agy_shared(store)
        shared = unit.discovery.agy_shared_cache[store.path]
        if meta_key in unit.sources and (sid not in shared[0] or _agy_entry_fingerprint(shared[0][sid]) != unit.sources[meta_key]):
            raise SourceChanged('agy metadata entry changed after discovery')
        ref = sessions.agy_ref_for_conversation(store, sid, shared)
        for source in ref.extra.get('sources', []):
            if source.get('role') == 'lineage' and source.get('path') and source_key(str(source['path'])) not in unit.sources:
                raise SourceChanged('agy lineage source appeared after discovery')
        ref.extra['sources'] = [source for source in ref.extra.get('sources', []) if source.get('path') != str(metadata)]
        _register_ref_sources(unit, ref, snapshot, store)
        native_title_prefixed = sessions.is_delegated_brief(ref.title)
        result = extract_session(store, ref, snapshot.revisions())
        if ref.title and not native_title_prefixed:
            annotation = store.path / 'annotations' / (sid + '.pbtxt')
            title_source = annotation if sessions._agy_annotation_title(store, sid) else metadata
            title_revision = snapshot.revisions().get(str(title_source))
            if title_revision:
                for document in result[1]:
                    if document['kind'] == 'title':
                        document['locator_json'] = json.dumps({'file': str(title_source)}, ensure_ascii=False)
                        document['source_key'] = meta_key if title_source == metadata else source_key(str(title_source))
                        document['source_rev'] = unit.sources[meta_key] if title_source == metadata else title_revision
        if result[0]['content_state'] == 'unavailable':
            result[1].clear()
        snapshot.verify()
        for key in unit.stat_only:
            errors = []
            stat = sessions._source_stat(Path(json.loads(key)[0]), errors)
            if errors or stat is None or _revision(stat) != unit.sources[key]:
                raise SourceChanged('agy metadata source changed during extraction')
        _verify_extra_databases(unit)
        return result


def extract_file_unit(unit: Unit) -> tuple[dict, list[dict], list[dict], list[dict]]:
    store = next(iter(unit.stores.values()))
    with FileSnapshot() as snapshot:
        for key, fingerprint in unit.sources.items():
            if fingerprint == 'missing' or key in unit.extra_databases or key in unit.extra_directories:
                continue
            path = json.loads(key)[0]
            snapshot.add(Path(path))
            if snapshot.revisions()[path] != fingerprint:
                raise SourceChanged('source changed after discovery: ' + path)
        if unit.harness == 'porch':
            meta_key = next((key for key in unit.sources if Path(json.loads(key)[0]).name == 'meta.json'), None)
            if meta_key is None:
                raise SessionIndexError('run has no meta.json')
            run_dir = Path(json.loads(meta_key)[0]).parent
            with (run_dir / 'meta.json').open(encoding='utf-8') as handle:
                meta = json.load(handle)
            if not isinstance(meta, dict):
                raise SessionIndexError('run metadata is not an object')
            artifact = Path(meta['artifacts_dir']) if meta.get('artifacts_dir') else None
            if artifact and artifact.is_dir():
                for directory in (artifact, artifact / 'normalized'):
                    if not directory.is_dir():
                        continue
                    stat = sessions._source_stat(directory)
                    key = source_key(str(directory))
                    unit.sources[key], unit.roles[key], unit.stores[key] = _revision(stat), 'metadata', store
                    unit.extra_directories.add(key)
                paths = [artifact / 'final.txt'] + list((artifact / 'normalized').glob('*.jsonl'))
                for path in paths:
                    if path.is_file():
                        snapshot.add(path)
                        key = source_key(str(path))
                        unit.sources[key], unit.roles[key], unit.stores[key] = snapshot.revisions()[str(path)], 'content', store
            ref = sessions.porch_ref_for_run(store, run_dir)
            if ref and sessions.is_delegated_brief(meta.get('task')):
                ref.title = meta['task']
        elif unit.harness == 'opencode':
            meta_key = next((key for key in unit.sources if unit.roles[key] == 'metadata'), None)
            if meta_key is None:
                raise SessionIndexError('OpenCode session has no metadata')
            ref = sessions.opencode_file_ref(store, Path(json.loads(meta_key)[0]), set())
        else:
            meta_key = next((key for key in unit.sources if unit.roles[key] == 'metadata' and key not in unit.extra_directories), None)
            if meta_key is None:
                raise SessionIndexError('Cowork session has no manifest')
            ref = sessions.claude_desktop_ref_for_file(store, Path(json.loads(meta_key)[0]))
        if ref is None or ref.harness + ':' + str(ref.extra.get('native_id') or ref.session_id) != unit.session_key:
            raise SessionIndexError('session metadata identity changed during extraction')
        _register_ref_sources(unit, ref, snapshot, store)
        stats = {}
        fragments = list(sessions.iter_all_fragments(store, ref, 0, stats))
        cowork_content = unit.harness != 'claude-desktop' or any(
            role == 'content' and unit.sources[key] != 'missing' and key not in unit.extra_directories
            for key, role in unit.roles.items())
        if unit.harness == 'claude-desktop' and not cowork_content and stats.get('unreadable') == 1:
            stats.pop('unreadable')
        if unit.harness == 'porch' and stats.get('unreadable') == 1:
            # The Porch reader records the absence of normalized output here;
            # a run may legitimately have only metadata or a saved final.
            local_norm = run_dir / 'normalized'
            remote_norm = artifact / 'normalized' if artifact else None
            if not list(local_norm.glob('*.jsonl')) and (remote_norm is None or not list(remote_norm.glob('*.jsonl'))):
                stats.pop('unreadable')
        snapshot.verify()
        _check_extraction_stats(stats, store)
        result = session_payload(store, ref, fragments, snapshot.revisions())
        if unit.harness == 'porch':
            meta_path = str(run_dir / 'meta.json')
            task = _task_text(meta.get('task'))
            if task:
                result[0]['_facts'].append({'kind':'status_text','target':'task',
                    'ts':meta.get('started_at') or meta.get('launched_at'),
                    'locator':{'file':meta_path},'short_text':task[:160]})
            delivery_path = run_dir / 'deliveries.jsonl'
            status = json.loads(result[0]['status_json'])
            status.update(deliveries_malformed=0, deliveries_count=0, deliveries_collectors_count=0)
            if source_key(str(delivery_path)) in unit.sources:
                facts, malformed, count, collectors = _delivery_facts(delivery_path, unit.session_key.split(':', 1)[1])
                result[0]['_facts'].extend(facts)
                status['deliveries_malformed'] = malformed
                status['deliveries_count'] = count
                status['deliveries_collectors_count'] = collectors
            result[0]['status_json'] = json.dumps(status, ensure_ascii=False)
            for document in result[1]:
                if document['kind'] == 'title':
                    document['locator_json'] = json.dumps({'file': meta_path}, ensure_ascii=False)
                    document['source_key'] = source_key(meta_path)
                    document['source_rev'] = snapshot.revisions()[meta_path]
        porch_content = unit.harness != 'porch' or any(
            role == 'content' and unit.sources[key] != 'missing' and key not in unit.extra_directories
            for key, role in unit.roles.items())
        if not cowork_content or not porch_content:
            result[0]['content_state'] = 'unavailable'
            result[1].clear()
        snapshot.verify()
        for key in unit.extra_directories:
            if unit.sources[key] == 'missing':
                continue
            errors = []
            stat = sessions._source_stat(Path(json.loads(key)[0]), errors)
            if errors or stat is None or _revision(stat) != unit.sources[key]:
                raise SourceChanged('artifact directory changed during extraction')
        _verify_extra_databases(unit)
        return result


def _delivery_facts(path, run_id):
    facts = collections.deque(maxlen=20)
    malformed = 0
    count = 0
    collectors = set()
    with path.open('rb') as handle:
        for line_no, line in enumerate(handle, 1):
            try:
                item = json.loads(line)
                if (not isinstance(item, dict) or not {'ts','via','format','stdout','collector'} <= item.keys() or not isinstance(item.get('ts'), str) or
                        item.get('via') not in ('wait', 'foreground') or item.get('format') not in ('text', 'json') or
                        item.get('stdout') not in ('file', 'pipe', 'tty', 'other') or
                        (item.get('collector') is not None and not isinstance(item['collector'], str))):
                    raise ValueError('invalid delivery')
                facts.append({'kind': 'delivery', 'target': run_id, 'ts': item['ts'],
                              'locator': {'file': str(path), 'line': line_no},
                              'data': {key:item.get(key) for key in ('via','format','stdout','collector')}})
                count += 1
                if item['collector'] is not None:
                    collectors.add(item['collector'])
            except (ValueError, TypeError):
                malformed += 1
    return list(facts), malformed, count, len(collectors)


def _extract_claude_unit(unit: Unit):
    """Merge Claude transcript and Desktop manifest within one snapshot."""
    collector = sessions_facts.FactCollector()
    with FileSnapshot() as snapshot:
        for key in unit.sources:
            if unit.sources[key] == 'missing':
                continue
            if key in unit.extra_databases:
                continue
            path = json.loads(key)[0]
            try:
                snapshot.add(Path(path))
            except FileNotFoundError as exc:
                raise SourceChanged('source disappeared after discovery: ' + path) from exc
            if snapshot.revisions()[path] != unit.sources[key]:
                raise SourceChanged('source changed after discovery: ' + path)
        content = [key for key in unit.sources if unit.roles[key] == 'content' and unit.sources[key] != 'missing' and key not in unit.extra_databases]
        manifests = [key for key in unit.sources if unit.roles[key] == 'metadata' and unit.sources[key] != 'missing' and key not in unit.extra_databases]
        if content:
            key = content[0]
            store = unit.stores[key]
            ref = sessions.claude_ref_for_file(store, Path(json.loads(key)[0]))
            _register_ref_sources(unit, ref, snapshot, store)
            stats = {}
            fragments = list(collector.stream(sessions.iter_all_fragments(store, ref, 0, stats)))
            snapshot.verify()
            _check_extraction_stats(stats, store)
        else:
            store = next(iter(unit.stores.values()))
            ref = sessions.SessionRef('claude-code', unit.store, unit.session_key.split(':', 1)[1],
                                      extra={'native_id': unit.session_key.split(':', 1)[1]})
            fragments = []
        aliases = []
        title_manifest = None
        for key in manifests:
            path = Path(json.loads(key)[0])
            try:
                with path.open(encoding='utf-8') as handle:
                    data = json.load(handle)
            except (OSError, ValueError):
                snapshot.verify()
                raise
            if not isinstance(data, dict) or 'claude-code:' + str(data.get('cliSessionId')) != unit.session_key:
                raise SourceChanged('manifest mapping changed during extraction')
            desktop = sessions._claude_desktop_manifest(unit.stores[key], path, data)
            if desktop.title:
                ref.title = desktop.title
                title_manifest = path
            ref.cwd = desktop.cwd or ref.cwd
            if not ref.models:
                ref.models = desktop.models
            ref.created = ref.created or desktop.created
            ref.updated = desktop.updated or ref.updated
            ref.extra['surface'] = 'code-tab'
            ref.extra['titleSource'] = desktop.extra.get('titleSource')
            ref.extra.setdefault('native_status', {}).update(desktop.extra.get('native_status', {}))
            if desktop.extra.get('titleSource') is not None:
                ref.extra['native_status']['titleSource'] = desktop.extra['titleSource']
            ref.extra.setdefault('edges', []).extend(desktop.extra.get('edges', []))
            aliases.append({'alias': 'claude-desktop:' + desktop.session_id, 'kind': 'desktop'})
        native_title_prefixed = sessions.is_delegated_brief(ref.title)
        row, docs, payload_aliases, edges = session_payload(store, ref, fragments, snapshot.revisions())
        row['_facts'].extend(collector.facts())
        if title_manifest and not native_title_prefixed:
            for document in docs:
                if document['kind'] == 'title':
                    document['locator_json'] = json.dumps({'file': str(title_manifest)}, ensure_ascii=False)
                    document['source_key'] = source_key(str(title_manifest))
                    document['source_rev'] = snapshot.revisions()[str(title_manifest)]
        if not content:
            row['content_state'] = 'unavailable'
            docs = []
        snapshot.verify()
        _verify_extra_databases(unit)
        return row, docs, payload_aliases + aliases, edges



def extract_cursor_unit(unit: Unit) -> tuple[dict, list[dict], list[dict], list[dict]]:
    """One chat dir (meta.json + store.db) or ACP session dir under snapshot."""
    store = next(iter(unit.stores.values()))
    with FileSnapshot() as snapshot:
        for key, fingerprint in unit.sources.items():
            if fingerprint == 'missing':
                continue
            path = json.loads(key)[0]
            if key in unit.extra_databases:
                if _db_revision(Path(path)) != fingerprint:
                    raise SourceChanged('source changed after discovery: ' + path)
                continue
            snapshot.add(Path(path))
            if snapshot.revisions()[path] != fingerprint:
                raise SourceChanged('source changed after discovery: ' + path)
        try:
            if unit.store == 'chats':
                meta_key = next((key for key in unit.sources
                                 if Path(json.loads(key)[0]).name == 'meta.json'), None)
                if meta_key is None:
                    raise SessionIndexError('CLI chat has no meta.json')
                ref = sessions.cursor_cli_ref(store, Path(json.loads(meta_key)[0]).parent)
            else:
                dir_key = next((key for key in unit.sources
                                if Path(json.loads(key)[0]).name in ('meta.json', 'store.db')), None)
                if dir_key is None:
                    raise SessionIndexError('ACP session has no meta.json or store.db')
                ref = sessions.cursor_acp_ref(store, Path(json.loads(dir_key)[0]).parent)
        except (OSError, ValueError, TypeError, KeyError, sqlite3.Error, json.JSONDecodeError) as exc:
            raise SessionIndexError(f'cursor session unreadable: {exc}') from exc
        if ref.harness + ':' + str(ref.extra.get('native_id') or ref.session_id) != unit.session_key:
            raise SessionIndexError('session identity changed during extraction')
        result = extract_session(store, ref, snapshot.revisions())
        snapshot.verify()
        _verify_extra_databases(unit)
        return result


def extract_unit(unit: Unit) -> tuple[dict, list[dict], list[dict], list[dict]]:
    if unit.harness == 'agy':
        return extract_agy_unit(unit)
    if unit.harness == 'cursor':
        return extract_cursor_unit(unit)
    if unit.harness in ('porch', 'claude-desktop') or (unit.harness == 'opencode' and not any(k.startswith('session:') for k in unit.sources)):
        return extract_file_unit(unit)
    if unit.harness == 'codex':
        return extract_codex_unit(unit)
    if unit.harness in ('devin', 'opencode'):
        key = next(key for key in unit.sources if key.startswith('session:'))
        store = unit.stores[key]
        sid = unit.session_key.split(':', 1)[1]
        legacy = unit.harness == 'opencode' and key.startswith('session:' + str(store.path) + '#legacy#')
        label = 'legacy#' + sid if legacy else sid
        if _database_fingerprints(store, unit.discovery).get(label) != unit.sources[key]:
            raise SourceChanged('session database changed after discovery')
        source = unit.discovery.database(store.path)
        ref = (sessions.devin_ref_for_session(store, source, sid) if unit.harness == 'devin'
               else sessions.opencode_legacy_ref(store, source, sid) if legacy
               else sessions.opencode_v2_ref(store, source, sid))
        if ref is None:
            raise SourceChanged('session disappeared from database snapshot')
        with FileSnapshot() as snapshot:
            _register_ref_sources(unit, ref, snapshot, store)
            result = extract_session(store, ref, {str(store.path): unit.sources[key]}, source)
            snapshot.verify()
            _verify_extra_databases(unit)
        for document in result[1]:
            if document.get('source_key') == source_key(str(store.path)):
                document['source_key'] = key
        return result
    return _extract_claude_unit(unit)

# Coverage and synchronization orchestration
DEVIN_LIMIT = 'in-place edits of old messages that change no fingerprint field are not detected; `porch sessions index build --full` re-reads everything'


def _reason_class(reason: str) -> str:
    text = reason.lower()
    if 'changed' in text or 'disappeared after discovery' in text or 'disappeared during discovery' in text:
        return 'changed_during_read'
    if 'deadline' in text:
        return 'deadline'
    if 'enumeration incomplete' in text:
        return 'discovery_incomplete'
    if 'identity' in text or 'rollout reference' in text:
        return 'identity_mismatch'
    if 'permission' in text or 'denied' in text:
        return 'permission_denied'
    if 'malformed' in text or 'parse' in text or 'truncated' in text:
        return 'parse_error'
    if 'unavailable' in text or 'offline' in text:
        return 'unavailable'
    return 'extraction_error'


def _coverage(conn: sqlite3.Connection | None, harnesses=()) -> dict:
    def empty():
        return {'ok': 0, 'pending': 0, 'error': 0, 'offline': 0, 'unsupported': 0,
                'content_unavailable': 0, 'stale': 0, 'deleted': 0, 'extracted': 0,
                'reason_classes': {'pending': {}, 'error': {}}, 'error_units': 0,
                'unit_error_reasons': {}, 'reasons': {'pending': {}, 'error': {}},
                'unit_error_details': {}}
    counts = {harness: empty() for harness in harnesses}
    basis = {'native': 0, 'fallback': 0}
    verified = None
    discovery_complete = False
    if conn is not None:
        for harness, store_name, state, count in conn.execute('SELECT harness,store,state,count(*) FROM sources GROUP BY harness,store,state'):
            harness = _coverage_harness(harness, store_name)
            counts.setdefault(harness, empty())[state] += count
        for harness, store_name, state, reason, count in conn.execute("""SELECT harness,store,state,error,count(*) FROM sources
                WHERE state IN ('pending','error') AND error IS NOT NULL GROUP BY harness,store,state,error"""):
            harness = _coverage_harness(harness, store_name)
            entry = counts.setdefault(harness, empty())
            reasons = entry['reason_classes'][state]
            name = _reason_class(reason)
            reasons[name] = reasons.get(name, 0) + count
            entry['reasons'][state][reason] = entry['reasons'][state].get(reason, 0) + count
        if sessions.table_exists(conn, 'unit_errors'):
            for harness, name, count in conn.execute('SELECT harness,reason_class,count(*) FROM unit_errors GROUP BY harness,reason_class'):
                entry = counts.setdefault(harness, empty())
                entry['error_units'] += count
                entry['unit_error_reasons'][name] = count
            for harness, reason, count in conn.execute('SELECT harness,reason,count(*) FROM unit_errors GROUP BY harness,reason'):
                counts.setdefault(harness, empty())['unit_error_details'][reason] = count
            for entry in counts.values():
                # Sources may be shared by several failed units.
                entry['error'] = max(entry['error'], entry['error_units'])
        for harness, state, count in conn.execute('SELECT harness,content_state,count(*) FROM sessions GROUP BY harness,content_state'):
            entry = counts.setdefault(harness, empty())
            if state == 'unavailable':
                entry['content_unavailable'] += count
            elif state == 'stale':
                entry['stale'] += count
        for (raw,) in conn.execute('SELECT status_json FROM sessions WHERE status_json IS NOT NULL'):
            status = json.loads(raw)
            for name, count in (status.get('final_basis') or {}).items():
                if name.startswith('native:'):
                    basis['native'] += count
                elif name == 'fallback':
                    basis['fallback'] += count
        verified = conn.execute("SELECT min(verified_at) FROM sources WHERE state='ok' AND verified_at IS NOT NULL").fetchone()[0]
        row = conn.execute("SELECT value FROM meta WHERE key='discovery_complete'").fetchone()
        discovery_complete = row == ('1',)
        row = conn.execute("SELECT value FROM meta WHERE key='store_issues'").fetchone()
        if row:
            for harness, issue in json.loads(row[0]).items():
                entry = counts.setdefault(harness, empty())
                entry['error'] = max(entry['error'], issue.get('error', 0))
                entry['unsupported'] = issue.get('unsupported', 0)
        row = conn.execute("SELECT value FROM meta WHERE key='walk_complete_by_harness'").fetchone()
        if row:
            for harness, complete in json.loads(row[0]).items():
                counts.setdefault(harness, empty())['walk_complete'] = complete
    return {**counts, 'final_basis': basis, 'verified_at': verified,
            'discovery_complete': discovery_complete,
            'limits': [DEVIN_LIMIT] if 'devin' in counts else []}


def _stage_pending_sources(conn: sqlite3.Connection, unit: Unit, version: str, reason: str | None = None) -> None:
    for key, fingerprint in unit.sources.items():
        store = unit.stores[key]
        conn.execute('''INSERT INTO sources(source_key,harness,store,kind,stat_json,state,error,extractor_version,verified_at)
                        VALUES(?,?,?,?,?,'pending',?,?,NULL)
                        ON CONFLICT(source_key) DO UPDATE SET state='pending',error=excluded.error''',
                     (key, store.harness, store.name, _unit_source_kind(unit, key), fingerprint, reason, version))


def _memoize_error(conn, unit, version, exc, initial_sources):
    previous = conn.execute('SELECT details_json FROM unit_errors WHERE session_key=?', (unit.session_key,)).fetchone()
    previous_details = json.loads(previous[0]) if previous else {}
    details = {key: (unit.roles[key], _unit_source_kind(unit, key),
                     unit.stores[key].harness, unit.stores[key].name,
                     key not in initial_sources or (len(previous_details.get(key, ())) > 4 and previous_details[key][4]))
               for key in unit.sources}
    reason = str(exc) or type(exc).__name__
    conn.execute('INSERT OR REPLACE INTO unit_errors VALUES(?,?,?,?,?,?,?)',
                 (unit.session_key, unit.harness, version, json.dumps(unit.sources, sort_keys=True),
                  json.dumps(details, sort_keys=True), _reason_class(reason), reason))
    for key in unit.sources:
        store = unit.stores[key]
        conn.execute('''INSERT INTO sources(source_key,harness,store,kind,stat_json,state,error,extractor_version)
                        VALUES(?,?,?,?,?,'error',?,?) ON CONFLICT(source_key)
                        DO UPDATE SET state='error',error=excluded.error''',
                     (key, store.harness, store.name, _unit_source_kind(unit, key), unit.sources[key], reason, version))
    conn.commit()


def _advance_gates(conn, previous, units, failed_sources, failed_units):
    """Advance each gate only after its own sessions have committed."""
    pending = []
    for path, revision in previous.revisions.items():
        db_store = previous.db_stores[path]
        prefix = ('state:' if db_store.harness == 'codex' else 'session:') + str(path) + '#'
        expected = {(unit.session_key, key): (fingerprint, EXTRACTOR_VERSIONS[unit.harness])
                    for unit in units.values() for key, fingerprint in unit.sources.items() if key.startswith(prefix)}
        actual = {(sid, key): (fingerprint, version) for sid, key, fingerprint, version in conn.execute(
            'SELECT session_key,source_key,fingerprint,extractor_version FROM session_sources') if key.startswith(prefix)}
        if (expected != actual or any(key.startswith(prefix) for key in failed_sources)
                or any(sid in failed_units for sid, _ in expected)):
            continue
        try:
            current_revision = _db_revision(path)
        except (OSError, SessionIndexError) as exc:
            state = 'offline' if not path.exists() else 'error'
            conn.execute('''INSERT INTO sources(source_key,harness,store,kind,state,error)
                            VALUES(?,?,?,'sqlite',?,?) ON CONFLICT(source_key)
                            DO UPDATE SET state=excluded.state,error=excluded.error''',
                         (source_key(str(path)), db_store.harness, db_store.name, state, str(exc)))
            pending.append(db_store.harness)
            continue
        if current_revision != revision:
            conn.execute('''INSERT INTO sources(source_key,harness,store,kind,state,error)
                            VALUES(?,?,?,'sqlite','pending','database changed during read')
                            ON CONFLICT(source_key) DO UPDATE SET state='pending',error=excluded.error''',
                         (source_key(str(path)), db_store.harness, db_store.name))
            pending.append(db_store.harness)
            continue
        conn.execute('''INSERT INTO sources(source_key,harness,store,kind,stat_json,state,error,extractor_version,verified_at)
                        VALUES(?,?,?,'sqlite',?,'ok',NULL,?,?)
                        ON CONFLICT(source_key) DO UPDATE SET stat_json=excluded.stat_json,state='ok',error=NULL,
                        extractor_version=excluded.extractor_version,verified_at=excluded.verified_at''',
                     (source_key(str(path)), db_store.harness, db_store.name, revision, EXTRACTOR_VERSIONS[db_store.harness], sessions._iso(time.time())))
    for path, (revision, store) in previous.agy_revisions.items():
        prefix = 'agy-meta:' + str(path) + '#'
        expected = {(unit.session_key, key): (fingerprint, EXTRACTOR_VERSIONS[unit.harness])
                    for unit in units.values() for key, fingerprint in unit.sources.items() if key.startswith(prefix)}
        actual = {(sid, key): (fingerprint, version) for sid, key, fingerprint, version in conn.execute(
            'SELECT session_key,source_key,fingerprint,extractor_version FROM session_sources') if key.startswith(prefix)}
        if expected != actual or any(sid in failed_units for sid, _ in expected):
            continue
        stat = sessions._source_stat(path)
        if stat is None or _revision(stat) != revision:
            conn.execute('''INSERT INTO sources(source_key,harness,store,kind,state,error)
                            VALUES(?,'agy',?,'file','pending','metadata changed during read')
                            ON CONFLICT(source_key) DO UPDATE SET state='pending',error=excluded.error''',
                         ('agy-gate:' + str(path), store.name))
            pending.append('agy')
            continue
        conn.execute('''INSERT INTO sources(source_key,harness,store,kind,stat_json,state,error,extractor_version,verified_at)
                        VALUES(?,'agy',?,'file',?,'ok',NULL,?,?)
                        ON CONFLICT(source_key) DO UPDATE SET stat_json=excluded.stat_json,state='ok',error=NULL,
                        extractor_version=excluded.extractor_version,verified_at=excluded.verified_at''',
                     ('agy-gate:' + str(path), store.name, revision, EXTRACTOR_VERSIONS['agy'], sessions._iso(time.time())))
    return pending


def _restore_known_sources(conn, previous_details, units, roots, failed_sources, discovered_stores):
    """Re-stat extraction-time sources outside enumerated roots."""
    for (session_key, key), details in previous_details.items():
        role, kind, harness, store_name = details[:4]
        restore_inside_root = len(details) > 4 and details[4]
        unit = units.get(session_key)
        if unit is None or key in unit.sources or key.startswith(('state:', 'session:')):
            continue
        try:
            path = Path(json.loads(key)[0])
        except (ValueError, IndexError, TypeError):
            continue
        if harness == 'cursor' and path.name.endswith(('-wal', '-shm', '-journal')):
            # Companion files of store.db are covered by the db's
            # _db_revision fingerprint, never tracked as own sources.
            continue
        if not restore_inside_root and any(harness == root_harness and path.is_relative_to(root) for root_harness, root in roots):
            continue
        errors_found = []
        stat = sessions._source_stat(path, errors_found)
        if errors_found:
            failed_sources.add(key)
            conn.execute("UPDATE sources SET state='error',error=? WHERE source_key=?", ('; '.join(errors_found), key))
            continue
        if stat is None:
            fingerprint = 'missing'
        elif kind == 'sqlite':
            fingerprint = _db_revision(path)
        else:
            fingerprint = _revision(stat)
        unit.sources[key] = fingerprint
        unit.roles[key] = role
        unit.stores[key] = discovered_stores.get((harness, store_name)) or next(iter(unit.stores.values()))
        if kind == 'sqlite':
            unit.extra_databases.add(key)
        elif stat and path.is_dir():
            unit.extra_directories.add(key)
        if stat:
            unit.newest_mtime = max(unit.newest_mtime, stat[3])


def _commit_unit(conn, unit, version):
    """Publish a unit and its extraction-time sources."""
    row, docs, aliases, edges = extract_unit(unit)
    # Extractors may discover additional paths while building the ref.
    for key, fingerprint in unit.sources.items():
        store = unit.stores[key]
        conn.execute('''INSERT INTO sources(source_key,harness,store,kind,stat_json,state,error,extractor_version,verified_at)
                        VALUES(?,?,?,?,?,'pending',NULL,?,NULL)
                        ON CONFLICT(source_key) DO NOTHING''',
                     (key, store.harness, store.name, _unit_source_kind(unit, key), fingerprint, version))
    conn.commit()
    links = [{'source_key': key, 'role': unit.roles[key], 'fingerprint': fingerprint,
              'extractor_version': version} for key, fingerprint in unit.sources.items()]
    replace_session(conn, row, docs, links, aliases, edges)
    for key, fingerprint in unit.sources.items():
        conn.execute("UPDATE sources SET stat_json=?,state=?,error=NULL,extractor_version=?,verified_at=? WHERE source_key=?",
                     (fingerprint, 'offline' if fingerprint == 'missing' else 'ok', version, sessions._iso(time.time()), key))
    conn.commit()


def _store_builder(harness, store):
    if harness == 'claude-desktop':
        return {'code-sessions': units_desktop_code, 'cowork': units_cowork}.get(store.name)
    if harness == 'codex' and (store.name in ('catalog', 'summaries', 'history') or
                                (store.kind == 'sqlite' and not store.name.startswith('state:'))):
        return None
    if harness == 'devin' and store.kind != 'sqlite':
        return None
    if harness == 'opencode' and store.kind != 'sqlite':
        return units_opencode_files
    if harness == 'cursor':
        # Only the CLI chats and ACP session stores are indexed; the desktop
        # IDE dbs (globalStorage, workspaceStorage) are not.
        return units_cursor if store.kind == 'session_dirs' else None
    return UNIT_BUILDERS[harness]


def _merge_discovered(units, unit):
    existing = units.get(unit.session_key)
    if existing is None:
        units[unit.session_key] = unit
    elif unit.harness == 'opencode':
        def rank(candidate):
            keys = [key for key in candidate.sources if key.startswith('session:')]
            return 0 if not keys else 1 if all('#legacy#' in key for key in keys) else 2
        if rank(unit) > rank(existing):
            units[unit.session_key] = unit
    else:
        existing.sources.update(unit.sources)
        existing.roles.update(unit.roles)
        existing.stores.update(unit.stores)
        existing.stat_only.update(unit.stat_only)
        existing.extra_directories.update(unit.extra_directories)
        existing.extra_databases.update(unit.extra_databases)
        existing.newest_mtime = max(existing.newest_mtime, unit.newest_mtime)


def _coverage_harness(harness, store):
    return 'claude-code' if (harness, store) == ('claude-desktop', 'code-sessions') else harness


def _discover_stores(conn, selected, previous):
    """Keep enumeration and per-store deletion safety in one pass."""
    units: dict[str, Unit] = {}
    complete_walk = True
    failed_sources: set[str] = set()
    errors = 0
    error_counts = {}
    unsupported_counts = {}
    seen_stores = set()
    complete_stores = set()
    discovered_stores = {}
    roots = []
    for harness in selected:
        for store in sessions.stores_for(harness):
            builder = _store_builder(harness, store)
            if builder is None:
                continue
            seen_stores.add((harness, store.name))
            discovered_stores[(harness, store.name)] = store
            if store.kind != 'sqlite':
                roots.append((harness, store.path))
            store.walk_stats.clear()
            store.status, store.error = 'ok', None
            recorded = {key for (key,) in conn.execute(
                'SELECT source_key FROM sources WHERE harness=? AND store=?', (harness, store.name))}
            if not (store.path.is_file() if store.kind == 'sqlite' else store.path.is_dir()):
                # A previously indexed root may be an offline volume.
                if recorded:
                    complete_walk = False
                    errors += 1
                    failed_sources.update(recorded)
                    conn.executemany("UPDATE sources SET state='offline',error=? WHERE source_key=?",
                                     [('store unavailable', key) for key in recorded])
                continue
            try:
                for unit in builder(store, previous):
                    unit.discovery = previous
                    _merge_discovered(units, unit)
            except UnsupportedStore as exc:
                store.walk_stats['unreadable_dirs'] = 1
                store.error = str(exc)
                store.status = 'unsupported'
                unsupported_counts[harness] = unsupported_counts.get(harness, 0) + 1
            except (OSError, ValueError, sqlite3.Error, SessionIndexError) as exc:
                store.walk_stats['unreadable_dirs'] = 1
                store.error = str(exc)
            has_issue = False
            for (issue_harness, issue_store, key), (state, reason, revision) in previous.discovery_issues.items():
                if (issue_harness, issue_store) != (harness, store.name):
                    continue
                has_issue = True
                complete_walk = False
                failed_sources.add(key)
                if state == 'error':
                    errors += 1
                    error_counts[harness] = error_counts.get(harness, 0) + 1
                conn.execute('''INSERT INTO sources(source_key,harness,store,kind,stat_json,state,error)
                                VALUES(?,?,?,'file',?,?,?) ON CONFLICT(source_key)
                                DO UPDATE SET state=excluded.state,error=excluded.error''',
                             (key, harness, store.name, revision, state, reason))
            if store.walk_stats.get('unreadable_dirs'):
                complete_walk = False
                name = _coverage_harness(harness, store.name)
                broken_only = (store.walk_stats.get('broken_symlinks', 0) == store.walk_stats['unreadable_dirs'])
                if store.status != 'unsupported':
                    errors += 1
                    if not broken_only:
                        error_counts[name] = error_counts.get(name, 0) + 1
                failed_sources.update(recorded)
                for key in recorded:
                    state = 'error'
                    if broken_only:
                        try:
                            path = Path(json.loads(key)[0])
                            if sessions._source_stat(path) is not None:
                                continue
                            state = 'offline'
                        except (ValueError, TypeError, IndexError):
                            continue
                    conn.execute('UPDATE sources SET state=?,error=? WHERE source_key=?',
                                 (state, store.error or 'store enumeration incomplete', key))
            elif not has_issue:
                complete_stores.add((harness, store.name))
    missing = set()
    for key, harness, name in conn.execute('SELECT source_key,harness,store FROM sources').fetchall():
        if harness in selected and (harness, name) not in seen_stores:
            missing.add((harness, name))
            failed_sources.add(key)
            conn.execute("UPDATE sources SET state='offline',error='store unavailable' WHERE source_key=?", (key,))
    if missing:
        complete_walk = False
        errors += len(missing)
    return (units, complete_walk, failed_sources, errors, error_counts,
            unsupported_counts, discovered_stores, roots, complete_stores)


def _process_changed_units(conn, changed, deadline, failed_sources, old, result, counts, failed_units, progress=None):
    """Extract newest first and retain committed rows on failure."""
    totals = {h: sum(u.harness == h for u in changed) for h in counts} if progress else {}
    processed = {h: 0 for h in counts} if progress else {}
    for number, unit in enumerate(changed):
        if deadline is not None and time.monotonic() >= deadline:
            result['pending'] += len(changed) - number
            for pending in changed[number:]:
                counts[pending.harness]['pending'] += 1
                failed_units.add(pending.session_key)
                _stage_pending_sources(conn, pending, EXTRACTOR_VERSIONS[pending.harness], 'sync deadline reached')
            conn.commit()
            break
        if failed_sources.intersection(set(old.get(unit.session_key, {})) - set(unit.sources)):
            result['pending'] += 1
            counts[unit.harness]['pending'] += 1
            failed_units.add(unit.session_key)
            _stage_pending_sources(conn, unit, EXTRACTOR_VERSIONS[unit.harness], 'store enumeration incomplete')
            conn.commit()
            continue
        version = EXTRACTOR_VERSIONS[unit.harness]
        initial_sources = set(unit.sources)
        had_missing = 'missing' in unit.sources.values()
        _stage_pending_sources(conn, unit, version)
        conn.commit()
        try:
            _commit_unit(conn, unit, version)
            result['extracted'] += 1
            result['ok'] += 1
            counts[unit.harness]['extracted'] += 1
            counts[unit.harness]['ok'] += 1
            if not had_missing and 'missing' in unit.sources.values():
                result['error'] += 1
                counts[unit.harness]['error'] += 1
        except SourceChanged as exc:
            _stage_pending_sources(conn, unit, version, str(exc))
            conn.commit()
            result['pending'] += 1
            counts[unit.harness]['pending'] += 1
            failed_units.add(unit.session_key)
        except (OSError, ValueError, sqlite3.Error, SessionIndexError) as exc:
            _memoize_error(conn, unit, version, exc, initial_sources)
            result['error'] += 1
            counts[unit.harness]['error'] += 1
            failed_units.add(unit.session_key)
        if progress:
            processed[unit.harness] += 1
            n = processed[unit.harness]
            if n == 1 or n % 100 == 0 or n == totals[unit.harness]:
                progress(unit.harness, n, totals[unit.harness])


def _finish_sync(conn, selected, error_counts, unsupported_counts, complete_walk, counts, result, incomplete_harnesses):
    """Publish coverage metadata and report the committed snapshot."""
    row = conn.execute("SELECT value FROM meta WHERE key='store_issues'").fetchone()
    issues = json.loads(row[0]) if row else {}
    for harness in selected:
        issues[harness] = {'error': error_counts.get(harness, 0), 'unsupported': unsupported_counts.get(harness, 0)}
    conn.execute("INSERT OR REPLACE INTO meta(key,value) VALUES('store_issues',?)", (json.dumps(issues),))
    conn.execute("INSERT OR REPLACE INTO meta(key,value) VALUES('discovery_complete',?)", ('1' if complete_walk else '0',))
    row = conn.execute("SELECT value FROM meta WHERE key='walk_complete_by_harness'").fetchone()
    walks = json.loads(row[0]) if row else {}
    for harness in selected:
        walks[harness] = harness not in incomplete_harnesses
    conn.execute("INSERT OR REPLACE INTO meta(key,value) VALUES('walk_complete_by_harness',?)", (json.dumps(walks),))
    coverage = _coverage(conn, selected)
    for harness, transient in counts.items():
        coverage[harness]['extracted'] = transient['extracted']
        coverage[harness]['deleted'] = transient['deleted']
        coverage[harness]['pending'] = max(coverage[harness]['pending'], transient['pending'])
        coverage[harness]['error'] = max(coverage[harness]['error'], transient['error'])
    complete = complete_walk and not result['pending'] and not result['error']
    if any(coverage[h][state] for h in counts for state in ('pending', 'error', 'offline', 'unsupported')):
        complete = False
    response = {'synced': True, 'complete': complete, 'reason': None, **coverage}
    if not complete:
        response['next'] = 'porch sessions index build'
    conn.execute("INSERT OR REPLACE INTO meta(key,value) VALUES('last_sync_at',?)", (sessions._iso(time.time()),))
    conn.execute("INSERT OR REPLACE INTO meta(key,value) VALUES('last_sync_coverage',?)", (json.dumps(response, ensure_ascii=False),))
    conn.commit()
    return response


def _load_previous(conn, stack, full):
    """Load committed session links and cache gates for discovery."""
    previous: dict[str, tuple[str, str]] = {}
    old: dict[str, dict[str, tuple[str, str]]] = {}
    previous_details = {}
    for session_key, key, fingerprint, version in conn.execute(
            'SELECT session_key,source_key,fingerprint,extractor_version FROM session_sources'):
        previous[key] = (session_key, fingerprint)
        old.setdefault(session_key, {})[key] = (fingerprint, version)
    for session_key, key, role, kind, harness, store_name in conn.execute('''SELECT ss.session_key,ss.source_key,ss.role,s.kind,s.harness,s.store
             FROM session_sources ss JOIN sources s ON s.source_key=ss.source_key'''):
        previous_details[(session_key, key)] = (role, kind, harness, store_name)
    memos = {}
    for key, version, sources_json, details_json in conn.execute(
            'SELECT session_key,extractor_version,sources_json,details_json FROM unit_errors'):
        memos[key] = (version, json.loads(sources_json))
        for source, detail in json.loads(details_json).items():
            previous_details.setdefault((key, source), tuple(detail))
    if full:
        # A full refresh has no unchanged units, so it does not need the status cache.
        source_details = {}
        gates = {}
        source_states = dict(conn.execute('SELECT source_key,state FROM sources'))
    else:
        source_details = {key: (fingerprint, state, error, version)
                          for key, fingerprint, state, error, version in conn.execute(
                              'SELECT source_key,stat_json,state,error,extractor_version FROM sources')}
        gates = {key: value[0] for key, value in source_details.items()}
        source_states = {key: value[1] for key, value in source_details.items()}
    previous = DiscoveryState(previous, gates, stack)
    previous.source_details = source_details
    previous.full = full
    return previous, old, previous_details, source_states, memos


def _remove_obsolete_sessions(conn, old, units, selected, failed_sources, failed_units, deadline, changed, previous, result, counts, details, complete_stores):
    """Delete only sessions whose former sources were completely enumerated."""
    failed_keys = {key for unit in units.values() if unit.session_key in failed_units for key in unit.sources}
    for session_key in set(old) - set(units):
        if session_key.split(':', 1)[0] not in selected:
            continue
        if not old[session_key] or any(details.get((session_key, key), (None, None, None, None))[2:4] not in complete_stores
                                       for key in old[session_key]):
            continue
        if failed_sources.intersection(old[session_key]) or failed_keys.intersection(old[session_key]):
            continue
        if deadline is not None and time.monotonic() >= deadline:
            result['pending'] += 1
            counts[session_key.split(':', 1)[0]]['pending'] += 1
            break
        conn.execute('BEGIN IMMEDIATE')
        _delete_session(conn, session_key)
        conn.commit()
        result['deleted'] += 1
        counts[session_key.split(':', 1)[0]]['deleted'] += 1
    if changed or result['deleted']:
        _gc_sources(conn, previous)
    # Cursor store.db -wal/-shm/-journal companions are covered by the db's
    # _db_revision fingerprint and are never tracked as sources. Version 1
    # recorded some as own sources; sweep the unreferenced leftovers.
    conn.execute("""DELETE FROM sources WHERE harness='cursor' AND NOT EXISTS
                    (SELECT 1 FROM session_sources WHERE source_key=sources.source_key)
                    AND (source_key LIKE '%-wal"]' OR source_key LIKE '%-shm"]'
                         OR source_key LIKE '%-journal"]')""")
    conn.commit()


def sync(directory: Path, *, deadline: float | None = None, full: bool = False,
         harnesses: list[str] | None = None, progress=None, rebuild_normalization=False) -> dict:
    """Synchronize under the same process memory budget used by raw scans."""
    limit = sessions._nonnegative_env(sessions.MEMORY_ENV, 1536)
    monitor = sessions._memory_limit_monitor('index build', limit)
    try:
        result = _sync(directory, deadline=deadline, full=full, harnesses=harnesses, progress=progress, rebuild_normalization=rebuild_normalization)
        current = sessions._memory_usage_bytes() if limit else None
        if current is not None and current > limit * 1024 * 1024:
            sessions._memory_limit_exit('index build', current, limit)
        return result
    finally:
        if monitor:
            monitor[0].set()
            monitor[1].join()


def _sync(directory: Path, *, deadline: float | None = None, full: bool = False,
          harnesses: list[str] | None = None, progress=None, rebuild_normalization=False) -> dict:
    selected = list(UNIT_BUILDERS) if harnesses is None else list(harnesses)
    unknown = set(selected) - set(UNIT_BUILDERS)
    if unknown:
        raise ValueError('unsupported index harnesses: ' + ', '.join(sorted(unknown)))
    # A code-tab and its transcript form one unit and must be enumerated together.
    if 'claude-desktop' in selected or 'claude-code' in selected:
        selected = list(dict.fromkeys(selected + ['claude-code', 'claude-desktop']))
    with writer(directory, deadline) as conn, contextlib.ExitStack() as stack:
        if conn is None:
            reader = None
            try:
                if (directory / 'index-v1.sqlite').exists():
                    reader = open_reader(directory)
                coverage = _coverage(reader, selected)
            finally:
                if reader is not None:
                    reader.close()
            return {'synced': False, 'reason': 'another sync in progress', 'complete': False,
                    'next': 'porch sessions index build', **coverage}
        try:
            check_normalization(conn)
        except SessionIndexError:
            if full or rebuild_normalization:
                _rebuild_normalization(conn)
            else:
                raise
        sessions.reset_discovery_caches()
        previous, old, previous_details, source_states, memos = _load_previous(conn, stack, full)
        (units, complete_walk, failed_sources, errors, error_counts,
         unsupported_counts, discovered_stores, roots, complete_stores) = _discover_stores(conn, selected, previous)
        recorded_stores = set(conn.execute('SELECT DISTINCT harness,store FROM sources'))
        incomplete_harnesses = set()
        for pair in recorded_stores | set(discovered_stores):
            if pair[0] not in selected or pair in complete_stores:
                continue
            store = discovered_stores.get(pair)
            if pair in recorded_stores or (store is not None and store.path.exists()):
                incomplete_harnesses.add(_coverage_harness(*pair))
                incomplete_harnesses.add(pair[0])
        _restore_known_sources(conn, previous_details, units, roots, failed_sources, discovered_stores)
        for key, (_, memo_sources) in memos.items():
            if (key.split(':', 1)[0] in selected and key not in units
                    and not failed_sources.intersection(memo_sources)):
                conn.execute('DELETE FROM unit_errors WHERE session_key=?', (key,))
                _gc_sources(conn, memo_sources)
        for unit in units.values():
            if 'missing' in unit.sources.values():
                errors += 1
                error_counts[unit.harness] = error_counts.get(unit.harness, 0) + 1
        conn.commit()
        memoized = {u.session_key for u in units.values() if not full and
                    memos.get(u.session_key) == (EXTRACTOR_VERSIONS[u.harness], u.sources)}
        changed = [u for u in units.values() if u.session_key not in memoized and (full or old.get(u.session_key) !=
                   {key: (fingerprint, EXTRACTOR_VERSIONS[u.harness]) for key, fingerprint in u.sources.items()}
                   or any(source_states.get(key) == 'offline' and fingerprint != 'missing'
                          for key, fingerprint in u.sources.items()))]
        changed.sort(key=lambda u: (u.newest_mtime, u.session_key), reverse=True)
        failed_units = set(memoized)
        result = {'ok': len(units) - len(changed), 'pending': 0, 'error': errors,
                  'deleted': 0, 'extracted': 0}
        counts = {h:
                  {'ok': 0, 'pending': 0, 'error': 0, 'deleted': 0, 'extracted': 0} for h in selected}
        for name, count in error_counts.items():
            counts[name]['error'] = count
        changed_keys = {unit.session_key for unit in changed}
        # Incomplete enumeration cannot establish that a previously joined
        # source disappeared; retain the entire committed snapshot.
        for unit in units.values():
            if unit.session_key not in changed_keys and unit.session_key not in memoized:
                counts[unit.harness]['ok'] += 1
                version = EXTRACTOR_VERSIONS[unit.harness]
                for key, fingerprint in unit.sources.items():
                    if key in failed_sources:
                        continue
                    state = 'offline' if fingerprint == 'missing' else 'ok'
                    if previous.source_details.get(key) == (fingerprint, state, None, version):
                        continue
                    conn.execute("""UPDATE sources SET state=?,error=NULL,stat_json=?,extractor_version=?
                                    WHERE source_key=? AND (state != 'ok' OR error IS NOT NULL
                                    OR stat_json IS NOT ? OR extractor_version IS NOT ?)""",
                                 (state, fingerprint, version, key, fingerprint, version))
        conn.commit()
        _process_changed_units(conn, changed, deadline, failed_sources, old, result, counts, failed_units, progress)
        _remove_obsolete_sessions(conn, old, units, selected, failed_sources, failed_units,
                                  deadline, changed, previous, result, counts, previous_details, complete_stores)
        for harness in _advance_gates(conn, previous, units, failed_sources, failed_units):
            result['pending'] += 1
            counts[harness]['pending'] += 1
        return _finish_sync(conn, selected, error_counts, unsupported_counts, complete_walk, counts, result, incomplete_harnesses)

# Indexed literal search and revision-aware navigation

def parse_groups(groups: list[str]) -> list[list[str]]:
    parsed = []
    for group in groups:
        alternatives, part, pos = [], [], 0
        while pos < len(group):
            char = group[pos]
            if char == '\\' and pos + 1 < len(group) and group[pos + 1] == '|':
                part.append('|')
                pos += 2
                continue
            if char == '|':
                alternatives.append(''.join(part))
                part = []
            else:
                part.append(char)
            pos += 1
        alternatives.append(''.join(part))
        if any(not alternative for alternative in alternatives):
            raise ValueError('empty alternative in search group; use \\| for a literal bar')
        parsed.append(alternatives)
    return parsed


def _trigram_query(term: str) -> str | None:
    if len(term) < 3 or '\x00' in term:
        return None
    grams = [term[i:i + 3] for i in range(len(term) - 2)]
    if len(grams) > 8:
        grams = [grams[i * (len(grams) - 1) // 7] for i in range(8)]
    return ' AND '.join('"' + gram.replace('"', '""') + '"' for gram in dict.fromkeys(grams))


def _find_filters(harnesses, kind, origin, session_class, cwd, since, until):
    terms, params = [], []
    for column, value in (('d.kind', kind), ('d.origin', origin), ('s.class', session_class)):
        if value is not None:
            terms.append(column + '=?')
            params.append(value)
    if harnesses is not None:
        if not harnesses:
            terms.append('0')
        else:
            terms.append('s.harness IN (' + ','.join('?' for _ in harnesses) + ')')
            params.extend(harnesses)
    if cwd is not None:
        terms.append('instr(s.cwd,?)>0')
        params.append(cwd)
    for comparison, bound in (('>=', since), ('<=', until)):
        if bound is not None:
            terms.append('session_epoch(s.updated)' + comparison + '?')
            params.append(bound)
    return ' AND '.join(terms) if terms else '1', params


def _find_alternative(conn, term, where, params, candidates=None):
    query = _trigram_query(term)
    join = ' JOIN fts ON fts.rowid=d.doc_id' if query else ''
    extra = ' AND fts MATCH ?' if query else ''
    values = [*params, query] if query else list(params)
    if candidates is not None:
        if not candidates:
            return
        extra += ' AND s.session_key IN (SELECT value FROM json_each(?))'
        values.append(json.dumps(sorted(candidates)))
    sql = ("SELECT d.doc_id,d.session_key,d.kind,d.origin,d.ts,d.model,d.locator_json,d.source_key,d.source_rev,d.text "
           "FROM docs d JOIN sessions s ON s.session_key=d.session_key" + join + ' WHERE ' + where + extra)
    for row in conn.execute(sql, values):
        if _matches_normalized(row[9], term):
            yield row


def _snippet(text: str, terms: list[str], cap: int = 240) -> str:
    normalized = normalize(text)
    positions = [normalized.find(term) for term in terms if term in normalized]
    at = min(positions) if positions else 0
    if len(normalized) != len(text):
        # Map the normalized position back through combining-character clusters.
        cursor = offset = 0
        while cursor < len(text):
            end = cursor + 1
            while end < len(text) and unicodedata.combining(text[end]):
                end += 1
            length = len(normalize(text[cursor:end]))
            if offset + length > at:
                at = cursor
                break
            offset += length
            cursor = end
    start = max(0, min(len(text) - cap, at - cap // 3))
    return text[start:start + cap]


def _matches_normalized(text: str, term: str) -> bool:
    """Fast positive proof for ASCII matches; normalize all other cases exactly."""
    if term.isascii():
        position = text.find(term)
        while position >= 0:
            end = position + len(term)
            # An ASCII boundary cannot compose with the match's final character.
            if end == len(text) or ord(text[end]) < 128:
                return True
            position = text.find(term, position + 1)
    return term in normalize(text)


def _normalized_contains(text: str, term: str) -> int:
    return int(_matches_normalized(text, term))


def _evidence(row, session, terms, duplicates):
    doc_id, session_key, kind, origin, ts, model, raw_locator, source, revision, text = row
    anchor = json.loads(raw_locator) if raw_locator else {}
    loc = json.dumps({'v': 1, 'session': session_key, 'store': session['store'],
                      'source': source, 'revision': revision, 'anchor': anchor,
                      'kind': kind, 'text_digest': hashlib.sha256(text.encode('utf-8')).hexdigest()},
                     ensure_ascii=False, separators=(',', ':'))
    return {'kind': kind, 'origin': origin, 'ts': ts, 'model': model,
            'snippet': _snippet(text, terms), 'locator': loc,
            'show': 'porch sessions show --at ' + shlex.quote(loc), 'count': duplicates}


def find_in_snapshot(conn, groups, *, harnesses=None, kind=None, origin=None, session_class=None,
                     cwd=None, since=None, until=None, limit=20):
    """Find session-level literal group intersections in a committed snapshot."""
    check_normalization(conn)
    if not groups:
        raise ValueError('at least one search group is required')
    if limit < 0:
        raise ValueError('--limit must be non-negative')
    parsed = parse_groups(groups)
    conn.create_function('session_epoch', 1, sessions.ts_epoch)
    conn.create_function('normalized_contains', 2, _normalized_contains)
    where, params = _find_filters(harnesses, kind, origin, session_class, cwd, since, until)
    matches = [None] * len(parsed)
    alternatives = [None] * len(parsed)
    priority = {'title': 0, 'prompt': 1, 'answer': 2}
    candidates = None
    slow = False
    # A group containing only indexed alternatives can narrow later groups.
    order = sorted(range(len(parsed)), key=lambda i: not all(_trigram_query(normalize(a)) for a in parsed[i]))
    for group_no in order:
        group = parsed[group_no]
        docs, alt_counts = {}, []
        for raw in group:
            term = normalize(raw)
            short = _trigram_query(term) is None
            slow |= short
            alt_sessions = set()
            if short and candidates is not None:
                count_sql = ('SELECT count(DISTINCT d.session_key) FROM docs d JOIN sessions s '
                             'ON s.session_key=d.session_key WHERE ' + where + ' AND normalized_contains(d.text,?)')
                total = conn.execute(count_sql, [*params, term]).fetchone()[0]
            else:
                total = None
            for row in _find_alternative(conn, term, where, params, candidates if short else None):
                alt_sessions.add(row[1])
                if candidates is None or row[1] in candidates:
                    entry = docs.setdefault(row[1], {'ids': set(), 'best': row})
                    entry['ids'].add(row[0])
                    if (priority[row[2]], row[0]) < (priority[entry['best'][2]], entry['best'][0]):
                        entry['best'] = row
            alt_counts.append({'alternative': raw, 'sessions': total if total is not None else len(alt_sessions)})
        candidates = set(docs)
        matches[group_no] = docs
        alternatives[group_no] = alt_counts
    selected = candidates or set()
    session_rows = {}
    for row in conn.execute('SELECT session_key,harness,native_id,store,title,surface,class,cwd,models_json,created,updated '
                            'FROM sessions WHERE session_key IN (SELECT value FROM json_each(?))', (json.dumps(list(selected)),)):
        session_rows[row[0]] = dict(zip(('session_key','harness','native_id','store','title','surface','class','cwd','models_json','created','updated'), row))
    by_session = {key: [group[key] for group in matches] for key in selected}
    ranked = []
    for key, per_group in by_session.items():
        common = set.intersection(*(group['ids'] for group in per_group))
        best_kind = min(priority[group['best'][2]] for group in per_group)
        updated = sessions.ts_epoch(session_rows[key]['updated'])
        ranked.append((not bool(common), best_kind, -(updated if updated is not None else float('-inf')), key))
    ranked.sort()
    output = []
    for _, _, _, key in ranked[:limit or None]:
        session = session_rows[key]
        evidence = {}
        ids = set.union(*(group['ids'] for group in by_session[key]))
        text_by_id = dict(conn.execute('SELECT doc_id,text FROM docs WHERE doc_id IN (SELECT value FROM json_each(?))',
                                       (json.dumps(sorted(ids)),)))
        chosen_texts = set()
        for group_no, group in enumerate(by_session[key]):
            best = group['best']
            chosen_texts.add(best[9])
            duplicates = sum(text_by_id[doc_id] == best[9] for doc_id in group['ids'])
            evidence[groups[group_no]] = _evidence(best, session, [normalize(x) for x in parsed[group_no]], duplicates)
        unique_texts = set(text_by_id.values())
        output.append({'harness': session['harness'], 'native_id': session['native_id'],
                       'ref': key, 'title': session['title'], 'surface': session['surface'],
                       'class': session['class'], 'cwd': session['cwd'],
                       'models': json.loads(session['models_json'] or '[]'),
                       'created': session['created'], 'updated': session['updated'], 'evidence': evidence,
                       'more_hits': max(0, len(unique_texts) - len(chosen_texts))})
    return output, {'alternatives': alternatives, 'matched_sessions': len(selected),
                    'limit': limit, 'slow_path': slow}


def index_status(directory: Path) -> dict:
    """Inspect the last committed sync without opening any source store."""
    path = directory / 'index-v1.sqlite'
    if not path.exists():
        return {'exists': False, 'path': str(path), 'next': 'porch sessions index build'}
    with _checked_reader(directory) as conn:
        conn.execute('BEGIN')
        meta = dict(conn.execute('SELECT key,value FROM meta'))
        harnesses = {}
        for harness, count in conn.execute('SELECT harness,count(*) FROM sessions GROUP BY harness'):
            harnesses[harness] = {'sessions': count, 'docs': {}}
        for harness, kind, count in conn.execute('''SELECT s.harness,d.kind,count(*) FROM docs d
                JOIN sessions s ON s.session_key=d.session_key GROUP BY s.harness,d.kind'''):
            harnesses.setdefault(harness, {'sessions': 0, 'docs': {}})['docs'][kind] = count
        versions = {}
        for harness, version in conn.execute('SELECT DISTINCT harness,extractor_version FROM sources WHERE extractor_version IS NOT NULL'):
            versions.setdefault(harness, []).append(version)
        db_size = path.stat().st_size
        try:
            wal_size = (directory / (path.name + '-wal')).stat().st_size
        except FileNotFoundError:
            wal_size = 0
        sizes = {path.name: db_size, path.name + '-wal': wal_size}
        return {'exists': True, 'path': str(path), 'sizes': sizes,
                'schema_version': meta.get('schema_version'),
                'normalization_version': meta.get('normalization_version'),
                'extractor_versions': versions, 'last_sync_at': meta.get('last_sync_at'),
                'harnesses': harnesses,
                'coverage': json.loads(meta['last_sync_coverage']) if 'last_sync_coverage' in meta else None}


def drop_index(directory: Path) -> None:
    """Remove the index under the writer lock, retaining the persistent lock file."""
    with writer_lock(directory) as acquired:
        if not acquired:
            raise SessionIndexError('another sync in progress')
        paths = [directory / ('index-v1.sqlite' + suffix) for suffix in ('', '-wal', '-shm')]
        paths = [path for path in paths if path.exists()]
        staged = []
        try:
            for path in paths:
                target = path.with_name(path.name + f'.drop-{os.getpid()}-{time.monotonic_ns()}')
                os.replace(path, target)
                staged.append((path, target))
        except OSError as exc:
            for original, target in reversed(staged):
                os.replace(target, original)
            if isinstance(exc, PermissionError) or getattr(exc, 'winerror', None) in (5, 32, 33):
                raise SessionIndexError('index in use; close readers and try again') from exc
            raise
        for _, target in staged:
            target.unlink()


def cmd_index(args):
    directory = index_dir()
    if args.index_command == 'status':
        sessions.emit(index_status(directory))
        return sessions.EXIT_OK
    if args.index_command == 'drop':
        drop_index(directory)
        sessions.emit({'dropped': True, 'path': str(directory / 'index-v1.sqlite')})
        return sessions.EXIT_OK
    def progress(harness, done, total):
        print(f'index build: {harness} {done}/{total}', file=sys.stderr, flush=True)
    for harness in UNIT_BUILDERS:
        print(f'index build: {harness} discovering', file=sys.stderr, flush=True)
    coverage = sync(directory, full=args.full, progress=progress, rebuild_normalization=True)
    for harness in UNIT_BUILDERS:
        item = coverage[harness]
        print(f"index build: {harness} extracted={item['extracted']} error={item['error']} pending={item['pending']}",
              file=sys.stderr, flush=True)
    sessions.emit(coverage)
    return sessions.EXIT_OK if coverage['complete'] else sessions.EXIT_PARTIAL


def cmd_find(args):
    parse_groups(args.groups)  # Reject malformed input before doing a sync.
    if not math.isfinite(args.deadline) or args.deadline < 0:
        raise ValueError('--deadline must be a finite non-negative number')
    if args.limit < 0:
        raise ValueError('--limit must be non-negative')
    since, until = sessions.parse_time_bound(args.since), sessions.parse_time_bound(args.until)
    harnesses = sessions.resolve_harnesses(args.agent) if args.agent else None
    unsupported = set(harnesses or ()) - set(UNIT_BUILDERS)
    if unsupported:
        raise ValueError('harness not supported by the index: ' + ', '.join(sorted(unsupported)))
    directory = index_dir()
    coverage = sync(directory, deadline=time.monotonic() + args.deadline, harnesses=harnesses)
    if not coverage['synced'] and not (directory / 'index-v1.sqlite').exists():
        raise SessionIndexError('no committed session index; run porch sessions index build')
    with _checked_reader(directory) as conn:
        conn.execute('BEGIN')
        snapshot_coverage = _snapshot_coverage(conn, coverage, harnesses)
        rows, summary = find_in_snapshot(conn, args.groups, harnesses=harnesses, kind=args.kind,
                                         origin=args.origin, session_class=args.session_class,
                                         cwd=args.cwd, since=since, until=until, limit=args.limit)
    for row in rows:
        sessions.emit(row)
    sessions.emit({'_summary': True, **summary, 'coverage': snapshot_coverage})
    return sessions.EXIT_OK


def _snapshot_coverage(conn, sync_result, harnesses=None):
    metadata = dict(conn.execute("SELECT key,value FROM meta WHERE key IN ('last_sync_coverage','last_sync_at')"))
    coverage = (json.loads(metadata['last_sync_coverage']) if 'last_sync_coverage' in metadata else
                {'synced': False, 'complete': False, 'reason': 'no recorded sync coverage',
                 'next': 'porch sessions index build', **_coverage(conn, harnesses or ())})
    if not sync_result['synced']:
        coverage.update(synced=False, complete=False, reason=sync_result['reason'],
                        stale_since=metadata.get('last_sync_at'), stat_pass=False,
                        next='porch sessions index build')
    return coverage


def _locator_source_revision(locator, store):
    key = locator['source']
    if key.startswith('agy-meta:'):
        path = Path(locator['anchor']['file'])
        sid = key[len('agy-meta:' + str(path) + '#'):]
        with path.open(encoding='utf-8') as handle:
            data = json.load(handle)
        entry = data.get('conversations', {}).get(sid)
        return _agy_entry_fingerprint(entry) if entry is not None else 'missing'
    if key.startswith('state:'):
        path = locator['anchor']['db']
        sid = key[len('state:' + path + '#'):]
        with contextlib.closing(sessions.open_sqlite_ro(Path(path))) as conn:
            conn.execute('BEGIN')
            rows, edges = _codex_rows(conn)
            return _codex_state_revision(rows[sid], edges.get(sid, [])) if sid in rows else 'missing'
    if key.startswith('session:'):
        path = locator['anchor']['db']
        sid = key[len('session:' + path + '#'):]
        with contextlib.ExitStack() as stack:
            state = DiscoveryState({}, {}, stack)
            selected = sessions.Store(store.harness, store.name, Path(path), 'sqlite')
            return _database_fingerprints(selected, state).get(sid, 'missing')
    path = Path(json.loads(key)[0])
    stat = sessions._source_stat(path)
    return _revision(stat) if stat else 'missing'


def _anchor_matches(fragment, anchor):
    native = fragment.get('native') or {}
    rel = fragment.get('rel') or {}
    field, value = anchor.get('field'), anchor.get('value')
    if field == 'uuid':
        return rel.get('uuid') == value
    if field == 'message_id':
        return native.get('message_id') == value or rel.get('message_id') == value
    if field == 'item_id':
        return rel.get('item_id') == value
    if field == 'locator_key':
        return (fragment.get('locator') or {}).get('key') == value
    return False


def _show_title_record(address, store):
    anchor = address['anchor']
    if address.get('kind') != 'title':
        return None
    if anchor.get('db') and anchor.get('table') in ('threads', 'sessions', 'session_v2', 'session') and anchor.get('key') is not None:
        with contextlib.closing(sessions.open_sqlite_ro(Path(anchor['db']))) as conn:
            conn.row_factory = sqlite3.Row
            table = anchor['table']  # restricted to known table names above
            row = conn.execute('SELECT * FROM ' + table + ' WHERE id=?', (anchor['key'],)).fetchone()
            if row is None:
                return []
            data = dict(row)
            text = next((data.get(k) for k in ('title','name','preview','first_user_message') if data.get(k)), None)
            if isinstance(text, str):
                text = text[:160]
            else:
                return []
            return [sessions.frag(store.harness, store.name, address['session'].split(':',1)[1], 1,
                                  'metadata', 'indexed.title', anchor, text=text, authorship='system', visible=True)]
    if anchor.get('file') and not anchor.get('line') and Path(anchor['file']).name.startswith('local_'):
        path = Path(anchor['file'])
        with path.open(encoding='utf-8') as handle:
            data = json.load(handle)
        desktop_store = sessions.Store('claude-desktop', 'code-sessions', path.parent, 'json_dir')
        ref = sessions._claude_desktop_manifest(desktop_store, path, data)
        if not ref.title:
            return []
        return [sessions.frag('claude-desktop', desktop_store.name, ref.session_id, 1, 'metadata',
                              'manifest.title', anchor, text=ref.title, authorship='system', visible=True)]
    if anchor.get('file') and not anchor.get('line') and store.harness == 'opencode':
        path = Path(anchor['file'])
        storage = store.path if (store.path / 'session').is_dir() else store.path / 'storage'
        if path.is_relative_to(storage / 'session'):
            with path.open(encoding='utf-8') as handle:
                text = json.load(handle).get('title')
            if not text:
                return []
            return [sessions.frag('opencode', store.name, address['session'].split(':',1)[1], 1,
                                  'metadata', 'session.title', anchor, text=text, authorship='system', visible=True)]
    if anchor.get('file') and not anchor.get('line') and store.harness in ('porch', 'agy'):
        path = Path(anchor['file'])
        sid = address['session'].split(':', 1)[1]
        if store.harness == 'porch' and path.name == 'meta.json':
            with path.open(encoding='utf-8') as handle:
                text = (json.load(handle).get('task') or '')[:120]
        elif store.harness == 'agy' and path.suffix == '.pbtxt':
            text = sessions._agy_annotation_title(store, sid)
        elif store.harness == 'agy' and path.name == 'conversation_metadata.json':
            text = (sessions._agy_metadata(store).get(sid, {}).get('summary') or {}).get('Title')
        else:
            return None
        if not text:
            return []
        return [sessions.frag(store.harness, store.name, sid, 1, 'metadata', 'indexed.title', anchor,
                              text=text, authorship='system', visible=True)]
    return None


def cmd_show_at(args):
    """Read the addressed native record and reject stale positional locators."""
    try:
        address = json.loads(args.at)
        key = address['session']
        harness, sid = key.split(':', 1)
        anchor = address['anchor']
        source = address['source']
        revision = address['revision']
        if (address['v'] != 1 or not isinstance(source, str) or not source or
                not isinstance(revision, str) or not revision or not isinstance(anchor, dict)):
            raise ValueError('incomplete locator')
        if source.startswith('agy-meta:'):
            prefix = 'agy-meta:' + str(anchor.get('file')) + '#'
            if not anchor.get('file') or not source.startswith(prefix):
                raise ValueError('source and anchor disagree')
        elif source.startswith(('state:', 'session:')):
            prefix = source.split(':', 1)[0] + ':' + str(anchor.get('db')) + '#'
            if not anchor.get('db') or not source.startswith(prefix):
                raise ValueError('source and anchor disagree')
        elif anchor.get('file') != json.loads(source)[0]:
            raise ValueError('source and anchor disagree')
    except (ValueError, KeyError, TypeError, IndexError, AttributeError) as exc:
        raise SessionIndexError('invalid locator; search again with porch sessions find') from exc
    if harness not in UNIT_BUILDERS:
        raise SessionIndexError('unsupported locator harness; search again with porch sessions find')
    store = next((s for s in sessions.stores_for(harness) if s.name == address['store']), None)
    if store is None:
        raise SessionIndexError('source store unavailable; search again with porch sessions find')
    try:
        before = _locator_source_revision(address, store)
    except (OSError, ValueError, TypeError, KeyError, AttributeError, sqlite3.Error, SessionIndexError) as exc:
        raise SessionIndexError('source unavailable; search again with porch sessions find') from exc
    if before == 'missing':
        raise SessionIndexError('source unavailable; search again with porch sessions find')
    changed = before != revision
    native_anchor = anchor.get('message_anchor')
    if changed and (not isinstance(native_anchor, dict) or
                    native_anchor.get('field') not in ('uuid', 'message_id', 'item_id', 'locator_key') or
                    native_anchor.get('value') is None):
        raise SessionIndexError('source changed and has no stable message id; search again with porch sessions find')
    try:
        title_record = _show_title_record(address, store) if not changed else None
    except (OSError, ValueError, sqlite3.Error) as exc:
        raise SessionIndexError('source could not be read; search again with porch sessions find') from exc
    if title_record is not None:
        ref = sessions.SessionRef(harness, store.name, sid, locator=anchor)
    elif harness in ('claude-code', 'codex') and anchor.get('file'):
        ref = sessions.SessionRef(harness, store.name, sid, locator={'file': anchor['file']})
    else:
        store, ref = sessions._resolve_session([harness], key, address['store'])
    stats, found = {}, []
    fragments = title_record if title_record is not None else sessions.iter_all_fragments(store, ref, 0, stats)
    for fragment in fragments:
        if changed:
            loc = fragment.get('locator') or {}
            match = (_anchor_matches(fragment, native_anchor) and
                     all(loc.get(k) == anchor.get(k) for k in ('file', 'db', 'table') if k in anchor))
        else:
            loc = fragment.get('locator') or {}
            match = all(loc.get(k) == v for k, v in anchor.items() if k != 'message_anchor')
            if match and address.get('kind') == 'title' and not anchor.get('line') and title_record is None:
                if native_anchor:
                    match = _anchor_matches(fragment, native_anchor)
                else:
                    value = fragment.get('text')
                    match = isinstance(value, str) and hashlib.sha256(value.encode('utf-8')).hexdigest() == address.get('text_digest')
        if match:
            if args.max_chars and isinstance(fragment.get('text'), str) and len(fragment['text']) > args.max_chars:
                fragment['text'] = fragment['text'][:args.max_chars]
                fragment['truncated'] = True
            found.append(fragment)
    if stats.get('changed_during_read') or stats.get('unreadable') or stats.get('malformed') or stats.get('truncated_tail') or stats.get('compressed_errors') or store.status == 'unavailable':
        raise SessionIndexError('source could not be read completely; search again with porch sessions find')
    if _locator_source_revision(address, store) != before:
        raise SessionIndexError('source changed during read; search again with porch sessions find')
    if not found:
        raise SessionIndexError('record no longer found; search again with porch sessions find')
    sessions.emit({'_session': ref.as_dict()})
    for fragment in found:
        sessions.emit(fragment)
    sessions.emit({'_summary': True, 'fragments_emitted': len(found), 'source_changed': changed})
    return sessions.EXIT_OK
