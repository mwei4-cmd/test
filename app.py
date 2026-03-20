import streamlit as st
import io
import time
import tempfile
import os
import json
import math
import uuid
import numpy as np
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt

import ezdxf
from ezdxf.path import make_path
from shapely.geometry import Polygon, box, Point, LineString
from shapely.affinity import rotate, translate, scale
from shapely.ops import unary_union
try:
    from shapely.geometry import JOIN_STYLE
    JOIN_STYLE_ROUND = JOIN_STYLE.round
except ImportError:
    JOIN_STYLE_ROUND = 1

# ─────────────────────────────────────────────
# Page config
# ─────────────────────────────────────────────
st.set_page_config(
    page_title="PCB Nesting Tool",
    page_icon="⬡",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ─────────────────────────────────────────────
# Styling
# ─────────────────────────────────────────────
st.markdown("""
<style>
@import url('https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;600&family=Inter:wght@300;400;600&display=swap');

html, body, [class*="css"] { font-family: 'Inter', sans-serif; }

.stApp { background: #0f1117; color: #e2e8f0; }

section[data-testid="stSidebar"] {
    background: #141720;
    border-right: 1px solid #2a2f45;
}
section[data-testid="stSidebar"] * { color: #c8cfe8 !important; }

.metric-card {
    background: #1a1f2e;
    border: 1px solid #2a2f45;
    border-radius: 10px;
    padding: 14px 18px;
    margin-bottom: 10px;
}
.metric-label {
    font-size: 11px;
    color: #6b7599;
    text-transform: uppercase;
    letter-spacing: 0.08em;
    margin-bottom: 4px;
}
.metric-value {
    font-family: 'JetBrains Mono', monospace;
    font-size: 18px;
    font-weight: 600;
    color: #7aa2f7;
}
.metric-value.green { color: #9ece6a; }
.metric-value.amber { color: #e0af68; }
.metric-value.red   { color: #f7768e; }

.section-header {
    font-size: 12px;
    font-weight: 600;
    color: #565f89;
    text-transform: uppercase;
    letter-spacing: 0.1em;
    padding: 6px 0 4px;
    border-bottom: 1px solid #2a2f45;
    margin-bottom: 12px;
}

.suggest-box {
    background: #1e2030;
    border: 1px solid #3d59a1;
    border-radius: 8px;
    padding: 12px 16px;
    margin-top: 8px;
}
.suggest-row {
    font-family: 'JetBrains Mono', monospace;
    font-size: 13px;
    color: #7dcfff;
    margin: 3px 0;
}

div[data-testid="stFileUploader"] {
    background: #1a1f2e;
    border: 1px dashed #3d4466;
    border-radius: 10px;
    padding: 8px;
}

.stButton > button {
    background: #3d59a1;
    color: #c0caf5;
    border: none;
    border-radius: 8px;
    font-family: 'Inter', sans-serif;
    font-weight: 600;
    padding: 10px 24px;
    width: 100%;
    transition: background 0.2s;
}
.stButton > button:hover { background: #4a6bc7; }

.stDownloadButton > button {
    background: #1a6b3a !important;
    color: #9ece6a !important;
    border: 1px solid #2d8a52 !important;
    border-radius: 8px;
    width: 100%;
}

hr { border-color: #2a2f45; }

.stSelectbox label, .stSlider label, .stNumberInput label,
.stRadio label { color: #a9b1d6 !important; font-size: 13px; }

canvas#bridge_canvas {
    display: block;
    width: 100%;
    background: #000;
    border-radius: 8px;
    cursor: crosshair;
}
</style>
""", unsafe_allow_html=True)

# ─────────────────────────────────────────────
# Core geometry functions
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
                r = entity.dxf.radius
                poly = Point(c.x, c.y).buffer(r, quad_segs=64)
            else:
                path = make_path(entity)
                pts = list(path.flattening(distance=0.01))
                if len(pts) < 3:
                    continue
                poly = Polygon([(pt.x, pt.y) for pt in pts])
                if not poly.is_valid:
                    poly = poly.buffer(0)
            if poly.is_valid and not poly.is_empty and poly.area > 0.01:
                all_polys.append(poly)
        except:
            continue

    if not all_polys:
        raise ValueError("在 DXF 中找不到有效的封閉輪廓。")

    all_polys.sort(key=lambda p: p.area, reverse=True)
    shell_poly = all_polys[0]
    orig_centroid = shell_poly.centroid
    cx, cy = orig_centroid.x, orig_centroid.y

    holes = []
    for p in all_polys[1:]:
        if shell_poly.buffer(0.01).contains(p):
            holes.append(p.exterior.coords)

    raw_poly_at_origin = translate(Polygon(shell_poly.exterior.coords, holes), -cx, -cy)

    block_name = "PART_GEO_BLOCK"
    if target_doc:
        new_block = target_doc.blocks.new(name=block_name)
        for ent in msp.query('*'):
            try:
                new_ent = ent.copy()
                new_ent.translate(-cx, -cy, 0)
                new_block.add_entity(new_ent)
            except:
                continue

    return raw_poly_at_origin, block_name, (cx, cy), len(holes)


def create_rounded_panel(w, h, r):
    inner_box = box(r, r, w - r, h - r)
    return inner_box.buffer(r, quad_segs=32, join_style=JOIN_STYLE_ROUND)


def refine_position(poly_a, poly_b, init_dx, init_dy, target_dist):
    best_dx, best_dy = init_dx, init_dy
    best_b = translate(poly_b, best_dx, best_dy)
    cb = unary_union([poly_a, best_b]).bounds
    best_area = (cb[2] - cb[0]) * (cb[3] - cb[1])
    step = 1.0
    while step >= 0.001:
        improved = False
        for ddx, ddy in [(step, 0), (-step, 0), (0, step), (0, -step)]:
            trial_dx = best_dx + ddx
            trial_dy = best_dy + ddy
            trial_b = translate(poly_b, trial_dx, trial_dy)
            if poly_a.distance(trial_b) < target_dist - 0.001:
                continue
            cb_t = unary_union([poly_a, trial_b]).bounds
            trial_area = (cb_t[2] - cb_t[0]) * (cb_t[3] - cb_t[1])
            if trial_area < best_area - 1e-6:
                best_area = trial_area
                best_dx, best_dy = trial_dx, trial_dy
                best_b = trial_b
                improved = True
                break
        if not improved:
            step *= 0.5
    return best_dx, best_dy, best_b, best_area


def run_nesting(raw_poly, container_w, container_h, 左邊界, 右邊界, 上邊界, 下邊界,
                part_spacing, 排版模式, progress_cb=None):
    eff_w = container_w - 左邊界 - 右邊界
    eff_h = container_h - 上邊界 - 下邊界
    safe_zone_limit = box(左邊界, 下邊界, container_w - 右邊界, container_h - 上邊界)

    best_layout = []
    best_step_w = best_step_h = 0.0
    best_count_col = best_count_row = 0
    candidates_top3 = []

    if "Nesting" in 排版模式:
        candidates = []
        target_sp = float(part_spacing)
        rotated_data = {ang: rotate(raw_poly, ang, origin=(0, 0)) for ang in [0, 90, 180, 270]}
        ba_ref = rotated_data[0]
        total_iters = len(rotated_data) * 36
        done = 0

        for ang_b, bb_ref in rotated_data.items():
            bw = bb_ref.bounds[2] - bb_ref.bounds[0]
            bh = bb_ref.bounds[3] - bb_ref.bounds[1]
            search_radius = (bw**2 + bh**2)**0.5 * 2

            for angle_deg in range(0, 360, 10):
                done += 1
                if progress_cb:
                    progress_cb(done / total_iters)

                angle_rad = np.radians(angle_deg)
                dir_x, dir_y = np.cos(angle_rad), np.sin(angle_rad)
                lo, hi = 0.0, search_radius
                test_far = translate(bb_ref, dir_x * hi, dir_y * hi)
                if ba_ref.distance(test_far) < target_sp:
                    hi *= 2
                for _ in range(50):
                    mid = (lo + hi) / 2
                    if ba_ref.distance(translate(bb_ref, dir_x * mid, dir_y * mid)) < target_sp:
                        lo = mid
                    else:
                        hi = mid

                init_dx, init_dy = dir_x * hi, dir_y * hi
                test_b_init = translate(bb_ref, init_dx, init_dy)
                if ba_ref.distance(test_b_init) < target_sp - 0.05:
                    continue

                curr_dx, curr_dy, test_b_final, unit_area = refine_position(
                    ba_ref, bb_ref, init_dx, init_dy, target_sp)
                if ba_ref.distance(test_b_final) < target_sp - 0.05:
                    continue

                cb = unary_union([ba_ref, test_b_final]).bounds
                unit_w, unit_h = cb[2] - cb[0], cb[3] - cb[1]
                step_w, step_h = unit_w + target_sp, unit_h + target_sp
                count_col = int(eff_w // step_w)
                count_row = int(eff_h // step_h)
                total_count = count_col * count_row * 2
                if total_count <= 0:
                    continue

                offset_x, offset_y = cb[0], cb[1]
                temp_coords = []
                for r in range(count_row):
                    for c in range(count_col):
                        temp_coords.append((
                            左邊界 + c * step_w - offset_x,
                            下邊界 + r * step_h - offset_y
                        ))

                candidates.append((
                    total_count, -(unit_w * unit_h), temp_coords,
                    (ba_ref, test_b_final, curr_dx, curr_dy, ang_b),
                    unit_w * unit_h, count_col, count_row, step_w, step_h
                ))

        candidates = sorted(candidates, key=lambda x: (x[4], -x[0]))[:3]
        candidates_top3 = candidates

        if candidates:
            best_c = candidates[0]
            ba_ref_final, test_b_final, dx_final, dy_final, ang_b_final = best_c[3]
            best_count_col, best_count_row = best_c[5], best_c[6]
            best_step_w, best_step_h = best_c[7], best_c[8]

            for (tx, ty) in best_c[2]:
                fa = translate(ba_ref_final, tx, ty)
                fb = translate(test_b_final, tx, ty)
                best_layout.append((fa, 0, False, (tx, ty)))
                best_layout.append((fb, ang_b_final, False, (tx + dx_final, ty + dy_final)))

    else:
        max_found = 0
        target_offset = float(part_spacing)
        for ang in [0, 90, 180, 270]:
            t_b = rotate(raw_poly, ang, origin=(0, 0))
            s_minx, s_miny, s_maxx, s_maxy = t_b.bounds
            sw, sh = s_maxx - s_minx, s_maxy - s_miny
            step_x, step_y = sw + target_offset, sh + target_offset
            temp_layout = []
            curr_y = 下邊界
            while curr_y + sh <= container_h - 上邊界 + 0.001:
                curr_x = 左邊界
                while curr_x + sw <= container_w - 右邊界 + 0.001:
                    test_part = translate(t_b, curr_x - s_minx, curr_y - s_miny)
                    if safe_zone_limit.contains(test_part):
                        temp_layout.append((test_part, ang, False,
                                            (test_part.centroid.x, test_part.centroid.y)))
                    curr_x += step_x
                curr_y += step_y
            if len(temp_layout) > max_found:
                max_found = len(temp_layout)
                best_layout = temp_layout
                best_step_w, best_step_h = step_x, step_y
                best_count_col = int(eff_w // step_x)
                best_count_row = int(eff_h // step_y)

    return best_layout, best_step_w, best_step_h, best_count_col, best_count_row, candidates_top3


def build_output(best_layout, best_step_w, best_step_h, best_count_col, best_count_row,
                 container_w, container_h, 左邊界, 右邊界, 上邊界, 下邊界,
                 圓角半徑_R, geo_block_name, doc_out, raw_poly, part_spacing):
    msp_out = doc_out.modelspace()
    p_w = raw_poly.bounds[2] - raw_poly.bounds[0]
    p_h = raw_poly.bounds[3] - raw_poly.bounds[1]

    all_polys_for_union = [b for b, a, f, c in best_layout]
    total_union = unary_union(all_polys_for_union)
    final_ox = (container_w / 2) - total_union.centroid.x
    final_oy = (container_h / 2) - total_union.centroid.y

    final_polys_with_info = [
        (translate(b, final_ox, final_oy), a, f, (c[0] + final_ox, c[1] + final_oy))
        for b, a, f, c in best_layout
    ]
    final_polys = [p for p, a, f, c in final_polys_with_info]

    final_union = unary_union(final_polys)
    fu_minx, fu_miny, fu_maxx, fu_maxy = final_union.bounds
    compressed_w = (fu_maxx - fu_minx) + 左邊界 + 右邊界
    compressed_h = (fu_maxy - fu_miny) + 上邊界 + 下邊界
    shrink_w = container_w - compressed_w
    shrink_h = container_h - compressed_h

    compress_ox = 左邊界 - fu_minx
    compress_oy = 下邊界 - fu_miny
    final_polys_with_info = [
        (translate(p, compress_ox, compress_oy), a, f,
         (c[0] + compress_ox, c[1] + compress_oy))
        for p, a, f, c in final_polys_with_info
    ]
    final_polys = [p for p, a, f, c in final_polys_with_info]

    # Plot
    fig, ax = plt.subplots(figsize=(8, 10), facecolor='#1a1b26')
    ax.set_facecolor('#1a1b26')
    panel_geo = create_rounded_panel(compressed_w, compressed_h, 圓角半徑_R)
    ax.plot(*panel_geo.exterior.xy, color='#7aa2f7', linewidth=1.5)

    for poly, ang, is_bf, (actual_x, actual_y) in final_polys_with_info:
        ax.plot(*poly.exterior.xy, color='white', linewidth=0.9)
        for hole in poly.interiors:
            hx, hy = zip(*hole.coords)
            ax.plot(hx, hy, color='#ff9e64', linewidth=0.7, alpha=0.9)
        msp_out.add_blockref(geo_block_name, (actual_x, actual_y), dxfattribs={
            'rotation': ang, 'xscale': 1.0, 'yscale': 1.0, 'layer': 'PART_BODY', 'color': 7
        })

    msp_out.add_lwpolyline(list(panel_geo.exterior.coords),
                           dxfattribs={'layer': 'PANEL_FRAME', 'color': 7, 'closed': True})
    corner_centers = [(4, 4), (compressed_w - 4, 4),
                      (compressed_w - 4, compressed_h - 4), (4, compressed_h - 4)]
    for cx, cy in corner_centers:
        msp_out.add_circle((cx, cy), radius=1.53, dxfattribs={'layer': 'PANEL_HOLES', 'color': 7})
        ax.plot(*Point(cx, cy).buffer(1.53).exterior.xy, color='#7aa2f7', linewidth=0.8)

    ax.set_aspect('equal')
    ax.axis('off')
    fig.tight_layout(pad=0.5)

    # Stats
    def get_groups(coords, tolerance=10.0):
        if not coords: return []
        sorted_c = sorted(coords)
        groups = [[sorted_c[0]]]
        for c in sorted_c[1:]:
            if c - groups[-1][-1] < tolerance: groups[-1].append(c)
            else: groups.append([c])
        return groups

    def calc_min_gap(polys, axis='x'):
        if not polys or len(polys) < 2: return 0.0
        gaps = []
        if axis == 'x':
            sorted_p = sorted(polys, key=lambda p: p.bounds[0])
            for i in range(len(sorted_p) - 1):
                for j in range(i + 1, len(sorted_p)):
                    gap = sorted_p[j].bounds[0] - sorted_p[i].bounds[2]
                    if gap > -0.1: gaps.append(gap); break
        else:
            sorted_p = sorted(polys, key=lambda p: p.bounds[1])
            for i in range(len(sorted_p) - 1):
                for j in range(i + 1, len(sorted_p)):
                    gap = sorted_p[j].bounds[1] - sorted_p[i].bounds[3]
                    if gap > -0.1: gaps.append(gap); break
        return min(gaps) if gaps else 0.0

    x_groups = get_groups([p.centroid.x for p in final_polys])
    y_groups = get_groups([p.centroid.y for p in final_polys])
    gap_x = calc_min_gap(final_polys, 'x')
    gap_y = calc_min_gap(final_polys, 'y')

    fu2 = unary_union(final_polys).bounds
    stats = {
        'pcs': len(final_polys),
        'p_w': p_w, 'p_h': p_h,
        'utilization': sum(p.area for p in final_polys) / (compressed_w * compressed_h) * 100,
        'cols': len(x_groups), 'rows': len(y_groups),
        'gap_x': gap_x, 'gap_y': gap_y,
        'band_l': fu2[0], 'band_r': compressed_w - fu2[2],
        'band_b': fu2[1], 'band_t': compressed_h - fu2[3],
        'container_w': container_w, 'container_h': container_h,
        'compressed_w': compressed_w, 'compressed_h': compressed_h,
        'shrink_w': shrink_w, 'shrink_h': shrink_h,
    }

    # Suggest
    if best_step_w > 0 and best_step_h > 0:
        target_sp = float(part_spacing)
        suggest_eff_w = (best_count_col + 1) * best_step_w + best_step_w * 0.01
        suggest_eff_h = (best_count_row + 1) * best_step_h + best_step_h * 0.01
        suggest_w = suggest_eff_w + 左邊界 + 右邊界
        suggest_h = suggest_eff_h + 上邊界 + 下邊界
        stats['suggest_w'] = suggest_w
        stats['suggest_h'] = suggest_h
        stats['extra_w'] = suggest_w - compressed_w
        stats['extra_h'] = suggest_h - compressed_h
        stats['suggest_cols'] = best_count_col + 1
        stats['suggest_rows'] = best_count_row + 1
        stats['cur_cols'] = best_count_col
        stats['cur_rows'] = best_count_row

    # Save DXF
    tmp = tempfile.NamedTemporaryFile(suffix='.dxf', delete=False)
    doc_out.saveas(tmp.name)
    with open(tmp.name, 'rb') as f:
        dxf_bytes = f.read()
    os.unlink(tmp.name)

    return fig, stats, final_polys_with_info, dxf_bytes


# ─────────────────────────────────────────────
# Session state init
# ─────────────────────────────────────────────
for key in ['result_fig', 'result_stats', 'result_polys', 'result_dxf',
            'interactive_lines', 'manual_connectors', 'bridge_dxf',
            'candidates_top3', 'raw_poly_cache']:
    if key not in st.session_state:
        st.session_state[key] = None
if 'manual_connectors' not in st.session_state or st.session_state.manual_connectors is None:
    st.session_state.manual_connectors = []
if 'interactive_lines' not in st.session_state or st.session_state.interactive_lines is None:
    st.session_state.interactive_lines = []

# ─────────────────────────────────────────────
# Sidebar — settings
# ─────────────────────────────────────────────
with st.sidebar:
    st.markdown("## ⬡ PCB Nesting")
    st.markdown('<div class="section-header">排版模式</div>', unsafe_allow_html=True)
    排版模式 = st.radio("", ["Nesting (嵌合模式)", "Matrix (矩形模式)"],
                       label_visibility="collapsed")

    st.markdown('<div class="section-header">面板規格 (mm)</div>', unsafe_allow_html=True)
    面板規格 = st.selectbox("", ["160×200", "200×250", "自定義"], label_visibility="collapsed")
    if 面板規格 == "160×200":
        面板寬度_W, 面板高度_H = 160.0, 200.0
        st.info(f"📐 {面板寬度_W} × {面板高度_H} mm")
    elif 面板規格 == "200×250":
        面板寬度_W, 面板高度_H = 200.0, 250.0
        st.info(f"📐 {面板寬度_W} × {面板高度_H} mm")
    else:
        col1, col2 = st.columns(2)
        面板寬度_W = col1.number_input("寬 W", value=162.5, step=0.1, format="%.2f")
        面板高度_H = col2.number_input("高 H", value=190.5, step=0.1, format="%.2f")

    圓角半徑_R = st.number_input("圓角半徑 R (mm)", value=4.0, step=0.5, format="%.1f")

    st.markdown('<div class="section-header">安全邊界 (mm)</div>', unsafe_allow_html=True)
    c1, c2 = st.columns(2)
    左邊界 = c1.number_input("左", value=0.0, step=0.5, format="%.1f")
    右邊界 = c2.number_input("右", value=0.0, step=0.5, format="%.1f")
    c3, c4 = st.columns(2)
    上邊界 = c3.number_input("上", value=2.0, step=0.5, format="%.1f")
    下邊界 = c4.number_input("下", value=2.0, step=0.5, format="%.1f")

    st.markdown('<div class="section-header">零件間距</div>', unsafe_allow_html=True)
    零件間距 = st.slider("", 0.0, 20.0, 2.0, 0.5, label_visibility="collapsed")
    st.caption(f"間距：{零件間距} mm")

    st.markdown("---")
    st.markdown('<div class="section-header">上傳 DXF 零件</div>', unsafe_allow_html=True)
    uploaded_file = st.file_uploader("", type=["dxf"], label_visibility="collapsed")

    run_btn = st.button("🚀 開始排版", use_container_width=True)

# ─────────────────────────────────────────────
# Main area — tabs
# ─────────────────────────────────────────────
tab1, tab2, tab3 = st.tabs(["📐 排版結果", "🎨 橋接工具", "📄 DXF 輸出"])

# ══════════════════════════════════════════════
# TAB 1 — Nesting
# ══════════════════════════════════════════════
with tab1:
    if run_btn:
        if uploaded_file is None:
            st.error("請先上傳 DXF 檔案")
        else:
            with st.spinner("讀取零件中..."):
                tmp_dxf = tempfile.NamedTemporaryFile(suffix='.dxf', delete=False)
                tmp_dxf.write(uploaded_file.read())
                tmp_dxf.flush()

                doc_out = ezdxf.new('R2010')
                try:
                    raw_poly, geo_block_name, _, hole_count = get_full_polygon_with_holes(
                        tmp_dxf.name, doc_out)
                    st.session_state.raw_poly_cache = (raw_poly, geo_block_name, doc_out)
                except Exception as e:
                    st.error(f"讀取失敗：{e}")
                    os.unlink(tmp_dxf.name)
                    st.stop()
                os.unlink(tmp_dxf.name)

            st.success(f"✅ 偵測到外框 1 個，孔洞 {hole_count} 個")

            progress_bar = st.progress(0, text="計算嵌合方案中...")

            def update_progress(v):
                progress_bar.progress(min(v, 1.0), text=f"計算中... {int(v*100)}%")

            start_time = time.time()
            best_layout, bsw, bsh, bcc, bcr, candidates_top3 = run_nesting(
                raw_poly, 面板寬度_W, 面板高度_H,
                左邊界, 右邊界, 上邊界, 下邊界,
                零件間距, 排版模式,
                progress_cb=update_progress if "Nesting" in 排版模式 else None
            )
            elapsed = time.time() - start_time
            progress_bar.empty()

            if not best_layout:
                st.error("❌ 無法找到合法排版，請確認零件尺寸與面板規格設定。")
            else:
                doc_out_fresh = ezdxf.new('R2010')
                _, geo_block_name_fresh, _, _ = get_full_polygon_with_holes(
                    None if True else None, doc_out_fresh
                ) if False else (None, geo_block_name, None, None)

                # rebuild block in fresh doc
                raw_poly2, geo_block_name2, _, _ = (raw_poly, geo_block_name, None, None)

                fig, stats, polys_info, dxf_bytes = build_output(
                    best_layout, bsw, bsh, bcc, bcr,
                    面板寬度_W, 面板高度_H,
                    左邊界, 右邊界, 上邊界, 下邊界,
                    圓角半徑_R, geo_block_name, doc_out, raw_poly, 零件間距
                )
                stats['elapsed'] = elapsed
                st.session_state.result_fig = fig
                st.session_state.result_stats = stats
                st.session_state.result_polys = polys_info
                st.session_state.result_dxf = dxf_bytes
                st.session_state.candidates_top3 = candidates_top3

    # Display results
    if st.session_state.result_stats is not None:
        stats = st.session_state.result_stats

        # Top row metrics
        c1, c2, c3, c4 = st.columns(4)
        with c1:
            st.markdown(f"""<div class="metric-card">
                <div class="metric-label">總數量</div>
                <div class="metric-value">{stats['pcs']} PCS</div>
            </div>""", unsafe_allow_html=True)
        with c2:
            st.markdown(f"""<div class="metric-card">
                <div class="metric-label">空間利用率</div>
                <div class="metric-value green">{stats['utilization']:.1f}%</div>
            </div>""", unsafe_allow_html=True)
        with c3:
            st.markdown(f"""<div class="metric-card">
                <div class="metric-label">壓縮後板子</div>
                <div class="metric-value amber">{stats['compressed_w']:.1f} × {stats['compressed_h']:.1f}</div>
            </div>""", unsafe_allow_html=True)
        with c4:
            st.markdown(f"""<div class="metric-card">
                <div class="metric-label">耗時</div>
                <div class="metric-value">{stats.get('elapsed', 0):.2f}s</div>
            </div>""", unsafe_allow_html=True)

        col_plot, col_info = st.columns([2, 1])

        with col_plot:
            if st.session_state.candidates_top3:
                tops = st.session_state.candidates_top3
                fig3, axes3 = plt.subplots(1, len(tops), figsize=(12, 3.5),
                                           facecolor='#1a1b26')
                for i, cand in enumerate(tops):
                    ax = axes3[i] if len(tops) > 1 else axes3
                    count, _, _, pair_info, area, cc, cr, sw, sh = cand
                    p1, p2 = pair_info[0], pair_info[1]
                    ax.fill(*p1.exterior.xy, color='#7aa2f7', alpha=0.8)
                    ax.fill(*p2.exterior.xy, color='#e0af68', alpha=0.8)
                    ax.set_facecolor('#1a1b26')
                    ax.set_title(f"#{i+1}  {count} PCS\n{area:.0f} mm²",
                                 color='#c0caf5', fontsize=9)
                    ax.set_aspect('equal'); ax.axis('off')
                fig3.tight_layout(pad=0.3)
                st.pyplot(fig3, use_container_width=True)
                plt.close(fig3)

            st.pyplot(st.session_state.result_fig, use_container_width=True)

        with col_info:
            st.markdown('<div class="section-header">零件資訊</div>', unsafe_allow_html=True)
            st.markdown(f"""<div class="metric-card">
                <div class="metric-label">零件尺寸</div>
                <div class="metric-value" style="font-size:14px">{stats['p_w']:.2f} × {stats['p_h']:.2f} mm</div>
            </div>""", unsafe_allow_html=True)
            st.markdown(f"""<div class="metric-card">
                <div class="metric-label">列 × 行</div>
                <div class="metric-value" style="font-size:14px">{stats['cols']} × {stats['rows']}</div>
            </div>""", unsafe_allow_html=True)
            st.markdown(f"""<div class="metric-card">
                <div class="metric-label">最小間距 X / Y</div>
                <div class="metric-value" style="font-size:13px">{stats['gap_x']:.3f} / {stats['gap_y']:.3f} mm</div>
            </div>""", unsafe_allow_html=True)

            st.markdown('<div class="section-header">邊緣餘裕</div>', unsafe_allow_html=True)
            st.markdown(f"""<div class="metric-card">
                <div class="metric-label">左 / 右</div>
                <div class="metric-value" style="font-size:13px">{stats['band_l']:.2f} / {stats['band_r']:.2f} mm</div>
            </div>""", unsafe_allow_html=True)
            st.markdown(f"""<div class="metric-card">
                <div class="metric-label">上 / 下</div>
                <div class="metric-value" style="font-size:13px">{stats['band_t']:.2f} / {stats['band_b']:.2f} mm</div>
            </div>""", unsafe_allow_html=True)

            st.markdown('<div class="section-header">🗜️ 自動壓縮</div>', unsafe_allow_html=True)
            st.markdown(f"""<div class="metric-card">
                <div class="metric-label">原始 → 壓縮</div>
                <div class="metric-value" style="font-size:12px; color:#f7768e">
                    {stats['container_w']:.1f}×{stats['container_h']:.1f}<br>
                    → {stats['compressed_w']:.1f}×{stats['compressed_h']:.1f}
                </div>
                <div style="font-size:11px; color:#6b7599; margin-top:4px">
                    縮 {stats['shrink_w']:.2f} × {stats['shrink_h']:.2f} mm
                </div>
            </div>""", unsafe_allow_html=True)

            if 'suggest_w' in stats:
                st.markdown('<div class="section-header">📐 建議擴充</div>', unsafe_allow_html=True)
                st.markdown(f"""<div class="suggest-box">
                    <div class="suggest-row">目前 {stats['cur_cols']} 列 × {stats['cur_rows']} 行</div>
                    <div class="suggest-row">+1列 → W ≥ {stats['suggest_w']:.2f} mm</div>
                    <div class="suggest-row">+1行 → H ≥ {stats['suggest_h']:.2f} mm</div>
                    <div style="font-size:11px; color:#565f89; margin-top:6px">
                        增加 +{stats['extra_w']:.2f} / +{stats['extra_h']:.2f} mm
                    </div>
                </div>""", unsafe_allow_html=True)

        if st.session_state.result_dxf:
            st.download_button(
                "⬇️ 下載排版 DXF",
                data=st.session_state.result_dxf,
                file_name=f"nested_{stats['pcs']}pcs.dxf",
                mime="application/dxf",
                use_container_width=True,
            )
    else:
        st.markdown("""
        <div style="text-align:center; padding: 80px 20px; color: #565f89;">
            <div style="font-size: 48px; margin-bottom: 16px;">⬡</div>
            <div style="font-size: 18px; font-weight: 600; color: #7aa2f7; margin-bottom: 8px;">PCB Nesting Tool</div>
            <div style="font-size: 14px;">上傳 DXF 零件檔案，設定參數後點擊「開始排版」</div>
        </div>
        """, unsafe_allow_html=True)

# ══════════════════════════════════════════════
# TAB 2 — Bridge tool
# ══════════════════════════════════════════════
with tab2:
    if st.session_state.result_polys is None:
        st.info("請先完成排版（Tab 1）才能使用橋接工具。")
    else:
        polys_info = st.session_state.result_polys
        stats = st.session_state.result_stats

        # Build interactive_lines if not yet built or stale
        if not st.session_state.interactive_lines:
            lines = []
            for p, ang, is_bf, center in polys_info:
                coords = list(p.exterior.coords)
                for i in range(len(coords) - 1):
                    lines.append({"id": str(uuid.uuid4()),
                                  "coords": [[coords[i][0], coords[i][1]],
                                             [coords[i+1][0], coords[i+1][1]]],
                                  "type": "body"})
                for hole in p.interiors:
                    hc = list(hole.coords)
                    for i in range(len(hc) - 1):
                        lines.append({"id": str(uuid.uuid4()),
                                      "coords": [[hc[i][0], hc[i][1]],
                                                 [hc[i+1][0], hc[i+1][1]]],
                                      "type": "hole"})
                try:
                    op = p.buffer(2, join_style=2)
                    oc = list(op.exterior.coords)
                    for i in range(len(oc) - 1):
                        lines.append({"id": str(uuid.uuid4()),
                                      "coords": [[oc[i][0], oc[i][1]],
                                                 [oc[i+1][0], oc[i+1][1]]],
                                      "type": "offset"})
                    for hole in op.interiors:
                        hc = list(hole.coords)
                        for i in range(len(hc) - 1):
                            lines.append({"id": str(uuid.uuid4()),
                                          "coords": [[hc[i][0], hc[i][1]],
                                                     [hc[i+1][0], hc[i+1][1]]],
                                          "type": "offset"})
                except:
                    pass
            st.session_state.interactive_lines = lines
            st.session_state.manual_connectors = []

        cW = stats['compressed_w']
        cH = stats['compressed_h']
        all_lines_json = json.dumps([{"c": l["coords"], "t": l["type"]}
                                     for l in st.session_state.interactive_lines])
        connectors_json = json.dumps([{
            'lx': b['left_center'].x, 'ly': b['left_center'].y,
            'rx': b['right_center'].x, 'ry': b['right_center'].y,
            'r': b['radius'], 'ang': b['base_angle']
        } for b in st.session_state.manual_connectors])

        st.markdown(f"""
        <div style="margin-bottom:10px; color:#565f89; font-size:13px;">
            線段總數：{len(st.session_state.interactive_lines)} 條 ｜
            橋接數：{len(st.session_state.manual_connectors)} 個 ｜
            板子：{cW:.1f} × {cH:.1f} mm
        </div>
        """, unsafe_allow_html=True)

        col_canvas, col_ctrl = st.columns([4, 1])
        with col_ctrl:
            st.markdown("**操作說明**")
            st.markdown("""
            <div style="font-size:12px; color:#6b7599; line-height:1.8">
            🖱️ 左鍵：選線段<br>
            🖱️ 滾輪：縮放<br>
            🖱️ 中鍵拖曳：平移<br>
            </div>
            """, unsafe_allow_html=True)
            if st.button("↩️ 撤銷", use_container_width=True):
                if st.session_state.manual_connectors:
                    st.session_state.manual_connectors.pop()
                st.rerun()
            if st.button("🔄 全部復原", use_container_width=True):
                st.session_state.manual_connectors = []
                st.session_state.interactive_lines = []
                st.rerun()

        with col_canvas:
            bridge_html = f"""
<div style="background:#1a1b26; border-radius:10px; padding:8px;">
<canvas id="bridge_canvas" width="1600" height="900"
  style="width:100%; border-radius:8px; cursor:crosshair; background:#000;"></canvas>
<div id="bridge_status" style="padding:8px 12px; font-size:12px; color:#7dcfff; font-family:monospace;">
  提示：左鍵點選兩條 offset 線段以建立橋接
</div>
</div>
<script>
const allLines = {all_lines_json};
const bridgeList = {connectors_json};
const panelW = {cW}, panelH = {cH};
const canvas = document.getElementById('bridge_canvas');
const ctx = canvas.getContext('2d');
let scale=1, offsetX=0, offsetY=0;
let isDrag=false, lastX=0, lastY=0;
let selected=[], hoverIdx=-1, firstPt=null;

const TYPE_COLOR = {{
  body:  {{n:'#ffffff', g:'#ffffff'}},
  hole:  {{n:'#ff9e64', g:'#ff9e64'}},
  offset:{{n:'#2ac3de', g:'#2ac3de'}},
}};

function zoomReset(){{
  const s=Math.min(760/panelW, 440/panelH);
  scale=s; offsetX=(800-panelW*s)/2; offsetY=(450+panelH*s)/2; draw();
}}
function toX(x){{return offsetX+x*scale;}}
function toY(y){{return offsetY-y*scale;}}
function fromX(x){{return(x-offsetX)/scale;}}
function fromY(y){{return(offsetY-y)/scale;}}

function draw(){{
  ctx.setTransform(2,0,0,2,0,0);
  ctx.clearRect(0,0,800,450);
  const dw=Math.max(0.5,Math.min(1.4/Math.pow(scale/8,0.4),2.5));
  allLines.forEach((l,i)=>{{
    const s=TYPE_COLOR[l.t]||TYPE_COLOR.body;
    const isS=selected.includes(i), isH=(i===hoverIdx);
    ctx.beginPath();
    ctx.moveTo(toX(l.c[0][0]),toY(l.c[0][1]));
    ctx.lineTo(toX(l.c[1][0]),toY(l.c[1][1]));
    if(isS){{ctx.strokeStyle='#ff007c';ctx.lineWidth=dw*3;ctx.shadowBlur=0;}}
    else if(isH){{ctx.strokeStyle=s.n;ctx.lineWidth=dw*2.5;ctx.shadowBlur=8;ctx.shadowColor=s.g;}}
    else{{ctx.strokeStyle=s.n;ctx.lineWidth=(l.t==='offset')?dw*0.8:dw;ctx.shadowBlur=0;}}
    ctx.stroke();ctx.shadowBlur=0;
  }});
  bridgeList.forEach(b=>{{
    ctx.strokeStyle='#ffff00';ctx.lineWidth=dw*1.5;
    const rad=b.r*scale, ar=b.ang*Math.PI/180;
    ctx.beginPath();ctx.arc(toX(b.lx),toY(b.ly),rad,-ar-Math.PI/2,-ar+Math.PI/2);ctx.stroke();
    ctx.beginPath();ctx.arc(toX(b.rx),toY(b.ry),rad,-ar+Math.PI/2,-ar+1.5*Math.PI);ctx.stroke();
  }});
  ctx.strokeStyle='#2d3f76';ctx.lineWidth=1;ctx.setLineDash([8,4]);
  ctx.strokeRect(toX(0),toY(panelH),panelW*scale,panelH*scale);ctx.setLineDash([]);
  if(firstPt){{ctx.fillStyle='#ff007c';ctx.beginPath();ctx.arc(toX(firstPt.x),toY(firstPt.y),4,0,7);ctx.fill();}}
}}

canvas.addEventListener('contextmenu',e=>e.preventDefault());
canvas.onmousedown=e=>{{
  if(e.button===1){{isDrag=true;lastX=e.clientX;lastY=e.clientY;e.preventDefault();}}
  else if(e.button===0&&hoverIdx!==-1){{
    const r=canvas.getBoundingClientRect();
    const rx=fromX(e.clientX-r.left),ry=fromY(e.clientY-r.top);
    if(selected.length===0){{selected=[hoverIdx];firstPt={{x:rx,y:ry}};}}
    else{{
      const l1=allLines[selected[0]],l2=allLines[hoverIdx];
      if(l1&&l2){{
        document.getElementById('bridge_status').textContent='✅ 橋接已建立（請重新執行 Tab 3 輸出）';
      }}
      selected=[];firstPt=null;
    }}
    draw();
  }}
}};
window.onmouseup=()=>isDrag=false;
canvas.onmousemove=e=>{{
  const r=canvas.getBoundingClientRect();
  const rx=fromX(e.clientX-r.left),ry=fromY(e.clientY-r.top);
  if(isDrag){{offsetX+=e.clientX-lastX;offsetY+=e.clientY-lastY;lastX=e.clientX;lastY=e.clientY;draw();}}
  else{{
    let lh=hoverIdx;hoverIdx=-1;let md=16/scale;
    allLines.forEach((l,i)=>{{
      const dx=l.c[0][0]-l.c[1][0],dy=l.c[0][1]-l.c[1][1],d2=dx*dx+dy*dy;
      let t=d2===0?0:Math.max(0,Math.min(1,((rx-l.c[0][0])*(l.c[1][0]-l.c[0][0])+(ry-l.c[0][1])*(l.c[1][1]-l.c[0][1]))/d2));
      const d=Math.sqrt((rx-(l.c[0][0]+t*(l.c[1][0]-l.c[0][0])))**2+(ry-(l.c[0][1]+t*(l.c[1][1]-l.c[0][1])))**2);
      if(d<md){{md=d;hoverIdx=i;}}
    }});
    if(lh!==hoverIdx)draw();
  }}
}};
canvas.onwheel=e=>{{
  e.preventDefault();const r=canvas.getBoundingClientRect();
  const mx=e.clientX-r.left,my=e.clientY-r.top;
  const w=e.deltaY<0?1.15:0.85;
  const wx=fromX(mx),wy=fromY(my);
  scale*=w;offsetX=mx-wx*scale;offsetY=my+wy*scale;draw();
}};
zoomReset();
</script>
"""
            st.components.v1.html(bridge_html, height=560, scrolling=False)

# ══════════════════════════════════════════════
# TAB 3 — DXF output
# ══════════════════════════════════════════════
with tab3:
    if st.session_state.result_polys is None:
        st.info("請先完成排版（Tab 1）才能輸出 DXF。")
    else:
        st.markdown("### 輸出設定")
        include_offset = st.checkbox("包含 Offset 加工線", value=True)
        include_bridges = st.checkbox("包含橋接弧線", value=True)
        include_holes = st.checkbox("包含內孔", value=True)

        if st.button("🔧 產生最終 DXF", use_container_width=True):
            stats = st.session_state.result_stats
            polys_info = st.session_state.result_polys
            cW, cH = stats['compressed_w'], stats['compressed_h']
            R_corner, r_fix = 4.0, 2.0

            doc_final = ezdxf.new('R2010')
            doc_final.header['$INSUNITS'] = 4
            msp = doc_final.modelspace()
            doc_final.layers.new('PARTS',   dxfattribs={'color': 7})
            doc_final.layers.new('OFFSETS', dxfattribs={'color': 4})
            doc_final.layers.new('BRIDGES', dxfattribs={'color': 2})
            doc_final.layers.new('FRAME',   dxfattribs={'color': 7})

            # Lines from interactive
            if st.session_state.interactive_lines:
                for ld in st.session_state.interactive_lines:
                    if ld['type'] == 'body':
                        msp.add_line(ld['coords'][0], ld['coords'][1],
                                     dxfattribs={'layer': 'PARTS'})
                    elif ld['type'] == 'hole' and include_holes:
                        msp.add_line(ld['coords'][0], ld['coords'][1],
                                     dxfattribs={'layer': 'PARTS'})
                    elif ld['type'] == 'offset' and include_offset:
                        msp.add_line(ld['coords'][0], ld['coords'][1],
                                     dxfattribs={'layer': 'OFFSETS'})

            # Bridges
            if include_bridges and st.session_state.manual_connectors:
                for bridge in st.session_state.manual_connectors:
                    if bridge.get('type') == 'precise_sandglass_arc':
                        theta = bridge['base_angle']
                        msp.add_arc((bridge['left_center'].x, bridge['left_center'].y),
                                    radius=bridge['radius'],
                                    start_angle=theta - 90, end_angle=theta + 90,
                                    dxfattribs={'layer': 'BRIDGES'})
                        msp.add_arc((bridge['right_center'].x, bridge['right_center'].y),
                                    radius=bridge['radius'],
                                    start_angle=theta + 90, end_angle=theta + 270,
                                    dxfattribs={'layer': 'BRIDGES'})

            # Frame
            centers = [(R_corner, R_corner), (cW - R_corner, R_corner),
                       (cW - R_corner, cH - R_corner), (R_corner, cH - R_corner)]
            msp.add_line((R_corner, 0), (cW - R_corner, 0), dxfattribs={'layer': 'FRAME'})
            msp.add_line((cW, R_corner), (cW, cH - R_corner), dxfattribs={'layer': 'FRAME'})
            msp.add_line((cW - R_corner, cH), (R_corner, cH), dxfattribs={'layer': 'FRAME'})
            msp.add_line((0, cH - R_corner), (0, R_corner), dxfattribs={'layer': 'FRAME'})
            arc_angles = [(180, 270), (270, 360), (0, 90), (90, 180)]
            for i, center in enumerate(centers):
                msp.add_arc(center, radius=R_corner,
                            start_angle=arc_angles[i][0], end_angle=arc_angles[i][1],
                            dxfattribs={'layer': 'FRAME'})
                msp.add_circle(center, radius=r_fix, dxfattribs={'layer': 'FRAME'})
            # Extra hole
            msp.add_circle((centers[0][0] + 10.0, centers[0][1]),
                           radius=r_fix, dxfattribs={'layer': 'FRAME'})

            tmp2 = tempfile.NamedTemporaryFile(suffix='.dxf', delete=False)
            doc_final.saveas(tmp2.name)
            with open(tmp2.name, 'rb') as f:
                final_dxf_bytes = f.read()
            os.unlink(tmp2.name)
            st.session_state.bridge_dxf = final_dxf_bytes
            st.success("✅ DXF 產生完成！")

        if st.session_state.bridge_dxf:
            st.download_button(
                "⬇️ 下載最終生產 DXF",
                data=st.session_state.bridge_dxf,
                file_name=f"production_{int(time.time())}.dxf",
                mime="application/dxf",
                use_container_width=True,
            )

        # Summary
        if st.session_state.result_stats:
            s = st.session_state.result_stats
            st.markdown("---")
            st.markdown("### 板子規格摘要")
            col1, col2 = st.columns(2)
            with col1:
                st.markdown(f"""
                | 項目 | 數值 |
                |------|------|
                | 壓縮後寬度 | `{s['compressed_w']:.2f} mm` |
                | 壓縮後高度 | `{s['compressed_h']:.2f} mm` |
                | 縮減寬度 | `-{s['shrink_w']:.2f} mm` |
                | 縮減高度 | `-{s['shrink_h']:.2f} mm` |
                """)
            with col2:
                if 'suggest_w' in s:
                    st.markdown(f"""
                    | 建議擴充 | 尺寸 |
                    |---------|------|
                    | 多一列寬度 | `{s['suggest_w']:.2f} mm` |
                    | 多一行高度 | `{s['suggest_h']:.2f} mm` |
                    | 擴充後列數 | `{s['suggest_cols']} 列` |
                    | 擴充後行數 | `{s['suggest_rows']} 行` |
                    """)
