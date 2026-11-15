"""Evidence-based trace verdicts over one committed index snapshot."""
from __future__ import annotations

import collections
import json
import time
from datetime import datetime, timezone
from pathlib import Path

import sessions
import sessions_index as index


def _epoch(value):
    """Interpret offset-less stored timestamps as UTC, independent of host timezone."""
    if isinstance(value, str):
        if value.replace('.', '', 1).isdigit():
            return sessions.ts_epoch(value)
        try:
            parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
            if parsed.tzinfo is None:
                return parsed.replace(tzinfo=timezone.utc).timestamp()
        except ValueError:
            pass
    return sessions.ts_epoch(value)


class Verdicts:
    def __init__(self, conn, nodes, aliases, edges, unresolved, coverage, pointer, now=None):
        self.conn, self.nodes, self.aliases, self.edges = conn, nodes, aliases, edges
        self.unresolved, self.coverage, self.pointer = unresolved, coverage, pointer
        self.now = time.time() if now is None else now
        self.incoming = collections.defaultdict(list)
        self.descendants = collections.defaultdict(set)
        for edge in edges:
            self.incoming[edge['to']].append(edge)
            if edge['type'] in ('continuation','fork'):
                self.descendants[edge['from']].add(edge['to'])
        self._fact_cache, self._source_cache = {}, {}
        self._pending_cache = {}
        self.has_facts = sessions.table_exists(conn, 'facts')
        self.known_targets = set()
        for key, node in nodes.items():
            if node['harness'] == 'porch':
                self.known_targets.add(node['native_id'])
                status = json.loads(node['status_json'] or '{}')
                if status.get('artifacts_dir'):
                    self.known_targets.add(Path(status['artifacts_dir']).name)

    def facts(self, key):
        if not self.has_facts: return []
        if key not in self._fact_cache:
            node = self.nodes[key]
            self._fact_cache[key] = [dict(rowid=rowid, kind=k, ts=ts, target=target,
                                           locator=json.loads(loc) if loc else None,
                                           text=text, data=(json.loads(data) or {}) if data else {})
                for rowid,k,ts,target,loc,text,data in self.conn.execute(
                    'SELECT rowid,kind,ts,target,locator,short_text,data FROM facts WHERE session_key IN (SELECT value FROM json_each(?)) ORDER BY rowid',
                    (json.dumps(node['_members']),))]
        return self._fact_cache[key]

    def evidence(self, key, locator=None, description=None, ts=None):
        if locator is None and description is None:
            row=self.conn.execute('''SELECT d.locator_json FROM docs d WHERE d.session_key IN (SELECT value FROM json_each(?))
                    AND d.locator_json IS NOT NULL ORDER BY d.doc_id DESC LIMIT 1''', (json.dumps(self.nodes[key]['_members']),)).fetchone()
            if row:
                locator=json.loads(row[0])
            else:
                row=self.conn.execute('''SELECT s.source_key FROM sources s JOIN session_sources ss ON ss.source_key=s.source_key
                        WHERE ss.session_key IN (SELECT value FROM json_each(?)) AND ss.role='metadata' LIMIT 1''',
                        (json.dumps(self.nodes[key]['_members']),)).fetchone()
                if row:
                    try: locator={'file':json.loads(row[0])[0]}
                    except (ValueError,TypeError,IndexError): pass
        result = {'description':(description + ' in ' + key) if description else None, 'ts':ts}
        pointer = self.pointer(self.conn, self.nodes[key], locator)
        result.update(pointer)
        return {k:v for k,v in result.items() if v is not None}

    def _walk_complete(self, harness):
        state = self.coverage.get(harness)
        return (isinstance(state, dict) and self.coverage.get('synced', True) and
                state.get('walk_complete', self.coverage.get('discovery_complete', False)))

    def _complete(self, key):
        node = self.nodes[key]
        # Only these adapters extract caller tool observations; absence of rows
        # from other harnesses cannot prove that no collection occurred.
        if not self.has_facts or node['harness'] not in ('claude-code','codex') or node['content_state'] != 'ok': return False
        if not self._walk_complete(node['harness']):
            return False
        if key not in self._source_cache:
            self._source_cache[key] = list(self.conn.execute('''SELECT ss.role,s.state FROM session_sources ss
                    JOIN sources s ON s.source_key=ss.source_key WHERE ss.session_key IN (SELECT value FROM json_each(?))''',
                    (json.dumps(node['_members']),)))
        rows = self._source_cache[key]
        return bool(rows) and any(role == 'content' for role,_ in rows) and all(state == 'ok' for _,state in rows)

    def _first_time_pending(self, harnesses):
        key = tuple(sorted(harnesses))
        if key not in self._pending_cache:
            self._pending_cache[key] = bool(self.conn.execute('''SELECT 1 FROM sources s
                WHERE s.harness IN (SELECT value FROM json_each(?)) AND s.state IN ('pending','error')
                  AND NOT EXISTS (SELECT 1 FROM session_sources ss WHERE ss.source_key=s.source_key)
                LIMIT 1''', (json.dumps(key),)).fetchone())
        return self._pending_cache[key]

    def _orphaned(self, key):
        node = self.nodes[key]
        missing = [edge for edge in self.unresolved if edge['from'] == key and edge['type'] != 'worker_session']
        if node['harness'] == 'porch' and not self._launcher(key):
            return 'unknown', 'launcher not recorded', self.evidence(key, description='launcher not recorded', ts=node['updated'])
        if missing:
            edge = next((e for e in missing if e['reason'] != 'target not indexed; store walk complete'), missing[0])
            evidence = self.evidence(key, edge.get('locator'), edge['reason'])
            if edge['reason'] != 'target not indexed; store walk complete':
                return 'unknown', 'unresolved parent: ' + str(edge['target']), evidence
            return True, 'caller/parent ' + str(edge['target']) + ' not found; its store was walked completely', evidence
        if node['class'] == 'subagent':
            return 'unknown', 'parent not recorded by the harness', self.evidence(key, description='parent not recorded by the harness', ts=node['updated'])
        basis = node.get('class_basis') or ''
        if node['class'] != 'headless' or not any(term in basis for term in ('executor-prefix','porch registry')):
            return None
        if not self._walk_complete('porch'):
            return 'unknown', 'caller store incomplete', self.evidence(key, description='caller store coverage incomplete', ts=node['updated'])
        if self._first_time_pending({'porch'}):
            return 'unknown', 'first-time caller source pending or error', self.evidence(key, description='first-time caller source pending or error', ts=node['updated'])
        return True, 'no porch run record references this worker', self.evidence(key, description='no porch run record references this worker', ts=node['updated'])

    def _caller_set(self, key):
        callers = {e['from'] for e in self.incoming[key] if e['type']=='caller_run'}
        if not callers: return set()
        queue = collections.deque(sorted(callers))
        while queue:
            for nxt in sorted(self.descendants[queue.popleft()]):
                if nxt not in callers:
                    callers.add(nxt); queue.append(nxt)
        return callers

    def _launcher(self, key):
        for member in self.nodes[key]['_members']:
            row=self.conn.execute("SELECT coalesce(unresolved,target_key) FROM edges WHERE session_key=? AND type='caller_run' LIMIT 1",(member,)).fetchone()
            if row and row[0]: return row[0]
        return None

    def _last_activity_evidence(self, key):
        candidates=[]
        rows=self.conn.execute('''SELECT ts,locator_json FROM docs WHERE session_key IN (SELECT value FROM json_each(?))
                AND ts IS NOT NULL AND locator_json IS NOT NULL''', (json.dumps(self.nodes[key]['_members']),))
        for ts,raw in rows:
            epoch=_epoch(ts)
            if epoch is not None: candidates.append((epoch,ts,json.loads(raw)))
        for fact in self.facts(key):
            data=fact['data']; ts=data.get('result_ts') or fact['ts']
            locator=data.get('result_locator') or fact['locator']; epoch=_epoch(ts)
            if epoch is not None and locator: candidates.append((epoch,ts,locator))
        if candidates:
            _,ts,locator=max(candidates,key=lambda value:value[0])
            return self.evidence(key,locator,'last indexed activity',ts)
        return self.evidence(key,description='caller last activity',ts=self.nodes[key]['updated'])

    def collection(self, key):
        node = self.nodes[key]; status = json.loads(node['status_json'] or '{}')
        final = status.get('status') == 'completed' or status.get('final_available') or bool(self.conn.execute(
            "SELECT 1 FROM docs WHERE session_key IN (SELECT value FROM json_each(?)) AND kind='answer' LIMIT 1",
            (json.dumps(node['_members']),)).fetchone())
        if not final: return {'observed':None,'reason':'no final text','evidence':[self.evidence(key,description='run status',ts=node['updated'])]}
        finish = _epoch(status.get('finished_at'))
        if finish is None: return {'observed':'unknown','reason':'run not finished','evidence':[self.evidence(key,description='finish time unavailable',ts=node['updated'])]}
        launcher = self._launcher(key)
        for fact in self.facts(key):
            data = fact['data']
            if fact['kind']!='delivery' or _epoch(fact['ts']) is None or _epoch(fact['ts']) < finish:
                continue
            ev = self.evidence(key,fact['locator'],'delivery record',fact['ts'])
            collector = data.get('collector')
            if collector and collector != launcher:
                ev['collector'] = self.aliases.get(collector) or ('porch:'+collector[4:] if collector.startswith('run:') and 'porch:'+collector[4:] in self.nodes else 'another, unresolved session')
            return {'observed':True,'reason':'delivered via '+str(data.get('via')), 'evidence':[ev]}
        callers = self._caller_set(key)
        if not callers: return {'observed':'unknown','reason':'caller not resolved','evidence':[self.evidence(key,description='caller unresolved',ts=node['updated'])]}
        run_targets = {node['native_id']}
        if status.get('artifacts_dir'): run_targets.add(Path(status['artifacts_dir']).name)
        all_calls = []
        for caller in sorted(callers):
            all_calls.extend((caller,f) for f in self.facts(caller) if f['kind']=='porch_call')
        all_calls.sort(key=lambda pair: (_epoch(pair[1]['data'].get('result_ts')) if _epoch(pair[1]['data'].get('result_ts')) is not None else float('inf'), pair[0], pair[1]['rowid']))
        for caller,fact in all_calls:
            data = fact['data']; op=data.get('op'); ts=_epoch(data.get('result_ts'))
            acceptable = (op in ('wait','result','read-final') and not (op=='wait' and data.get('quiet'))) or (op=='delegate' and not data.get('detach'))
            launch_only = data.get('background') and data.get('via') != 'background_output'
            if fact['target'] in run_targets and acceptable and not launch_only and ts is not None and ts>=finish and data.get('is_error') is False:
                reason = 'waited' if op in ('wait','result') else 'read final.txt' if op=='read-final' else 'foreground result'
                if data.get('via')=='background_output': reason += ' via background output'
                evidence=[self.evidence(caller,fact['locator'],op+' call',data.get('call_ts'))]
                if data.get('result_locator') and data.get('result_locator') != fact['locator']:
                    evidence.append(self.evidence(caller,data['result_locator'],op+' result',data.get('result_ts')))
                return {'observed':True,'reason':reason,'evidence':evidence}
        incomplete = next((caller for caller in sorted(callers) if not self._complete(caller)), None)
        if incomplete:
            return {'observed':'unknown','reason':'caller content incomplete','evidence':[self.evidence(incomplete,description='caller source or coverage incomplete',ts=self.nodes[incomplete]['updated'])]}
        updates = [(_epoch(self.nodes[c]['updated']),c) for c in sorted(callers)]
        if any(ts is None for ts,_ in updates):
            return {'observed':'unknown','reason':'caller content incomplete','evidence':[self.evidence(c,description='caller last activity unavailable') for ts,c in updates if ts is None][:3]}
        last, caller = max(updates)
        if last > self.now - 1800:
            return {'observed':'unknown','reason':'caller still active','evidence':[self.evidence(caller,description='caller last activity',ts=self.nodes[caller]['updated'])]}
        mentioned = [(c,f) for c,f in all_calls if f['target'] in run_targets and
                     not (f['data'].get('op') == 'delegate' and f['data'].get('detach'))]
        if mentioned:
            c,f = mentioned[-1]
            return {'observed':'unknown','reason':'only status/events/wait-any/quiet/background-launch observations',
                    'evidence':[self.evidence(c,f['locator'],f['data'].get('op'),f['ts'])]}
        groups = collections.defaultdict(set)
        examples = {}
        for c,f in all_calls:
            d=f['data']; ts=_epoch(d.get('result_ts'))
            if d.get('op') not in ('wait','result','read-final','delegate') or ts is None or ts<finish: continue
            group=(c,json.dumps(f['locator'],sort_keys=True),d.get('call_ts'),d.get('result_ts'),d.get('op'),d.get('via'))
            groups[group].add(f['target']); examples[group]=(c,f)
        for group, targets in groups.items():
            if None in targets or not any(target in self.known_targets for target in targets):
                c,f=examples[group]
                return {'observed':'unknown','reason':'unattributed read or wait after finish',
                        'evidence':[self.evidence(c,f['locator'],f['data'].get('op'),f['ts'])]}
        reason = 'caller_stopped_before_finish' if last < finish else 'caller_continued_without_collecting'
        return {'observed':False,'reason':reason,
                'evidence':[self._last_activity_evidence(caller),
                            {'description':'caller session updated '+str(self.nodes[caller]['updated'])}]}

    def verdicts(self, key, collection=None):
        node=self.nodes[key]; status=json.loads(node['status_json'] or '{}'); result=[]
        def add(kind, reason, evidence=(), observed=True):
            result.append({'kind':kind,'observed':observed,'reason':reason,'evidence':list(evidence)[:3]})
        if node['harness']=='porch':
            state=status.get('status')
            if state is None:
                add('unfinished','run status unknown',[self.evidence(key,description='run status unavailable',ts=node['updated'])],observed='unknown')
            elif state not in ('completed','failed','cancelled','canceled'):
                add('unfinished','run status: '+str(state),[self.evidence(key,description='run status '+str(state),ts=node['updated'])])
            elif state in ('failed','cancelled','canceled'):
                add('unfinished','run '+str(state),[self.evidence(key,description='status '+str(state)+', exit '+str(status.get('exit_code'))+', error '+str(status.get('error')),ts=node['updated'])])
            if collection and collection['observed'] is False:
                add('uncollected',collection['reason'],collection['evidence'])
        for f in self.facts(key):
            data=f['data']
            if f['kind']=='last_turn' and data.get('state')=='open':
                updated=_epoch(node['updated']); live=updated is not None and updated>self.now-1800
                started=_epoch(f['ts'])
                idle='unknown' if started is None else f'{max(0,(self.now-started)/3600):.1f} hours'
                add('active' if live else 'unfinished','open last turn' if live else 'open last turn; idle '+idle,
                    [self.evidence(key,f['locator'],'last turn',f['ts'])], observed='unknown' if updated is None else True)
            elif f['kind']=='last_turn' and data.get('state')=='aborted':
                last_doc = self.conn.execute('''SELECT kind FROM docs WHERE session_key IN
                    (SELECT value FROM json_each(?)) AND kind IN ('prompt','answer') ORDER BY doc_id DESC LIMIT 1''',
                    (json.dumps(node['_members']),)).fetchone()
                has_answer = bool(data.get('final_ts') or (last_doc and last_doc[0] == 'answer'))
                reason = 'last turn interrupted after an answer' if has_answer else 'last turn aborted: '+str(data.get('abort_reason') or 'unknown')
                add('unfinished',reason,[self.evidence(key,f['locator'],'last turn',f['ts'])])
            elif f['kind']=='status_text' and f['target']=='plan_items':
                items=data.get('items') or []; opened=[str(i.get('text') or '')[:120] for i in items if i.get('status')!='completed']
                if opened:
                    add('unfinished',f'{len(opened)} of {len(items)} plan items incomplete: '+ '; '.join(opened[:3]),[self.evidence(key,f['locator'],'plan items',f['ts'])])
        for field in ('interruptedAwaitingPerson','error','errorCategory','turn_aborted'):
            if status.get(field): add('unfinished',field+': '+str(status[field])[:200],[self.evidence(key,description='native status '+field,ts=node['updated'])])
        if status.get('aborted_count') and not any(f['kind']=='last_turn' for f in self.facts(key)):
            add('unfinished','prior aborts; current turn unknown',[self.evidence(key,description='native abort count',ts=node['updated'])],observed='unknown')
        if node['harness']=='porch' or node['class'] in ('headless','subagent'):
            if not self.incoming[key]:
                orphan = self._orphaned(key)
                if orphan:
                    observed, reason, evidence = orphan
                    add('orphaned', reason, [evidence], observed=observed)
        return result
