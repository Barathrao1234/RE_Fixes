"""
interactive_tree.py
-------------------
Reads 007_1_Demerged_Filled.xlsx and generates an interactive HTML tree diagram.
- Click a node  → expand / collapse its children
- All L1 nodes start expanded, children collapsed
- Pan by dragging, zoom with mouse wheel
- No frameworks except openpyxl for Excel reading
"""

import openpyxl
import json
import os
import html

# ─────────────────────────────────────────
# 1. READ & BUILD TREE
# ─────────────────────────────────────────
def extract_name(cell_value):
    if cell_value is None:
        return None
    s = str(cell_value)
    idx = s.find(" no_of_lines")
    return s[:idx].strip() if idx != -1 else s.strip()

def extract_lines(cell_value):
    if cell_value is None:
        return 0
    s = str(cell_value)
    idx = s.find("no_of_lines : ")
    if idx == -1:
        return 0
    try:
        return int(s[idx + 14:].strip())
    except:
        return 0

def build_tree(filepath):
    wb = openpyxl.load_workbook(filepath, read_only=True)
    ws = wb.active

    # node structure for JSON
    node_id = [0]
    def make_node(name, lines, depth):
        node_id[0] += 1
        return {"id": node_id[0], "name": name, "lines": lines,
                "depth": depth, "children": []}

    root = make_node("ROOT", 0, -1)
    last_vals  = [None] * 6
    last_lines = [0]   * 6
    first_row  = True

    def find_or_create(parent, name, lines, depth):
        for c in parent["children"]:
            if c["name"] == name:
                return c
        child = make_node(name, lines, depth)
        parent["children"].append(child)
        return child

    for row in ws.iter_rows(values_only=True):
        if first_row:
            first_row = False
            continue
        current      = list(row[:6])
        current_vals = [None] * 6
        current_ln   = [0]   * 6

        for i in range(6):
            if current[i] is not None:
                last_vals[i]  = extract_name(current[i])
                last_lines[i] = extract_lines(current[i])
                for j in range(i + 1, 6):
                    last_vals[j]  = None
                    last_lines[j] = 0
            current_vals[i] = last_vals[i]
            current_ln[i]   = last_lines[i]

        names = current_vals[:]
        while names and names[-1] is None:
            names.pop()
        if not names:
            continue

        node = root
        for depth, name in enumerate(names):
            node = find_or_create(node, name, current_ln[depth], depth)

    return root

# ─────────────────────────────────────────
# 2. GENERATE HTML
# ─────────────────────────────────────────
HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8"/>
<title>Call Tree Diagram</title>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { background: #F0F4FF; font-family: 'Segoe UI', Arial, sans-serif; overflow: hidden; }

  #toolbar {
    position: fixed; top: 0; left: 0; right: 0; height: 48px;
    background: #1E3A8A; color: #fff;
    display: flex; align-items: center; gap: 14px; padding: 0 18px;
    z-index: 100; box-shadow: 0 2px 8px #0006;
  }
  #toolbar h1 { font-size: 16px; font-weight: 700; letter-spacing: .5px; }
  #toolbar button {
    background: #2563EB; color: #fff; border: none; border-radius: 5px;
    padding: 5px 12px; cursor: pointer; font-size: 12px;
    transition: background .2s;
  }
  #toolbar button:hover { background: #1D4ED8; }
  #info {
    margin-left: auto; font-size: 12px; opacity: .8;
  }

  /* Legend */
  #legend {
    position: fixed; bottom: 16px; left: 16px;
    background: #ffffffee; border-radius: 8px;
    padding: 8px 12px; z-index: 100;
    box-shadow: 0 2px 8px #0002; font-size: 11px;
  }
  .leg-row { display: flex; align-items: center; gap: 6px; margin: 2px 0; }
  .leg-dot { width: 12px; height: 12px; border-radius: 3px; border: 2px solid; }

  /* SVG canvas */
  #canvas-wrap {
    position: fixed; top: 48px; left: 0; right: 0; bottom: 0;
    overflow: hidden; cursor: grab;
  }
  #canvas-wrap.panning { cursor: grabbing; }
  svg { position: absolute; top: 0; left: 0; }

  /* Nodes */
  .node-group { cursor: pointer; }
  .node-box {
    rx: 7px; ry: 7px;
    transition: filter .15s;
  }
  .node-group:hover .node-box { filter: brightness(0.92); }
  .node-group:hover .node-label { font-weight: 900; }

  .node-label {
    font-family: Consolas, monospace;
    font-size: 10px;
    pointer-events: none;
    dominant-baseline: middle;
    text-anchor: middle;
  }
  .badge {
    font-family: Arial;
    font-size: 9px;
    pointer-events: none;
    dominant-baseline: middle;
    text-anchor: middle;
    opacity: .75;
  }
  .expand-icon {
    font-size: 13px;
    dominant-baseline: middle;
    text-anchor: middle;
    pointer-events: none;
  }

  /* Edges */
  .edge {
    fill: none;
    stroke: #94A3B8;
    stroke-width: 1.5;
    stroke-dasharray: 5,3;
  }
  .edge-arrow {
    fill: #94A3B8;
  }
