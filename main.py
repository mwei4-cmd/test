import io, math, time, tempfile, os, uuid, json, sys
from typing import Optional
from fastapi import FastAPI, UploadFile, File, Request
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

# ── Google Sheet 設定（可選） ──────────────────────────────────────────────────
GOOGLE_ENABLED = False
try:
    from google.oauth2 import service_account
    from googleapiclient.discovery import build
    GOOGLE_ENABLED = True
except ImportError:
    pass

SCOPES   = ["https://www.googleapis.com/auth/spreadsheets"]
SHEET_ID  = os.environ.get("GOOGLE_SHEET_ID", "")
SHEET_TAB = "Input"

def _get_service():
    if not GOOGLE_ENABLED:
        return None
    sa_json = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", "")
    if not sa_json:
        return None
    try:
        info  = json.loads(sa_json)
        creds = service_account.Credentials.from_service_account_info(
            info, scopes=SCOPES)
        return build("sheets", "v4", credentials=creds, cache_discovery=False)
    except Exception as e:
        print(f"[Sheet] Service Account error: {e}")
        return None

# ── 靜態資源路徑（PyInstaller 相容） ──────────────────────────────────────────
def get_static_dir():
    if getattr(sys, 'frozen', False):
        base = sys._MEIPASS
    else:
        base = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(base, "static")

# ── FastAPI app ────────────────────────────────────────────────────────────────
app = FastAPI(title="PCB Nesting Tool")
app.add_middleware(CORSMiddleware, allow_origins=["*"],
                   allow_methods=["*"], allow_headers=["*"])

@app.on_event("startup")
async def startup_event():
    static_dir = get_static_dir()
    app.mount("/static", StaticFiles(directory=static_dir), name="static")

@app.get("/auth/status")
async def auth_status():
    svc = _get_service()
    return {"logged_in": svc is not None}

@app.get("/sheet/dropdown_options")
async def sheet_dropdown_options():
    svc = _get_service()
    if not svc:
        return JSONResponse({"error": "Service Account 未設定"}, status_code=401)
    try:
        result = svc.spreadsheets().get(
            spreadsheetId=SHEET_ID,
            ranges=[f"{SHEET_TAB}!C14:C20"],
            includeGridData=True,
        ).execute()
        rows = (result["sheets"][0]["data"][0].get("rowData") or [])
        options_per_row = []
        for row in rows:
            cell = (row.get("values") or [{}])[0]
            dv   = cell.get("dataValidation", {})
            vals = [v["userEnteredValue"] for v in dv.get("condition", {}).get("values", [])]
            options_per_row.append(vals)
        while len(options_per_row) < 7:
            options_per_row.append([])
        return {"options": options_per_row}
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)

@app.post("/sheet/calculate")
async def sheet_calculate(request: Request):
    import traceback
    svc = _get_service()
    if not svc:
        return JSONResponse({"error": "Service Account 未設定"}, status_code=401)
    body = await request.json()
    try:
        sheets = svc.spreadsheets()
        sheets.values().batchUpdate(
            spreadsheetId=SHEET_ID,
            body={
                "valueInputOption": "USER_ENTERED",
                "data": [
                    {"range": f"{SHEET_TAB}!B11", "values": [[body.get("B11", "")]]},
                    {"range": f"{SHEET_TAB}!C11", "values": [[body.get("C11", "")]]},
                    {"range": f"{SHEET_TAB}!D11", "values": [[body.get("D11", "")]]},
                    {"range": f"{SHEET_TAB}!E11", "values": [[body.get("E11", "")]]},
                    {"range": f"{SHEET_TAB}!F11", "values": [[body.get("F11", "")]]},
                    {"range": f"{SHEET_TAB}!G11", "values": [[body.get("G11", "")]]},
                    {"range": f"{SHEET_TAB}!H11", "values": [[body.get("H11", "")]]},
                    {"range": f"{SHEET_TAB}!I11", "values": [[body.get("I11", "")]]},
                    {"range": f"{SHEET_TAB}!J11", "values": [[body.get("J11", 250)]]},
                ]
            }
        ).execute()
        for cell, key in [("C14","C14"),("C15","C15"),("C16","C16"),
                           ("C17","C17"),("C18","C18"),("C19","C19"),("C20","C20")]:
            val = body.get(key, "")
            if val == "" or val is None:
                continue
            try:
                sheets.values().update(
                    spreadsheetId=SHEET_ID,
                    range=f"{SHEET_TAB}!{cell}",
                    valueInputOption="USER_ENTERED",
                    body={"values": [[val]]}
                ).execute()
            except Exception as cell_err:
                print(f"[Sheet] {cell} write failed: {cell_err}")
        time.sleep(1.5)
        result = sheets.values().batchGet(
            spreadsheetId=SHEET_ID,
            ranges=[f"{SHEET_TAB}!C27", f"{SHEET_TAB}!C28"],
        ).execute()
        vr  = result.get("valueRanges", [])
        c27 = vr[0]["values"][0][0] if vr[0].get("values") else ""
        c28 = vr[1]["values"][0][0] if vr[1].get("values") else ""
        return {"C27": c27, "C28": c28}
    except Exception as e:
        tb = traceback.format_exc()
        return JSONResponse({"error": str(e), "detail": tb}, status_code=500)

# ── In-memory session ──────────────────────────────────────────────────────────
SESSION = {
    "raw_poly": None, "geo_block_name": None, "doc_out": None,
    "best_layout": [], "best_step_w": 0, "best_step_h": 0,
    "best_count_col": 0, "best_count_row": 0,
    "final_polys_with_info": [], "stats": {},
    "base_lines": [],
    "interactive_lines": [],
    "source_connectors": [],
    "propagated_connectors": [],
    "manual_connectors": [],
    "lines_history": [],
}


def _rebuild_manual_connectors():
    SESSION["manual_connectors"] = (
        list(SESSION["source_connectors"]) +
        list(SESSION["propagated_connectors"])
    )


def _serialize_connectors(conns):
    return [
        {"id": c.get("id", ""), "lx": c["lx"], "ly": c["ly"],
         "rx": c["rx"], "ry": c["ry"], "r": c["r"], "ang": c["ang"]}
        for c in conns
    ]


# ── Geometry helpers ───────────────────────────────────────────────────────────
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
            elif entity.dxftype() == 'SPLINE':
                spline_flags = entity.dxf.flags if entity.dxf.hasattr('flags') else 0
                is_closed   = bool(spline_flags & 1)
                is_rational = bool(spline_flags & 4)
                has_ctrl    = len(list(entity.control_points)) > 0
                if is_rational and is_closed and has_ctrl:
                    from ezdxf.math import BSpline
                    import numpy as np
                    ctrl    = [(float(p[0]), float(p[1])) for p in entity.control_points]
                    knots   = list(entity.knots)
                    weights = list(entity.weights) if entity.weights else None
                    degree  = entity.dxf.degree
                    bsp     = BSpline(ctrl, order=degree+1, knots=knots, weights=weights)
                    t0, t1  = knots[degree], knots[-(degree+1)]
                    n_pts   = max(256, len(ctrl) * 6)
                    pts_xy  = []
                    for t in np.linspace(t0, t1, n_pts, endpoint=False):
                        p = bsp.point(t)
                        pts_xy.append((p.x, p.y))
                    pts_xy.append(pts_xy[0])
                    poly = Polygon(pts_xy)
                else:
                    pts = list(make_path(entity).flattening(distance=0.005))
                    if len(pts) < 3: continue
                    poly = Polygon([(p.x, p.y) for p in pts])
            else:
                pts = list(make_path(entity).flattening(distance=0.005))
                if len(pts) < 3: continue
                poly = Polygon([(p.x, p.y) for p in pts])
            if not poly.is_valid: poly = poly.buffer(0)
            if poly.is_valid and not poly.is_empty and poly.area > 0.01:
                all_polys.append(poly)
        except Exception as e:
            print(f"[ERROR] {entity.dxftype()} failed: {e}")
            continue
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
    while step >= 0.01:
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
            if no_improve_count > 6: break
        else:
            no_improve_count = 0
    return best_dx, best_dy, best_b, best_area

