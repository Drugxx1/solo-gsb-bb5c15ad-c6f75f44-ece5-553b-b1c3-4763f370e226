"""请求 / 响应数据模型（单位均为毫米）。"""

from __future__ import annotations

from pydantic import BaseModel, Field


class Numbering(BaseModel):
    """一种标签的可选流水号设置。

    排版完成后按页码、页内从上到下再从左到右的实际位置为该规格的
    每个已排入标签分配连续整数：first + 0、first + 1……
    标签内部显示 prefix + str(整数).zfill(pad)；未排入的份数不消耗编号。
    """

    prefix: str = Field("", max_length=20, description="流水号前缀")
    start: int = Field(1, ge=0, description="起始整数（含），不能为负")
    pad: int = Field(0, ge=0, le=12, description="数字部分补零位数；0 表示不补零")


class LabelSpec(BaseModel):
    """一种标签规格及其需要的份数。"""

    name: str = Field("", description="标签名称")
    width: float = Field(..., gt=0, description="标签宽度 mm")
    height: float = Field(..., gt=0, description="标签高度 mm")
    count: int = Field(..., ge=1, description="需要份数")
    allow_rotate: bool = Field(False, description="是否允许旋转 90 度排版")
    numbering: Numbering | None = Field(
        None, description="流水号设置；为 null 时不编号，保持原有标注"
    )


class OccupiedZone(BaseModel):
    """首张纸上已被占用的区域（如已贴过的标签），单位毫米。

    坐标原点为纸张左上角。排版时视为障碍，新标签与其保持裁切间距；
    仅作用于第一页，放不下的份数续到后续空白页。
    """

    x: float = Field(..., description="左上角横坐标 mm")
    y: float = Field(..., description="左上角纵坐标 mm")
    width: float = Field(..., gt=0, description="占用区宽度 mm")
    height: float = Field(..., gt=0, description="占用区高度 mm")


class LayoutRequest(BaseModel):
    """拼版参数。"""

    page_width: float = Field(..., gt=0, description="纸张宽度 mm")
    page_height: float = Field(..., gt=0, description="纸张高度 mm")
    margin_top: float = Field(0, ge=0)
    margin_right: float = Field(0, ge=0)
    margin_bottom: float = Field(0, ge=0)
    margin_left: float = Field(0, ge=0)
    gap_x: float = Field(0, ge=0, description="水平裁切间距 mm")
    gap_y: float = Field(0, ge=0, description="垂直裁切间距 mm")
    labels: list[LabelSpec] = Field(..., min_length=1)
    occupied_zones: list[OccupiedZone] = Field(
        default_factory=list, description="首张纸占用区（障碍），可多个"
    )


class PlacedLabel(BaseModel):
    """单个已排入标签（毫米坐标，原点为纸张左上角）。

    serial 仅对启用了流水号的规格非空，值为含前缀、补零后的最终编号；
    同一规格跨页连续，旋转、首页占用区与续页都不会跳号或重号。
    """

    type_index: int
    name: str
    x: float
    y: float
    width: float
    height: float
    rotated: bool
    serial: str | None = None


class PageLayout(BaseModel):
    page: int
    width: float
    height: float
    labels: list[PlacedLabel]


class LabelSummary(BaseModel):
    type_index: int
    name: str
    width: float
    height: float
    requested: int
    placed: int
    rotated: int
    unplaced: int
    numbered: bool = False
    number_first: str | None = Field(
        None, description="启用流水号时本次实际排入的首个编号（未排入为 null）"
    )
    number_last: str | None = Field(
        None, description="启用流水号时本次实际排入的末个编号（未排入为 null）"
    )


class LayoutResponse(BaseModel):
    pages: list[PageLayout]
    summaries: list[LabelSummary]
    total_requested: int
    total_placed: int
    warnings: list[str]
    # 回显参数，供绘制 / 导出使用，保证预览与导出同源
    page_width: float
    page_height: float
    margin_top: float
    margin_right: float
    margin_bottom: float
    margin_left: float
    gap_x: float
    gap_y: float
    occupied_zones: list[OccupiedZone] = Field(
        default_factory=list, description="回显首张纸占用区"
    )


class CalibrationLine(BaseModel):
    """一条基准刻线：设计坐标与纸上实测坐标（毫米）。"""

    design: float = Field(..., description="刻线的设计坐标 mm")
    measured: float = Field(..., description="刻线在纸上的实测坐标 mm")


