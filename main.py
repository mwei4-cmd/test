import io, math, time, tempfile, os, uuid, json
from typing import Optional
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, UploadFile, File
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
import numpy as np

import ezdxf
from ezdxf.path import make_path
from shapely.geometry import Polygon, box, Point, LineString
from shapely.affinity import rotate, translate, scale
from shapely.ops import unary_union
try:
    from shapely.geometry import JOIN_STYLE
    JS_ROUND = JOIN_STYLE.round
except ImportError:
    JS_ROUND = 1

app = FastAPI()
app.add_middleware(CORSMiddleware, allow_origins=["*"],
                   allow_methods=["*"], allow_headers=["*"])
app.mount("/static", StaticFiles(directory="static"), name="static")

# ── In-memory session store (single-user; extend with session IDs for multi-user) ──
SESSION = {
    "raw_poly": None, "geo_block_name": None, "doc_out": None,
    "best_layout": [], "best_step_w": 0, "best_step_h": 0,
    "best_count_col": 0, "best_count_row": 0,
    "final_polys_with_info": [], "stats": {},
    "interactive_lines": [], "manual_connectors": [],
    "lines_history": [],
}

# ─────────────────────────────────────────────
# Geometry helpers
# ─────────────────────────────────────────────
def get_full_polygon_with_holes(file_path, target_doc=None):
    doc = ezdxf.readfile(file_path)
    msp = doc.modelspace()
    all_polys = []
    entities = msp.query('*[layer=="BOARD_OUTLINE_00"]')
    if not entities:
        entities = msp.query('LWPOLYLINE POLYLINE CIRCLE ELLIPSE SPLINE')
    for entity in entities:
        try:
            if entity.dxftype() == 'CIRCLE':
                c = entity.dxf.center
                poly = Point(c.x, c.y).buffer(entity.dxf.radius, quad_segs=32)
            else:
                pts = list(make_path(entity).flattening(distance=0.05))
                if len(pts) < 3: continue
                poly = Polygon([(p.x, p.y) for p in pts])
                if not poly.is_valid: poly = poly.buffer(0)
            if poly.is_valid and not poly.is_empty and poly.area > 0.01:
                all_polys.append(poly)
        except: continue
    if not all_polys: raise ValueError("找不到有效輪廓")
    all_polys.sort(key=lambda p: p.area, reverse=True)
    shell = all_polys[0]
    cx, cy = shell.centroid.x, shell.centroid.y
    holes = [p.exterior.coords for p in all_polys[1:]
             if shell.buffer(0.01).contains(p)]
    raw = translate(Polygon(shell.exterior.coords, holes), -cx, -cy)
    block_name = "PART_GEO_BLOCK"
    if target_doc:
        nb = target_doc.blocks.new(name=block_name)
        for ent in msp.query('*'):
            try:
                ne = ent.copy(); ne.translate(-cx, -cy, 0); nb.add_entity(ne)
            except: pass
    return raw, block_name, (cx, cy), len(holes)

def create_rounded_panel(w, h, r):
    return box(r, r, w - r, h - r).buffer(r, quad_segs=32, join_style=JS_ROUND)

def refine_position(poly_a, poly_b, init_dx, init_dy, target_dist):
    best_dx, best_dy = init_dx, init_dy
    best_b = translate(poly_b, best_dx, best_dy)
    cb = unary_union([poly_a, best_b]).bounds
    best_area = (cb[2]-cb[0])*(cb[3]-cb[1])
    step = 1.0
    no_improve_count = 0
    while step >= 0.01:          # 精度從 0.001 放寬到 0.01，速度提升 ~3x
        improved = False
        for ddx, ddy in [(step,0),(-step,0),(0,step),(0,-step)]:
            tb = translate(poly_b, best_dx+ddx, best_dy+ddy)
            if poly_a.distance(tb) < target_dist - 0.001: continue
            cb2 = unary_union([poly_a, tb]).bounds
            a2 = (cb2[2]-cb2[0])*(cb2[3]-cb2[1])
            if a2 < best_area - 1e-4:
                best_area = a2; best_dx += ddx; best_dy += ddy; best_b = tb
                improved = True; break
        if not improved:
            step *= 0.5
            no_improve_count += 1
            if no_improve_count > 6: break   # 連續 6 次無改善則提早結束
        else:
            no_improve_count = 0
    return best_dx, best_dy, best_b, best_area

