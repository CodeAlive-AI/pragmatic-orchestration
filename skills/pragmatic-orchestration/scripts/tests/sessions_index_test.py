"""Fixture-only tests of session-index storage primitives."""
from __future__ import annotations

import os
import contextlib
import hashlib
import io
import json
import random
import argparse
import sqlite3
import subprocess
import stat
import sys
import tempfile
import time
import tracemalloc
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'lib'))
import sessions
import sessions_index as index


class CapabilityTests(unittest.TestCase):
    def test_normalized_match_fast_path_matches_full_unicode_normalization(self):
        rng = random.Random(771)
        alphabet = ['a','b','e','i','I','K','Ё','ё','Σ','ς','İ','K','\u0301','\u0345','\u1100','\u1161','😀',' ','|','"']
        terms = ['a','e','i','ab','b ', 'ё','k','σ','a|b','"', '']
        for _ in range(5000):
            text = ''.join(rng.choices(alphabet,k=rng.randrange(0,35)))
            raw = rng.choice(terms + ([text[rng.randrange(len(text)):] if text else '']))
            term = index.normalize(raw)
            self.assertEqual(index._matches_normalized(text,term),term in index.normalize(text),(text,raw))

    def test_devin_index_discards_tool_text_without_changing_turns(self):
        store = sessions.Store('devin','sessions.db',Path('/fixture'), 'sqlite')
        ref = sessions.SessionRef('devin','sessions.db','s')
        fragments = [
            {'kind':'prompt','text':'question','origin':'human','native':{'canonical':True,'message_id':'p'}},
            {'kind':'tool_result','text':'large tool output','native':{'canonical':True}},
            {'kind':'reasoning','text':'large hidden reasoning','native':{'canonical':True}},
            {'kind':'assistant','text':'answer','native':{'canonical':True,'finish_reason':'stop','has_tool_calls':False}},
        ]
        expected = sessions.interpret_session(store, ref, fragments)
        compact = list(index._devin_index_fragments([dict(f) for f in fragments]))
        self.assertNotIn('text', compact[1])
        self.assertNotIn('text', compact[2])
        self.assertEqual(sessions.interpret_session(store, ref, compact), expected)

    def test_codex_index_fragment_compaction_preserves_turns(self):
        store = sessions.Store('codex', 'fixture', Path('/fixture'), 'jsonl_dir')
        ref = sessions.SessionRef('codex', 'fixture', 'id')
        fragments = [
            {'kind':'boundary','native_type':'event_msg.task_started','ts':'1'},
            {'kind':'prompt','native_type':'event_msg.user_message','text':'hello','ts':'2'},
            {'kind':'compaction','native_type':'compacted','text':'very large history','ts':'3'},
            {'kind':'tool_result','native_type':'response_item.function_call_output','text':'tool output','ts':'4'},
            {'kind':'assistant','native_type':'event_msg.agent_message','text':'answer','ts':'5',
             'native':{'phase':'final_answer','layer':'event'}},
            {'kind':'boundary','native_type':'event_msg.task_complete','ts':'6'},
        ]
        expected = sessions.interpret_session(store, ref, fragments)
        compact = list(index._codex_index_fragments([dict(f) for f in fragments]))
        self.assertNotIn('text', compact[2])
        self.assertNotIn('text', compact[3])
        self.assertEqual(sessions.interpret_session(store, ref, compact), expected)
        record = {'type':'compacted','timestamp':'3','payload':{'message':'discarded history'}}
        self.assertEqual(list(index._codex_index_records([(1, record)])),
                         [(1, {'type':'compacted','timestamp':'3','payload':{}})])

    def test_sync_enforces_raw_scan_memory_guard(self):
        with tempfile.TemporaryDirectory() as tmp:
            script = "import sys; sys.path.insert(0, sys.argv[1]); import sessions_index; sessions_index.sync(__import__('pathlib').Path(sys.argv[2]))"
            env = dict(os.environ, PORCH_SESSIONS_MEMORY_LIMIT_MB='1')
            result = subprocess.run([sys.executable, '-c', script, str(Path(index.__file__).parent), tmp],
                                    env=env, capture_output=True, text=True)
            self.assertEqual(result.returncode, sessions.EXIT_MEMORY_LIMIT, result.stderr)
            self.assertIn('memory budget exceeded', result.stderr)

    def test_unavailable_explains_sqlite_version(self):
        with patch.object(index, 'sqlite_capability', return_value=(False, 'SQLite 3.40.1 lacks FTS5')):
            with self.assertRaisesRegex(index.SessionIndexError, r'SQLite >= 3\.43.*SQLite 3\.40\.1'):
                index.require_sqlite()

    def test_normalize(self):
        self.assertEqual(index.normalize('Е\u0308Ж ABC'), 'еж abc')

    def test_stat_permission_failure_is_reported(self):
        source_path = Path('/fixture/session.jsonl')
        with patch.object(Path, 'stat', side_effect=PermissionError('denied')):
            source = sessions._file_source(source_path, 'content', 'session')
        self.assertIsNone(source.stat)
        self.assertIn('denied', source.error)

    def test_enumeration_preserves_stat_errors(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / 'session.jsonl'
            path.write_text('{}')
            store = sessions.Store('claude-code', 'projects', root, 'jsonl_dir')
            original = Path.stat
            def guarded(candidate, *args, **kwargs):
                if candidate == path:
                    raise PermissionError('denied')
                return original(candidate, *args, **kwargs)
            with patch.object(Path, 'stat', guarded):
                sources = list(sessions.SOURCES['claude-code'](store))
            self.assertEqual(len(sources), 1)
            self.assertIn('denied', sources[0].error)
            self.assertEqual(store.walk_stats['unreadable_dirs'], 1)


@unittest.skipUnless(index.sqlite_capability()[0], index.sqlite_capability()[1])
class IndexStorageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.directory = Path(self.tmp.name) / 'index'

    def test_missing_store_is_offline_then_complete_walk_deletes(self):
        root = Path(self.tmp.name) / 'projects'; root.mkdir()
        path = root / 'one.jsonl'
        path.write_text('{"type":"user","message":{"role":"user","content":"hello"}}\n')
        store = sessions.Store('claude-code', 'projects', root, 'jsonl_dir')
        with patch.object(sessions, 'stores_for', side_effect=lambda h: [store] if h == 'claude-code' else []):
            index.sync(self.directory, harnesses=['claude-code'])
        path.unlink()
        with patch.object(sessions, 'stores_for', return_value=[]):
            result = index.sync(self.directory, harnesses=['claude-code'])
        self.assertFalse(result['complete'])
        self.assertEqual(result['claude-code']['deleted'], 0)
        with contextlib.closing(index.open_reader(self.directory)) as conn:
            self.assertEqual(conn.execute('SELECT count(*) FROM sessions').fetchone()[0], 1)
            self.assertEqual(conn.execute("SELECT count(*) FROM sources WHERE state='offline'").fetchone()[0], 1)
        with patch.object(sessions, 'stores_for', side_effect=lambda h: [store] if h == 'claude-code' else []):
            result = index.sync(self.directory, harnesses=['claude-code'])
        self.assertEqual(result['claude-code']['deleted'], 1)

    def test_file_snapshot_rejects_sqlite(self):
        path = Path(self.tmp.name) / 'source.db'
        with contextlib.closing(sqlite3.connect(path)) as conn, conn:
            conn.execute('CREATE TABLE example (id INTEGER)')
        with index.FileSnapshot() as snapshot:
            with self.assertRaisesRegex(index.SessionIndexError, 'plain non-SQLite'):
                snapshot.add(path)

    def test_find_uses_coverage_of_searched_snapshot(self):
        root = Path(self.tmp.name) / 'projects'; root.mkdir()
        (root / 'one.jsonl').write_text('{"type":"user","message":{"role":"user","content":"hello"}}\n')
        store = sessions.Store('claude-code', 'projects', root, 'jsonl_dir')
        with patch.object(sessions, 'stores_for', side_effect=lambda h: [store] if h == 'claude-code' else []):
            old = index.sync(self.directory, harnesses=['claude-code'])
        with contextlib.closing(index.open_reader(self.directory)) as reader:
            statements = []
            reader.set_trace_callback(statements.append)
            index.find_in_snapshot(reader, ['hello'])
        self.assertTrue(any('FROM sessions WHERE session_key IN (SELECT value FROM json_each(' in sql for sql in statements))
        def sync_then_commit(*args, **kwargs):
            with index.writer(self.directory) as conn:
                newer = dict(old, complete=False, reason='new snapshot')
                conn.execute("UPDATE meta SET value=? WHERE key='last_sync_coverage'", (json.dumps(newer),))
                conn.commit()
            return old
        args = argparse.Namespace(groups=['hello'], deadline=1, limit=20, since=None, until=None,
                                  agent=None, kind=None, origin=None, session_class=None, cwd=None)
        output = io.StringIO()
        with patch.object(index, 'index_dir', return_value=self.directory), \
             patch.object(index, 'sync', side_effect=sync_then_commit), contextlib.redirect_stdout(output):
            index.cmd_find(args)
        coverage = json.loads(output.getvalue().splitlines()[-1])['coverage']
        self.assertFalse(coverage['complete'])
        self.assertEqual(coverage['reason'], 'new snapshot')

    def test_index_commands_status_build_full_and_drop(self):
        root = Path(self.tmp.name) / 'projects'; root.mkdir()
        (root / 'one.jsonl').write_text('{"type":"user","message":{"role":"user","content":"hello"}}\n')
        store = sessions.Store('claude-code', 'projects', root, 'jsonl_dir')
        def invoke(*argv):
            out, err = io.StringIO(), io.StringIO()
            with patch.object(sys, 'argv', ['sessions', 'index', *argv]), \
                 contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                result = sessions.main()
            return result, json.loads(out.getvalue()), err.getvalue()
        with patch.object(index, 'index_dir', return_value=self.directory), \
             patch.object(sessions, 'stores_for', side_effect=lambda h: [store] if h == 'claude-code' else []), \
             patch.object(sessions, '_acquire_scan_slot', return_value=None) as acquire:
            result, status, _ = invoke('status')
            self.assertEqual(result, 0)
            self.assertFalse(status['exists'])
            self.assertEqual(status['next'], 'porch sessions index build')
            self.assertFalse(self.directory.exists())
            acquire.assert_not_called()
            result, coverage, progress = invoke('build')
            self.assertEqual(result, 0)
            self.assertEqual(coverage['claude-code']['extracted'], 1)
            self.assertIn('claude-code 1/1', progress)
            acquire.assert_called_once()
            with patch.object(index, 'sync', side_effect=AssertionError('status must not sync')):
                result, status, _ = invoke('status')
            self.assertEqual(status['coverage'], coverage)
            self.assertIsNotNone(status['last_sync_at'])
            self.assertEqual(status['schema_version'], index.SCHEMA_VERSION)
            self.assertEqual(status['normalization_version'], index.NORMALIZATION_VERSION)
            self.assertIn(index.EXTRACTOR_VERSIONS['claude-code'], status['extractor_versions']['claude-code'])
            self.assertEqual(status['harnesses']['claude-code']['sessions'], 1)
            self.assertEqual(status['harnesses']['claude-code']['docs']['prompt'], 1)
            self.assertGreater(status['sizes']['index-v1.sqlite'], 0)
            self.assertEqual(invoke('build')[1]['claude-code']['extracted'], 0)
            with index.writer(self.directory) as conn:
                conn.execute("UPDATE meta SET value='old' WHERE key='normalization_version'")
                conn.commit()
            self.assertEqual(invoke('build')[0], 0)
            self.assertEqual(invoke('status')[1]['normalization_version'], index.NORMALIZATION_VERSION)
            self.assertEqual(invoke('build', '--full')[1]['claude-code']['extracted'], 1)
            before = acquire.call_count
            self.assertTrue(invoke('drop')[1]['dropped'])
            self.assertEqual(acquire.call_count, before)
            self.assertTrue((self.directory / 'index.lock').exists())
            self.assertFalse((self.directory / 'index-v1.sqlite').exists())
            self.assertFalse(invoke('status')[1]['exists'])

    @unittest.skipIf(os.name == 'nt', 'Windows refuses drop with an open reader')
    def test_drop_open_reader_and_lost_snapshot_diagnostic(self):
        root = Path(self.tmp.name) / 'projects'; root.mkdir()
        (root / 'one.jsonl').write_text('{"type":"user","message":{"role":"user","content":"hello"}}\n')
        store = sessions.Store('claude-code', 'projects', root, 'jsonl_dir')
        with patch.object(sessions, 'stores_for', side_effect=lambda h: [store] if h == 'claude-code' else []):
            index.sync(self.directory, harnesses=['claude-code'])
        args = argparse.Namespace(groups=['hello'], deadline=1, limit=20, since=None, until=None,
                                  agent=None, kind=None, origin=None, session_class=None, cwd=None)
        def lost(*args, **kwargs):
            index.drop_index(self.directory)
            raise sqlite3.OperationalError('reader lost file')
        with patch.object(index, 'index_dir', return_value=self.directory), \
             patch.object(index, 'sync', return_value={'synced': True}), \
             patch.object(index, 'find_in_snapshot', side_effect=lost):
            with self.assertRaisesRegex(index.SessionIndexError, 'index dropped, run find again'):
                index.cmd_find(args)
        with patch.object(sessions, 'stores_for', side_effect=lambda h: [store] if h == 'claude-code' else []):
            index.sync(self.directory, harnesses=['claude-code'])
        def lost_without_sql_error(*args, **kwargs):
            index.drop_index(self.directory)
            return [], {}
        with patch.object(index, 'index_dir', return_value=self.directory), \
             patch.object(index, 'sync', return_value={'synced': True}), \
             patch.object(index, 'find_in_snapshot', side_effect=lost_without_sql_error), \
             contextlib.redirect_stdout(io.StringIO()) as out:
            with self.assertRaisesRegex(index.SessionIndexError, 'index dropped, run find again'):
                index.cmd_find(args)
            self.assertEqual(out.getvalue(), '')
        self.assertTrue((self.directory / 'index.lock').exists())
        for suffix in ('', '-wal', '-shm'):
            self.assertFalse((self.directory / ('index-v1.sqlite' + suffix)).exists())

    def test_drop_rename_failure_restores_all_files(self):
        self.directory.mkdir()
        paths = [self.directory / ('index-v1.sqlite' + suffix) for suffix in ('', '-wal', '-shm')]
        for n, path in enumerate(paths):
            path.write_bytes(bytes([n]))
        original = os.replace
        def rename(source, target):
            if Path(source) == paths[1]:
                raise PermissionError('in use')
            return original(source, target)
        with patch.object(index.os, 'replace', side_effect=rename):
            with self.assertRaisesRegex(index.SessionIndexError, 'index in use'):
                index.drop_index(self.directory)
        self.assertEqual([path.read_bytes() for path in paths], [b'\0', b'\1', b'\2'])
        self.assertEqual(sorted(p.name for p in self.directory.iterdir()),
                         ['index-v1.sqlite', 'index-v1.sqlite-shm', 'index-v1.sqlite-wal', 'index.lock'])

    @unittest.skipUnless(os.name == 'nt', 'Windows file sharing behavior')
    def test_windows_drop_with_open_reader_changes_nothing(self):
        with index.writer(self.directory):
            pass
        with contextlib.closing(index.open_reader(self.directory)) as reader:
            reader.execute('BEGIN')
            reader.execute('SELECT count(*) FROM meta').fetchone()
            paths = [self.directory / ('index-v1.sqlite' + suffix) for suffix in ('', '-wal', '-shm')]
            before = {p.name: p.read_bytes() for p in paths if p.exists()}
            with self.assertRaisesRegex(index.SessionIndexError, 'index in use'):
                index.drop_index(self.directory)
            self.assertEqual({p.name: p.read_bytes() for p in paths if p.exists()}, before)

    def test_devin_streams_large_nonindexed_message_bodies(self):
        db = Path(self.tmp.name)/'devin.db'
        blob = json.dumps({'role':'tool','content':'😀' + 'x' * (256 * 1024)}, ensure_ascii=False)
        with contextlib.closing(sqlite3.connect(db)) as conn, conn:
            conn.execute('CREATE TABLE message_nodes(row_id INTEGER,session_id TEXT,node_id INTEGER,parent_node_id INTEGER,chat_message TEXT,created_at TEXT,metadata TEXT)')
            conn.executemany('INSERT INTO message_nodes VALUES(?,?,?,?,?,?,?)',
                             [(n,'s',n,None,blob,None,None) for n in range(32)])
        store = sessions.Store('devin','sessions.db',db,'sqlite')
        ref = sessions.SessionRef('devin','sessions.db','s')
        with contextlib.closing(sessions.open_sqlite_ro(db)) as conn:
            conn.execute('BEGIN')
            tracemalloc.start()
            try:
                fragments = list(index._devin_index_fragments(sessions.devin_iter_fragments(store,ref,0,{},conn)))
                _, peak = tracemalloc.get_traced_memory()
            finally:
                tracemalloc.stop()
        self.assertEqual(len(fragments),32)
        self.assertTrue(all(f['kind']=='tool_result' and 'text' not in f for f in fragments))
        self.assertLess(peak, 12 * 1024 * 1024)

    def test_claude_desktop_unit_lifecycle(self):
        root = Path(self.tmp.name)
        cc = root / 'projects'; cc.mkdir()
        desk = root / 'desktop'; desk.mkdir()
        transcript = cc / 'one.jsonl'
        manifest = desk / 'local_a.json'
        def prompt(text):
            return json.dumps({'type':'user','message':{'role':'user','content':text}}) + '\n'
        def answer(text):
            return json.dumps({'type':'assistant','message':{'id':text,'role':'assistant','content':[{'type':'text','text':text}],'stop_reason':'end_turn'}}) + '\n'
        def write_manifest(title):
            manifest.write_text(json.dumps({'sessionId':'desktop-a','cliSessionId':'one','title':title,'titleSource':'user'}))
        transcript.write_text(prompt('hello') + answer('first answer'))
        write_manifest('First title')
        stores = {'claude-code':[sessions.Store('claude-code','projects',cc,'jsonl_dir')],
                  'claude-desktop':[sessions.Store('claude-desktop','code-sessions',desk,'json_dir')]}
        with patch.object(sessions, 'stores_for', side_effect=lambda h: stores.get(h, [])):
            result = index.sync(self.directory)
            self.assertEqual(result['claude-code']['extracted'], 1)
            with contextlib.closing(index.open_reader(self.directory)) as conn:
                self.assertEqual(conn.execute('SELECT session_key,title FROM sessions').fetchall(), [('claude-code:one','First title')])
                self.assertEqual(conn.execute('SELECT alias FROM aliases WHERE kind=?',('desktop',)).fetchall(), [('claude-desktop:desktop-a',)])
            with patch.object(index.json, 'load', side_effect=AssertionError('unchanged manifest was read')):
                self.assertEqual(index.sync(self.directory)['claude-code']['extracted'], 0)
            with transcript.open('a') as out:
                out.write(prompt('more') + answer('second answer'))
            self.assertEqual(index.sync(self.directory)['claude-code']['extracted'], 1)
            with contextlib.closing(index.open_reader(self.directory)) as conn:
                self.assertIn(('second answer',), conn.execute("SELECT text FROM docs WHERE kind='answer'").fetchall())
            write_manifest('Changed title')
            self.assertEqual(index.sync(self.directory)['claude-code']['extracted'], 1)
            with contextlib.closing(index.open_reader(self.directory)) as conn:
                self.assertEqual(conn.execute('SELECT title FROM sessions').fetchone(), ('Changed title',))
            transcript.unlink()
            self.assertEqual(index.sync(self.directory)['claude-code']['extracted'], 1)
            with contextlib.closing(index.open_reader(self.directory)) as conn:
                self.assertEqual(conn.execute('SELECT content_state FROM sessions').fetchone(), ('unavailable',))
                self.assertEqual(conn.execute('SELECT count(*) FROM docs').fetchone(), (0,))
            manifest.unlink()
            self.assertEqual(index.sync(self.directory)['claude-code']['deleted'], 1)

    def test_sync_pending_snapshot_and_lock(self):
        root = Path(self.tmp.name) / 'projects'; root.mkdir()
        path = root / 'one.jsonl'
        path.write_text('{"type":"user","message":{"role":"user","content":"old"}}\n')
        store = sessions.Store('claude-code','projects',root,'jsonl_dir')
        with patch.object(sessions, 'stores_for', side_effect=lambda h: [store] if h == 'claude-code' else []):
            index.sync(self.directory)
            path.write_text('{"type":"user","message":{"role":"user","content":"new"}}\n')
            timed = index.sync(self.directory, deadline=time.monotonic()-1)
            self.assertGreaterEqual(timed['claude-code']['pending'], 1)
            self.assertEqual(timed['next'], 'porch sessions index build')
            status = index.index_status(self.directory)
            self.assertEqual(status['coverage'], timed)
            self.assertIn('sync deadline reached', status['coverage']['claude-code']['reasons']['pending'])
            original = sessions.iter_all_fragments
            def changing(*args, **kwargs):
                yield from original(*args, **kwargs)
                with path.open('a') as out:
                    out.write('{}\n')
            with patch.object(sessions, 'iter_all_fragments', changing):
                raced = index.sync(self.directory)
                self.assertEqual(raced['claude-code']['pending'], 1)
                self.assertGreaterEqual(raced['claude-code']['reason_classes']['pending']['changed_during_read'], 1)
            with contextlib.closing(index.open_reader(self.directory)) as conn:
                self.assertEqual(conn.execute("SELECT text FROM docs WHERE kind='prompt'").fetchall(), [('old',)])
                self.assertIn('changed', conn.execute("SELECT error FROM sources WHERE state='pending'").fetchone()[0])
            with index.writer(self.directory):
                busy = index.sync(self.directory, deadline=time.monotonic()+0.05)
                self.assertFalse(busy['synced'])
                self.assertEqual(busy['reason'], 'another sync in progress')
            self.assertTrue(index.sync(self.directory)['complete'])

    def test_extraction_errors_are_memoized_until_revision_version_or_full(self):
        root = Path(self.tmp.name) / 'projects'; root.mkdir()
        path = root / 'one.jsonl'
        path.write_text('{"type":"user","message":{"role":"user","content":"old"}}\n')
        store = sessions.Store('claude-code','projects',root,'jsonl_dir')
        with patch.object(sessions, 'stores_for', side_effect=lambda h: [store] if h == 'claude-code' else []):
            index.sync(self.directory)
            with path.open('a') as out: out.write('{}\n')
            with patch.object(index, 'extract_unit', side_effect=ValueError('parse error: fixture')) as extract:
                first = index.sync(self.directory)
                self.assertEqual(extract.call_count, 1)
                self.assertEqual(first['claude-code']['reason_classes']['error']['parse_error'], 1)
                second = index.sync(self.directory)
                self.assertEqual(extract.call_count, 1)
                self.assertFalse(second['complete'])
                self.assertEqual(second['claude-code']['reason_classes']['error']['parse_error'], 1)
                saved = index.index_status(self.directory)['coverage']['claude-code']
                self.assertEqual(saved['unit_error_details']['parse error: fixture'], 1)
                self.assertEqual(saved['reasons']['error']['parse error: fixture'], 1)
                with contextlib.closing(index.open_reader(self.directory)) as conn:
                    self.assertEqual(conn.execute("SELECT text FROM docs WHERE kind='prompt'").fetchall(), [('old',)])
                    self.assertEqual(conn.execute('SELECT reason FROM unit_errors').fetchone(), ('parse error: fixture',))
                    self.assertEqual(conn.execute("SELECT error FROM sources WHERE state='error'").fetchone(), ('parse error: fixture',))
                with path.open('a') as out: out.write('{}\n')
                index.sync(self.directory)
                self.assertEqual(extract.call_count, 2)
                with patch.dict(index.EXTRACTOR_VERSIONS, {'claude-code':'next'}):
                    index.sync(self.directory)
                    self.assertEqual(extract.call_count, 3)
                index.sync(self.directory, full=True)
                self.assertEqual(extract.call_count, 4)
            with path.open('a') as out: out.write('{}\n')
            self.assertTrue(index.sync(self.directory)['complete'])
            with contextlib.closing(index.open_reader(self.directory)) as conn:
                self.assertEqual(conn.execute('SELECT count(*) FROM unit_errors').fetchone(), (0,))

    def test_codex_suffixed_uuid_rollout_is_same_thread(self):
        root = Path(self.tmp.name) / 'codex'; active = root / 'sessions'; active.mkdir(parents=True)
        sid = 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa'
        suffix = 'bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb'
        path = active / f'rollout-2026-01-01T00-00-00-{sid}_{suffix}.jsonl'
        path.write_text(json.dumps({'type':'session_meta','payload':{'id':sid, 'forked_from_id':'parent'}})+'\n' +
                        json.dumps({'type':'event_msg','payload':{'type':'user_message','message':'hello'}})+'\n')
        db = root / 'state_1.sqlite'
        with contextlib.closing(sqlite3.connect(db)) as conn, conn:
            conn.executescript('CREATE TABLE threads(id TEXT PRIMARY KEY, title TEXT, rollout_path TEXT); CREATE TABLE thread_spawn_edges(parent_thread_id TEXT,child_thread_id TEXT);')
            conn.execute('INSERT INTO threads VALUES(?,?,?)', (sid, 'title', str(path)))
        stores = [sessions.Store('codex','sessions',active,'jsonl_dir'), sessions.Store('codex','state:state_1.sqlite',db,'sqlite')]
        with patch.object(sessions, 'stores_for', return_value=stores):
            result = index.sync(self.directory, harnesses=['codex'])
            self.assertEqual(result['codex']['extracted'], 1)
            self.assertEqual(result['codex']['error'], 0)
            with contextlib.closing(index.open_reader(self.directory)) as conn:
                self.assertEqual(conn.execute('SELECT session_key FROM sessions').fetchall(), [('codex:'+sid,)])
                self.assertEqual(conn.execute("SELECT target_key FROM edges WHERE type='fork'").fetchone(), ('codex:parent',))
            self.assertEqual(index.sync(self.directory,harnesses=['codex'])['codex']['extracted'], 0)
            try:
                from compression import zstd
            except ImportError:
                return
            packed = Path(str(path) + '.zst')
            packed.write_bytes(zstd.compress(path.read_bytes()))
            path.unlink()
            self.assertEqual(index.sync(self.directory,harnesses=['codex'])['codex']['extracted'], 1)
            with contextlib.closing(index.open_reader(self.directory)) as conn:
                self.assertEqual(conn.execute('SELECT session_key FROM sessions').fetchall(), [('codex:'+sid,)])

    def test_failed_extraction_tracks_new_source_inside_store(self):
        root = Path(self.tmp.name) / 'projects'; root.mkdir()
        (root / 'one.jsonl').write_text('{}\n')
        extra = root / 'selected.data'; extra.write_text('first')
        store = sessions.Store('claude-code','projects',root,'jsonl_dir')
        key = index.source_key(str(extra))
        def fail(unit):
            unit.sources[key] = index._revision(sessions._source_stat(extra))
            unit.roles[key] = 'content'
            unit.stores[key] = store
            raise ValueError('parse error in extra source')
        with patch.object(sessions,'stores_for',side_effect=lambda h:[store] if h == 'claude-code' else []):
            with patch.object(index,'extract_unit',side_effect=fail) as extract:
                index.sync(self.directory)
                index.sync(self.directory)
                self.assertEqual(extract.call_count, 1)
                extra.write_text('changed source')
                index.sync(self.directory)
                self.assertEqual(extract.call_count, 2)
                (root / 'one.jsonl').unlink()
                self.assertTrue(index.sync(self.directory)['complete'])
                with contextlib.closing(index.open_reader(self.directory)) as conn:
                    self.assertEqual(conn.execute('SELECT count(*) FROM unit_errors').fetchone(), (0,))

    def test_existing_schema_gets_additive_error_memo_table(self):
        with index.writer(self.directory) as conn:
            conn.execute('DROP TABLE unit_errors')
            conn.commit()
        with patch.object(sessions,'stores_for',return_value=[]):
            self.assertTrue(index.sync(self.directory,harnesses=['codex'])['complete'])
        with contextlib.closing(index.open_reader(self.directory)) as conn:
            self.assertTrue(sessions.table_exists(conn,'unit_errors'))

    def test_subagent_transcript_is_a_unit(self):
        root = Path(self.tmp.name) / 'projects'; root.mkdir()
        sub = root / 'project' / 'parent' / 'subagents'; sub.mkdir(parents=True)
        (sub / 'agent-child.jsonl').write_text('{"type":"user","message":{"role":"user","content":"child"}}\n')
        store = sessions.Store('claude-code','projects',root,'jsonl_dir')
        with patch.object(sessions, 'stores_for', side_effect=lambda h: [store] if h == 'claude-code' else []):
            self.assertEqual(index.sync(self.directory)['claude-code']['extracted'], 1)
        with contextlib.closing(index.open_reader(self.directory)) as conn:
            self.assertEqual(conn.execute('SELECT session_key,class FROM sessions').fetchone(), ('claude-code:agent-child','subagent'))

    def test_incomplete_walk_preserves_committed_session(self):
        root = Path(self.tmp.name) / 'projects'; root.mkdir()
        target = Path(self.tmp.name) / 'external'; target.mkdir()
        path = target / 'one.jsonl'
        path.write_text('{"type":"user","message":{"role":"user","content":"old"}}\n')
        link = root / 'linked'
        try:
            link.symlink_to(target, target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest('symlinks unavailable')
        store = sessions.Store('claude-code','projects',root,'jsonl_dir')
        with patch.object(sessions, 'stores_for', side_effect=lambda h: [store] if h == 'claude-code' else []):
            self.assertEqual(index.sync(self.directory)['claude-code']['extracted'], 1)
            moved = Path(self.tmp.name) / 'held'
            target.rename(moved)
            result = index.sync(self.directory)
            self.assertFalse(result['complete'])
            self.assertEqual(result['claude-code']['deleted'], 0)
            self.assertEqual(result['claude-code']['offline'], 1)
            self.assertEqual(result['next'], 'porch sessions index build')
            with contextlib.closing(index.open_reader(self.directory)) as conn:
                self.assertEqual(conn.execute('SELECT count(*) FROM sessions').fetchone(), (1,))
            moved.rename(target)
            result = index.sync(self.directory)
            self.assertEqual(result['claude-code']['extracted'], 1)
            self.assertEqual(result['claude-code']['offline'], 0)

    def test_broken_file_symlink_is_offline_and_recovers(self):
        root = Path(self.tmp.name) / 'projects'; nested = root / 'nested'; nested.mkdir(parents=True)
        target = Path(self.tmp.name) / 'target.jsonl'
        target.write_text('{"type":"user","message":{"content":"old"}}\n')
        link = nested / 'one.jsonl'
        try:
            link.symlink_to(target)
        except (OSError, NotImplementedError):
            self.skipTest('symlinks unavailable')
        store = sessions.Store('claude-code','projects',root,'jsonl_dir')
        with patch.object(sessions,'stores_for',side_effect=lambda h:[store] if h == 'claude-code' else []):
            index.sync(self.directory,harnesses=['claude-code'])
            held = Path(self.tmp.name) / 'held.jsonl'; target.rename(held)
            result = index.sync(self.directory,harnesses=['claude-code'])
            self.assertEqual(result['claude-code']['offline'],1)
            self.assertEqual(result['claude-code']['error'],0)
            self.assertEqual(result['claude-code']['deleted'],0)
            held.rename(target)
            result = index.sync(self.directory,harnesses=['claude-code'])
            self.assertEqual(result['claude-code']['extracted'],1)
            self.assertEqual(result['claude-code']['offline'],0)

    def test_permission_denied_walk_keeps_session_and_reports_error(self):
        root = Path(self.tmp.name) / 'projects'; nested = root / 'nested'; nested.mkdir(parents=True)
        path = nested / 'one.jsonl'
        path.write_text('{"type":"user","message":{"role":"user","content":"old"}}\n')
        store = sessions.Store('claude-code','projects',root,'jsonl_dir')
        original = os.scandir
        def guarded(path):
            if Path(path) == nested:
                raise PermissionError('denied')
            return original(path)
        with patch.object(sessions,'stores_for',side_effect=lambda h:[store] if h == 'claude-code' else []):
            index.sync(self.directory)
            with patch.object(os,'scandir',guarded):
                result = index.sync(self.directory)
            self.assertFalse(result['discovery_complete'])
            self.assertGreaterEqual(result['claude-code']['error'],1)
            self.assertEqual(result['claude-code']['deleted'],0)
            with contextlib.closing(index.open_reader(self.directory)) as conn:
                self.assertEqual(conn.execute('SELECT count(*) FROM sessions').fetchone(), (1,))

    def test_source_stat_error_does_not_stop_another_store(self):
        root = Path(self.tmp.name)
        first = root / 'first'; first.mkdir()
        second = root / 'second'; second.mkdir()
        denied = first / 'denied.jsonl'; denied.write_text('{}\n')
        healthy = second / 'healthy.jsonl'; healthy.write_text('{}\n')
        stores = [sessions.Store('claude-code','first',first,'jsonl_dir'),
                  sessions.Store('claude-code','second',second,'jsonl_dir')]
        original = Path.stat
        def guarded(path,*args,**kwargs):
            if path == denied:
                raise PermissionError('denied')
            return original(path,*args,**kwargs)
        with patch.object(sessions,'stores_for',side_effect=lambda h:stores if h == 'claude-code' else []):
            index.sync(self.directory,harnesses=['claude-code'])
            healthy.write_text('{"type":"user","message":{"content":"new"}}\n')
            with patch.object(Path,'stat',guarded):
                result = index.sync(self.directory,harnesses=['claude-code'])
            self.assertFalse(result['discovery_complete'])
            self.assertEqual(result['claude-code']['extracted'],1)
            self.assertGreaterEqual(result['claude-code']['error'],1)
            with contextlib.closing(index.open_reader(self.directory)) as conn:
                self.assertEqual(conn.execute('SELECT state FROM sources WHERE source_key=?',(index.source_key(str(denied)),)).fetchone(),('error',))
                self.assertEqual(conn.execute('SELECT count(*) FROM sessions').fetchone(),(2,))

    def test_database_disappearing_during_discovery_preserves_session(self):
        db = Path(self.tmp.name) / 'devin.db'
        with contextlib.closing(sqlite3.connect(db)) as source, source:
            source.executescript('''CREATE TABLE sessions(id TEXT,working_directory TEXT,model TEXT,agent_mode TEXT,created_at TEXT,last_activity_at TEXT,title TEXT,hidden INTEGER,main_chain_id INTEGER);
                CREATE TABLE message_nodes(row_id INTEGER,session_id TEXT,node_id INTEGER,parent_node_id INTEGER,chat_message TEXT,created_at TEXT,metadata TEXT);
                INSERT INTO sessions VALUES('a',NULL,NULL,NULL,'1','1','a',0,1);''')
        store = sessions.Store('devin','sessions.db',db,'sqlite')
        original = index._db_revision
        def vanish(path):
            if path == db:
                path.unlink()
            return original(path)
        with patch.object(sessions,'stores_for',return_value=[store]):
            index.sync(self.directory,harnesses=['devin'])
            with patch.object(index,'_db_revision',side_effect=vanish):
                result = index.sync(self.directory,harnesses=['devin'],full=True)
            self.assertFalse(result['complete'])
            self.assertGreaterEqual(result['devin']['error'],1)
            with contextlib.closing(index.open_reader(self.directory)) as conn:
                self.assertEqual(conn.execute('SELECT count(*) FROM sessions').fetchone(),(1,))

    def test_coverage_counts_finals_and_unavailable_sessions(self):
        with index.writer(self.directory) as conn:
            row = {'session_key':'devin:a','harness':'devin','native_id':'a','store':'test',
                   'content_state':'stale', 'status_json':json.dumps({'final_basis':{'native:marker':2,'fallback':1}})}
            index.replace_session(conn,row,[{'kind':'answer','text':'one'},{'kind':'answer','text':'two'},{'kind':'answer','text':'three'}],[],[],[])
            index.replace_session(conn,{'session_key':'agy:a','harness':'agy','native_id':'a','store':'test','content_state':'unavailable'},[],[],[],[])
            result = index._coverage(conn,['devin','agy'])
            self.assertEqual(result['final_basis'],{'native':2,'fallback':1})
            self.assertEqual(result['devin']['stale'],1)
            self.assertEqual(result['agy']['content_unavailable'],1)
            self.assertIn(index.DEVIN_LIMIT,result['limits'])

    def test_unsupported_store_is_reported_in_coverage(self):
        db = Path(self.tmp.name) / 'empty.db'
        sqlite3.connect(db).close()
        store = sessions.Store('opencode','db',db,'sqlite')
        with patch.object(sessions,'stores_for',return_value=[store]):
            result = index.sync(self.directory,harnesses=['opencode'])
        self.assertFalse(result['complete'])
        self.assertFalse(result['discovery_complete'])
        self.assertEqual(result['opencode']['unsupported'],1)
        self.assertEqual(result['next'],'porch sessions index build')
        with contextlib.closing(index.open_reader(self.directory)) as conn:
            self.assertEqual(index._coverage(conn)['opencode']['unsupported'],1)

    def test_normalization_switch_is_atomic_for_existing_reader(self):
        root = Path(self.tmp.name) / 'projects'; root.mkdir()
        (root / 'one.jsonl').write_text('{"type":"user","message":{"role":"user","content":"needle"}}\n')
        store = sessions.Store('claude-code','projects',root,'jsonl_dir')
        with patch.object(sessions,'stores_for',side_effect=lambda h:[store] if h == 'claude-code' else []):
            index.sync(self.directory,harnesses=['claude-code'])
            with contextlib.closing(index.open_reader(self.directory)) as reader:
                reader.execute('BEGIN')
                before = reader.execute("SELECT value FROM meta WHERE key='normalization_version'").fetchone()
                self.assertEqual(reader.execute("SELECT count(*) FROM fts WHERE fts MATCH 'nee'").fetchone(),(1,))
                with patch.object(index,'NORMALIZATION_VERSION','new-version'):
                    result = index.sync(self.directory,harnesses=['claude-code'],full=True)
                    self.assertTrue(result['complete'])
                    self.assertEqual(reader.execute("SELECT value FROM meta WHERE key='normalization_version'").fetchone(),before)
                    self.assertEqual(reader.execute("SELECT count(*) FROM fts WHERE fts MATCH 'nee'").fetchone(),(1,))
                    with contextlib.closing(index.open_reader(self.directory)) as after:
                        self.assertEqual(after.execute("SELECT value FROM meta WHERE key='normalization_version'").fetchone(),('new-version',))
                        self.assertEqual(after.execute("SELECT count(*) FROM fts WHERE fts MATCH 'nee'").fetchone(),(1,))

    def test_full_sync_rereads_unchanged_manifest_mapping(self):
        root = Path(self.tmp.name) / 'desktop'; root.mkdir()
        path = root / 'local_a.json'
        def manifest(sid):
            return json.dumps({'sessionId':'desktop','cliSessionId':sid})
        path.write_text(manifest('one'))
        store = sessions.Store('claude-desktop','code-sessions',root,'json_dir')
        with patch.object(sessions,'stores_for',side_effect=lambda h:[store] if h == 'claude-desktop' else []):
            index.sync(self.directory,harnesses=['claude-desktop'])
            before = path.stat()
            path.write_text(manifest('two'))
            os.utime(path, ns=(before.st_atime_ns,before.st_mtime_ns))
            index.sync(self.directory,harnesses=['claude-desktop'],full=True)
            with contextlib.closing(index.open_reader(self.directory)) as conn:
                self.assertEqual(conn.execute('SELECT session_key FROM sessions').fetchall(), [('claude-code:two',)])

    def test_manifest_reassignment_failure_keeps_original_session(self):
        root = Path(self.tmp.name) / 'desktop'; root.mkdir()
        path = root / 'local_a.json'
        path.write_text(json.dumps({'sessionId':'desktop','cliSessionId':'one'}))
        store = sessions.Store('claude-desktop','code-sessions',root,'json_dir')
        with patch.object(sessions,'stores_for',side_effect=lambda h:[store] if h == 'claude-desktop' else []):
            index.sync(self.directory,harnesses=['claude-desktop'])
            path.write_text(json.dumps({'sessionId':'desktop','cliSessionId':'other'}))
            with patch.object(index,'extract_unit',side_effect=index.SessionIndexError('fixture error')):
                result = index.sync(self.directory,harnesses=['claude-desktop'])
            self.assertFalse(result['complete'])
            with contextlib.closing(index.open_reader(self.directory)) as conn:
                self.assertEqual(conn.execute('SELECT session_key FROM sessions').fetchall(), [('claude-code:one',)])
                self.assertEqual(conn.execute("SELECT session_key FROM aliases WHERE kind='desktop'").fetchall(), [('claude-code:one',)])

    def test_extractor_version_bump_only_reextracts_affected_harness(self):
        root = Path(self.tmp.name)
        claude = root / 'claude'; claude.mkdir()
        (claude / 'one.jsonl').write_text('{"type":"user","message":{"role":"user","content":"hello"}}\n')
        runs = root / 'runs'; run = runs / 'r'; run.mkdir(parents=True)
        (run / 'meta.json').write_text('{"run_id":"r","status":"completed"}')
        (run / 'final.txt').write_text('answer')
        stores = {'claude-code':[sessions.Store('claude-code','projects',claude,'jsonl_dir')],
                  'claude-desktop':[], 'porch':[sessions.Store('porch','runs',runs,'run_registry')]}
        with patch.object(sessions,'stores_for',side_effect=lambda h:stores[h]):
            index.sync(self.directory,harnesses=['claude-code','porch'])
            with patch.dict(index.EXTRACTOR_VERSIONS,{'porch':'changed'}):
                result = index.sync(self.directory,harnesses=['claude-code','porch'])
            self.assertEqual(result['porch']['extracted'],1)
            self.assertEqual(result['claude-code']['extracted'],0)

    def test_concurrent_sync_readers_observe_committed_session(self):
        root = Path(self.tmp.name) / 'projects'; root.mkdir()
        transcript = root / 'one.jsonl'
        def record(text):
            return json.dumps({'type':'user','message':{'role':'user','content':text}})+'\n'
        transcript.write_text(record('old'))
        store = sessions.Store('claude-code','projects',root,'jsonl_dir')
        with patch.object(sessions,'stores_for',side_effect=lambda h:[store] if h == 'claude-code' else []):
            index.sync(self.directory,harnesses=['claude-code'])
        transcript.write_text(record('new'))
        marker = Path(self.tmp.name) / 'paused'
        release = Path(self.tmp.name) / 'release'
        script = '''
import json,sys,time
from pathlib import Path
sys.path.insert(0,sys.argv[1])
import sessions,sessions_index as index
store = sessions.Store('claude-code','projects',Path(sys.argv[3]),'jsonl_dir')
sessions.stores_for = lambda h: [store] if h == 'claude-code' else []
if sys.argv[6] == 'writer':
    original = index.replace_session
    def replace(conn,*args,**kwargs):
        def pause():
            Path(sys.argv[4]).write_text('paused')
            end = time.monotonic() + 15
            while not Path(sys.argv[5]).exists() and time.monotonic() < end:
                time.sleep(.01)
            return 0
        conn.create_function('test_pause',0,pause)
        conn.execute('CREATE TEMP TRIGGER test_pause_trigger BEFORE INSERT ON sessions BEGIN SELECT test_pause(); END')
        return original(conn,*args,**kwargs)
    index.replace_session = replace
    result = index.sync(Path(sys.argv[2]),harnesses=['claude-code'])
else:
    result = index.sync(Path(sys.argv[2]),harnesses=['claude-code'],deadline=time.monotonic()+.2)
print(json.dumps(result))
'''
        lib = str(Path(__file__).resolve().parents[1] / 'lib')
        args = [sys.executable,'-c',script,lib,str(self.directory),str(root),str(marker),str(release)]
        writer = subprocess.Popen(args+['writer'],stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
        try:
            end = time.monotonic() + 10
            while not marker.exists() and writer.poll() is None and time.monotonic() < end:
                time.sleep(.01)
            self.assertTrue(marker.exists(), 'writer did not reach the open transaction')
            readers = [subprocess.Popen(args+['reader'],stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True) for _ in range(3)]
            with contextlib.closing(index.open_reader(self.directory)) as conn:
                self.assertEqual(conn.execute("SELECT text FROM docs WHERE kind='prompt'").fetchone(),('old',))
            for process in readers:
                out, err = process.communicate(timeout=10)
                self.assertEqual(process.returncode,0,err)
                result = json.loads(out)
                self.assertFalse(result['synced'])
                self.assertEqual(result['reason'],'another sync in progress')
                self.assertEqual(result['claude-code']['error'],0)
            release.touch()
            out, err = writer.communicate(timeout=10)
            self.assertEqual(writer.returncode,0,err)
            self.assertEqual(json.loads(out)['claude-code']['extracted'],1)
            with contextlib.closing(index.open_reader(self.directory)) as conn:
                self.assertEqual(conn.execute("SELECT text FROM docs WHERE kind='prompt'").fetchone(),('new',))
        finally:
            release.touch()
            if writer.poll() is None:
                writer.kill()
                writer.communicate()

    def test_offline_store_does_not_block_other_stores(self):
        root = Path(self.tmp.name)
        one = root / 'one'; one.mkdir()
        two = root / 'two'; two.mkdir()
        record = lambda text: json.dumps({'type':'user','message':{'role':'user','content':text}}) + '\n'
        (one / 'a.jsonl').write_text(record('first'))
        (two / 'b.jsonl').write_text(record('offline'))
        stores = [sessions.Store('claude-code','one',one,'jsonl_dir'), sessions.Store('claude-code','two',two,'jsonl_dir')]
        with patch.object(sessions, 'stores_for', side_effect=lambda h: stores if h == 'claude-code' else []):
            self.assertEqual(index.sync(self.directory)['claude-code']['extracted'], 2)
            moved = root / 'offline'; two.rename(moved)
            (one / 'a.jsonl').write_text(record('updated'))
            result = index.sync(self.directory)
            self.assertEqual(result['claude-code']['extracted'], 1)
            self.assertEqual(result['claude-code']['deleted'], 0)
            with contextlib.closing(index.open_reader(self.directory)) as conn:
                self.assertEqual(conn.execute("SELECT text FROM docs WHERE session_key='claude-code:a' AND kind='prompt'").fetchone(), ('updated',))
                self.assertEqual(conn.execute("SELECT state FROM sources WHERE store='two'").fetchone(), ('offline',))
                self.assertEqual(conn.execute('SELECT count(*) FROM sessions').fetchone(), (2,))
            moved.rename(two)
            index.sync(self.directory)
            with contextlib.closing(index.open_reader(self.directory)) as conn:
                self.assertEqual(conn.execute("SELECT state FROM sources WHERE store='two'").fetchone(), ('ok',))

    def test_sync_uses_unit_version_and_deletes_other_harness(self):
        root = Path(self.tmp.name) / 'fake'; root.mkdir()
        store = sessions.Store('fake', 'fake', root, 'jsonl_dir')
        key = index.source_key(str(root / 'new'))
        unit = index.Unit('fake:new', 'fake', 'fake', {key: 'revision'}, {key: 'content'}, 1, {key: store})
        def payload(_):
            return ({'session_key':'fake:new', 'harness':'fake', 'native_id':'new',
                     'store':'fake', 'content_state':'ok'}, [], [], [])
        with index.writer(self.directory) as conn:
            old_key = index.source_key(str(root / 'old'))
            conn.execute("INSERT INTO sources(source_key,harness,store,kind,state) VALUES(?,'fake','fake','file','ok')", (old_key,))
            conn.commit()
            index.replace_session(conn, {'session_key':'fake:old','harness':'fake','native_id':'old','store':'fake','content_state':'ok'}, [],
                                  [{'source_key':old_key,'role':'content','fingerprint':'old','extractor_version':'9'}], [], [])
        with patch.dict(index.UNIT_BUILDERS, {'fake': lambda *_: iter([unit])}), \
             patch.dict(index.EXTRACTOR_VERSIONS, {'fake':'9'}), \
             patch.object(sessions, 'stores_for', return_value=[store]), \
             patch.object(index, 'extract_unit', side_effect=payload):
            result = index.sync(self.directory, harnesses=['fake'])
            self.assertEqual(result['fake']['extracted'], 1)
            self.assertEqual(result['fake']['deleted'], 1)
        with contextlib.closing(index.open_reader(self.directory)) as conn:
            self.assertEqual(conn.execute('SELECT session_key FROM sessions').fetchall(), [('fake:new',)])
            self.assertEqual(conn.execute('SELECT extractor_version FROM session_sources').fetchone(), ('9',))

    def test_codex_state_rollout_and_lineage(self):
        root = Path(self.tmp.name) / 'codex'; root.mkdir()
        active = root / 'sessions'; active.mkdir()
        archived = root / 'archived_sessions'; archived.mkdir()
        db = root / 'state_1.sqlite'
        files = {}
        for sid in ('one', 'two'):
            path = active / f'rollout-2026-01-01T00-00-00-{sid}.jsonl'
            path.write_text(json.dumps({'timestamp':'2026-01-01T00:00:00Z','type':'session_meta','payload':{'id':sid}})+'\n')
            files[sid] = path
        with contextlib.closing(sqlite3.connect(db)) as conn, conn:
            conn.executescript('CREATE TABLE threads(id TEXT PRIMARY KEY, title TEXT, rollout_path TEXT); CREATE TABLE thread_spawn_edges(parent_thread_id TEXT,child_thread_id TEXT);')
            conn.executemany('INSERT INTO threads VALUES(?,?,?)', [(sid, sid, str(files[sid])) for sid in files])
        stores = [sessions.Store('codex','sessions',active,'jsonl_dir'), sessions.Store('codex','archived',archived,'jsonl_dir'), sessions.Store('codex','state:state_1.sqlite',db,'sqlite')]
        with patch.object(sessions, 'stores_for', return_value=stores):
            self.assertEqual(index.sync(self.directory,harnesses=['codex'])['codex']['extracted'], 2)
            with patch.object(index, '_codex_rows', side_effect=AssertionError('unchanged database read')):
                self.assertEqual(index.sync(self.directory,harnesses=['codex'])['codex']['extracted'], 0)
            with files['one'].open('a') as out: out.write('{}\n')
            self.assertEqual(index.sync(self.directory,harnesses=['codex'])['codex']['extracted'], 1)
            with contextlib.closing(sqlite3.connect(db)) as conn, conn: conn.execute("UPDATE threads SET title='changed' WHERE id='two'")
            self.assertEqual(index.sync(self.directory,harnesses=['codex'])['codex']['extracted'], 1)
            with contextlib.closing(index.open_reader(self.directory)) as conn:
                self.assertEqual(conn.execute("SELECT title FROM sessions WHERE session_key='codex:two'").fetchone(), ('changed',))
            with contextlib.closing(sqlite3.connect(db)) as conn, conn: conn.execute("INSERT INTO thread_spawn_edges VALUES('one','two')")
            self.assertEqual(index.sync(self.directory,harnesses=['codex'])['codex']['extracted'], 1)
            with contextlib.closing(index.open_reader(self.directory)) as conn:
                self.assertEqual(conn.execute("SELECT target_key FROM edges WHERE session_key='codex:two' AND basis='thread_spawn_edges'").fetchone(), ('codex:one',))
            files['one'].rename(archived / files['one'].name)
            result = index.sync(self.directory,harnesses=['codex'])
            self.assertEqual(result['codex']['deleted'], 0)
            self.assertEqual(result['codex']['extracted'], 1)
            try:
                from compression import zstd
            except ImportError:
                return
            plain = archived / files['one'].name
            compressed = Path(str(plain) + '.zst')
            compressed.write_bytes(zstd.compress(plain.read_bytes()))
            plain.unlink()
            result = index.sync(self.directory,harnesses=['codex'])
            self.assertEqual(result['codex']['deleted'], 0)
            self.assertEqual(result['codex']['extracted'], 1)
            with contextlib.closing(index.open_reader(self.directory)) as conn:
                self.assertEqual(conn.execute("SELECT count(*) FROM sessions WHERE session_key='codex:one'").fetchone(), (1,))

    def test_codex_snapshot_ignores_other_harness_databases_and_survives_live_change(self):
        root = Path(self.tmp.name) / 'codex'; root.mkdir()
        active = root / 'sessions'; active.mkdir()
        (root / 'archived_sessions').mkdir()
        rollout = active / 'rollout-2026-01-01T00-00-00-one.jsonl'
        rollout.write_text(json.dumps({'timestamp':'2026-01-01T00:00:00Z','type':'session_meta','payload':{'id':'one'}})+'\n')
        state_db = root / 'state_1.sqlite'
        with contextlib.closing(sqlite3.connect(state_db)) as conn:
            conn.execute('PRAGMA journal_mode=WAL')
            conn.execute('CREATE TABLE threads(id TEXT PRIMARY KEY,title TEXT,rollout_path TEXT)')
            conn.execute('INSERT INTO threads VALUES(?,?,?)',('one','old',str(rollout)))
            conn.commit()
        devin_db = Path(self.tmp.name) / 'devin.db'
        with contextlib.closing(sqlite3.connect(devin_db)) as conn:
            conn.executescript('''CREATE TABLE sessions(id TEXT,working_directory TEXT,model TEXT,agent_mode TEXT,created_at TEXT,last_activity_at TEXT,title TEXT,hidden INTEGER,main_chain_id INTEGER);
                CREATE TABLE message_nodes(row_id INTEGER,session_id TEXT,node_id INTEGER,parent_node_id INTEGER,chat_message TEXT,created_at TEXT,metadata TEXT);
                INSERT INTO sessions VALUES('d',NULL,NULL,NULL,'1','1','devin',0,1);''')
            conn.commit()
        stores = {'codex':[sessions.Store('codex','sessions',active,'jsonl_dir'),
                           sessions.Store('codex','archived',root/'archived_sessions','jsonl_dir'),
                           sessions.Store('codex','state:state_1.sqlite',state_db,'sqlite')],
                  'devin':[sessions.Store('devin','sessions.db',devin_db,'sqlite')]}
        original = sessions.codex_rollout_shared
        def update_state(*args,**kwargs):
            with contextlib.closing(sqlite3.connect(state_db)) as conn:
                conn.execute("UPDATE threads SET title='new' WHERE id='one'")
                conn.commit()
            return original(*args,**kwargs)
        with patch.object(sessions,'stores_for',side_effect=lambda h:stores[h]):
            with patch.object(sessions,'codex_rollout_shared',side_effect=update_state):
                result = index.sync(self.directory,harnesses=['codex','devin'])
            self.assertEqual(result['codex']['extracted'],1)
            self.assertEqual(result['devin']['extracted'],1)
            self.assertGreaterEqual(result['codex']['pending'],1)
            with contextlib.closing(index.open_reader(self.directory)) as conn:
                self.assertEqual(conn.execute("SELECT title FROM sessions WHERE session_key='codex:one'").fetchone(),('old',))
            index.sync(self.directory,harnesses=['codex','devin'])
            with contextlib.closing(index.open_reader(self.directory)) as conn:
                self.assertEqual(conn.execute("SELECT title FROM sessions WHERE session_key='codex:one'").fetchone(),('new',))

    def test_codex_killed_between_state_units_resumes(self):
        root = Path(self.tmp.name) / 'codex'; root.mkdir()
        db = root / 'state_1.sqlite'
        with contextlib.closing(sqlite3.connect(db)) as conn, conn:
            conn.executescript("CREATE TABLE threads(id TEXT PRIMARY KEY, title TEXT); INSERT INTO threads VALUES('one','first'); INSERT INTO threads VALUES('two','second');")
        store = sessions.Store('codex','state:state_1.sqlite',db,'sqlite')
        with patch.object(sessions, 'stores_for', return_value=[store]):
            self.assertEqual(index.sync(self.directory, harnesses=['codex'])['codex']['extracted'], 2)
        with contextlib.closing(sqlite3.connect(db)) as conn, conn:
            conn.execute("UPDATE threads SET title=title || ' changed'")
        script = '''
import os, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
import sessions, sessions_index as index
store = sessions.Store('codex','state:state_1.sqlite',Path(sys.argv[3]),'sqlite')
sessions.stores_for = lambda h: [store]
original = index.replace_session
def die(*args, **kwargs):
    original(*args, **kwargs)
    os._exit(23)
index.replace_session = die
index.sync(Path(sys.argv[2]), harnesses=['codex'])
'''
        lib = str(Path(__file__).resolve().parents[1] / 'lib')
        process = subprocess.run([sys.executable, '-c', script, lib, str(self.directory), str(db)], capture_output=True, timeout=20)
        self.assertEqual(process.returncode, 23, process.stderr.decode())
        with contextlib.closing(index.open_reader(self.directory)) as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM sessions WHERE title LIKE '% changed'").fetchone(), (1,))
        with patch.object(sessions, 'stores_for', return_value=[store]):
            result = index.sync(self.directory, harnesses=['codex'])
        self.assertEqual(result['codex']['extracted'], 1)
        with contextlib.closing(index.open_reader(self.directory)) as conn:
            self.assertEqual(conn.execute('SELECT count(*) FROM sessions').fetchone(), (2,))
            self.assertEqual(conn.execute('SELECT count(*) FROM session_sources').fetchone(), (2,))

    def test_codex_external_rollout_is_tracked(self):
        root = Path(self.tmp.name) / 'codex'; root.mkdir()
        (root / 'sessions').mkdir(); (root / 'archived_sessions').mkdir()
        external = Path(self.tmp.name) / 'elsewhere'; external.mkdir()
        path = external / 'rollout-2026-01-01T00-00-00-one.jsonl'
        def record(text):
            return json.dumps({'timestamp':'2026-01-01T00:00:00Z','type':'event_msg','payload':{'type':'user_message','message':text}}) + '\n'
        path.write_text(json.dumps({'timestamp':'2026-01-01T00:00:00Z','type':'session_meta','payload':{'id':'one'}})+'\n' + record('first'))
        db = root / 'state_1.sqlite'
        with contextlib.closing(sqlite3.connect(db)) as conn, conn:
            conn.execute('CREATE TABLE threads(id TEXT PRIMARY KEY,title TEXT,rollout_path TEXT)')
            conn.execute('INSERT INTO threads VALUES(?,?,?)', ('one','title',str(path)))
        stores = [sessions.Store('codex','sessions',root/'sessions','jsonl_dir'),
                  sessions.Store('codex','archived',root/'archived_sessions','jsonl_dir'),
                  sessions.Store('codex','state:state_1.sqlite',db,'sqlite')]
        with patch.object(sessions, 'stores_for', return_value=stores):
            self.assertEqual(index.sync(self.directory,harnesses=['codex'])['codex']['extracted'], 1)
            with contextlib.closing(index.open_reader(self.directory)) as conn:
                self.assertEqual(conn.execute('SELECT count(*) FROM session_sources WHERE source_key=?', (index.source_key(str(path)),)).fetchone(), (1,))
            with path.open('a') as out: out.write(record('second'))
            self.assertEqual(index.sync(self.directory,harnesses=['codex'])['codex']['extracted'], 1)
            with contextlib.closing(index.open_reader(self.directory)) as conn:
                self.assertIn(('second',), conn.execute("SELECT text FROM docs WHERE kind='prompt'").fetchall())
            path.unlink()
            index.sync(self.directory,harnesses=['codex'])
            with contextlib.closing(index.open_reader(self.directory)) as conn:
                self.assertEqual(conn.execute('SELECT content_state FROM sessions').fetchone(), ('unavailable',))
                self.assertEqual(conn.execute('SELECT state FROM sources WHERE source_key=?', (index.source_key(str(path)),)).fetchone(), ('offline',))
            still_missing = index.sync(self.directory,harnesses=['codex'])
            self.assertEqual(still_missing['codex']['extracted'], 0)
            self.assertGreaterEqual(still_missing['codex']['offline'], 1)
            path.write_text(json.dumps({'timestamp':'2026-01-01T00:00:00Z','type':'session_meta','payload':{'id':'one'}})+'\n' + record('returned'))
            self.assertEqual(index.sync(self.directory,harnesses=['codex'])['codex']['extracted'], 1)
            with contextlib.closing(index.open_reader(self.directory)) as conn:
                self.assertEqual(conn.execute('SELECT content_state FROM sessions').fetchone(), ('ok',))
                self.assertEqual(conn.execute('SELECT state FROM sources WHERE source_key=?', (index.source_key(str(path)),)).fetchone(), ('ok',))

    def test_devin_and_opencode_database_units(self):
        root = Path(self.tmp.name)
        devin = root / 'devin.db'; oc = root / 'opencode.db'
        with contextlib.closing(sqlite3.connect(devin)) as db:
            db.executescript('''CREATE TABLE sessions(id TEXT,working_directory TEXT,model TEXT,agent_mode TEXT,created_at TEXT,last_activity_at TEXT,title TEXT,hidden INTEGER,main_chain_id INTEGER);
                CREATE TABLE message_nodes(row_id INTEGER PRIMARY KEY,session_id TEXT,node_id INTEGER,parent_node_id INTEGER,chat_message TEXT,created_at TEXT,metadata TEXT);
                INSERT INTO sessions VALUES('a',NULL,NULL,NULL,'1','1','a',0,1);
                INSERT INTO sessions VALUES('b',NULL,NULL,NULL,'1','1','b',0,2);
                INSERT INTO message_nodes VALUES(1,'a',1,NULL,'{"role":"user","content":"first","metadata":{"is_user_input":1}}',NULL,NULL);''')
            db.commit()
        with contextlib.closing(sqlite3.connect(oc)) as db:
            db.executescript('''CREATE TABLE session_v2(id TEXT,title TEXT,time_updated INTEGER,time_busy INTEGER,time_idle INTEGER);
                CREATE TABLE session_message(id TEXT,session_id TEXT,type TEXT,seq INTEGER,time_created INTEGER,time_updated INTEGER,data TEXT);
                CREATE TABLE session_part(message_id TEXT,session_id TEXT,ordinal INTEGER,type TEXT,call_id TEXT,time_created INTEGER,time_updated INTEGER,data TEXT);
                INSERT INTO session_v2 VALUES('v','title',1,NULL,NULL);
                INSERT INTO session_message VALUES('m','v','user',1,1,1,'{"text":"hello"}');
                INSERT INTO session_part VALUES('m','v',1,'text',NULL,1,1,'{"text":"hello"}');''')
            db.commit()
        stores = {'devin':[sessions.Store('devin','sessions.db',devin,'sqlite')],
                  'opencode':[sessions.Store('opencode','db',oc,'sqlite')]}
        with patch.object(sessions,'stores_for',side_effect=lambda h: stores[h]):
            result = index.sync(self.directory,harnesses=['devin','opencode'])
            self.assertEqual(result['devin']['extracted'], 2)
            self.assertEqual(result['opencode']['extracted'], 1)
            with patch.object(sessions,'session_fingerprints',side_effect=AssertionError('unchanged DB queried')):
                result = index.sync(self.directory,harnesses=['devin','opencode'])
                self.assertEqual(result['devin']['extracted'] + result['opencode']['extracted'], 0)
            with contextlib.closing(sqlite3.connect(devin)) as db:
                db.execute('INSERT INTO message_nodes VALUES(2,?,?,?,?,?,?)', ('a',2,1,'{"role":"user","content":"second","metadata":{"is_user_input":1}}',None,None)); db.commit()
            self.assertEqual(index.sync(self.directory,harnesses=['devin','opencode'])['devin']['extracted'],1)
            with contextlib.closing(sqlite3.connect(devin)) as db:
                db.execute('DELETE FROM message_nodes WHERE row_id=2'); db.commit()
            self.assertEqual(index.sync(self.directory,harnesses=['devin','opencode'])['devin']['extracted'],1)
            with contextlib.closing(sqlite3.connect(oc)) as db:
                db.execute("UPDATE session_part SET time_updated=2,data='{\"text\":\"changed\"}'"); db.commit()
            self.assertEqual(index.sync(self.directory,harnesses=['devin','opencode'])['opencode']['extracted'],1)
            with contextlib.closing(sqlite3.connect(oc)) as db:
                db.execute('UPDATE session_v2 SET time_busy=10,time_idle=11'); db.commit()
            self.assertEqual(index.sync(self.directory,harnesses=['devin','opencode'])['opencode']['extracted'],1)
            with contextlib.closing(sqlite3.connect(devin)) as db:
                db.execute("DELETE FROM sessions WHERE id='b'"); db.commit()
            self.assertEqual(index.sync(self.directory,harnesses=['devin','opencode'])['devin']['deleted'],1)

    def test_full_sync_reads_devin_unchanged_fingerprint_and_rebuilds_normalization(self):
        db = Path(self.tmp.name) / 'devin.db'
        with contextlib.closing(sqlite3.connect(db)) as source, source:
            source.executescript('''CREATE TABLE sessions(id TEXT,working_directory TEXT,model TEXT,agent_mode TEXT,created_at TEXT,last_activity_at TEXT,title TEXT,hidden INTEGER,main_chain_id INTEGER);
              CREATE TABLE message_nodes(row_id INTEGER PRIMARY KEY,session_id TEXT,node_id INTEGER,parent_node_id INTEGER,chat_message TEXT,created_at TEXT,metadata TEXT);
              INSERT INTO sessions VALUES('a',NULL,NULL,NULL,'1','1','a',0,1);
              INSERT INTO message_nodes VALUES(1,'a',1,NULL,'{"role":"user","content":"old","metadata":{"is_user_input":1}}',NULL,NULL);''')
        store = sessions.Store('devin','sessions.db',db,'sqlite')
        with patch.object(sessions,'stores_for',return_value=[store]):
            self.assertEqual(index.sync(self.directory,harnesses=['devin'])['devin']['extracted'],1)
            with contextlib.closing(sqlite3.connect(db)) as source, source:
                source.execute('UPDATE message_nodes SET chat_message=? WHERE row_id=1', ('{"role":"user","content":"new","metadata":{"is_user_input":1}}',))
            unchanged = index.sync(self.directory,harnesses=['devin'])
            self.assertEqual(unchanged['devin']['extracted'],0)
            self.assertIn(index.DEVIN_LIMIT, unchanged['limits'])
            with contextlib.closing(index.open_reader(self.directory)) as reader:
                self.assertEqual(reader.execute("SELECT text FROM docs WHERE kind='prompt'").fetchone(),('old',))
            self.assertEqual(index.sync(self.directory,harnesses=['devin'],full=True)['devin']['extracted'],1)
            with index.writer(self.directory) as writer:
                writer.execute("UPDATE meta SET value='old' WHERE key='normalization_version'")
                writer.commit()
            with contextlib.closing(index.open_reader(self.directory)) as reader:
                with self.assertRaisesRegex(index.SessionIndexError,'run porch sessions index build'):
                    index.check_normalization(reader)
            index.sync(self.directory,harnesses=['devin'],full=True)
            with contextlib.closing(index.open_reader(self.directory)) as reader:
                index.check_normalization(reader)
                self.assertEqual(reader.execute("SELECT text FROM docs WHERE kind='prompt'").fetchone(),('new',))
                self.assertEqual(reader.execute("SELECT count(*) FROM fts WHERE fts MATCH 'new'").fetchone(),(1,))

    def test_devin_killed_sync_resumes_remaining_session(self):
        db = Path(self.tmp.name) / 'devin.db'
        with contextlib.closing(sqlite3.connect(db)) as source:
            source.executescript('''CREATE TABLE sessions(id TEXT,working_directory TEXT,model TEXT,agent_mode TEXT,created_at TEXT,last_activity_at TEXT,title TEXT,hidden INTEGER,main_chain_id INTEGER);
                CREATE TABLE message_nodes(row_id INTEGER PRIMARY KEY,session_id TEXT,node_id INTEGER,parent_node_id INTEGER,chat_message TEXT,created_at TEXT,metadata TEXT);
                INSERT INTO sessions VALUES('a',NULL,NULL,NULL,'1','1','a',0,1);
                INSERT INTO sessions VALUES('b',NULL,NULL,NULL,'1','1','b',0,2);''')
            source.commit()
        store = sessions.Store('devin','sessions.db',db,'sqlite')
        with patch.object(sessions,'stores_for',return_value=[store]):
            self.assertEqual(index.sync(self.directory,harnesses=['devin'])['devin']['extracted'],2)
        with contextlib.closing(sqlite3.connect(db)) as source:
            source.executemany('INSERT INTO message_nodes VALUES(?,?,?,?,?,?,?)', [
                (1,'a',1,None,'{"role":"user","content":"new-a","metadata":{"is_user_input":1}}',None,None),
                (2,'b',2,None,'{"role":"user","content":"new-b","metadata":{"is_user_input":1}}',None,None)])
            source.commit()
        script = '''
import os, sys
from pathlib import Path
sys.path.insert(0,sys.argv[1])
import sessions, sessions_index as index
store = sessions.Store('devin','sessions.db',Path(sys.argv[3]),'sqlite')
sessions.stores_for = lambda h: [store]
original = index.replace_session
def die(*args,**kwargs):
    original(*args,**kwargs)
    os._exit(23)
index.replace_session = die
index.sync(Path(sys.argv[2]),harnesses=['devin'])
'''
        lib = str(Path(__file__).resolve().parents[1] / 'lib')
        process = subprocess.run([sys.executable,'-c',script,lib,str(self.directory),str(db)],capture_output=True,timeout=20)
        self.assertEqual(process.returncode,23,process.stderr.decode())
        with contextlib.closing(index.open_reader(self.directory)) as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM docs WHERE kind='prompt'").fetchone(), (1,))
        with patch.object(sessions,'stores_for',return_value=[store]):
            self.assertEqual(index.sync(self.directory,harnesses=['devin'])['devin']['extracted'],1)
        with contextlib.closing(index.open_reader(self.directory)) as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM docs WHERE kind='prompt'").fetchone(), (2,))

    def test_porch_and_cowork_updates(self):
        root = Path(self.tmp.name)
        runs = root / 'runs'; run = runs / 'r'; run.mkdir(parents=True)
        artifacts = root / 'artifacts'; (artifacts / 'normalized').mkdir(parents=True)
        (run / 'meta.json').write_text(json.dumps({'run_id':'r','task':'do it','artifacts_dir':str(artifacts),'status':'completed'}))
        events = artifacts / 'normalized' / 'events.jsonl'
        events.write_text(json.dumps({'type':'run_started'})+'\n')
        desktop = root / 'cowork'; desktop.mkdir()
        (desktop / 'local_a.json').write_text(json.dumps({'sessionId':'local_a','title':'Cowork'}))
        audit = desktop / 'local_a' / 'audit.jsonl'; audit.parent.mkdir()
        audit.write_text(json.dumps({'type':'user','message':{'content':'hello'}})+'\n')
        stores = {'porch':[sessions.Store('porch','runs',runs,'run_registry')],
                  'claude-desktop':[sessions.Store('claude-desktop','cowork',desktop,'session_dirs')], 'claude-code':[]}
        with patch.object(sessions,'stores_for',side_effect=lambda h: stores.get(h,[])):
            result = index.sync(self.directory,harnesses=['porch','claude-desktop'])
            self.assertEqual(result['porch']['extracted'],1)
            self.assertEqual(result['claude-desktop']['extracted'],1)
            unchanged = index.sync(self.directory,harnesses=['porch','claude-desktop'])
            self.assertEqual(unchanged['porch']['extracted'] + unchanged['claude-desktop']['extracted'],0)
            (run / 'final.txt').write_text('saved answer')
            self.assertEqual(index.sync(self.directory,harnesses=['porch','claude-desktop'])['porch']['extracted'],1)
            with contextlib.closing(index.open_reader(self.directory)) as conn:
                self.assertIn(('saved answer',),conn.execute("SELECT text FROM docs WHERE session_key='porch:r' AND kind='answer'").fetchall())
            with events.open('a') as out: out.write(json.dumps({'type':'run_completed'})+'\n')
            self.assertEqual(index.sync(self.directory,harnesses=['porch','claude-desktop'])['porch']['extracted'],1)
            with audit.open('a') as out: out.write(json.dumps({'type':'user','message':{'content':'second'}})+'\n')
            self.assertEqual(index.sync(self.directory,harnesses=['porch','claude-desktop'])['claude-desktop']['extracted'],1)

    def test_porch_manifest_race_does_not_invalidate_other_runs(self):
        root = Path(self.tmp.name) / 'runs'
        for sid in ('one','two','three'):
            run = root/sid; run.mkdir(parents=True)
            (run/'meta.json').write_text(json.dumps({'run_id':sid,'task':sid}))
            (run/'final.txt').write_text('old')
        store = sessions.Store('porch','runs',root,'run_registry')
        with patch.object(sessions,'stores_for',side_effect=lambda h:[store] if h=='porch' else []):
            index.sync(self.directory,harnesses=['porch'])
            meta = root/'one'/'meta.json'
            meta.write_text(json.dumps({'run_id':'one','task':'one changed'}))
            (root/'two'/'final.txt').write_text('new answer')
            original = index.FileSnapshot.verify
            def race(snapshot):
                if str(meta) in snapshot._files:
                    raise index.SourceChanged('run metadata changed during discovery')
                return original(snapshot)
            with patch.object(index.FileSnapshot,'verify',race):
                result = index.sync(self.directory,harnesses=['porch'])
            self.assertFalse(result['complete'])
            self.assertEqual(result['porch']['extracted'],1)
            self.assertGreaterEqual(result['porch']['pending'],1)
            with contextlib.closing(index.open_reader(self.directory)) as conn:
                self.assertEqual(conn.execute("SELECT text FROM docs WHERE session_key='porch:two' AND kind='answer'").fetchone(),('new answer',))
                self.assertEqual(conn.execute('SELECT count(*) FROM sessions').fetchone(),(3,))
                self.assertEqual(conn.execute("SELECT count(*) FROM sources WHERE state='error'").fetchone(),(0,))
            result = index.sync(self.directory,harnesses=['porch'])
            self.assertEqual(result['porch']['extracted'],1)
            self.assertEqual(result['porch']['pending'],0)

    def test_recovered_enumeration_error_does_not_reextract_unchanged_sessions(self):
        root = Path(self.tmp.name) / 'runs'
        for sid in ('one','two'):
            run = root/sid; run.mkdir(parents=True)
            (run/'meta.json').write_text(json.dumps({'run_id':sid,'task':sid}))
            (run/'final.txt').write_text('answer')
        store = sessions.Store('porch','runs',root,'run_registry')
        with patch.object(sessions,'stores_for',side_effect=lambda h:[store] if h=='porch' else []):
            index.sync(self.directory,harnesses=['porch'])
            builder = index.UNIT_BUILDERS['porch']
            def incomplete(store, previous):
                yield from builder(store, previous)
                raise index.SessionIndexError('permission denied during enumeration')
            with patch.dict(index.UNIT_BUILDERS,{'porch':incomplete}):
                failed = index.sync(self.directory,harnesses=['porch'])
            self.assertFalse(failed['complete'])
            with contextlib.closing(index.open_reader(self.directory)) as conn:
                self.assertGreater(conn.execute("SELECT count(*) FROM sources WHERE state='error'").fetchone()[0],0)
            recovered = index.sync(self.directory,harnesses=['porch'])
            self.assertEqual(recovered['porch']['extracted'],0)
            self.assertTrue(recovered['complete'])

    def test_unchanged_sources_need_no_redundant_status_updates(self):
        root = Path(self.tmp.name) / 'projects'; root.mkdir()
        (root/'one.jsonl').write_text('{"type":"user","message":{"role":"user","content":"hello"}}\n')
        store = sessions.Store('claude-code','projects',root,'jsonl_dir')
        original = index._open_writer
        sql = []
        def traced(directory):
            conn = original(directory)
            conn.set_trace_callback(sql.append)
            return conn
        with patch.object(sessions,'stores_for',side_effect=lambda h:[store] if h=='claude-code' else []):
            index.sync(self.directory,harnesses=['claude-code'])
            with patch.object(index,'_open_writer',side_effect=traced):
                result = index.sync(self.directory,harnesses=['claude-code'])
        self.assertEqual(result['claude-code']['extracted'],0)
        self.assertFalse(any(statement.lstrip().startswith('UPDATE sources SET state=') for statement in sql))

    def test_bad_porch_manifest_keeps_its_session_and_does_not_block_other_runs(self):
        root = Path(self.tmp.name) / 'runs'
        for sid in ('one','two'):
            run = root/sid; run.mkdir(parents=True)
            (run/'meta.json').write_text(json.dumps({'run_id':sid,'task':sid}))
            (run/'final.txt').write_text('old')
        store = sessions.Store('porch','runs',root,'run_registry')
        with patch.object(sessions,'stores_for',side_effect=lambda h:[store] if h=='porch' else []):
            index.sync(self.directory,harnesses=['porch'])
            (root/'one'/'meta.json').write_text('{bad json')
            (root/'two'/'final.txt').write_text('updated')
            result = index.sync(self.directory,harnesses=['porch'])
            self.assertEqual(result['porch']['extracted'],1)
            self.assertGreaterEqual(result['porch']['error'],1)
            self.assertFalse(result['complete'])
            with contextlib.closing(index.open_reader(self.directory)) as conn:
                self.assertEqual(conn.execute('SELECT count(*) FROM sessions').fetchone(),(2,))
                self.assertEqual(conn.execute("SELECT text FROM docs WHERE session_key='porch:two' AND kind='answer'").fetchone(),('updated',))
            (root/'one'/'meta.json').write_text(json.dumps({'run_id':'one','task':'repaired'}))
            result = index.sync(self.directory,harnesses=['porch'])
            self.assertEqual(result['porch']['extracted'],1)
            self.assertEqual(result['porch']['error'],0)

    def test_opencode_legacy_and_file_precedence(self):
        root = Path(self.tmp.name)
        storage = root / 'storage'
        for part in ('session/proj','message/file','part/m'):
            (storage / part).mkdir(parents=True)
        (storage / 'session/proj/file.json').write_text(json.dumps({'id':'file','title':'File'}))
        (storage / 'session/proj/dup.json').write_text(json.dumps({'id':'dup','title':'File duplicate'}))
        (storage / 'message/file/m.json').write_text(json.dumps({'id':'m','sessionID':'file','role':'user','time':{'created':1}}))
        part = storage / 'part/m/p.json'
        part.write_text(json.dumps({'id':'p','messageID':'m','sessionID':'file','type':'text','text':'first'}))
        db = root / 'opencode.db'
        with contextlib.closing(sqlite3.connect(db)) as conn, conn:
            conn.executescript('''CREATE TABLE session_v2(id TEXT,title TEXT,time_updated INTEGER,time_busy INTEGER,time_idle INTEGER);
                CREATE TABLE session_message(id TEXT,session_id TEXT,type TEXT,seq INTEGER,time_created INTEGER,time_updated INTEGER,data TEXT);
                CREATE TABLE session_part(message_id TEXT,session_id TEXT,ordinal INTEGER,type TEXT,time_updated INTEGER,data TEXT);
                CREATE TABLE session(id TEXT,title TEXT,time_created INTEGER,time_updated INTEGER);
                CREATE TABLE message(id TEXT,session_id TEXT,time_created INTEGER,time_updated INTEGER,data TEXT);
                CREATE TABLE part(id TEXT,message_id TEXT,session_id TEXT,time_created INTEGER,time_updated INTEGER,data TEXT);
                INSERT INTO session_v2 VALUES('dup','V2 wins',1,NULL,NULL);
                INSERT INTO session VALUES('dup','legacy dup',1,1);
                INSERT INTO session VALUES('legacy','legacy only',1,1);
                INSERT INTO message VALUES('lm','legacy',1,1,'{"id":"lm","role":"user","time":{"created":1}}');
                INSERT INTO part VALUES('lp','lm','legacy',1,1,'{"id":"lp","type":"text","text":"legacy prompt"}');''')
        stores = [sessions.Store('opencode','db',db,'sqlite'), sessions.Store('opencode','storage',storage,'json_dir')]
        with patch.object(sessions,'stores_for',return_value=stores):
            result = index.sync(self.directory,harnesses=['opencode'])
            self.assertEqual(result['opencode']['extracted'],3)
            with contextlib.closing(index.open_reader(self.directory)) as conn:
                self.assertEqual(dict(conn.execute('SELECT native_id,title FROM sessions')),
                                 {'dup':'V2 wins','legacy':'legacy only','file':'File'})
                self.assertIn(('legacy prompt',),conn.execute("SELECT text FROM docs WHERE session_key='opencode:legacy' AND kind='prompt'").fetchall())
            self.assertEqual(index.sync(self.directory,harnesses=['opencode'])['opencode']['extracted'],0)
            part.write_text(json.dumps({'id':'p','messageID':'m','sessionID':'file','type':'text','text':'changed'}))
            self.assertEqual(index.sync(self.directory,harnesses=['opencode'])['opencode']['extracted'],1)
            with contextlib.closing(index.open_reader(self.directory)) as conn:
                self.assertIn(('changed',), conn.execute("SELECT text FROM docs WHERE session_key='opencode:file' AND kind='prompt'").fetchall())

    def test_agy_entry_fingerprints_and_ide_do_not_open_db(self):
        root = Path(self.tmp.name) / 'agy'
        for part in ('cache','brain','annotations','conversations'):
            (root / part).mkdir(parents=True)
        meta = root / 'cache' / 'conversation_metadata.json'
        data = {'conversations':{'one':{'summary':{'Title':'One','NumSteps':0}},
                                 'two':{'summary':{'Title':'Two','NumSteps':0}},
                                 'ide':{'summary':{'Title':'IDE','AppDataDir':'antigravity'}}}}
        meta.write_text(json.dumps(data))
        transcript = root / 'brain' / 'one' / '.system_generated' / 'logs' / 'transcript_full.jsonl'
        transcript.parent.mkdir(parents=True)
        transcript.write_text(json.dumps({'source':'USER_EXPLICIT','type':'USER_INPUT','content':'hello','step_index':0})+'\n')
        (root / 'conversations' / 'ide.db').write_text('must not open')
        store = sessions.Store('agy','cli',root,'agy_root')
        original_open = Path.open
        def guarded(path, *args, **kwargs):
            if path.suffix == '.db':
                raise AssertionError('agy SQLite opened')
            return original_open(path, *args, **kwargs)
        with patch.object(sessions,'stores_for',return_value=[store]), patch.object(Path,'open',guarded):
            result = index.sync(self.directory,harnesses=['agy'])
            self.assertEqual(result['agy']['extracted'],3)
            with contextlib.closing(index.open_reader(self.directory)) as conn:
                self.assertEqual(conn.execute("SELECT content_state FROM sessions WHERE native_id='ide'").fetchone(), ('unavailable',))
            with transcript.open('a') as out:
                out.write(json.dumps({'source':'USER_EXPLICIT','type':'USER_INPUT','content':'next','step_index':1})+'\n')
            self.assertEqual(index.sync(self.directory,harnesses=['agy'])['agy']['extracted'],1)
            data['conversations']['two']['summary']['Title'] = 'Two changed'
            meta.write_text(json.dumps(data))
            self.assertEqual(index.sync(self.directory,harnesses=['agy'])['agy']['extracted'],1)
            data['conversations']['one']['summary']['UpdatedAt'] = '2099-01-01T00:00:00Z'
            meta.write_text(json.dumps(data))
            self.assertEqual(index.sync(self.directory,harnesses=['agy'])['agy']['extracted'],1)
            with contextlib.closing(index.open_reader(self.directory)) as conn:
                self.assertEqual(conn.execute("SELECT content_state FROM sessions WHERE native_id='one'").fetchone(), ('stale',))
            with patch.object(index.json,'load',side_effect=AssertionError('unchanged metadata read')):
                self.assertEqual(index.sync(self.directory,harnesses=['agy'])['agy']['extracted'],0)
            transcript.unlink()
            result = index.sync(self.directory,harnesses=['agy'])
            self.assertEqual(result['agy']['deleted'],0)
            with contextlib.closing(index.open_reader(self.directory)) as conn:
                self.assertEqual(conn.execute("SELECT content_state FROM sessions WHERE native_id='one'").fetchone(), ('unavailable',))
                self.assertEqual(conn.execute("SELECT count(*) FROM docs WHERE session_key='agy:one'").fetchone(), (0,))

    def test_combined_s4_first_build(self):
        root = Path(self.tmp.name)
        agy = root / 'agy'; (agy / 'cache').mkdir(parents=True)
        (agy / 'cache' / 'conversation_metadata.json').write_text(json.dumps({'conversations':{'ide':{'summary':{'Title':'IDE','AppDataDir':'antigravity'}}}}))
        storage = root / 'storage'; (storage / 'session' / 'proj').mkdir(parents=True)
        (storage / 'message' / 'file').mkdir(parents=True); (storage / 'part').mkdir()
        (storage / 'session' / 'proj' / 'file.json').write_text(json.dumps({'id':'file','title':'File'}))
        runs = root / 'runs'; (runs / 'r').mkdir(parents=True)
        (runs / 'r' / 'meta.json').write_text(json.dumps({'run_id':'r','status':'completed'}))
        (runs / 'r' / 'final.txt').write_text('answer')
        desktop = root / 'cowork'; (desktop / 'local_a').mkdir(parents=True)
        (desktop / 'local_a.json').write_text(json.dumps({'sessionId':'local_a'}))
        (desktop / 'local_a' / 'audit.jsonl').write_text(json.dumps({'type':'user','message':{'content':'hello'}})+'\n')
        stores = {'agy':[sessions.Store('agy','cli',agy,'agy_root')],
                  'opencode':[sessions.Store('opencode','storage',storage,'json_dir')],
                  'porch':[sessions.Store('porch','runs',runs,'run_registry')],
                  'claude-desktop':[sessions.Store('claude-desktop','cowork',desktop,'session_dirs')],
                  'claude-code':[]}
        with patch.object(sessions,'stores_for',side_effect=lambda h:stores[h]):
            result = index.sync(self.directory,harnesses=['agy','opencode','porch','claude-desktop'])
        self.assertTrue(result['complete'])
        with contextlib.closing(index.open_reader(self.directory)) as conn:
            self.assertEqual({row[0] for row in conn.execute('SELECT session_key FROM sessions')},
                             {'agy:ide','opencode:file','porch:r','claude-desktop:local_a'})

    def test_schema_and_privacy(self):
        with index.writer(self.directory) as conn:
            self.assertIsNotNone(conn)
            names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master")}
            self.assertTrue({'meta','sources','sessions','session_sources','aliases','edges','docs','fts'} <= names)
            versions = dict(conn.execute('SELECT key,value FROM meta'))
            self.assertEqual(versions['schema_version'], index.SCHEMA_VERSION)
            conn.execute("INSERT INTO fts(rowid,text) VALUES(1,?)", (index.normalize('Ёжик'),))
            self.assertEqual(conn.execute("SELECT rowid FROM fts WHERE fts MATCH ?", ('ежи',)).fetchall(), [(1,)])
            conn.commit()
            if os.name != 'nt':
                self.assertEqual(stat.S_IMODE(self.directory.stat().st_mode), 0o700)
                for path in self.directory.iterdir():
                    self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        with contextlib.closing(index.open_reader(self.directory)) as reader:
            with self.assertRaises(sqlite3.OperationalError):
                reader.execute("INSERT INTO meta VALUES('forbidden','yes')")
        self.assertTrue((self.directory / 'index.lock').exists())

    def test_lock_deadline_leaves_committed_snapshot(self):
        with index.writer(self.directory) as first:
            first.execute("INSERT INTO meta VALUES ('test','committed')")
            first.commit()
            with index.writer(self.directory, time.monotonic() + 0.05) as second:
                self.assertIsNone(second)
                reader = index.open_reader(self.directory)
                try:
                    self.assertEqual(reader.execute("SELECT value FROM meta WHERE key='test'").fetchone(), ('committed',))
                finally:
                    reader.close()

    def test_session_replacement_and_rollback(self):
        with index.writer(self.directory) as conn:
            session = {'session_key':'codex:one', 'harness':'codex', 'native_id':'one',
                       'store':'sessions', 'content_state':'ok'}
            first = [{'kind':'prompt','origin':'human','text':'Ёжик'}]
            index.replace_session(conn, session, first, [], [], [])
            self.assertEqual(conn.execute("SELECT rowid FROM fts WHERE fts MATCH 'ежи'").fetchall(), [(1,)])
            with self.assertRaises(sqlite3.IntegrityError):
                index.replace_session(conn, session, [{'kind':'invalid','text':'replacement'}], [], [], [])
            self.assertEqual(conn.execute('SELECT text FROM docs').fetchall(), [('Ёжик',)])
            self.assertEqual(conn.execute("SELECT rowid FROM fts WHERE fts MATCH 'ежи'").fetchall(), [(1,)])
            index.replace_session(conn, session, [{'kind':'answer','text':'Second answer'}], [], [], [])
            self.assertEqual(conn.execute('SELECT text FROM docs').fetchall(), [('Second answer',)])
            self.assertEqual(conn.execute("SELECT rowid FROM fts WHERE fts MATCH 'ежи'").fetchall(), [])
            self.assertEqual(conn.execute("SELECT count(*) FROM fts WHERE fts MATCH 'sec'").fetchone()[0], 1)
            conn.execute("UPDATE meta SET value='old' WHERE key='normalization_version'")
            conn.commit()
            with self.assertRaisesRegex(index.SessionIndexError, 'porch sessions index build'):
                index.replace_session(conn, session, first, [], [], [])

    def test_interpretation_only_indexes_prompts_and_completed_answers(self):
        ref = sessions.SessionRef('claude-code', 'projects', 'p/id', title='Title',
            locator={'file':'/tmp/source'}, extra={'native_id':'id','session_class':'unknown'})
        store = sessions.Store('claude-code', 'projects', Path('/tmp'), 'jsonl_dir')
        def f(kind, text, **kw):
            return {'kind':kind, 'text':text, 'locator':{'file':'/tmp/source'}, **kw}
        fragments = [f('prompt','Request',origin='human'),
                     f('prompt','Injected',origin='injected'),
                     f('assistant','Thinking',native={'message_id':'1','stop_reason':'tool_use'}),
                     f('assistant','Answer one',native={'message_id':'2','stop_reason':'end_turn'}),
                     f('assistant','Answer two',native={'message_id':'3','stop_reason':'end_turn'})]
        row, docs, aliases, edges = index.session_payload(store, ref, fragments, {'/tmp/source':'revision'})
        self.assertEqual([(d['kind'],d['text']) for d in docs], [('title','Title'),('prompt','Request'),('answer','Answer one'),('answer','Answer two')])
        self.assertTrue(all(d['source_rev']=='revision' for d in docs))
        self.assertEqual(row['class'], 'interactive')
        self.assertEqual(len(aliases),1)
        with index.writer(self.directory) as conn:
            index.replace_session(conn,row,docs,[],aliases,edges)
            self.assertEqual(conn.execute('SELECT count(*) FROM docs').fetchone(),(4,))

    def test_snapshot_detects_rewrite_replacement_and_disappearance(self):
        path = Path(self.tmp.name) / 'source.jsonl'
        for change in ('rewrite','replace','remove'):
            path.write_text('first')
            with index.FileSnapshot() as snapshot:
                snapshot.add(path)
                self.assertIn(str(path),snapshot.revisions())
                if change == 'rewrite':
                    path.write_text('longer')
                elif change == 'replace':
                    replacement = path.with_suffix('.new'); replacement.write_text('first')
                    os.replace(replacement,path)
                else:
                    path.unlink()
                with self.assertRaises(index.SourceChanged):
                    snapshot.verify()

    def test_extraction_rejects_malformed_or_changing_transcripts(self):
        root = Path(self.tmp.name) / 'store'; root.mkdir()
        path = root / 'one.jsonl'
        store = sessions.Store('claude-code','projects',root,'jsonl_dir')
        path.write_text('not-json\n')
        ref = sessions.claude_ref_for_file(store,path)
        with self.assertRaisesRegex(index.SessionIndexError,'incomplete'):
            index.extract_session(store,ref,{})
        path.write_bytes(b'{"type":"user","message":{"role":"user","content":"' + b'x' * 100000 + b'"}}\n')
        ref = sessions.claude_ref_for_file(store,path)
        original = sessions._bounded_chunks
        def changing(fh, size, chunk_size=64*1024):
            for i, chunk in enumerate(original(fh,size,chunk_size)):
                yield chunk
                if not i:
                    with path.open('ab') as out: out.write(b'{}\n')
        with patch.object(sessions,'_bounded_chunks',changing):
            with self.assertRaises(index.SourceChanged):
                index.extract_session(store,ref,{})







    def test_file_sync_extracts_updates_and_rejects_malformed(self):
        root = Path(self.tmp.name) / 'cc'; root.mkdir()
        path = root / 'one.jsonl'
        def write(text):
            path.write_text(json.dumps({'type':'user','message':{'role':'user','content':text}})+'\n')
        write('first')
        store = sessions.Store('claude-code','projects',root,'jsonl_dir')
        with patch.object(sessions, 'stores_for', side_effect=lambda h: [store] if h == 'claude-code' else []):
            self.assertEqual(index.sync(self.directory)['claude-code']['extracted'], 1)
            write('second')
            self.assertEqual(index.sync(self.directory)['claude-code']['extracted'], 1)
            path.write_text('bad JSON\n')
            self.assertEqual(index.sync(self.directory)['claude-code']['error'], 1)
        with contextlib.closing(index.open_reader(self.directory)) as conn:
            self.assertEqual(conn.execute("SELECT text FROM docs WHERE kind='prompt'").fetchall(), [('second',)])
            self.assertEqual(conn.execute('SELECT state FROM sources').fetchone(),('error',))



    def test_reader_does_not_checkpoint_or_modify_database(self):
        db = self.directory / 'index-v1.sqlite'
        with index.writer(self.directory) as conn:
            conn.execute("INSERT INTO meta VALUES('reader-test','committed')")
            conn.commit()
        self.assertFalse(Path(str(db) + '-wal').exists())
        def db_state():
            return (db.stat().st_mtime_ns, hashlib.sha256(db.read_bytes()).digest())
        initial = db_state()
        reader = index.open_reader(self.directory)
        try:
            self.assertEqual(reader.execute("SELECT value FROM meta WHERE key='reader-test'").fetchone(), ('committed',))
        finally:
            reader.close()
        self.assertEqual(db_state(), initial)
        with index.writer(self.directory) as writer:
            writer.execute('PRAGMA wal_autocheckpoint=0')
            writer.execute("INSERT INTO meta VALUES('wal-only','not checkpointed')")
            writer.commit()
            writer.execute('BEGIN IMMEDIATE')
            writer.execute("UPDATE meta SET value='uncommitted' WHERE key='reader-test'")
            before = db_state()
            reader = index.open_reader(self.directory)
            try:
                self.assertEqual(reader.execute("SELECT value FROM meta WHERE key='reader-test'").fetchone(), ('committed',))
                self.assertEqual(reader.execute("SELECT value FROM meta WHERE key='wal-only'").fetchone(), ('not checkpointed',))
                self.assertEqual(db_state(), before)
            finally:
                reader.close()
            self.assertEqual(db_state(), before)
            writer.rollback()

    def test_incompatible_schema_fails(self):
        with index.writer(self.directory) as conn:
            conn.execute("UPDATE meta SET value='999' WHERE key='schema_version'")
            conn.commit()
        with self.assertRaisesRegex(index.SessionIndexError, 'unsupported session index schema'):
            with index.writer(self.directory):
                pass


@unittest.skipUnless(index.sqlite_capability()[0], index.sqlite_capability()[1])
class IndexedFindTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.directory = self.root / 'index'

    def test_randomized_find_matches_linear_scan(self):
        rng = random.Random(314159)
        words = ['Ёжик','ежик','КОт','нашёл','найти','ABC','a|b','x"y','точка.','a b','яя','q', '😀']
        corpus = []
        with index.writer(self.directory) as conn:
            for n in range(50):
                harness = rng.choice(['codex','claude-code'])
                key = f'{harness}:{n:02d}'
                cls = rng.choice(['interactive','headless'])
                cwd = rng.choice(['/work/one','/work/two'])
                updated = rng.choice(['2026-01-01T00:00:00Z','2026-03-01T00:00:00Z'])
                session = {'session_key':key,'harness':harness,'native_id':str(n),'store':'fixture',
                           'class':cls,'cwd':cwd,'updated':updated,'content_state':'ok'}
                docs = []
                for _ in range(rng.randrange(1,5)):
                    text = ' '.join(rng.choices(words,k=rng.randrange(1,5)))
                    kind = rng.choice(['title','prompt','answer'])
                    origin = rng.choice(['human','delegated','unknown']) if kind == 'prompt' else None
                    docs.append({'kind':kind,'origin':origin,'text':text})
                index.replace_session(conn, session, docs, [], [], [])
                corpus.append((key,cls,cwd,docs,harness,sessions.ts_epoch(updated)))
            samples = ['еж','кот','нашел','найти|abc',r'a\|b','x"y','.', ' ', 'q', '😀', 'точка.', 'a b', 'ёжик']
            for _ in range(120):
                groups = rng.sample(samples,rng.randrange(1,4))
                kind = rng.choice([None,'prompt','answer','title'])
                origin = rng.choice([None,'human','delegated'])
                cls = rng.choice([None,'interactive','headless'])
                cwd = rng.choice([None,'one','two'])
                harnesses = rng.choice([None,['codex'],['claude-code']])
                since = rng.choice([None,sessions.parse_time_bound('2026-02-01T00:00:00Z')])
                until = rng.choice([None,sessions.parse_time_bound('2026-02-01T00:00:00Z')])
                parsed = index.parse_groups(groups)
                expected = set()
                for key, cl, path, docs, harness, updated in corpus:
                    if (cls and cls != cl or cwd and cwd not in path or harnesses and harness not in harnesses
                            or since is not None and updated < since or until is not None and updated > until):
                        continue
                    filtered = [d for d in docs if (not kind or d['kind']==kind) and (not origin or d['origin']==origin)]
                    if all(any(any(index.normalize(alt) in index.normalize(d['text']) for alt in group) for d in filtered) for group in parsed):
                        expected.add(key)
                found, summary = index.find_in_snapshot(conn, groups, kind=kind,origin=origin,session_class=cls,cwd=cwd,
                                                        harnesses=harnesses,since=since,until=until,limit=0)
                self.assertEqual({row['ref'] for row in found}, expected, (groups,kind,origin,cls,cwd))
                self.assertEqual(summary['matched_sessions'],len(expected))
                for group_no, alternatives in enumerate(parsed):
                    for alt_no, alternative in enumerate(alternatives):
                        count = sum(1 for _,cl,path,docs,harness,updated in corpus if (not cls or cls == cl) and (not cwd or cwd in path)
                                    and (not harnesses or harness in harnesses) and (since is None or updated >= since)
                                    and (until is None or updated <= until)
                                    and any((not kind or d['kind'] == kind) and (not origin or d['origin'] == origin)
                                            and index.normalize(alternative) in index.normalize(d['text']) for d in docs))
                        self.assertEqual(summary['alternatives'][group_no][alt_no]['sessions'], count)

    def test_ranking_and_duplicate_evidence(self):
        with index.writer(self.directory) as conn:
            for key, updated, docs in [('one','2026-01-01T00:00:00Z',[('answer','alpha beta'),('answer','alpha beta')]),
                                       ('two','2026-02-01T00:00:00Z',[('title','alpha'),('answer','beta')]),
                                       ('three','2026-03-01T00:00:00Z',[('prompt','alpha beta')])]:
                index.replace_session(conn, {'session_key':'codex:'+key,'harness':'codex','native_id':key,'store':'fixture',
                    'updated':updated,'content_state':'ok'}, [{'kind':k,'text':t} for k,t in docs], [], [], [])
            rows, summary = index.find_in_snapshot(conn,['alpha','beta'],limit=0)
            self.assertEqual([r['native_id'] for r in rows], ['three','one','two'])
            self.assertEqual(rows[1]['evidence']['alpha']['count'],2)
            self.assertEqual(summary['matched_sessions'],3)
            for bad in ('|a','a|','a||b'):
                with self.assertRaisesRegex(ValueError,'empty alternative'):
                    index.find_in_snapshot(conn,[bad])

    def test_show_at_revision_and_stable_message_anchor(self):
        root = self.root / 'projects'; root.mkdir()
        path = root / 'one.jsonl'
        record = {'type':'user','uuid':'msg-one','message':{'role':'user','content':'hello world'}}
        path.write_text(json.dumps(record)+'\n')
        store = sessions.Store('claude-code','projects',root,'jsonl_dir')
        with patch.object(sessions,'stores_for',side_effect=lambda h:[store] if h=='claude-code' else []):
            index.sync(self.directory,harnesses=['claude-code'])
            with contextlib.closing(index.open_reader(self.directory)) as conn:
                rows,_ = index.find_in_snapshot(conn,['hello'])
            locator = rows[0]['evidence']['hello']['locator']
            args = argparse.Namespace(at=locator,max_chars=0)
            with patch.object(sessions,'emit') as emit:
                self.assertEqual(index.cmd_show_at(args),0)
                self.assertTrue(any(call.args[0].get('text')=='hello world' for call in emit.call_args_list))
            path.write_text('{}\n'+json.dumps(record)+'\n')
            with patch.object(sessions,'emit') as emit:
                self.assertEqual(index.cmd_show_at(args),0)
                self.assertTrue(emit.call_args_list[-1].args[0]['source_changed'])
            address = json.loads(locator)
            address['anchor'].pop('message_anchor')
            args.at = json.dumps(address)
            with self.assertRaisesRegex(index.SessionIndexError,'source changed.*search again'):
                index.cmd_show_at(args)

    def test_desktop_title_evidence_points_to_manifest(self):
        root = self.root / 'projects'; root.mkdir()
        desk = self.root / 'desktop'; desk.mkdir()
        (root/'one.jsonl').write_text('{"type":"user","message":{"role":"user","content":"hello"}}\n')
        manifest = desk/'local_a.json'
        manifest.write_text(json.dumps({'sessionId':'desktop','cliSessionId':'one','title':'desktop title'}))
        stores = {'claude-code':[sessions.Store('claude-code','projects',root,'jsonl_dir')],
                  'claude-desktop':[sessions.Store('claude-desktop','code-sessions',desk,'json_dir')]}
        with patch.object(sessions,'stores_for',side_effect=lambda h:stores.get(h,[])):
            index.sync(self.directory,harnesses=['claude-code'])
            with contextlib.closing(index.open_reader(self.directory)) as conn:
                rows,_ = index.find_in_snapshot(conn,['desktop title'])
            locator = rows[0]['evidence']['desktop title']['locator']
            self.assertEqual(json.loads(locator)['anchor']['file'],str(manifest))
            with patch.object(sessions,'emit') as emit:
                index.cmd_show_at(argparse.Namespace(at=locator,max_chars=0))
                self.assertTrue(any(call.args[0].get('text')=='desktop title' for call in emit.call_args_list))

    def test_porch_and_agy_title_sources_are_navigable(self):
        runs = self.root/'runs'; run = runs/'r'; run.mkdir(parents=True)
        (run/'meta.json').write_text(json.dumps({'run_id':'r','task':'porch unique title'}))
        (run/'final.txt').write_text('answer')
        agy = self.root/'agy'
        for part in ('cache','brain','annotations','conversations'):
            (agy/part).mkdir(parents=True)
        metadata = agy/'cache'/'conversation_metadata.json'
        metadata.write_text(json.dumps({'conversations':{'one':{'summary':{'Title':'agy unique title'}}}}))
        transcript = agy/'brain'/'one'/'.system_generated'/'logs'/'transcript_full.jsonl'
        transcript.parent.mkdir(parents=True)
        transcript.write_text(json.dumps({'source':'USER_EXPLICIT','type':'USER_INPUT','content':'hello','step_index':0})+'\n')
        stores = {'porch':[sessions.Store('porch','runs',runs,'run_registry')],
                  'agy':[sessions.Store('agy','cli',agy,'agy_root')]}
        with patch.object(sessions,'stores_for',side_effect=lambda h:stores.get(h,[])):
            index.sync(self.directory,harnesses=['porch','agy'])
            with contextlib.closing(index.open_reader(self.directory)) as conn:
                for text,path in [('porch unique title',run/'meta.json'),('agy unique title',metadata)]:
                    rows,_ = index.find_in_snapshot(conn,[text])
                    locator = rows[0]['evidence'][text]['locator']
                    self.assertEqual(json.loads(locator)['anchor']['file'],str(path))
                    with patch.object(sessions,'emit') as emit:
                        index.cmd_show_at(argparse.Namespace(at=locator,max_chars=0))
                        self.assertTrue(any(call.args[0].get('text')==text for call in emit.call_args_list))

    def test_sqlite_message_anchor_survives_database_change(self):
        db = self.root/'opencode.db'
        with contextlib.closing(sqlite3.connect(db)) as conn, conn:
            conn.executescript('''CREATE TABLE session_v2(id TEXT,title TEXT,time_updated INTEGER,time_busy INTEGER,time_idle INTEGER);
                CREATE TABLE session_message(id TEXT,session_id TEXT,type TEXT,seq INTEGER,time_created INTEGER,time_updated INTEGER,data TEXT);
                CREATE TABLE session_part(message_id TEXT,session_id TEXT,ordinal INTEGER,type TEXT,time_updated INTEGER,data TEXT);
                INSERT INTO session_v2 VALUES('s','Database title',1,NULL,NULL);
                INSERT INTO session_message VALUES('m','s','user',1,1,1,'{"text":"unique prompt"}');''')
        store = sessions.Store('opencode','db',db,'sqlite')
        with patch.object(sessions,'stores_for',side_effect=lambda h:[store] if h=='opencode' else []):
            index.sync(self.directory,harnesses=['opencode'])
            with contextlib.closing(index.open_reader(self.directory)) as conn:
                prompt,_ = index.find_in_snapshot(conn,['unique prompt'])
                title,_ = index.find_in_snapshot(conn,['Database title'])
            with patch.object(sessions,'emit') as emit:
                index.cmd_show_at(argparse.Namespace(at=title[0]['evidence']['Database title']['locator'],max_chars=0))
                self.assertTrue(any(c.args[0].get('text')=='Database title' for c in emit.call_args_list))
            with contextlib.closing(sqlite3.connect(db)) as conn, conn:
                conn.execute("UPDATE session_message SET time_updated=2 WHERE id='m'")
            with patch.object(sessions,'emit') as emit:
                index.cmd_show_at(argparse.Namespace(at=prompt[0]['evidence']['unique prompt']['locator'],max_chars=0))
                self.assertTrue(emit.call_args_list[-1].args[0]['source_changed'])
                self.assertTrue(any(c.args[0].get('text')=='unique prompt' for c in emit.call_args_list))

    def test_busy_lock_snapshot_and_normalization_refusal(self):
        root = self.root / 'projects'; root.mkdir()
        (root/'one.jsonl').write_text('{"type":"user","message":{"role":"user","content":"needle"}}\n')
        store = sessions.Store('claude-code','projects',root,'jsonl_dir')
        args = argparse.Namespace(groups=['needle'],deadline=0,agent='claude-code',kind=None,origin=None,
                                  session_class=None,cwd=None,since=None,until=None,limit=20)
        with patch.object(sessions,'stores_for',side_effect=lambda h:[store] if h=='claude-code' else []), patch.object(index,'index_dir',return_value=self.directory):
            index.sync(self.directory,harnesses=['claude-code'])
            with index.writer(self.directory), patch.object(sessions,'emit') as emit:
                self.assertEqual(index.cmd_find(args),0)
                self.assertFalse(emit.call_args_list[-1].args[0]['coverage']['synced'])
                self.assertFalse(emit.call_args_list[-1].args[0]['coverage']['complete'])
                self.assertFalse(emit.call_args_list[-1].args[0]['coverage']['stat_pass'])
                self.assertIsNotNone(emit.call_args_list[-1].args[0]['coverage']['stale_since'])
                self.assertEqual(emit.call_args_list[0].args[0]['native_id'],'one')
            with index.writer(self.directory) as conn:
                conn.execute("UPDATE meta SET value='old' WHERE key='normalization_version'")
                conn.commit()
            with self.assertRaisesRegex(index.SessionIndexError,'run porch sessions index build'):
                index.cmd_find(args)


if __name__ == '__main__':
    unittest.main(verbosity=2)