def compact_layout(fps, spacing, mode="Matrix"):
    if len(fps) < 2:
        return fps

    polys = [p for p, a, f, c in fps]
    angs  = [a for p, a, f, c in fps]
    flags = [f for p, a, f, c in fps]

    TOL = 0.005

    def _make_fps(new_polys):
        return [(new_polys[i], angs[i], flags[i],
                 (new_polys[i].centroid.x, new_polys[i].centroid.y))
                for i in range(len(new_polys))]

    if "Nesting" in mode:
        pair_count = len(polys) // 2
        units = [unary_union([polys[i*2], polys[i*2+1]]) for i in range(pair_count)]

        row_tol = (units[0].bounds[3] - units[0].bounds[1]) * 0.5

        sorted_idxs = sorted(range(pair_count),
                             key=lambda i: (round(units[i].centroid.y, 1),
                                            round(units[i].centroid.x, 1)))

        rows_of_pairs = []
        cur_row = [sorted_idxs[0]]
        for k in range(1, len(sorted_idxs)):
            prev = sorted_idxs[k-1]
            curr = sorted_idxs[k]
            if abs(units[curr].centroid.y - units[prev].centroid.y) < row_tol:
                cur_row.append(curr)
            else:
                rows_of_pairs.append(cur_row)
                cur_row = [curr]
        rows_of_pairs.append(cur_row)

        pair_pos = {}
        for ri, row in enumerate(rows_of_pairs):
            row_sorted = sorted(row, key=lambda i: units[i].centroid.x)
            for ci, idx in enumerate(row_sorted):
                pair_pos[idx] = (ri, ci)
        pos_to_idx = {v: k for k, v in pair_pos.items()}

        num_cols = max(ci for _, ci in pair_pos.values()) + 1
        num_rows = len(rows_of_pairs)

        # 初始化 new_units：直接從 units 複製
        new_units = list(units)

        def left_col_nbrs(ci, ri):
            result = []
            for dr in [-1, 0, 1]:
                nidx = pos_to_idx.get((ri + dr, ci - 1))
                if nidx is not None:
                    result.append(new_units[nidx])
            return result

        def below_row_nbrs(ci, ri):
            result = []
            for dc in [-1, 0, 1]:
                nidx = pos_to_idx.get((ri - 1, ci + dc))
                if nidx is not None:
                    result.append(new_units[nidx])
            return result

        def col_idxs_n(ci):
            return [idx for idx, (_, c) in pair_pos.items() if c == ci]

        # ── 第一階段：逐欄推 X（整欄統一平移）────────────────────────────────
        for ci in range(1, num_cols):
            idxs = col_idxs_n(ci)
            max_shifts = []
            for idx in idxs:
                ri, _ = pair_pos[idx]
                u = new_units[idx]
                nbrs = left_col_nbrs(ci, ri)
                if not nbrs:
                    max_shifts.append(float('inf'))
                    continue
                cur_min = min(n.distance(u) for n in nbrs)
                if cur_min <= spacing + TOL:
                    max_shifts.append(0.0)
                    continue
                hi = cur_min - spacing + 0.5
                lo = 0.0
                for _ in range(32):
                    mid = (lo + hi) / 2
                    test = translate(u, -mid, 0)
                    if all(n.distance(test) >= spacing for n in nbrs):
                        lo = mid
                    else:
                        hi = mid
                max_shifts.append(lo)

            col_shift = min(s for s in max_shifts if s != float('inf')) if any(
                s != float('inf') for s in max_shifts) else 0.0
            if col_shift > TOL:
                for idx in idxs:
                    new_units[idx] = translate(new_units[idx], -col_shift, 0)

        # ── 第二階段：逐行推 Y ────────────────────────────────────────────────
        for ri in range(1, num_rows):
            for ci in range(num_cols):
                idx = pos_to_idx.get((ri, ci))
                if idx is None:
                    continue
                u = new_units[idx]
                nbrs = below_row_nbrs(ci, ri)
                if not nbrs:
                    continue
                cur_min = min(n.distance(u) for n in nbrs)
                if cur_min <= spacing + TOL:
                    continue
                hi = cur_min - spacing + 0.5
                lo = 0.0
                for _ in range(32):
                    mid = (lo + hi) / 2
                    test = translate(u, 0, -mid)
                    if all(n.distance(test) >= spacing for n in nbrs):
                        lo = mid
                    else:
                        hi = mid
                if lo > TOL:
                    new_units[idx] = translate(u, 0, -lo)

        # ── 第三階段以後：X/Y 交替補推直到收斂（最多 6 輪）────────────────────
        for _pass in range(6):
            total_moved = 0.0

            # X 補推（逐欄整欄平移）
            for ci in range(1, num_cols):
                idxs = col_idxs_n(ci)
                max_shifts = []
                for idx in idxs:
                    ri, _ = pair_pos[idx]
                    u = new_units[idx]
                    nbrs = left_col_nbrs(ci, ri)
                    if not nbrs:
                        max_shifts.append(float('inf'))
                        continue
                    cur_min = min(n.distance(u) for n in nbrs)
                    if cur_min <= spacing + TOL:
                        max_shifts.append(0.0)
                        continue
                    hi = cur_min - spacing + 0.5
                    lo = 0.0
                    for _ in range(32):
                        mid = (lo + hi) / 2
                        test = translate(u, -mid, 0)
                        if all(n.distance(test) >= spacing for n in nbrs):
                            lo = mid
                        else:
                            hi = mid
                    max_shifts.append(lo)

                col_shift = min(s for s in max_shifts if s != float('inf')) if any(
                    s != float('inf') for s in max_shifts) else 0.0
                if col_shift > TOL:
                    for idx in idxs:
                        new_units[idx] = translate(new_units[idx], -col_shift, 0)
                    total_moved += col_shift

            # Y 補推（逐行）
            for ri in range(1, num_rows):
                for ci in range(num_cols):
                    idx = pos_to_idx.get((ri, ci))
                    if idx is None:
                        continue
                    u = new_units[idx]
                    nbrs = below_row_nbrs(ci, ri)
                    if not nbrs:
                        continue
                    cur_min = min(n.distance(u) for n in nbrs)
                    if cur_min <= spacing + TOL:
                        continue
                    hi = cur_min - spacing + 0.5
                    lo = 0.0
                    for _ in range(32):
                        mid = (lo + hi) / 2
                        test = translate(u, 0, -mid)
                        if all(n.distance(test) >= spacing for n in nbrs):
                            lo = mid
                        else:
                            hi = mid
                    if lo > TOL:
                        new_units[idx] = translate(u, 0, -lo)
                        total_moved += lo

            if total_moved < TOL:
                break

        # ── 將 new_units 的位移同步回 polys ──────────────────────────────────
        new_polys = list(polys)
        for i in range(pair_count):
            orig_unit = units[i]
            new_unit  = new_units[i]
            dx = new_unit.centroid.x - orig_unit.centroid.x
            dy = new_unit.centroid.y - orig_unit.centroid.y
            new_polys[i*2]   = translate(polys[i*2],   dx, dy)
            new_polys[i*2+1] = translate(polys[i*2+1], dx, dy)

        return _make_fps(new_polys)

    elif "V-Cut" in mode:
        # V-Cut：純 bbox 最緊密排列（零間距，所有板子對齊到同一步距）
        # 所有 poly 的 bbox 尺寸相同，直接用第一個 poly 的 bbox 當步距基準。
        new_polys = list(polys)

        # 取所有 poly bbox 的最大寬高作為統一步距（V-Cut 不留 spacing）
        step_w = max(p.bounds[2] - p.bounds[0] for p in polys)
        step_h = max(p.bounds[3] - p.bounds[1] for p in polys)

        avg_h = sum(p.bounds[3] - p.bounds[1] for p in polys) / len(polys)
        row_tol = avg_h * 0.5

        # 分 row
        by_y = sorted(range(len(polys)), key=lambda i: polys[i].centroid.y)
        rows = []
        cur_row = [by_y[0]]
        for k in range(1, len(by_y)):
            i, j = by_y[k], by_y[k-1]
            if abs(polys[i].centroid.y - polys[j].centroid.y) < row_tol:
                cur_row.append(by_y[k])
            else:
                rows.append(cur_row)
                cur_row = [by_y[k]]
        rows.append(cur_row)

        # 分 col（用原始 polys 分欄，不受推擠影響）
        avg_w = sum(p.bounds[2] - p.bounds[0] for p in polys) / len(polys)
        col_tol = avg_w * 0.5
        by_x = sorted(range(len(polys)), key=lambda i: polys[i].centroid.x)
        cols = []
        cur_col = [by_x[0]]
        for k in range(1, len(by_x)):
            i, j = by_x[k], by_x[k-1]
            if abs(polys[i].centroid.x - polys[j].centroid.x) < col_tol:
                cur_col.append(by_x[k])
            else:
                cols.append(cur_col)
                cur_col = [by_x[k]]
        cols.append(cur_col)

        num_rows = len(rows)
        num_cols = len(cols)

        # 建立 (ri, ci) -> idx 對照
        # rows 由下到上（by_y 是從小 y 開始），cols 由左到右
        pos_to_orig = {}  # (ri, ci) -> original idx
        for ri, row in enumerate(rows):
            row_sorted = sorted(row, key=lambda i: polys[i].centroid.x)
            for ci, idx in enumerate(row_sorted):
                pos_to_orig[(ri, ci)] = idx

        # 錨點：(0, 0) 的 poly 左下角固定不動
        anchor = new_polys[pos_to_orig[(0, 0)]]
        anchor_x0 = anchor.bounds[0]
        anchor_y0 = anchor.bounds[1]

        # 所有 poly 重新對齊到整齊的 bbox 格子
        for ri in range(num_rows):
            for ci in range(num_cols):
                idx = pos_to_orig.get((ri, ci))
                if idx is None:
                    continue
                p = polys[idx]
                target_x = anchor_x0 + ci * step_w
                target_y = anchor_y0 + ri * step_h
                dx = target_x - p.bounds[0]
                dy = target_y - p.bounds[1]
                new_polys[idx] = translate(p, dx, dy)

        return _make_fps(new_polys)

    else:
        # Matrix：形狀感知緊密推擠
        # 架構：先逐欄推 X（整欄統一平移），再逐欄推 Y（整欄統一平移）
        # 這樣同一欄內所有 poly 的間距保證一致。
        n_polys = len(polys)

        avg_h = sum(p.bounds[3] - p.bounds[1] for p in polys) / n_polys
        avg_w = sum(p.bounds[2] - p.bounds[0] for p in polys) / n_polys
        row_tol = avg_h * 0.5

        # ── 建立 row / col 索引 ───────────────────────────────────────────────
        sorted_by_y = sorted(range(n_polys), key=lambda i: polys[i].centroid.y)
        rows_of_polys = []
        cur_row = [sorted_by_y[0]]
        for k in range(1, len(sorted_by_y)):
            prev, curr = sorted_by_y[k-1], sorted_by_y[k]
            if abs(polys[curr].centroid.y - polys[prev].centroid.y) < row_tol:
                cur_row.append(curr)
            else:
                rows_of_polys.append(cur_row)
                cur_row = [curr]
        rows_of_polys.append(cur_row)

        poly_pos = {}   # idx -> (ri, ci)
        for ri, row in enumerate(rows_of_polys):
            row_sorted = sorted(row, key=lambda i: polys[i].centroid.x)
            for ci, idx in enumerate(row_sorted):
                poly_pos[idx] = (ri, ci)
        pos_to_idx = {v: k for k, v in poly_pos.items()}

        num_cols = max(ci for _, ci in poly_pos.values()) + 1
        num_rows = len(rows_of_polys)

        new_polys = list(polys)

        def col_idxs(ci):
            return [idx for idx, (_, c) in poly_pos.items() if c == ci]

        def row_idxs(ri):
            return [idx for idx, (r, _) in poly_pos.items() if r == ri]

        def left_col_neighbors(ci, ri):
            """回傳左欄（ci-1）中與 row ri 相鄰的所有 poly（ri-1, ri, ri+1）"""
            result = []
            for dr in [-1, 0, 1]:
                nidx = pos_to_idx.get((ri + dr, ci - 1))
                if nidx is not None:
                    result.append(new_polys[nidx])
            return result

        def below_row_neighbors(ci, ri):
            """回傳下方 row（ri-1）中與 col ci 相鄰的所有 poly（ci-1, ci, ci+1）"""
            result = []
            for dc in [-1, 0, 1]:
                nidx = pos_to_idx.get((ri - 1, ci + dc))
                if nidx is not None:
                    result.append(new_polys[nidx])
            return result

        # ── 第一階段：逐欄推 X ────────────────────────────────────────────────
        # 對每一欄（ci >= 1），找出該欄能往左平移的最大量，
        # 限制條件：欄內每個 poly 跟左欄所有鄰居都必須保持 >= spacing。
        # 取所有 poly 中最小的可移動量 → 整欄統一平移。
        for ci in range(1, num_cols):
            idxs = col_idxs(ci)
            if not idxs:
                continue

            # 每個 poly 各自能往左移多少
            max_shifts = []
            for idx in idxs:
                ri, _ = poly_pos[idx]
                u = new_polys[idx]
                nbrs = left_col_neighbors(ci, ri)
                if not nbrs:
                    max_shifts.append(float('inf'))
                    continue

                # 先確認目前距離
                cur_min_dist = min(n.distance(u) for n in nbrs)
                if cur_min_dist <= spacing + TOL:
                    max_shifts.append(0.0)
                    continue

                # 二分搜尋：最多能往左移多少
                hi = cur_min_dist - spacing + 0.5
                lo = 0.0
                for _ in range(32):
                    mid = (lo + hi) / 2
                    test = translate(u, -mid, 0)
                    if all(n.distance(test) >= spacing for n in nbrs):
                        lo = mid
                    else:
                        hi = mid
                max_shifts.append(lo)

            # 整欄取最小值（最保守）
            col_shift = min(s for s in max_shifts if s != float('inf')) if any(
                s != float('inf') for s in max_shifts) else 0.0

            if col_shift > TOL:
                for idx in idxs:
                    new_polys[idx] = translate(new_polys[idx], -col_shift, 0)

        # ── 第二階段：逐欄推 Y（由下到上每 row）─────────────────────────────
        # 對每一欄 ci，從 row 1 開始往上，找出該欄 row ri 能往下平移的最大量，
        # 限制：跟下方 row（ri-1）所有相鄰 poly 保持 >= spacing。
        # 同樣取最小值 → 整欄同 row 統一平移（逐 row 處理）。
        for ri in range(1, num_rows):
            for ci in range(num_cols):
                idx = pos_to_idx.get((ri, ci))
                if idx is None:
                    continue
                u = new_polys[idx]
                nbrs = below_row_neighbors(ci, ri)
                if not nbrs:
                    continue

                cur_min_dist = min(n.distance(u) for n in nbrs)
                if cur_min_dist <= spacing + TOL:
                    continue

                hi = cur_min_dist - spacing + 0.5
                lo = 0.0
                for _ in range(32):
                    mid = (lo + hi) / 2
                    test = translate(u, 0, -mid)
                    if all(n.distance(test) >= spacing for n in nbrs):
                        lo = mid
                    else:
                        hi = mid
                if lo > TOL:
                    new_polys[idx] = translate(u, 0, -lo)

        # ── 第三階段以後：X/Y 交替補推直到收斂（最多 6 輪）────────────────────
        # 每推一次 X 會讓左欄更緊，右欄因此可以再跟進；
        # 每推一次 Y 同理。迭代直到整體位移量 < TOL 為止。
        for _pass in range(6):
            total_moved = 0.0

            # X 補推（逐欄整欄平移）
            for ci in range(1, num_cols):
                idxs = col_idxs(ci)
                if not idxs:
                    continue
                max_shifts = []
                for idx in idxs:
                    ri, _ = poly_pos[idx]
                    u = new_polys[idx]
                    nbrs = left_col_neighbors(ci, ri)
                    if not nbrs:
                        max_shifts.append(float('inf'))
                        continue
                    cur_min_dist = min(n.distance(u) for n in nbrs)
                    if cur_min_dist <= spacing + TOL:
                        max_shifts.append(0.0)
                        continue
                    hi = cur_min_dist - spacing + 0.5
                    lo = 0.0
                    for _ in range(32):
                        mid = (lo + hi) / 2
                        test = translate(u, -mid, 0)
                        if all(n.distance(test) >= spacing for n in nbrs):
                            lo = mid
                        else:
                            hi = mid
                    max_shifts.append(lo)

                col_shift = min(s for s in max_shifts if s != float('inf')) if any(
                    s != float('inf') for s in max_shifts) else 0.0
                if col_shift > TOL:
                    for idx in idxs:
                        new_polys[idx] = translate(new_polys[idx], -col_shift, 0)
                    total_moved += col_shift

            # Y 補推（逐行）
            for ri in range(1, num_rows):
                for ci in range(num_cols):
                    idx = pos_to_idx.get((ri, ci))
                    if idx is None:
                        continue
                    u = new_polys[idx]
                    nbrs = below_row_neighbors(ci, ri)
                    if not nbrs:
                        continue
                    cur_min_dist = min(n.distance(u) for n in nbrs)
                    if cur_min_dist <= spacing + TOL:
                        continue
                    hi = cur_min_dist - spacing + 0.5
                    lo = 0.0
                    for _ in range(32):
                        mid = (lo + hi) / 2
                        test = translate(u, 0, -mid)
                        if all(n.distance(test) >= spacing for n in nbrs):
                            lo = mid
                        else:
                            hi = mid
                    if lo > TOL:
                        new_polys[idx] = translate(u, 0, -lo)
                        total_moved += lo

            if total_moved < TOL:
                break

        return _make_fps(new_polys)


