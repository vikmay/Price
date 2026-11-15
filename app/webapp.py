from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import time
from urllib.parse import parse_qsl

from aiohttp import web

MAX_AGE = 3600
TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{8,64}$")

PAGE = """<!doctype html>
<html lang="uk"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1">
<title>Сканування штрихкоду</title>
<script src="https://telegram.org/js/telegram-web-app.js"></script>
<style>
body{margin:0;font-family:system-ui,sans-serif;background:var(--tg-theme-bg-color,#fff);color:var(--tg-theme-text-color,#000);text-align:center}
video{width:100%;max-height:60vh;background:#000;object-fit:cover}
#code{font-size:22px;margin:12px;word-break:break-all}
#msg{margin:8px 12px;min-height:22px}
button{font-size:17px;margin:6px;padding:12px 20px;border:0;border-radius:10px;background:var(--tg-theme-button-color,#2481cc);color:var(--tg-theme-button-text-color,#fff)}
input{font-size:17px;padding:10px;width:70%;box-sizing:border-box}
</style></head><body>
<video id="v" playsinline muted></video>
<div id="msg">Наведіть камеру на штрихкод</div>
<div id="code"></div>
<div id="actions" hidden><button id="save">Зберегти</button><button id="again">Сканувати ще</button></div>
<div id="manual"><input id="m" placeholder="або введіть штрихкод" autocomplete="off"><button id="mb">OK</button></div>
<script>
const tg=window.Telegram.WebApp;tg.ready();tg.expand();
const token=new URLSearchParams(location.search).get("t")||"";
const v=document.getElementById("v"),msg=document.getElementById("msg"),codeEl=document.getElementById("code"),actions=document.getElementById("actions");
let stream=null,stopped=false,found="",busy=false,zx=null;
function say(t){msg.textContent=t}
function show(c){found=c;codeEl.textContent=c;actions.hidden=false;say("Перевірте штрихкод")}
async function send(c){
  if(busy)return;busy=true;say("Збереження...");
  try{
    const r=await fetch("/api/scan",{method:"POST",headers:{"Content-Type":"application/json"},
      body:JSON.stringify({initData:tg.initData,token:token,barcode:c})});
    const d=await r.json();
    say(d.message||"Помилка");
    if(d.outcome==="ok"){setTimeout(()=>tg.close(),700)}
    else if(d.outcome==="retry"){actions.hidden=true;codeEl.textContent="";again()}
    else{actions.hidden=true;setTimeout(()=>tg.close(),2500)}
  }catch(e){say("Немає зв'язку з сервером. Спробуйте ще раз.");actions.hidden=false}
  busy=false;
}
function stop(){stopped=true;if(zx){try{zx.reset()}catch(e){}zx=null}if(stream){stream.getTracks().forEach(t=>t.stop());stream=null}}
async function start(){
  stopped=false;
  try{
    stream=await navigator.mediaDevices.getUserMedia({video:{facingMode:{ideal:"environment"}},audio:false});
    v.srcObject=stream;await v.play();
  }catch(e){say("Немає доступу до камери. Введіть штрихкод вручну.");return}
  if("BarcodeDetector" in window){
    const det=new BarcodeDetector({formats:["ean_13","ean_8","upc_a","upc_e","code_128","code_39","itf"]});
    const loop=async()=>{
      if(stopped)return;
      try{const r=await det.detect(v);if(r.length&&r[0].rawValue){stop();show(r[0].rawValue);return}}catch(e){}
      setTimeout(loop,200);
    };loop();
  }else{
    const s=document.createElement("script");
    s.src="https://cdn.jsdelivr.net/npm/@zxing/library@0.21.3/umd/index.min.js";
    s.onload=()=>{
      zx=new ZXing.BrowserMultiFormatReader();
      zx.decodeFromVideoElement(v,(res)=>{if(res&&!stopped){const t=res.getText();stop();show(t)}});
    };
    s.onerror=()=>say("Сканер недоступний. Введіть штрихкод вручну.");
    document.head.appendChild(s);
  }
}
function again(){codeEl.textContent="";actions.hidden=true;say("Наведіть камеру на штрихкод");stop();start()}
document.getElementById("save").onclick=()=>send(found);
document.getElementById("again").onclick=again;
document.getElementById("mb").onclick=()=>{const c=document.getElementById("m").value.trim();if(c){stop();show(c)}};
start();
</script></body></html>
"""


def validate_init_data(init_data: str, bot_token: str, max_age: int = MAX_AGE) -> int | None:
    if not init_data or not bot_token or len(init_data) > 4096:
        return None
    try:
        pairs = dict(parse_qsl(init_data, keep_blank_values=True, strict_parsing=True))
    except ValueError:
        return None
    received = pairs.pop("hash", "")
    if not received:
        return None
    check = "\n".join(f"{k}={pairs[k]}" for k in sorted(pairs))
    secret = hmac.new(b"WebAppData", bot_token.encode("utf-8"), hashlib.sha256).digest()
    expected = hmac.new(secret, check.encode("utf-8"), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, received):
        return None
    try:
        auth_date = int(pairs.get("auth_date", "0"))
        user = json.loads(pairs.get("user", ""))
        user_id = int(user["id"])
    except (ValueError, KeyError, TypeError):
        return None
    if auth_date <= 0 or time.time() - auth_date > max_age or auth_date - time.time() > 300:
        return None
    return user_id


def add_routes(app: web.Application) -> None:
    from app.product_photos import scan_hooks

    headers = {
        "Cache-Control": "no-store",
        "Content-Security-Policy": (
            "default-src 'none'; script-src 'unsafe-inline' https://telegram.org https://cdn.jsdelivr.net; "
            "style-src 'unsafe-inline'; connect-src 'self'; media-src 'self' blob:; img-src 'self' data:"
        ),
        "Referrer-Policy": "no-referrer",
        "X-Content-Type-Options": "nosniff",
    }

    async def page(request: web.Request) -> web.Response:
        return web.Response(text=PAGE, content_type="text/html", headers=headers)

    async def submit(request: web.Request) -> web.Response:
        def reply(outcome: str, message: str, status: int = 200) -> web.Response:
            return web.json_response({"outcome": outcome, "message": message}, status=status,
                                     headers={"Cache-Control": "no-store"})

        try:
            if request.content_length is None or request.content_length > 8192:
                return reply("reset", "Некоректний запит.", 400)
            data = await request.json()
            init_data, token, code = data["initData"], data["token"], data["barcode"]
            if not all(isinstance(x, str) for x in (init_data, token, code)):
                raise TypeError
        except (ValueError, KeyError, TypeError):
            return reply("reset", "Некоректний запит.", 400)
        user_id = validate_init_data(init_data, os.getenv("TELEGRAM_BOT_TOKEN", ""))
        if user_id is None or not TOKEN_RE.match(token):
            return reply("reset", "Сеанс недійсний. Відкрийте сканер із чату ще раз.", 401)
        submit_hook = scan_hooks.get("submit")
        if submit_hook is None:
            return reply("reset", "Сканер недоступний.", 503)
        outcome, message = await submit_hook(user_id, token, code)
        return reply(outcome, message)

    app.router.add_get("/scan", page)
    app.router.add_post("/api/scan", submit)
