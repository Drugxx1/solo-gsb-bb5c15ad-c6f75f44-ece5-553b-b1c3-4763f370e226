"""FastAPI 入口：拼版计算与 SVG 导出接口，以及静态前端。"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse, Response
from fastapi.staticfiles import StaticFiles

from .calibration import (
    calibration_marks,
    ensure_calibrated_bounds,
    solve_calibration,
)
from .layout import compute_layout
from .models import (
    Calibration,
    CalibrationData,
    LayoutRequest,
    LayoutResponse,
    ReprintRequest,
    SvgRequest,
)
from .svg import build_calibration_page, build_reprint_svg, build_svg, resolve_reprint_selection

_STATIC_DIR = Path(__file__).resolve().parent.parent / "static"

app = FastAPI(title="小批量标签拼版工具", version="1.3.0")


@app.get("/health", tags=["meta"])
def health() -> dict:
    return {"status": "ok"}


@app.post("/api/layout", response_model=LayoutResponse, tags=["layout"])
def api_layout(req: LayoutRequest) -> LayoutResponse:
    try:
        return compute_layout(req)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/svg", tags=["layout"])
def api_svg(req: SvgRequest) -> Response:
    try:
        plan = compute_layout(req.params)
        calibration = None
        if req.calibration is not None:
            # 拟合 X/Y 线性走纸误差（重合、顺序颠倒在此报错），
            # 再确认逆变换补偿后本页标签没有被推到纸张之外
            calibration = solve_calibration(req.calibration)
            ensure_calibrated_bounds(plan, req.page, calibration)
        svg = build_svg(
            plan, req.page, req.show_margins, req.show_annotations,
            req.show_occupied, calibration=calibration,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    filename = f"labels-page-{req.page}.svg"
    return Response(
        content=svg,
        media_type="image/svg+xml; charset=utf-8",
        headers={"Content-Disposition": f'inline; filename="{filename}"'},
    )


@app.post("/api/reprint", tags=["layout"])
def api_reprint(req: ReprintRequest) -> Response:
    """原位补印：按页码 + 页内标签序号（从 1 开始）只导出选中标签。

    服务端仍按 params 计算完整原排版，再只绘制选中标签的轮廓、编号、
    标注与裁切角线；未选位置、页码标记与页边距线均留白。选中标签的
    坐标与流水号不重排、不重新编号；提供 calibration 时按逆变换补偿，
    与 /api/svg 的校准语义一致。选择为空或序号越界返回 HTTP 400。
    """
    try:
        plan = compute_layout(req.params)
        # 先校验选择（空 / 越界给出明确错误），再拟合校准
        only = resolve_reprint_selection(plan, req.page, req.indexes)
        calibration = None
        if req.calibration is not None:
            calibration = solve_calibration(req.calibration)
            # 补印只绘制选中标签，出界校验也仅针对选中标签
            ensure_calibrated_bounds(plan, req.page, calibration, only=only)
        svg = build_reprint_svg(
            plan, req.page, req.indexes, req.show_annotations,
            calibration=calibration,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    filename = f"reprint-page-{req.page}.svg"
    return Response(
        content=svg,
        media_type="image/svg+xml; charset=utf-8",
        headers={"Content-Disposition": f'inline; filename="{filename}"'},
    )


@app.get("/api/calibration/marks", tags=["calibration"])
def api_calibration_marks(
    page_width: float = Query(210, gt=0, description="纸张宽度 mm"),
    page_height: float = Query(297, gt=0, description="纸张高度 mm"),
) -> dict:
    """返回校准测试页横、纵两组基准刻线的设计坐标。"""
    x_marks, y_marks = calibration_marks(page_width, page_height)
    return {
        "page_width": page_width,
        "page_height": page_height,
        "x_marks": x_marks,
        "y_marks": y_marks,
    }


@app.get("/api/calibration/page", tags=["calibration"])
def api_calibration_page(
    page_width: float = Query(210, gt=0, description="纸张宽度 mm"),
    page_height: float = Query(297, gt=0, description="纸张高度 mm"),
) -> Response:
    """下载 / 打印校准测试页（含横、纵两组基准刻线及设计坐标标注）。"""
    svg = build_calibration_page(page_width, page_height)
    return Response(
        content=svg,
        media_type="image/svg+xml; charset=utf-8",
        headers={
            "Content-Disposition": 'inline; filename="calibration-page.svg"'
        },
    )


@app.post(
    "/api/calibration/solve", response_model=Calibration, tags=["calibration"]
)
def api_calibration_solve(req: CalibrationData) -> Calibration:
    """由基准刻线的设计/实测坐标分别拟合 X、Y 方向线性走纸误差。"""
    try:
        return solve_calibration(req)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


# 静态资源（index.html / app.js / style.css）
app.mount("/static", StaticFiles(directory=_STATIC_DIR), name="static")


@app.get("/", response_class=HTMLResponse, include_in_schema=False)
def index() -> HTMLResponse:
    with open(_STATIC_DIR / "index.html", encoding="utf-8") as f:
        return HTMLResponse(f.read())