def _group_by_coord(coords, tol=30):
    if not coords:
        return []
    indexed = sorted(enumerate(coords), key=lambda x: x[1])
    groups = [[indexed[0][0]]]
    for idx, val in indexed[1:]:
        if abs(val - coords[groups[-1][-1]]) < tol:
            groups[-1].append(idx)
        else:
            groups.append([idx])
    return groups


def _push_shape_x(pa, pb, spacing, tol=0.005):
    if pb.bounds[0] < pa.bounds[2] - 0.1:
        return pb
    cur_dist = pa.distance(pb)
    if cur_dist <= spacing + tol:
        return pb
    max_shift = cur_dist - spacing
    if max_shift <= 0:
        return pb
    lo, hi = 0.0, max_shift+0.02
    for _ in range(24):
        mid = (lo + hi) / 2
        pb_test = translate(pb, -mid, 0)
        if pa.distance(pb_test) < spacing:
            hi = mid
        else:
            lo = mid
    result = translate(pb, -lo, 0)
    if pa.distance(result) < spacing:
        return pb
    return result


def _push_shape_y(pa, pb, spacing, tol=0.005):
    if pb.bounds[1] < pa.bounds[3] - 0.1:
        return pb
    cur_dist = pa.distance(pb)
    if cur_dist <= spacing + tol:
        return pb
    max_shift = cur_dist - spacing
    if max_shift <= 0:
        return pb
    lo, hi = 0.0, max_shift+0.02
    for _ in range(24):
        mid = (lo + hi) / 2
        pb_test = translate(pb, 0, -mid)
        if pa.distance(pb_test) < spacing:
            hi = mid
        else:
            lo = mid
    result = translate(pb, 0, -lo)
    if pa.distance(result) < spacing:
        return pb
    return result