def run_nesting(raw_poly, params, progress_cb=None):
    cw, ch = params["panel_w"], params["panel_h"]
    lb, rb, ub, db = params["left"], params["right"], params["top"], params["bottom"]
    spacing = params["spacing"]
    mode = params["mode"]
    eff_w = cw - lb - rb
    eff_h = ch - ub - db
    safe = box(lb, db, cw-rb, ch-ub)

    best_layout = []; bsw=bsh=0.0; bcc=bcr=0
    candidates_top3 = []

    if "Nesting" in mode:
        target_sp = float(spacing)
        rotated = {a: rotate(raw_poly, a, origin=(0,0)) for a in [0,90,180,270]}
        ba = rotated[0]

        # ── 平行計算：每個 (ang_b, angle_deg) 組合獨立處理 ──
        from concurrent.futures import ProcessPoolExecutor, as_completed
        import functools

        tasks = []
        for ang_b, bb in rotated.items():
            for ad in range(0, 360, 10):
                tasks.append((ang_b, ad))

        total = len(tasks)
        done_count = [0]

        def compute_one(task_args):
            ang_b, ad = task_args
            bb = rotated[ang_b]
            bw = bb.bounds[2]-bb.bounds[0]; bh = bb.bounds[3]-bb.bounds[1]
            sr = (bw**2+bh**2)**0.5*2
            ar = np.radians(ad); dx, dy = np.cos(ar), np.sin(ar)
            lo, hi = 0.0, sr
            if ba.distance(translate(bb, dx*hi, dy*hi)) < target_sp: hi *= 2
            for _ in range(30):
                mid = (lo+hi)/2
                if ba.distance(translate(bb, dx*mid, dy*mid)) < target_sp: lo=mid
                else: hi=mid
            idx, idy = dx*hi, dy*hi
            if ba.distance(translate(bb, idx, idy)) < target_sp - 0.05:
                return None
            cdx, cdy, tbf, _ = refine_position(ba, bb, idx, idy, target_sp)
            if ba.distance(tbf) < target_sp - 0.05:
                return None
            cb = unary_union([ba, tbf]).bounds
            uw, uh = cb[2]-cb[0], cb[3]-cb[1]
            sw, sh = uw+target_sp, uh+target_sp
            cc = int(eff_w//sw); cr = int(eff_h//sh)
            if cc*cr*2 <= 0: return None
            ox, oy = cb[0], cb[1]
            coords = [(lb+c*sw-ox, db+r*sh-oy) for r in range(cr) for c in range(cc)]
            return (cc*cr*2, -(uw*uh), coords,
                    (ba, tbf, cdx, cdy, ang_b), uw*uh, cc, cr, sw, sh)

        # 使用 ThreadPoolExecutor（Render 環境 fork 受限，thread 更穩定）
        from concurrent.futures import ThreadPoolExecutor
        candidates = []
        with ThreadPoolExecutor(max_workers=4) as executor:
            futures = {executor.submit(compute_one, t): i for i, t in enumerate(tasks)}
            for future in as_completed(futures):
                done_count[0] += 1
                if progress_cb:
                    progress_cb(done_count[0] / total)
                result = future.result()
                if result is not None:
                    candidates.append(result)

        candidates = sorted(candidates, key=lambda x:(x[4],-x[0]))[:3]
        candidates_top3 = candidates
        if candidates:
            bc = candidates[0]
            baf, tbf, dxf, dyf, abf = bc[3]
            bcc, bcr, bsw, bsh = bc[5], bc[6], bc[7], bc[8]
            for tx, ty in bc[2]:
                fa = translate(baf, tx, ty)
                fb = translate(tbf, tx, ty)
                best_layout.append((fa, 0, False, (tx, ty)))
                best_layout.append((fb, abf, False, (tx+dxf, ty+dyf)))
    else:
        max_found = 0
        for ang in [0,90,180,270]:
            tb = rotate(raw_poly, ang, origin=(0,0))
            sx, sy, ex, ey = tb.bounds
            sw, sh = ex-sx, ey-sy
            stx, sty = sw+spacing, sh+spacing
            temp = []
            cy2 = db
            while cy2+sh <= ch-ub+0.001:
                cx2 = lb
                while cx2+sw <= cw-rb+0.001:
                    tp = translate(tb, cx2-sx, cy2-sy)
                    if safe.contains(tp):
                        temp.append((tp, ang, False, (tp.centroid.x, tp.centroid.y)))
                    cx2 += stx
                cy2 += sty
            if len(temp) > max_found:
                max_found = len(temp); best_layout = temp
                bsw, bsh = stx, sty
                bcc, bcr = int(eff_w//stx), int(eff_h//sty)
        if progress_cb:
            progress_cb(1.0)

    return best_layout, bsw, bsh, bcc, bcr, candidates_top3

def build_interactive_lines_from_dxf(src_path, fps, cx_cy_orig):
    """
    Builds interactive segments for the bridge canvas.

    Body/hole/offset positions come entirely from the placed Shapely polygons
    (fps) — guaranteed correct for both Nesting and Matrix modes.

    Additionally, for DXF arc/circle entities, we compute the arc's world
    position via the transform (shift→rotate→translate to poly.centroid)
    and emit an extra arc segment so the canvas draws a smooth circle
    instead of a Shapely-flattened polyline.

    Strategy per entity type:
      - Arc/Circle  → emit arc segment (smooth canvas rendering)
      - Line/Other  → emit line segments from Shapely poly coords (accurate)
    Offset         → always poly.buffer(2, join_style=2) from Shapely (accurate)
    """
    import math as _math

    lines = []

    # ── Read DXF to find arc entities (for smooth rendering only) ────
    dxf_arcs = []  # list of {"cx","cy","r","a0","a1"} in original DXF coords
    try:
        doc = ezdxf.readfile(src_path)
        msp = doc.modelspace()
        entities = msp.query('*[layer=="BOARD_OUTLINE_00"]')
        if not entities:
            entities = msp.query('CIRCLE ARC')
        else:
            # filter to only arc types
            entities = [e for e in entities if e.dxftype() in ('CIRCLE','ARC')]
        for ent in entities:
            t = ent.dxftype()
            if t == 'CIRCLE':
                c = ent.dxf.center
                dxf_arcs.append({"cx":c.x,"cy":c.y,"r":ent.dxf.radius,
                                  "a0":0.0,"a1":360.0})
            elif t == 'ARC':
                c = ent.dxf.center
                dxf_arcs.append({"cx":c.x,"cy":c.y,"r":ent.dxf.radius,
                                  "a0":ent.dxf.start_angle,"a1":ent.dxf.end_angle})
    except: pass

    ocx, ocy = cx_cy_orig

    for poly, ang_deg, _, _ in fps:
        rad = _math.radians(ang_deg)
        cos_a, sin_a = _math.cos(rad), _math.sin(rad)
        pcx, pcy = poly.centroid.x, poly.centroid.y

        def world_pt(dxf_x, dxf_y):
            bx, by = dxf_x - ocx, dxf_y - ocy
            rx = bx * cos_a - by * sin_a
            ry = bx * sin_a + by * cos_a
            return rx + pcx, ry + pcy

        # ── Body: emit arc segments for DXF arcs (smooth rendering) ──
        # For non-arc entities, body lines come from poly.exterior below
        placed_arc_ids = set()
        for arc in dxf_arcs:
            ncx, ncy = world_pt(arc["cx"], arc["cy"])
            a0 = (arc["a0"] + ang_deg) % 360
            a1 = (arc["a1"] + ang_deg) % 360
            is_hole = poly.contains(Point(ncx, ncy))
            seg_type = "hole" if is_hole else "body"
            sid = str(uuid.uuid4())
            placed_arc_ids.add((round(ncx,2), round(ncy,2), round(arc["r"],2)))
            lines.append({"id":sid, "type":seg_type, "kind":"arc",
                           "cx":round(ncx,4), "cy":round(ncy,4),
                           "r":round(arc["r"],4),
                           "a0":round(a0,4), "a1":round(a1,4)})

        # ── Body/Hole lines from Shapely exterior/interiors ──
        # Skip segments that are already covered by a DXF arc
        # (rough check: if the midpoint is within 0.1mm of an arc, skip)
        def near_arc(mx, my):
            for (acx, acy, ar) in placed_arc_ids:
                if abs(_math.sqrt((mx-acx)**2+(my-acy)**2) - ar) < 0.2:
                    return True
            return False

        ec = list(poly.exterior.coords)
        for i in range(len(ec)-1):
            mx2, my2 = (ec[i][0]+ec[i+1][0])/2, (ec[i][1]+ec[i+1][1])/2
            if not near_arc(mx2, my2):
                lines.append({"id":str(uuid.uuid4()), "type":"body", "kind":"line",
                               "coords":[[round(ec[i][0],4),round(ec[i][1],4)],
                                         [round(ec[i+1][0],4),round(ec[i+1][1],4)]]})
        for hole in poly.interiors:
            hc = list(hole.coords)
            for i in range(len(hc)-1):
                mx2, my2 = (hc[i][0]+hc[i+1][0])/2, (hc[i][1]+hc[i+1][1])/2
                if not near_arc(mx2, my2):
                    lines.append({"id":str(uuid.uuid4()), "type":"hole", "kind":"line",
                                   "coords":[[round(hc[i][0],4),round(hc[i][1],4)],
                                             [round(hc[i+1][0],4),round(hc[i+1][1],4)]]})

        # ── Offset: always Shapely buffer — accurate for both modes ──
        try:
            op = poly.buffer(2, join_style=2)
            oc = list(op.exterior.coords)
            for i in range(len(oc)-1):
                lines.append({"id":str(uuid.uuid4()), "type":"offset", "kind":"line",
                               "coords":[[round(oc[i][0],4),round(oc[i][1],4)],
                                         [round(oc[i+1][0],4),round(oc[i+1][1],4)]]})
            for h in op.interiors:
                hc2 = list(h.coords)
                for i in range(len(hc2)-1):
                    lines.append({"id":str(uuid.uuid4()), "type":"offset", "kind":"line",
                                   "coords":[[round(hc2[i][0],4),round(hc2[i][1],4)],
                                             [round(hc2[i+1][0],4),round(hc2[i+1][1],4)]]})
        except: pass

    return lines



def build_output_data(best_layout, bsw, bsh, bcc, bcr, params, geo_block_name, doc_out, raw_poly):
    cw, ch = params["panel_w"], params["panel_h"]
    lb, rb, ub, db = params["left"], params["right"], params["top"], params["bottom"]
    spacing = params["spacing"]
    r_corner = params.get("corner_r", 4.0)
    msp_out = doc_out.modelspace()
    p_w = raw_poly.bounds[2]-raw_poly.bounds[0]
    p_h = raw_poly.bounds[3]-raw_poly.bounds[1]

    all_u = unary_union([b for b,a,f,c in best_layout])
    fox = (cw/2) - all_u.centroid.x
    foy = (ch/2) - all_u.centroid.y
    fps = [(translate(b,fox,foy),a,f,(c[0]+fox,c[1]+foy)) for b,a,f,c in best_layout]
    fp = [p for p,a,f,c in fps]

    fu = unary_union(fp)
    fx1,fy1,fx2,fy2 = fu.bounds
    compw = (fx2-fx1)+lb+rb
    comph = (fy2-fy1)+ub+db
    cox, coy = lb-fx1, db-fy1
    fps = [(translate(p,cox,coy),a,f,(c[0]+cox,c[1]+coy)) for p,a,f,c in fps]
    fp = [p for p,a,f,c in fps]

    # Write DXF blockrefs
    for poly, ang, _, (ax, ay) in fps:
        msp_out.add_blockref(geo_block_name, (ax, ay), dxfattribs={
            'rotation': ang, 'xscale': 1.0, 'yscale': 1.0,
            'layer': 'PART_BODY', 'color': 7})

    panel_geo = create_rounded_panel(compw, comph, r_corner)
    msp_out.add_lwpolyline(list(panel_geo.exterior.coords),
                           dxfattribs={'layer':'PANEL_FRAME','color':7,'closed':True})
    for cx, cy in [(4,4),(compw-4,4),(compw-4,comph-4),(4,comph-4)]:
        msp_out.add_circle((cx,cy), radius=1.53, dxfattribs={'layer':'PANEL_HOLES','color':7})

    # Stats
    def get_groups(coords, tol=10.0):
        if not coords: return []
        sc = sorted(coords); g = [[sc[0]]]
        for c in sc[1:]:
            if c-g[-1][-1]<tol: g[-1].append(c)
            else: g.append([c])
        return g
    def min_gap(polys, axis):
        if len(polys)<2: return 0.0
        gaps=[]
        sp = sorted(polys, key=lambda p: p.bounds[0 if axis=='x' else 1])
        for i in range(len(sp)-1):
            for j in range(i+1,len(sp)):
                g = sp[j].bounds[0 if axis=='x' else 1] - sp[i].bounds[2 if axis=='x' else 3]
                gaps.append(g); break   # 允許負值（鑲嵌重疊）
        return round(min(gaps), 4) if gaps else 0.0

    xg = get_groups([p.centroid.x for p in fp])
    yg = get_groups([p.centroid.y for p in fp])
    fu2 = unary_union(fp).bounds

    stats = {
        "pcs": len(fp), "p_w": round(p_w,3), "p_h": round(p_h,3),
        "utilization": round(sum(p.area for p in fp)/(compw*comph)*100, 2),
        "cols": len(xg), "rows": len(yg),
        "gap_x": min_gap(fp,'x'), "gap_y": min_gap(fp,'y'),
        "band_l": round(fu2[0],2), "band_r": round(compw-fu2[2],2),
        "band_b": round(fu2[1],2), "band_t": round(comph-fu2[3],2),
        "original_w": round(cw,3), "original_h": round(ch,3),
        "compressed_w": round(compw,3), "compressed_h": round(comph,3),
        "shrink_w": round(cw-compw,3), "shrink_h": round(ch-comph,3),
        "cur_cols": bcc, "cur_rows": bcr,
    }
    if bsw > 0 and bsh > 0:
        sew = (bcc+1)*bsw + bsw*0.01
        seh = (bcr+1)*bsh + bsh*0.01
        stats["suggest_w"] = round(sew+lb+rb, 3)
        stats["suggest_h"] = round(seh+ub+db, 3)
        stats["suggest_cols"] = bcc+1
        stats["suggest_rows"] = bcr+1
        stats["extra_w"] = round(sew+lb+rb-compw, 3)
        stats["extra_h"] = round(seh+ub+db-comph, 3)

    # Serialize polys for front-end canvas
    polys_data = []
    for poly, ang, _, (ax, ay) in fps:
        ext = [[x,y] for x,y in poly.exterior.coords]
        holes = [[[x,y] for x,y in h.coords] for h in poly.interiors]
        polys_data.append({"exterior": ext, "holes": holes, "ax": ax, "ay": ay, "ang": ang})

    return fps, stats, polys_data, compw, comph

# ─────────────────────────────────────────────
# Bridge geometry (Python-side, exact same as Colab)
# ─────────────────────────────────────────────
def calculate_bridge(data, lines, connectors):
    gap_half = 1.75
    l1 = lines[data['idx1']]
    l2 = lines[data['idx2']]

    # Arcs cannot be split — treat them like body/hole
    def to_shapely_line(seg):
        if seg.get('kind') == 'arc':
            return None  # will be kept intact
        return LineString(seg['coords'])

    line1 = to_shapely_line(l1)
    line2 = to_shapely_line(l2)

    if line1 is None or line2 is None:
        # At least one is an arc — no split, just add connector between projected points
        # Use midpoint of arc for projection
        def arc_midpoint(seg):
            import math as _m
            a_mid = _m.radians((seg['a0'] + seg['a1']) / 2)
            return Point(seg['cx'] + seg['r'] * _m.cos(a_mid),
                         seg['cy'] + seg['r'] * _m.sin(a_mid))
        p1 = arc_midpoint(l1) if line1 is None else line1.interpolate(line1.project(arc_midpoint(l2)))
        p2 = arc_midpoint(l2) if line2 is None else line2.interpolate(line2.project(p1))
    else:
        p1 = line1.interpolate(line1.project(Point(data['pt1'])))
        p2 = line2.interpolate(line2.project(p1))

    mx, my = (p1.x+p2.x)/2, (p1.y+p2.y)/2

    # Use direction from whichever is a line segment
    ref = l1 if l1.get('kind','line') == 'line' else l2
    if ref.get('kind','line') == 'line':
        v1x = ref['coords'][1][0]-ref['coords'][0][0]
        v1y = ref['coords'][1][1]-ref['coords'][0][1]
    else:
        import math as _m
        a_mid = _m.radians((ref['a0']+ref['a1'])/2)
        v1x, v1y = -_m.sin(a_mid), _m.cos(a_mid)

    vl = math.sqrt(v1x**2+v1y**2)
    if vl < 1e-9: vl = 1.0
    ux, uy = v1x/vl, v1y/vl
    ang = math.degrees(math.atan2(uy,ux))

    def split(line_obj, seg, proj, ltype):
        # arcs and body/hole lines are never split
        if ltype in ('body','hole') or seg.get('kind') == 'arc':
            new_seg = dict(seg)
            new_seg['id'] = str(uuid.uuid4())
            return [new_seg]
        d = line_obj.project(proj)
        segs = []
        if d > gap_half:
            s = line_obj.interpolate(0); e = line_obj.interpolate(d-gap_half)
            segs.append({"id":str(uuid.uuid4()),"coords":[[s.x,s.y],[e.x,e.y]],
                         "type":ltype,"kind":"line"})
        if d+gap_half < line_obj.length:
            s = line_obj.interpolate(d+gap_half); e = line_obj.interpolate(line_obj.length)
            segs.append({"id":str(uuid.uuid4()),"coords":[[s.x,s.y],[e.x,e.y]],
                         "type":ltype,"kind":"line"})
        return segs

    ids_remove = {l1['id'], l2['id']}
    new_lines = [l for l in lines if l['id'] not in ids_remove]
    new_lines += split(line1, l1, p1, l1['type'])
    new_lines += split(line2, l2, p2, l2['type'])

    connector = {
        'type':'precise_sandglass_arc',
        'lx': mx-ux*gap_half, 'ly': my-uy*gap_half,
        'rx': mx+ux*gap_half, 'ry': my+uy*gap_half,
        'r': 1.0, 'ang': ang
    }
    new_connectors = list(connectors) + [connector]
    return new_lines, new_connectors

# ─────────────────────────────────────────────
# REST endpoints
# ─────────────────────────────────────────────
@app.get("/")
async def index():
    return FileResponse("static/index.html")

@app.post("/upload")
async def upload_dxf(file: UploadFile = File(...)):
    # 把原始 DXF 存到一個持久的暫存路徑，供後續 nest 重建 block 使用
    if SESSION.get('_src_dxf_path') and os.path.exists(SESSION['_src_dxf_path']):
        try: os.unlink(SESSION['_src_dxf_path'])
        except: pass

    tmp = tempfile.NamedTemporaryFile(suffix='.dxf', delete=False)
    tmp.write(await file.read()); tmp.flush(); tmp.close()
    try:
        doc_out = ezdxf.new('R2010')
        raw, block_name, orig_cxy, hole_count = get_full_polygon_with_holes(tmp.name, doc_out)
        minx,miny,maxx,maxy = raw.bounds
        if (maxx-minx) > 400:
            raw = scale(raw, xfact=0.0254, yfact=0.0254, origin=(0,0))
        SESSION['raw_poly'] = raw
        SESSION['geo_block_name'] = block_name
        SESSION['doc_out'] = doc_out
        SESSION['_src_dxf_path'] = tmp.name
        SESSION['_orig_cx_cy'] = orig_cxy   # original DXF centroid for arc transforms
        SESSION['interactive_lines'] = []
        SESSION['manual_connectors'] = []
        SESSION['lines_history'] = []
        return {"ok": True, "holes": hole_count,
                "w": round(raw.bounds[2]-raw.bounds[0],3),
                "h": round(raw.bounds[3]-raw.bounds[1],3)}
    except Exception as e:
        try: os.unlink(tmp.name)
        except: pass
        return JSONResponse({"ok":False,"error":str(e)}, status_code=400)

@app.post("/nest")
async def nest(params: dict):
    raw = SESSION.get('raw_poly')
    if raw is None:
        return JSONResponse({"ok":False,"error":"請先上傳 DXF"}, status_code=400)

    from fastapi.responses import StreamingResponse as SR
    import asyncio, threading, queue as q_module

    progress_queue = q_module.Queue()

    def progress_cb(pct):
        progress_queue.put(("progress", round(pct * 100)))

    def run_in_thread():
        try:
            t0 = time.time()
            doc_out = ezdxf.new('R2010')
            src_path = SESSION.get('_src_dxf_path')
            if src_path and os.path.exists(src_path):
                _, block_name, _, _ = get_full_polygon_with_holes(src_path, doc_out)
            else:
                block_name = SESSION['geo_block_name']
            SESSION['doc_out'] = doc_out
            SESSION['geo_block_name'] = block_name

            bl, bsw, bsh, bcc, bcr, top3 = run_nesting(raw, params, progress_cb)
            if not bl:
                progress_queue.put(("error", "無法排版"))
                return

            fps, stats, polys_data, compw, comph = build_output_data(
                bl, bsw, bsh, bcc, bcr, params, block_name, doc_out, raw)

            SESSION['best_layout'] = bl
            SESSION['final_polys_with_info'] = fps
            SESSION['stats'] = stats
            SESSION['compressed_w'] = compw
            SESSION['compressed_h'] = comph
            SESSION['interactive_lines'] = []
            SESSION['manual_connectors'] = []
            SESSION['lines_history'] = []

            # Build arc-aware interactive lines from original DXF
            src_path = SESSION.get('_src_dxf_path')
            cx_cy = SESSION.get('_orig_cx_cy', (0.0, 0.0))
            if src_path and os.path.exists(src_path):
                lines = build_interactive_lines_from_dxf(src_path, fps, cx_cy)
            else:
                # fallback: polygon segments
                lines = []
                for poly, ang, _, _ in fps:
                    ec = list(poly.exterior.coords)
                    for i in range(len(ec)-1):
                        lines.append({"id":str(uuid.uuid4()),
                                      "coords":[[ec[i][0],ec[i][1]],[ec[i+1][0],ec[i+1][1]]],
                                      "type":"body","kind":"line"})
                    for hole in poly.interiors:
                        hc = list(hole.coords)
                        for i in range(len(hc)-1):
                            lines.append({"id":str(uuid.uuid4()),
                                          "coords":[[hc[i][0],hc[i][1]],[hc[i+1][0],hc[i+1][1]]],
                                          "type":"hole","kind":"line"})
                    try:
                        op = poly.buffer(2, join_style=2)
                        oc = list(op.exterior.coords)
                        for i in range(len(oc)-1):
                            lines.append({"id":str(uuid.uuid4()),
                                          "coords":[[oc[i][0],oc[i][1]],[oc[i+1][0],oc[i+1][1]]],
                                          "type":"offset","kind":"line"})
                    except: pass
            SESSION['interactive_lines'] = lines

            # Add panel frame as 'frame' type segments
            panel_geo = create_rounded_panel(compw, comph, params.get('corner_r', 4.0))
            fc = list(panel_geo.exterior.coords)
            for i in range(len(fc)-1):
                lines.append({"id":str(uuid.uuid4()),
                               "coords":[[round(fc[i][0],4),round(fc[i][1],4)],
                                         [round(fc[i+1][0],4),round(fc[i+1][1],4)]],
                               "type":"frame","kind":"line"})
            SESSION['interactive_lines'] = lines
            for c in top3:
                p1, p2 = c[3][0], c[3][1]
                top3_data.append({
                    "pcs": c[0], "area": round(c[4],1),
                    "p1": [[x,y] for x,y in p1.exterior.coords],
                    "p2": [[x,y] for x,y in p2.exterior.coords],
                })

            stats['elapsed'] = round(time.time()-t0, 3)
            payload = {"ok":True, "stats":stats, "polys":polys_data,
                       "top3":top3_data, "lines":lines,
                       "compressed_w":compw, "compressed_h":comph}
            progress_queue.put(("done", payload))
        except Exception as e:
            import traceback; traceback.print_exc()
            progress_queue.put(("error", str(e)))

    thread = threading.Thread(target=run_in_thread, daemon=True)
    thread.start()

    async def event_stream():
        loop = asyncio.get_event_loop()
        while True:
            try:
                msg_type, data = await loop.run_in_executor(
                    None, lambda: progress_queue.get(timeout=120))
                if msg_type == "progress":
                    yield f"data: {json.dumps({'type':'progress','pct':data})}\n\n"
                elif msg_type == "done":
                    yield f"data: {json.dumps({'type':'done','payload':data})}\n\n"
                    break
                elif msg_type == "error":
                    yield f"data: {json.dumps({'type':'error','msg':data})}\n\n"
                    break
            except Exception:
                break

    return SR(event_stream(), media_type="text/event-stream",
              headers={"Cache-Control":"no-cache","X-Accel-Buffering":"no"})

@app.post("/bridge")
async def bridge(data: dict):
    lines = SESSION['interactive_lines']
    connectors = SESSION['manual_connectors']
    SESSION['lines_history'].append(json.loads(json.dumps(lines)))
    try:
        new_lines, new_conn = calculate_bridge(data, lines, connectors)
        SESSION['interactive_lines'] = new_lines
        SESSION['manual_connectors'] = new_conn
        return {"ok":True, "lines":new_lines, "connectors":[
            {"lx":c['lx'],"ly":c['ly'],"rx":c['rx'],"ry":c['ry'],
             "r":c['r'],"ang":c['ang']} for c in new_conn]}
    except Exception as e:
        return JSONResponse({"ok":False,"error":str(e)}, status_code=400)

@app.post("/undo")
async def undo():
    if SESSION['lines_history']:
        SESSION['interactive_lines'] = SESSION['lines_history'].pop()
    if SESSION['manual_connectors']:
        SESSION['manual_connectors'].pop()
    return {"ok":True, "lines":SESSION['interactive_lines'],
            "connectors":[{"lx":c['lx'],"ly":c['ly'],"rx":c['rx'],"ry":c['ry'],
                            "r":c['r'],"ang":c['ang']}
                           for c in SESSION['manual_connectors']]}

@app.post("/reset_bridge")
async def reset_bridge():
    SESSION['interactive_lines'] = []
    SESSION['manual_connectors'] = []
    SESSION['lines_history'] = []
    fps = SESSION.get('final_polys_with_info', [])
    src_path = SESSION.get('_src_dxf_path')
    cx_cy = SESSION.get('_orig_cx_cy', (0.0, 0.0))
    compw = SESSION.get('compressed_w', 0)
    comph = SESSION.get('compressed_h', 0)
    r_corner = SESSION.get('stats', {}).get('corner_r', 4.0)
    if src_path and os.path.exists(src_path) and fps:
        lines = build_interactive_lines_from_dxf(src_path, fps, cx_cy)
    else:
        lines = []
        for poly, ang, _, _ in fps:
            ec = list(poly.exterior.coords)
            for i in range(len(ec)-1):
                lines.append({"id":str(uuid.uuid4()),
                              "coords":[[ec[i][0],ec[i][1]],[ec[i+1][0],ec[i+1][1]]],
                              "type":"body","kind":"line"})
    # Add panel frame
    if compw and comph:
        panel_geo = create_rounded_panel(compw, comph, r_corner)
        fc = list(panel_geo.exterior.coords)
        for i in range(len(fc)-1):
            lines.append({"id":str(uuid.uuid4()),
                           "coords":[[round(fc[i][0],4),round(fc[i][1],4)],
                                     [round(fc[i+1][0],4),round(fc[i+1][1],4)]],
                           "type":"frame","kind":"line"})
    SESSION['interactive_lines'] = lines
    return {"ok":True, "lines":lines, "connectors":[]}

@app.get("/download_nest_dxf")
async def download_nest_dxf():
    doc = SESSION.get('doc_out')
    if not doc: return JSONResponse({"error":"no data"}, status_code=400)
    tmp = tempfile.NamedTemporaryFile(suffix='.dxf', delete=False)
    doc.saveas(tmp.name)
    with open(tmp.name,'rb') as f: data = f.read()
    os.unlink(tmp.name)
    return StreamingResponse(io.BytesIO(data), media_type="application/dxf",
        headers={"Content-Disposition":f"attachment; filename=nested.dxf"})

@app.get("/download_bridge_dxf")
async def download_bridge_dxf():
    compw = SESSION.get('compressed_w', 162.5)
    comph = SESSION.get('compressed_h', 190.5)
    fps   = SESSION.get('final_polys_with_info', [])
    src_doc = SESSION.get('doc_out')           # 含 block 的 doc
    block_name = SESSION.get('geo_block_name', 'PART_GEO_BLOCK')
    R_corner, r_fix = 4.0, 2.0

    doc = ezdxf.new('R2010'); doc.header['$INSUNITS'] = 4
    msp = doc.modelspace()
    for lname, col in [('PARTS',7),('OFFSETS',4),('BRIDGES',2),('FRAME',7)]:
        doc.layers.new(lname, dxfattribs={'color': col})

    # ── 1. 從 src_doc 複製 block 到新 doc，保留完整圓弧幾何 ──
    if src_doc and block_name in src_doc.blocks:
        src_block = src_doc.blocks[block_name]
        new_block = doc.blocks.new(name=block_name)
        for ent in src_block:
            try:
                new_block.add_entity(ent.copy())
            except:
                pass

    # ── 2. 以 blockref 插入每個零件（含旋轉，圓弧完整保留）──
    for poly, ang, _, (ax, ay) in fps:
        msp.add_blockref(block_name, (ax, ay), dxfattribs={
            'rotation': ang, 'xscale': 1.0, 'yscale': 1.0,
            'layer': 'PARTS', 'color': 7})

    # ── 3. Offset 線段（橋接後已切斷的折線，不用圓弧）──
    for ld in SESSION.get('interactive_lines', []):
        if ld['type'] == 'offset':
            msp.add_line(ld['coords'][0], ld['coords'][1],
                         dxfattribs={'layer': 'OFFSETS'})

    # ── 4. 橋接弧線 ──
    for b in SESSION.get('manual_connectors', []):
        if b.get('type') == 'precise_sandglass_arc':
            t = b['ang']
            msp.add_arc((b['lx'], b['ly']), radius=b['r'],
                        start_angle=t-90, end_angle=t+90,
                        dxfattribs={'layer': 'BRIDGES'})
            msp.add_arc((b['rx'], b['ry']), radius=b['r'],
                        start_angle=t+90, end_angle=t+270,
                        dxfattribs={'layer': 'BRIDGES'})

    # ── 5. 面板邊框與定位孔 ──
    centers = [(R_corner, R_corner), (compw-R_corner, R_corner),
               (compw-R_corner, comph-R_corner), (R_corner, comph-R_corner)]
    msp.add_line((R_corner, 0),      (compw-R_corner, 0),      dxfattribs={'layer': 'FRAME'})
    msp.add_line((compw, R_corner),  (compw, comph-R_corner),  dxfattribs={'layer': 'FRAME'})
    msp.add_line((compw-R_corner, comph), (R_corner, comph),   dxfattribs={'layer': 'FRAME'})
    msp.add_line((0, comph-R_corner), (0, R_corner),           dxfattribs={'layer': 'FRAME'})
    for i, center in enumerate(centers):
        msp.add_arc(center, radius=R_corner,
                    start_angle=[(180,270),(270,360),(0,90),(90,180)][i][0],
                    end_angle  =[(180,270),(270,360),(0,90),(90,180)][i][1],
                    dxfattribs={'layer': 'FRAME'})
        msp.add_circle(center, radius=r_fix, dxfattribs={'layer': 'FRAME'})
    msp.add_circle((centers[0][0]+10, centers[0][1]),
                   radius=r_fix, dxfattribs={'layer': 'FRAME'})

    tmp = tempfile.NamedTemporaryFile(suffix='.dxf', delete=False)
    doc.saveas(tmp.name)
    with open(tmp.name, 'rb') as f: data = f.read()
    os.unlink(tmp.name)
    return StreamingResponse(io.BytesIO(data), media_type="application/dxf",
        headers={"Content-Disposition": "attachment; filename=production.dxf"})

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
