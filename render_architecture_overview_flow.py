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
    dir: str = ""   # 'down' | 'up' | 'hdouble'


# ═══════════════════════════════════════════════════════════════
#  框线字符常量（Unicode 码点）
# ═══════════════════════════════════════════════════════════════

_TOPLEFT     = '\u250c'   # ┌
_TOPRIGHT    = '\u2510'   # ┐
_BOTLEFT     = '\u2514'   # └
_BOTRIGHT    = '\u2518'   # ┘
_HORIZ       = '\u2500'   # ─
_VERT        = '\u2502'   # │

_BOX_ENDS = {_TOPLEFT, _TOPRIGHT, _BOTLEFT, _BOTRIGHT}
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

_ALL_SPECIAL = _BOX_ENDS | _BOX_LINES | set(_ARROWS.keys())


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

    return roots, arrows, w, h


# ═══════════════════════════════════════════════════════════════
#  渲染器
# ═══════════════════════════════════════════════════════════════

def render_diagram(
    roots: list[Box],
    arrows: list[Arrow],
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

        if box.title:
            fs = font_size + (4 if level == 0 else 0)
            ax.text(
                x1 + bw / 2, y1 + bh / 2, box.title,
                ha="center", va="center",
                fontproperties=font_props,
                fontsize=fs,
                fontweight="bold" if level <= 1 else "normal",
                linespacing=1.3,
            )

        for child in box.children:
            _draw(child, level + 1)

    for root in roots:
        _draw(root)

    # ── 画箭头 ────────────────────────────────────────
    alen = 0.03
    for arr in arrows:
        cx, cy = nx(arr.col), ny(arr.row)
        if arr.dir in ('down', 'up'):
            dy = -alen if arr.dir == 'down' else alen
            ax.annotate("", xy=(cx, cy + dy), xytext=(cx, cy),
                        arrowprops={"arrowstyle": "-|>", "lw": 1.2})
        elif arr.dir == 'hdouble':
            ax.annotate("", xy=(cx + alen, cy), xytext=(cx - alen, cy),
                        arrowprops={"arrowstyle": "<->", "lw": 1.2})

    fig.savefig(out_path, bbox_inches="tight", pad_inches=0.3)
    plt.close(fig)
    print(f"渲染完成: {out_path.name}")


# ═══════════════════════════════════════════════════════════════
#  输入数据（改这里即可渲染自己的字符画）
# ═══════════════════════════════════════════════════════════════

DIAGRAM_TEXT = r"""\
┌─────────────────────────────────────────────────────────────────────────────┐
│                               SoSoC 项目架构                                │
│                                                                             │
│  ┌──────────────┐    ↔    ┌─────────────────────┐    ↔    ┌────────────────┐ │
│  │    NEMU      │         │      DiffTest        │         │    NPC RTL     │ │
│  │  (参考模型)  │         │    (差分测试)        │         │   (RTL 目标)   │ │
│  └──────────────┘         └─────────────────────┘         └────────────────┘ │
│        │                                                         │          │
│        ▼                                                         ▼          │
│  ┌───────────────────────────────────────────────────────────────────────┐  │
│  │                         BSP (板级支持包)                              │  │
│  │                                                                       │  │
│  │        ┌──────┐        ┌────────┐        ┌────────┐                  │  │
│  │        │  AM  │        │  klib  │        │  libc  │                  │  │
│  │        └──────┘        └────────┘        └────────┘                  │  │
│  └───────────────────────────────────────────────────────────────────────┘  │
│                                │                                            │
│                                ▼                                            │
│  ┌───────────────────────────────────────────────────────────────────────┐  │
│  │                          tests / benchmarks                            │  │
│  │        cpu-tests | CoreMark | Dhrystone | MicroBench                    │  │
│  └───────────────────────────────────────────────────────────────────────┘  │
│                                                                             │
│  ┌──────────────┐   ↔   ┌────────────────┐   ↔   ┌────────────────┐        │
│  │    Bus       │       │     Periph     │       │      FPGA       │        │
│  │  (AXI互联)   │       │  (外设控制器)  │       │   (上板验证)    │        │
│  └──────────────┘       └────────────────┘       └────────────────┘        │
└─────────────────────────────────────────────────────────────────────────────┘
"""


# ═══════════════════════════════════════════════════════════════
#  入口
# ═══════════════════════════════════════════════════════════════

def main() -> None:
    ap = argparse.ArgumentParser(description="ASCII 字符画 → matplotlib 矢量图")
    ap.add_argument("--out", type=Path,
                    default=Path(__file__).resolve().parents[1]
                    / "source" / "resource" / "architecture-overview-flow.png")
    ap.add_argument("--font", type=Path,
                    default=Path("/usr/share/fonts/noto-cjk/NotoSansCJK-Medium.ttc"))
    ap.add_argument("--font-size", type=int, default=14)
    ap.add_argument("--dpi", type=int, default=200)
    args = ap.parse_args()

    if not args.font.exists():
        raise FileNotFoundError(
            f"找不到字体: {args.font}\n请安装 noto-fonts-cjk")

    roots, arrows, gw, gh = parse_diagram(DIAGRAM_TEXT)
    print(f"解析结果: {len(roots)} 顶层方框, {len(arrows)} 箭头, 网格 {gw}×{gh}")

    render_diagram(roots, arrows, gw, gh,
                   args.font, args.font_size, args.dpi, args.out)


if __name__ == "__main__":
    main()
