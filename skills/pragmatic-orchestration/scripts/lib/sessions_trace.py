"""Snapshot-based traversal of indexed session lineage."""
from __future__ import annotations

import collections
import json
import math
import re
import shlex
import time

import sessions
import sessions_index as index
import sessions_verdicts


def _reason(target, coverage):
    prefix = (target or '').split(':', 1)[0]
    harness = {'claude': 'claude-code', 'run': 'porch'}.get(prefix, prefix)
    if harness not in index.UNIT_BUILDERS:
        return 'target harness not indexed'
    state = coverage.get(harness)
    if (not coverage.get('synced', True) or not isinstance(state, dict) or
            not state.get('walk_complete', coverage.get('discovery_complete', False))):
        return 'target store incomplete (pending|error|offline)'
    return 'target not indexed; store walk complete'


def _resolve(value, keys, aliases):
    if not value:
        return None
    if value in keys:
        return value
    if value in aliases:
        return aliases[value]
    if value.startswith('run:') and 'porch:' + value[4:] in keys:
        return 'porch:' + value[4:]
    if 'porch:' + value in keys:
        return 'porch:' + value
    return None


def _graph(conn, coverage):
    rows = conn.execute('SELECT session_key,harness,native_id,store,title,cwd,models_json,created,updated,surface,class,class_basis,status_json,content_state FROM sessions').fetchall()
    names = ('key','harness','native_id','store','title','cwd','models_json','created','updated','surface','class','class_basis','status_json','content_state')
    nodes = {row[0]: dict(zip(names, row)) for row in rows}
    aliases = dict(conn.execute('SELECT alias,session_key FROM aliases'))
    raw = conn.execute('SELECT session_key,type,target_key,unresolved,basis,locator_json FROM edges').fetchall()
    representatives = {key:key for key in nodes}
    def root(key):
        while representatives[key] != key:
            key = representatives[key]
        return key
    for source, kind, target, unresolved, _, _ in raw:
        if kind != 'alias': continue
        resolved = _resolve(target, nodes, aliases) or _resolve(unresolved, nodes, aliases)
        if resolved and root(source) != root(resolved):
            a,b = root(source),root(resolved)
            preferred = min((a,b), key=lambda key:(not key.startswith('claude-code:'),key))
            representatives[b if preferred == a else a] = preferred
    members = collections.defaultdict(list)
    for key in nodes:
        members[root(key)].append(key)
    original_nodes = nodes
    nodes = {key:dict(original_nodes[key], _members=sorted(group),
                     _member_stores={member:original_nodes[member]['store'] for member in group})
             for key,group in members.items()}
    aliases = {alias:root(target) for alias,target in aliases.items()}
    for key in representatives:
        if root(key) != key: aliases[key] = root(key)
    # Alias edges denote identity; register aliases before resolving other edges.
    for source, kind, target, unresolved, _, _ in raw:
        if kind == 'alias':
            for value in (target, unresolved):
                if value and value not in nodes:
                    aliases.setdefault(value, root(source))
    edges, unresolved_edges = [], []
    for source, kind, target, unresolved, basis, locator in raw:
        if kind == 'alias':
            continue
        source = root(source)
        resolved = _resolve(target, nodes, aliases) or _resolve(unresolved, nodes, aliases)
        evidence = json.loads(locator) if locator else None
        if resolved is None:
            missing = target or unresolved
            unresolved_edges.append({'from': source, 'target': missing, 'type': kind, 'basis': basis,
                                     'locator': evidence, 'reason': _reason(missing, coverage)})
            continue
        if sessions.EDGE_DIRECTION.get(kind) == 'forward':
            parent, child = source, resolved
        else:
            parent, child = resolved, source
        edges.append({'from':parent, 'to':child, 'type':kind, 'basis':basis, 'locator':evidence})
    return nodes, aliases, edges, unresolved_edges


def _ancestor_keys(seeds, parents):
    ancestors, queue = set(seeds), collections.deque(seeds)
    while queue:
        for parent in sorted(parents[queue.popleft()]):
            if parent not in ancestors:
                ancestors.add(parent); queue.append(parent)
    return ancestors


