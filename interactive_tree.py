"""
interactive_tree.py — collapsible call-tree HTML generator
- On open: only L1 (top parent) nodes visible
- Click a node → shows its direct children only
- Click again → hides them
- Node width auto-fits the text
"""

import openpyxl, json, os

# ── 1. READ EXCEL & BUILD TREE ─────────────────────────────────
def extract_name(v):
    if v is None: return None
    s = str(v); i = s.find(" no_of_lines")
    return s[:i].strip() if i != -1 else s.strip()

def extract_lines(v):
    if v is None: return 0
    s = str(v); i = s.find("no_of_lines : ")
    if i == -1: return 0
    try: return int(s[i+14:].strip())
    except: return 0

_nid = [0]
def make_node(name, lines, depth):
    _nid[0] += 1
    return {"id": _nid[0], "name": name, "lines": lines,
            "depth": depth, "children": []}

def build_tree(path):
    wb = openpyxl.load_workbook(path, read_only=True)
    ws = wb.active
    root = make_node("ROOT", 0, -1)
    last_v = [None]*6; last_l = [0]*6; first = True

    def find_or_add(parent, name, lines, depth):
        for c in parent["children"]:
            if c["name"] == name: return c
        c = make_node(name, lines, depth)
        parent["children"].append(c)
        return c

    for row in ws.iter_rows(values_only=True):
        if first: first = False; continue
        cur = list(row[:6])
        for i in range(6):
            if cur[i] is not None:
                last_v[i] = extract_name(cur[i])
                last_l[i] = extract_lines(cur[i])
                for j in range(i+1, 6): last_v[j]=None; last_l[j]=0
            cur[i] = last_v[i]
        names = cur[:]
        while names and names[-1] is None: names.pop()
        if not names: continue
        node = root
        for depth, name in enumerate(names):
            node = find_or_add(node, name, last_l[depth], depth)
    return root

# ── 2. HTML ────────────────────────────────────────────────────
HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8"/>
<title>Call Tree</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{background:#EEF2FF;font-family:'Segoe UI',Arial,sans-serif;overflow:hidden}

/* ── toolbar ── */
#toolbar{
  position:fixed;top:0;left:0;right:0;height:46px;
  background:#1E3A8A;color:#fff;
  display:flex;align-items:center;gap:10px;padding:0 16px;
  z-index:200;box-shadow:0 2px 10px #0004;
}
#toolbar h1{font-size:15px;font-weight:700;letter-spacing:.4px;margin-right:6px}
#toolbar button{
  background:#3B82F6;color:#fff;border:none;border-radius:5px;
  padding:5px 11px;cursor:pointer;font-size:12px;
}
#toolbar button:hover{background:#2563EB}
#breadcrumb{
  margin-left:auto;font-size:11px;opacity:.85;
  white-space:nowrap;overflow:hidden;text-overflow:ellipsis;max-width:400px;
}

/* ── legend ── */
#legend{
  position:fixed;bottom:12px;left:12px;
  background:#ffffffee;border-radius:8px;
  padding:7px 11px;z-index:200;
  box-shadow:0 2px 8px #0002;font-size:11px;line-height:1.8;
}
.lr{display:flex;align-items:center;gap:6px}
.ld{width:11px;height:11px;border-radius:3px;border:2px solid;flex-shrink:0}

/* ── canvas ── */
#wrap{
  position:fixed;top:46px;left:0;right:0;bottom:0;
  overflow:hidden;
}
#wrap.grabbing{cursor:grabbing}
svg{position:absolute;top:0;left:0;overflow:visible}

/* ── nodes ── */
.ngrp{cursor:pointer}
.ngrp rect.box{transition:filter .12s}
.ngrp:hover rect.box{filter:brightness(.88)}
.ngrp text{pointer-events:none}

