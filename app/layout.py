"""标签拼版核心算法。

使用天际线（skyline）左下角装箱：标签逐个放到当前可用的最低处，
放不下则续页。支持旋转。所有坐标单位为毫米，原点在纸张左上角。

间距处理：每个标签放置时按 (标签宽 + gap_x) × (标签高 + gap_y)
预留位置，同时把可排区域在右、下方向各扩展一个间距（仅用于装箱
判断）。于是相邻标签之间恰好间隔 gap，而第一张/最后一张标签可以
紧贴页边距，纸张边缘不会多出半个间距。

首张纸占用区（已贴标签等）视为障碍矩形：碰撞盒在占用区右侧、下侧
各外扩一个裁切间距，因此新标签无论从哪个方向接近都至少保持一个
裁切间距。候选落点除天际线段起点外，还包括每个障碍的右缘与左缘
锚点；某锚点与障碍重叠时把 y 抬到障碍下沿之后重试。
"""

from __future__ import annotations

from .models import (
    LabelSpec,
    LayoutRequest,
    LayoutResponse,
    LabelSummary,
    OccupiedZone,
    PageLayout,
    PlacedLabel,
)

EPS = 1e-6


def format_serial(prefix: str, value: int, pad: int) -> str:
    """生成最终编号：前缀 + 起始整数补零（pad=0 不补零，位数超出不截断）。"""
    digits = str(value)
    if pad > 0:
        digits = digits.zfill(pad)
    return f"{prefix}{digits}"


def _reading_order(
    labels: list[PlacedLabel],
) -> list[PlacedLabel]:
    """页内阅读顺序：从上到下成行，同一行内从左到右。

    天际线装箱中，同一逻辑行的标签顶边 y 取自同一条天际线高度，必然
    相等（PlacedLabel 坐标已经两位小数舍入）；而短标签塞进高标签之间
    下方空隙时会有自己更小的 y，属于独立一行。因此按顶边 y 聚类成行、
    行内按左边 x 排序即可正确还原阅读位置。旋转只交换宽高，不影响 y。
    """
    rows: list[list[PlacedLabel]] = []
    for lab in sorted(labels, key=lambda l: (l.y, l.x)):
        if rows and abs(rows[-1][-1].y - lab.y) <= EPS:
            rows[-1].append(lab)
        else:
            rows.append([lab])
    result: list[PlacedLabel] = []
    for row in rows:
        result.extend(sorted(row, key=lambda l: l.x))
    return result


def _r(v: float) -> float:
    """输出坐标统一保留 2 位小数，避免 SVG 里出现长浮点。"""
    return round(v + 0.0, 2)


