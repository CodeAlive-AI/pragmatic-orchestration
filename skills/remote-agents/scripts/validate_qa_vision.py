"""Validate the agent's vision smoke-test events against ground truth.

Companion to verify-qa-vision.py. Asserts the worker saw real images (native
image deliveries, exact capture calls) and answered only from them.
"""
import json
import os
from pathlib import Path

MCP_NAME = os.environ.get('QA_MCP_NAME', 'windows-qa')
MODEL = os.environ.get('QA_AGENT_MODEL', 'grok-4.7')

def validate(root):
    root=Path(root)
    expected=json.loads((root/'expected.json').read_text())
    events=[json.loads(line) for line in (root/'agent-events.jsonl').read_text(encoding='utf-8').splitlines() if line.strip()]
    result=next(e for e in reversed(events) if e.get('type')=='result')
    assert not result.get('is_error') and result.get('stop_reason')=='end_turn',result
    answer=result['result'].strip()
    if answer.startswith('```'):answer=answer.split('\n',1)[1].rsplit('```',1)[0]
    answer=json.loads(answer)
    assert answer['top_code']==expected['top_code']
    assert answer['detail_code']==expected['detail_code']
    shapes=[{'shape':s,'color':expected[k]} for s,k in [('circle','left_color'),('triangle','middle_color'),('square','right_color')]]
    assert answer['shapes_left_to_right']==shapes
    assert answer['images_seen']==2 and not answer.get('limitation')
    captures=[];image_deliveries=0;reads=[]
    for event in events:
        for block in event.get('message',{}).get('content',[]):
            if block.get('type')=='tool_use':
                name=block['name'];args=block.get('input',{})
                assert name in ('read_file','search_tool','use_tool'),name
                if name=='read_file':
                    path=args['target_file'].replace('\\','/').replace('//','/').lower()
                    assert path.endswith('/skill.md'),path
                    reads.append(path)
                if name=='use_tool':
                    tool=args['tool_name']
                    allowed=(f'{MCP_NAME}__qa_refresh_and_list_windows',
                             f'{MCP_NAME}__select_application_window',
                             f'{MCP_NAME}__qa_view_screenshot')
                    assert tool in allowed,tool
                    if tool.endswith('__qa_view_screenshot'):captures.append(args['tool_input'])
            if block.get('type')=='tool_result' and isinstance(block.get('content'),str):
                content=block['content']
                if '[image content will be provided separately]' in content:image_deliveries+=1
    assert captures==[{'scope':'window'},{'scope':'window','region':[450,285,330,180]}],captures
    assert image_deliveries==2,image_deliveries
    transport=json.loads((root/'transport-report.json').read_text())
    assert transport['ok']
    report=dict(ok=True,model=MODEL,session_id=result['session_id'],answer=answer,
        image_deliveries=image_deliveries,captures=captures,only_instruction_file_read=True,
        transport=transport,duration_ms=result['duration_ms'])
    (root/'verified-report.json').write_text(json.dumps(report,indent=2),encoding='utf-8')
    return report
if __name__=='__main__':
    import sys
    default=os.environ.get('QA_CHECK_ROOT', os.environ.get('SystemDrive','C:') + r'\Work\qa-vision-check')
    print(json.dumps(validate(sys.argv[1] if len(sys.argv)>1 else default),indent=2))
