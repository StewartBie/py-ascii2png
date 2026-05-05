#!/usr/bin/env python3
"""
ASCII 字符画 → matplotlib 矢量图转换器

本脚本从 Unicode 框线字符画（┌─┐│└─┘▼↔等）中自动识别方框、箭头和文字，
再用 matplotlib 渲染为带中文的矢量 PNG。核心思路：

  - 字符画本身仍然是"单一信息源"，所见即所得
  - 解析器自动提取方框位置/尺寸/嵌套关系 + 箭头 + 文字
  - 渲染器用 matplotlib 画矩形、箭头和居中文字
  - 使用 Noto CJK 字体，中文不会变成方块

用法：
    source .venv-docs/bin/activate
    python report/tools/render_architecture_overview_flow.py

如果想渲染自己的字符画，只需修改本文件末尾的 DIAGRAM_TEXT 变量，
把新的字符画粘贴进去即可。
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass, field
from pathlib import Path

import subprocess

import matplotlib.pyplot as plt
from matplotlib.font_manager import FontProperties
from matplotlib.patches import Rectangle


# ═══════════════════════════════════════════════════════════════
#  数据结构
# ═══════════════════════════════════════════════════════════════

@dataclass
class Box:
    """从字符画解析出的一个方框。"""
    row1: int         # 网格坐标：上边界行
    col1: int         # 网格坐标：左边界列
    row2: int         # 网格坐标：下边界行
    col2: int         # 网格坐标：右边界列
    title: str = ""   # 框内第一段有意义文字（标题）
    children: list["Box"] = field(default_factory=list)


@dataclass
class Arrow:
    """从字符画解析出的一个箭头。"""
    row: int
    col: int
    dir: str = ""       # 'down' | 'hdouble'
    source: "Box | None" = None   # 源框（箭头起始的一端）
    target: "Box | None" = None   # 目标框（箭头指向的一端）


@dataclass
class ExternalText:
    """方框外部的文字（标注、标题等）。"""
    text: str
    row: int              # 所在行
    col1: int             # 起始列
    col2: int             # 结束列（不含）
    anchor: "Box | None" = None   # 最近邻的方框
    side: str = ""        # 'right' | 'left' | 'top' | 'bottom'


# ═══════════════════════════════════════════════════════════════
#  框线字符常量（Unicode 码点）
# ═══════════════════════════════════════════════════════════════

_TOPLEFT     = '\u250c'   # ┌
_TOPRIGHT    = '\u2510'   # ┐
_BOTLEFT     = '\u2514'   # └
_BOTRIGHT    = '\u2518'   # ┘
_HORIZ       = '\u2500'   # ─
_VERT        = '\u2502'   # │
_TEE_DOWN    = '\u252c'   # ┬  下形三通（竖线从横线下伸出）
_TEE_UP      = '\u2534'   # ┴  上形三通（竖线从横线上伸出）
_TEE_RIGHT   = '\u251c'   # ├  右形三通（横线从竖线右伸出）
_TEE_LEFT    = '\u2524'   # ┤  左形三通（横线从竖线左伸出）

_BOX_ENDS = {_TOPLEFT, _TOPRIGHT, _BOTLEFT, _BOTRIGHT}
_BOX_TEES = {_TEE_DOWN, _TEE_UP, _TEE_RIGHT, _TEE_LEFT}
_BOX_LINES = {_HORIZ, _VERT}

_ARROWS = {
    '\u25bc': 'down',   # ▼
    '\u25b2': 'up',     # ▲
    '\u2194': 'hdouble',# ↔
    '\u2193': 'down',   # ↓
    '\u2191': 'up',     # ↑
    '\u2190': 'left',   # ←
    '\u2192': 'right',  # →
}

_ALL_SPECIAL = _BOX_ENDS | _BOX_LINES | _BOX_TEES | set(_ARROWS.keys())

_CONNECTORS_V = {_VERT, _TEE_DOWN, _TEE_UP, _TEE_RIGHT, _TEE_LEFT}
_CONNECTORS_H = {_HORIZ, _TEE_DOWN, _TEE_UP, _TEE_RIGHT, _TEE_LEFT}


# ═══════════════════════════════════════════════════════════════
#  解析器
# ═══════════════════════════════════════════════════════════════

def parse_diagram(text: str):
    """解析字符画，返回 (顶层方框列表, 箭头列表, 网格宽, 网格高)。"""
    # ── 转成等宽网格 ──────────────────────────────────
    lines = text.rstrip().split('\n')
    h = len(lines)
    w = max(len(line) for line in lines)
    grid = [list(line.ljust(w)) for line in lines]

    # ── 第一遍：找所有方框 ────────────────────────────
    visited = set()
    boxes = []

    for y in range(h):
        for x in range(w):
            if (x, y) in visited or grid[y][x] != _TOPLEFT:
                continue

            # 找右上 ┐
            x2 = x
            while x2 < w and grid[y][x2] != _TOPRIGHT:
                x2 += 1
            if x2 >= w:
                continue

            # 找左下 └
            y2 = y
            while y2 < h and grid[y2][x] != _BOTLEFT:
                y2 += 1
            if y2 >= h:
                continue

            # 找右下 ┘
            x3 = x
            while x3 < w and grid[y2][x3] != _BOTRIGHT:
                x3 += 1
            if x3 >= w:
                continue

            # 标记已访问（只标记边框，不标记内部区域！
            # 否则内层方框的 ┌ 会被跳过，导致嵌套框丢失）
            for sx in range(x, x3 + 1):
                visited.add((sx, y))    # 上边框
                visited.add((sx, y2))   # 下边框
            for sy in range(y + 1, y2):
                visited.add((x, sy))    # 左边框
                visited.add((x3, sy))   # 右边框

            boxes.append(Box(row1=y, col1=x, row2=y2, col2=x3))

    # ── 建立父子层级 ──────────────────────────────────
    # 原则：先基于位置建层级，再基于层级提取文字。
    # 子框可能被多个更大的框包含，选"面积最小的那一个"作为直属父框。
    roots = []
    for b in boxes:
        parent = None
        best_area = 10**9
        for p in boxes:
            if p is b:
                continue
            if (p.col1 <= b.col1 and b.col2 <= p.col2 and
                p.row1 <= b.row1 and b.row2 <= p.row2 and
                (p.col1 < b.col1 or p.row1 < b.row1 or
                 p.col2 > b.col2 or p.row2 > b.row2)):
                area = (p.col2 - p.col1) * (p.row2 - p.row1)
                if area < best_area:
                    best_area = area
                    parent = p
        if parent is None:
            roots.append(b)
        else:
            parent.children.append(b)

    # ── 提取文字 ──────────────────────────────────────
    # 建好层级之后，可以准确知道每个方框的"直属子框"有哪些，
    # 提取文字时跳过这些子框的区域即可。
    def _collect_title(box: Box) -> str:
        """提取方框内文字：跳过直属子框的区域和框线字符。"""
        # 收集直属子框的单元格集合
        child_cells = set()
        for child in box.children:
            for sy in range(child.row1, child.row2 + 1):
                for sx in range(child.col1, child.col2 + 1):
                    child_cells.add((sx, sy))

        lines_txt = []
        for r in range(box.row1 + 1, box.row2):
            chars = []
            for c in range(box.col1 + 1, box.col2):
                if (c, r) in child_cells:
                    if chars and chars[-1] != ' ':
                        chars.append(' ')
                    continue
                ch = grid[r][c]
                if ch in _ALL_SPECIAL:
                    if chars and chars[-1] != ' ':
                        chars.append(' ')
                    continue
                chars.append(ch)
            text = ''.join(chars).strip()
            if text:
                lines_txt.append(text)
        return lines_txt[0] if lines_txt else ""

    # 递归为所有框提取文字
    def _assign_titles(boxes_list):
        for b in boxes_list:
            b.title = _collect_title(b)
            _assign_titles(b.children)

    for root in roots:
        _assign_titles([root])

    # ── 找箭头 ────────────────────────────────────────
    arrows = []
    for y in range(h):
        for x in range(w):
            ch = grid[y][x]
            if ch in _ARROWS:
                arrows.append(Arrow(row=y, col=x, dir=_ARROWS[ch]))

    # ── 连接箭头与方框 ────────────────────────────────
    # 根据箭头位置和方向，在所有方框中找到"源框"和"目标框"。
    # 不依赖于网格中的连字符（│─），只基于方框位置推算。
    def _find_containing_boxes(r: int, c: int, boxes_list) -> list[Box]:
        """找出给定网格坐标被哪些方框包含（从内到外）。"""
        result = []
        for b in boxes_list:
            if b.col1 <= c <= b.col2 and b.row1 <= r <= b.row2:
                result.append(b)
            result.extend(_find_containing_boxes(r, c, b.children))
        return result

    def _find_closest_box_above(r: int, c: int, boxes_list) -> Box | None:
        """在给定行 r 上方，找 col 区间覆盖 c 且底边 row2 最接近 r 的方框。"""
        best = None
        best_r2 = -1
        for b in boxes_list:
            if b.row2 < r and b.col1 <= c <= b.col2 and b.row2 > best_r2:
                best_r2 = b.row2
                best = b
        if best is None and boxes_list:
            for b in boxes_list:
                cand = _find_closest_box_above(r, c, b.children)
                if cand and cand.row2 > best_r2:
                    best_r2 = cand.row2
                    best = cand
        return best

    def _find_closest_box_below(r: int, c: int, boxes_list) -> Box | None:
        """在给定行 r 下方，找 col 区间覆盖 c 且顶边 row1 最接近 r 的方框。"""
        best = None
        best_r1 = 10**9
        for b in boxes_list:
            if b.row1 > r and b.col1 <= c <= b.col2 and b.row1 < best_r1:
                best_r1 = b.row1
                best = b
        if best is None and boxes_list:
            for b in boxes_list:
                cand = _find_closest_box_below(r, c, b.children)
                if cand and cand.row1 < best_r1:
                    best_r1 = cand.row1
                    best = cand
        return best

    def _find_closest_box_left(r: int, c: int, boxes_list) -> Box | None:
        """在给定列 c 左方，找 row 区间覆盖 r 且右边 col2 最接近 c 的方框。"""
        best = None
        best_c2 = -1
        for b in boxes_list:
            if b.col2 < c and b.row1 <= r <= b.row2 and b.col2 > best_c2:
                best_c2 = b.col2
                best = b
        if best is None and boxes_list:
            for b in boxes_list:
                cand = _find_closest_box_left(r, c, b.children)
                if cand and cand.col2 > best_c2:
                    best_c2 = cand.col2
                    best = cand
        return best

    def _find_closest_box_right(r: int, c: int, boxes_list) -> Box | None:
        """在给定列 c 右方，找 row 区间覆盖 r 且左边 col1 最接近 c 的方框。"""
        best = None
        best_c1 = 10**9
        for b in boxes_list:
            if b.col1 > c and b.row1 <= r <= b.row2 and b.col1 < best_c1:
                best_c1 = b.col1
                best = b
        if best is None and boxes_list:
            for b in boxes_list:
                cand = _find_closest_box_right(r, c, b.children)
                if cand and cand.col1 < best_c1:
                    best_c1 = cand.col1
                    best = cand
        return best

    for arr in arrows:
        if arr.dir == 'down':
            arr.source = _find_closest_box_above(arr.row, arr.col, roots)
            arr.target = _find_closest_box_below(arr.row, arr.col, roots)
        elif arr.dir == 'hdouble':
            arr.source = _find_closest_box_left(arr.row, arr.col, roots)
            arr.target = _find_closest_box_right(arr.row, arr.col, roots)

    # ── 提取框外文字 ────────────────────────────────
    # 建立一个"已被框覆盖"的单元格集合
    box_cells = set()
    def _mark_box(b):
        for sy in range(b.row1, b.row2 + 1):
            for sx in range(b.col1, b.col2 + 1):
                box_cells.add((sx, sy))
        for ch in b.children:
            _mark_box(ch)
    for r in roots:
        _mark_box(r)

    external_texts: list[ExternalText] = []
    for y in range(h):
        x = 0
        while x < w:
            # 跳过框内单元格
            if (x, y) in box_cells:
                x += 1
                continue
            ch = grid[y][x]
            # 跳过空格和特殊字符
            if ch in _ALL_SPECIAL | {' '}:
                x += 1
                continue
            # 找到连续文本
            x_start = x
            while x < w and (x, y) not in box_cells and grid[y][x] not in _ALL_SPECIAL:
                x += 1
            x_end = x
            text = ''.join(grid[y][xx] for xx in range(x_start, x_end)).strip()
            if text and len(text) >= 1:
                external_texts.append(ExternalText(
                    text=text, row=y, col1=x_start, col2=x_end,
                ))

    # ── 框外文字 → 绑定最近方框 ──────────────────────
    for et in external_texts:
        # 找最近方框
        best_box = None
        best_dist = 10**9
        best_side = ""
        def _search_boxes(boxes_list, depth=0):
            nonlocal best_box, best_dist, best_side
            for b in boxes_list:
                # 计算中心
                cx = (b.col1 + b.col2) / 2
                cy = (b.row1 + b.row2) / 2
                etx = (et.col1 + et.col2) / 2
                ety = et.row

                # 判断方位
                if ety < b.row1 and b.col1 <= etx <= b.col2:
                    # 正上方
                    dist = b.row1 - ety
                    if dist < best_dist:
                        best_dist = dist; best_box = b; best_side = "top"
                elif ety > b.row2 and b.col1 <= etx <= b.col2:
                    # 正下方
                    dist = ety - b.row2
                    if dist < best_dist:
                        best_dist = dist; best_box = b; best_side = "bottom"
                elif et.col2 <= b.col1 and b.row1 <= ety <= b.row2:
                    # 左侧（同行）
                    dist = b.col1 - et.col2
                    if dist < best_dist:
                        best_dist = dist; best_box = b; best_side = "left"
                elif et.col1 >= b.col2 and b.row1 <= ety <= b.row2:
                    # 右侧（同行）
                    dist = et.col1 - b.col2
                    if dist < best_dist:
                        best_dist = dist; best_box = b; best_side = "right"
                # 递归子框
                _search_boxes(b.children, depth + 1)
        _search_boxes(roots)
        et.anchor = best_box
        et.side = best_side

    return roots, arrows, external_texts, w, h


# ═══════════════════════════════════════════════════════════════
#  渲染器
# ═══════════════════════════════════════════════════════════════

def render_diagram(
    roots: list[Box],
    arrows: list[Arrow],
    external_texts: list[ExternalText],
    grid_w: int,
    grid_h: int,
    font_path: Path,
    font_size: int,
    dpi: int,
    out_path: Path,
) -> None:
    """将解析结果渲染为 PNG。"""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    font_props = FontProperties(fname=str(font_path))

    # 坐标映射：网格坐标 (col,row) → 归一化 [0,1]
    pad = 1.5  # 字符单位边距
    tw = grid_w + 2 * pad
    th = grid_h + 2 * pad

    def nx(c: int) -> float: return (c + pad) / tw
    def ny(r: int) -> float: return 1.0 - (r + pad) / th

    def _centered_horizontal_arrow_y(source: Box, target: Box, fallback_row: int) -> float:
        """尽量把横向箭头放在两端方框的共同垂直中线位置。"""
        top = max(source.row1, target.row1)
        bottom = min(source.row2, target.row2)
        if top <= bottom:
            row = (top + bottom) / 2
        else:
            # 两个框没有重叠时，退回到两个中心的平均位置，避免箭头偏向某一侧
            row = ((source.row1 + source.row2) + (target.row1 + target.row2)) / 4
            row = (row + fallback_row) / 2
        return ny(row)

    # 画布
    fig = plt.figure(figsize=(tw / 6, th / 6), dpi=dpi)
    ax = fig.add_axes([0, 0, 1, 1])
    ax.set_xlim(0, 1)
    ax.set_ylim(0, 1)
    ax.axis("off")

    # ── 递归画框 ──────────────────────────────────────
    def _draw(box: Box, level: int = 0):
        x1, y1 = nx(box.col1), ny(box.row2)   # 左下
        x2, y2 = nx(box.col2), ny(box.row1)   # 右上
        bw, bh = x2 - x1, y2 - y1

        lw = 1.5 if level == 0 else 1.0
        ax.add_patch(Rectangle((x1, y1), bw, bh, fill=False, linewidth=lw))

        # 内部标题：非最外层框 → 居中；最外层框 → 靠顶部放置
        if box.title and level > 0:
            fs = font_size + (2 if level == 1 else 0)
            ax.text(x1 + bw / 2, y1 + bh / 2, box.title,
                    ha="center", va="center",
                    fontproperties=font_props, fontsize=fs,
                    fontweight="bold" if level <= 1 else "normal",
                    linespacing=1.3)

        for child in box.children:
            _draw(child, level + 1)

    for root in roots:
        _draw(root)

    # 最外层框的标题 → 渲染在框正下方（框外底部标注）
    for root in roots:
        if root.title:
            gap = 1.0 / th  # 一行高对应的归一化值
            bx = nx((root.col1 + root.col2) / 2)
            by = ny(root.row2) - gap * 1.5
            ax.text(bx, by, root.title,
                    ha="center", va="top",
                    fontproperties=font_props,
                    fontsize=font_size + 4,
                    fontweight="bold")

    # ── 渲染框外文字 ──────────────────────────────────
    # 按 side 分组后，文字之间留间距
    gap = 1.0 / tw  # 一个字符宽度的归一化值
    for et in external_texts:
        if not et.anchor or not et.side:
            continue
        b = et.anchor
        if et.side == 'right':
            rx = nx(b.col2) + gap
            cy = ny((b.row1 + b.row2) / 2)
            ax.text(rx, cy, et.text, ha="left", va="center",
                    fontproperties=font_props, fontsize=font_size)
        elif et.side == 'left':
            lx = nx(b.col1) - gap
            cy = ny((b.row1 + b.row2) / 2)
            ax.text(lx, cy, et.text, ha="right", va="center",
                    fontproperties=font_props, fontsize=font_size)
        elif et.side == 'top':
            tx = nx((b.col1 + b.col2) / 2)
            ty = ny(b.row1) + gap * 2
            ax.text(tx, ty, et.text, ha="center", va="bottom",
                    fontproperties=font_props, fontsize=font_size)
        elif et.side == 'bottom':
            bx = nx((b.col1 + b.col2) / 2)
            by = ny(b.row2) - gap * 2
            ax.text(bx, by, et.text, ha="center", va="top",
                    fontproperties=font_props, fontsize=font_size)

    # ── 画箭头（箭头字符所在列/行 → 框边缘） ────────
    for arr in arrows:
        if arr.dir == 'down' and arr.source and arr.target:
            # 从源框底边 → 目标框顶边，在箭头字符所在列垂直连接
            x = nx(arr.col)
            y_src = ny(arr.source.row2)
            y_tgt = ny(arr.target.row1)
            ax.annotate("", xy=(x, y_tgt), xytext=(x, y_src),
                        arrowprops={"arrowstyle": "-|>", "lw": 1.2,
                                     "connectionstyle": "arc3,rad=0"})
        elif arr.dir == 'hdouble' and arr.source and arr.target:
            # 左右框之间，尽量把箭头放在两端框的垂直公共中线附近
            y = _centered_horizontal_arrow_y(arr.source, arr.target, arr.row)
            x_src = nx(arr.source.col2)
            x_tgt = nx(arr.target.col1)
            ax.annotate("", xy=(x_tgt, y), xytext=(x_src, y),
                        arrowprops={"arrowstyle": "<->", "lw": 1.2,
                                     "connectionstyle": "arc3,rad=0"})
        # fallback
        else:
            cx, cy = nx(arr.col), ny(arr.row)
            if arr.dir == 'down':
                ax.scatter(cx, cy, marker="v", s=40, c="gray", zorder=5)
            elif arr.dir == 'hdouble':
                ax.scatter(cx, cy, marker="_", s=40, c="gray", zorder=5)

    fig.savefig(out_path, bbox_inches="tight", pad_inches=0.3)
    plt.close(fig)
    print(f"渲染完成: {out_path.name}")


# ═══════════════════════════════════════════════════════════════
#  输入数据（改这里即可渲染自己的字符画）
# ═══════════════════════════════════════════════════════════════

DIAGRAM_TEXT = r"""
┌─────────────────────────────────────────────────────────────────────────────┐
│                               SoSoC 项目架构                                 │
│                                                                             │
│  ┌──────────────┐    ↔    ┌─────────────────────┐    ↔    ┌────────────────┐ │
│  │    NEMU      │         │      DiffTest       │         │    NPC RTL     │ │
│  │  (参考模型)   │         │    (差分测试)         │         │   (RTL 目标)   │ │
│  └──────────────┘         └─────────────────────┘         └────────────────┘ │
│        │                                                         │          │
│        ▼                                                         ▼          │
│  ┌───────────────────────────────────────────────────────────────────────┐  │
│  │                         BSP (板级支持包)                               │  │
│  │                                                                       │  │
│  │        ┌──────┐        ┌────────┐        ┌────────┐                  │  │
│  │        │  AM  │        │  klib  │        │  libc  │                  │  │
│  │        └──────┘        └────────┘        └────────┘                  │  │
│  └───────────────────────────────────────────────────────────────────────┘  │
│                                │                                            │
│                                ▼                                            │
│  ┌───────────────────────────────────────────────────────────────────────┐  │
│  │                          tests / benchmarks                           │  │
│  │        cpu-tests | CoreMark | Dhrystone | MicroBench                  │  │
│  └───────────────────────────────────────────────────────────────────────┘  │
│                                                                             │
│  ┌──────────────┐   ↔   ┌────────────────┐   ↔   ┌────────────────┐         │
│  │    Bus       │       │     Periph     │       │      FPGA      │         │
│  │  (AXI互联)    │       │  (外设控制器)   │       │   (上板验证)    │          │
│  └──────────────┘       └────────────────┘       └────────────────┘         │
└─────────────────────────────────────────────────────────────────────────────┘
"""


# ═══════════════════════════════════════════════════════════════
#  入口
# ═══════════════════════════════════════════════════════════════

_FONT_CANDIDATES = [
    # Linux — Noto CJK (Arch / Ubuntu / Debian / Fedora)
    Path("/usr/share/fonts/noto-cjk/NotoSansCJK-Medium.ttc"),
    Path("/usr/share/fonts/noto/NotoSansCJK-Medium.ttc"),
    Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Medium.ttc"),
    Path("/usr/share/fonts/truetype/noto/NotoSansCJK-Medium.ttc"),
    Path("/usr/share/fonts/google-noto-cjk/NotoSansCJK-Medium.ttc"),
    # Linux — WenQuanYi
    Path("/usr/share/fonts/wenquanyi/wqy-zenhei/wqy-zenhei.ttc"),
    Path("/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc"),
    # macOS (Homebrew)
    Path("/usr/local/share/fonts/noto-sans-cjk/NotoSansCJK-Medium.ttc"),
    Path("/opt/homebrew/share/fonts/noto-sans-cjk/NotoSansCJK-Medium.ttc"),
    # ~/.local/share/fonts (手动安装)
    Path.home() / ".local/share/fonts/NotoSansCJK-Medium.ttc",
    # Windows / WSL
    Path("/mnt/c/Windows/Fonts/msyh.ttc"),       # Microsoft YaHei
    Path("/mnt/c/Windows/Fonts/simsun.ttc"),      # SimSun
]


def _find_cjk_font() -> Path:
    """在系统中自动搜索一个可用的中文字体。"""
    for path in _FONT_CANDIDATES:
        if path.exists():
            return path
    # Linux: 用 fontconfig 匹配任意中文字体
    try:
        result = subprocess.run(
            ["fc-match", "-f", "%{file}", "sans-serif"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0 and result.stdout.strip():
            p = Path(result.stdout.strip())
            if p.exists():
                return p
    except Exception:
        pass
    raise FileNotFoundError(
        "找不到中文字体。请安装一个 CJK 字体，或通过 --font 参数指定路径。\n"
        "  Arch Linux:       sudo pacman -S noto-fonts-cjk\n"
        "  Ubuntu / Debian:  sudo apt install fonts-noto-cjk\n"
        "  Fedora:           sudo dnf install google-noto-cjk-fonts\n"
        "  macOS:            brew install font-noto-sans-cjk"
    )


def main() -> None:
    ap = argparse.ArgumentParser(description="ASCII 字符画 → matplotlib 矢量图")
    ap.add_argument("--out", type=Path,
                    help="直接指定输出图片完整路径，优先级高于 --out-dir / --filename")
    ap.add_argument("--out-dir", type=Path,
                    default=Path(__file__).resolve().parents[1]
                    / "source" / "resource",
                    help="输出目录，默认写到 report/source/resource")
    ap.add_argument("--filename", type=str,
                    default="architecture-overview-flow.png",
                    help="输出文件名，默认 architecture-overview-flow.png")
    ap.add_argument("--font", type=Path, default=None,
                    help="字体路径，不指定则自动检测")
    ap.add_argument("--font-size", type=int, default=14)
    ap.add_argument("--dpi", type=int, default=200)
    args = ap.parse_args()

    out_path = args.out if args.out else args.out_dir / args.filename

    font_path = args.font if args.font else _find_cjk_font()

    roots, arrows, ext_texts, gw, gh = parse_diagram(DIAGRAM_TEXT)
    ext_right = sum(1 for e in ext_texts if e.side == 'right')
    print(f"解析结果: {len(roots)} 顶层方框, {len(arrows)} 箭头, "
          f"{len(ext_texts)} 框外文字(R:{ext_right}), 网格 {gw}×{gh}")
    print(f"字体: {font_path}")

    render_diagram(roots, arrows, ext_texts, gw, gh,
                 font_path, args.font_size, args.dpi, out_path)


if __name__ == "__main__":
    main()
