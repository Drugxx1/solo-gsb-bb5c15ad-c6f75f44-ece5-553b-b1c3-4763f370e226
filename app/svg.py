"""打印 / 预览 SVG 生成。

与页面预览共用 compute_layout 输出的毫米坐标：浏览器和打印工具都
按 viewBox 等比缩放，坐标本身永远是毫米，因此预览所见即导出所得。

打印位置校准：calibration 给定时（仅下载 / 打印的 SVG 会带），所有
绘制元素按线性走纸误差的逆变换逐点修正——坐标与尺寸平移/缩放，
线宽与字号保持不变；viewBox 与纸张尺寸仍是原始纸张。常规预览不传
calibration，坐标与 /api/layout 返回完全一致。

裁切角线避让：每个标签四角的 8 段角线逐段求可绘部分——与本页其他
标签轮廓相交、与首页占用区相交、或落到纸张范围之外的残段被裁去，
残段短于 0.5mm 时整段省略。因此标签紧贴（间距为 0）或靠近纸边时
角线不会侵入邻签、占用区或画到纸外。判定一律在最终 SVG 坐标空间
进行：应用走纸校准时，邻签、占用区与纸边都按逆变换补偿后的坐标
参与避让。标签轮廓本身、排版数量与位置不随角线避让改变。

流水号：排版结果中 serial 非空的标签在内部居中绘制同一编号
（预览、下载与打印完全相同，因为都来自这份排版结果）。字号在
最终坐标空间按标签补偿后的内框自动选取；即便最小可读字号，编号
仍会超出标签内宽时抛出明确中文错误（/api/svg 返回 400），绝不
输出越界文字。未启用编号的标签保持「名称 + 尺寸」原标注。

原位补印：build_svg 的 only 参数（或 build_reprint_svg）只绘制
完整排版中选中标签的轮廓、编号、标注与裁切角线；未选位置、页码
标记与页边距线均留白。坐标与流水号直接取自完整排版结果，不重排、
不重新编号；裁切角线的避让判定仍使用本页全部标签与首页占用区
（它们已印在纸上），因此补印角线不会探入未选标签的位置。
"""

from __future__ import annotations

import unicodedata
from xml.sax.saxutils import escape

from .calibration import calibration_marks
from .models import Calibration, LayoutResponse

_CUT = 3.0  # 裁切角线单臂长度 mm
_CUT_INSET = 1.5  # 角线相对标签轮廓的外扩 mm
_MIN_MARK = 0.5  # 残段短于该长度（mm）则整段省略
_EPS = 1e-9

# 编号文字排版参数（字号单位 mm；应用走纸校准时字号不随坐标缩放）
_SERIAL_PADDING = 0.8  # 文字与标签轮廓的左右/上下安全留白
_SERIAL_FS_MIN = 1.8   # 编号最小可读字号，再小即判定放不进
_SERIAL_FS_MAX = 6.0   # 编号最大字号，避免大标签上文字过大
_SERIAL_FS_STEP = 0.1


def _text_width_mm(text: str, font_size: float) -> float:
    """保守估算单行文字在 font_size(mm) 下的渲染宽度。

    按字符 East Asian Width 分类：全角（中日韩字符、全角标点）按
    1.0em，其余拉丁 / 数字 / 半角符号按 0.6em 估算。对 Arial 系字体
    该取值偏保守，确保真实渲染不会越过估算边界。
    """
    units = 0.0
    for ch in text:
        if unicodedata.east_asian_width(ch) in ("W", "F"):
            units += 1.0
        else:
            units += 0.6
    return units * font_size


def _fmt(v: float) -> str:
    """SVG 属性中的毫米数：整数不带小数点，其余保留 2 位。"""
    if abs(v - round(v)) < 1e-9:
        return str(int(round(v)))
    return f"{v:.2f}".rstrip("0").rstrip(".")


