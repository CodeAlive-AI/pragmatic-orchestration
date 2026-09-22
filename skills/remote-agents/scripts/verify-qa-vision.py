"""Image-native vision smoke test for the Windows QA MCP driver.

Runs on the Windows host inside the interactive desktop. Draws a random
fixture window, checks the MCP transport directly (native image content,
exact region crops, invalid-region rejection), then asks the agent to answer
visual questions that are only answerable by actually seeing the images.

Environment overrides:
  QA_CHECK_ROOT   run directory (default <SystemDrive>/Work/qa-vision-check)
  QA_PYTHON       python for fixture + MCP server (required)
  QA_AGENT_BIN    agent CLI binary (default %USERPROFILE%/.grok/bin/grok.exe)
  QA_AGENT_MODEL  agent model id (default grok-4.7)
  QA_MCP_SERVER   absolute path of the QA MCP server script (required)
  QA_MCP_NAME     MCP server name / tool prefix (default windows-qa)
  QA_MCP_ENV      optional JSON dict of extra env for the MCP server
"""
import os,sys,json,subprocess,secrets,time,asyncio,base64,io
from pathlib import Path

ROOT = Path(os.environ.get('QA_CHECK_ROOT', os.environ['SystemDrive'] + r'\Work\qa-vision-check'))
ROOT.mkdir(exist_ok=True)
PYTHON = os.environ.get('QA_PYTHON') or sys.executable
GROK = os.environ.get('QA_AGENT_BIN', str(Path.home() / '.grok' / 'bin' / 'grok.exe'))
MODEL = os.environ.get('QA_AGENT_MODEL', 'grok-4.7')
MCP_SERVER = os.environ.get('QA_MCP_SERVER')
MCP_NAME = os.environ.get('QA_MCP_NAME', 'windows-qa')
MCP_ENV = json.loads(os.environ.get('QA_MCP_ENV', '{}'))
WINDOW_TITLE = 'RemoteAgents Vision Probe'

if not MCP_SERVER:
    raise SystemExit('Set QA_MCP_SERVER to the QA MCP server script path')

if '--fixture' in sys.argv:
    import tkinter as tk
    from PIL import Image,ImageDraw,ImageFont,ImageTk
    expected=json.loads((ROOT/'expected.json').read_text())
    image=Image.new('RGB',(800,480),'#f8f6ee');d=ImageDraw.Draw(image)
    font=ImageFont.truetype(r'C:\Windows\Fonts\consolab.ttf',36)
    small=ImageFont.truetype(r'C:\Windows\Fonts\consolab.ttf',24)
    d.text((40,25),expected['top_code'],font=font,fill='#192333')
    d.ellipse((70,120,190,240),fill=expected['left_color'])
    d.polygon([(390,110),(320,240),(460,240)],fill=expected['middle_color'])
    d.rectangle((590,120,710,240),fill=expected['right_color'])
    d.rectangle((460,285,770,445),outline='#192333',width=4)
    d.text((485,315),expected['detail_code'],font=small,fill='#192333')
    d.text((485,365),'DETAIL',font=small,fill='#192333')
    root=tk.Tk();root.title(WINDOW_TITLE);root.geometry('800x480+80+80');root.resizable(False,False)
    root.attributes('-topmost',True)
    photo=ImageTk.PhotoImage(image);label=tk.Label(root,image=photo,borderwidth=0);label.pack()
    root.after(360000,root.destroy);root.mainloop();sys.exit()

status_path=ROOT/'status.json'
def status(**kwargs):
    status_path.write_text(json.dumps(kwargs),encoding='utf-8')

