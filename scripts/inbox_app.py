"""Email Intelligence — a refined three-pane email viewer (editorial light theme).

Self-contained: a background Pub/Sub consumer stores each received CleanEmail into
SQLite; an HTTP server serves a SPA with a Gmail-style nav rail (Inbox / Important /
Sent / All Mail), a conversation list, and a reading pane. Opening a conversation marks
it read; attachments download; new mail surfaces via badge + desktop notification.

Run it INSTEAD of showcase_dashboard.py (they consume the same subscription):
  python scripts/inbox_app.py        # then open http://localhost:5001

Reads env: PUBSUB_PROJECT_ID, CLEAN_EMAILS_SUBSCRIPTION, EMAIL_DB (default emails.db).
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlparse

from mailflow.persistence import SqliteEmailStore

STORE: SqliteEmailStore | None = None
PORT = 5001
ATTACH_DIR = "attachments"
_REF_RE = re.compile(r"[A-Fa-f0-9]{1,128}")

PAGE = r"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Email Intelligence</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Fraunces:opsz,wght@9..144,500;9..144,600&family=IBM+Plex+Sans:wght@400;500;600;700&display=swap" rel="stylesheet">
<style>
 :root{
   --ink:#1b1b1f;--ink-soft:#3f3f46;--ink-mute:#6b6b75;--ink-faint:#9a9aa3;
   --paper:#ffffff;--rail:#faf8f5;--nav:#f5f3ef;--line:#ecece8;--line-soft:#f3f2ee;
   --accent:#2b4cf0;--accent-soft:#eef1ff;--accent-ink:#1f3bd0;
   --danger:#e5484d;--good:#1f9d57;
   --display:'Fraunces','Iowan Old Style',Georgia,serif;
   --sans:'IBM Plex Sans',system-ui,-apple-system,'Segoe UI',sans-serif;
 }
 *{box-sizing:border-box}
 body{margin:0;height:100vh;display:flex;flex-direction:column;font-family:var(--sans);
      background:var(--paper);color:var(--ink);-webkit-font-smoothing:antialiased}
 header{flex:none;height:60px;padding:0 22px;display:flex;align-items:center;gap:12px;
        background:var(--paper);border-bottom:1px solid var(--line)}
 .logo{width:34px;height:34px;border-radius:9px;background:linear-gradient(135deg,#2b4cf0,#5b7bff);
       display:flex;align-items:center;justify-content:center;color:#fff;font-size:17px;
       box-shadow:0 2px 8px rgba(43,76,240,.30)}
 .brand{font-family:var(--display);font-weight:600;font-size:22px;letter-spacing:-.015em;color:var(--ink)}
 .spacer{margin-left:auto}
 .bell{background:var(--rail);border:1px solid var(--line);border-radius:10px;width:40px;height:40px;
       font-size:17px;cursor:pointer;color:var(--ink-mute);transition:.15s}
 .bell:hover{background:#f1efea;color:var(--ink)} .bell.on{background:var(--accent-soft);color:var(--accent);border-color:#dbe1ff}
 .live{display:flex;align-items:center;gap:7px;font-size:12px;font-weight:600;color:var(--good);text-transform:uppercase;letter-spacing:.06em}
 .dot{width:8px;height:8px;border-radius:50%;background:var(--good);animation:p 1.5s infinite}
 @keyframes p{0%{opacity:.35}50%{opacity:1}100%{opacity:.35}}
 .wrap{flex:1;display:flex;min-height:0}
 /* nav rail */
 .nav{width:236px;flex:none;background:var(--nav);border-right:1px solid var(--line);padding:16px 12px;overflow-y:auto}
 .navitem{display:flex;align-items:center;gap:13px;padding:11px 16px;border-radius:0 999px 999px 0;cursor:pointer;
          color:var(--ink-soft);font-size:14px;font-weight:500;margin:2px 0;transition:.12s;user-select:none}
 .navitem:hover{background:#ece9e3}
 .navitem.active{background:var(--accent-soft);color:var(--accent-ink);font-weight:700}
 .navitem .ic{font-size:17px;width:20px;text-align:center}
 .navitem .lab{flex:1}
 .navitem .num{font-size:12px;font-weight:700;color:var(--ink-mute)}
 .navitem.active .num{color:var(--accent-ink)}
 .navitem .num.zero{display:none}
 /* list */
 .list{width:404px;flex:none;overflow-y:auto;border-right:1px solid var(--line);background:var(--rail)}
 .list::-webkit-scrollbar,.pane::-webkit-scrollbar,.nav::-webkit-scrollbar{width:10px}
 .list::-webkit-scrollbar-thumb,.pane::-webkit-scrollbar-thumb{background:#dcdad4;border-radius:8px;border:3px solid transparent;background-clip:content-box}
 .lhead{padding:14px 18px 8px;font-family:var(--display);font-size:18px;color:var(--ink);font-weight:600}
 .row{display:flex;gap:12px;padding:13px 18px 13px 16px;border-bottom:1px solid var(--line-soft);cursor:pointer;position:relative;animation:rise .3s ease both}
 @keyframes rise{from{opacity:0;transform:translateY(4px)}to{opacity:1}}
 .row:hover{background:#f4f2ed} .row.sel{background:var(--paper);box-shadow:inset 3px 0 0 var(--accent)}
 .row.unread{background:var(--paper)} .row.unread::before{content:"";position:absolute;left:0;top:0;bottom:0;width:3px;background:var(--accent)}
 .av{flex:none;width:38px;height:38px;border-radius:50%;display:flex;align-items:center;justify-content:center;color:#fff;font-weight:600;font-size:15px}
 .rc{flex:1;min-width:0}
 .rtop{display:flex;align-items:center;gap:8px}
 .who{font-size:14px;color:var(--ink-soft);overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-weight:500}
 .row.unread .who{color:var(--ink);font-weight:700}
 .time{margin-left:auto;font-size:12px;color:var(--ink-faint);flex:none;font-variant-numeric:tabular-nums}
 .subj{font-size:13px;color:var(--ink-mute);overflow:hidden;text-overflow:ellipsis;white-space:nowrap;margin-top:2px}
 .row.unread .subj{color:var(--ink-soft);font-weight:600} .cnt{color:var(--accent);font-weight:600}
 .empty{padding:40px 20px;text-align:center;color:var(--ink-faint);font-size:14px}
 /* reading pane */
 .pane{flex:1;overflow-y:auto;padding:34px 44px;background:var(--paper)}
 .ph{color:var(--ink-faint);text-align:center;margin-top:14vh;font-size:15px}
 .ph .big{font-family:var(--display);font-size:26px;color:var(--ink-mute);display:block;margin-bottom:6px}
 .psubj{font-family:var(--display);font-weight:600;font-size:28px;line-height:1.25;margin:0 0 24px;color:var(--ink);letter-spacing:-.015em;max-width:760px}
 .msg{border-top:1px solid var(--line);padding:22px 0;display:flex;gap:15px;max-width:820px}
 .msg:first-of-type{border-top:none}
 .mr{flex:1;min-width:0}
 .meta{display:flex;align-items:center;gap:9px;flex-wrap:wrap}
 .seqchip{font-size:11px;font-weight:700;color:var(--ink-faint);font-variant-numeric:tabular-nums}
 .mfrom{color:var(--ink);font-size:15px;font-weight:600}
 .tag{font-size:10px;padding:2px 9px;border-radius:999px;background:var(--accent-soft);color:var(--accent-ink);text-transform:uppercase;font-weight:700;letter-spacing:.04em}
 .tag.out{background:#fdecec;color:#c5221f}
 .mtime{color:var(--ink-faint);font-size:12.5px;margin-left:auto;font-variant-numeric:tabular-nums}
 .mbody{color:var(--ink-soft);font-size:15px;margin-top:10px;white-space:pre-wrap;line-height:1.72;word-break:break-word}
 .mbody.clip{display:-webkit-box;-webkit-line-clamp:7;-webkit-box-orient:vertical;overflow:hidden}
 .more{margin-top:7px;background:none;border:none;color:var(--accent);cursor:pointer;font-size:13px;padding:0;font-weight:600;font-family:var(--sans)}
 .more:hover{text-decoration:underline}
 .atts{margin-top:14px;display:flex;flex-wrap:wrap;gap:9px}
 .att{display:inline-flex;align-items:center;gap:7px;font-size:13px;padding:8px 13px;border:1px solid var(--line);border-radius:10px;background:var(--paper);color:var(--accent-ink);text-decoration:none;font-weight:500;transition:.15s}
 .att:hover{background:var(--accent-soft);border-color:#dbe1ff} .att.nodl{color:var(--ink-mute);border-style:dashed;background:var(--rail)}
 #toast{position:fixed;bottom:26px;left:50%;transform:translateX(-50%) translateY(8px);background:var(--ink);color:#fff;padding:12px 22px;border-radius:11px;font-size:14px;font-weight:500;opacity:0;transition:.3s;pointer-events:none;box-shadow:0 8px 24px rgba(0,0,0,.22)}
 #toast.show{opacity:1;transform:translateX(-50%) translateY(0)}
</style></head><body>
<header>
  <span class="logo">✉</span><span class="brand">Email Intelligence</span>
  <span class="spacer"></span>
  <button class="bell" id="bell" title="Enable desktop notifications">🔔</button>
  <span class="live"><span class="dot"></span>Live</span>
</header>
<div class="wrap">
  <nav class="nav" id="nav">
    <div class="navitem active" data-folder="inbox"><span class="ic">📥</span><span class="lab">Inbox</span><span class="num zero" data-c="inbox">0</span></div>
  </nav>
  <div class="list" id="list"></div>
  <div class="pane" id="pane"><div class="ph"><span class="big">No conversation open</span>Select an email to read it.</div></div>
</div>
<div id="toast"></div>
<script>
 let openKey=null,openSig="",prevUnread=0,notifOn=false,ALL=[],folder='inbox',lastKey='';
 const LABELS={inbox:'Inbox',important:'Important',sent:'Sent',all:'All Mail'};
 const PAL=['#d93025','#1a73e8','#188038','#e37400','#9334e6','#0b8043','#c5221f','#1967d2','#7b1fa2','#00897b'];
 function kb(n){return n>=1024?(n/1024).toFixed(1)+' KB':(n||0)+' B';}
 function esc(s){const d=document.createElement('div');d.textContent=s||'';return d.innerHTML;}
 function tm(s){return (s||'').replace('T',' ').slice(0,16);}
 function nm(s){return (s||'?').replace(/\s*<.*/,'').replace(/["']/g,'').trim()||(s||'?');}
 function ini(s){const n=nm(s);return (n[0]||'?').toUpperCase();}
 function col(s){let h=0;for(const c of (s||'')) h=(h*31+c.charCodeAt(0))>>>0;return PAL[h%PAL.length];}
 function toast(t){const e=document.getElementById('toast');e.textContent=t;e.classList.add('show');setTimeout(()=>e.classList.remove('show'),2700);}
 function sig(th){const m=th.messages||[];const l=m[m.length-1]||{};return m.length+'|'+(l.date||'')+'|'+((l.body||'').length);}
 function pass(t){return folder==='inbox'?t.has_inbound:folder==='sent'?t.has_outbound:folder==='important'?t.unread:true;}

 const bell=document.getElementById('bell');
 bell.onclick=async()=>{if(!('Notification' in window))return toast('Notifications not supported');
   const p=await Notification.requestPermission();notifOn=(p==='granted');bell.classList.toggle('on',notifOn);
   toast(notifOn?'Desktop notifications on':'Notifications blocked');};
 if('Notification' in window && Notification.permission==='granted'){notifOn=true;bell.classList.add('on');}
 function notify(t,b){if(notifOn&&Notification.permission==='granted'){try{new Notification(t,{body:b});}catch(_){}}}

 // nav
 document.querySelectorAll('.navitem').forEach(el=>el.onclick=()=>{
   folder=el.dataset.folder;
   document.querySelectorAll('.navitem').forEach(x=>x.classList.toggle('active',x===el));
   lastKey='';renderList();
 });
 function counts(){
   const c={inbox:0,important:0,sent:0,all:0};
   for(const t of ALL){ if(!t.unread) continue;
     if(t.has_inbound)c.inbox++; c.important++; if(t.has_outbound)c.sent++; c.all++; }
   for(const k in c){ const el=document.querySelector('.num[data-c="'+k+'"]'); if(!el) continue; el.textContent=c[k]; el.classList.toggle('zero',!c[k]); }
 }
 function renderList(){
   const items=ALL.filter(pass);
   const key=folder+'|'+JSON.stringify(items); if(key===lastKey) return; lastKey=key;
   const list=document.getElementById('list'); list.innerHTML='<div class="lhead">'+LABELS[folder]+'</div>';
   if(!items.length){ list.innerHTML+='<div class="empty">Nothing here yet.</div>'; return; }
   items.forEach((t,i)=>{
     const row=document.createElement('div');
     row.className='row'+(t.unread?' unread':'')+(t.thread_key===openKey?' sel':'');
     row.style.animationDelay=Math.min(i*22,400)+'ms'; row.onclick=()=>openThread(t.thread_key);
     row.innerHTML='<div class="av" style="background:'+col(t.last_from)+'">'+esc(ini(t.last_from))+'</div>'+
       '<div class="rc"><div class="rtop"><span class="who">'+esc(nm(t.last_from))+'</span>'+
       '<span class="time">'+esc(tm(t.last_date))+'</span></div>'+
       '<div class="subj">'+esc(t.subject)+(t.count>1?' <span class="cnt">('+t.count+')</span>':'')+'</div></div>';
     list.append(row);
   });
 }

 async function loadList(){
   let data;try{data=await (await fetch('/api/threads')).json();}catch(_){return;}
   document.title=(data.unread_total?'('+data.unread_total+') ':'')+'Email Intelligence';
   if(data.unread_total>prevUnread){const n=data.unread_total-prevUnread;const f=data.threads.find(t=>t.unread)||{};
     toast(n+' new email'+(n>1?'s':''));notify('✉ '+n+' new email'+(n>1?'s':''),nm(f.last_from)+' — '+(f.subject||''));}
   prevUnread=data.unread_total; ALL=data.threads; counts(); renderList();
 }

 function renderThread(th){
   const pane=document.getElementById('pane');pane.innerHTML='';
   const h=document.createElement('h2');h.className='psubj';h.textContent=th.subject;pane.append(h);
   th.messages.forEach(m=>{
     const row=document.createElement('div');row.className='msg';
     const av=document.createElement('div');av.className='av';av.style.background=col(m.from);av.textContent=ini(m.from);
     const mr=document.createElement('div');mr.className='mr';
     const meta=document.createElement('div');meta.className='meta';
     meta.innerHTML='<span class="seqchip">#'+m.seq+'</span><span class="mfrom">'+esc(nm(m.from))+'</span>'+
       '<span class="tag'+(m.direction==='outbound'?' out':'')+'">'+esc(m.direction)+'</span>'+
       '<span class="mtime">'+esc(tm(m.date))+'</span>';
     mr.append(meta);
     const body=document.createElement('div');body.className='mbody clip';body.textContent=m.body||'(no body)';mr.append(body);
     if((m.body||'').length>360){const b=document.createElement('button');b.className='more';b.textContent='Show more';
       b.onclick=(e)=>{e.stopPropagation();const c=body.classList.toggle('clip');b.textContent=c?'Show more':'Show less';};mr.append(b);}
     if(m.attachments&&m.attachments.length){const at=document.createElement('div');at.className='atts';
       for(const a of m.attachments){const fn=a.filename||'file';
         if(a.storage_ref){const link=document.createElement('a');link.className='att';
           link.href='/file?ref='+encodeURIComponent(a.storage_ref)+'&name='+encodeURIComponent(fn)+'&type='+encodeURIComponent(a.content_type||'');
           link.download=fn;link.textContent='⬇ '+fn+' · '+kb(a.size_bytes);at.append(link);
         }else{const sp=document.createElement('span');sp.className='att nodl';sp.textContent='📎 '+fn;at.append(sp);}}
       mr.append(at);}
     row.append(av,mr);pane.append(row);
   });
 }

 async function openThread(key){
   openKey=key;
   const th=await (await fetch('/api/thread?key='+encodeURIComponent(key))).json();
   renderThread(th);openSig=sig(th);
   await fetch('/api/thread/read?key='+encodeURIComponent(key),{method:'POST'});
   lastKey='';await loadList();
 }
 async function refreshOpen(){if(!openKey)return;
   const th=await (await fetch('/api/thread?key='+encodeURIComponent(openKey))).json();
   const s=sig(th);if(s===openSig)return;openSig=s;renderThread(th);}

 setInterval(()=>{loadList();refreshOpen();},3000);loadList();
</script></body></html>"""