def _cut_segments(x: float, y: float, w: float, h: float):
    """矩形四角 8 段轴对齐角线（标签轮廓外扩 _CUT_INSET 的角框上）。

    返回 ("h"/"v", 起点, 固定坐标, 终点)：h 表示水平线 y 固定、沿 x；
    v 表示竖直线 x 固定、沿 y。
    """
    x0, y0 = x - _CUT_INSET, y - _CUT_INSET
    x1, y1 = x + w + _CUT_INSET, y + h + _CUT_INSET
    c = _CUT
    return [
        ("v", y0 + c, x0, y0),          # 左上竖
        ("h", x0, y0, x0 + c),          # 左上横
        ("v", y0, x1, y0 + c),          # 右上竖
        ("h", x1 - c, y0, x1),          # 右上横
        ("v", y1 - c, x1, y1),          # 右下竖
        ("h", x1, y1, x1 - c),          # 右下横
        ("v", y1, x0, y1 - c),          # 左下竖
        ("h", x0 + c, y1, x0),          # 左下横
    ]


def _subtract_interval(
    a0: float, a1: float, b0: float, b1: float
) -> list[tuple[float, float]]:
    """区间 [a0,a1] 减去区间 (b0,b1)（开区间）后的剩余部分。

    开区间相减：本角线恰好贴着对方矩形边缘（端点接触或共线）时不算
    相交，该部分予以保留；只有真正探入矩形内部的段被裁掉。
    """
    if b0 >= a0 + _EPS and b1 <= a1 - _EPS:  # (b) 完全在内部，两侧残段
        return [(a0, b0), (b1, a1)]
    if b0 <= a0 + _EPS and b1 >= a1 - _EPS:  # [a] 完全被覆盖
        return []
    if a0 < b1 - _EPS and a1 > b0 + _EPS:  # 部分相交
        if b0 > a0 + _EPS:  # 相交段在右侧
            return [(a0, b0)]
        return [(b1, a1)]  # 相交段在左侧
    return [(a0, a1)]  # 不相交


def _clip_mark(
    orientation: str, p0: float, fixed: float, p1: float,
    paper: tuple[float, float, float, float],
    blockers: list[tuple[float, float, float, float]],
) -> list[tuple[float, float]]:
    """求单段角线的可绘区间（轴向参数为 p0→p1，固定坐标为 fixed）。

    paper 为纸张矩形 (x0,y0,x1,y1)；blockers 为本页其他标签轮廓与
    （首页）占用区矩形。先裁掉纸外部分，再逐个减去与障碍矩形内部
    相交的区间；区间反转时先归一化。
    """
    lo, hi = (p0, p1) if p0 <= p1 else (p1, p0)
    if orientation == "h":
        lo = max(lo, paper[0])
        hi = min(hi, paper[2])
        if not (fixed > paper[1] + _EPS and fixed < paper[3] - _EPS):
            return []
    else:
        lo = max(lo, paper[1])
        hi = min(hi, paper[3])
        if not (fixed > paper[0] + _EPS and fixed < paper[2] - _EPS):
            return []
    if hi - lo < _EPS:
        return []

    intervals: list[tuple[float, float]] = [(lo, hi)]
    for bx0, by0, bx1, by1 in blockers:
        if orientation == "h":
            # 水平线在矩形的上下边界上（共线）不算探入内部
            if not (fixed > by0 + _EPS and fixed < by1 - _EPS):
                continue
            b_lo, b_hi = bx0, bx1
        else:
            if not (fixed > bx0 + _EPS and fixed < bx1 - _EPS):
                continue
            b_lo, b_hi = by0, by1
        next_intervals: list[tuple[float, float]] = []
        for a0, a1 in intervals:
            next_intervals.extend(_subtract_interval(a0, a1, b_lo, b_hi))
        intervals = next_intervals
        if not intervals:
            break
    return [(a0, a1) for a0, a1 in intervals if a1 - a0 >= _MIN_MARK - _EPS]