def _walk(seeds, edges, depth, maximum):
    parents, children = collections.defaultdict(set), collections.defaultdict(set)
    for edge in edges:
        parents[edge['to']].add(edge['from'])
        children[edge['from']].add(edge['to'])
    ancestors = _ancestor_keys(seeds, parents)
    roots = sorted(key for key in ancestors if not (parents[key] & ancestors))
    # Seed rootless cycles as well, including those beside rooted components.
    reachable, pending = set(), collections.deque(roots)
    while len(reachable) < len(ancestors):
        if not pending:
            root = min(ancestors - reachable)
            roots.append(root); pending.append(root)
        key = pending.popleft()
        if key in reachable: continue
        reachable.add(key)
        pending.extend(sorted((children[key] & ancestors) - reachable))
    seen, ordered, queue = set(), [], collections.deque((key, 0) for key in roots)
    while queue:
        key, level = queue.popleft()
        if key in seen:
            continue
        seen.add(key)
        if depth is None or level <= depth: ordered.append(key)
        queue.extend((child, level + 1) for child in sorted(children[key] - seen))
    # Include cycle components and mandatory ancestors even if a root path misses them.
    ordered.extend(sorted(ancestors - seen))
    kept = set(ancestors)
    for key in ordered:
        if len(kept) >= maximum:
            break
        kept.add(key)
    omitted = len((seen | ancestors) - kept)
    depth_limited = bool(seen - set(ordered))
    return kept, omitted, depth_limited


def _document(conn, node, kind, last, include_evidence=False, task_text=False):
    order = 'DESC' if last else 'ASC'
    row = conn.execute('SELECT doc_id,session_key,kind,origin,ts,model,locator_json,source_key,source_rev,text '
                       'FROM docs WHERE session_key IN (SELECT value FROM json_each(?)) AND kind=? ORDER BY doc_id ' + order + ' LIMIT 1',
                       (json.dumps(node['_members']), kind)).fetchone()
    if row is None:
        return None
    text = index._task_text(row[9]) if task_text else row[9]
    if not text: return None
    result = {'_id':row[0], 'ts':row[4], 'snippet':text[:160]}
    if include_evidence:
        evidence = index._evidence(row, dict(node, store=node['_member_stores'][row[1]]), [], 1)
        result['show'] = evidence['show']
    return result


def _fact_pointer(conn, node, locator):
    if not isinstance(locator, dict):
        return {}
    path = locator.get('file') or locator.get('db')
    if not path:
        return {'locator':locator}
    for member in node['_members']:
        for key, revision, store in conn.execute('''SELECT ss.source_key,ss.fingerprint,s.store FROM session_sources ss
                JOIN sources s ON s.source_key=ss.source_key WHERE ss.session_key=?''', (member,)):
            if key == index.source_key(path) or key.startswith('session:' + path + '#') or key.startswith('state:' + path + '#'):
                if revision and not path.endswith('deliveries.jsonl') and ('line' in locator or ('table' in locator and 'key' in locator)):
                    address = json.dumps({'v':1,'session':member,'store':store,'source':key,'revision':revision,
                                          'anchor':locator}, ensure_ascii=False, separators=(',',':'))
                    return {'show':'porch sessions show --at ' + shlex.quote(address)}
    return {'locator':locator}