def _pull_loop() -> None:
    from google.cloud import pubsub_v1  # local import

    project = os.environ["PUBSUB_PROJECT_ID"]
    sub = os.environ.get("CLEAN_EMAILS_SUBSCRIPTION", "clean-email-sub")
    client = pubsub_v1.SubscriberClient()
    path = client.subscription_path(project, sub)

    def cb(message) -> None:  # noqa: ANN001
        try:
            if STORE is not None:
                STORE.save(json.loads(message.data))
        finally:
            message.ack()

    client.subscribe(path, callback=cb)
    while True:
        time.sleep(60)


class Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        p = urlparse(self.path)
        if p.path == "/api/threads":
            self._json(STORE.thread_list() if STORE else {"unread_total": 0, "threads": []})
        elif p.path == "/api/thread":
            key = (parse_qs(p.query).get("key") or [""])[0]
            self._json(STORE.thread(key) if STORE else {})
        elif p.path == "/file":
            self._serve_file(p.query)
        else:
            self._send(200, "text/html; charset=utf-8", PAGE.encode("utf-8"))

    def do_POST(self) -> None:
        p = urlparse(self.path)
        if p.path == "/api/thread/read":
            key = (parse_qs(p.query).get("key") or [""])[0]
            if STORE:
                STORE.mark_read(key)
            self._json({"unread_total": STORE.thread_list()["unread_total"] if STORE else 0})
        else:
            self._send(404, "text/plain", b"not found")

    def _serve_file(self, query: str) -> None:
        q = parse_qs(query)
        ref = (q.get("ref") or [""])[0]
        name = (q.get("name") or ["attachment"])[0]
        ctype = (q.get("type") or ["application/octet-stream"])[0]
        if not _REF_RE.fullmatch(ref):
            self._send(400, "text/plain", b"bad ref")
            return
        fpath = os.path.join(ATTACH_DIR, ref)
        if not os.path.exists(fpath):
            self._send(404, "text/plain", b"file not found")
            return
        with open(fpath, "rb") as f:
            data = f.read()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Disposition", f'attachment; filename="{name}"')
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _json(self, obj: object) -> None:
        self._send(200, "application/json", json.dumps(obj).encode("utf-8"))

    def _send(self, code: int, ctype: str, body: bytes) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a) -> None:  # noqa: ANN002
        pass


def main() -> None:
    global STORE
    STORE = SqliteEmailStore(os.environ.get("EMAIL_DB", "emails.db"))
    print(f"Email Intelligence store: {STORE.db_path} ({STORE.count()} emails)")
    threading.Thread(target=_pull_loop, daemon=True).start()
    print(f"Email Intelligence running -> open http://localhost:{PORT}  (Ctrl+C to stop)")
    HTTPServer(("127.0.0.1", PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