def _cut_marks(
    x: float, y: float, w: float, h: float,
    paper: tuple[float, float, float, float],
    blockers: list[tuple[float, float, float, float]],
) -> str:
    """四角角线：逐段裁去纸外、其他标签与占用区内的部分。

    残段短于 0.5mm 时省略；所有段都被裁光时返回空字符串（连角线组
    也不输出）。
    """
    cmds: list[str] = []
    for orientation, p0, fixed, p1 in _cut_segments(x, y, w, h):
        for a0, a1 in _clip_mark(orientation, p0, fixed, p1, paper, blockers):
            if orientation == "h":
                cmds.append(
                    f"M{_fmt(a0)} {_fmt(fixed)} H{_fmt(a1)}"
                )
            else:
                cmds.append(
                    f"M{_fmt(fixed)} {_fmt(a0)} V{_fmt(a1)}"
                )
    if not cmds:
        return ""
    return (
        f'<g class="cutmarks" stroke="#d23f3f" stroke-width="{_fmt(0.15)}" '
        f'fill="none">'
        f'<path d="{"".join(cmds)}"/>'
        "</g>"
    )


def _fit_font_size(text: str, max_w: float, max_h: float,
                   fs_min: float, fs_max: float, step: float = _SERIAL_FS_STEP):
    """在 max_w×max_h 内框中为单行文字选最大字号；min 字号都放不下返回 None。

    行高按 1.0 个字号计（覆盖升部/降部），宽度按 _text_width_mm 保守估算。
    """
    fs = min(fs_max, max_h, max_w)
    fs = round(fs / step) * step  # 向下对齐到 0.1mm 网格
    if fs > min(fs_max, max_h, max_w) + _EPS:
        fs -= step
    while fs >= fs_min - _EPS:
        if fs <= max_h + _EPS and _text_width_mm(text, fs) <= max_w + _EPS:
            return round(fs, 2)
        fs -= step
    return None


def _serial_elements(
    lab, lx: float, ly: float, lw: float, lh: float,
    show_annotations: bool,
) -> str:
    """生成一个启用流水号标签内部的 <text> 元素串。

    编号始终绘制且必须放得下（放不下抛 ValueError，阻止该页 SVG 生成）；
    名称、尺寸仅在「显示标注」打开、且宽度与垂直空间都充足时作为上下
    小字附加，任一行放不下就自动省略。所有几何判定使用最终坐标
    （走纸校准时即补偿后坐标），字号不随校准缩放。
    """
    label_name = lab.name or f"#{lab.type_index + 1}"
    serial = lab.serial or ""
    inner_w = max(0.0, lw - 2 * _SERIAL_PADDING)
    if inner_w <= 0 or lh < 2 * _SERIAL_PADDING + _SERIAL_FS_MIN:
        raise ValueError(
            f"编号「{serial}」放不进标签「{label_name}」：标签内框仅 "
            f"{_fmt(lw)}×{_fmt(lh)}mm，没有可供编号书写的空间。请加大标签"
            "尺寸、缩短前缀/减小补零位数，或取消该规格的流水号。"
        )

    name_txt = (lab.name or f"#{lab.type_index + 1}")[:12]
    if lab.rotated:
        name_txt += " ↻"
    size_txt = f"{_fmt(lab.width)}×{_fmt(lab.height)}"

    fs_name = fs_size = None
    if show_annotations:
        # 附注最小 1.5mm；宽度放不下的行直接省略
        fs_name = _fit_font_size(name_txt, inner_w, 2.0, 1.5, 2.0)
        fs_size = _fit_font_size(size_txt, inner_w, 1.8, 1.5, 1.8)

    gap = 0.3
    top_extra = 0.0  # 编号行上方被附注占据的高度
    if fs_name is not None:
        top_extra += fs_name + gap
    if fs_size is not None:
        top_extra += fs_size + gap
    avail_h = lh - 2 * _SERIAL_PADDING - top_extra
    fs_serial = _fit_font_size(
        serial, inner_w, avail_h, _SERIAL_FS_MIN,
        min(_SERIAL_FS_MAX, avail_h),
    )
    if fs_serial is None:
        # 附注挤占导致失败时，再尝试只留编号一行
        fs_name = fs_size = None
        avail_h = lh - 2 * _SERIAL_PADDING
        fs_serial = _fit_font_size(
            serial, inner_w, avail_h, _SERIAL_FS_MIN,
            min(_SERIAL_FS_MAX, avail_h),
        )
    if fs_serial is None:
        raise ValueError(
            f"编号「{serial}」放不进标签「{label_name}」：标签内框约 "
            f"{_fmt(lw)}×{_fmt(lh)}mm，即使以 {_SERIAL_FS_MIN:g}mm 最小字号"
            "绘制，编号仍会越过标签边界。请缩短前缀、减小补零位数 / 起始号，"
            "或加大标签尺寸；若正在应用打印校准，请核对实测数据（逆变换会"
            "改变标签在纸上的可用尺寸）。"
        )

    block_h = top_extra + fs_serial
    cx = lx + lw / 2
    ty = ly + (lh - block_h) / 2  # 当前文字行的顶部 y
    parts: list[str] = []
    if fs_name is not None:
        parts.append(
            f'<text class="annot" x="{_fmt(cx)}" y="{_fmt(ty + fs_name * 0.8)}" '
            f'font-size="{_fmt(fs_name)}mm">{escape(name_txt)}</text>'
        )
        ty += fs_name + gap
    if fs_size is not None:
        parts.append(
            f'<text class="annot" x="{_fmt(cx)}" y="{_fmt(ty + fs_size * 0.8)}" '
            f'font-size="{_fmt(fs_size)}mm">{escape(size_txt)}</text>'
        )
        ty += fs_size + gap
    parts.append(
        f'<text class="serial" x="{_fmt(cx)}" y="{_fmt(ty + fs_serial * 0.8)}" '
        f'font-size="{_fmt(fs_serial)}mm">{escape(serial)}</text>'
    )
    return "".join(parts)


