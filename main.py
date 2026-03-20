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
                poly = Point(c.x, c.y).buffer(entity.dxf.radius, quad_segs=64)
            else:
                pts = list(make_path(entity).flattening(distance=0.01))
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
    while step >= 0.001:
        improved = False
        for ddx, ddy in [(step,0),(-step,0),(0,step),(0,-step)]:
            tb = translate(poly_b, best_dx+ddx, best_dy+ddy)
            if poly_a.distance(tb) < target_dist - 0.001: continue
            cb2 = unary_union([poly_a, tb]).bounds
            a2 = (cb2[2]-cb2[0])*(cb2[3]-cb2[1])
            if a2 < best_area - 1e-6:
                best_area = a2; best_dx += ddx; best_dy += ddy; best_b = tb
                improved = True; break
        if not improved: step *= 0.5
    return best_dx, best_dy, best_b, best_area

def run_nesting(raw_poly, params):
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
        candidates = []
        target_sp = float(spacing)
        rotated = {a: rotate(raw_poly, a, origin=(0,0)) for a in [0,90,180,270]}
        ba = rotated[0]
        for ang_b, bb in rotated.items():
            bw = bb.bounds[2]-bb.bounds[0]; bh = bb.bounds[3]-bb.bounds[1]
            sr = (bw**2+bh**2)**0.5*2
            for ad in range(0, 360, 10):
                ar = np.radians(ad); dx, dy = np.cos(ar), np.sin(ar)
                lo, hi = 0.0, sr
                if ba.distance(translate(bb, dx*hi, dy*hi)) < target_sp: hi *= 2
                for _ in range(50):
                    mid = (lo+hi)/2
                    if ba.distance(translate(bb, dx*mid, dy*mid)) < target_sp: lo=mid
                    else: hi=mid
                idx, idy = dx*hi, dy*hi
                if ba.distance(translate(bb, idx, idy)) < target_sp - 0.05: continue
                cdx, cdy, tbf, _ = refine_position(ba, bb, idx, idy, target_sp)
                if ba.distance(tbf) < target_sp - 0.05: continue
                cb = unary_union([ba, tbf]).bounds
                uw, uh = cb[2]-cb[0], cb[3]-cb[1]
                sw, sh = uw+target_sp, uh+target_sp
                cc = int(eff_w//sw); cr = int(eff_h//sh)
                if cc*cr*2 <= 0: continue
                ox, oy = cb[0], cb[1]
                coords = [(lb+c*sw-ox, db+r*sh-oy) for r in range(cr) for c in range(cc)]
                candidates.append((cc*cr*2, -(uw*uh), coords,
                                   (ba, tbf, cdx, cdy, ang_b), uw*uh, cc, cr, sw, sh))
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

    return best_layout, bsw, bsh, bcc, bcr, candidates_top3

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
                if g>-0.1: gaps.append(g); break
        return min(gaps) if gaps else 0.0

    xg = get_groups([p.centroid.x for p in fp])
    yg = get_groups([p.centroid.y for p in fp])
    fu2 = unary_union(fp).bounds

    stats = {
        "pcs": len(fp), "p_w": round(p_w,3), "p_h": round(p_h,3),
        "utilization": round(sum(p.area for p in fp)/(compw*comph)*100, 2),
        "cols": len(xg), "rows": len(yg),
        "gap_x": round(min_gap(fp,'x'),4), "gap_y": round(min_gap(fp,'y'),4),
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
    line1 = LineString(l1['coords'])
    line2 = LineString(l2['coords'])
    p1 = line1.interpolate(line1.project(Point(data['pt1'])))
    p2 = line2.interpolate(line2.project(p1))
    mx, my = (p1.x+p2.x)/2, (p1.y+p2.y)/2
    v1x = l1['coords'][1][0]-l1['coords'][0][0]
    v1y = l1['coords'][1][1]-l1['coords'][0][1]
    vl = math.sqrt(v1x**2+v1y**2)
    ux, uy = v1x/vl, v1y/vl
    ang = math.degrees(math.atan2(uy,ux))

    def split(line_obj, proj, ltype, lid):
        if ltype in ('body','hole'):
            return [{"id":str(uuid.uuid4()),
                     "coords":[[c[0],c[1]] for c in line_obj.coords],
                     "type":ltype}]
        d = line_obj.project(proj)
        segs = []
        if d > gap_half:
            s = line_obj.interpolate(0); e = line_obj.interpolate(d-gap_half)
            segs.append({"id":str(uuid.uuid4()),"coords":[[s.x,s.y],[e.x,e.y]],"type":ltype})
        if d+gap_half < line_obj.length:
            s = line_obj.interpolate(d+gap_half); e = line_obj.interpolate(line_obj.length)
            segs.append({"id":str(uuid.uuid4()),"coords":[[s.x,s.y],[e.x,e.y]],"type":ltype})
        return segs

    ids_remove = {l1['id'], l2['id']}
    new_lines = [l for l in lines if l['id'] not in ids_remove]
    new_lines += split(line1, p1, l1['type'], l1['id'])
    new_lines += split(line2, p2, l2['type'], l2['id'])
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
    tmp = tempfile.NamedTemporaryFile(suffix='.dxf', delete=False)
    tmp.write(await file.read()); tmp.flush()
    try:
        doc_out = ezdxf.new('R2010')
        raw, block_name, _, hole_count = get_full_polygon_with_holes(tmp.name, doc_out)
        minx,miny,maxx,maxy = raw.bounds
        if (maxx-minx) > 400:
            raw = scale(raw, xfact=0.0254, yfact=0.0254, origin=(0,0))
        SESSION['raw_poly'] = raw
        SESSION['geo_block_name'] = block_name
        SESSION['doc_out'] = doc_out
        SESSION['interactive_lines'] = []
        SESSION['manual_connectors'] = []
        SESSION['lines_history'] = []
        return {"ok": True, "holes": hole_count,
                "w": round(raw.bounds[2]-raw.bounds[0],3),
                "h": round(raw.bounds[3]-raw.bounds[1],3)}
    except Exception as e:
        return JSONResponse({"ok":False,"error":str(e)}, status_code=400)
    finally:
        os.unlink(tmp.name)

@app.post("/nest")
async def nest(params: dict):
    raw = SESSION.get('raw_poly')
    if raw is None:
        return JSONResponse({"ok":False,"error":"請先上傳 DXF"}, status_code=400)
    try:
        t0 = time.time()
        # Re-init doc for fresh nesting
        doc_out = ezdxf.new('R2010')
        SESSION['doc_out'] = doc_out
        tmp_f = tempfile.NamedTemporaryFile(suffix='.dxf', delete=False)
        # re-upload raw poly as block (just re-use stored block name)
        bl, bsw, bsh, bcc, bcr, top3 = run_nesting(raw, params)
        if not bl:
            return JSONResponse({"ok":False,"error":"無法排版"}, status_code=400)

        fps, stats, polys_data, compw, comph = build_output_data(
            bl, bsw, bsh, bcc, bcr, params,
            SESSION['geo_block_name'], doc_out, raw)

        SESSION['best_layout'] = bl
        SESSION['final_polys_with_info'] = fps
        SESSION['stats'] = stats
        SESSION['compressed_w'] = compw
        SESSION['compressed_h'] = comph
        SESSION['interactive_lines'] = []
        SESSION['manual_connectors'] = []
        SESSION['lines_history'] = []

        # Build interactive lines
        lines = []
        for poly, ang, _, _ in fps:
            ec = list(poly.exterior.coords)
            for i in range(len(ec)-1):
                lines.append({"id":str(uuid.uuid4()),
                              "coords":[[ec[i][0],ec[i][1]],[ec[i+1][0],ec[i+1][1]]],
                              "type":"body"})
            for hole in poly.interiors:
                hc = list(hole.coords)
                for i in range(len(hc)-1):
                    lines.append({"id":str(uuid.uuid4()),
                                  "coords":[[hc[i][0],hc[i][1]],[hc[i+1][0],hc[i+1][1]]],
                                  "type":"hole"})
            try:
                op = poly.buffer(2, join_style=2)
                oc = list(op.exterior.coords)
                for i in range(len(oc)-1):
                    lines.append({"id":str(uuid.uuid4()),
                                  "coords":[[oc[i][0],oc[i][1]],[oc[i+1][0],oc[i+1][1]]],
                                  "type":"offset"})
                for h in op.interiors:
                    hc = list(h.coords)
                    for i in range(len(hc)-1):
                        lines.append({"id":str(uuid.uuid4()),
                                      "coords":[[hc[i][0],hc[i][1]],[hc[i+1][0],hc[i+1][1]]],
                                      "type":"offset"})
            except: pass
        SESSION['interactive_lines'] = lines

        # Top3 pairs for preview
        top3_data = []
        for c in top3:
            p1, p2 = c[3][0], c[3][1]
            top3_data.append({
                "pcs": c[0], "area": round(c[4],1),
                "p1": [[x,y] for x,y in p1.exterior.coords],
                "p2": [[x,y] for x,y in p2.exterior.coords],
            })

        stats['elapsed'] = round(time.time()-t0, 3)
        return {"ok":True, "stats":stats, "polys":polys_data,
                "top3":top3_data, "lines":lines,
                "compressed_w":compw, "compressed_h":comph}
    except Exception as e:
        import traceback; traceback.print_exc()
        return JSONResponse({"ok":False,"error":str(e)}, status_code=500)

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
    # Rebuild lines from polys
    lines = []
    for poly, ang, _, _ in SESSION.get('final_polys_with_info', []):
        ec = list(poly.exterior.coords)
        for i in range(len(ec)-1):
            lines.append({"id":str(uuid.uuid4()),
                          "coords":[[ec[i][0],ec[i][1]],[ec[i+1][0],ec[i+1][1]]],
                          "type":"body"})
        for hole in poly.interiors:
            hc = list(hole.coords)
            for i in range(len(hc)-1):
                lines.append({"id":str(uuid.uuid4()),
                              "coords":[[hc[i][0],hc[i][1]],[hc[i+1][0],hc[i+1][1]]],
                              "type":"hole"})
        try:
            op = poly.buffer(2, join_style=2)
            oc = list(op.exterior.coords)
            for i in range(len(oc)-1):
                lines.append({"id":str(uuid.uuid4()),
                              "coords":[[oc[i][0],oc[i][1]],[oc[i+1][0],oc[i+1][1]]],
                              "type":"offset"})
        except: pass
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
    stats = SESSION.get('stats', {})
    compw = SESSION.get('compressed_w', 162.5)
    comph = SESSION.get('compressed_h', 190.5)
    R_corner, r_fix = 4.0, 2.0
    doc = ezdxf.new('R2010'); doc.header['$INSUNITS']=4
    msp = doc.modelspace()
    for lname, col in [('PARTS',7),('OFFSETS',4),('BRIDGES',2),('FRAME',7)]:
        doc.layers.new(lname, dxfattribs={'color':col})
    for ld in SESSION.get('interactive_lines', []):
        if ld['type'] == 'body':
            msp.add_line(ld['coords'][0], ld['coords'][1], dxfattribs={'layer':'PARTS'})
        elif ld['type'] == 'hole':
            msp.add_line(ld['coords'][0], ld['coords'][1], dxfattribs={'layer':'PARTS'})
        elif ld['type'] == 'offset':
            msp.add_line(ld['coords'][0], ld['coords'][1], dxfattribs={'layer':'OFFSETS'})
    for b in SESSION.get('manual_connectors', []):
        if b.get('type') == 'precise_sandglass_arc':
            t = b['ang']
            msp.add_arc((b['lx'],b['ly']),radius=b['r'],
                        start_angle=t-90,end_angle=t+90,dxfattribs={'layer':'BRIDGES'})
            msp.add_arc((b['rx'],b['ry']),radius=b['r'],
                        start_angle=t+90,end_angle=t+270,dxfattribs={'layer':'BRIDGES'})
    centers = [(R_corner,R_corner),(compw-R_corner,R_corner),
               (compw-R_corner,comph-R_corner),(R_corner,comph-R_corner)]
    msp.add_line((R_corner,0),(compw-R_corner,0),dxfattribs={'layer':'FRAME'})
    msp.add_line((compw,R_corner),(compw,comph-R_corner),dxfattribs={'layer':'FRAME'})
    msp.add_line((compw-R_corner,comph),(R_corner,comph),dxfattribs={'layer':'FRAME'})
    msp.add_line((0,comph-R_corner),(0,R_corner),dxfattribs={'layer':'FRAME'})
    for i, center in enumerate(centers):
        msp.add_arc(center,radius=R_corner,
                    start_angle=[(180,270),(270,360),(0,90),(90,180)][i][0],
                    end_angle=[(180,270),(270,360),(0,90),(90,180)][i][1],
                    dxfattribs={'layer':'FRAME'})
        msp.add_circle(center,radius=r_fix,dxfattribs={'layer':'FRAME'})
    msp.add_circle((centers[0][0]+10,centers[0][1]),radius=r_fix,dxfattribs={'layer':'FRAME'})
    tmp = tempfile.NamedTemporaryFile(suffix='.dxf', delete=False)
    doc.saveas(tmp.name)
    with open(tmp.name,'rb') as f: data = f.read()
    os.unlink(tmp.name)
    return StreamingResponse(io.BytesIO(data), media_type="application/dxf",
        headers={"Content-Disposition":f"attachment; filename=production.dxf"})

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