class _Skyline:
    """天际线段：[x, x+w) 区间内盒子可以放置的最低 y。

    obstacles 为障碍碰撞盒 (x, y, w, h)，已经过间距外扩；只用于
    find_position 判断，不抬升天际线本身。
    """

    def __init__(
        self,
        area_x: float,
        area_y: float,
        content_w: float,
        content_h: float,
        gap_x: float,
        gap_y: float,
        obstacles: list[tuple[float, float, float, float]] | None = None,
    ):
        self.area_x = area_x
        self.area_y = area_y
        self.content_w = content_w  # 真实可排区域
        self.content_h = content_h
        # 盒右最多到达 真实右边界 + 一个间距（把尾部间距“借”到界外）
        self.limit_x = area_x + content_w + gap_x
        self.limit_y = area_y + content_h + gap_y
        self.segments: list[dict] = [
            {"x": area_x, "w": content_w + gap_x, "y": area_y}
        ]
        self.obstacles = obstacles or []

    def _span_height(self, x: float, w: float) -> float | None:
        """覆盖 [x, x+w) 所需的天际线高度；超出天际线范围返回 None。"""
        if x < self.area_x - EPS or x + w > self.limit_x + EPS:
            return None
        y = self.area_y
        covered = 0.0
        for seg in self.segments:
            sx, sw, sy = seg["x"], seg["w"], seg["y"]
            if sx + sw <= x + EPS:
                continue  # 完全在查询区间左侧
            if sx >= x + w - EPS:
                break
            y = max(y, sy)
            covered += min(sx + sw, x + w) - max(sx, x)
        if covered + EPS < w:
            return None
        return y

    def _candidate_xs(self, w: float) -> list[float]:
        """所有可能产生最优落点的 x 锚点。

        天际线段起点（已放盒子的右/左缘）之外，再补充每个障碍的
        右缘（放到障碍右侧）和左缘 - 盒宽（放到障碍左侧）。
        """
        xs = [seg["x"] for seg in self.segments]
        for ox, _, ow, _ in self.obstacles:
            right = ox + ow
            if right + w <= self.limit_x + EPS:
                xs.append(right)
            left_anchor = ox - w
            if left_anchor >= self.area_x - EPS:
                xs.append(left_anchor)
        return xs

    def _clear_obstacles(
        self, x: float, y: float, w: float, h: float
    ) -> float | None:
        """锚点 (x, y) 与障碍重叠时，不断把 y 抬到相碰障碍的下沿。

        水平方向的避让由 _candidate_xs 的障碍边缘锚点保证；这里只需
        处理垂直方向。抬出可排区域即视为该锚点不可用。
        """
        while True:
            hit = None
            for ox, oy, ow, oh in self.obstacles:
                # 严格重叠（含 EPS 容差）；恰好贴边不算碰
                if (
                    x < ox + ow - EPS
                    and x + w > ox + EPS
                    and y < oy + oh - EPS
                    and y + h > oy + EPS
                ):
                    hit = oy + oh
                    break
            if hit is None:
                return y
            y = hit
            if y + h > self.limit_y + EPS:
                return None

    def find_position(self, w: float, h: float) -> tuple[float, float] | None:
        """在天际线上找能容纳 w×h 盒子的位置，先最低 y、再最左 x。

        边界条件为 盒右 ≤ 内容右 + gap；因盒宽 = 标签宽 + gap，
        等价于标签右缘不超出内容区，尾部 gap 可以悬空。
        """
        best: tuple[float, float] | None = None
        for x in dict.fromkeys(self._candidate_xs(w)):
            if x < self.area_x - EPS or x + w > self.limit_x + EPS:
                continue
            y = self._span_height(x, w)
            if y is None:
                continue
            y = self._clear_obstacles(x, y, w, h)
            if y is None or y + h > self.limit_y + EPS:
                continue
            if best is None or y < best[1] - EPS or (abs(y - best[1]) <= EPS and x < best[0]):
                best = (x, y)
        return best

    def add(self, x: float, y: float, w: float, h: float) -> None:
        """放入 w×h 盒子（左上角 x,y）后抬升对应天际线。"""
        new_top = y + h
        kept: list[dict] = []
        for seg in self.segments:
            sx, sw, sy = seg["x"], seg["w"], seg["y"]
            if sx + sw <= x + EPS or sx >= x + w - EPS:
                kept.append(seg)  # 完全在盒子左/右侧
                continue
            if sx < x - EPS:  # 左侧残余
                kept.append({"x": sx, "w": x - sx, "y": sy})
            if sx + sw > x + w + EPS:  # 右侧残余
                kept.append({"x": x + w, "w": sx + sw - (x + w), "y": sy})
        kept.append({"x": x, "w": w, "y": new_top})
        kept.sort(key=lambda s: s["x"])
        # 合并同高相邻段，抑制线段数量增长
        merged: list[dict] = []
        for seg in kept:
            if merged and abs(merged[-1]["y"] - seg["y"]) <= EPS and abs(
                merged[-1]["x"] + merged[-1]["w"] - seg["x"]
            ) <= EPS:
                merged[-1]["w"] += seg["w"]
            else:
                merged.append(dict(seg))
        self.segments = merged