class CalibrationData(BaseModel):
    """打印位置校准录入：横、纵两组基准刻线，各至少 2 条。

    x_lines 为纵向刻线（校准 X 方向走纸误差），
    y_lines 为横向刻线（校准 Y 方向走纸误差）。
    """

    x_lines: list[CalibrationLine] = Field(
        ..., min_length=2, description="纵向刻线（测 X），至少 2 条"
    )
    y_lines: list[CalibrationLine] = Field(
        ..., min_length=2, description="横向刻线（测 Y），至少 2 条"
    )


class AxisCalibration(BaseModel):
    """单方向线性走纸误差：实测坐标 = scale × 设计坐标 + offset。"""

    scale: float = Field(..., description="缩放系数（理想为 1）")
    offset: float = Field(..., description="平移偏移 mm（理想为 0）")


class Calibration(BaseModel):
    """X / Y 两个方向的线性走纸误差拟合结果。"""

    x: AxisCalibration
    y: AxisCalibration


class SvgRequest(BaseModel):
    """SVG 导出请求：参数与 /api/layout 完全一致，附带页码与显示开关。"""

    page: int = Field(1, ge=1)
    show_margins: bool = True
    show_annotations: bool = True
    show_occupied: bool = Field(
        False,
        description="是否绘制首张纸占用区（预览用；下载/打印应为 false，只含新标签）",
    )
    params: LayoutRequest
    calibration: CalibrationData | None = Field(
        None,
        description="打印位置校准数据；提供时按拟合误差的逆变换修正导出坐标"
        "（页面常规预览不要传，保持原样）",
    )


class SvgResponse(BaseModel):
    svg: str


class ReprintRequest(BaseModel):
    """原位补印导出请求：拼版参数与 /api/layout 完全一致，附带页码与
    页内标签序号（从 1 开始，即预览中标签角标显示的序号）。

    服务端仍按 params 计算完整原排版，再只绘制选中标签的轮廓、编号、
    标注与裁切角线；未选位置、页码标记与页边距线均留白。选中标签的
    坐标与流水号取自完整排版结果，不重排、不重新编号。
    """

    page: int = Field(..., ge=1, description="页码（从 1 开始）")
    indexes: list[int] = Field(
        default_factory=list,
        description="本页需补印的标签序号（从 1 开始）；为空或越界返回明确错误",
    )
    show_annotations: bool = Field(True, description="是否绘制标签标注")
    params: LayoutRequest
    calibration: CalibrationData | None = Field(
        None,
        description="打印位置校准数据；提供时按拟合误差的逆变换修正导出坐标"
        "（与 /api/svg 的校准语义一致）",
    )


# ---------------- 纸张库存与领料 ----------------


class StockRegisterRequest(BaseModel):
    """纸张库存登记：按纸宽 × 纸高（毫米）入库若干张。

    同一纸型重复登记会累加余量；纸宽 / 纸高统一保留 2 位小数作为纸型
    唯一标识，领料时纸张尺寸必须与之完全一致。
    """

    paper_width: float = Field(..., gt=0, description="纸张宽度 mm")
    paper_height: float = Field(..., gt=0, description="纸张高度 mm")
    quantity: int = Field(..., ge=1, description="本次入库张数（正整数，累加到余量）")


class StockItem(BaseModel):
    """一种纸型的当前库存余量。"""

    paper_width: float
    paper_height: float
    quantity: int = Field(..., description="当前余量（张）")
    updated_at: str | None = Field(None, description="最近入库时间")


class RequisitionConfirmRequest(BaseModel):
    """确认领料请求：领料单号 + 当前拼版参数。

    服务端收到后用 params **重新计算拼版**，仅在所有标签全部排入、
    且纸张宽高与库存纸型完全一致时，按实际页数扣减余量，并写入领料
    记录。同一 order_no 重试时：参数指纹一致则返回原记录不重复扣纸；
    参数变化返回 HTTP 409 冲突。
    """

    order_no: str = Field(
        ..., min_length=1, max_length=40, description="领料单号（业务幂等键）"
    )
    params: LayoutRequest


class RequisitionResult(BaseModel):
    """领料确认结果（同时也是近期领料记录中每条记录的形状）。"""

    reused: bool = Field(
        False,
        description="true 表示命中同单号同参数的原记录，本次未重新计算扣纸",
    )
    order_no: str
    paper_width: float
    paper_height: float
    pages_required: int = Field(..., description="服务端重算拼版后的实际用纸页数")
    labels_requested: int
    labels_placed: int
    stock_before: int = Field(..., description="扣减前该纸型余量（张）")
    stock_after: int = Field(..., description="扣减后该纸型余量（张）")
    created_at: str
