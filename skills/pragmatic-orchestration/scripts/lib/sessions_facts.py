"""Bounded extraction of trace evidence from streamed tool fragments."""
from __future__ import annotations

import json
import re
from collections import OrderedDict

TARGET = re.compile(r'(?<![\w-])(?:run_[A-Za-z0-9_-]+|run-delegate-[A-Za-z0-9_-]+)')
OP = re.compile(r'\bdelegate(?:\s+|["\x27]+\s*,?\s*["\x27]*)(wait-any|wait|watch|events|status|result|cancel|steer)\b')
DELEGATE = re.compile(r'(?:\$\{?PORCH\}?|(?<![\w-])porch|/[^\s"\x27]*porch)["\x27]*\s+delegate\b')
OUTPUT_FILE = re.compile(r'(?i)(?:output[_ ]file|output_file_path)["\x27\s:=]+([^\s"\x27,}]+)')
OUTPUT_WRITTEN = re.compile(r'(?i)output[^\n]{0,80}written\s+to["\x27\s:=]+([^\s"\x27,}]+)')
TASK_ID = re.compile(r'(?i)running in background with ID:\s*([A-Za-z0-9_-]{6,})')
OUTPUT_RUN = re.compile(r'^[ \t]*(?:\[porch\][ \t]+)?(?:done|wait|watch|steer|event|delegate|start)[ \t]+run_id=(run_[A-Za-z0-9_-]+)\b', re.MULTILINE)
JSON_RUN = re.compile(r'^[ \t]*(?:\{[ \t]*)?"run_id"[ \t]*:[ \t]*"(run_[A-Za-z0-9_-]+)"', re.MULTILINE)
DETACHED_RUN = re.compile(r'^[ \t]*(?:\[porch\][ \t]+stage=delegate[ \t]+detached[ \t]+)?run_id=(run_[A-Za-z0-9_-]+)[ \t]*$', re.MULTILINE)
READER = re.compile(r'(?:^|[;|&\n(])\s*(?:[\w./-]*/)?(?:cat|head|tail|sed|less|more|bat|awk|jq)\b[^;|&\n]*final\.txt\b')
SCRIPT_READER = re.compile(r'(?:^|[;|&\n(])\s*(?:[\w./-]*/)?(?:python(?:3(?:\.\d+)?)?|node)\b[^;|&\n]*(?:open|readFile)[^;|&\n]*final\.txt\b')


def _targets(text):
    result = []
    for match in TARGET.finditer(text or ''):
        value = match.group()
        if value in ('run_id', 'run_dir', 'run_status', 'run_started', 'run_finished', 'run_completed', 'run_in_background'):
            continue
        if value not in result:
            result.append(value)
            if len(result) == 20:
                break
    return result


def _output_targets(text, allow_json, detached=False):
    """Read porch progress fields and whole JSON records, not answer prose."""
    found = [match.group(1) for _,match in zip(range(20), OUTPUT_RUN.finditer(text))]
    if detached:
        found.extend(match.group(1) for _,match in zip(range(20), DETACHED_RUN.finditer(text)))
    if allow_json:
        found.extend(match.group(1) for _,match in zip(range(20), JSON_RUN.finditer(text)))
    def add_json(value):
        if isinstance(value, dict):
            target = value.get('run_id')
            if isinstance(target, str) and re.fullmatch(r'run_[A-Za-z0-9_-]+', target): found.append(target)
    if allow_json and len(text) <= 1024 * 1024:
        try: add_json(json.loads(text))
        except (ValueError, TypeError): pass
    for line_match in re.finditer(r'[^\r\n]+', text) if allow_json else ():
        if line_match.end() - line_match.start() > 1024 * 1024: continue
        line = line_match.group()
        if not line.lstrip().startswith('{'): continue
        try: add_json(json.loads(line))
        except (ValueError, TypeError): pass
        if len(found) >= 20: break
    return found[:20]