/* ── edges ── */
.edge{fill:none;stroke:#94A3B8;stroke-width:1.8;marker-end:url(#arr)}

/* ── collapse indicator ── */
.indicator{font-size:11px;font-weight:900}
</style>
</head>
<body>
<div id="toolbar">
  <h1>📊 Call Tree</h1>
  <button onclick="expandAll()">Expand All</button>
  <button onclick="collapseAll()">Collapse All</button>
  <button onclick="resetView()">Reset View</button>
  <span id="breadcrumb">Click a node to expand its children</span>
</div>

<div id="legend">
  <div class="lr"><div class="ld" style="background:#DBEAFE;border-color:#2563EB"></div><span style="color:#2563EB">Level 1</span></div>
  <div class="lr"><div class="ld" style="background:#D1FAE5;border-color:#059669"></div><span style="color:#059669">Level 2</span></div>
  <div class="lr"><div class="ld" style="background:#FEF3C7;border-color:#D97706"></div><span style="color:#D97706">Level 3</span></div>
  <div class="lr"><div class="ld" style="background:#EDE9FE;border-color:#7C3AED"></div><span style="color:#7C3AED">Level 4</span></div>
  <div class="lr"><div class="ld" style="background:#FEE2E2;border-color:#DC2626"></div><span style="color:#DC2626">Level 5</span></div>
  <div class="lr"><div class="ld" style="background:#CFFAFE;border-color:#0891B2"></div><span style="color:#0891B2">Level 6</span></div>
</div>

<div id="wrap">
  <svg id="svg" xmlns="http://www.w3.org/2000/svg">
    <defs>
      <marker id="arr" markerWidth="8" markerHeight="8" refX="7" refY="3.5" orient="auto">
        <path d="M0,0 L0,7 L8,3.5 z" fill="#94A3B8"/>
      </marker>
    </defs>
    <g id="scene"></g>
  </svg>
</div>

<script>
// ── DATA ─────────────────────────────────────────────────────
const TREE = TREE_JSON;

// ── PALETTE ──────────────────────────────────────────────────
const PAL = [
  {b:"#2563EB",f:"#DBEAFE",t:"#1E3A8A"},
  {b:"#059669",f:"#D1FAE5",t:"#065F46"},
  {b:"#D97706",f:"#FEF3C7",t:"#92400E"},
  {b:"#7C3AED",f:"#EDE9FE",t:"#4C1D95"},
  {b:"#DC2626",f:"#FEE2E2",t:"#7F1D1D"},
  {b:"#0891B2",f:"#CFFAFE",t:"#164E63"},
];
const pal = d => PAL[Math.min(d, PAL.length-1)];

// ── LAYOUT CONSTANTS ─────────────────────────────────────────
const CHAR_W    = 6.8;      // px per character at font-size 12
const BOX_MIN_W = 120;      // minimum box width
const BOX_MAX_W = 220;      // maximum box width (caps long paths)
const LINE_H    = 16;       // px per text line inside box
const V_PAD     = 10;       // top+bottom padding inside box (each side)
const H_PAD     = 16;       // horizontal text padding inside box
const H_GAP     = 28;       // gap between sibling boxes
const V_GAP     = 72;       // vertical gap between levels

// ── COLLAPSE STATE ────────────────────────────────────────────
// open[id] = true  → children visible
const open = {};

// Build id→node map
const nodeMap = {};
function index(node){ nodeMap[node.id]=node; node.children.forEach(index); }
index(TREE);

// ── TEXT WRAPPING ─────────────────────────────────────────────
// Max chars that fit in BOX_MAX_W (minus padding)
const MAX_CHARS = Math.floor((BOX_MAX_W - H_PAD * 2) / CHAR_W);

function wrapText(name){
  // Split on common path separators and underscores for natural break points
  // but keep the token itself attached to the next segment
  if(name.length <= MAX_CHARS) return [name];
  const lines = [];
  let remaining = name;
  while(remaining.length > MAX_CHARS){
    // Find a good break point (space, _, -, .) working backwards from MAX_CHARS
    let cut = MAX_CHARS;
    for(let i = MAX_CHARS; i > MAX_CHARS - 20 && i > 0; i--){
      const ch = remaining[i];
      if(ch === '_' || ch === '-' || ch === '.' || ch === '/' || ch === ' '){
        cut = i + 1; // break after separator
        break;
      }
    }
    lines.push(remaining.slice(0, cut));
    remaining = remaining.slice(cut);
  }
  if(remaining) lines.push(remaining);
  return lines;
}

// ── BOX DIMENSIONS (width + height, wrapping-aware) ──────────
function boxDims(name){
  const lines = wrapText(name);
  const maxLine = lines.reduce((m,l)=>Math.max(m,l.length),0);
  const w = Math.min(BOX_MAX_W,
              Math.max(BOX_MIN_W, Math.ceil(maxLine * CHAR_W) + H_PAD * 2));
  const h = lines.length * LINE_H + V_PAD * 2;
  return {w, h, lines};
}

// ── LAYOUT ───────────────────────────────────────────────────
// Compute subtree width considering only currently open nodes
function subtreeW(node){
  const {w} = boxDims(node.name);
  if (!open[node.id] || node.children.length === 0) return w;
  const cw = node.children.reduce((s,c)=>s+subtreeW(c),0)
           + H_GAP*(node.children.length-1);
  return Math.max(w, cw);
}

const pos = {};   // id → {cx, cy}
function layout(node, cx, cy){
  pos[node.id] = {cx, cy};
  if (!open[node.id] || node.children.length===0) return;
  const children = node.children;
  const ws = children.map(subtreeW);
  const total = ws.reduce((s,w)=>s+w,0) + H_GAP*(children.length-1);
  let x = cx - total/2;
  const {h} = boxDims(node.name);
  const cy2 = cy + h + V_GAP;
  children.forEach((c,i)=>{ layout(c, x+ws[i]/2, cy2); x+=ws[i]+H_GAP; });
}

const PADDING = 60;
function layoutAll(){
  const roots = TREE.children;
  const ws = roots.map(subtreeW);
  const total = ws.reduce((s,w)=>s+w,0) + H_GAP*(roots.length-1);
  let x = PADDING;
  roots.forEach((r,i)=>{ layout(r, x+ws[i]/2, PADDING); x+=ws[i]+H_GAP; });
  let mx=0, my=0;
  Object.keys(pos).forEach(id=>{
    const {cx,cy}=pos[id];
    const {w,h}=boxDims(nodeMap[id].name);
    mx=Math.max(mx, cx+w/2);
    my=Math.max(my, cy+h);
  });
  return {w: mx+PADDING, h: my+PADDING};
}

// ── SVG ───────────────────────────────────────────────────────
const NS="http://www.w3.org/2000/svg";
const svgEl=document.getElementById('svg');
const scene=document.getElementById('scene');

function mkEl(tag,attrs,parent){
  const e=document.createElementNS(NS,tag);
  for(const[k,v] of Object.entries(attrs)) e.setAttribute(k,v);
  if(parent) parent.appendChild(e);
  return e;
}
function mkTxt(s,attrs,parent){
  const e=mkEl('text',attrs,parent);
  e.textContent=s; return e;
}

// ── RENDER ───────────────────────────────────────────────────
function render(){
  while(scene.firstChild) scene.removeChild(scene.firstChild);
  Object.keys(pos).forEach(k=>delete pos[k]);

  const {w,h}=layoutAll();
  svgEl.setAttribute('width',  Math.max(w, window.innerWidth));
  svgEl.setAttribute('height', Math.max(h, window.innerHeight-46));

  // Draw edges first (behind nodes)
  const edgeG = mkEl('g',{},scene);
  // Draw nodes
  const nodeG = mkEl('g',{},scene);

  function drawNode(node, parentId){
    const {cx,cy} = pos[node.id];
    const {w:bw, h:bh, lines} = boxDims(node.name);
    const p  = pal(node.depth);
    const hasKids = node.children.length > 0;
    const isOpen  = !!open[node.id];

    // Edge from parent
    if(parentId !== null && pos[parentId]){
      const {cx:px, cy:py} = pos[parentId];
      const {h:ph} = boxDims(nodeMap[parentId].name);
      const midY = py + ph + V_GAP/2;
      mkEl('path',{
        'class':'edge',
        d:`M${px},${py+ph} L${px},${midY} L${cx},${midY} L${cx},${cy}`
      }, edgeG);
    }

    // Group
    const g = mkEl('g',{'class':'ngrp'}, nodeG);
    if(hasKids) g.addEventListener('click', e=>{ e.stopPropagation(); toggle(node.id); });

    // Shadow
    mkEl('rect',{
      x:cx-bw/2+3, y:cy+3, width:bw, height:bh,
      rx:7, fill:'#C7D2FE', opacity:.5
    }, g);

    // Box
    mkEl('rect',{
      'class':'box',
      x:cx-bw/2, y:cy, width:bw, height:bh,
      rx:7, fill:p.f, stroke:p.b, 'stroke-width':2
    }, g);

    // Label — multi-line via <tspan>, centred vertically in box
    // Reserve right margin for expand indicator when node has kids
    const textX = hasKids ? cx - 6 : cx;
    const totalTextH = lines.length * LINE_H;
    const startY = cy + (bh - totalTextH) / 2 + LINE_H * 0.8; // first baseline
    const txtEl = mkEl('text',{
      x: textX,
      y: startY,
      'text-anchor':'middle',
      'font-family':'Consolas,monospace',
      'font-size':12, fill:p.t, 'font-weight':'bold'
    }, g);
    lines.forEach((line, i) => {
      const ts = document.createElementNS(NS, 'tspan');
      ts.textContent = line;
      ts.setAttribute('x', textX);
      if(i > 0) ts.setAttribute('dy', LINE_H);
      txtEl.appendChild(ts);
    });

    // Expand indicator (▶ collapsed / ▼ open) — anchored to bottom-right
    if(hasKids){
      mkTxt(isOpen?'▾':'▸',{
        x:cx+bw/2-9, y:cy+bh-V_PAD,
        'class':'indicator',
        'text-anchor':'middle','dominant-baseline':'middle',
        'font-family':'Arial', fill:p.b
      }, g);
    }

    // Recurse
    if(isOpen){
      node.children.forEach(c => drawNode(c, node.id));
    }
  }

  TREE.children.forEach(r => drawNode(r, null));
}

// ── TOGGLE ───────────────────────────────────────────────────
function toggle(id){
  if(open[id]) delete open[id];
  else open[id] = true;
  render(); applyXform();
}

function expandAll(){
  function exp(n){ open[n.id]=true; n.children.forEach(exp); }
  TREE.children.forEach(exp);
  render(); applyXform();
}
function collapseAll(){
  Object.keys(open).forEach(k=>delete open[k]);
  render(); applyXform();
}

// ── PAN + ZOOM ────────────────────────────────────────────────
let tx=0,ty=0,sc=1, drag=false, ox,oy,stx,sty;
const wrap=document.getElementById('wrap');

function applyXform(){
  scene.setAttribute('transform',`translate(${tx},${ty}) scale(${sc})`);
}
wrap.addEventListener('mousedown',e=>{
  if(e.target.closest('.ngrp')) return;
  drag=true; wrap.classList.add('grabbing');
  ox=e.clientX; oy=e.clientY; stx=tx; sty=ty;
});
window.addEventListener('mousemove',e=>{
  if(!drag) return;
  tx=stx+(e.clientX-ox); ty=sty+(e.clientY-oy); applyXform();
});
window.addEventListener('mouseup',()=>{ drag=false; wrap.classList.remove('grabbing'); });
wrap.addEventListener('wheel',e=>{
  e.preventDefault();
  sc=Math.min(4,Math.max(0.08, sc*(e.deltaY<0?1.12:0.9)));
  applyXform();
},{passive:false});
function resetView(){ tx=0;ty=0;sc=1; applyXform(); }

// ── INIT ─────────────────────────────────────────────────────
render();
applyXform();
</script>
</body>
</html>
"""

def generate_html(root, out_path):
    tree_json = json.dumps(root, separators=(',', ':'))
    out = HTML.replace('TREE_JSON', tree_json)
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, 'w', encoding='utf-8') as f:
        f.write(out)
    print(f"Saved: {out_path}  ({len(out)//1024} KB)")

def main():
    xlsx = "/mnt/user-data/uploads/007_1_Demerged_Filled.xlsx"
    out  = "/mnt/user-data/outputs/call_tree_interactive.html"
    print("Reading Excel …")
    root = build_tree(xlsx)
    def cnt(n): return 1+sum(cnt(c) for c in n["children"])
    print(f"Unique nodes: {cnt(root)-1}")
    print("Generating HTML …")
    generate_html(root, out)
    print("Done!")

if __name__ == "__main__":
    main()
