"""打印位置校准。

校准测试页（见 svg.build_calibration_page）上印有横、纵两组基准刻线，
每条刻线的设计坐标已知。用户以 100% 缩放打印测试页后，测量各刻线在
纸上的实际坐标，本模块据此分别拟合 X / Y 方向的线性走纸误差：

    实测坐标 = scale × 设计坐标 + offset

导出 SVG 时按其逆变换  设计坐标' = (设计坐标 - offset) / scale  修正
标签轮廓、裁切线与标注，打印机再走一遍误差后，内容恰好落回设计位置。

拟合前校验：基准点重合（无法确定线性关系）、实测顺序颠倒（实测值未随
设计坐标递增）；导出前校验补偿后所有标签仍在纸张范围内。
"""

from __future__ import annotations

from .models import (
    AxisCalibration,
    Calibration,
    CalibrationData,
    CalibrationLine,
    LayoutResponse,
)

EPS = 1e-6
# 逆变换后允许的标签出界容差（毫米），吸收坐标舍入
BOUNDS_TOL = 0.05
# 测试页每组基准刻线的数量
MARK_COUNT = 5


def calibration_marks(
    page_width: float, page_height: float
) -> tuple[list[float], list[float]]:
    """测试页基准刻线的设计坐标：纵、横各 MARK_COUNT 条，均布于 10%–90%。

    纵向刻线坐标沿 X 方向均布（用于校准 X），横向刻线坐标沿 Y 方向均布。
    坐标范围尽量拉开，以降低尺量误差对斜率的影响。
    """

    def axis_marks(length: float) -> list[float]:
        return [
            round(length * (0.1 + 0.8 * i / (MARK_COUNT - 1)), 2)
            for i in range(MARK_COUNT)
        ]

    return axis_marks(page_width), axis_marks(page_height)


def solve_axis(lines: list[CalibrationLine], axis: str) -> AxisCalibration:
    """对单方向基准刻线做最小二乘拟合：measured = scale·design + offset。

    阻止拟合的两类数据问题均抛出中文 ValueError：
    1. 基准点重合：设计坐标重复，或实测坐标全部相同，无法确定线性关系；
    2. 实测顺序颠倒：按设计坐标排序后实测值未严格递增（录入顺序与
       刻线编号不一致，如从右往左 / 从下往上读数）。
    """
    pts = sorted(lines, key=lambda l: l.design)
    designs = [p.design for p in pts]
    measureds = [p.measured for p in pts]

    # 设计坐标重复（排序后相邻）→ 基准点重合，斜率分母为零
    for i in range(1, len(pts)):
        if designs[i] - designs[i - 1] < EPS:
            raise ValueError(
                f"{axis} 方向基准点重合：第 {i}、{i + 1} 条刻线的设计坐标均为 "
                f"{round(designs[i], 2)}mm，无法求出线性走纸误差，请核对设计坐标录入。"
            )
    # 实测坐标全部相同 → 斜率必然为 0，逆变换不存在
    if max(measureds) - min(measureds) < EPS:
        raise ValueError(
            f"{axis} 方向基准点重合：所有刻线的实测坐标均为 "
            f"{round(measureds[0], 2)}mm，无法求出线性走纸误差，请核对测量值。"
        )
    # 实测值必须随设计坐标严格递增，否则就是顺序颠倒/错位录入
    for i in range(1, len(pts)):
        if measureds[i] - measureds[i - 1] < EPS:
            raise ValueError(
                f"{axis} 方向实测坐标顺序与设计顺序不一致（可能录入顺序颠倒）："
                f"设计 {round(designs[i - 1], 2)}mm 处实测 "
                f"{round(measureds[i - 1], 2)}mm，而设计 {round(designs[i], 2)}mm "
                f"处实测 {round(measureds[i], 2)}mm。实测值应随设计坐标递增，"
                "请按测试页刻线编号顺序录入。"
            )

    n = len(pts)
    sd = sum(designs)
    sm = sum(measureds)
    sdd = sum(d * d for d in designs)
    sdm = sum(d * m for d, m in zip(designs, measureds))
    denom = n * sdd - sd * sd  # 设计坐标严格递增 ⇒ denom > 0
    scale = (n * sdm - sd * sm) / denom
    offset = (sm - scale * sd) / n
    return AxisCalibration(scale=round(scale, 6), offset=round(offset, 4))


def solve_calibration(data: CalibrationData) -> Calibration:
    """分别拟合 X（纵向刻线）/ Y（横向刻线）两个方向的走纸误差。"""
    return Calibration(
        x=solve_axis(data.x_lines, "X"),
        y=solve_axis(data.y_lines, "Y"),
    )


def ensure_calibrated_bounds(
    plan: LayoutResponse, page_no: int, cal: Calibration,
    only: set[int] | None = None,
) -> None:
    """校验逆变换补偿后该页所有标签仍在纸张范围内。

    走纸误差（尤其是缩放）的逆变换会把内容整体放大/平移，补偿量过大会
    使标签落出纸张。出现任何出界标签即阻止校准导出，并给出具体标签与
    修正后坐标，便于用户核对测量数据。

    only 非空时（原位补印）只校验这些 0 基序号对应的标签——补印 SVG
    只绘制选中标签，未选位置不输出内容，无需参与出界判定。
    """
    page = next((p for p in plan.pages if p.page == page_no), None)
    if page is None:
        return  # 页码错误交给 build_svg 统一报错

    pw, ph = plan.page_width, plan.page_height
    xs, xo = cal.x.scale, cal.x.offset
    ys, yo = cal.y.scale, cal.y.offset
    for idx, lab in enumerate(page.labels):
        if only is not None and idx not in only:
            continue
        x0 = (lab.x - xo) / xs
        y0 = (lab.y - yo) / ys
        x1 = x0 + lab.width / xs
        y1 = y0 + lab.height / ys
        if (
            x0 < -BOUNDS_TOL
            or y0 < -BOUNDS_TOL
            or x1 > pw + BOUNDS_TOL
            or y1 > ph + BOUNDS_TOL
        ):
            name = lab.name or f"#{lab.type_index + 1}"
            raise ValueError(
                f"校准补偿后标签「{name}」超出纸张：修正后占据 "
                f"({x0:.2f}, {y0:.2f}) – ({x1:.2f}, {y1:.2f})mm，"
                f"超出 {_r(pw)}×{_r(ph)}mm 纸张范围。已阻止校准导出，"
                "请检查实测坐标是否准确。"
            )


def _r(v: float) -> float:
    return round(v + 0.0, 2)
