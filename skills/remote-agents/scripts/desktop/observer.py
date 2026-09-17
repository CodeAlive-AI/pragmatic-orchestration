"""Optional read-only localhost screen observer. No input or execution endpoints."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import hmac
import io
import json
from pathlib import Path
import sys
import threading
import time
from urllib.parse import parse_qs, urlparse

from probe import capture

PAGE = b'''<!doctype html><meta charset="utf-8"><title>Remote agent desktop</title>
<style>body{margin:0;background:#17191c;color:#ddd;font:14px system-ui}header{padding:12px}
img{display:block;max-width:100%;margin:auto}span{color:#aaa}</style>
<header>Remote agent desktop &middot; read-only &nbsp; <span id="status">Connecting...</span></header>
<img id="screen" alt="Windows agent desktop"><script>
const screen=document.getElementById('screen'), status=document.getElementById('status');
let previous;
async function frame(){
 try{
  const response=await fetch('/frame'+location.search,{cache:'no-store',signal:AbortSignal.timeout(5000)});
  if(!response.ok)throw new Error('Desktop unavailable ('+response.status+')');
  const next=URL.createObjectURL(await response.blob());screen.src=next;
  if(previous)URL.revokeObjectURL(previous);previous=next;
  status.textContent='Updated '+new Date().toLocaleTimeString();
 }catch(e){status.textContent=e.message;screen.removeAttribute('src');}
 setTimeout(frame,750);
}frame();</script>'''


def main():
    config_path = Path(sys.argv[1])
    config = json.loads(config_path.read_text())
    config_path.unlink()
    token = config['token']
    port = int(config.get('port', 16081))
    capture_lock = threading.Lock()
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass  # URLs contain the observer token.

        def do_GET(self):
            target = urlparse(self.path)
            supplied = parse_qs(target.query).get('token', [''])[0]
            if not hmac.compare_digest(supplied, token):
                self.send_error(403)
                return
            if target.path == '/':
                data, mime = PAGE, 'text/html; charset=utf-8'
            elif target.path == '/frame':
                with capture_lock:
                    result, image = capture()
                if not result['ready']:
                    self.send_error(503, 'Windows desktop is not ready')
                    return
                buffer = io.BytesIO()
                image.save(buffer, format='JPEG', quality=80)
                data, mime = buffer.getvalue(), 'image/jpeg'
            else:
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header('Content-Type', mime)
            self.send_header('Content-Length', str(len(data)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('Referrer-Policy', 'no-referrer')
            self.send_header('X-Frame-Options', 'DENY')
            self.end_headers()
            try:
                self.wfile.write(data)
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                pass
    server = ThreadingHTTPServer(('127.0.0.1', port), Handler)
    deadline = time.monotonic() + 3600
    server.timeout = 1
    while time.monotonic() < deadline:
        server.handle_request()
    server.server_close()


if __name__ == '__main__':
    main()
