"""FastAPI 入口：拼版计算与 SVG 导出接口，以及静态前端。"""

from __future__ import annotations

from contextlib import asynccontextmanager
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
    RequisitionItem,
    RequisitionRequest,
    RequisitionResult,
    StockItem,
    StockRegisterRequest,
    SvgRequest,
)
from .stock import (
    StockConflict,
    confirm_requisition,
    init_db,
    list_requisitions,
    list_stock,
    register_stock,
)
from .svg import build_calibration_page, build_reprint_svg, build_svg, resolve_reprint_selection

_STATIC_DIR = Path(__file__).resolve().parent.parent / "static"


@asynccontextmanager
async def lifespan(_app: FastAPI):
    # 启动时确保 SQLite 库存 / 领料表存在（幂等）
    init_db()
    yield


app = FastAPI(title="小批量标签拼版工具", version="1.4.0", lifespan=lifespan)


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


# ---------------- 纸张库存与领料 ----------------


@app.post("/api/stock", response_model=StockItem, tags=["stock"])
def api_stock_register(req: StockRegisterRequest) -> StockItem:
    """按纸宽 × 纸高登记入库；同一纸型再次登记时余量累加。"""
    try:
        item = register_stock(req.width, req.height, req.quantity)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return StockItem(**item)


@app.get("/api/stock", response_model=list[StockItem], tags=["stock"])
def api_stock_list() -> list[StockItem]:
    """查看各纸型当前余量（张）。"""
    return [StockItem(**item) for item in list_stock()]


@app.post(
    "/api/requisitions/confirm",
    response_model=RequisitionResult,
    tags=["stock"],
)
def api_requisition_confirm(req: RequisitionRequest) -> RequisitionResult:
    """确认领料：服务端用当前参数重新计算拼版，全部标签排入且纸型匹配
    时，在单个 SQLite 事务内按实际页数核对 / 扣减库存并写入领料记录。

    - 同单号同参数重试：返回原记录（retried=true），不重复扣纸；
    - 同单号参数变化：HTTP 409 冲突；
    - 纸型未登记 / 余量不足 / 有标签未排入：HTTP 400，余量不足时
      错误信息包含所需页数与当前余量。
    """
    try:
        plan = compute_layout(req.params)
        if plan.total_placed != plan.total_requested:
            unplaced = plan.total_requested - plan.total_placed
            names = "、".join(
                f"「{s.name or s.type_index + 1}」缺 {s.unplaced} 份"
                for s in plan.summaries if s.unplaced > 0
            )
            raise ValueError(
                f"有 {unplaced} 个标签未能排入（{names}），不能领料："
                "请调整纸张尺寸、页边距、间距或标签份数后重试。"
            )
        result = confirm_requisition(
            req.order_no, req.params.model_dump(), plan
        )
    except StockConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return RequisitionResult(**result)


@app.get(
    "/api/requisitions",
    response_model=list[RequisitionItem],
    tags=["stock"],
)
def api_requisition_list(
    limit: int = Query(20, ge=1, le=200, description="返回最近 N 条"),
) -> list[RequisitionItem]:
    """近期领料记录（最新在前）。"""
    return [RequisitionItem(**item) for item in list_requisitions(limit)]


# 静态资源（index.html / app.js / style.css）
app.mount("/static", StaticFiles(directory=_STATIC_DIR), name="static")


@app.get("/", response_class=HTMLResponse, include_in_schema=False)
def index() -> HTMLResponse:
    with open(_STATIC_DIR / "index.html", encoding="utf-8") as f:
        return HTMLResponse(f.read())