def resolve_reprint_selection(
    plan: LayoutResponse, page_no: int, indexes: list[int]
) -> set[int]:
    """校验补印选择（页内标签序号，从 1 开始），返回 0 基索引集合。

    选择为空、页码越界或序号超出本页标签数量时抛出明确中文
    ValueError（由接口转为 HTTP 400）。重复序号去重，不算错误。
    """
    page = next((p for p in plan.pages if p.page == page_no), None)
    if page is None:
        raise ValueError(f"页码超出范围：共 {len(plan.pages)} 页，请求第 {page_no} 页。")
    if not indexes:
        raise ValueError(
            "补印选择为空：请先在预览中点选本页需要补印的标签，再导出补印 SVG。"
        )
    total = len(page.labels)
    for i in indexes:
        if i < 1 or i > total:
            raise ValueError(
                f"页内标签序号 {i} 超出范围：第 {page_no} 页共排入 {total} 个标签"
                f"（序号为 1–{total}）。请重新生成拼版后按最新预览点选。"
            )
    return {i - 1 for i in indexes}


def build_reprint_svg(plan: LayoutResponse, page_no: int,
                      indexes: list[int], show_annotations: bool = True,
                      calibration: Calibration | None = None) -> str:
    """原位补印 SVG：仅绘制第 page_no 页中选中序号（从 1 开始）的标签。

    plan 必须是完整原排版结果；选中标签的坐标与流水号原样取自其中，
    未选位置、页码标记与页边距线均不绘制。
    """
    only = resolve_reprint_selection(plan, page_no, indexes)
    return build_svg(
        plan, page_no, show_margins=False, show_annotations=show_annotations,
        show_occupied=False, calibration=calibration, only=only,
    )