def _node(conn, node, aliases, has_facts, include_evidence=False, left_behind=False):
    key = node['key']
    out = {k:node[k] for k in ('key','harness','surface','class','title','cwd','created','updated')}
    if node['content_state'] != 'ok': out['content_state'] = node['content_state']
    out['models'] = json.loads(node['models_json'] or '[]')
    if include_evidence:
        out['class_basis'] = node['class_basis']
        out['aliases'] = sorted(alias for alias, target in aliases.items() if target == key)
    task = _document(conn, node, 'prompt', False, include_evidence)
    last_prompt = _document(conn, node, 'prompt', True, include_evidence)
    final = _document(conn, node, 'answer', True, include_evidence)
    if task: out['task'] = {k:v for k,v in task.items() if k != '_id'}
    if last_prompt and not left_behind and (not task or last_prompt['_id'] != task['_id']):
        out['last_prompt'] = {k:v for k,v in last_prompt.items() if k != '_id'}
    if final: out['last_final'] = {k:v for k,v in final.items() if k != '_id'}
    status = json.loads(node['status_json'] or '{}')
    porch_fields = ('status','exit_code','started_at','finished_at','backend','model','continued_from')
    if include_evidence:
        out['native_status'] = {k:v for k,v in status.items() if v is not None}
    deliveries = []
    facts = conn.execute('SELECT kind,ts,target,locator,short_text,data FROM facts WHERE session_key IN (SELECT value FROM json_each(?)) ORDER BY rowid',
                         (json.dumps(node['_members']),)) if has_facts else ()
    for kind, ts, target, locator, short_text, data in facts:
        if kind == 'porch_call':
            continue
        loc = json.loads(locator) if locator else None
        details = json.loads(data) if data else None
        if kind == 'last_turn':
            out['state'] = {'last_turn':(details or {}).get('state'), 'ts':ts}
            if (details or {}).get('state') == 'aborted': out['state']['abort_reason'] = details.get('abort_reason')
            if include_evidence:
                out['state'].update(_fact_pointer(conn, node, loc))
        elif kind == 'status_text':
            item = {'ts':ts}
            if include_evidence:
                item.update(_fact_pointer(conn,node,loc))
            if target == 'plan_items':
                items = (details or {}).get('items') or []
                item.pop('ts', None)
                item.update(total=len(items), completed=sum(x.get('status')=='completed' for x in items),
                            open=[str(x.get('text') or '')[:80] for x in items if x.get('status')!='completed'][:5])
                if (details or {}).get('closed_by'): item['closed_by'] = details['closed_by']
                if include_evidence: item['items'] = items
                out['plan'] = item
            else:
                item['snippet'] = (short_text or '')[:160]
                if target == 'task' and node['harness'] == 'porch': out['task'] = item
                elif target == 'wrap_up': out['wrap_up'] = item
                elif target == 'plan' and include_evidence: out['exit_plan'] = item
        elif kind == 'delivery': deliveries.append({'ts':ts, '_locator':loc, **(details or {})})
    if node['harness'] == 'porch':
        for field in porch_fields:
            out[field] = status.get(field)
        if include_evidence:
            out['artifacts_dir'] = status.get('artifacts_dir')
            last = deliveries[-1] if deliveries else {}
            out['deliveries'] = {'count':status.get('deliveries_count',len(deliveries)),
                                 'last_ts':last.get('ts'), 'last_via':last.get('via'),
                                 'collectors':status.get('deliveries_collectors_count',len({x.get('collector') for x in deliveries if x.get('collector')}))}
            if last: out['deliveries'].update(_fact_pointer(conn, node, last.get('_locator')))
    # Absence means the value was not recorded. Keep zero and false values:
    # they are observations, not missing metadata.
    identity = {'key','harness','surface','class','title','cwd','created','updated','models'}
    return {k:v for k,v in out.items() if k in identity or (v is not None and v != [] and v != {})}


def _summary_verdicts(nodes):
    groups = collections.defaultdict(lambda: {'true':0,'false':0,'unknown':0,'keys':[]})
    for node in sorted(nodes, key=lambda n: (-(sessions_verdicts._epoch(n.get('updated')) or 0), n['key'])):
        seen = set()
        for verdict in node.get('verdicts',[]):
            kind = verdict['kind']
            observed = verdict.get('observed',True)
            value = 'unknown' if observed == 'unknown' else 'true' if observed is True else 'false'
            if (kind,value) not in seen:
                groups[kind][value] += 1
                if node['key'] not in groups[kind]['keys']: groups[kind]['keys'].append(node['key'])
                seen.add((kind,value))
    return dict(groups)


def _compact_coverage(coverage):
    states = ('ok','pending','error','offline','unsupported','content_unavailable','stale')
    totals = {state:sum(coverage.get(h,{}).get(state,0) for h in index.UNIT_BUILDERS) for state in states}
    result = {k:v for k,v in coverage.items() if k not in index.UNIT_BUILDERS}
    for harness in index.UNIT_BUILDERS:
        entry = coverage.get(harness)
        if isinstance(entry,dict) and (any(entry.get(k,0) for k in states if k != 'ok') or entry.get('walk_complete') is False):
            result[harness] = entry
    result['totals'] = totals
    return result


def _since(value, now):
    if not value: return now-7*86400
    match=re.fullmatch(r'(\d+(?:\.\d+)?)([dhwm])', value)
    if match:
        return now-float(match.group(1))*{'d':86400,'h':3600,'w':604800,'m':60}[match.group(2)]
    return sessions.parse_time_bound(value)