</style>
</head>
<body>

<div id="toolbar">
  <h1>📊 Call Tree Diagram</h1>
  <button onclick="expandAll()">Expand All</button>
  <button onclick="collapseAll()">Collapse All</button>
  <button onclick="resetView()">Reset View</button>
  <span id="info">Click a node to expand / collapse its children</span>
</div>

<div id="legend">
  <div class="leg-row"><div class="leg-dot" style="background:#DBEAFE;border-color:#2563EB"></div><span style="color:#2563EB">Level 1</span></div>
  <div class="leg-row"><div class="leg-dot" style="background:#D1FAE5;border-color:#059669"></div><span style="color:#059669">Level 2</span></div>
  <div class="leg-row"><div class="leg-dot" style="background:#FEF3C7;border-color:#D97706"></div><span style="color:#D97706">Level 3</span></div>
  <div class="leg-row"><div class="leg-dot" style="background:#EDE9FE;border-color:#7C3AED"></div><span style="color:#7C3AED">Level 4</span></div>
  <div class="leg-row"><div class="leg-dot" style="background:#FEE2E2;border-color:#DC2626"></div><span style="color:#DC2626">Level 5</span></div>
  <div class="leg-row"><div class="leg-dot" style="background:#CFFAFE;border-color:#0891B2"></div><span style="color:#0891B2">Level 6</span></div>
</div>

<div id="canvas-wrap">
  <svg id="svg" xmlns="http://www.w3.org/2000/svg">
    <defs>
      <marker id="arr" markerWidth="7" markerHeight="7" refX="6" refY="3" orient="auto">
        <path d="M0,0 L0,6 L7,3 z" class="edge-arrow"/>
      </marker>
    </defs>
    <g id="root-g"></g>
  </svg>
</div>

<script>
// ── DATA ─────────────────────────────────────────────────────
const TREE = TREE_DATA_PLACEHOLDER;

// ── PALETTE ──────────────────────────────────────────────────
const PAL = [
  {border:"#2563EB", fill:"#DBEAFE", text:"#1E3A8A"},
  {border:"#059669", fill:"#D1FAE5", text:"#065F46"},
  {border:"#D97706", fill:"#FEF3C7", text:"#92400E"},
  {border:"#7C3AED", fill:"#EDE9FE", text:"#4C1D95"},
  {border:"#DC2626", fill:"#FEE2E2", text:"#7F1D1D"},
  {border:"#0891B2", fill:"#CFFAFE", text:"#164E63"},
];
const pal = d => PAL[Math.min(d, PAL.length-1)];

// ── LAYOUT CONFIG ─────────────────────────────────────────────
const BOX_W  = 200;
const BOX_H  = 46;
const H_GAP  = 24;
const V_GAP  = 64;
const PAD    = 60;

// ── COLLAPSE STATE ────────────────────────────────────────────
// collapsed[id] = true means children are hidden
const collapsed = {};
// Initially: L1 nodes open, everyone else collapsed
function initCollapse(node) {
  if (node.depth === -1) {
    node.children.forEach(initCollapse);
  } else {
    // collapse depth >= 1 (L2+)
    if (node.depth >= 1) collapsed[node.id] = true;
    node.children.forEach(initCollapse);
  }
}
initCollapse(TREE);