def build_svg(plan: LayoutResponse, page_no: int,
              show_margins: bool = True, show_annotations: bool = True,
              show_occupied: bool = False,
              calibration: Calibration | None = None,
              only: set[int] | None = None) -> str:
    page = next((p for p in plan.pages if p.page == page_no), None)
    if page is None:
        raise ValueError(f"页码超出范围：共 {len(plan.pages)} 页，请求第 {page_no} 页。")
    reprint = only is not None
    if reprint and not only:
        raise ValueError("补印选择为空：没有可绘制的标签。")

    # 逆变换：打印机满足 实测 = scale·设计 + offset，导出写入 设计' 使得
    # scale·设计' + offset = 设计，即 (设计 - offset) / scale。
    # 无校准时恒等，输出与未加校准功能前逐字节同构。
    if calibration is None:
        def fx(v: float) -> float:
            return v
        fy = fw = fh = fx
    else:
        xs, xo = calibration.x.scale, calibration.x.offset
        ys, yo = calibration.y.scale, calibration.y.offset

        def fx(v: float) -> float:
            return (v - xo) / xs

        def fy(v: float) -> float:
            return (v - yo) / ys

        def fw(v: float) -> float:
            return v / xs

        def fh(v: float) -> float:
            return v / ys

    w, h = plan.page_width, plan.page_height
    parts: list[str] = [
        '<?xml version="1.0" encoding="UTF-8" standalone="no"?>',
        (
            f'<svg xmlns="http://www.w3.org/2000/svg" version="1.1" '
            f'width="{_fmt(w)}mm" height="{_fmt(h)}mm" '
            f'viewBox="0 0 {_fmt(w)} {_fmt(h)}">'
        ),
        f'<rect x="0" y="0" width="{_fmt(w)}" height="{_fmt(h)}" fill="#ffffff"/>',
        (
            '<style>.label{fill:none;stroke:#222;stroke-width:0.15;}'
            ".annot{font-family:Arial,'Noto Sans CJK SC',sans-serif;"
            "font-size:2.2mm;fill:#9aa0a6;text-anchor:middle;}"
            ".serial{font-family:Arial,'Noto Sans CJK SC',sans-serif;"
            "font-weight:bold;fill:#111;text-anchor:middle;}"
            ".marginguide{fill:none;stroke:#5b8def;stroke-width:0.1;stroke-dasharray:1 1;}"
            ".pageno{font-family:Arial,sans-serif;font-size:2.5mm;fill:#9aa0a6;}</style>"
        ),
    ]
    if calibration is not None:
        parts.append(
            f"<!-- 打印位置校准已应用：X scale={calibration.x.scale} "
            f"offset={calibration.x.offset}mm；Y scale={calibration.y.scale} "
            f"offset={calibration.y.offset}mm（按逆变换补偿） -->"
        )
    if reprint:
        shown = ", ".join(str(i + 1) for i in sorted(only))
        parts.append(
            f"<!-- 原位补印：仅绘制第 {page_no} 页选中的 {len(only)} 个标签"
            f"（页内序号 {shown}），坐标与编号保持完整原排版，"
            "未选位置、页码与边距线留白 -->"
        )

    # 占用区仅存在于第一张纸；仅在预览（show_occupied）时绘制，
    # 下载 / 打印 / 补印的 SVG 跳过本块（连同其专用样式），因此只含新标签。
    if show_occupied and not reprint and page_no == 1 and plan.occupied_zones:
        parts.append(
            '<defs><pattern id="occHatch" patternUnits="userSpaceOnUse" '
            'width="3" height="3" patternTransform="rotate(45)">'
            '<rect width="3" height="3" fill="#e8eaed"/>'
            '<line x1="0" y1="0" x2="0" y2="3" stroke="#b9bdc4" stroke-width="0.5"/>'
            "</pattern></defs>"
        )
        parts.append(
            "<style>.occupied{fill:url(#occHatch);stroke:#9aa0a6;"
            "stroke-width:0.15;stroke-dasharray:1.2 0.8;}"
            ".occtext{font-family:Arial,'Noto Sans CJK SC',sans-serif;"
            "font-size:2.5mm;fill:#7b808a;text-anchor:middle;}</style>"
        )
        for z in plan.occupied_zones:
            parts.append(
                f'<rect class="occupied" x="{_fmt(fx(z.x))}" y="{_fmt(fy(z.y))}" '
                f'width="{_fmt(fw(z.width))}" height="{_fmt(fh(z.height))}"/>'
            )
            if min(z.width, z.height) >= 12:
                cx, cy = fx(z.x + z.width / 2), fy(z.y + z.height / 2)
                parts.append(
                    f'<text class="occtext" x="{_fmt(cx)}" y="{_fmt(cy + 0.9)}">'
                    "已占用</text>"
                )

    # 补印页不画页边距参考线（留白），普通预览 / 整页导出保持原行为
    if show_margins and not reprint:
        mx0, my0 = fx(plan.margin_left), fy(plan.margin_top)
        mx1, my1 = fx(w - plan.margin_right), fy(h - plan.margin_bottom)
        parts.append(
            f'<rect class="marginguide" x="{_fmt(mx0)}" y="{_fmt(my0)}" '
            f'width="{_fmt(mx1 - mx0)}" height="{_fmt(my1 - my0)}"/>'
        )

    # 角线避让用几何（全部在最终 SVG 坐标空间，校准时即补偿后坐标）：
    # 纸张矩形——物理纸边映射回本坐标系为 fx(0)/fx(w)（校准时并非 0/w）；
    # 本页每个标签轮廓（补印时未选标签也算，它们已印在纸上）；
    # 首页占用区（即便导出不绘制占用区，角线仍须避让）。
    paper_rect = (fx(0), fy(0), fx(w), fy(h))
    label_rects = [
        (
            fx(lab.x), fy(lab.y),
            fx(lab.x + lab.width), fy(lab.y + lab.height),
        )
        for lab in page.labels
    ]
    zone_rects: list[tuple[float, float, float, float]] = []
    if page_no == 1:
        zone_rects = [
            (
                fx(z.x), fy(z.y),
                fx(z.x + z.width), fy(z.y + z.height),
            )
            for z in plan.occupied_zones
        ]

    for idx, lab in enumerate(page.labels):
        if reprint and idx not in only:
            continue  # 补印：未选位置留白，只绘制选中标签
        lx, ly, lw, lh = fx(lab.x), fy(lab.y), fw(lab.width), fh(lab.height)
        parts.append(
            f'<rect class="label" x="{_fmt(lx)}" y="{_fmt(ly)}" '
            f'width="{_fmt(lw)}" height="{_fmt(lh)}"/>'
        )
        blockers = [r for k, r in enumerate(label_rects) if k != idx]
        blockers.extend(zone_rects)
        marks = _cut_marks(lx, ly, lw, lh, paper_rect, blockers)
        if marks:
            parts.append(marks)
        if lab.serial is not None:
            # 启用流水号：编号始终居中绘制；放不下时在此抛错，
            # 由 /api/svg 转为 400，避免任何文字越界
            parts.append(
                _serial_elements(lab, lx, ly, lw, lh, show_annotations)
            )
        elif show_annotations and min(lab.width, lab.height) >= 8:
            name = escape((lab.name or f"#{lab.type_index + 1}")[:12])
            tag = name + (" ↻" if lab.rotated else "")
            size_txt = f"{_fmt(lab.width)}×{_fmt(lab.height)}"
            fs = 2.2
            if len(tag) > 8:
                fs = 1.8
            cx, cy = lx + lw / 2, ly + lh / 2
            parts.append(
                f'<text class="annot" x="{_fmt(cx)}" y="{_fmt(cy - 0.2)}" '
                f'font-size="{_fmt(fs)}mm">{tag}</text>'
                f'<text class="annot" x="{_fmt(cx)}" y="{_fmt(cy + fs + 0.8)}" '
                f'font-size="{_fmt(min(fs, 2.0))}mm">{size_txt}</text>'
            )

    # 补印页不画页码标记（留白），普通预览 / 整页导出保持原行为
    if not reprint:
        parts.append(
            f'<text class="pageno" x="{_fmt(fx(w / 2))}" y="{_fmt(fy(h - 2))}">'
            f"Page {page.page} / {len(plan.pages)}</text>"
        )
    parts.append("</svg>")
    return "\n".join(parts)