def _obj(text):
    try:
        value = json.loads(text or '{}')
        return value if isinstance(value, dict) else {}
    except (ValueError, TypeError):
        return {}


def _items(values):
    if not isinstance(values, list):
        return []
    result = []
    for item in values[:50]:
        if isinstance(item, str):
            text, status = item, 'pending'
        elif isinstance(item, dict):
            text = item.get('text') or item.get('step') or item.get('content') or item.get('subject') or ''
            status = item.get('status') or 'pending'
        else:
            continue
        result.append({'text': str(text)[:200], 'status': status if status in ('pending','in_progress','completed') else 'unknown'})
    return result


class FactCollector:
    def __init__(self):
        self.status = {}
        self.plan = []
        self.plan_tool = None
        self.pending = {}
        self.background = OrderedDict()
        self.calls = []

    def _status(self, target, fragment, text=None, data=None):
        self.status[target] = {'kind':'status_text', 'target':target, 'ts':fragment.get('ts'),
                               'locator':fragment.get('locator'), 'short_text':str(text)[:1000] if text is not None else None,
                               'data':data}

    def _plan(self, name, args, fragment):
        self.plan_tool = name
        if name == 'mcp__ccd_turn__update_plan':
            if 'items' in args: self.plan = _items(args['items'])
            if args.get('advance'):
                current = next((i for i,x in enumerate(self.plan) if x['status']=='in_progress'), None)
                if current is not None: self.plan[current]['status']='completed'
                nxt = next((x for x in self.plan if x['status']=='pending'), None)
                if nxt: nxt['status']='in_progress'
            for n in args.get('done') or []:
                if isinstance(n,int) and 1<=n<=len(self.plan): self.plan[n-1]['status']='completed'
            n=args.get('start')
            if isinstance(n,int) and 1<=n<=len(self.plan): self.plan[n-1]['status']='in_progress'
        else:
            self.plan = _items(args.get('todos') if name=='TodoWrite' else args.get('plan'))
        self._status('plan_items', fragment, data={'items':[dict(x) for x in self.plan]})

    def _call(self, fragment):
        value = _obj(fragment.get('text'))
        name = value.get('name') or ''
        args = value.get('input',value.get('arguments'))
        if isinstance(args,str):
            raw = args
            try: args=json.loads(args)
            except ValueError: args={'command':raw}
        if not isinstance(args,dict): args={}
        if name == 'mcp__ccd_turn__wrap_up':
            self._status('wrap_up', fragment, args.get('recap'))
            if self.plan_tool == 'mcp__ccd_turn__update_plan':
                current = next((item for item in self.plan if item['status'] == 'in_progress'), None)
                if current is not None:
                    current['status'] = 'completed'
                self._status('plan_items', fragment,
                             data={'items': [dict(item) for item in self.plan], 'closed_by': 'wrap_up'})
        elif name == 'ExitPlanMode': self._status('plan',fragment,args.get('plan'))
        elif name in ('mcp__ccd_turn__update_plan','TodoWrite','update_plan'): self._plan(name,args,fragment)
        cid=(fragment.get('rel') or {}).get('call_id')
        if not cid: return
        raw = str(args.get('command') or args.get('cmd') or args.get('file_path') or json.dumps(args,ensure_ascii=False))
        reads_background = (name in ('BashOutput','TaskOutput','Read','write_stdin','wait') or
                            (name in ('Bash','exec','exec_command','shell','local_shell') and
                             bool(re.search(r'\b(cat|head|tail|sed|less)\b', raw))))
        if reads_background:
            for token, original in self.background.items():
                if token in raw and re.search(r'(?<![A-Za-z0-9_-])' + re.escape(token) + r'(?![A-Za-z0-9_-])',raw):
                    self.pending[cid] = {'followup':original, 'locator':fragment.get('locator')}
                    return
        if name not in ('Bash','Read','exec','exec_command','shell','local_shell'): return
        read = 'final.txt' in raw
        delegation = bool(DELEGATE.search(raw))
        if not delegation and not read: return
        match = OP.search(raw)
        # A path mention is collection evidence only for a Read tool or a
        # plainly identifiable reader program operating on that path.
        reading = read and (name == 'Read' or bool(READER.search(raw) or SCRIPT_READER.search(raw)))
        op = match.group(1) if match else 'delegate' if delegation else 'read-final' if reading else 'other'
        self.pending[cid] = {'op':op, 'quiet':'--quiet' in raw, 'json':'--json' in raw,
                             'detach':'--detach' in raw, 'background':bool(args.get('run_in_background')),
                             'call_ts':fragment.get('ts'), 'locator':fragment.get('locator'), 'targets':_targets(raw)}

    def _result(self, fragment):
        cid=(fragment.get('rel') or {}).get('call_id')
        call=self.pending.pop(cid,None)
        if call is None: return
        output=fragment.get('text') or ''
        if not isinstance(output,str): output=json.dumps(output,ensure_ascii=False)
        original=call.get('followup')
        item=dict(original or call)
        output_targets = ([] if item.get('op') in ('read-final','other') else
                          _output_targets(output, item.get('json') or item.get('op') in ('events','wait-any'),
                                          item.get('op') == 'delegate' and item.get('detach')))
        targets=list(dict.fromkeys(item.pop('targets',[]) + output_targets))[:20]
        locator=call.get('locator') if original else item.pop('locator',None)
        item.pop('locator',None)
        item['result_ts']=fragment.get('ts')
        item['result_locator']=fragment.get('locator')
        item['is_error']=bool(fragment.get('error'))
        if original: item['via']='background_output'
        if item.get('background') and not original:
            for pattern in (OUTPUT_FILE,OUTPUT_WRITTEN,TASK_ID):
                for match in pattern.finditer(output):
                    token = match.group(1)
                    if len(token) < 6: continue
                    self.background.pop(token, None)
                    self.background[token] = dict(call)
                    if len(self.background) > 1000: self.background.popitem(last=False)
        for target in targets or [None]:
            self.calls.append({'kind':'porch_call','target':target,'ts':fragment.get('ts'),
                               'locator':locator,'data':dict(item)})

    def stream(self, fragments):
        for fragment in fragments:
            kind=fragment.get('kind')
            tool=(fragment.get('rel') or {}).get('tool')
            if kind=='tool_call':
                text=fragment.get('text') or ''
                status_tool = tool in ('mcp__ccd_turn__wrap_up','mcp__ccd_turn__update_plan','TodoWrite','ExitPlanMode','update_plan')
                command_tool = tool in ('Bash','Read','BashOutput','TaskOutput','exec','exec_command','shell','local_shell','write_stdin','wait')
                may_read = tool in ('Read','BashOutput','TaskOutput','write_stdin','wait') or any(word in text for word in ('cat ','head ','tail ','sed ','less '))
                if status_tool or (command_tool and isinstance(text,str) and
                        ('delegate' in text or 'final.txt' in text or (may_read and any(token in text for token in self.background)))):
                    self._call(fragment)
            elif kind=='tool_result': self._result(fragment)
            if kind not in ('prompt','assistant') and fragment.get('native_type') != 'event_msg.user_message':
                fragment.pop('text',None)
            yield fragment

    def facts(self):
        outstanding = []
        for call in self.pending.values():
            original = call.get('followup')
            data = dict(original or call)
            targets = data.pop('targets', [])
            locator = call.get('locator') if original else data.get('locator')
            data.pop('locator', None)
            data.update(result_ts=None, result_locator=None, is_error=None)
            if original: data['via'] = 'background_output'
            for target in targets or [None]:
                outstanding.append({'kind':'porch_call','target':target,'ts':None,'locator':locator,'data':dict(data)})
        return list(self.status.values()) + self.calls + outstanding
