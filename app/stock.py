"""纸张库存与领料：SQLite 持久化。

两张表：

- ``paper_stock``：按纸宽 × 纸高（毫米，登记时四舍五入到 2 位小数）
  登记纸张库存，余量按张累计；再次登记同一纸型只累加余量。
- ``paper_requisitions``：领料单。单号唯一；同时保存领料参数的
  规范 JSON（``params_json``）与用于快速比对的哈希指纹。

领料确认在**单个 SQLite 事务**内完成库存核对、扣减与领料记录写入：
服务端用当前参数重新计算拼版，仅当所有标签全部排入且纸型尺寸与
库存登记匹配时，按实际页数扣纸。同一单号 + 同一参数重试时返回
原记录且不重复扣纸；单号相同但参数变化时报冲突。
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any

EPS = 1e-6
"""纸型尺寸匹配容差（毫米）。"""

DIM_DIGITS = 2
"""库存纸宽 / 纸高的登记精度（毫米）。"""

PARAM_DIGITS = 6
"""领料参数规范化时的浮点精度，用来消除 210.0000001 / 210.0 之类差异。"""


class StockConflict(Exception):
    """同一领料单号已使用但参数不一致（HTTP 409）。"""


def default_db_path() -> Path:
    """数据库文件位置：环境变量 PAPER_DB_PATH 优先。

    容器内默认 /data/paper.db（由 Compose 卷持久化）；本地开发时
    回落到项目根目录的 data/paper.db。
    """
    env = os.environ.get("PAPER_DB_PATH")
    if env:
        return Path(env)
    return Path(__file__).resolve().parent.parent / "data" / "paper.db"


def _connect(db_path: Path | str | None = None) -> sqlite3.Connection:
    path = Path(db_path) if db_path is not None else default_db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    # 外键、WAL 提升并发读写时的健壮性
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def init_db(db_path: Path | str | None = None) -> None:
    """创建库存 / 领料表（幂等）。"""
    with _connect(db_path) as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS paper_stock (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                width_mm     REAL    NOT NULL,
                height_mm    REAL    NOT NULL,
                quantity     INTEGER NOT NULL DEFAULT 0 CHECK (quantity >= 0),
                created_at   TEXT    NOT NULL,
                updated_at   TEXT    NOT NULL,
                UNIQUE (width_mm, height_mm)
            );

            CREATE TABLE IF NOT EXISTS paper_requisitions (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                order_no        TEXT    NOT NULL UNIQUE,
                width_mm        REAL    NOT NULL,
                height_mm       REAL    NOT NULL,
                pages           INTEGER NOT NULL,
                labels_placed   INTEGER NOT NULL,
                labels_requested INTEGER NOT NULL,
                params_json     TEXT    NOT NULL,
                params_hash     TEXT    NOT NULL,
                created_at      TEXT    NOT NULL
            );
            """
        )


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds", sep=" ")


def _dim(v: float) -> float:
    return round(float(v), DIM_DIGITS)


def canonicalize_params(value: Any) -> Any:
    """把拼版参数规范化为可稳定序列化 / 比较的结构。

    递归处理：浮点数四舍五入到 PARAM_DIGITS 位（消除重算 / JSON
    往返引入的微小尾差），字典按键排序，列表保持顺序（标签规格
    顺序属于参数的一部分）。
    """
    if isinstance(value, dict):
        return {k: canonicalize_params(value[k]) for k in sorted(value)}
    if isinstance(value, (list, tuple)):
        return [canonicalize_params(v) for v in value]
    if isinstance(value, bool):
        return value
    if isinstance(value, float):
        return round(value, PARAM_DIGITS)
    return value


def _params_artifacts(params: dict) -> tuple[str, str]:
    """返回 (规范 JSON, SHA-256 指纹)。"""
    payload = json.dumps(
        canonicalize_params(params), ensure_ascii=False, sort_keys=True,
        separators=(",", ":"),
    )
    return payload, hashlib.sha256(payload.encode("utf-8")).hexdigest()