def build_calibration_page(page_width: float, page_height: float) -> str:
    """打印位置校准测试页：横、纵两组基准刻线，每根刻线标注设计坐标。

    纵向实线（沿 X 方向均布）用于校准 X：尺量其到纸张左边缘的距离；
    横向虚线（沿 Y 方向均布）用于校准 Y：尺量其到纸张顶边缘的距离。
    所有位置按纸张比例布置，任意纸张尺寸均可使用；坐标标注与
    /api/calibration/marks 返回的设计坐标一致。
    """
    x_marks, y_marks = calibration_marks(page_width, page_height)
    w, h = page_width, page_height
    cx = w / 2
    line_top, line_bottom = h * 0.14, h * 0.95
    line_left, line_right = w * 0.08, w * 0.96
    parts: list[str] = [
        '<?xml version="1.0" encoding="UTF-8" standalone="no"?>',
        (
            f'<svg xmlns="http://www.w3.org/2000/svg" version="1.1" '
            f'width="{_fmt(w)}mm" height="{_fmt(h)}mm" '
            f'viewBox="0 0 {_fmt(w)} {_fmt(h)}">'
        ),
        f'<rect x="0" y="0" width="{_fmt(w)}" height="{_fmt(h)}" fill="#ffffff"/>',
        (
            "<style>"
            ".cal-title{font-family:Arial,'Noto Sans CJK SC',sans-serif;"
            "font-size:4mm;fill:#111;text-anchor:middle;font-weight:bold;}"
            ".cal-note{font-family:Arial,'Noto Sans CJK SC',sans-serif;"
            "font-size:2.6mm;fill:#333;text-anchor:middle;}"
            ".vline{stroke:#0b57d0;stroke-width:0.3;}"
            ".hline{stroke:#0b57d0;stroke-width:0.3;stroke-dasharray:5 2;}"
            ".marklab{font-family:Arial,sans-serif;font-size:2.8mm;fill:#0b57d0;}"
            ".frame{fill:none;stroke:#9aa0a6;stroke-width:0.2;}"
            "</style>"
        ),
        f'<rect class="frame" x="0.4" y="0.4" width="{_fmt(w - 0.8)}" height="{_fmt(h - 0.8)}"/>',
        f'<text class="cal-title" x="{_fmt(cx)}" y="{_fmt(h * 0.04)}">'
        f"打印位置校准测试页（纸张 {_fmt(w)}×{_fmt(h)}mm）</text>",
        f'<text class="cal-note" x="{_fmt(cx)}" y="{_fmt(h * 0.04 + 4.5)}">'
        f"请以 100% 缩放打印并关闭「适应页面/无边距缩放」；"
        f"竖直实线 X1–X{len(x_marks)} 量到纸左缘、水平虚线 Y1–Y{len(y_marks)} "
        "量到纸顶缘，读数(mm)填入软件实测列</text>",
    ]
    for i, mx in enumerate(x_marks):
        parts.append(
            f'<line class="vline" x1="{_fmt(mx)}" y1="{_fmt(line_top)}" '
            f'x2="{_fmt(mx)}" y2="{_fmt(line_bottom)}"/>'
        )
        parts.append(
            f'<text class="marklab" text-anchor="middle" x="{_fmt(mx)}" '
            f'y="{_fmt(line_top - 2)}">X{i + 1}={_fmt(mx)}</text>'
        )
    for i, my in enumerate(y_marks):
        parts.append(
            f'<line class="hline" x1="{_fmt(line_left)}" y1="{_fmt(my)}" '
            f'x2="{_fmt(line_right)}" y2="{_fmt(my)}"/>'
        )
        parts.append(
            f'<text class="marklab" text-anchor="start" x="{_fmt(line_left)}" '
            f'y="{_fmt(my - 1.5)}">Y{i + 1}={_fmt(my)}</text>'
        )
    parts.append("</svg>")
    return "\n".join(parts)
