"""Fixture-only tests of session-index storage primitives."""
from __future__ import annotations

import os
import contextlib
import hashlib
import io
import json
import random
import shlex
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
import sessions_facts
import sessions_trace
import sessions_verdicts


class CapabilityTests(unittest.TestCase):
    def test_detached_launch_output_recognizes_only_porch_owned_shapes(self):
        for output in ('[porch] stage=delegate detached run_id=run_one',
                       'run_id=run_one', '[porch] stage=delegate detached run_id=run_one\nrun_id=run_one'):
            collector=sessions_facts.FactCollector()
            rows=[{'kind':'tool_call','text':json.dumps({'name':'Bash','input':{'command':'porch delegate -a x --detach'}}),
                   'rel':{'call_id':'c','tool':'Bash'}},
                  {'kind':'tool_result','text':output,'rel':{'call_id':'c'}}]
            list(collector.stream(rows))
            calls=[f for f in collector.facts() if f['kind']=='porch_call']
            self.assertEqual([(f['target'],f['data']['op'],f['data']['detach']) for f in calls], [('run_one','delegate',True)])
        self.assertEqual(sessions_facts._output_targets('answer mentions run_id=run_fake',False,True),[])
        self.assertEqual(sessions_facts._output_targets('run_id=run_one',False,False),[])

    def test_porch_call_reader_targets_and_background_bounds(self):
        collector=sessions_facts.FactCollector()
        def pair(n,command,output='',tool='Bash',background=False):
            args={'file_path':command} if tool=='Read' else {'command':command,'run_in_background':background}
            return [{'kind':'tool_call','text':json.dumps({'name':tool,'input':args}),'ts':str(n),'rel':{'call_id':str(n),'tool':tool}},
                    {'kind':'tool_result','text':output,'ts':str(n+1),'rel':{'call_id':str(n)}}]
        rows=[]
        for n,command in enumerate(('ls /tmp/run_a/final.txt','test -f /tmp/run_a/final.txt',
                                    'stat /tmp/run_a/final.txt','grep -l foo */final.txt'),1):
            rows+=pair(n*10,command)
        for n,command in enumerate(('cat /tmp/run_a/final.txt','head /tmp/run_a/final.txt',
                                    'python -c "open(\x27/tmp/run_a/final.txt\x27).read()"'),10):
            rows+=pair(n*10,command,'run_b')
        rows+=pair(200,'porch delegate wait run_a','answer mentions run_b\n[porch] done run_id=run_c')
        rows+=pair(210,'porch delegate wait "$RID" --json','{"run_id":"run_json"}')
        rows+=pair(212,'porch delegate wait "$RID" --json','{\n "run_id": "run_large",\n "final_text": "'+('x'*1100000)+'"\n}')
        rows+=pair(220,'porch delegate wait "$RID"','plain run_other and run_id=run_fake')
        rows+=pair(225,'porch delegate wait "$RID"','{"run_id":"run_answer"}')
        rows+=pair(226,'porch delegate wait "$RID"','[porch] wait run_id=run_progress attached')
        rows+=pair(230,'porch delegate wait run_bg','running in background with ID: 3',background=True)
        rows+=pair(240,'head -n 3 /tmp/other','unrelated')
        rows+=pair(250,'porch delegate wait run_bg','running in background with ID: task123',background=True)
        rows+=pair(260,'TaskOutput task123','[porch] done run_id=run_bg',tool='TaskOutput')
        list(collector.stream(rows)); calls=[f for f in collector.facts() if f['kind']=='porch_call']
        self.assertEqual(sum(f['data']['op']=='other' for f in calls),4)
        self.assertEqual(sum(f['data']['op']=='read-final' for f in calls),3)
        self.assertFalse(any(f['target']=='run_b' for f in calls))
        self.assertFalse(any(f['target']=='run_fake' for f in calls))
        self.assertFalse(any(f['target']=='run_answer' for f in calls))
        self.assertTrue(any(f['target']=='run_c' and f['data']['op']=='wait' for f in calls))
        self.assertTrue(any(f['target']=='run_json' for f in calls))
        self.assertTrue(any(f['target']=='run_large' for f in calls))
        self.assertTrue(any(f['target']=='run_progress' for f in calls))
        self.assertTrue(any(f['target'] is None and f['data']['op']=='wait' for f in calls))
        self.assertFalse(any(f['data'].get('via')=='background_output' and f['ts']=='241' for f in calls))
        self.assertTrue(any(f['data'].get('via')=='background_output' and f['target']=='run_bg' for f in calls))
        for n in range(1100):
            collector.pending[str(n)]={'op':'wait','targets':[],'background':True}
            collector._result({'text':f'running in background with ID: task{n:05}','rel':{'call_id':str(n)}})
        self.assertLessEqual(len(collector.background),1000)

    def test_verdict_naive_timestamps_use_utc(self):
        self.assertEqual(sessions_verdicts._epoch('2025-01-01T00:00:00'),sessions_verdicts._epoch('2025-01-01T00:00:00Z'))

    def test_codex_completed_task_after_abort_clears_last_abort(self):
        ref=sessions.SessionRef('codex','sessions','fixture')
        prompt={'kind':'prompt','native_type':'event_msg.user_message','text':'question','ts':'1'}
        abort={'kind':'boundary','native_type':'event_msg.turn_aborted','native':{'reason':'cancel'},'ts':'2'}
        start={'kind':'boundary','native_type':'event_msg.task_started','ts':'3'}
        final={'kind':'assistant','native_type':'event_msg.agent_message','text':'answer','ts':'4',
               'native':{'phase':'final_answer','layer':'event'}}
        complete={'kind':'boundary','native_type':'event_msg.task_complete','ts':'5'}
        turns=sessions._codex_turns(ref,[prompt,abort,start,final,complete])
        self.assertFalse(turns[-1]['aborted'])
        self.assertFalse(turns[-1]['open'])
        self.assertEqual(turns[-1]['final']['text'],'answer')
        turns=sessions._codex_turns(ref,[prompt,abort,start,final])
        self.assertFalse(turns[-1]['aborted'])
        turns=sessions._codex_turns(ref,[prompt,final,complete,abort])
        self.assertTrue(turns[-1]['aborted'])

    def test_codex_parent_metadata_layouts_deduplicate_state_edge(self):
        for source in ({'subagent': {'thread_spawn': {'parent_thread_id':'parent'}}}, {'subagent': {}}):
            meta={'source':source,'parent_thread_id':'parent'}
            ref=sessions.SessionRef('codex','sessions','child')
            with patch.object(sessions,'porch_workers',return_value=set()):
                sessions._codex_enrich_ref(ref,Path('/fixture/rollout.jsonl'),meta=meta,
                                           state_parent='parent',state_db=Path('/fixture/state.sqlite'))
            edges=ref.extra['edges']
            self.assertEqual([(edge['type'],edge['target_id']) for edge in edges],[('subagent','parent')])
            self.assertEqual(edges[0]['basis'], 'thread_spawn.parent_thread_id' if source['subagent'].get('thread_spawn') else 'session_meta.parent_thread_id')
            self.assertEqual(ref.extra['session_class'],'subagent')

    def test_trace_last_turn_harness_states(self):
        prompt={'kind':'prompt','origin':'human','text':'question','ts':'1','locator':{'line':1}}
        claude_answer={'kind':'assistant','text':'answer','ts':'2','locator':{'line':2},'native':{'message_id':'m','stop_reason':'end_turn'}}
        codex_answer={'kind':'assistant','text':'answer','ts':'2','locator':{'line':2},'native':{'phase':'final_answer','layer':'response'}}
        devin_prompt={**prompt,'native_type':'message_nodes.user','seq':1,'native':{'node_id':1,'canonical':True}}
        devin_answer={'kind':'assistant','text':'answer','ts':'2','seq':2,'native_type':'message_nodes.assistant',
                      'native':{'node_id':2,'canonical':True,'finish_reason':'stop','has_tool_calls':False}}
        cases=[('claude-code',[prompt],'open'),('claude-code',[prompt,claude_answer],'closed'),
               ('claude-code',[prompt,{'kind':'context','interrupted':True,'ts':'3'}],'aborted'),
               ('codex',[prompt],'open'),('codex',[prompt,codex_answer,{'kind':'boundary','native_type':'event_msg.task_complete'}],'closed'),
               ('codex',[prompt,{'kind':'boundary','native_type':'event_msg.turn_aborted','native':{'reason':'interrupted'},'ts':'3'}],'aborted'),
               ('devin',[devin_prompt],'open'),('devin',[devin_prompt,devin_answer],'closed')]
        for harness,fragments,expected in cases:
            with self.subTest(harness=harness,state=expected):
                store=sessions.Store(harness,'test',Path('/fixture'),'sqlite' if harness=='devin' else 'jsonl_dir')
                ref=sessions.SessionRef(harness,'test','id')
                row,*_=index.session_payload(store,ref,fragments,{})
                self.assertEqual(row['_facts'][0]['data']['state'],expected)
        store=sessions.Store('devin','test',Path('/fixture'),'sqlite')
        ref=sessions.SessionRef('devin','test','id')
        native_turn={'prompts':[prompt],'final':None,'finals':[],'open':False,'aborted':True,
                     'abort_reason':'native cancellation','ts_start':'1','ts_end':'3'}
        with patch.object(sessions,'interpret_session',return_value=[native_turn]):
            row,*_=index.session_payload(store,ref,[],{})
        self.assertEqual(row['_facts'][0]['data']['state'],'aborted')
        self.assertEqual(row['_facts'][0]['data']['abort_reason'],'native cancellation')

    def test_trace_tool_facts_and_bounds(self):
        collector=sessions_facts.FactCollector()
        def call(n, tool, args):
            return {'kind':'tool_call','text':json.dumps({'name':tool,'input':args}), 'ts':str(n),
                    'locator':{'line':n},'rel':{'call_id':str(n),'tool':tool}}
        def result(n,text,error=False):
            return {'kind':'tool_result','text':text,'ts':str(n+1),'locator':{'line':n+1},
                    'rel':{'call_id':str(n)},'error':error}
        rows=[call(1,'Bash',{'command':'"$PORCH" delegate wait "$RID"'}),result(1,'SECRET\n[porch] done run_id=run_abc'),
              call(3,'Bash',{'command':'porch delegate wait run_a --quiet'}),result(3,'done'),
              call(5,'Bash',{'command':'porch delegate wait-any run_a run_b'}),result(5,'done'),
              call(7,'Bash',{'command':'porch delegate status run_a --json'}),result(7,'done'),
              call(9,'Bash',{'command':'porch delegate events run_a'}),result(9,'done',True),
              call(11,'Bash',{'command':'porch delegate -a x --detach --name x'}),result(11,'[porch] stage=delegate detached run_id=run_new\nrun_id=run_new'),
              call(13,'Bash',{'command':'porch delegate wait run_bg','run_in_background':True}),result(13,'Command running in background with ID: task123. Output is being written to: /tmp/task.out'),
              call(15,'Read',{'file_path':'/tmp/task.out'}),result(15,'done run_bg'),
              call(17,'Read',{'file_path':'/tmp/run-delegate-foo/final.txt'}),result(17,'answer'),
              call(19,'Bash',{'command':'cat "$ART/final.txt"'}),result(19,'answer'),
              call(21,'mcp__ccd_turn__wrap_up',{'recap':'x'*1200}),
              call(23,'mcp__ccd_turn__update_plan',{'items':[{'text':str(i)*300,'status':'in_progress' if i==0 else 'pending'} for i in range(60)]}),
              call(25,'mcp__ccd_turn__update_plan',{'advance':True}),
              call(27,'mcp__ccd_turn__update_plan',{'done':[2],'start':3}),
              call(29,'ExitPlanMode',{'plan':'first'}), call(31,'ExitPlanMode',{'plan':'last'})]
        consumed=list(collector.stream(rows)); facts=collector.facts()
        self.assertTrue(all('text' not in f for f in consumed if f['kind']=='tool_result'))
        calls=[f for f in facts if f['kind']=='porch_call']
        self.assertTrue(any(f['target']=='run_abc' and f['data']['op']=='wait' for f in calls))
        self.assertFalse(any(f['target']=='run_id' for f in calls))
        self.assertTrue(any(f['data']['quiet'] for f in calls))
        self.assertTrue(any(f['data']['op']=='wait-any' for f in calls))
        self.assertTrue(any(f['data']['op']=='status' and f['data']['json'] for f in calls))
        self.assertTrue(any(f['data']['op']=='events' and f['data']['is_error'] for f in calls))
        self.assertTrue(any(f['target']=='run_new' and f['data']['detach'] for f in calls))
        self.assertTrue(any(f['target']=='run_bg' and f['data'].get('via')=='background_output' for f in calls))
        self.assertTrue(any(f['target']=='run-delegate-foo' and f['data']['op']=='read-final' for f in calls))
        self.assertTrue(any(f['target'] is None and f['data']['op']=='read-final' for f in calls))
        status={f['target']:f for f in facts if f['kind']=='status_text'}
        self.assertEqual(len(status['wrap_up']['short_text']),1000)
        items=status['plan_items']['data']['items']
        self.assertEqual(len(items),50); self.assertLessEqual(len(items[0]['text']),200)
        self.assertEqual([item['status'] for item in items[:3]],['completed','completed','in_progress'])
        self.assertEqual(status['plan']['short_text'],'last')

    def test_trace_codex_tools_and_todowrite(self):
        collector=sessions_facts.FactCollector()
        def call(n,name,args):
            return {'kind':'tool_call','text':json.dumps({'name':name,'arguments':json.dumps(args)}),
                    'rel':{'call_id':str(n),'tool':name},'ts':str(n),'locator':{'line':n}}
        fragments=[call(1,'exec_command',{'cmd':'porch delegate wait run_x'}),
                   {'kind':'tool_result','text':'hidden output','rel':{'call_id':'1'},'ts':'2'},
                   call(3,'update_plan',{'plan':[{'step':'step','status':'in_progress'}]}),
                   call(5,'TodoWrite',{'todos':[{'content':'todo','status':'pending'}]})]
        list(collector.stream(fragments)); facts=collector.facts()
        self.assertTrue(any(f['target']=='run_x' and f['data']['op']=='wait' for f in facts))
        self.assertEqual(next(f for f in facts if f['kind']=='status_text')['data']['items'],[{'text':'todo','status':'pending'}])

    def test_wrap_up_closes_only_in_progress_plan_item(self):
        collector = sessions_facts.FactCollector()
        def call(name, args, ts):
            return {'kind':'tool_call', 'text':json.dumps({'name':name,'input':args}), 'ts':ts,
                    'locator':{'line':int(ts)}, 'rel':{'tool':name}}
        fragments = [call('mcp__ccd_turn__update_plan', {'items':[{'text':'one','status':'in_progress'},
                                                                   {'text':'two','status':'pending'}]}, '1'),
                     call('mcp__ccd_turn__wrap_up', {'recap':'done'}, '2')]
        list(collector.stream(fragments))
        fact = next(f for f in collector.facts() if f.get('target') == 'plan_items')
        self.assertEqual(fact['data'], {'items':[{'text':'one','status':'completed'},
                                                {'text':'two','status':'pending'}], 'closed_by':'wrap_up'})
        self.assertEqual(fact['locator'], {'line':2})
        self.assertEqual(fact['ts'], '2')
        collector = sessions_facts.FactCollector()
        list(collector.stream([call('mcp__ccd_turn__update_plan', {'items':[{'text':'later','status':'pending'}]}, '3'),
                               call('mcp__ccd_turn__wrap_up', {'recap':'done'}, '4')]))
        fact = next(f for f in collector.facts() if f.get('target') == 'plan_items')
        self.assertEqual(fact['data'], {'items':[{'text':'later','status':'pending'}], 'closed_by':'wrap_up'})
        collector = sessions_facts.FactCollector()
        list(collector.stream([call('TodoWrite', {'todos':[{'content':'other','status':'in_progress'}]}, '3'),
                               call('mcp__ccd_turn__wrap_up', {'recap':'done'}, '4')]))
        fact = next(f for f in collector.facts() if f.get('target') == 'plan_items')
        self.assertEqual(fact['data']['items'][0]['status'], 'in_progress')
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
        compact = list(sessions_facts.FactCollector().stream([dict(f) for f in fragments]))
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

    def test_porch_task_fact_and_worker_fallback(self):
        root=Path(self.tmp.name)/'runs'; run=root/'run_brief'; run.mkdir(parents=True)
        contract=(Path(index.__file__).resolve().parents[2]/'prompts'/'delegate-executor.txt').read_text()
        (run/'meta.json').write_text(json.dumps({'run_id':'run_brief','task':contract+'Build the fixture\nDetails',
                                                 'started_at':'2025-01-01T00:00:00Z','status':'running'}))
        (run/'normalized').mkdir(); (run/'normalized'/'events.jsonl').write_text('{"type":"run_started"}\n')
        store=sessions.Store('porch','runs',root,'run_registry')
        with patch.object(sessions,'stores_for',side_effect=lambda h:[store] if h=='porch' else []):
            index.sync(self.directory,harnesses=['porch'])
            with contextlib.closing(index.open_reader(self.directory)) as conn:
                fact=conn.execute("SELECT short_text,ts FROM facts WHERE session_key='porch:run_brief' AND target='task'").fetchone()
                self.assertEqual(fact,('Build the fixture\nDetails','2025-01-01T00:00:00Z'))
                self.assertEqual(conn.execute("SELECT title FROM sessions WHERE session_key='porch:run_brief'").fetchone()[0], 'Build the fixture')
                self.assertEqual(conn.execute("SELECT count(*) FROM docs WHERE session_key='porch:run_brief' AND kind='prompt'").fetchone()[0],0)
                graph=sessions_trace.trace_snapshot(conn,['porch:run_brief'],{'synced':True,'discovery_complete':True})
                self.assertEqual(graph['nodes'][0]['task']['snippet'],'Build the fixture\nDetails')
        with index.writer(self.directory) as conn:
            index.replace_session(conn,{'session_key':'codex:worker','harness':'codex','native_id':'worker','store':'test','content_state':'ok'},
                [{'kind':'prompt','origin':'delegated','text':contract+'Worker task','ts':'later','locator_json':'{}'}],[],[],[])
            index.replace_session(conn,{'session_key':'porch:run_empty','harness':'porch','native_id':'run_empty','store':'runs','content_state':'ok'},[],[],[],
                [{'type':'worker_session','target_key':'codex:worker','basis':'native'}])
            graph=sessions_trace.trace_snapshot(conn,['porch:run_empty'],{'synced':True,'discovery_complete':True})
            self.assertEqual(next(n for n in graph['nodes'] if n['key']=='porch:run_empty')['task']['snippet'],'Worker task')

    def test_cursor_cli_chat_is_indexed_and_found(self):
        sid='aaaaaaaa-1111-2222-3333-444444444444'
        root=Path(self.tmp.name)/'chats'
        chat=root/'0123456789abcdef0123456789abcdef'/sid; chat.mkdir(parents=True)
        (chat/'meta.json').write_text(json.dumps({'schemaVersion':1,'createdAtMs':0,
            'hasConversation':True,'cwd':'/work/cursor-cli'}))
        def field(n,data): return bytes([n*8+2,len(data)])+data
        mid=bytes([1])*32; rid=bytes([2])*32
        conn=sqlite3.connect(chat/'store.db')
        conn.executescript('CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);'
                           'CREATE TABLE blobs (id TEXT PRIMARY KEY, data BLOB);')
        conn.execute("INSERT INTO meta VALUES ('0', ?)",
                     (json.dumps({'agentId':sid,'latestRootBlobId':rid.hex()}).encode().hex(),))
        conn.executemany('INSERT INTO blobs VALUES (?, ?)',
            [(mid.hex(),json.dumps({'role':'user','content':'<user_query>cursor needle phrase</user_query>'}).encode()),
             (rid.hex(),field(1,mid))])
        conn.commit(); conn.close()
        store=sessions.Store('cursor','chats',root,'session_dirs')
        with patch.object(sessions,'stores_for',side_effect=lambda h:[store] if h=='cursor' else []):
            result=index.sync(self.directory,harnesses=['cursor'])
        self.assertEqual(result['cursor']['extracted'],1)
        with contextlib.closing(index.open_reader(self.directory)) as conn:
            hits,_=index.find_in_snapshot(conn,['cursor needle phrase'],kind='prompt')
            self.assertEqual(len(hits),1)
            self.assertIn(sid,str(hits[0]))

    def test_prefixed_native_title_uses_prompt_and_does_not_index_prefix_as_title(self):
        contract=(Path(index.__file__).resolve().parents[2]/'prompts'/'delegate-executor.txt').read_text()
        prefix=sessions.executor_prefix()
        store=sessions.Store('codex','test',Path('/fixture'),'jsonl_dir')
        prompt={'kind':'prompt','origin':'delegated','text':contract+'Implement a useful change\nMore detail',
                'native_type':'event_msg.user_message','ts':'1','locator':{'file':'/fixture/one','line':2}}
        ref=sessions.SessionRef('codex','test','one',title=prefix)
        row,docs,aliases,edges=index.session_payload(store,ref,[prompt],{'/fixture/one':'revision'})
        self.assertEqual(row['title'],'Implement a useful change')
        self.assertEqual(next(d for d in docs if d['kind']=='title')['text'],'Implement a useful change')
        with index.writer(self.directory) as conn:
            index.replace_session(conn,row,docs,[],aliases,edges)
            self.assertEqual(index.find_in_snapshot(conn,[prefix],kind='title')[0],[])
            self.assertEqual(len(index.find_in_snapshot(conn,['useful change'],kind='title')[0]),1)
        normal=sessions.SessionRef('codex','test','two',title='Ordinary title')
        self.assertEqual(index.session_payload(store,normal,[prompt],{})[0]['title'],'Ordinary title')

    def test_codex_state_prefix_title_uses_rollout_prompt(self):
        root=Path(self.tmp.name)/'codex'; active=root/'sessions'; active.mkdir(parents=True)
        sid='aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa'; rollout=active/f'rollout-2025-01-01T00-00-00-{sid}.jsonl'
        contract=(Path(index.__file__).resolve().parents[2]/'prompts'/'delegate-executor.txt').read_text()
        records=[{'type':'session_meta','payload':{'id':sid}},
                 {'type':'event_msg','payload':{'type':'user_message','message':contract+'Make a useful fix\nDetails'}}]
        rollout.write_text(''.join(json.dumps(record)+'\n' for record in records))
        db=root/'state_1.sqlite'
        with contextlib.closing(sqlite3.connect(db)) as conn, conn:
            conn.executescript('CREATE TABLE threads(id TEXT PRIMARY KEY,title TEXT,rollout_path TEXT); CREATE TABLE thread_spawn_edges(parent_thread_id TEXT,child_thread_id TEXT);')
            conn.execute('INSERT INTO threads VALUES(?,?,?)',(sid,sessions.executor_prefix(),str(rollout)))
        stores=[sessions.Store('codex','sessions',active,'jsonl_dir'),sessions.Store('codex','state:state_1.sqlite',db,'sqlite')]
        with patch.object(sessions,'stores_for',return_value=stores), patch.object(sessions,'porch_workers',return_value=set()):
            self.assertEqual(index.sync(self.directory,harnesses=['codex'])['codex']['extracted'],1)
        with contextlib.closing(index.open_reader(self.directory)) as conn:
            self.assertEqual(conn.execute('SELECT title FROM sessions').fetchone()[0],'Make a useful fix')
            title,source=conn.execute("SELECT text,source_key FROM docs WHERE kind='title'").fetchone()
            self.assertEqual(title,'Make a useful fix'); self.assertEqual(source,index.source_key(str(rollout)))
            self.assertEqual(index.find_in_snapshot(conn,[sessions.executor_prefix()],kind='title')[0],[])

    def test_derived_sqlite_title_links_to_its_prompt(self):
        db=Path(self.tmp.name)/'opencode.db'
        contract=(Path(index.__file__).resolve().parents[2]/'prompts'/'delegate-executor.txt').read_text()
        with contextlib.closing(sqlite3.connect(db)) as conn, conn:
            conn.executescript('''CREATE TABLE session_v2(id TEXT,title TEXT,time_updated INTEGER,time_busy INTEGER,time_idle INTEGER);
                CREATE TABLE session_message(id TEXT,session_id TEXT,type TEXT,seq INTEGER,time_created INTEGER,time_updated INTEGER,data TEXT);
                CREATE TABLE session_part(message_id TEXT,session_id TEXT,ordinal INTEGER,type TEXT,time_updated INTEGER,data TEXT);''')
            conn.execute('INSERT INTO session_v2 VALUES(?,?,?,?,?)',('s',sessions.executor_prefix(),1,None,None))
            conn.execute('INSERT INTO session_message VALUES(?,?,?,?,?,?,?)',('m','s','user',1,1,1,json.dumps({'text':contract+'Useful SQLite task'})))
        store=sessions.Store('opencode','db',db,'sqlite')
        with patch.object(sessions,'stores_for',side_effect=lambda h:[store] if h=='opencode' else []):
            index.sync(self.directory,harnesses=['opencode'])
            with contextlib.closing(index.open_reader(self.directory)) as conn:
                found,_=index.find_in_snapshot(conn,['Useful SQLite task'],kind='title')
            with patch.object(sessions,'emit') as emit:
                index.cmd_show_at(argparse.Namespace(at=found[0]['evidence']['Useful SQLite task']['locator'],max_chars=0))
                self.assertTrue(any('Useful SQLite task' in str(c.args[0].get('text','')) for c in emit.call_args_list))

    def test_collection_evidence_is_hash_seed_independent(self):
        with index.writer(self.directory) as conn:
            for name,op,ts in [('a','wait','2025-01-01T02:00:00Z'),('b','read-final','2025-01-01T03:00:00Z')]:
                key='claude-code:'+name
                edge=[] if name=='a' else [{'type':'continuation','target_key':'claude-code:a'}]
                fact={'kind':'porch_call','target':'run_x','ts':ts,'data':{'op':op,'result_ts':ts,'is_error':False}}
                index.replace_session(conn,{'session_key':key,'harness':'claude-code','native_id':name,'store':'test',
                                            'content_state':'ok','_facts':[fact]},[],[],[],edge)
            index.replace_session(conn,{'session_key':'porch:run_x','harness':'porch','native_id':'run_x','store':'runs',
                'content_state':'ok','status_json':json.dumps({'status':'completed','finished_at':'2025-01-01T01:00:00Z'})},[],[],[],
                [{'type':'caller_run','target_key':'claude-code:a'}])
        script='''import sys,json;sys.path.insert(0,sys.argv[1]);import sessions_index as i,sessions_trace as t,sessions_verdicts as v;from pathlib import Path
c=i.open_reader(Path(sys.argv[2]));coverage={'synced':True,'discovery_complete':True};n,a,e,u=t._graph(c,coverage);engine=v.Verdicts(c,n,a,e,u,coverage,t._fact_pointer);print(json.dumps(engine.collection('porch:run_x'),sort_keys=True))'''
        outputs=[]
        for seed in ('1','987'):
            result=subprocess.run([sys.executable,'-c',script,str(Path(index.__file__).parent),str(self.directory)],
                                  env=dict(os.environ,PYTHONHASHSEED=seed),capture_output=True,text=True,check=True)
            outputs.append(result.stdout)
        self.assertEqual(outputs[0],outputs[1])
        self.assertEqual(json.loads(outputs[0])['reason'],'waited')

    def test_trace_compact_node_evidence_and_coverage_contract(self):
        coverage={'synced':True,'complete':False,'discovery_complete':True,'next':'porch sessions index build',
                  'codex':{'ok':4,'pending':0,'error':0,'offline':0,'unsupported':0},
                  'devin':{'ok':2,'pending':1,'error':0,'offline':0,'unsupported':0}}
        with index.writer(self.directory) as conn:
            row={'session_key':'codex:one','harness':'codex','native_id':'one','store':'test','class':'interactive',
                 'content_state':'ok','status_json':'{"custom": true}',
                 '_facts':[{'kind':'last_turn','ts':'3','data':{'state':'aborted','final_ts':'2','abort_reason':'cancel'}},
                           {'kind':'status_text','target':'plan_items','data':{'items':[{'text':'x'*250,'status':'pending'}]*8,'closed_by':'wrap_up'}},
                           {'kind':'status_text','target':'wrap_up','short_text':'w'*200},
                           {'kind':'status_text','target':'plan','short_text':'secret plan'}]}
            doc={'kind':'prompt','origin':'human','ts':'1','text':'p'*200,'locator_json':'{}','source_key':'["/tmp/x"]','source_rev':'[]'}
            index.replace_session(conn,row,[doc],[],[{'alias':'alias','kind':'test'}],[])
            compact=sessions_trace.trace_snapshot(conn,['codex:one'],coverage)
            node=compact['nodes'][0]
            self.assertNotIn('last_prompt',node); self.assertNotIn('ref',node); self.assertNotIn('native_status',node)
            self.assertEqual(set(node['task']),{'ts','snippet'}); self.assertEqual(len(node['task']['snippet']),160)
            self.assertEqual(node['state'],{'last_turn':'aborted','ts':'3','abort_reason':'cancel'})
            self.assertEqual(node['plan']['closed_by'],'wrap_up'); self.assertEqual(len(node['plan']['open']),5)
            self.assertTrue(all(len(x)==80 for x in node['plan']['open']))
            self.assertEqual(node['verdicts'][0]['reason'],'last turn interrupted after an answer')
            self.assertNotIn('exit_plan',node); self.assertNotIn('codex',compact['_summary']['coverage'])
            self.assertEqual(compact['_summary']['coverage']['totals']['pending'],1)
            expanded=sessions_trace.trace_snapshot(conn,['codex:one'],coverage,evidence=True)['nodes'][0]
            self.assertIn('alias',expanded['aliases']); self.assertIn('show',expanded['task'])
            self.assertEqual(len(expanded['plan']['items']),8); self.assertIn('exit_plan',expanded)
            second={**doc,'text':'second','ts':'2'}
            index.replace_session(conn,row,[doc,second],[],[{'alias':'alias','kind':'test'}],[])
            self.assertEqual(sessions_trace.trace_snapshot(conn,['codex:one'],coverage)['nodes'][0]['last_prompt']['snippet'],'second')
            row['_facts'][0]['data']['final_ts'] = None
            answer={**doc,'kind':'answer','text':'answer','ts':None}
            index.replace_session(conn,row,[doc,answer],[],[],[])
            self.assertEqual(sessions_trace.trace_snapshot(conn,['codex:one'],coverage)['nodes'][0]['verdicts'][0]['reason'],
                             'last turn interrupted after an answer')

    def test_collection_verdict_evidence_branches(self):
        coverage={'synced':True,'discovery_complete':True,**{h:{'pending':0,'error':0,'offline':0,'unsupported':0} for h in index.UNIT_BUILDERS}}
        finish='2025-01-01T01:00:00Z'; after='2025-01-01T02:00:00Z'; before='2025-01-01T00:00:00Z'
        def call(op,target='run_a',ts=after,**kwargs):
            return {'kind':'porch_call','target':target,'ts':ts,'locator':{'file':'/fixture/caller','line':1},
                    'data':{'op':op,'call_ts':ts,'result_ts':ts,'is_error':False,**kwargs}}
        with index.writer(self.directory) as conn:
            def put(key,updated=after,status=None,facts=(),edges=(),aliases=(),state='ok',source_state='ok'):
                h,n=key.split(':',1); source=index.source_key('/fixture/'+n)
                conn.execute("INSERT OR REPLACE INTO sources(source_key,harness,store,kind,state) VALUES(?,?,?,'file',?)",(source,h,'test',source_state));conn.commit()
                row={'session_key':key,'harness':h,'native_id':n,'store':'test','updated':updated,'class':'interactive',
                     'status_json':json.dumps(status or {}),'content_state':state,'_facts':list(facts)}
                index.replace_session(conn,row,[],[{'source_key':source,'role':'content','fingerprint':'[]'}],
                    [{'alias':a,'kind':'test'} for a in aliases],list(edges))
            def verdict():
                nodes,aliases,edges,unresolved=sessions_trace._graph(conn,coverage)
                engine=sessions_verdicts.Verdicts(conn,nodes,aliases,edges,unresolved,coverage,sessions_trace._fact_pointer,now=sessions.ts_epoch('2025-01-03T00:00:00Z'))
                return engine.collection('porch:run_a')
            put('claude-code:caller',aliases=['claude:caller'])
            edge={'type':'caller_run','unresolved':'claude:caller','basis':'launcher'}
            status={'status':'completed','finished_at':finish,'artifacts_dir':'/tmp/run-delegate-art'}
            def run(facts=()): put('porch:run_a',status=status,edges=[edge],facts=facts)
            run()
            self.assertEqual(verdict()['reason'],'caller_continued_without_collecting')
            put('claude-code:caller',facts=[call('delegate',detach=True)],aliases=['claude:caller'])
            self.assertEqual(verdict()['reason'],'caller_continued_without_collecting')
            put('claude-code:caller',updated=before,facts=[call('delegate',ts=before,detach=True)],aliases=['claude:caller'])
            self.assertEqual(verdict()['reason'],'caller_stopped_before_finish')
            for observation in (call('status',ts=before),call('events'),call('wait-any'),call('wait',quiet=True),call('wait',background=True)):
                put('claude-code:caller',facts=[call('delegate',detach=True),observation],aliases=['claude:caller'])
                self.assertEqual(verdict()['observed'],'unknown')
            put('claude-code:caller',aliases=['claude:caller'])
            coverage['claude-code']['pending'] = 1  # Another session in the same harness.
            coverage['codex']['pending'] = 1
            self.assertEqual(verdict()['reason'],'caller_continued_without_collecting')
            coverage['claude-code']['walk_complete'] = False
            self.assertEqual(verdict()['reason'],'caller content incomplete')
            coverage['claude-code']['walk_complete'] = True
            put('claude-code:caller',facts=[{'kind':'status_text','target':'wrap_up','ts':after,'data':None}],aliases=['claude:caller'])
            self.assertEqual(verdict()['reason'],'caller_continued_without_collecting')
            put('claude-code:caller',updated=before,aliases=['claude:caller'])
            self.assertEqual(verdict()['reason'],'caller_stopped_before_finish')
            for op,options,expected in [('wait',{},'waited'),('result',{},'waited'),('read-final',{'target':'run-delegate-art'},'read final.txt'),
                                        ('delegate',{'detach':False},'foreground result'),
                                        ('wait',{'background':True,'via':'background_output'},'waited via background output')]:
                target=options.pop('target','run_a')
                put('claude-code:caller',facts=[call(op,target,**options)],aliases=['claude:caller'])
                self.assertEqual(verdict()['reason'],expected)
            put('claude-code:caller',facts=[call('wait',result_locator={'file':'/fixture/caller','line':2})],aliases=['claude:caller'])
            compact=sessions_trace.trace_snapshot(conn,['porch:run_a'],coverage)
            expanded=sessions_trace.trace_snapshot(conn,['porch:run_a'],coverage,evidence=True)
            compact_collection=next(n['collection'] for n in compact['nodes'] if n['key']=='porch:run_a')
            expanded_collection=next(n['collection'] for n in expanded['nodes'] if n['key']=='porch:run_a')
            self.assertEqual(len(compact_collection['evidence']),1)
            self.assertEqual(len(expanded_collection['evidence']),2)
            self.assertNotIn('show',compact_collection['evidence'][0])
            self.assertEqual(json.loads(shlex.split(expanded_collection['evidence'][-1]['show'])[-1])['v'],1)
            self.assertNotIn('porch sessions show --at',json.dumps(compact))
            self.assertNotIn('"locator"',json.dumps(compact))
            for item in (call('wait',quiet=True),call('wait-any'),call('events'),call('status'),
                         call('wait',background=True),call('result',ts=before)):
                put('claude-code:caller',facts=[item],aliases=['claude:caller'])
                self.assertEqual(verdict()['observed'],'unknown')
            for target in (None,'run_unrelated'):
                put('claude-code:caller',facts=[call('read-final',target)],aliases=['claude:caller'])
                self.assertEqual(verdict()['reason'],'unattributed read or wait after finish')
            put('claude-code:caller',updated='2025-01-02T23:59:00Z',aliases=['claude:caller'])
            self.assertEqual(verdict()['reason'],'caller still active')
            put('claude-code:caller',aliases=['claude:caller'],state='unavailable')
            self.assertEqual(verdict()['reason'],'caller content incomplete')
            put('claude-code:caller',aliases=['claude:caller'],source_state='pending')
            self.assertEqual(verdict()['reason'],'caller content incomplete')
            put('claude-code:caller',aliases=['claude:caller'])
            for collector,expected in [('claude:caller',None),('claude:else','another, unresolved session')]:
                run([{'kind':'delivery','target':'run_a','ts':after,'locator':{'file':'/fixture/deliveries.jsonl','line':1},
                      'data':{'via':'wait','format':'text','stdout':'pipe','collector':collector}}])
                got=verdict(); self.assertEqual(got['reason'],'delivered via wait')
                self.assertEqual(got['evidence'][0].get('collector'),expected)
            put('codex:collector',aliases=['codex:other'])
            run([{'kind':'delivery','target':'run_a','ts':after,'locator':{'file':'/fixture/deliveries.jsonl','line':1},
                  'data':{'via':'foreground','format':'text','stdout':'pipe','collector':'codex:other'}}])
            got=verdict(); self.assertEqual(got['reason'],'delivered via foreground')
            self.assertEqual(got['evidence'][0]['collector'],'codex:collector')
            run(); put('claude-code:next',facts=[call('wait')],edges=[{'type':'continuation','target_key':'claude-code:caller'}])
            self.assertEqual(verdict()['reason'],'waited')
            put('claude-code:next',edges=[{'type':'continuation','target_key':'claude-code:caller'}])
            put('claude-code:fork',facts=[call('wait')],edges=[{'type':'fork','target_key':'claude-code:caller'}])
            self.assertEqual(verdict()['reason'],'waited')
            put('porch:run_a',status=status)
            self.assertEqual(verdict()['reason'],'caller not resolved')
            put('porch:parent',status={'status':'completed'})
            put('porch:run_a',status=status,edges=[{'type':'caller_run','target_key':'porch:parent'}])
            self.assertEqual(verdict()['reason'],'caller content incomplete')
            put('porch:run_a',status={'status':'completed'})
            self.assertEqual(verdict()['reason'],'run not finished')
            put('porch:run_a',status={'status':'running'})
            self.assertIsNone(verdict()['observed'])

    def test_trace_verdicts_and_left_behind_filters(self):
        coverage={'synced':True,'discovery_complete':True,**{h:{'pending':0,'error':0,'offline':0,'unsupported':0} for h in index.UNIT_BUILDERS}}
        now=sessions.ts_epoch('2025-01-03T00:00:00Z')
        with index.writer(self.directory) as conn:
            def put(key,status,updated='2025-01-02T00:00:00Z',cls='headless',facts=(),edges=(),cwd='/project'):
                h,n=key.split(':',1)
                index.replace_session(conn,{'session_key':key,'harness':h,'native_id':n,'store':'test','class':cls,
                    'class_basis':'executor-prefix' if cls=='headless' else None,
                    'updated':updated,'cwd':cwd,'content_state':'ok','status_json':json.dumps(status),'_facts':list(facts)},[],[],[],list(edges))
            put('claude-code:root',{},cls='interactive')
            edge={'type':'caller_run','target_key':'claude-code:root','basis':'fixture'}
            put('porch:running',{'status':'running'},edges=[edge])
            put('porch:failed',{'status':'failed','exit_code':1},edges=[edge])
            put('codex:open',{},facts=[{'kind':'last_turn','ts':'2025-01-02T00:00:00Z','data':{'state':'open'}}])
            put('codex:live',{},updated='2025-01-02T23:59:00Z',facts=[{'kind':'last_turn','ts':'2025-01-02T23:59:00Z','data':{'state':'open'}}])
            put('codex:aborted',{},facts=[{'kind':'last_turn','ts':'2025-01-02T00:00:00Z','data':{'state':'aborted','abort_reason':'cancel'}}])
            put('codex:plan',{},facts=[{'kind':'status_text','target':'plan_items','data':{'items':[{'text':'todo','status':'pending'}]}}])
            put('codex:other',{},cwd='/other',facts=[{'kind':'last_turn','data':{'state':'aborted'}}])
            put('codex:old',{},updated='2024-01-01T00:00:00Z',facts=[{'kind':'last_turn','data':{'state':'aborted'}}])
            put('claude-desktop:interrupted',{'interruptedAwaitingPerson':True},cls='interactive')
            put('agy:existing',{},cls='interactive')
            unrelated=index.source_key('/fixture/existing')
            conn.execute("INSERT INTO sources(source_key,harness,store,kind,state) VALUES(?,'agy','conversations','file','pending')",(unrelated,))
            conn.execute("INSERT INTO session_sources(session_key,source_key,role) VALUES('agy:existing',?,'content')",(unrelated,))
            nodes,aliases,edges,unresolved=sessions_trace._graph(conn,coverage)
            engine=sessions_verdicts.Verdicts(conn,nodes,aliases,edges,unresolved,coverage,sessions_trace._fact_pointer,now=now)
            for key in ('porch:running','porch:failed','codex:open','codex:aborted','codex:plan'):
                self.assertIn('unfinished',[v['kind'] for v in engine.verdicts(key)])
            self.assertIn('active',[v['kind'] for v in engine.verdicts('codex:live')])
            self.assertIn('unfinished',[v['kind'] for v in engine.verdicts('claude-desktop:interrupted')])
            self.assertTrue(any(v['kind']=='orphaned' and v['observed'] is True for v in engine.verdicts('codex:open')))
            coverage['agy']['pending']=1
            self.assertTrue(any(v['kind']=='orphaned' and v['observed'] is True for v in engine.verdicts('codex:open')))
            pending=index.source_key('/fixture/new')
            conn.execute("INSERT INTO sources(source_key,harness,store,kind,state) VALUES(?,'porch','runs','file','pending')",(pending,))
            engine._pending_cache.clear()
            self.assertTrue(any(v['kind']=='orphaned' and v['observed']=='unknown' and 'first-time' in v['reason'] for v in engine.verdicts('codex:open')))
            conn.execute("UPDATE sources SET state='error' WHERE source_key=?",(pending,))
            engine._pending_cache.clear()
            self.assertTrue(any(v['kind']=='orphaned' and v['observed']=='unknown' and 'first-time' in v['reason'] for v in engine.verdicts('codex:open')))
            conn.execute('DELETE FROM sources WHERE source_key=?',(pending,))
            engine._pending_cache.clear()
            coverage['discovery_complete']=False
            self.assertTrue(any(v['kind']=='orphaned' and v['observed']=='unknown' for v in engine.verdicts('codex:open')))
            coverage['discovery_complete']=True
            with patch.object(sessions,'iter_all_fragments',side_effect=AssertionError('raw scan')):
                result=sessions_trace.trace_snapshot(conn,[],coverage,left_behind=True,since='7d',cwd='/project',now=now)
            keys={n['key'] for n in result['nodes']}
            self.assertIn('claude-code:root',keys); self.assertIn('porch:running',keys)
            self.assertEqual(set(next(n for n in result['nodes'] if n['key']=='claude-code:root')),{'key','harness','title','updated'})
            self.assertNotIn('codex:other',keys)
            self.assertNotIn('codex:old',keys)
            only_porch=sessions_trace.trace_snapshot(conn,[],coverage,left_behind=True,since='7d',harnesses=['porch'],now=now)
            self.assertEqual(set(only_porch['_summary']['seeds']),{'porch:running','porch:failed'})
            self.assertIn('unfinished',result['_summary']['verdicts'])

    def test_left_behind_evaluates_each_candidate_once(self):
        coverage={'synced':True,'discovery_complete':True,**{h:{'walk_complete':True} for h in index.UNIT_BUILDERS}}
        with index.writer(self.directory) as conn:
            index.replace_session(conn,{'session_key':'porch:run_x','harness':'porch','native_id':'run_x',
                'store':'runs','class':'headless','updated':'2025-01-01T00:00:00Z','content_state':'ok',
                'status_json':'{"status":"running"}'},[],[],[],[])
            calls=[]; original=sessions_verdicts.Verdicts.verdicts
            def counted(engine,key,collection=None):
                calls.append(key); return original(engine,key,collection)
            with patch.object(sessions_verdicts.Verdicts,'verdicts',counted):
                sessions_trace.trace_snapshot(conn,[],coverage,left_behind=True,since='2024-01-01',now=sessions.ts_epoch('2025-01-02T00:00:00Z'))
            self.assertEqual(calls,['porch:run_x'])

    def test_trace_edges_alias_late_resolution_and_limits(self):
        coverage={'discovery_complete':True, **{h:{'pending':0,'error':0,'offline':0} for h in index.UNIT_BUILDERS}}
        def add(conn,key,edges=(),aliases=(),text=None):
            harness,native=key.split(':',1)
            row={'session_key':key,'harness':harness,'native_id':native,'store':'runs' if harness=='porch' else 'test',
                 'created':'2026-01-01','status_json':'{}','content_state':'ok'}
            docs=[{'kind':'prompt','origin':'human','text':text,'locator_json':'{}','source_key':'["/tmp/test"]','source_rev':'[]'}] if text else []
            index.replace_session(conn,row,docs,[],[{'alias':a,'kind':'test'} for a in aliases],
                                  [{'type':t,'target_key':target,'unresolved':unresolved,'basis':t,'locator':{'file':'fixture'}} for t,target,unresolved in edges])
        with index.writer(self.directory) as conn:
            # A child is committed before its target and resolves when queried later.
            add(conn,'porch:run_1',[('caller_run',None,'claude:caller-key'),('worker_session','codex:worker',None)],text='x'*400)
            add(conn,'porch:run_2',[('caller_run',None,'run:run_1'),('parent_run','porch:run_1',None)])
            add(conn,'porch:run_3',[('continuation','porch:run_2',None)])
            add(conn,'claude-code:caller',aliases=['claude:caller-key','claude-desktop:tab'])
            add(conn,'codex:worker',[('subagent','codex:root',None),('fork','codex:root',None)])
            add(conn,'codex:root',[('subagent','codex:worker',None)])
            add(conn,'opencode:child',[('subagent','codex:worker',None)])
            add(conn,'agy:child',[('subagent','opencode:child',None)])
            add(conn,'claude-code:child',[('subagent','claude-code:caller',None),('spawned','claude-desktop:tab',None),
                                           ('continuation','claude-code:caller',None)])
            result=sessions_trace.trace_snapshot(conn,['claude-desktop:tab','run_2'],coverage,max_nodes=100)
            self.assertTrue(all('locator' not in edge and 'show' not in edge for edge in result['edges']))
            with_evidence=sessions_trace.trace_snapshot(conn,['claude-desktop:tab','run_2'],coverage,max_nodes=100,evidence=True)
            self.assertTrue(all('locator' in edge or 'show' in edge for edge in with_evidence['edges']))
            pairs={(e['from'],e['to'],e['type']) for e in result['edges']}
            self.assertIn(('claude-code:caller','porch:run_1','caller_run'),pairs)
            self.assertIn(('porch:run_1','codex:worker','worker_session'),pairs)
            self.assertIn(('porch:run_1','porch:run_2','parent_run'),pairs)
            self.assertIn(('porch:run_2','porch:run_3','continuation'),pairs)
            self.assertIn(('codex:root','codex:worker','subagent'),pairs)
            self.assertIn(('opencode:child','agy:child','subagent'),pairs)
            self.assertEqual(result['_summary']['seeds'],['claude-code:caller','porch:run_2'])
            self.assertLessEqual(len(next(n for n in result['nodes'] if n['key']=='porch:run_1')['task']['snippet']),300)
            self.assertEqual(result['nodes'],sorted(result['nodes'],key=lambda n:(n.get('created') is None,n.get('created') or '',n['key'])))
            limited=sessions_trace.trace_snapshot(conn,['claude-code:caller'],coverage,max_nodes=2)
            self.assertGreater(limited['_summary']['truncated']['omitted_nodes'],0)
            self.assertIn('--max-nodes',limited['_summary']['truncated']['next'])
            shallow=sessions_trace.trace_snapshot(conn,['claude-code:caller'],coverage,depth=0)
            self.assertEqual([n['key'] for n in shallow['nodes']],['claude-code:caller'])
            self.assertGreater(shallow['_summary']['truncated']['omitted_nodes'],0)
            self.assertIn('--depth 1',shallow['_summary']['truncated']['next'])
            with self.assertRaisesRegex(index.SessionIndexError,'unknown session reference: absent'):
                sessions_trace.trace_snapshot(conn,['claude-code:caller','absent'],coverage)

    def test_trace_unresolved_reasons(self):
        with index.writer(self.directory) as conn:
            row={'session_key':'porch:r','harness':'porch','native_id':'r','store':'runs','content_state':'ok'}
            edges=[{'type':'worker_session','target_key':key,'basis':'fixture'} for key in ('codex:absent','devin:absent','gemini:absent')]
            index.replace_session(conn,row,[],[],[],edges)
            coverage={'discovery_complete':True,'codex':{'pending':0,'error':0,'offline':0},'devin':{'pending':1,'error':0,'offline':0,'walk_complete':False}}
            result=sessions_trace.trace_snapshot(conn,['r'],coverage)
            reasons={e['target']:e['reason'] for e in result['unresolved']}
            self.assertEqual(reasons['codex:absent'],'target not indexed; store walk complete')
            self.assertEqual(reasons['devin:absent'],'target store incomplete (pending|error|offline)')
            self.assertEqual(reasons['gemini:absent'],'target harness not indexed')

    def test_orphan_parent_evidence_and_walk_completeness(self):
        coverage={'synced':True,'discovery_complete':True,
                  **{h:{'walk_complete':True,'pending':0,'error':0,'offline':0} for h in index.UNIT_BUILDERS}}
        with index.writer(self.directory) as conn:
            row={'session_key':'codex:child','harness':'codex','native_id':'child','store':'test',
                 'class':'subagent','content_state':'ok'}
            index.replace_session(conn,row,[],[],[],[{'type':'fork','target_key':'codex:missing','basis':'native',
                                                       'locator':{'file':'/fixture','line':1}}])
            def orphan():
                nodes,aliases,edges,unresolved=sessions_trace._graph(conn,coverage)
                engine=sessions_verdicts.Verdicts(conn,nodes,aliases,edges,unresolved,coverage,sessions_trace._fact_pointer)
                return next(v for v in engine.verdicts('codex:child') if v['kind']=='orphaned')
            self.assertEqual(orphan()['observed'],True)
            self.assertEqual(orphan()['reason'],'caller/parent codex:missing not found; its store was walked completely')
            coverage['codex']['walk_complete']=False
            self.assertEqual(orphan()['observed'],'unknown')
            self.assertEqual(orphan()['reason'],'unresolved parent: codex:missing')
            index.replace_session(conn,{'session_key':'porch:unrecorded','harness':'porch','native_id':'unrecorded',
                                        'store':'runs','class':'headless','content_state':'ok','status_json':'{}'},[],[],[],[])
            nodes,aliases,edges,unresolved=sessions_trace._graph(conn,coverage)
            engine=sessions_verdicts.Verdicts(conn,nodes,aliases,edges,unresolved,coverage,sessions_trace._fact_pointer)
            self.assertTrue(any(v['reason']=='launcher not recorded' and v['observed']=='unknown'
                                for v in engine.verdicts('porch:unrecorded')))

    def test_orphan_requires_recorded_parent_or_delegated_worker_evidence(self):
        coverage={'synced':True,'discovery_complete':True,**{h:{'walk_complete':True} for h in index.UNIT_BUILDERS}}
        with index.writer(self.directory) as conn:
            for name,cls,basis in [('script','headless','sdk'),('worker','headless','executor-prefix'),('child','subagent','thread_spawn')]:
                index.replace_session(conn,{'session_key':'codex:'+name,'harness':'codex','native_id':name,
                    'store':'test','class':cls,'class_basis':basis,'content_state':'ok'},[],[],[],[])
            nodes,aliases,edges,unresolved=sessions_trace._graph(conn,coverage)
            engine=sessions_verdicts.Verdicts(conn,nodes,aliases,edges,unresolved,coverage,sessions_trace._fact_pointer)
            self.assertFalse(any(v['kind']=='orphaned' for v in engine.verdicts('codex:script')))
            worker=next(v for v in engine.verdicts('codex:worker') if v['kind']=='orphaned')
            self.assertTrue(worker['observed']); self.assertEqual(worker['reason'],'no porch run record references this worker')
            child=next(v for v in engine.verdicts('codex:child') if v['kind']=='orphaned')
            self.assertEqual(child['observed'],'unknown'); self.assertEqual(child['reason'],'parent not recorded by the harness')
            coverage['porch']['walk_complete']=False
            self.assertEqual(next(v for v in engine.verdicts('codex:worker') if v['kind']=='orphaned')['observed'],'unknown')
            coverage['porch']['walk_complete']=True
            index.replace_session(conn,{'session_key':'porch:run_x','harness':'porch','native_id':'run_x',
                'store':'runs','class':'headless','content_state':'ok'},[],[],[],
                [{'type':'worker_session','target_key':'codex:worker'}])
            nodes,aliases,edges,unresolved=sessions_trace._graph(conn,coverage)
            engine=sessions_verdicts.Verdicts(conn,nodes,aliases,edges,unresolved,coverage,sessions_trace._fact_pointer)
            self.assertFalse(any(v['kind']=='orphaned' for v in engine.verdicts('codex:worker')))

    def test_trace_alias_edges_merge_existing_nodes(self):
        coverage={'discovery_complete':True}
        with index.writer(self.directory) as conn:
            for key in ('claude-code:cli','claude-desktop:tab'):
                harness,native=key.split(':',1)
                row={'session_key':key,'harness':harness,'native_id':native,'store':'test','content_state':'ok'}
                edges=[{'type':'alias','target_key':'claude-code:cli','basis':'cliSessionId'}] if harness=='claude-desktop' else []
                index.replace_session(conn,row,[],[],[],edges)
            result=sessions_trace.trace_snapshot(conn,['claude-desktop:tab'],coverage)
            self.assertEqual([n['key'] for n in result['nodes']],['claude-code:cli'])
            self.assertNotIn('aliases',result['nodes'][0])
            expanded=sessions_trace.trace_snapshot(conn,['claude-desktop:tab'],coverage,evidence=True)
            self.assertIn('claude-desktop:tab',expanded['nodes'][0]['aliases'])
            self.assertEqual(result['edges'],[])

    def test_trace_cli_emits_one_document_and_busy_coverage(self):
        with index.writer(self.directory) as conn:
            index.replace_session(conn,{'session_key':'porch:r','harness':'porch','native_id':'r',
                                        'store':'runs','content_state':'ok',
                                        '_facts':[{'kind':'porch_call','short_text':'must-not-print'}]},[],[],[],[])
        args=argparse.Namespace(refs=['r'],depth=None,max_nodes=200,deadline=0)
        out=io.StringIO()
        with patch.object(index,'index_dir',return_value=self.directory), \
             patch.object(index,'sync',return_value={'synced':False,'reason':'another sync in progress'}), \
             contextlib.redirect_stdout(out):
            self.assertEqual(sessions_trace.cmd_trace(args),0)
        lines=out.getvalue().splitlines()
        self.assertEqual(len(lines),1)
        data=json.loads(lines[0])
        self.assertFalse(data['_summary']['coverage']['synced'])
        self.assertFalse(data['_summary']['coverage']['stat_pass'])
        self.assertEqual(data['_summary']['seeds'],['porch:r'])
        self.assertNotIn('facts',data['nodes'][0])
        self.assertNotIn('must-not-print',out.getvalue())
        args.refs=['r','absent']
        out=io.StringIO()
        with patch.object(index,'index_dir',return_value=self.directory), \
             patch.object(index,'sync',return_value={'synced':False,'reason':'another sync in progress'}), \
             contextlib.redirect_stdout(out):
            with self.assertRaisesRegex(index.SessionIndexError,'unknown session reference: absent'):
                sessions_trace.cmd_trace(args)
        self.assertEqual(out.getvalue(),'')
        args.refs=['r']; args.left_behind=True
        with self.assertRaisesRegex(ValueError,'supply REF or --left-behind'):
            sessions_trace.cmd_trace(args)
        out=io.StringIO()
        with patch.object(index,'index_dir',return_value=self.directory), \
             patch.object(index,'sync',return_value={'synced':False,'reason':'another sync in progress'}), \
             patch.object(sys,'argv',['sessions','trace','--left-behind','--since','7d','--cwd','/project','-a','porch','--evidence']), \
             contextlib.redirect_stdout(out):
            self.assertEqual(sessions.main(),0)
        self.assertIn('nodes',json.loads(out.getvalue()))

    def test_trace_fact_lifecycle_and_unsearchable_tool_output(self):
        root=Path(self.tmp.name)/'projects'; root.mkdir(); path=root/'one.jsonl'
        def records(command=True):
            data=[{'type':'user','timestamp':'2026-01-01T00:00:00Z','message':{'role':'user','content':'question'}}]
            if command:
                data += [{'type':'assistant','timestamp':'2026-01-01T00:00:01Z','message':{'role':'assistant','content':[{'type':'tool_use','id':'t','name':'Bash','input':{'command':'porch delegate wait run_test'}}]}},
                         {'type':'user','timestamp':'2026-01-01T00:00:02Z','message':{'role':'user','content':[{'type':'tool_result','tool_use_id':'t','content':'secrettooloutput'}]}}]
            return ''.join(json.dumps(x)+'\n' for x in data)
        path.write_text(records())
        store=sessions.Store('claude-code','projects',root,'jsonl_dir')
        with patch.object(sessions,'stores_for',side_effect=lambda h:[store] if h=='claude-code' else []):
            index.sync(self.directory,harnesses=['claude-code'])
            with contextlib.closing(index.open_reader(self.directory)) as conn:
                rows=conn.execute('SELECT kind,target,data FROM facts ORDER BY kind').fetchall()
                self.assertEqual([r[0] for r in rows],['last_turn','porch_call'])
                self.assertEqual(json.loads(rows[0][2])['state'],'open')
                self.assertEqual(index.find_in_snapshot(conn,['secrettooloutput'])[0],[])
                graph=sessions_trace.trace_snapshot(conn,['claude-code:one'],{'synced':True,'discovery_complete':True})
                self.assertEqual(set(graph['nodes'][0]['task']), {'ts','snippet'})
                graph=sessions_trace.trace_snapshot(conn,['claude-code:one'],{'synced':True,'discovery_complete':True},evidence=True)
                show=graph['nodes'][0]['task']['show']
            with patch.object(sessions,'emit') as emitted:
                self.assertEqual(index.cmd_show_at(argparse.Namespace(at=shlex.split(show)[-1],max_chars=0)),0)
                self.assertTrue(any(c.args[0].get('text')=='question' for c in emitted.call_args_list))
            for invalid in ('z:abc', 'd:abc'):
                with self.assertRaisesRegex(index.SessionIndexError, 'invalid locator'):
                    index.cmd_show_at(argparse.Namespace(at=invalid,max_chars=0))
            path.write_text(records(False))
            self.assertEqual(index.sync(self.directory,harnesses=['claude-code'])['claude-code']['extracted'],1)
            with contextlib.closing(index.open_reader(self.directory)) as conn:
                self.assertEqual(conn.execute('SELECT kind FROM facts').fetchall(),[('last_turn',)])
            with patch.dict(index.EXTRACTOR_VERSIONS,{'claude-code':'test-new'}):
                self.assertEqual(index.sync(self.directory,harnesses=['claude-code'])['claude-code']['extracted'],1)
            path.unlink(); index.sync(self.directory,harnesses=['claude-code'])
            with contextlib.closing(index.open_reader(self.directory)) as conn:
                self.assertEqual(conn.execute('SELECT count(*) FROM facts').fetchone(),(0,))

    def test_existing_index_gains_facts_table(self):
        with index.writer(self.directory) as conn:
            conn.execute('DROP TABLE facts'); conn.commit()
        with index.writer(self.directory) as conn:
            self.assertTrue(sessions.table_exists(conn,'facts'))

    def test_fact_replacement_rolls_back_with_documents(self):
        row={'session_key':'agy:a','harness':'agy','native_id':'a','store':'test','content_state':'ok',
             '_facts':[{'kind':'status_text','target':'plan','short_text':'old'}]}
        with index.writer(self.directory) as conn:
            index.replace_session(conn,row,[{'kind':'answer','text':'old'}],[],[],[])
            row['_facts']=[{'kind':'status_text','target':'plan','short_text':'new'}]
            with self.assertRaises(sqlite3.IntegrityError):
                index.replace_session(conn,row,[{'kind':'invalid','text':'new'}],[],[],[])
            self.assertEqual(conn.execute('SELECT short_text FROM facts').fetchall(),[('old',)])

    def test_codex_rollout_trace_facts(self):
        root=Path(self.tmp.name)/'codex'; active=root/'sessions'; active.mkdir(parents=True)
        sid='aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa'
        path=active/f'rollout-2026-01-01T00-00-00-{sid}.jsonl'
        records=[{'type':'session_meta','payload':{'id':sid}},
                 {'type':'event_msg','timestamp':'2026-01-01T00:00:00Z','payload':{'type':'user_message','message':'question'}},
                 {'type':'response_item','timestamp':'2026-01-01T00:00:01Z','payload':{'type':'function_call','name':'exec_command','call_id':'c1','arguments':json.dumps({'cmd':'porch delegate wait run_a'})}},
                 {'type':'response_item','timestamp':'2026-01-01T00:00:02Z','payload':{'type':'function_call_output','call_id':'c1','output':'done'}},
                 {'type':'response_item','timestamp':'2026-01-01T00:00:03Z','payload':{'type':'function_call','name':'update_plan','call_id':'c2','arguments':json.dumps({'plan':[{'step':'work','status':'in_progress'}]})}}]
        path.write_text(''.join(json.dumps(r)+'\n' for r in records))
        store=sessions.Store('codex','sessions',active,'jsonl_dir')
        with patch.object(sessions,'stores_for',side_effect=lambda h:[store] if h=='codex' else []):
            self.assertEqual(index.sync(self.directory,harnesses=['codex'])['codex']['extracted'],1)
        with contextlib.closing(index.open_reader(self.directory)) as conn:
            rows=conn.execute('SELECT kind,target,data FROM facts').fetchall()
            self.assertTrue(any(k=='porch_call' and target=='run_a' and json.loads(data)['op']=='wait' for k,target,data in rows))
            self.assertTrue(any(k=='status_text' and target=='plan_items' and json.loads(data)['items']==[{'text':'work','status':'in_progress'}] for k,target,data in rows))

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
        self.assertFalse(result['claude-code']['walk_complete'])
        self.assertEqual(result['claude-code']['deleted'], 0)
        with contextlib.closing(index.open_reader(self.directory)) as conn:
            self.assertEqual(conn.execute('SELECT count(*) FROM sessions').fetchone()[0], 1)
            self.assertEqual(conn.execute("SELECT count(*) FROM sources WHERE state='offline'").fetchone()[0], 1)
        with patch.object(sessions, 'stores_for', side_effect=lambda h: [store] if h == 'claude-code' else []):
            result = index.sync(self.directory, harnesses=['claude-code'])
        self.assertEqual(result['claude-code']['deleted'], 1)
        self.assertTrue(result['claude-code']['walk_complete'])

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

    def test_codex_top_level_parent_is_indexed_once(self):
        root=Path(self.tmp.name)/'codex'; active=root/'sessions'; active.mkdir(parents=True)
        sid='aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa'; parent='bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb'
        path=active/f'rollout-2026-01-01T00-00-00-{sid}.jsonl'
        path.write_text(json.dumps({'type':'session_meta','payload':{'id':sid,'parent_thread_id':parent,
                       'source':{'subagent':{'other':{}}}}})+'\n')
        db=root/'state_1.sqlite'
        with contextlib.closing(sqlite3.connect(db)) as conn, conn:
            conn.executescript('CREATE TABLE threads(id TEXT PRIMARY KEY, rollout_path TEXT); CREATE TABLE thread_spawn_edges(parent_thread_id TEXT, child_thread_id TEXT);')
            conn.execute('INSERT INTO threads VALUES(?,?)',(sid,str(path)))
            conn.execute('INSERT INTO thread_spawn_edges VALUES(?,?)',(parent,sid))
        stores=[sessions.Store('codex','sessions',active,'jsonl_dir'),sessions.Store('codex','state:state_1.sqlite',db,'sqlite')]
        with patch.object(sessions,'stores_for',return_value=stores), patch.object(sessions,'porch_workers',return_value=set()):
            index.sync(self.directory,harnesses=['codex'])
        with contextlib.closing(index.open_reader(self.directory)) as conn:
            self.assertEqual(conn.execute("SELECT type,target_key,basis FROM edges WHERE session_key=?",('codex:'+sid,)).fetchall(),
                             [('subagent','codex:'+parent,'session_meta.parent_thread_id')])

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
            self.assertFalse(result['claude-code']['walk_complete'])
            with contextlib.closing(index.open_reader(self.directory)) as conn:
                self.assertEqual(conn.execute("SELECT text FROM docs WHERE session_key='claude-code:a' AND kind='prompt'").fetchone(), ('updated',))
                self.assertEqual(conn.execute("SELECT state FROM sources WHERE store='two'").fetchone(), ('offline',))
                self.assertEqual(conn.execute('SELECT count(*) FROM sessions').fetchone(), (2,))
            moved.rename(two)
            self.assertTrue(index.sync(self.directory)['claude-code']['walk_complete'])
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
            with contextlib.closing(index.open_reader(self.directory)) as conn:
                status=json.loads(conn.execute("SELECT status_json FROM sessions WHERE session_key='porch:r'").fetchone()[0])
                self.assertTrue({'status','exit_code','started_at','finished_at','artifacts_dir','backend','native_session_id','continued_from'} <= status.keys())
                self.assertEqual(status['artifacts_dir'],str(artifacts))
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

    def test_porch_delivery_appears_and_is_bounded(self):
        root=Path(self.tmp.name)/'runs'; run=root/'r'; run.mkdir(parents=True)
        (run/'meta.json').write_text(json.dumps({'run_id':'r','status':'completed'}))
        store=sessions.Store('porch','runs',root,'run_registry')
        with patch.object(sessions,'stores_for',side_effect=lambda h:[store] if h=='porch' else []):
            self.assertEqual(index.sync(self.directory,harnesses=['porch'])['porch']['extracted'],1)
            delivery=run/'deliveries.jsonl'
            entry={'ts':'2026-01-01T00:00:00Z','via':'wait','format':'text','stdout':'pipe','collector':None}
            delivery.write_text((''.join(json.dumps(dict(entry,ts=str(i)))+'\n' for i in range(25)))+'broken\n')
            self.assertEqual(index.sync(self.directory,harnesses=['porch'])['porch']['extracted'],1)
            with contextlib.closing(index.open_reader(self.directory)) as conn:
                facts=conn.execute("SELECT ts,target,data FROM facts WHERE kind='delivery' ORDER BY rowid").fetchall()
                self.assertEqual(len(facts),20)
                self.assertEqual((facts[0][0],facts[-1][0],facts[-1][1]),('5','24','r'))
                self.assertEqual(json.loads(facts[-1][2]),{'via':'wait','format':'text','stdout':'pipe','collector':None})
                status=json.loads(conn.execute("SELECT status_json FROM sessions WHERE session_key='porch:r'").fetchone()[0])
                self.assertEqual((status['deliveries_count'],status['deliveries_malformed']),(25,1))
            self.assertEqual(index.sync(self.directory,harnesses=['porch'])['porch']['extracted'],0)
            with delivery.open('a') as handle: handle.write(json.dumps(dict(entry,ts='later'))+'\n')
            self.assertEqual(index.sync(self.directory,harnesses=['porch'])['porch']['extracted'],1)
            with contextlib.closing(index.open_reader(self.directory)) as conn:
                self.assertEqual(json.loads(conn.execute("SELECT status_json FROM sessions WHERE session_key='porch:r'").fetchone()[0])['deliveries_count'],26)

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
        with self.assertRaisesRegex(index.SessionIndexError, 'porch sessions index drop then porch sessions index build'):
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
