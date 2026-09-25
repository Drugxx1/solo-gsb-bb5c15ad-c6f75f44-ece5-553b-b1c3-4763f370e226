"""启动初始化：校验配置、自检关键模块可导入。

容器启动时由 entrypoint.sh 调用（python -m app.bootstrap）。
无数据库等外部依赖；这里执行运行环境自检并输出启动横幅。
"""

from __future__ import annotations

import sys


def main() -> int:
    print("[bootstrap] 小批量标签拼版工具 —— 初始化开始")

    # 依赖与核心模块自检
    try:
        import fastapi  # noqa: F401
        import uvicorn  # noqa: F401
        import pydantic  # noqa: F401
    except ImportError as exc:  # pragma: no cover
        print(f"[bootstrap] 依赖缺失: {exc}", file=sys.stderr)
        return 1

    from .calibration import (
        ensure_calibrated_bounds,
        solve_calibration,
    )
    from .layout import compute_layout
    from .models import (
        CalibrationData,
        CalibrationLine,
        LayoutRequest,
        LabelSpec,
        OccupiedZone,
    )
    from .svg import _fmt, build_calibration_page, build_svg

    import re

    def _cut_segments_of(svg_text: str):
        """解析全部 cutmarks 组，返回每段子线 (x0,y0,x1,y1)（轴对齐）。"""
        out = []
        for m in re.finditer(
            r'<g class="cutmarks"[^>]*><path d="([^"]*)"', svg_text
        ):
            for x, y, axis, end in re.findall(
                r"M(-?[\d.]+) (-?[\d.]+) ([VH])(-?[\d.]+)", m.group(1)
            ):
                x, y, end = float(x), float(y), float(end)
                if axis == "H":
                    out.append((min(x, end), y, max(x, end), y))
                else:
                    out.append((x, min(y, end), x, max(y, end)))
        return out

    # 裁切角线避让：20×20mm 标签、0 间距紧贴、40mm 纸宽下两列铺满，
    # 角线既不能探入邻签，也不能画到纸外
    tight = LayoutRequest(
        page_width=40, page_height=60,
        gap_x=0, gap_y=0,
        labels=[LabelSpec(width=20, height=20, count=6)],
    )
    tight_plan = compute_layout(tight)
    assert len(tight_plan.pages[0].labels) == 6
    assert tight_plan.pages[0].labels[0].x == 0
    tight_svg = build_svg(tight_plan, 1, show_annotations=False)
    rects = [(l.x, l.y, l.x + l.width, l.y + l.height)
             for l in tight_plan.pages[0].labels]
    for sx0, sy0, sx1, sy1 in _cut_segments_of(tight_svg):
        assert sx0 >= 0 and sy0 >= 0 and sx1 <= 40 and sy1 <= 60, (
            f"角线超出纸边: {(sx0, sy0, sx1, sy1)}"
        )
        for rx0, ry0, rx1, ry1 in rects:
            # 与任何标签轮廓内部不得相交（共边 / 端点接触允许）
            assert not (
                sx0 < rx1 - 1e-6 and sx1 > rx0 + 1e-6
                and sy0 < ry1 - 1e-6 and sy1 > ry0 + 1e-6
            ), f"角线侵入标签 ({rx0},{ry0},{rx1},{ry1}): {(sx0, sy0, sx1, sy1)}"

    # 20×20 标签铺满 20×20 整张纸：角线全部在纸外，cutmarks 组不输出
    one = LayoutRequest(
        page_width=20, page_height=20,
        labels=[LabelSpec(width=20, height=20, count=1)],
    )
    assert 'class="cutmarks"' not in build_svg(compute_layout(one), 1)

    # 开阔纸面上的单个标签，四角 8 段齐全
    free = LayoutRequest(
        page_width=100, page_height=100,
        margin_top=30, margin_left=30,
        labels=[LabelSpec(width=20, height=20, count=1)],
    )
    assert len(_cut_segments_of(build_svg(compute_layout(free), 1))) == 8

    # 首页占用区避让：导出即使不画占用区，角线也不得进入占用区矩形
    occ_avoid = LayoutRequest(
        page_width=120, page_height=120,
        labels=[LabelSpec(width=20, height=20, count=1)],
        occupied_zones=[OccupiedZone(x=30, y=0, width=40, height=30)],
    )
    occ_plan = compute_layout(occ_avoid)
    z = occ_plan.occupied_zones[0]
    export_no_zone = build_svg(occ_plan, 1)  # show_occupied=False
    assert 'class="occupied"' not in export_no_zone
    for sx0, sy0, sx1, sy1 in _cut_segments_of(export_no_zone):
        assert not (
            sx0 < z.x + z.width - 1e-6 and sx1 > z.x + 1e-6
            and sy0 < z.y + z.height - 1e-6 and sy1 > z.y + 1e-6
        ), f"角线侵入首页占用区: {(sx0, sy0, sx1, sy1)}"

    # 用一个最小样例冒烟测试排版 + SVG 管线
    smoke = LayoutRequest(
        page_width=210, page_height=297,
        margin_top=8, margin_right=8, margin_bottom=8, margin_left=8,
        gap_x=2, gap_y=2,
        labels=[LabelSpec(width=60, height=30, count=4, allow_rotate=True)],
    )
    plan = compute_layout(smoke)
    build_svg(plan, 1)
    assert plan.total_placed == 4

    # 首张纸占用区：障碍位于 (48,48)、100×100，间距 2。
    # 标签盒为 52×32，障碍碰撞盒右缘 150、下沿 150。
    occ = LayoutRequest(
        page_width=210, page_height=297,
        margin_top=8, margin_right=8, margin_bottom=8, margin_left=8,
        gap_x=2, gap_y=2,
        labels=[LabelSpec(name="避让标签", width=50, height=30, count=40)],
        occupied_zones=[OccupiedZone(x=48, y=48, width=100, height=100)],
    )
    p2 = compute_layout(occ)
    p2_svg = build_svg(p2, 1, show_occupied=True)
    export_svg = build_svg(p2, 1)  # 导出不含占用区绘制
    assert 'class="occupied"' in p2_svg and "已占用" in p2_svg
    assert 'class="occupied"' not in export_svg and "已占用" not in export_svg
    # 不丢失、不重复
    assert sum(len(pg.labels) for pg in p2.pages) == 40
    assert p2.total_placed == 40
    assert len(p2.pages) >= 2  # 障碍占掉首页空间，必然续页
    z = occ.occupied_zones[0]
    for lab in p2.pages[0].labels:
        # 盒 = 标签矩形 + 右/下一个间距；盒与障碍碰撞盒不得重叠
        bx2, by2 = lab.x + lab.width + 2, lab.y + lab.height + 2
        assert not (
            lab.x < z.x + z.width + 2 and bx2 > z.x
            and lab.y < z.y + z.height + 2 and by2 > z.y
        ), f"标签与占用区间距不足: {lab}"
    # 续页为无障碍空白页，正常排满
    assert len(p2.pages[1].labels) > 0

    # 越界占用区必须报明确错误
    def _expect_error(z: OccupiedZone) -> None:
        bad = LayoutRequest(
            page_width=210, page_height=297,
            labels=[LabelSpec(width=10, height=10, count=1)],
            occupied_zones=[z],
        )
        try:
            compute_layout(bad)
        except ValueError:
            return
        raise AssertionError(f"占用区 {z} 越界却未报错")

    _expect_error(OccupiedZone(x=120, y=10, width=100, height=10))
    _expect_error(OccupiedZone(x=10, y=250, width=10, height=60))
    _expect_error(OccupiedZone(x=-1, y=0, width=10, height=10))

    # ---- 流水号：阅读顺序、跨页连续、旋转/占用区不跳号、未排入不消耗 ----
    from .layout import format_serial
    from .models import Numbering

    assert format_serial("P-", 1, 3) == "P-001"
    assert format_serial("", 42, 0) == "42"
    assert format_serial("X", 100, 2) == "X100"  # 位数超出补零位不截断
    assert format_serial("X", 5, 4) == "X0005"

    def _num(prefix="P-", start=1, pad=3) -> Numbering:
        return Numbering(prefix=prefix, start=start, pad=pad)

    # 2×2 网格：编号按 页内从上到下成行、行内从左到右 分配
    grid = LayoutRequest(
        page_width=100, page_height=100,
        labels=[LabelSpec(width=50, height=50, count=4, numbering=_num())],
    )
    gp = compute_layout(grid)
    g0 = gp.pages[0].labels
    assert [(l.x, l.y, l.serial) for l in g0] == [
        (0, 0, "P-001"), (50, 0, "P-002"),
        (0, 50, "P-003"), (50, 50, "P-004"),
    ], [(l.x, l.y, l.serial) for l in g0]
    gs = gp.summaries[0]
    assert gs.numbered and gs.number_first == "P-001" and gs.number_last == "P-004"

    # 跨页连续：40×100 纸面 20×20 标签，首页 10 个，续页 2 个
    paged = LayoutRequest(
        page_width=40, page_height=100,
        labels=[LabelSpec(width=20, height=20, count=12, numbering=_num())],
    )
    pp = compute_layout(paged)
    assert len(pp.pages) == 2
    all_serials = [
        l.serial for pg in pp.pages for l in
        sorted(pg.labels, key=lambda l: (l.y, l.x))
    ]
    assert all_serials == [f"P-{i:03d}" for i in range(1, 13)], all_serials
    assert pp.summaries[0].number_last == "P-012"

    # 多规格各自独立计数（同一版面内交错放置，编号互不干扰）
    two = LayoutRequest(
        page_width=40, page_height=40,
        labels=[
            LabelSpec(width=20, height=20, count=2,
                      numbering=_num("A-", 1, 3)),
            LabelSpec(width=20, height=20, count=2,
                      numbering=_num("B-", 1, 3)),
        ],
    )
    tp = compute_layout(two)
    by_pos = {(l.x, l.y): l.serial for l in tp.pages[0].labels}
    assert by_pos == {
        (0, 0): "A-001", (20, 0): "B-001",
        (0, 20): "A-002", (20, 20): "B-002",
    }, by_pos

    # 未启用编号：serial 为空，汇总保持原字段
    plain = LayoutRequest(
        page_width=40, page_height=40,
        labels=[LabelSpec(width=20, height=20, count=1)],
    )
    plain_plan = compute_layout(plain)
    assert plain_plan.pages[0].labels[0].serial is None
    assert plain_plan.summaries[0].numbered is False
    assert plain_plan.summaries[0].number_first is None

    # 完全排不下（份数未排入）：不消耗任何编号
    unfit = LayoutRequest(
        page_width=100, page_height=100,
        labels=[
            LabelSpec(name="巨标", width=200, height=200, count=3,
                      numbering=_num())
        ],
    )
    up = compute_layout(unfit)
    assert up.summaries[0].placed == 0
    assert up.summaries[0].number_first is None
    assert up.summaries[0].number_last is None
    assert all(l.serial is None for pg in up.pages for l in pg.labels)

    # 首页占用区 + 续页：40 份编号恰好 001–040 各一次，不跳号不重号
    occ_num = LayoutRequest(
        page_width=210, page_height=297,
        margin_top=8, margin_right=8, margin_bottom=8, margin_left=8,
        gap_x=2, gap_y=2,
        labels=[LabelSpec(name="编号避让", width=50, height=30, count=40,
                          numbering=_num())],
        occupied_zones=[OccupiedZone(x=48, y=48, width=100, height=100)],
    )
    onp = compute_layout(occ_num)
    occ_serials = [l.serial for pg in onp.pages for l in pg.labels]
    assert sorted(occ_serials) == [f"P-{i:03d}" for i in range(1, 41)]
    assert len(occ_serials) == len(set(occ_serials)) == 40

    # 旋转排版下编号仍按实际位置连续分配
    rot = LayoutRequest(
        page_width=30, page_height=120,
        labels=[LabelSpec(name="转标", width=50, height=20, count=6,
                          allow_rotate=True, numbering=_num())],
    )
    rp = compute_layout(rot)
    rot_serials = [l.serial for pg in rp.pages for l in pg.labels]
    assert sorted(rot_serials) == [f"P-{i:03d}" for i in range(1, 7)]
    assert any(l.rotated for pg in rp.pages for l in pg.labels)

    # SVG 内显示同一编号（预览与导出同源）；未编号标签无 serial 文本
    grid_svg = build_svg(gp, 1, show_occupied=False)
    for s in ("P-001", "P-002", "P-003", "P-004"):
        assert f">{s}</text>" in grid_svg, f"SVG 缺少编号 {s}"
    assert 'class="serial"' in grid_svg
    plain_svg = build_svg(plain_plan, 1)
    assert 'class="serial"' not in plain_svg

    # 编号放不进标签：最小字号仍越界时明确报错，不输出 SVG
    too_long = LayoutRequest(
        page_width=100, page_height=100,
        labels=[
            LabelSpec(name="小签", width=10, height=10, count=1,
                      numbering=_num("ABCDEFGH-", 1, 4))
        ],
    )
    try:
        build_svg(compute_layout(too_long), 1)
    except ValueError as exc:
        assert "放不进标签" in str(exc), str(exc)
    else:
        raise AssertionError("编号越界却未报错")

    # 小标签放短编号正常；校准导出路径同样包含编号文本
    short_num = LayoutRequest(
        page_width=100, page_height=100,
        labels=[LabelSpec(width=10, height=10, count=1,
                          numbering=_num("", 7, 2))],
    )
    short_plan = compute_layout(short_num)
    assert ">07</text>" in build_svg(short_plan, 1, show_annotations=False)

    # ---- 打印位置校准：拟合、逆变换修正与三类拦截 ----
    def _lines(scale: float, offset: float) -> list[CalibrationLine]:
        return [
            CalibrationLine(design=d, measured=round(scale * d + offset, 3))
            for d in (21.0, 63.0, 105.0, 147.0, 189.0)
        ]

    cal_data = CalibrationData(
        x_lines=_lines(1.01, -0.3), y_lines=_lines(0.99, 0.5)
    )
    cal = solve_calibration(cal_data)
    assert abs(cal.x.scale - 1.01) < 5e-4 and abs(cal.x.offset + 0.3) < 1e-2
    assert abs(cal.y.scale - 0.99) < 5e-4 and abs(cal.y.offset - 0.5) < 1e-2

    # 校准导出：标签坐标按逆变换 (设计 - offset) / scale 修正
    lab0 = plan.pages[0].labels[0]
    svg_cal = build_svg(plan, 1, calibration=cal)
    exp_x = _fmt((lab0.x - cal.x.offset) / cal.x.scale)
    exp_w = _fmt(lab0.width / cal.x.scale)
    assert f'x="{exp_x}"' in svg_cal and f'width="{exp_w}"' in svg_cal
    assert "打印位置校准已应用" in svg_cal
    # 未校准导出仍是原坐标，且不含校准注释
    svg_plain = build_svg(plan, 1)
    assert f'x="{_fmt(lab0.x)}"' in svg_plain
    assert "打印位置校准已应用" not in svg_plain
    # 无校准参数的导出与新增功能前保持一致（同一渲染管线恒等分支）
    assert build_svg(plan, 1, True, True, False) == build_svg(
        plan, 1, True, True, False, calibration=None
    )

    # 启用流水号时校准导出同样携带编号（编号字号不随校准缩放）
    assert ">07</text>" in build_svg(short_plan, 1, calibration=cal)
    assert 'class="serial"' in build_svg(short_plan, 1, calibration=cal)

    # 基准点重合：设计坐标重复 / 实测坐标全部相同，均拒绝
    def _expect_cal_error(data: CalibrationData, key: str) -> None:
        try:
            solve_calibration(data)
        except ValueError as exc:
            assert key in str(exc), f"错误信息未包含「{key}」: {exc}"
            return
        raise AssertionError(f"校准数据应被拦截（{key}）却通过求解")

    same_design = [
        CalibrationLine(design=100.0, measured=100.0 + i) for i in range(3)
    ]
    _expect_cal_error(
        CalibrationData(x_lines=same_design, y_lines=_lines(1.0, 0.0)),
        "基准点重合",
    )
    same_measured = [
        CalibrationLine(design=float(d), measured=50.0)
        for d in (21.0, 63.0, 105.0)
    ]
    _expect_cal_error(
        CalibrationData(x_lines=same_measured, y_lines=_lines(1.0, 0.0)),
        "基准点重合",
    )
    # 实测顺序颠倒（从右往左读数），拒绝
    reversed_rows = [
        CalibrationLine(design=21.0, measured=190.0),
        CalibrationLine(design=63.0, measured=148.0),
        CalibrationLine(design=105.0, measured=105.0),
        CalibrationLine(design=147.0, measured=62.0),
        CalibrationLine(design=189.0, measured=20.0),
    ]
    _expect_cal_error(
        CalibrationData(x_lines=reversed_rows, y_lines=_lines(1.0, 0.0)),
        "顺序",
    )
    # 补偿后内容超出纸张：offset=50 使修正后内容整体左移 50mm
    cal_shift = solve_calibration(
        CalibrationData(x_lines=_lines(1.0, 50.0), y_lines=_lines(1.0, 0.0))
    )
    try:
        ensure_calibrated_bounds(plan, 1, cal_shift)
    except ValueError as exc:
        assert "超出纸张" in str(exc), str(exc)
    else:
        raise AssertionError("补偿后标签出界却未拦截校准导出")

    # 校准测试页：横竖两组刻线及设计坐标标注齐全
    page_svg = build_calibration_page(210, 297)
    assert 'class="vline"' in page_svg and 'class="hline"' in page_svg
    assert "X1=21" in page_svg and "X5=189" in page_svg
    assert "Y1=29.7" in page_svg and "Y5=267.3" in page_svg

    # 校准导出的角线避让按补偿后坐标判定：纸边映射为 (0-offset)/scale，
    # 邻签矩形也整体逆变换；角线残段必须落在补偿后的纸内且不进补偿后邻签
    tight_cal_svg = build_svg(
        tight_plan, 1, show_annotations=False, calibration=cal
    )

    def _inv(v: float, scale: float, offset: float) -> float:
        return (v - offset) / scale

    cal_paper = (
        _inv(0, cal.x.scale, cal.x.offset),
        _inv(0, cal.y.scale, cal.y.offset),
        _inv(40, cal.x.scale, cal.x.offset),
        _inv(60, cal.y.scale, cal.y.offset),
    )
    cal_rects = [
        (
            _inv(l.x, cal.x.scale, cal.x.offset),
            _inv(l.y, cal.y.scale, cal.y.offset),
            _inv(l.x + l.width, cal.x.scale, cal.x.offset),
            _inv(l.y + l.height, cal.y.scale, cal.y.offset),
        )
        for l in tight_plan.pages[0].labels
    ]
    for sx0, sy0, sx1, sy1 in _cut_segments_of(tight_cal_svg):
        assert sx0 >= cal_paper[0] - 1e-6 and sy0 >= cal_paper[1] - 1e-6, (
            f"校准角线越过补偿后纸边: {(sx0, sy0, sx1, sy1)} / {cal_paper}"
        )
        assert sx1 <= cal_paper[2] + 1e-6 and sy1 <= cal_paper[3] + 1e-6
        for rx0, ry0, rx1, ry1 in cal_rects:
            assert not (
                sx0 < rx1 - 1e-6 and sx1 > rx0 + 1e-6
                and sy0 < ry1 - 1e-6 and sy1 > ry0 + 1e-6
            ), f"校准角线侵入补偿后邻签: {(sx0, sy0, sx1, sy1)} / {(rx0, ry0, rx1, ry1)}"

    # 校准导出的角线同样避让补偿后的首页占用区
    occ_cal_svg = build_svg(occ_plan, 1, calibration=cal)
    zc = (
        _inv(z.x, cal.x.scale, cal.x.offset),
        _inv(z.y, cal.y.scale, cal.y.offset),
        _inv(z.x + z.width, cal.x.scale, cal.x.offset),
        _inv(z.y + z.height, cal.y.scale, cal.y.offset),
    )
    for sx0, sy0, sx1, sy1 in _cut_segments_of(occ_cal_svg):
        assert not (
            sx0 < zc[2] - 1e-6 and sx1 > zc[0] + 1e-6
            and sy0 < zc[3] - 1e-6 and sy1 > zc[1] + 1e-6
        ), f"校准角线侵入补偿后占用区: {(sx0, sy0, sx1, sy1)} / {zc}"

    # ---- 原位补印：完整排版不变，仅绘制选中标签；空 / 越界给明确错误 ----
    from .svg import build_reprint_svg, resolve_reprint_selection

    # gp：100×100 纸面 50×50 标签 4 个（0 间距），编号 P-001..P-004，
    # 位置依次为 (0,0)、(50,0)、(0,50)、(50,50)
    only = resolve_reprint_selection(gp, 1, [2, 4, 2])  # 重复序号去重
    assert only == {1, 3}
    rp_svg = build_reprint_svg(gp, 1, [2, 4])
    full_svg = build_svg(gp, 1, show_occupied=False)

    # 只绘制 2 个标签轮廓；轮廓元素必须与整页导出逐字一致（坐标不重排）
    rp_rects = re.findall(r'<rect class="label"[^/]*/>', rp_svg)
    assert len(rp_rects) == 2
    for rect in rp_rects:
        assert rect in full_svg, f"补印轮廓与原排版不一致：{rect}"
    # 选中标签的流水号原样保留，未选标签的编号不出现（不重新编号）
    assert ">P-002</text>" in rp_svg and ">P-004</text>" in rp_svg
    assert "P-001" not in rp_svg and "P-003" not in rp_svg
    # 页码标记、页边距线与占用区留白
    assert 'class="pageno"' not in rp_svg and "Page" not in rp_svg
    assert 'class="marginguide"' not in rp_svg
    assert 'class="occupied"' not in rp_svg
    assert "原位补印" in rp_svg

    # 带边距 / 间距的版面（补印序号 2、4）：
    # 40×20 标签位于 (10,10)、(54,10)、(10,34)、(54,34)
    rp_req = LayoutRequest(
        page_width=120, page_height=120,
        margin_top=10, margin_right=10, margin_bottom=10, margin_left=10,
        gap_x=4, gap_y=4,
        labels=[LabelSpec(width=40, height=20, count=4, numbering=_num())],
    )
    rp_plan = compute_layout(rp_req)
    assert [(l.x, l.y) for l in rp_plan.pages[0].labels] == [
        (10, 10), (54, 10), (10, 34), (54, 34)
    ]
    rp2_svg = build_reprint_svg(rp_plan, 1, [2, 4])
    unselected_rects = [
        (l.x, l.y, l.x + l.width, l.y + l.height)
        for k, l in enumerate(rp_plan.pages[0].labels) if k in (0, 2)
    ]
    rp_segments = _cut_segments_of(rp2_svg)
    assert rp_segments, "补印标签的裁切角线缺失"
    for sx0, sy0, sx1, sy1 in rp_segments:
        assert 0 - 1e-6 <= sx0 and sx1 <= 120 + 1e-6, "补印角线画出纸外"
        assert 0 - 1e-6 <= sy0 and sy1 <= 120 + 1e-6
        for rx0, ry0, rx1, ry1 in unselected_rects:
            assert not (
                sx0 < rx1 - 1e-6 and sx1 > rx0 + 1e-6
                and sy0 < ry1 - 1e-6 and sy1 > ry0 + 1e-6
            ), f"补印角线侵入未选标签位置: {(sx0, sy0, sx1, sy1)} / {(rx0, ry0, rx1, ry1)}"
    assert 'x="54" y="10"' in rp2_svg and 'x="54" y="34"' in rp2_svg
    assert ">P-002</text>" in rp2_svg and ">P-004</text>" in rp2_svg
    assert "P-001" not in rp2_svg and "P-003" not in rp2_svg

    # 空选择 / 序号越界 / 页码越界均返回明确中文错误
    def _expect_reprint_error(page_no: int, indexes: list[int], key: str) -> None:
        try:
            build_reprint_svg(gp, page_no, indexes)
        except ValueError as exc:
            assert key in str(exc), f"错误信息未包含「{key}」: {exc}"
            return
        raise AssertionError(f"补印选择 {indexes} 应被拦截（{key}）却生成了 SVG")

    _expect_reprint_error(1, [], "为空")
    _expect_reprint_error(1, [5], "超出范围")
    _expect_reprint_error(1, [0], "超出范围")
    _expect_reprint_error(9, [1], "页码超出范围")

    # 补印沿用打印校准补偿：坐标按同一逆变换修正，编号仍在
    rp_cal = build_reprint_svg(gp, 1, [2], calibration=cal)
    assert "打印位置校准已应用" in rp_cal and "原位补印" in rp_cal
    assert f'x="{_fmt((50 - cal.x.offset) / cal.x.scale)}"' in rp_cal
    assert f'width="{_fmt(50 / cal.x.scale)}"' in rp_cal
    assert ">P-002</text>" in rp_cal
    assert "P-001" not in rp_cal  # 未选标签补偿后仍留白

    # 校准出界校验只针对选中标签：Y offset=-30 使下方标签（y=50）出界，
    # 整页导出被拦；只补印顶部标签（0 基序号 0）时通过
    cal_down = solve_calibration(
        CalibrationData(x_lines=_lines(1.0, 0.0), y_lines=_lines(1.0, -30.0))
    )
    try:
        ensure_calibrated_bounds(gp, 1, cal_down)
    except ValueError as exc:
        assert "超出纸张" in str(exc)
    else:
        raise AssertionError("补偿后未选标签出界，整页校验应失败")
    ensure_calibrated_bounds(gp, 1, cal_down, only={0})  # 顶部标签不越界

    # ---- 纸张库存与领料：登记、事务扣纸、幂等重试、冲突与库存不足 ----
    from .db import connect as db_connect, create_schema
    from .stock import (
        StockConflict,
        confirm_requisition,
        list_requisitions,
        list_stock,
        register_stock,
    )

    test_conn = db_connect(":memory:")
    create_schema(test_conn)

    # 40×40 纸面排 20×20 标签，每页 4 个；10 个标签恰好 3 页
    req_stock = LayoutRequest(
        page_width=40, page_height=40,
        labels=[LabelSpec(width=20, height=20, count=10)],
    )
    assert len(compute_layout(req_stock).pages) == 3

    # 纸型未登记：409 式冲突，提示所需页数与余量 0
    try:
        confirm_requisition(test_conn, "LL-001", req_stock)
    except StockConflict as exc:
        assert "需要 3 张" in str(exc) and "余量 0 张" in str(exc), str(exc)
    else:
        raise AssertionError("未登记库存却允许领料")

    # 登记 5 张（重复登记累加）
    item = register_stock(test_conn, 40, 40, 3)
    assert item.quantity == 3
    item = register_stock(test_conn, 40, 40, 2)
    assert item.quantity == 5
    assert {(s.paper_width, s.paper_height, s.quantity) for s in list_stock(test_conn)} \
        == {(40.0, 40.0, 5)}

    # 库存不足：需要 3 张时余量 5 张其实足够；先验不足分支——
    # 另一纸型 30×30 同标签每页 1 个，10 个需 10 页，未登记 → 0 张
    req_other = LayoutRequest(
        page_width=30, page_height=30,
        labels=[LabelSpec(width=20, height=20, count=10)],
    )
    try:
        confirm_requisition(test_conn, "LL-002", req_other)
    except StockConflict as exc:
        assert "需要 10 张" in str(exc) and "余量 0 张" in str(exc), str(exc)
    else:
        raise AssertionError("纸型不匹配却允许领料")
    # 冲突回滚后仍只有一条库存、余量不变
    assert list_stock(test_conn)[0].quantity == 5
    assert list_requisitions(test_conn) == []

    # 正常确认：按实际页数 3 扣纸，余量 5 → 2，并写入领料记录
    res = confirm_requisition(test_conn, "LL-001", req_stock)
    assert res["reused"] is False
    assert res["pages_required"] == 3
    assert res["labels_requested"] == 10 and res["labels_placed"] == 10
    assert res["stock_before"] == 5 and res["stock_after"] == 2
    assert list_stock(test_conn)[0].quantity == 2

    # 同单号 + 同参数重试：返回原记录（reused=True），不重复扣纸
    again = confirm_requisition(test_conn, "LL-001", req_stock)
    assert again["reused"] is True
    assert again["created_at"] == res["created_at"]
    assert again["stock_before"] == 5 and again["stock_after"] == 2
    assert list_stock(test_conn)[0].quantity == 2
    assert len(list_requisitions(test_conn)) == 1

    # 同单号但参数变化（份数 10 → 6）：冲突，不扣纸
    req_changed = LayoutRequest(
        page_width=40, page_height=40,
        labels=[LabelSpec(width=20, height=20, count=6)],
    )
    try:
        confirm_requisition(test_conn, "LL-001", req_changed)
    except StockConflict as exc:
        assert "已存在但参数与本次不同" in str(exc), str(exc)
    else:
        raise AssertionError("同单号参数变化应报冲突")
    assert list_stock(test_conn)[0].quantity == 2
    assert len(list_requisitions(test_conn)) == 1

    # 库存不足：余量 2，新单需要 3 张 → 提示所需页数、余量与差额
    try:
        confirm_requisition(test_conn, "LL-003", req_stock)
    except StockConflict as exc:
        msg = str(exc)
        assert "需要 3 张" in msg and "余量仅 2 张" in msg and "还差 1 张" in msg, msg
    else:
        raise AssertionError("库存不足却允许领料")
    assert list_stock(test_conn)[0].quantity == 2
    assert len(list_requisitions(test_conn)) == 1

    # 标签未全部排入（巨标）：不扣纸、不写记录
    req_unfit = LayoutRequest(
        page_width=40, page_height=40,
        labels=[
            LabelSpec(width=20, height=20, count=1),
            LabelSpec(name="巨标", width=80, height=80, count=2),
        ],
    )
    try:
        confirm_requisition(test_conn, "LL-004", req_unfit)
    except StockConflict as exc:
        assert "未能排入" in str(exc), str(exc)
    else:
        raise AssertionError("存在未排入标签却允许领料")
    assert list_stock(test_conn)[0].quantity == 2
    assert len(list_requisitions(test_conn)) == 1

    # 余量恰好足够：补登记 1 张后新单扣 3 张，余量归零
    register_stock(test_conn, 40, 40, 1)
    res2 = confirm_requisition(test_conn, "LL-005", req_stock)
    assert res2["stock_before"] == 3 and res2["stock_after"] == 0
    records = list_requisitions(test_conn)
    assert [r["order_no"] for r in records] == ["LL-005", "LL-001"]

    # 纸宽 / 纸高按 2 位小数归一：40.004 与 40.0 视为同一纸型
    fuzzy = register_stock(test_conn, 40.004, 40.0, 1)
    assert fuzzy.paper_width == 40.0 and fuzzy.quantity == 1

    test_conn.close()


    print(f"[bootstrap] 自检通过（FastAPI {fastapi.__version__}, "
          f"Pydantic {pydantic.VERSION}）")
    print("[bootstrap] 初始化完成，等待 uvicorn 启动 ...")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