def _left_candidates(conn, nodes, aliases, since, cwd, harnesses):
    conn.create_function('session_epoch',1,sessions_verdicts._epoch)
    where=["(harness='porch' OR class IN ('headless','subagent'))", 'session_epoch(updated)>=?']
    params=[since]
    if cwd is not None: where.append('instr(cwd,?)>0'); params.append(cwd)
    if harnesses is not None:
        where.append('harness IN (SELECT value FROM json_each(?))'); params.append(json.dumps(harnesses))
    rows=conn.execute('SELECT session_key FROM sessions WHERE '+' AND '.join(where)+' ORDER BY session_epoch(updated) DESC, session_key',params)
    return list(dict.fromkeys(_resolve(row[0],nodes,aliases) for row in rows))


def trace_snapshot(conn, refs, coverage, *, depth=None, max_nodes=200, left_behind=False, since=None, cwd=None, harnesses=None, now=None, evidence=False):
    index.check_normalization(conn)
    if max_nodes < 1 or (depth is not None and depth < 0):
        raise ValueError('--max-nodes must be positive and --depth non-negative')
    nodes, aliases, edges, unresolved = _graph(conn, coverage)
    engine = sessions_verdicts.Verdicts(conn,nodes,aliases,edges,unresolved,coverage,_fact_pointer,now)
    seeds = []
    for ref in refs:
        key = _resolve(ref, nodes, aliases)
        if key is None:
            harness, query = ref.split(':', 1) if ':' in ref else (None, ref)
            matches = {key for key,node in nodes.items() if (harness is None or node['harness'] == harness)
                       and query in node['native_id']}
            matches.update(target for alias,target in aliases.items() if query in alias and
                           (harness is None or alias.startswith(harness + ':')))
            if len(matches) == 1:
                key = matches.pop()
        if key is None:
            raise index.SessionIndexError('unknown session reference: ' + ref)
        if key not in seeds: seeds.append(key)
    if left_behind:
        candidates=_left_candidates(conn,nodes,aliases,_since(since,engine.now),cwd,harnesses)
        flagged=[]; cached={}; parents=collections.defaultdict(set)
        for edge in edges: parents[edge['to']].add(edge['from'])
        for key in candidates:
            if key is None: continue
            collection=engine.collection(key) if nodes[key]['harness']=='porch' else None
            verdicts=engine.verdicts(key,collection)
            cached[key] = (collection, verdicts)
            if any(v['kind'] in ('unfinished','uncollected','orphaned') and v.get('observed') in (True,'unknown') for v in verdicts):
                flagged.append(key)
        kept=set(); selected=[]
        for key in flagged:
            path=_ancestor_keys([key],parents)
            if selected and len(kept | path)>max_nodes: break
            kept.update(path); selected.append(key)
        omitted=len(_ancestor_keys(flagged,parents)-kept); depth_limited=False; seeds=selected
    else:
        kept, omitted, depth_limited = _walk(seeds, edges, depth, max_nodes)
    has_facts = sessions.table_exists(conn, 'facts')
    output_nodes = []
    for key in kept:
        if left_behind and key not in flagged:
            output_nodes.append({name:nodes[key][name] for name in ('key','harness','title','updated')})
        else:
            output_nodes.append(_node(conn, nodes[key], aliases, has_facts, evidence, left_behind))
    for node in output_nodes:
        if left_behind and node['key'] not in flagged:
            continue
        if node['harness'] == 'porch' and not node.get('task'):
            for edge in sorted((e for e in edges if e['from'] == node['key'] and e['type'] == 'worker_session'), key=lambda e:e['to']):
                task = _document(conn, nodes[edge['to']], 'prompt', False, evidence, task_text=True)
                if task:
                    node['task'] = {k:v for k,v in task.items() if k != '_id'}
                    break
        if left_behind:
            collection, node_verdicts = cached[node['key']]
        else:
            collection=engine.collection(node['key']) if node['harness']=='porch' else None
            node_verdicts=engine.verdicts(node['key'],collection)
        if collection and not evidence and len(collection['evidence']) > 1:
            collection = dict(collection)
            pointers = [item for item in collection['evidence'] if item.get('show')]
            collection['evidence'] = [pointers[-1] if pointers else collection['evidence'][-1]]
        if node['harness']=='porch': node['collection']=collection
        node['verdicts']=node_verdicts
        for verdict in ([collection] if collection else []) + node['verdicts']:
            verdict['evidence'] = verdict.get('evidence', [])[:2]
            if not evidence and verdict.get('kind') == 'unfinished' and ' plan items incomplete: ' in verdict.get('reason',''):
                verdict['reason'] = verdict['reason'].split(': ',1)[0]
        if not evidence:
            for verdict in ([collection] if collection else []) + node['verdicts']:
                for item in verdict.get('evidence', []):
                    item.pop('show', None)
                    item.pop('locator', None)
                    suffix = ' in ' + node['key']
                    if item.get('description','').endswith(suffix): item['description'] = item['description'][:-len(suffix)]
    output_nodes.sort(key=lambda n: (nodes[n['key']]['created'] is None, nodes[n['key']]['created'] or '', n['key']))
    output_edges = [edge for edge in edges if edge['from'] in kept and edge['to'] in kept]
    output_edges.sort(key=lambda edge: (edge['from'], edge['to'], edge['type'], edge['basis'] or '', json.dumps(edge['locator'], sort_keys=True)))
    output_unresolved = [edge for edge in unresolved if edge['from'] in kept]
    output_unresolved.sort(key=lambda edge: (edge['from'], edge['target'] or '', edge['type']))
    def edge_output(edge):
        result = {k:v for k,v in edge.items() if k != 'locator'}
        if evidence and edge.get('locator') is not None:
            pointer = _fact_pointer(conn, nodes[edge['from']], edge['locator'])
            if 'show' not in pointer and edge.get('to'):
                pointer = _fact_pointer(conn, nodes[edge['to']], edge['locator'])
            result.update(pointer)
        return result
    output_edges = [edge_output(edge) for edge in output_edges]
    output_unresolved = [edge_output(edge) for edge in output_unresolved]
    truncated = None
    if omitted:
        command = ['porch', 'sessions', 'trace', *refs]
        if left_behind:
            command.append('--left-behind')
            if since: command += ['--since',since]
            if cwd: command += ['--cwd',cwd]
            if harnesses: command += ['-a',','.join(harnesses)]
        if depth is not None: command += ['--depth', str(depth + 1 if depth_limited else depth)]
        if evidence: command.append('--evidence')
        command += ['--max-nodes', str(max(max_nodes * 2, max_nodes + omitted))]
        truncated = {'omitted_nodes':omitted, 'next':shlex.join(command)}
    return {'nodes':output_nodes, 'edges':output_edges, 'unresolved':output_unresolved,
            '_summary':{'seeds':seeds, 'coverage':_compact_coverage(coverage), 'truncated':truncated, 'verdicts':_summary_verdicts(output_nodes)}}