def _run_nesting_single(raw_poly, params, progress_cb=None):
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

        from concurrent.futures import ThreadPoolExecutor, as_completed

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
    elif "V-Cut" in mode:
        max_found = 0
        for ang in [0, 90, 180, 270]:
            tb = rotate(raw_poly, ang, origin=(0,0))
            sx, sy, ex, ey = tb.bounds
            sw, sh = ex-sx, ey-sy
            stx, sty = sw, sh
            temp = []
            cy2 = db
            while cy2 + sh <= ch - ub + 0.001:
                cx2 = lb
                while cx2 + sw <= cw - rb + 0.001:
                    tp = translate(tb, cx2-sx, cy2-sy)
                    temp.append((tp, ang, False, (tp.centroid.x, tp.centroid.y)))
                    cx2 += stx
                cy2 += sty
            if len(temp) > max_found:
                max_found = len(temp); best_layout = temp
                bsw, bsh = stx, sty
                bcc, bcr = int(eff_w // stx), int(eff_h // sty)
        if progress_cb:
            progress_cb(1.0)
    else:  # Matrix
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


def run_nesting(raw_poly, params, progress_cb=None):
    def cb_a(pct):
        if progress_cb: progress_cb(pct * 0.5)

    result_a = _run_nesting_single(raw_poly, params, cb_a)
    count_a  = len(result_a[0])

    swapped = abs(params["panel_w"] - params["panel_h"]) > 0.01
    result_b = None
    count_b  = -1
    if swapped:
        params_b = dict(params)
        params_b["panel_w"] = params["panel_h"]
        params_b["panel_h"] = params["panel_w"]

        def cb_b(pct):
            if progress_cb: progress_cb(0.5 + pct * 0.5)

        result_b = _run_nesting_single(raw_poly, params_b, cb_b)
        count_b  = len(result_b[0])
    else:
        if progress_cb: progress_cb(1.0)

    if result_b is not None and count_b > count_a:
        print(f"[Nesting] 交換方向勝出: {count_b} > {count_a} pcs")
        params["panel_w"] = params_b["panel_w"]
        params["panel_h"] = params_b["panel_h"]
        return result_b
    else:
        print(f"[Nesting] 原始方向勝出: {count_a} pcs (swapped={count_b})")
        return result_a


def build_interactive_lines_from_dxf(src_path, fps, cx_cy_orig, raw_poly, offset_dist=2.0):
    import math as _math

    lines = []
    dxf_arcs = []
    has_non_arc = False
    try:
        doc = ezdxf.readfile(src_path)
        msp = doc.modelspace()
        entities = list(msp.query('*[layer=="BOARD_OUTLINE_00"]'))
        if not entities:
            entities = list(msp.query('LWPOLYLINE POLYLINE CIRCLE ELLIPSE SPLINE ARC LINE'))
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
            else:
                has_non_arc = True
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
            rb_raw = rotate(raw_poly, ang_deg, origin=(0,0)).bounds
            pb = poly.bounds
            return rx + (pb[0] - rb_raw[0]), ry + (pb[1] - rb_raw[1])

        exterior_arcs = []
        for arc in dxf_arcs:
            ncx, ncy = world_pt(arc["cx"], arc["cy"])
            a0 = (arc["a0"] + ang_deg) % 360
            a1 = (arc["a1"] + ang_deg) % 360
            exterior_arcs.append({
                "ncx":round(ncx,4), "ncy":round(ncy,4),
                "r":round(arc["r"],4), "a0":round(a0,4), "a1":round(a1,4)
            })

        matched_exterior = set()

        for hole in poly.interiors:
            ring_poly = Polygon(hole)
            rcx, rcy = ring_poly.centroid.x, ring_poly.centroid.y
            import math as _m2
            hb = ring_poly.bounds
            r_est = ((hb[2]-hb[0]) + (hb[3]-hb[1])) / 4
            best_idx, best_dist = -1, 3.0
            for idx, ea in enumerate(exterior_arcs):
                d = _m2.sqrt((ea["ncx"]-rcx)**2 + (ea["ncy"]-rcy)**2)
                r_ratio = abs(ea["r"] - r_est) / max(r_est, 0.1)
                if d < best_dist and r_ratio < 0.15:
                    best_dist = d
                    best_idx = idx
            if best_idx >= 0:
                ea = exterior_arcs[best_idx]
                matched_exterior.add(best_idx)
                lines.append({"id":str(uuid.uuid4()), "type":"hole", "kind":"arc",
                               "cx":ea["ncx"], "cy":ea["ncy"],
                               "r":ea["r"], "a0":ea["a0"], "a1":ea["a1"]})
            else:
                hc = list(hole.coords)
                for i in range(len(hc)-1):
                    lines.append({"id":str(uuid.uuid4()), "type":"hole", "kind":"line",
                                   "coords":[[round(hc[i][0],4),round(hc[i][1],4)],
                                             [round(hc[i+1][0],4),round(hc[i+1][1],4)]]})

        for idx, ea in enumerate(exterior_arcs):
            if idx in matched_exterior:
                continue
            lines.append({"id":str(uuid.uuid4()), "type":"body", "kind":"arc",
                           "cx":ea["ncx"], "cy":ea["ncy"],
                           "r":ea["r"], "a0":ea["a0"], "a1":ea["a1"]})

        if has_non_arc:
            ec = list(poly.exterior.coords)
            for i in range(len(ec)-1):
                lines.append({"id":str(uuid.uuid4()), "type":"body", "kind":"line",
                               "coords":[[round(ec[i][0],4),round(ec[i][1],4)],
                                         [round(ec[i+1][0],4),round(ec[i+1][1],4)]]})

        try:
            op = poly.buffer(offset_dist, join_style=2, quad_segs=64)
            if op.geom_type == 'MultiPolygon':
                op = max(op.geoms, key=lambda g: g.area)
            oc = list(op.exterior.coords)
            for i in range(len(oc)-1):
                lines.append({"id":str(uuid.uuid4()), "type":"offset", "kind":"line",
                            "coords":[[round(oc[i][0],4),round(oc[i][1],4)],
                                        [round(oc[i+1][0],4),round(oc[i+1][1],4)]]})
        except: pass

    return lines

def compute_gaps(fp, mode, bsw, bsh, raw_poly, spacing):
    if len(fp) < 2:
        return 0.0, 0.0, None

    sorted_by_pos = sorted(fp, key=lambda p: (round(p.bounds[1], 1), round(p.bounds[0], 1)))
    anchor = sorted_by_pos[0]
    ax1, ay1, ax2, ay2 = anchor.bounds
    acx, acy = anchor.centroid.x, anchor.centroid.y

    if "Nesting" in mode:
        others = [p for p in fp if p is not anchor]
        nearest = min(others, key=lambda p:
            (p.centroid.x - acx)**2 + (p.centroid.y - acy)**2)
        nest_gap = round(anchor.distance(nearest), 3)

        right_candidates = [p for p in fp
                           if p is not anchor and p is not nearest
                           and p.centroid.x > ax2
                           and abs(p.centroid.y - acy) < bsh * 0.4]
        if right_candidates:
            right = min(right_candidates, key=lambda p: p.centroid.x)
            gap_x = round(right.bounds[0] - ax2, 3)
        else:
            gap_x = nest_gap

        up_candidates = [p for p in fp
                        if p is not anchor and p is not nearest
                        and p.centroid.y > ay2
                        and abs(p.centroid.x - acx) < bsw * 0.4]
        if up_candidates:
            up = min(up_candidates, key=lambda p: p.centroid.y)
            gap_y = round(up.bounds[1] - ay2, 3)
        else:
            gap_y = nest_gap

        return gap_x, gap_y, nest_gap

    elif "V-Cut" in mode:
        right_candidates = [p for p in fp
                           if p is not anchor and p.centroid.x > ax2
                           and abs(p.centroid.y - acy) < bsh * 0.4]
        gap_x = 0.0
        if right_candidates:
            right = min(right_candidates, key=lambda p: p.centroid.x)
            gap_x = round(right.bounds[0] - ax2, 3)

        up_candidates = [p for p in fp
                        if p is not anchor and p.centroid.y > ay2
                        and abs(p.centroid.x - acx) < bsw * 0.4]
        gap_y = 0.0
        if up_candidates:
            up = min(up_candidates, key=lambda p: p.centroid.y)
            gap_y = round(up.bounds[1] - ay2, 3)

        return gap_x, gap_y, None

    else:  # Matrix
        right_candidates = [p for p in fp
                           if p is not anchor and p.centroid.x > ax2
                           and abs(p.centroid.y - acy) < bsw * 0.4]
        gap_x = 0.0
        if right_candidates:
            right = min(right_candidates, key=lambda p: p.centroid.x)
            gap_x = round(right.bounds[0] - ax2, 3)

        up_candidates = [p for p in fp
                        if p is not anchor and p.centroid.y > ay2
                        and abs(p.centroid.x - acx) < bsw * 0.4]
        gap_y = 0.0
        if up_candidates:
            up = min(up_candidates, key=lambda p: p.centroid.y)
            gap_y = round(up.bounds[1] - ay2, 3)

        return gap_x, gap_y, None

def _translate_fps(fps, dx, dy):
    result = []
    for p, a, f, c in fps:
        np_ = translate(p, dx, dy)
        result.append((np_, a, f, (np_.centroid.x, np_.centroid.y)))
    return result

def build_output_data(best_layout, bsw, bsh, bcc, bcr, params, geo_block_name, doc_out, raw_poly):
    cw, ch = params["panel_w"], params["panel_h"]
    lb, rb, ub, db = params["left"], params["right"], params["top"], params["bottom"]
    spacing = params["spacing"]
    mode = params.get("mode", "")
    r_corner = params.get("corner_r", 4.0)
    msp_out = doc_out.modelspace()
    p_w = raw_poly.bounds[2]-raw_poly.bounds[0]
    p_h = raw_poly.bounds[3]-raw_poly.bounds[1]

    all_u = unary_union([b for b,a,f,c in best_layout])
    fox = (cw/2) - all_u.centroid.x
    foy = (ch/2) - all_u.centroid.y
    fps = [(translate(b,fox,foy), a, f, (c[0]+fox, c[1]+foy)) for b,a,f,c in best_layout]

    fps = compact_layout(fps, spacing, mode)

    fp = [p for p,a,f,c in fps]
    fu = unary_union(fp)
    fx1,fy1,fx2,fy2 = fu.bounds
    compw = (fx2-fx1)+lb+rb
    comph = (fy2-fy1)+ub+db
    cox, coy = lb-fx1, db-fy1

    new_fps = []
    for p, a, f, c in fps:
        np_ = translate(p, cox, coy)
        new_fps.append((np_, a, f, (np_.centroid.x, np_.centroid.y)))
    fps = new_fps
    fp = [p for p,a,f,c in fps]
    for poly, ang, _, (ax, ay) in fps:
        msp_out.add_blockref(geo_block_name, (ax, ay), dxfattribs={
            'rotation': ang, 'xscale': 1.0, 'yscale': 1.0,
            'layer': 'PART_BODY', 'color': 7})

    panel_geo = create_rounded_panel(compw, comph, r_corner)
    msp_out.add_lwpolyline(list(panel_geo.exterior.coords),
                           dxfattribs={'layer':'PANEL_FRAME','color':7,'closed':True})
    for cx, cy in [(4,4),(compw-4,4),(compw-4,comph-4),(4,comph-4)]:
        msp_out.add_circle((cx,cy), radius=1.53, dxfattribs={'layer':'PANEL_HOLES','color':7})

    def get_groups(coords, tol=10.0):
        if not coords: return []
        sc = sorted(coords); g = [[sc[0]]]
        for c in sc[1:]:
            if c-g[-1][-1]<tol: g[-1].append(c)
            else: g.append([c])
        return g

    mode = params.get("mode", "")
    gap_x, gap_y, nest_gap = compute_gaps(fp, mode, bsw, bsh, raw_poly, spacing)
    xg = get_groups([p.centroid.x for p in fp])
    yg = get_groups([p.centroid.y for p in fp])
    fu2 = unary_union(fp).bounds

    stats = {
        "pcs": len(fp), "p_w": round(p_w,3), "p_h": round(p_h,3),
        "utilization": round(sum(p.area for p in fp)/(compw*comph)*100, 2),
        "cols": len(xg), "rows": len(yg),
        "gap_x": gap_x, "gap_y": gap_y,
        "band_l": round(fu2[0],2), "band_r": round(compw-fu2[2],2),
        "band_b": round(fu2[1],2), "band_t": round(comph-fu2[3],2),
        "original_w": round(cw,3), "original_h": round(ch,3),
        "compressed_w": round(compw,3), "compressed_h": round(comph,3),
        "shrink_w": round(cw-compw,3), "shrink_h": round(ch-comph,3),
        "cur_cols": bcc, "cur_rows": bcr,
    }
    if nest_gap is not None:
        stats["nest_gap"] = nest_gap
    if bsw > 0 and bsh > 0:
        sew = (bcc+1)*bsw + bsw*0.01
        seh = (bcr+1)*bsh + bsh*0.01
        stats["suggest_w"] = round(sew+lb+rb, 3)
        stats["suggest_h"] = round(seh+ub+db, 3)
        stats["suggest_cols"] = bcc+1
        stats["suggest_rows"] = bcr+1
        stats["extra_w"] = round(sew+lb+rb-compw, 3)
        stats["extra_h"] = round(seh+ub+db-comph, 3)

    polys_data = []
    for poly, ang, _, (ax, ay) in fps:
        ext = [[x,y] for x,y in poly.exterior.coords]
        holes = [[[x,y] for x,y in h.coords] for h in poly.interiors]
        polys_data.append({"exterior": ext, "holes": holes, "ax": ax, "ay": ay, "ang": ang})

    return fps, stats, polys_data, compw, comph


def calculate_bridge(data, lines, connectors, gap_half=1.75, r=1.0):
    l1 = lines[data['idx1']]
    l2 = lines[data['idx2']]

    def to_shapely_line(seg):
        if seg.get('kind') == 'arc':
            return None
        return LineString(seg['coords'])

    line1 = to_shapely_line(l1)
    line2 = to_shapely_line(l2)

    if line1 is None or line2 is None:
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
    cut_segs_1 = split(line1, l1, p1, l1['type'])
    cut_segs_2 = split(line2, l2, p2, l2['type'])
    new_lines += cut_segs_1
    new_lines += cut_segs_2

    cut_ids = [s['id'] for s in cut_segs_1 + cut_segs_2]
    orig_l1 = dict(l1, id=str(uuid.uuid4()))
    orig_l2 = dict(l2, id=str(uuid.uuid4()))

    connector = {
        'type': 'precise_sandglass_arc',
        'lx': mx-ux*gap_half, 'ly': my-uy*gap_half,
        'rx': mx+ux*gap_half, 'ry': my+uy*gap_half,
        'r': r, 'ang': ang,
        'id': str(uuid.uuid4()),
        'cut_ids': cut_ids,
        'orig_l1': orig_l1,
        'orig_l2': orig_l2,
    }
    new_connectors = list(connectors) + [connector]
    return new_lines, new_connectors


def delete_bridge(connector_id, lines, connectors):
    target = next((c for c in connectors if c.get('id') == connector_id), None)
    if target is None:
        raise ValueError(f"connector {connector_id} not found")
    cut_ids = set(target.get('cut_ids', []))
    new_lines = [l for l in lines if l['id'] not in cut_ids]
    if target.get('orig_l1'):
        new_lines.append(target['orig_l1'])
    if target.get('orig_l2'):
        new_lines.append(target['orig_l2'])
    new_connectors = [c for c in connectors if c.get('id') != connector_id]
    return new_lines, new_connectors


def _connector_midpoint(c):
    return (c['lx'] + c['rx']) / 2, (c['ly'] + c['ry']) / 2


def _find_owning_pair(connector, fp_list):
    mx, my = _connector_midpoint(connector)
    pair_count = len(fp_list) // 2
    best_idx, best_dist = 0, float('inf')
    for i in range(pair_count):
        pa = fp_list[i * 2]
        pb = fp_list[i * 2 + 1]
        pcx = (pa.centroid.x + pb.centroid.x) / 2
        pcy = (pa.centroid.y + pb.centroid.y) / 2
        d = (mx - pcx) ** 2 + (my - pcy) ** 2
        if d < best_dist:
            best_dist = d
            best_idx = i
    return best_idx


def _find_owning_poly(connector, fp_list):
    mx, my = _connector_midpoint(connector)
    best_idx, best_dist = 0, float('inf')
    for i, poly in enumerate(fp_list):
        pcx, pcy = poly.centroid.x, poly.centroid.y
        d = (mx - pcx) ** 2 + (my - pcy) ** 2
        if d < best_dist:
            best_dist = d
            best_idx = i
    return best_idx


# ── REST endpoints ─────────────────────────────────────────────────────────────
@app.get("/")
async def index():
    return FileResponse(os.path.join(get_static_dir(), "index.html"))

@app.post("/upload")
async def upload_dxf(file: UploadFile = File(...)):
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
        SESSION['_orig_cx_cy'] = orig_cxy
        SESSION['base_lines'] = []
        SESSION['interactive_lines'] = []
        SESSION['source_connectors'] = []
        SESSION['propagated_connectors'] = []
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

            params_copy = dict(params)
            bl, bsw, bsh, bcc, bcr, top3 = run_nesting(raw, params_copy, progress_cb)
            if not bl:
                progress_queue.put(("error", "無法排版"))
                return

            fps, stats, polys_data, compw, comph = build_output_data(
                bl, bsw, bsh, bcc, bcr, params_copy, block_name, doc_out, raw)

            SESSION['best_layout'] = bl
            SESSION['best_step_w'] = bsw
            SESSION['best_step_h'] = bsh
            SESSION['best_count_col'] = bcc
            SESSION['best_count_row'] = bcr
            SESSION['last_params'] = dict(params_copy)
            SESSION['final_polys_with_info'] = fps
            SESSION['stats'] = stats
            SESSION['compressed_w'] = compw
            SESSION['compressed_h'] = comph
            SESSION['base_lines'] = []
            SESSION['interactive_lines'] = []
            SESSION['source_connectors'] = []
            SESSION['propagated_connectors'] = []
            SESSION['manual_connectors'] = []
            SESSION['lines_history'] = []

            offset_dist = float(params_copy.get("spacing", 2.0))
            if offset_dist <= 0:
                offset_dist = 0.5

            src_path = SESSION.get('_src_dxf_path')
            cx_cy = SESSION.get('_orig_cx_cy', (0.0, 0.0))
            if src_path and os.path.exists(src_path):
                lines = build_interactive_lines_from_dxf(src_path, fps, cx_cy, raw, offset_dist)
            else:
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
                        op = poly.buffer(offset_dist, join_style=2, quad_segs=64)
                        oc = list(op.exterior.coords)
                        for i in range(len(oc)-1):
                            lines.append({"id":str(uuid.uuid4()),
                                          "coords":[[oc[i][0],oc[i][1]],[oc[i+1][0],oc[i+1][1]]],
                                          "type":"offset","kind":"line"})
                    except: pass

            panel_geo = create_rounded_panel(compw, comph, params_copy.get('corner_r', 4.0))
            fc = list(panel_geo.exterior.coords)
            for i in range(len(fc)-1):
                lines.append({"id":str(uuid.uuid4()),
                               "coords":[[round(fc[i][0],4),round(fc[i][1],4)],
                                         [round(fc[i+1][0],4),round(fc[i+1][1],4)]],
                               "type":"frame","kind":"line"})

            SESSION['base_lines'] = json.loads(json.dumps(lines))
            SESSION['interactive_lines'] = lines

            top3_data = []
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
                       "compressed_w":compw, "compressed_h":comph,
                       "panel_w": params_copy['panel_w'],
                       "panel_h": params_copy['panel_h']}
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


