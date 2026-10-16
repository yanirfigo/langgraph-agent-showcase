"""Build a self-contained labelling page from postings JSONL (one Posting per line).

    uv run python tools/label_page.py data/pool.jsonl data/label.html

The page runs offline in a browser, keeps progress in localStorage, and "Save labels" downloads
labels.json: [{"key", "label": "fit"|"maybe"|"no", "note"}]. Nothing is sent anywhere.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from job_copilot.ingest.models import Posting

PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'">
<title>Label postings</title>
<style>
:root{--bg:#fafaf8;--fg:#1d1d1b;--mut:#6b6b66;--line:#d9d8d2;--acc:#1f5fbf;--fit:#1e7a3c;--may:#9a6a00;--no:#a3302a}
@media (prefers-color-scheme:dark){:root{--bg:#161615;--fg:#ecebe6;--mut:#9b9a94;--line:#34332f;--acc:#7aa7ff;--fit:#5cc27e;--may:#e0b450;--no:#ef7b73}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.45 system-ui,sans-serif}
header{position:sticky;top:0;background:var(--bg);border-bottom:1px solid var(--line);padding:12px 16px;display:flex;gap:12px;align-items:center;flex-wrap:wrap;z-index:1}
h1{font-size:17px;margin:0 auto 0 0}#count{font-family:ui-monospace,monospace;color:var(--mut)}
button{font:inherit;border:1px solid var(--line);background:transparent;color:var(--fg);padding:6px 12px;border-radius:3px;cursor:pointer}
button:focus-visible{outline:2px solid var(--acc);outline-offset:1px}
main{max-width:860px;margin:0 auto;padding:8px 16px 64px}
.help{color:var(--mut);font-size:13px;margin:10px 0 4px}
.card{border:1px solid var(--line);border-radius:3px;padding:12px;margin:10px 0}
.card.cur{border-color:var(--acc)}.t{font-weight:600}.m{color:var(--mut);font-size:13px}
.row{display:flex;gap:8px;margin-top:8px;flex-wrap:wrap;align-items:center}
.b-fit[aria-pressed=true]{background:var(--fit);border-color:var(--fit);color:#fff}
.b-maybe[aria-pressed=true]{background:var(--may);border-color:var(--may);color:#fff}
.b-no[aria-pressed=true]{background:var(--no);border-color:var(--no);color:#fff}
input{flex:1;min-width:160px;font:inherit;padding:5px 8px;border:1px solid var(--line);border-radius:3px;background:transparent;color:var(--fg)}
a{color:var(--acc)}
</style></head><body>
<header><h1>Label postings</h1><span id="count"></span><button id="save">Save labels</button></header>
<main>
<p class="help">For each posting: <b>Fit</b> = you would want to apply, <b>Maybe</b> = borderline, <b>Not for me</b>.
Keys: <kbd>1</kbd> Fit, <kbd>2</kbd> Maybe, <kbd>3</kbd> Not for me, <kbd>j</kbd>/<kbd>k</kbd> next/previous.
A note (optional) helps the scorer learn why. Progress is kept in this browser; click <b>Save labels</b> at the end.</p>
<div id="list"></div></main>
<script type="application/json" id="data">__DATA__</script>
<script>
(function(){
"use strict";
var items=JSON.parse(document.getElementById("data").textContent), STORE="label:"+__BATCH__;
var state={}; try{state=JSON.parse(localStorage.getItem(STORE)||"{}")}catch(e){}
var list=document.getElementById("list"), cur=0, cards=[];
function persist(){try{localStorage.setItem(STORE,JSON.stringify(state))}catch(e){} count()}
function count(){var n=items.filter(function(it){return state[it.key]&&state[it.key].label}).length;
 document.getElementById("count").textContent=n+" / "+items.length+" labelled"}
function el(tag,cls,text){var e=document.createElement(tag);if(cls)e.className=cls;if(text!=null)e.textContent=text;return e}
items.forEach(function(it,i){
 var c=el("section","card"); c.setAttribute("aria-label",it.title);
 c.appendChild(el("div","t",it.title));
 c.appendChild(el("div","m",[it.company||"(no company)",it.city,(it.roles||[]).join(", "),it.source].filter(Boolean).join(" · ")));
 var a=el("a",null,"open posting"); a.href=it.url; a.target="_blank"; a.rel="noopener noreferrer"; c.appendChild(a);
 var row=el("div","row"), btns={};
 [["fit","Fit"],["maybe","Maybe"],["no","Not for me"]].forEach(function(p){
  var b=el("button","b-"+p[0],p[1]); b.type="button"; b.setAttribute("aria-pressed","false");
  b.onclick=function(){set(i,p[0])}; btns[p[0]]=b; row.appendChild(b)});
 var note=el("input"); note.placeholder="why (optional)"; note.setAttribute("aria-label","note for "+it.title);
 note.value=(state[it.key]&&state[it.key].note)||"";
 note.oninput=function(){state[it.key]=Object.assign({},state[it.key],{note:note.value});persist()};
 row.appendChild(note); c.appendChild(row); list.appendChild(c);
 c.onclick=function(){focus(i,false)}; cards.push({c:c,btns:btns,note:note}); paint(i)});
function paint(i){var l=state[items[i].key]&&state[items[i].key].label;
 Object.keys(cards[i].btns).forEach(function(k){cards[i].btns[k].setAttribute("aria-pressed",String(k===l))})}
function set(i,l){state[items[i].key]=Object.assign({},state[items[i].key],{label:l});persist();paint(i);
 if(i===cur&&i<items.length-1)focus(i+1,true)}
function focus(i,scroll){cards[cur].c.classList.remove("cur");cur=i;cards[i].c.classList.add("cur");
 if(scroll)cards[i].c.scrollIntoView({block:"center",behavior:"smooth"})}
document.addEventListener("keydown",function(e){if(e.target.tagName==="INPUT")return;
 if(e.key==="1")set(cur,"fit");else if(e.key==="2")set(cur,"maybe");else if(e.key==="3")set(cur,"no");
 else if(e.key==="j"&&cur<items.length-1)focus(cur+1,true);else if(e.key==="k"&&cur>0)focus(cur-1,true)});
document.getElementById("save").onclick=function(){
 var out=items.filter(function(it){return state[it.key]&&state[it.key].label}).map(function(it){
  return {key:it.key,label:state[it.key].label,note:state[it.key].note||""}});
 var blob=new Blob([JSON.stringify({batch:__BATCH__,labels:out},null,1)],{type:"application/json"});
 var a=document.createElement("a");a.href=URL.createObjectURL(blob);a.download="labels.json";a.click();
 setTimeout(function(){URL.revokeObjectURL(a.href)},1000)};
focus(0,false);count();
})();
</script></body></html>
"""


def main(src: Path, dst: Path) -> None:
    rows = []
    for line in src.read_text(encoding="utf-8").splitlines():
        if line.strip():
            p = Posting.model_validate_json(line)
            rows.append(
                {
                    "key": p.key,
                    "title": p.title,
                    "company": p.company,
                    "city": p.city,
                    "roles": list(p.roles),
                    "source": p.source,
                    "url": p.url,
                }
            )
    # "<" escaped so posting text can never close the data <script> block
    data = json.dumps(rows, ensure_ascii=False).replace("<", "\\u003c")
    batch = json.dumps(src.stem)
    dst.write_text(PAGE.replace("__DATA__", data).replace("__BATCH__", batch), encoding="utf-8")
    print(f"{len(rows)} postings -> {dst}")


if __name__ == "__main__":
    main(Path(sys.argv[1]), Path(sys.argv[2]))