def compute_layout(req: LayoutRequest) -> LayoutResponse:
    """执行整批标签的拼版计算。"""

    area_x = req.margin_left
    area_y = req.margin_top
    area_w = req.page_width - req.margin_left - req.margin_right
    area_h = req.page_height - req.margin_top - req.margin_bottom

    warnings: list[str] = []
    if area_w <= EPS or area_h <= EPS:
        raise ValueError(
            "页边距过大：上下/左右边距之和不能达到或超过纸张尺寸，请减小页边距。"
        )

    # 校验占用区：坐标与尺寸必须有限，且整个区域在纸张范围内
    for k, z in enumerate(req.occupied_zones):
        tag = f"占用区 {k + 1}"
        for vname, v in (("x", z.x), ("y", z.y), ("宽", z.width), ("高", z.height)):
            if v != v or v in (float("inf"), float("-inf")):
                raise ValueError(f"{tag}的{vname}不是有效数字。")
        if z.x < -EPS or z.y < -EPS:
            raise ValueError(
                f"{tag}越界：左上角坐标 ({_r(z.x)}, {_r(z.y)}) 不能为负数。"
            )
        if z.x + z.width > req.page_width + EPS:
            raise ValueError(
                f"{tag}越界：x({_r(z.x)}) + 宽({_r(z.width)}) = {_r(z.x + z.width)}mm "
                f"超过纸张宽度 {_r(req.page_width)}mm。"
            )
        if z.y + z.height > req.page_height + EPS:
            raise ValueError(
                f"{tag}越界：y({_r(z.y)}) + 高({_r(z.height)}) = {_r(z.y + z.height)}mm "
                f"超过纸张高度 {_r(req.page_height)}mm。"
            )
        # 完全落在页边距区域内时不报错（纸张内即合法），但给出提示
        if (
            z.x + z.width <= area_x + EPS
            or z.x >= area_x + area_w - EPS
            or z.y + z.height <= area_y + EPS
            or z.y >= area_y + area_h - EPS
        ):
            warnings.append(
                f"{tag} ({_r(z.x)},{_r(z.y)}) {_r(z.width)}×{_r(z.height)}mm "
                "完全位于页边距区域内，不会影响排版。"
            )

    # 首页障碍碰撞盒：占用区向右、向下各外扩一个裁切间距。
    # 盒宽 = 标签宽 + gap，故盒与障碍盒不重叠 ⇔ 新标签与占用区在
    # 水平或垂直方向上至少相隔一个裁切间距。
    first_page_obstacles: list[tuple[float, float, float, float]] = [
        (z.x, z.y, z.width + req.gap_x, z.height + req.gap_y)
        for z in req.occupied_zones
    ]

    # 装箱用区域（向右、向下各借用一个间距）
    sky_area_w = area_w + req.gap_x
    sky_area_h = area_h + req.gap_y

    def candidate_boxes(i: int) -> list[tuple[bool, float, float]]:
        """返回 [(rotated, 盒宽, 盒高)]，不旋转方向优先。"""
        spec: LabelSpec = req.labels[i]
        boxes = [(False, spec.width + req.gap_x, spec.height + req.gap_y)]
        if spec.allow_rotate:
            boxes.append((True, spec.height + req.gap_x, spec.width + req.gap_y))
        return boxes

    # 预检：任何方向都放不下的标签直接判为排入失败
    fits_any: list[bool] = []
    for i, spec in enumerate(req.labels):
        ok = any(
            bw <= sky_area_w + EPS and bh <= sky_area_h + EPS
            for _, bw, bh in candidate_boxes(i)
        )
        fits_any.append(ok)
        if not ok:
            warnings.append(
                f"标签「{spec.name or i + 1}」{_r(spec.width)}×{_r(spec.height)}mm "
                f"超过可排版区域 {_r(area_w)}×{_r(area_h)}mm，全部 {spec.count} 份未能排入。"
            )

    pages: list[PageLayout] = []
    placed_count = [0] * len(req.labels)
    rotated_count = [0] * len(req.labels)
    remaining = [
        spec.count if fits_any[i] else 0 for i, spec in enumerate(req.labels)
    ]

    def new_page(first: bool = False) -> _Skyline:
        pages.append(
            PageLayout(
                page=len(pages) + 1,
                width=req.page_width,
                height=req.page_height,
                labels=[],
            )
        )
        return _Skyline(
            area_x,
            area_y,
            area_w,
            area_h,
            req.gap_x,
            req.gap_y,
            obstacles=first_page_obstacles if first else None,
        )

    def place(sky: _Skyline, i: int, rotated: bool, bw: float, bh: float) -> None:
        spec: LabelSpec = req.labels[i]
        x, y = sky.find_position(bw, bh)  # type: ignore[misc]
        sky.add(x, y, bw, bh)
        lw = spec.height if rotated else spec.width
        lh = spec.width if rotated else spec.height
        pages[-1].labels.append(
            PlacedLabel(
                type_index=i,
                name=spec.name,
                x=_r(x),
                y=_r(y),
                width=_r(lw),
                height=_r(lh),
                rotated=rotated,
            )
        )
        placed_count[i] += 1
        if rotated:
            rotated_count[i] += 1

    # 多趟扫描：每趟按用户输入顺序放置；当前页放不下的标签留给新页，
    # 后续较小的标签仍可回填本页空隙。没有任何标签能放下时续页。
    # 第一页带占用区障碍，后续页均为空白纸。
    sky = new_page(first=True)
    while sum(remaining) > 0:
        progressed = False
        for i, rem in enumerate(remaining):
            if rem <= 0:
                continue
            for rotated, bw, bh in candidate_boxes(i):
                if sky.find_position(bw, bh) is not None:
                    place(sky, i, rotated, bw, bh)
                    remaining[i] -= 1
                    progressed = True
                    break
        if not progressed:
            sky = new_page()

    # 流水号分配：跨页连续，按页码 → 页内从上到下、同排从左到右的
    # 实际排版位置分配。旋转、首页占用区避让、续页都只改变标签位置，
    # 不影响计数器，因此同一规格既不跳号也不重号；未排入的份数（remaining
    # 中未被放置的部分）从不参与计数，不消耗编号。
    counters = [
        spec.numbering.start if spec.numbering is not None else 0
        for spec in req.labels
    ]
    first_serial: list[str | None] = [None] * len(req.labels)
    last_serial: list[str | None] = [None] * len(req.labels)
    for pg in pages:
        for lab in _reading_order(pg.labels):
            i = lab.type_index
            num = req.labels[i].numbering
            if num is None:
                continue
            serial = format_serial(num.prefix, counters[i], num.pad)
            lab.serial = serial
            if first_serial[i] is None:
                first_serial[i] = serial
            last_serial[i] = serial
            counters[i] += 1

    summaries = [
        LabelSummary(
            type_index=i,
            name=spec.name,
            width=_r(spec.width),
            height=_r(spec.height),
            requested=spec.count,
            placed=placed_count[i],
            rotated=rotated_count[i],
            unplaced=spec.count - placed_count[i],
            numbered=spec.numbering is not None,
            number_first=first_serial[i],
            number_last=last_serial[i],
        )
        for i, spec in enumerate(req.labels)
    ]

    return LayoutResponse(
        pages=[
            PageLayout(
                page=p.page,
                width=_r(p.width),
                height=_r(p.height),
                labels=p.labels,
            )
            for p in pages
        ],
        summaries=summaries,
        total_requested=sum(s.count for s in req.labels),
        total_placed=sum(placed_count),
        warnings=warnings,
        page_width=_r(req.page_width),
        page_height=_r(req.page_height),
        margin_top=_r(req.margin_top),
        margin_right=_r(req.margin_right),
        margin_bottom=_r(req.margin_bottom),
        margin_left=_r(req.margin_left),
        gap_x=_r(req.gap_x),
        gap_y=_r(req.gap_y),
        occupied_zones=[
            OccupiedZone(
                x=_r(z.x), y=_r(z.y), width=_r(z.width), height=_r(z.height)
            )
            for z in req.occupied_zones
        ],
    )