# ─────────────────────────────────────────────────────────────────────────────
# Bridge state helpers
# ─────────────────────────────────────────────────────────────────────────────

def _apply_all_connectors_to_base():
    lines = json.loads(json.dumps(SESSION["base_lines"]))
    all_conns = list(SESSION["source_connectors"]) + list(SESSION["propagated_connectors"])
    spacing = float(SESSION.get("last_params", {}).get("spacing", 2.0))
    r_arc   = max(spacing / 2, 0.1)
    for c in all_conns:
        gap_half = math.sqrt((c["rx"]-c["lx"])**2 + (c["ry"]-c["ly"])**2) / 2
        orig_l1 = c.get("orig_l1")
        orig_l2 = c.get("orig_l2")
        if not orig_l1 or not orig_l2:
            continue
        def match(target, lines=lines):
            tc = target.get("coords")
            if not tc:
                return -1
            tmx = (tc[0][0] + tc[1][0]) / 2
            tmy = (tc[0][1] + tc[1][1]) / 2
            tdx = tc[1][0] - tc[0][0]
            tdy = tc[1][1] - tc[0][1]
            tlen = math.sqrt(tdx**2 + tdy**2)
            if tlen < 1e-9:
                return -1
            tux, tuy = tdx / tlen, tdy / tlen

            best_i = -1
            best_d = 0.8

            for i, l in enumerate(lines):
                if l.get("kind") == "arc":
                    continue
                lc = l.get("coords", [])
                if len(lc) < 2:
                    continue

                lx0, ly0 = lc[0][0], lc[0][1]
                lx1, ly1 = lc[1][0], lc[1][1]
                ldx, ldy = lx1 - lx0, ly1 - ly0
                ll = math.sqrt(ldx**2 + ldy**2)
                if ll < 1e-9:
                    continue
                lux, luy = ldx / ll, ldy / ll

                dot = abs(tux * lux + tuy * luy)
                if dot < 0.97:
                    continue

                proj = (tmx - lx0) * lux + (tmy - ly0) * luy
                if proj < -best_d or proj > ll + best_d:
                    continue

                perp = abs((tmx - lx0) * luy - (tmy - ly0) * lux)
                if perp < best_d:
                    best_d = perp
                    best_i = i

            return best_i
        idx1 = match(orig_l1)
        idx2 = match(orig_l2)
        if idx1 == -1 or idx2 == -1 or idx1 == idx2:
            continue
        smx = (c["lx"] + c["rx"]) / 2
        smy = (c["ly"] + c["ry"]) / 2
        data_fake = {"idx1": idx1, "idx2": idx2, "pt1": [smx, smy]}
        try:
            lines, _ = calculate_bridge(data_fake, lines, [], gap_half=gap_half, r=r_arc)
        except Exception as ex:
            print(f"[_apply_all] calculate_bridge failed: {ex}")
    SESSION["interactive_lines"] = lines