// ── LAYOUT ───────────────────────────────────────────────────
function subtreeW(node) {
  if (collapsed[node.id] || node.children.length === 0) return BOX_W;
  const childSum = node.children.reduce((s,c) => s + subtreeW(c), 0);
  const gaps     = H_GAP * (node.children.length - 1);
  return Math.max(BOX_W, childSum + gaps);
}

const positions = {};
function assignPos(node, cx, cy) {
  positions[node.id] = {cx, cy};
  if (collapsed[node.id] || node.children.length === 0) return;
  const children = node.children;
  const widths   = children.map(subtreeW);
  const total    = widths.reduce((s,w) => s+w, 0) + H_GAP*(children.length-1);
  let x = cx - total/2;
  const childY = cy + BOX_H + V_GAP;
  children.forEach((child, i) => {
    assignPos(child, x + widths[i]/2, childY);
    x += widths[i] + H_GAP;
  });
}

function layoutAll() {
  const topChildren = TREE.children;
  const widths      = topChildren.map(subtreeW);
  const total       = widths.reduce((s,w) => s+w, 0) + H_GAP*(topChildren.length-1);
  let x = PAD;
  topChildren.forEach((child, i) => {
    assignPos(child, x + widths[i]/2, PAD);
    x += widths[i] + H_GAP;
  });
  // canvas size
  const allPos = Object.values(positions);
  const maxX   = Math.max(...allPos.map(p => p.cx + BOX_W/2)) + PAD;
  const maxY   = Math.max(...allPos.map(p => p.cy + BOX_H))   + PAD;
  return {w: maxX, h: maxY};
}

// ── SVG HELPERS ───────────────────────────────────────────────
const NS = "http://www.w3.org/2000/svg";
function el(tag, attrs, parent) {
  const e = document.createElementNS(NS, tag);
  for (const [k,v] of Object.entries(attrs)) e.setAttribute(k, v);
  if (parent) parent.appendChild(e);
  return e;
}
function txt(str, attrs, parent) {
  const e = el("text", attrs, parent);
  e.textContent = str;
  return e;
}
function wrapLabel(name, maxCh=26) {
  const parts = name.split('.');
  const lines = [];
  let cur = '';
  parts.forEach(p => {
    const trial = cur ? cur+'.'+p : p;
    if (trial.length <= maxCh) { cur = trial; }
    else { if (cur) lines.push(cur+'.'); cur = p; }
  });
  if (cur) lines.push(cur);
  return lines.length ? lines : [name.slice(0, maxCh)];
}

// ── RENDER ────────────────────────────────────────────────────
const rootG = document.getElementById('root-g');
const svgEl = document.getElementById('svg');

// We keep a map of node-id → group element for fast toggle
const nodeEls = {};
const edgeEls = {}; // parent-id_child-id → path

function clearSVG() {
  while (rootG.firstChild) rootG.removeChild(rootG.firstChild);
}

function renderNode(node, parentPos) {
  const pos = positions[node.id];
  if (!pos) return;
  const {cx, cy} = pos;
  const p = pal(node.depth);
  const isCollapsible = node.children.length > 0;
  const isCollapsed   = !!collapsed[node.id];

  // Group
  const g = el('g', {'class':'node-group', 'data-id': node.id}, rootG);
  nodeEls[node.id] = g;

  // Shadow
  el('rect', {
    x: cx - BOX_W/2 + 4, y: cy + 4,
    width: BOX_W, height: BOX_H,
    rx: 7, ry: 7,
    fill: '#CBD5E1', opacity: 0.5
  }, g);

  // Box
  el('rect', {
    'class': 'node-box',
    x: cx - BOX_W/2, y: cy,
    width: BOX_W, height: BOX_H,
    rx: 7, ry: 7,
    fill: p.fill,
    stroke: p.border,
    'stroke-width': 2
  }, g);

  // Label lines
  const lines = wrapLabel(node.name);
  const lineH = 13;
  const totalTH = lines.length * lineH;
  const startTY = cy + (BOX_H - totalTH)/2 + lineH*0.75;
  lines.forEach((line, i) => {
    txt(line, {
      'class': 'node-label',
      x: cx, y: startTY + i*lineH,
      fill: p.text
    }, g);
  });

  // Expand/collapse indicator
  if (isCollapsible) {
    const icon = isCollapsed ? '▶' : '▼';
    txt(icon, {
      'class': 'expand-icon',
      x: cx + BOX_W/2 - 10,
      y: cy + BOX_H/2,
      fill: p.border
    }, g);
  }

  // Click handler
  if (isCollapsible) {
    g.addEventListener('click', () => toggleNode(node.id));
    g.style.cursor = 'pointer';
  }

  // Edge from parent
  if (parentPos) {
    const {cx:px, cy:py} = parentPos;
    const midY = (py + BOX_H + cy) / 2;
    const d = `M ${px},${py+BOX_H} L ${px},${midY} L ${cx},${midY} L ${cx},${cy}`;
    const path = el('path', {
      'class': 'edge',
      d,
      'marker-end': 'url(#arr)'
    }, rootG);
    // insert before first node-group so edges appear behind
    rootG.insertBefore(path, rootG.firstChild);
    edgeEls[`${node.id}`] = path;
  }

  // Recurse into visible children
  if (!isCollapsed) {
    node.children.forEach(child => renderNode(child, pos));
  }
}