def register_stock(
    width: float,
    height: float,
    quantity: int,
    db_path: Path | str | None = None,
) -> dict:
    """按纸宽 × 纸高登记入库；同一纸型余量累加。返回该纸型最新余量。"""
    if width <= 0 or height <= 0:
        raise ValueError("纸张宽、高必须为正数。")
    if quantity <= 0:
        raise ValueError("入库数量必须为正整数。")
    w, h = _dim(width), _dim(height)
    with _connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO paper_stock (width_mm, height_mm, quantity, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT (width_mm, height_mm) DO UPDATE SET
                quantity = quantity + excluded.quantity,
                updated_at = excluded.updated_at
            """,
            (w, h, quantity, _now(), _now()),
        )
        row = conn.execute(
            "SELECT id, width_mm, height_mm, quantity, created_at, updated_at "
            "FROM paper_stock WHERE width_mm = ? AND height_mm = ?",
            (w, h),
        ).fetchone()
    return dict(row)


def list_stock(db_path: Path | str | None = None) -> list[dict]:
    """全部纸型余量，余量为 0 的纸型保留显示，按宽、高排序。"""
    with _connect(db_path) as conn:
        rows = conn.execute(
            "SELECT id, width_mm, height_mm, quantity, created_at, updated_at "
            "FROM paper_stock ORDER BY width_mm, height_mm"
        ).fetchall()
    return [dict(r) for r in rows]


def list_requisitions(
    limit: int = 20, db_path: Path | str | None = None
) -> list[dict]:
    """近期领料记录（最新在前）。"""
    with _connect(db_path) as conn:
        rows = conn.execute(
            "SELECT order_no, width_mm, height_mm, pages, "
            "labels_placed, labels_requested, created_at "
            "FROM paper_requisitions ORDER BY id DESC LIMIT ?",
            (int(limit),),
        ).fetchall()
    return [dict(r) for r in rows]


def get_requisition(
    order_no: str, db_path: Path | str | None = None
) -> dict | None:
    with _connect(db_path) as conn:
        row = conn.execute(
            "SELECT order_no, width_mm, height_mm, pages, "
            "labels_placed, labels_requested, params_json, params_hash, created_at "
            "FROM paper_requisitions WHERE order_no = ?",
            (order_no,),
        ).fetchone()
    return dict(row) if row is not None else None


def _result_from_row(row: dict, retried: bool) -> dict:
    return {
        "order_no": row["order_no"],
        "page_width": row["width_mm"],
        "page_height": row["height_mm"],
        "pages": row["pages"],
        "labels_placed": row["labels_placed"],
        "labels_requested": row["labels_requested"],
        "deducted": not retried,
        "retried": retried,
        "message": (
            f"领料单 {row['order_no']} 已存在且参数一致：直接返回原记录，"
            "库存不重复扣减。"
            if retried
            else f"领料成功：按实际页数 {row['pages']} 张扣纸。"
        ),
        "created_at": row["created_at"],
    }


def confirm_requisition(
    order_no: str,
        params: dict,
        plan,
        db_path: Path | str | None = None,
) -> dict:
    """确认领料：核对 → 扣减 → 写记录，全部在单个事务中完成。

    ``plan`` 为服务端用当前 ``params`` 重新计算出的拼版结果
    （LayoutResponse）。调用方需保证它已通过「全部标签排入」校验。

    - 单号已存在且参数一致：返回原记录（retried=True），不扣纸；
    - 单号已存在但参数不同：抛 :class:`StockConflict`（HTTP 409）；
    - 纸型未登记 / 余量不足：抛 ValueError（HTTP 400），事务回滚。
    """
    order_no = (order_no or "").strip()
    if not order_no:
        raise ValueError("领料单号不能为空。")

    pages = len(plan.pages)
    if pages <= 0:
        raise ValueError("拼版结果为空，没有可领取的纸张。")
    if plan.total_placed != plan.total_requested:
        raise ValueError(
            "存在未排入的标签，不能领料：请调整纸张 / 边距 / 间距或标签规格后重试。"
        )

    w, h = _dim(params["page_width"]), _dim(params["page_height"])
    params_json, params_hash = _params_artifacts(params)

    conn = _connect(db_path)
    try:
        # 显式 BEGIN，保证「核对 → 扣减 → 写记录」为单个原子事务
        conn.execute("BEGIN IMMEDIATE")

        existing = conn.execute(
            "SELECT order_no, width_mm, height_mm, pages, labels_placed, "
            "labels_requested, params_json, params_hash, created_at "
            "FROM paper_requisitions WHERE order_no = ?",
            (order_no,),
        ).fetchone()

        if existing is not None:
            if existing["params_hash"] != params_hash:
                raise StockConflict(
                    f"领料单号「{order_no}」已被使用且拼版参数不一致，"
                    "不能改用新参数重复领料；请核对参数或更换单号。"
                )
            # 同单号同参数：幂等重试，返回原记录，绝不重复扣纸
            conn.commit()
            return _result_from_row(dict(existing), retried=True)

        row = conn.execute(
            "SELECT quantity FROM paper_stock "
            "WHERE ABS(width_mm - ?) <= ? AND ABS(height_mm - ?) <= ?",
            (w, EPS, h, EPS),
        ).fetchone()
        if row is None:
            raise ValueError(
                f"库存中没有纸型 {w:g} × {h:g}mm：请先在「纸张库存」中"
                "按相同纸宽、纸高登记入库，再确认领料。"
            )

        remaining = int(row["quantity"])
        if remaining < pages:
            raise ValueError(
                f"纸张 {w:g} × {h:g}mm 库存不足：本次拼版实际需要 "
                f"{pages} 张，当前余量仅 {remaining} 张，"
                f"还差 {pages - remaining} 张。请先补登记库存后再领料。"
            )

        conn.execute(
            "UPDATE paper_stock SET quantity = quantity - ?, updated_at = ? "
            "WHERE ABS(width_mm - ?) <= ? AND ABS(height_mm - ?) <= ?",
            (pages, _now(), w, EPS, h, EPS),
        )
        created_at = _now()
        conn.execute(
            "INSERT INTO paper_requisitions "
            "(order_no, width_mm, height_mm, pages, labels_placed, "
            "labels_requested, params_json, params_hash, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                order_no, w, h, pages,
                plan.total_placed, plan.total_requested,
                params_json, params_hash, created_at,
            ),
        )
        conn.commit()
    except StockConflict:
        # 单号冲突属正常业务分支（幂等 / 409 判定），不应触发回滚告警
        conn.rollback()
        raise
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    saved = {
        "order_no": order_no,
        "width_mm": w,
        "height_mm": h,
        "pages": pages,
        "labels_placed": plan.total_placed,
        "labels_requested": plan.total_requested,
        "created_at": created_at,
    }
    return _result_from_row(saved, retried=False)