def _save_undo_snap():
    SESSION["lines_history"].append({
        "source_connectors":    json.loads(json.dumps(SESSION["source_connectors"])),
        "propagated_connectors": json.loads(json.dumps(SESSION["propagated_connectors"])),
    })


@app.post("/bridge")
async def bridge(data: dict):
    _save_undo_snap()
    spacing  = float(SESSION.get("last_params", {}).get("spacing", 2.0))
    r_arc    = max(spacing / 2, 0.1)
    bridge_w = float(data.get("bridge_w", 3.5))
    gap_half = max(bridge_w / 2, 0.1)
    is_array = bool(data.get("is_array", False))
    try:
        lines = SESSION["interactive_lines"]
        new_lines, new_src = calculate_bridge(data, lines, SESSION["source_connectors"],
                                              gap_half=gap_half, r=r_arc)
        if new_src:
            new_src[-1]["_is_array"] = is_array
        SESSION["source_connectors"] = new_src
        _apply_all_connectors_to_base()
        _rebuild_manual_connectors()
        new_id = new_src[-1]["id"] if new_src else None
        return {"ok": True, "lines": SESSION["interactive_lines"],
                "new_connector_id": new_id,
                "connectors": _serialize_connectors(SESSION["manual_connectors"])}
    except Exception as e:
        SESSION["lines_history"].pop()
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)


@app.post("/delete_bridge")
async def delete_bridge_single(data: dict):
    connector_id = data.get("connector_id")
    if not connector_id:
        return JSONResponse({"ok": False, "error": "Missing connector_id"}, status_code=400)
    _save_undo_snap()
    src_conns  = SESSION["source_connectors"]
    prop_conns = SESSION["propagated_connectors"]
    is_source  = any(c.get("id") == connector_id for c in src_conns)
    try:
        if is_source:
            SESSION["source_connectors"] = [c for c in src_conns if c.get("id") != connector_id]
        else:
            SESSION["propagated_connectors"] = [c for c in prop_conns if c.get("id") != connector_id]
        _apply_all_connectors_to_base()
        _rebuild_manual_connectors()
        return {"ok": True,
                "lines": SESSION["interactive_lines"],
                "connectors": _serialize_connectors(SESSION["manual_connectors"])}
    except Exception as e:
        SESSION["lines_history"].pop()
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)