async def transport_checks(pid):
    from fastmcp import Client
    from PIL import Image
    server_env = dict(MCP_ENV)
    config={'mcpServers':{MCP_NAME:{'command':PYTHON,'args':[MCP_SERVER],'env':server_env}}}
    async with Client(config,timeout=45) as c:
        no_selection=await c.call_tool('qa_view_screenshot',{},raise_on_error=False)
        assert no_selection.is_error
        result=await c.call_tool('qa_refresh_and_list_windows',{})
        windows=json.loads(next(x.text for x in result.content if x.type=='text'))
        selected=next(w for w in windows if WINDOW_TITLE in str(w))
        (ROOT/'window-record.json').write_text(json.dumps(selected))
        selected_id=str(selected.get('id',selected.get('annotation_id','')))
        name=selected.get('name',selected.get('title'))
        await c.call_tool('select_application_window',{'id':selected_id,'name':name})
        metadata=[];decoded=[]
        for name,args in [('window',{}),('region',{'region':[450,285,330,180]})]:
            r=await c.call_tool('qa_view_screenshot',args)
            images=[x for x in r.content if x.type=='image']
            assert len(images)==1 and images[0].mimeType=='image/png'
            binary=base64.b64decode(images[0].data,validate=True)
            image=Image.open(io.BytesIO(binary));image.load();decoded.append(image)
            (ROOT/(name+'.png')).write_bytes(binary)
            meta=json.loads(next(x.text for x in r.content if x.type=='text'))
            assert list(image.size)==meta['image_size'];metadata.append(meta)
        assert decoded[1].size==(330,180)
        assert decoded[0].crop((450,285,780,465)).tobytes()==decoded[1].tobytes()
        for region in [[-1,0,20,20],[0,0,0,20],[0,0,99999,20],[0,0,1]]:
            r=await c.call_tool('qa_view_screenshot',{'region':region},raise_on_error=False)
            assert r.is_error,region
        report={'ok':True,'mcp_image_content':True,'crop_pixels_match':True,'invalid_regions_rejected':4,'missing_selection_rejected':True,'images':metadata}
        (ROOT/'transport-report.json').write_text(json.dumps(report))

fixture=None
try:
    colors=['red','blue','green'];secrets.SystemRandom().shuffle(colors)
    code=lambda: ''.join(secrets.choice('ABCDEFGHJKLMNPQRSTUVWXYZ23456789') for _ in range(7))
    expected=dict(top_code=code(),detail_code=code(),left_color=colors[0],middle_color=colors[1],right_color=colors[2])
    (ROOT/'expected.json').write_text(json.dumps(expected))
    fixture=subprocess.Popen([PYTHON,__file__,'--fixture'],stderr=(ROOT/'fixture-stderr.txt').open('w'))
    time.sleep(3)
    status(stage='transport_checks')
    asyncio.run(transport_checks(fixture.pid))
    prompt=f'''Perform a visual-only MCP smoke test in the Windows desktop. Use only the {MCP_NAME} MCP tools. Do not use shell, read files, OCR scripts, UI control text, subagents, or web. Discover and select the window titled {WINDOW_TITLE}. Call qa_view_screenshot(scope="window") and actually inspect its image content. Report the top random code and the left-to-right shape and color sequence. Then call qa_view_screenshot(scope="window", region=[450,285,330,180]) and inspect that cropped image; report the random code inside the lower-right detail panel. Do not guess: if you cannot see actual images explicitly state that. Return concise JSON with top_code, detail_code, shapes_left_to_right (each shape and color), images_seen, and limitation. Do not modify anything.'''
    (ROOT/'prompt.txt').write_text(prompt)
    status(stage='agent_running')
    with (ROOT/'agent-events.jsonl').open('w',encoding='utf-8') as out,(ROOT/'agent-stderr.txt').open('w',encoding='utf-8') as err:
        p=subprocess.run([GROK,'--cwd',str(ROOT),'--model',MODEL,'--no-subagents','--disable-web-search','--always-approve','--max-turns','12','--prompt-file',str(ROOT/'prompt.txt'),'--output-format','streaming-messages-json'],stdout=out,stderr=err,timeout=240)
    if p.returncode != 0:
        raise RuntimeError(f'Agent exited with {p.returncode}')
    from validate_qa_vision import validate
    validate(ROOT)
    status(stage='verified',exit_code=p.returncode)
except Exception as e:
    import traceback
    (ROOT/'failure.txt').write_text(traceback.format_exc())
    status(stage='failed',error=str(e))
    raise
finally:
    if fixture: fixture.terminate()