def cmd_trace(args):
    if not math.isfinite(args.deadline) or args.deadline < 0:
        raise ValueError('--deadline must be a finite non-negative number')
    if args.max_nodes < 1 or (args.depth is not None and args.depth < 0):
        raise ValueError('--max-nodes must be positive and --depth non-negative')
    left_behind=getattr(args,'left_behind',False)
    if left_behind == bool(args.refs):
        raise ValueError('supply REF or --left-behind, but not both')
    if not left_behind and any(getattr(args,k,None) for k in ('since','cwd','agent')):
        raise ValueError('--since, --cwd and --agent require --left-behind')
    directory = index.index_dir()
    sync_result = index.sync(directory, deadline=time.monotonic() + args.deadline)
    if not sync_result['synced'] and not (directory / 'index-v1.sqlite').exists():
        raise index.SessionIndexError('no committed session index; run porch sessions index build')
    with index._checked_reader(directory) as conn:
        conn.execute('BEGIN')
        coverage = index._snapshot_coverage(conn, sync_result)
        harnesses=sessions.resolve_harnesses(args.agent) if getattr(args,'agent',None) else None
        result = trace_snapshot(conn, args.refs, coverage, depth=args.depth, max_nodes=args.max_nodes,
                                left_behind=left_behind,since=getattr(args,'since',None),cwd=getattr(args,'cwd',None),harnesses=harnesses,
                                evidence=getattr(args,'evidence',False))
    sessions.emit(result)
    return sessions.EXIT_OK