@app.post("/delete_bridge_array")
async def delete_bridge_array_endpoint(data: dict):
    connector_id = data.get("connector_id")
    if not connector_id:
        return JSONResponse({"ok": False, "error": "Missing connector_id"}, status_code=400)
    src_conns  = SESSION["source_connectors"]
    prop_conns = SESSION["propagated_connectors"]
    fps        = SESSION.get("final_polys_with_info", [])
    mode       = SESSION.get("last_params", {}).get("mode", "")
    all_conns  = list(src_conns) + list(prop_conns)
    target = next((c for c in all_conns if c.get("id") == connector_id), None)
    if target is None:
        return JSONResponse({"ok": False, "error": "Connector not found"}, status_code=400)
    _save_undo_snap()
    fp_list = [p for p, a, f, c in fps]
    tmx, tmy = _connector_midpoint(target)
    if "Nesting" in mode:
        pair_count = len(fp_list) // 2
        src_pair   = _find_owning_pair(target, fp_list)
        ref_cx     = (fp_list[src_pair*2].centroid.x + fp_list[src_pair*2+1].centroid.x) / 2
        ref_cy     = (fp_list[src_pair*2].centroid.y + fp_list[src_pair*2+1].centroid.y) / 2
        local_x, local_y = tmx - ref_cx, tmy - ref_cy
        ids_to_delete = set()
        for i in range(pair_count):
            pcx = (fp_list[i*2].centroid.x + fp_list[i*2+1].centroid.x) / 2
            pcy = (fp_list[i*2].centroid.y + fp_list[i*2+1].centroid.y) / 2
            ex, ey = pcx + local_x, pcy + local_y
            for c in all_conns:
                cmx, cmy = _connector_midpoint(c)
                if abs(cmx - ex) < 0.5 and abs(cmy - ey) < 0.5:
                    ids_to_delete.add(c.get("id"))
    else:
        src_idx = _find_owning_poly(target, fp_list)
        ref_cx, ref_cy = fp_list[src_idx].centroid.x, fp_list[src_idx].centroid.y
        local_x, local_y = tmx - ref_cx, tmy - ref_cy
        ids_to_delete = set()
        for poly in fp_list:
            ex, ey = poly.centroid.x + local_x, poly.centroid.y + local_y
            for c in all_conns:
                cmx, cmy = _connector_midpoint(c)
                if abs(cmx - ex) < 0.5 and abs(cmy - ey) < 0.5:
                    ids_to_delete.add(c.get("id"))
    try:
        SESSION["source_connectors"]     = [c for c in src_conns  if c.get("id") not in ids_to_delete]
        SESSION["propagated_connectors"] = [c for c in prop_conns if c.get("id") not in ids_to_delete]
        _apply_all_connectors_to_base()
        _rebuild_manual_connectors()
        return {"ok": True,
                "lines": SESSION["interactive_lines"],
                "connectors": _serialize_connectors(SESSION["manual_connectors"]),
                "deleted": len(ids_to_delete)}
    except Exception as e:
        import traceback; traceback.print_exc()
        SESSION["lines_history"].pop()
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


@app.post("/undo")
async def undo():
    if SESSION["lines_history"]:
        snap = SESSION["lines_history"].pop()
        SESSION["source_connectors"]     = snap["source_connectors"]
        SESSION["propagated_connectors"] = snap["propagated_connectors"]
        _apply_all_connectors_to_base()
        _rebuild_manual_connectors()
    return {"ok": True,
            "lines": SESSION["interactive_lines"],
            "connectors": _serialize_connectors(SESSION["manual_connectors"])}


@app.post("/reset_bridge")
async def reset_bridge():
    SESSION["source_connectors"]     = []
    SESSION["propagated_connectors"] = []
    SESSION["manual_connectors"]     = []
    SESSION["lines_history"]         = []
    base = json.loads(json.dumps(SESSION.get("base_lines", [])))
    SESSION["interactive_lines"] = base
    return {"ok": True, "lines": base, "connectors": []}


@app.post("/propagate_bridge")
async def propagate_bridge(request: Request):
    body = {}
    try:
        body = await request.json()
    except Exception:
        pass

    all_src = SESSION.get("source_connectors", [])
    ids = body.get("connector_ids", None)
    if ids is not None:
        id_set = set(ids)
        to_propagate = [c for c in all_src
                        if c.get("id") in id_set and c.get("_is_array", False)]
    else:
        to_propagate = [c for c in all_src if c.get("_is_array", False)]

    fps     = SESSION.get("final_polys_with_info", [])
    mode    = SESSION.get("last_params", {}).get("mode", "")
    spacing = float(SESSION.get("last_params", {}).get("spacing", 2.0))
    r_arc   = max(spacing / 2, 0.1)

    if not to_propagate:
        return JSONResponse({"ok": False, "error": "No bridges to propagate"}, status_code=400)
    if not fps:
        return JSONResponse({"ok": False, "error": "No layout available"}, status_code=400)

    try:
        fp_list = [p for p, a, f, c in fps]

        if "Nesting" in mode:
            pair_count = len(fp_list) // 2
            pair_cx = [(fp_list[i*2].centroid.x + fp_list[i*2+1].centroid.x)/2 for i in range(pair_count)]
            pair_cy = [(fp_list[i*2].centroid.y + fp_list[i*2+1].centroid.y)/2 for i in range(pair_count)]
            copies  = pair_count
            def get_offsets(c):
                s = _find_owning_pair(c, fp_list)
                return [(pair_cx[i]-pair_cx[s], pair_cy[i]-pair_cy[s]) for i in range(pair_count)]
        else:
            copies = len(fp_list)
            def get_offsets(c):
                s = _find_owning_poly(c, fp_list)
                rcx, rcy = fp_list[s].centroid.x, fp_list[s].centroid.y
                return [(p.centroid.x-rcx, p.centroid.y-rcy) for p in fp_list]

        propagating_ids = {c["id"] for c in to_propagate}
        kept_prop = [c for c in SESSION["propagated_connectors"]
                     if c.get("_src_id") not in propagating_ids]
        new_propagated = list(kept_prop)

        for src_c in to_propagate:
            orig_l1 = src_c.get("orig_l1")
            orig_l2 = src_c.get("orig_l2")

            for dx, dy in get_offsets(src_c):
                if abs(dx) < 0.01 and abs(dy) < 0.01:
                    continue

                if orig_l1 and orig_l1.get("coords"):
                    exp_l1_coords = [
                        [orig_l1["coords"][0][0]+dx, orig_l1["coords"][0][1]+dy],
                        [orig_l1["coords"][1][0]+dx, orig_l1["coords"][1][1]+dy],
                    ]
                    exp_l1 = {"coords": exp_l1_coords, "kind": "line",
                               "type": orig_l1.get("type", "offset"), "id": str(uuid.uuid4())}
                else:
                    exp_l1 = None

                if orig_l2 and orig_l2.get("coords"):
                    exp_l2_coords = [
                        [orig_l2["coords"][0][0]+dx, orig_l2["coords"][0][1]+dy],
                        [orig_l2["coords"][1][0]+dx, orig_l2["coords"][1][1]+dy],
                    ]
                    exp_l2 = {"coords": exp_l2_coords, "kind": "line",
                               "type": orig_l2.get("type", "offset"), "id": str(uuid.uuid4())}
                else:
                    exp_l2 = None

                new_propagated.append({
                    "type":  "precise_sandglass_arc",
                    "lx": src_c["lx"]+dx, "ly": src_c["ly"]+dy,
                    "rx": src_c["rx"]+dx, "ry": src_c["ry"]+dy,
                    "r": r_arc, "ang": src_c["ang"],
                    "id": str(uuid.uuid4()),
                    "_src_id": src_c["id"],
                    "orig_l1": exp_l1,
                    "orig_l2": exp_l2,
                    "cut_ids": [],
                })

        SESSION["propagated_connectors"] = new_propagated
        _apply_all_connectors_to_base()
        _rebuild_manual_connectors()

        return {
            "ok": True,
            "lines":      SESSION["interactive_lines"],
            "connectors": _serialize_connectors(SESSION["manual_connectors"]),
            "copies":     copies,
        }
    except Exception as e:
        import traceback; traceback.print_exc()
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


