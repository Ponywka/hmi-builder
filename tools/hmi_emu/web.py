"""Browser front end: the screen as a PNG, touch input, a UART log, a place to send instructions."""

from __future__ import annotations

import io
import json
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

END = b"\xff\xff\xff"


def describe(data):
    """A readable line for bytes on the UART: instructions as text, frames as hex."""
    parts = []
    for chunk in re.split(rb"(?<=\xff\xff\xff)", data):
        if not chunk:
            continue
        body = chunk[:-3] if chunk.endswith(END) else chunk
        try:
            text = body.decode("utf-8")
            if text and all(c.isprintable() or c in "\r\n\t" for c in text):
                parts.append(text + ("" if chunk.endswith(END) else " ..."))
                continue
        except UnicodeDecodeError:
            pass
        parts.append(" ".join("%02x" % b for b in body) + (" ff ff ff" if chunk.endswith(END) else ""))
    return " | ".join(parts)


class Handler(BaseHTTPRequestHandler):
    server_version = "hmi-emu"

    def log_message(self, fmt, *args):
        pass

    @property
    def emu(self):
        return self.server.emu

    def _reply(self, body, ctype="application/json", status=200):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        url = urlparse(self.path)
        q = parse_qs(url.query)
        if url.path == "/":
            return self._reply(PAGE, "text/html; charset=utf-8")
        if url.path == "/frame.png":
            buf = io.BytesIO()
            self.emu.renderer.render(self.emu.device).save(buf, "PNG")
            return self._reply(buf.getvalue(), "image/png")
        if url.path == "/state":
            return self._reply(json.dumps(self.emu.state(int(q.get("since", ["0"])[0]))))
        if url.path == "/vars":
            return self._reply(json.dumps(self.emu.variables()))
        self._reply("not found", "text/plain", 404)

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        try:
            data = json.loads(self.rfile.read(length) or b"{}")
        except ValueError:
            return self._reply('{"error":"bad json"}', status=400)
        path = urlparse(self.path).path
        try:
            if path == "/touch":
                self.emu.device.touch(int(data["x"]), int(data["y"]), data["action"])
            elif path == "/send":            # an instruction as the host would send it
                self.emu.device.feed(data["text"].encode("utf-8") + END)
            elif path == "/tx":              # raw bytes from the screen to the host (hex)
                self.emu.send_to_host(bytes.fromhex(data["hex"]))
            elif path == "/reset":
                self.emu.device.power_on()
            elif path == "/reload":
                self.emu.reload()
            else:
                return self._reply('{"error":"not found"}', status=404)
        except Exception as exc:
            return self._reply(json.dumps({"error": str(exc)}), status=400)
        self._reply("{}")


def make_server(emu, host, port):
    server = ThreadingHTTPServer((host, port), Handler)
    server.daemon_threads = True
    server.emu = emu
    return server


PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>HMI emulator</title>
<style>
:root{--bg:#15171a;--panel:#1e2125;--line:#33373d;--fg:#e6e8eb;--dim:#8b929b;--acc:#4c9aff;--rx:#7bd88f;--tx:#ffb86b;--err:#ff6b6b}
@media (prefers-color-scheme:light){:root{--bg:#f2f3f5;--panel:#fff;--line:#d5d8dd;--fg:#1d2024;--dim:#68707a;--acc:#1b66d6;--rx:#1f8a3b;--tx:#b25e00;--err:#c62828}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:14px/1.4 system-ui,sans-serif}
main{display:flex;gap:16px;padding:16px;flex-wrap:wrap;align-items:flex-start}
#screen{background:#000;border:1px solid var(--line);border-radius:6px;touch-action:none;cursor:pointer;image-rendering:auto;display:block;user-select:none;-webkit-user-drag:none}
aside{flex:1;min-width:300px;max-width:760px;display:flex;flex-direction:column;gap:10px}
.card{background:var(--panel);border:1px solid var(--line);border-radius:8px;padding:10px}
h2{margin:0 0 6px;font-size:12px;text-transform:uppercase;letter-spacing:.06em;color:var(--dim)}
#log{height:300px;overflow:auto;font:12px/1.35 ui-monospace,monospace;white-space:pre-wrap;word-break:break-all}
#log .rx{color:var(--tx)}#log .tx{color:var(--rx)}#log .err{color:var(--err)}
.row{display:flex;gap:6px;flex-wrap:wrap;align-items:center}
input,select,button{font:inherit;color:var(--fg);background:var(--bg);border:1px solid var(--line);border-radius:6px;padding:5px 8px}
input[type=text]{flex:1;min-width:140px}button{cursor:pointer}button:hover{border-color:var(--acc)}
#vars{max-height:220px;overflow:auto;font:12px ui-monospace,monospace}#vars td{padding:0 10px 0 0;vertical-align:top}
.dim{color:var(--dim)}
</style></head><body><main>
<div><img id="screen" alt="screen" draggable="false" width="272" height="480">
<div class="dim" id="info" style="margin-top:6px"></div></div>
<aside>
<div class="card"><h2>Display</h2><div class="row">
<label>Zoom <select id="zoom"><option>1</option><option selected>1.5</option><option>2</option></select></label>
<label>Page <select id="pages"></select></label><button id="go">Go</button><button id="reset">Power cycle</button><button id="reload" title="re-read the project directory">Reload project</button></div></div>
<div class="card"><h2>UART <span class="dim">(<span style="color:var(--tx)">host &rarr; display</span>, <span style="color:var(--rx)">display &rarr; host</span>)</span></h2>
<div id="log"></div><div class="row" style="margin-top:8px">
<button id="clear">Clear</button><label><input type="checkbox" id="pause"> pause</label></div></div>
<div class="card"><h2>Send as the host</h2><div class="row"><input type="text" id="cmd" placeholder='t0.txt="hello"'><button id="send">Send</button></div></div>
<div class="card"><h2>Send to the host (hex)</h2><div class="row"><input type="text" id="hex" placeholder="65 03 01 ff ff ff"><button id="sendhex">Send</button></div></div>
<div class="card"><h2>Variables</h2><div id="vars"></div></div>
</aside></main>
<script>
const $=id=>document.getElementById(id);let since=0,version=-1,lastPage=null;
const img=$('screen');
function zoom(){const z=parseFloat($('zoom').value);img.style.width=272*z+'px';img.style.height=480*z+'px'}
$('zoom').onchange=()=>{zoom();localStorage.zoom=$('zoom').value};try{if(localStorage.zoom)$('zoom').value=localStorage.zoom}catch(e){}zoom();
function post(path,obj){return fetch(path,{method:'POST',body:JSON.stringify(obj||{})}).then(r=>r.json()).catch(()=>({}))}
function pos(e){const r=img.getBoundingClientRect();return{x:Math.max(0,Math.min(271,Math.floor((e.clientX-r.left)*272/r.width))),y:Math.max(0,Math.min(479,Math.floor((e.clientY-r.top)*480/r.height)))}}
let down=false;
img.addEventListener('pointerdown',e=>{down=true;img.setPointerCapture(e.pointerId);post('/touch',{...pos(e),action:'down'});e.preventDefault()});
img.addEventListener('pointermove',e=>{if(down)post('/touch',{...pos(e),action:'move'})});
img.addEventListener('pointerup',e=>{if(down){down=false;post('/touch',{...pos(e),action:'up'})}});
img.addEventListener('pointercancel',e=>{if(down){down=false;post('/touch',{...pos(e),action:'up'})}});
$('send').onclick=()=>{const t=$('cmd').value;if(t)post('/send',{text:t})};$('cmd').onkeydown=e=>{if(e.key==='Enter')$('send').click()};
$('sendhex').onclick=()=>{const t=$('hex').value.trim();if(t)post('/tx',{hex:t.replace(/[^0-9a-fA-F]/g,'')})};$('hex').onkeydown=e=>{if(e.key==='Enter')$('sendhex').click()};
$('go').onclick=()=>post('/send',{text:'page '+$('pages').value});$('reset').onclick=()=>post('/reset');$('reload').onclick=()=>post('/reload');
$('clear').onclick=()=>{$('log').textContent=''};
function addLog(l){const d=document.createElement('div');d.className=l.dir;d.textContent=(l.dir==='rx'?'> ':l.dir==='tx'?'< ':'! ')+l.text;$('log').appendChild(d)}
async function poll(){
 try{const s=await (await fetch('/state?since='+since)).json();
  if(!$('pages').options.length){s.pages.forEach((p,i)=>{const o=document.createElement('option');o.value=i;o.textContent=i+' '+p;$('pages').appendChild(o)})}
  if(!$('pause').checked){const stick=$('log').scrollTop+$('log').clientHeight>=$('log').scrollHeight-30;s.log.forEach(addLog);if(s.log.length&&stick)$('log').scrollTop=$('log').scrollHeight;
   while($('log').childNodes.length>1500)$('log').removeChild($('log').firstChild)}
  since=s.seq;
  if(s.version!==version||s.animating){version=s.version;img.src='/frame.png?'+Date.now()}
  $('info').textContent='page '+s.page+' ('+s.pagename+')  brightness '+s.dim+'%'+(s.sleep?'  sleeping':'')+'   '+s.links.join(', ')+(s.errors?'   errors: '+s.errors+' (last: '+s.last_error+')':'');
  if(s.page!==lastPage){lastPage=s.page;$('pages').value=s.page}
 }catch(e){}
 setTimeout(poll,120)}
async function vars(){try{const v=await (await fetch('/vars')).json();let h='<table>';for(const k in v.globals)h+='<tr><td class=dim>'+k+'</td><td>'+v.globals[k]+'</td></tr>';
 h+='<tr><td colspan=2 class=dim>— page —</td></tr>';for(const k in v.objects)h+='<tr><td class=dim>'+k+'</td><td>'+v.objects[k]+'</td></tr>';$('vars').innerHTML=h+'</table>'}catch(e){}setTimeout(vars,1000)}
poll();vars();
</script></body></html>
"""
