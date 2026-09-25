"""纸张库存登记与领料确认（全部写操作在单个 SQLite 事务内完成）。

业务规则：

- 库存按「纸宽 × 纸高」（毫米，统一保留 2 位小数）登记，重复登记累加余量；
- 确认领料时服务端用当前参数**重新计算拼版**，仅当所有标签全部排入、
  且纸张尺寸与库存纸型**完全一致**时，才按实际页数扣减余量；
- 库存核对、余量扣减、领料记录写入在同一事务（BEGIN IMMEDIATE）中完成，
  并发确认不会超扣；
- 同一领料单号重试：参数指纹一致则直接返回原记录，**不重复扣纸**；
  单号相同但参数变化时抛 :class:`StockConflict`（HTTP 409）。
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime

from .layout import compute_layout
from .models import LayoutRequest, StockItem


class StockConflict(Exception):
    """业务冲突：同单号参数变化、纸型无库存 / 尺寸不匹配、库存不足或
    标签未全部排入。路由层按 409（参数冲突）/ 400 区分映射。"""


def _norm_dim(v: float) -> float:
    """纸宽 / 纸高统一保留 2 位小数，作为纸型标识与匹配依据。"""
    return round(float(v) + 0.0, 2)


def canonical_params(params: LayoutRequest) -> str:
    """规范化参数 JSON：pydantic 先规整类型，再按键名排序，使同一业务
    参数无论字段出现顺序如何都得到完全相同的文本（重试幂等的基础）。"""
    return json.dumps(
        params.model_dump(), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )


def _params_fingerprint(params_json: str) -> str:
    return hashlib.sha256(params_json.encode("utf-8")).hexdigest()


def _now() -> str:
    return datetime.now().replace(microsecond=0).isoformat()


def _row_to_item(row: sqlite3.Row) -> StockItem:
    return StockItem(
        paper_width=row["paper_width"],
        paper_height=row["paper_height"],
        quantity=row["quantity"],
        updated_at=row["updated_at"],
    )


def list_stock(conn: sqlite3.Connection) -> list[StockItem]:
    """查看全部纸型余量（纸宽、纸高升序）。"""
    rows = conn.execute(
        "SELECT paper_width, paper_height, quantity, updated_at "
        "FROM paper_stock ORDER BY paper_width, paper_height"
    ).fetchall()
    return [_row_to_item(r) for r in rows]


def register_stock(
    conn: sqlite3.Connection, paper_width: float, paper_height: float, quantity: int
) -> StockItem:
    """按纸宽 × 纸高入库 quantity 张；同纸型重复登记累加到余量。"""
    w, h = _norm_dim(paper_width), _norm_dim(paper_height)
    now = _now()
    conn.execute(
        """
        INSERT INTO paper_stock (paper_width, paper_height, quantity, updated_at)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(paper_width, paper_height) DO UPDATE SET
            quantity = quantity + excluded.quantity,
            updated_at = excluded.updated_at
        """,
        (w, h, quantity, now),
    )
    row = conn.execute(
        "SELECT paper_width, paper_height, quantity, updated_at "
        "FROM paper_stock WHERE paper_width = ? AND paper_height = ?",
        (w, h),
    ).fetchone()
    return _row_to_item(row)


def _row_to_result(row: sqlite3.Row, reused: bool) -> dict:
    return {
        "reused": reused,
        "order_no": row["order_no"],
        "paper_width": row["paper_width"],
        "paper_height": row["paper_height"],
        "pages_required": row["pages_required"],
        "labels_requested": row["labels_requested"],
        "labels_placed": row["labels_placed"],
        "stock_before": row["stock_before"],
        "stock_after": row["stock_after"],
        "created_at": row["created_at"],
    }


def list_requisitions(conn: sqlite3.Connection, limit: int = 10) -> list[dict]:
    """近期领料记录（最新在前）。"""
    rows = conn.execute(
        "SELECT order_no, paper_width, paper_height, pages_required, "
        "labels_requested, labels_placed, stock_before, stock_after, created_at "
        "FROM requisitions ORDER BY id DESC LIMIT ?",
        (max(1, min(limit, 100)),),
    ).fetchall()
    return [_row_to_result(r, reused=False) for r in rows]


def confirm_requisition(
    conn: sqlite3.Connection, order_no: str, params: LayoutRequest
) -> dict:
    """确认领料：服务端重算拼版 → 事务内核对库存 → 扣纸 → 写领料记录。

    返回结果字典（reused=True 表示幂等命中原记录，未重新扣纸）。
    任何业务不满足条件的情况抛 :class:`StockConflict`，事务整体回滚，
    库存与领料记录都不会被改动。
    """
    order_no = order_no.strip()
    params_json = canonical_params(params)
    params_hash = _params_fingerprint(params_json)

    # 先在事务外做一次同单号判定：参数一致可直接幂等返回，不必重算拼版
    existing = conn.execute(
        "SELECT * FROM requisitions WHERE order_no = ?", (order_no,)
    ).fetchone()
    if existing is not None:
        if existing["params_hash"] == params_hash:
            return _row_to_result(existing, reused=True)
        raise StockConflict(
            f"领料单号「{order_no}」已存在但参数与本次不同：原记录纸型 "
            f"{existing['paper_width']:g}×{existing['paper_height']:g}mm、"
            f"{existing['labels_requested']} 个标签用 "
            f"{existing['pages_required']} 张纸。请核对单号；"
            "新参数请改用新的领料单号，原单号不能改单。"
        )

    # 服务端用当前参数重新计算拼版（不信任前端页数 / 排入数量）
    plan = compute_layout(params)
    pages_required = len(plan.pages)

    if plan.total_placed != plan.total_requested:
        unplaced = plan.total_requested - plan.total_placed
        raise StockConflict(
            f"当前拼版有 {unplaced} 个标签未能排入（需求 "
            f"{plan.total_requested} 个，仅排入 {plan.total_placed} 个），"
            "不能领料扣纸：请调大纸张 / 页边距或减小标签尺寸、份数后重新确认。"
        )

    w = _norm_dim(params.page_width)
    h = _norm_dim(params.page_height)
    now = _now()

    # BEGIN IMMEDIATE 立即取写锁：库存核对、扣减、记录写入原子提交，
    # 两个并发确认不可能同时读到同一余量而超扣
    conn.execute("BEGIN IMMEDIATE")
    try:
        # 并发请求可能恰好在本事务前插入了同一单号，按唯一键再判一次
        dup = conn.execute(
            "SELECT * FROM requisitions WHERE order_no = ?", (order_no,)
        ).fetchone()
        if dup is not None:
            if dup["params_hash"] == params_hash:
                conn.execute("ROLLBACK")
                return _row_to_result(dup, reused=True)
            raise StockConflict(
                f"领料单号「{order_no}」已存在但参数与本次不同，"
                "请改用新的领料单号。"
            )

        row = conn.execute(
            "SELECT paper_width, paper_height, quantity FROM paper_stock "
            "WHERE paper_width = ? AND paper_height = ?",
            (w, h),
        ).fetchone()
        if row is None:
            raise StockConflict(
                f"纸型 {w:g}×{h:g}mm 未登记库存（余量 0 张），"
                f"本次拼版需要 {pages_required} 张：请先在「纸张库存」中"
                "按完全相同的纸宽、纸高登记入库后再确认领料。"
            )
        stock_before = row["quantity"]
        if stock_before < pages_required:
            raise StockConflict(
                f"纸型 {w:g}×{h:g}mm 库存不足：本次拼版实际需要 "
                f"{pages_required} 张，当前余量仅 {stock_before} 张，"
                f"还差 {pages_required - stock_before} 张；请补登记库存后重试。"
            )

        stock_after = stock_before - pages_required
        conn.execute(
            "UPDATE paper_stock SET quantity = ?, updated_at = ? "
            "WHERE paper_width = ? AND paper_height = ?",
            (stock_after, now, w, h),
        )
        try:
            conn.execute(
                """
                INSERT INTO requisitions (
                    order_no, paper_width, paper_height, pages_required,
                    labels_requested, labels_placed, stock_before, stock_after,
                    params_json, params_hash, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    order_no, w, h, pages_required,
                    plan.total_requested, plan.total_placed,
                    stock_before, stock_after,
                    params_json, params_hash, now,
                ),
            )
        except sqlite3.IntegrityError as exc:
            # 并发同单号：两个请求都在事务外预检为空、在 BEGIN IMMEDIATE
            # 上排队，先到者提交后后到者撞 UNIQUE 约束。按幂等 / 冲突语义
            # 重判一次，避免直接冒出 500。
            winner = conn.execute(
                "SELECT * FROM requisitions WHERE order_no = ?", (order_no,)
            ).fetchone()
            if winner is not None and winner["params_hash"] == params_hash:
                conn.execute("ROLLBACK")
                return _row_to_result(winner, reused=True)
            raise StockConflict(
                f"领料单号「{order_no}」已存在但参数与本次不同，"
                "请改用新的领料单号。"
            ) from exc
        conn.execute("COMMIT")
    except BaseException:
        # 未提交（包括业务冲突与任何异常）一律回滚，库存保持原样
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        raise

    saved = conn.execute(
        "SELECT * FROM requisitions WHERE order_no = ?", (order_no,)
    ).fetchone()
    return _row_to_result(saved, reused=False)