@app.post("/adjust")
async def adjust(data: dict):
    mode    = SESSION.get('last_params', {}).get('mode', '')
    bsw     = SESSION.get('best_step_w', 0)
    bsh     = SESSION.get('best_step_h', 0)
    bcc     = SESSION.get('best_count_col', 0)
    bcr     = SESSION.get('best_count_row', 0)

    params  = SESSION.get('last_params')
    raw     = SESSION.get('raw_poly')
    block_name  = SESSION.get('geo_block_name')
    doc_out_old = SESSION.get('doc_out')
    src_path    = SESSION.get('_src_dxf_path')

    if not params or not raw or bsw == 0 or bsh == 0:
        return JSONResponse({"ok":False,"error":"No layout available"}, status_code=400)

    dcol = int(data.get('dcol', 0))
    drow = int(data.get('drow', 0))
    new_col = max(1, bcc + dcol)
    new_row = max(1, bcr + drow)

    lb = params['left']; rb = params['right']
    ub = params['top'];  db = params['bottom']

    try:
        doc_out = ezdxf.new('R2010')
        if src_path and os.path.exists(src_path):
            _, bn, _, _ = get_full_polygon_with_holes(src_path, doc_out)
        else:
            bn = block_name
            if doc_out_old and block_name in doc_out_old.blocks:
                nb = doc_out.blocks.new(name=block_name)
                for ent in doc_out_old.blocks[block_name]:
                    try: nb.add_entity(ent.copy())
                    except: pass

        if "Nesting" in mode and SESSION.get('best_layout'):
            orig_bl = SESSION['best_layout']
            if len(orig_bl) >= 2:
                p_a, ang_a, _, (tx_a0, ty_a0) = orig_bl[0]
                p_b, ang_b, _, (tx_b0, ty_b0) = orig_bl[1]
                dx_final = tx_b0 - tx_a0
                dy_final = ty_b0 - ty_a0
                from shapely.affinity import rotate as shp_rotate
                ba_ref = shp_rotate(raw, ang_a, origin=(0,0))
                bb_ref = shp_rotate(raw, ang_b, origin=(0,0))
                tb_placed = translate(bb_ref, dx_final, dy_final)
                cb = unary_union([ba_ref, tb_placed]).bounds
                offset_x, offset_y = cb[0], cb[1]
                new_bl = []
                for r in range(new_row):
                    for c in range(new_col):
                        tx = lb + c * bsw - offset_x
                        ty = db + r * bsh - offset_y
                        fa = translate(ba_ref, tx, ty)
                        fb = translate(tb_placed, tx, ty)
                        new_bl.append((fa, ang_a, False, (tx, ty)))
                        new_bl.append((fb, ang_b, False, (tx + dx_final, ty + dy_final)))
            else:
                new_bl = orig_bl
        else:
            orig_bl = SESSION.get('best_layout', [])
            ang = orig_bl[0][1] if orig_bl else 0
            tb = rotate(raw, ang, origin=(0,0))
            sx, sy, ex, ey = tb.bounds
            new_bl = []
            for r in range(new_row):
                for c in range(new_col):
                    cx2 = lb + c * bsw
                    cy2 = db + r * bsh
                    tp = translate(tb, cx2-sx, cy2-sy)
                    new_bl.append((tp, ang, False, (tp.centroid.x, tp.centroid.y)))

        if not new_bl:
            return JSONResponse({"ok":False,"error":"Cannot build layout"}, status_code=400)

        new_params = dict(params)
        new_params['panel_w'] = round(new_col * bsw + lb + rb, 4)
        new_params['panel_h'] = round(new_row * bsh + ub + db, 4)

        fps, stats, polys_data, compw, comph = build_output_data(
            new_bl, bsw, bsh, new_col, new_row,
            new_params, bn, doc_out, raw)

        SESSION['best_layout']    = new_bl
        SESSION['best_count_col'] = new_col
        SESSION['best_count_row'] = new_row
        SESSION['last_params']    = new_params
        SESSION['doc_out']        = doc_out
        SESSION['geo_block_name'] = bn
        SESSION['final_polys_with_info'] = fps
        SESSION['stats']          = stats
        SESSION['compressed_w']   = compw
        SESSION['compressed_h']   = comph
        SESSION['base_lines'] = []
        SESSION['interactive_lines'] = []
        SESSION['source_connectors'] = []
        SESSION['propagated_connectors'] = []
        SESSION['manual_connectors'] = []
        SESSION['lines_history'] = []

        offset_dist = float(new_params.get('spacing', 2.0))
        if offset_dist <= 0:
            offset_dist = 0.5

        cx_cy = SESSION.get('_orig_cx_cy', (0.0, 0.0))
        if src_path and os.path.exists(src_path):
            lines = build_interactive_lines_from_dxf(src_path, fps, cx_cy, raw, offset_dist)
        else:
            lines = []

        panel_geo = create_rounded_panel(compw, comph, new_params.get('corner_r', 4.0))
        fc = list(panel_geo.exterior.coords)
        for i in range(len(fc)-1):
            lines.append({"id":str(uuid.uuid4()),
                           "coords":[[round(fc[i][0],4),round(fc[i][1],4)],
                                     [round(fc[i+1][0],4),round(fc[i+1][1],4)]],
                           "type":"frame","kind":"line"})

        SESSION['base_lines'] = json.loads(json.dumps(lines))
        SESSION['interactive_lines'] = lines

        stats['elapsed'] = 0
        return {"ok":True, "stats":stats, "polys":polys_data,
                "lines":lines, "compressed_w":compw, "compressed_h":comph,
                "panel_w": new_params['panel_w'], "panel_h": new_params['panel_h']}

    except Exception as e:
        import traceback; traceback.print_exc()
        return JSONResponse({"ok":False,"error":str(e)}, status_code=500)

@app.get("/download_nest_dxf")
async def download_nest_dxf():
    doc = SESSION.get('doc_out')
    if not doc: return JSONResponse({"error":"no data"}, status_code=400)
    tmp = tempfile.NamedTemporaryFile(suffix='.dxf', delete=False)
    tmp.close()
    doc.saveas(tmp.name)
    with open(tmp.name,'rb') as f: data = f.read()
    os.unlink(tmp.name)
    return StreamingResponse(io.BytesIO(data), media_type="application/dxf",
        headers={"Content-Disposition":"attachment; filename=nested.dxf"})

@app.get("/download_bridge_dxf")
async def download_bridge_dxf():
    compw = SESSION.get('compressed_w', 162.5)
    comph = SESSION.get('compressed_h', 190.5)
    fps   = SESSION.get('final_polys_with_info', [])
    R_corner, r_fix = 4.0, 2.0

    doc = ezdxf.new('R2010'); doc.header['$INSUNITS'] = 4
    msp = doc.modelspace()
    for lname, col in [('PARTS',7),('OFFSETS',4),('BRIDGES',2),('FRAME',7)]:
        doc.layers.new(lname, dxfattribs={'color': col})

    for poly, ang, _, _ in fps:
        ec = list(poly.exterior.coords)
        msp.add_lwpolyline(ec, dxfattribs={'layer': 'PARTS', 'closed': True})
        for hole in poly.interiors:
            hc = list(hole.coords)
            msp.add_lwpolyline(hc, dxfattribs={'layer': 'PARTS', 'closed': True})

    for ld in SESSION.get('interactive_lines', []):
        if ld.get('type') == 'offset':
            coords = ld.get('coords', [])
            if len(coords) == 2:
                msp.add_line(coords[0], coords[1], dxfattribs={'layer': 'OFFSETS'})

    for b in SESSION.get('manual_connectors', []):
        if b.get('type') == 'precise_sandglass_arc':
            t = b['ang']
            msp.add_arc((b['lx'], b['ly']), radius=b['r'],
                        start_angle=t-90, end_angle=t+90,
                        dxfattribs={'layer': 'BRIDGES'})
            msp.add_arc((b['rx'], b['ry']), radius=b['r'],
                        start_angle=t+90, end_angle=t+270,
                        dxfattribs={'layer': 'BRIDGES'})

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
    tmp.close()
    doc.saveas(tmp.name)
    with open(tmp.name, 'rb') as f: data = f.read()
    os.unlink(tmp.name)
    return StreamingResponse(io.BytesIO(data), media_type="application/dxf",
        headers={"Content-Disposition": "attachment; filename=production.dxf"})


if __name__ == "__main__":
    import uvicorn
    import webbrowser
    import threading

    port = int(os.environ.get("PORT", 8000))

    def open_browser():
        import time
        time.sleep(1.5)
        webbrowser.open(f"http://localhost:{port}")

    browser_thread = threading.Thread(target=open_browser, daemon=True)
    browser_thread.start()

    print(f"\n{'='*50}")
    print(f"  PCB Nesting Tool")
    print(f"  Running at: http://localhost:{port}")
    print(f"  Press Ctrl+C to quit")
    print(f"{'='*50}\n")

    uvicorn.run(app, host="0.0.0.0", port=port, log_level="warning")