function render() {
  clearSVG();
  Object.keys(nodeEls).forEach(k => delete nodeEls[k]);
  Object.keys(edgeEls).forEach(k => delete edgeEls[k]);

  const {w, h} = layoutAll();
  svgEl.setAttribute('width',  w);
  svgEl.setAttribute('height', h);

  TREE.children.forEach(child => renderNode(child, null));
}

// ── TOGGLE ───────────────────────────────────────────────────
function toggleNode(id) {
  if (collapsed[id]) delete collapsed[id];
  else collapsed[id] = true;
  render();
}

function expandAll() {
  function clr(node) {
    delete collapsed[node.id];
    node.children.forEach(clr);
  }
  TREE.children.forEach(clr);
  render();
}

function collapseAll() {
  function col(node) {
    if (node.depth >= 0) collapsed[node.id] = true;
    node.children.forEach(col);
  }
  TREE.children.forEach(col);
  render();
}

// ── PAN & ZOOM ────────────────────────────────────────────────
let panX = 0, panY = 0, scale = 1;
let dragging = false, dragStartX, dragStartY, panStartX, panStartY;
const wrap = document.getElementById('canvas-wrap');

function applyTransform() {
  document.getElementById('root-g').setAttribute(
    'transform', `translate(${panX},${panY}) scale(${scale})`
  );
}

wrap.addEventListener('mousedown', e => {
  dragging = true; wrap.classList.add('panning');
  dragStartX = e.clientX; dragStartY = e.clientY;
  panStartX  = panX;      panStartY  = panY;
});
window.addEventListener('mousemove', e => {
  if (!dragging) return;
  panX = panStartX + (e.clientX - dragStartX);
  panY = panStartY + (e.clientY - dragStartY);
  applyTransform();
});
window.addEventListener('mouseup', () => {
  dragging = false; wrap.classList.remove('panning');
});
wrap.addEventListener('wheel', e => {
  e.preventDefault();
  const delta = e.deltaY < 0 ? 1.1 : 0.9;
  scale = Math.min(3, Math.max(0.1, scale * delta));
  applyTransform();
}, {passive: false});

function resetView() { panX=0; panY=0; scale=1; applyTransform(); }

// ── INIT ─────────────────────────────────────────────────────
render();
applyTransform();
</script>
</body>
</html>
"""

def generate_html(root, out_path):
    tree_json = json.dumps(root, separators=(',', ':'))
    html_out = HTML_TEMPLATE.replace('TREE_DATA_PLACEHOLDER', tree_json)
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, 'w', encoding='utf-8') as f:
        f.write(html_out)
    print(f"Saved: {out_path}")

def main():
    xlsx_path = "/mnt/user-data/uploads/007_1_Demerged_Filled.xlsx"
    out_path  = "/mnt/user-data/outputs/call_tree_interactive.html"
    print("Reading Excel …")
    root = build_tree(xlsx_path)

    def count_nodes(n):
        return 1 + sum(count_nodes(c) for c in n["children"])
    print(f"Total unique nodes: {count_nodes(root) - 1}")

    print("Generating interactive HTML …")
    generate_html(root, out_path)
    print("Done. Open call_tree_interactive.html in any browser.")

if __name__ == "__main__":
    main()
