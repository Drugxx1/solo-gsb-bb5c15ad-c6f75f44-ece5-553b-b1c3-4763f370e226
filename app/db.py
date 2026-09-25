"""SQLite 连接管理与建表（纸张库存 / 领料记录）。

数据库文件默认位于项目根目录的 data/label_stock.db，可用环境变量
LABEL_STOCK_DB 覆盖（docker-compose.yml 把 /app/data 挂为命名卷，
因此库存与领料记录在容器重建后仍然保留）。

连接策略：每个请求由 :func:`db_session` 依赖打开独立连接（请求结束
自动关闭）。SQLite 文件库的并发写由库级写锁串行化，领料事务使用
BEGIN IMMEDIATE 立即取锁，因此多线程 / 多连接下也不会超扣；
绝不能把同一个连接跨线程共享（各线程的事务状态会互相破坏）。
"""

from __future__ import annotations

import os
import sqlite3
from collections.abc import Iterator
from pathlib import Path

_DEFAULT_DB_PATH = (
    Path(__file__).resolve().parent.parent / "data" / "label_stock.db"
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS paper_stock (
    paper_width  REAL    NOT NULL,
    paper_height REAL    NOT NULL,
    quantity     INTEGER NOT NULL CHECK (quantity >= 0),
    updated_at   TEXT,
    PRIMARY KEY (paper_width, paper_height)
);

CREATE TABLE IF NOT EXISTS requisitions (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    order_no         TEXT    NOT NULL UNIQUE,
    paper_width      REAL    NOT NULL,
    paper_height     REAL    NOT NULL,
    pages_required   INTEGER NOT NULL,
    labels_requested INTEGER NOT NULL,
    labels_placed    INTEGER NOT NULL,
    stock_before     INTEGER NOT NULL,
    stock_after      INTEGER NOT NULL,
    params_json      TEXT    NOT NULL,
    params_hash      TEXT    NOT NULL,
    created_at       TEXT    NOT NULL
);
"""


def db_path() -> Path:
    return Path(os.environ.get("LABEL_STOCK_DB", str(_DEFAULT_DB_PATH)))


def connect(database: str | Path | None = None) -> sqlite3.Connection:
    """打开一个启用外键、Row 工厂的 SQLite 连接。

    isolation_level=None 表示完全由代码管理事务（autocommit 模式），
    领料确认里的 BEGIN IMMEDIATE / COMMIT / ROLLBACK 不会被驱动的
    隐式事务干扰；单语句的库存登记在该模式下天然原子提交。
    每个请求独占一个连接，故开启 check_same_thread=False 后也不会有
    两个线程同时操作同一连接事务状态的问题。
    """
    conn = sqlite3.connect(
        str(database or db_path()),
        isolation_level=None,
        check_same_thread=False,
    )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    # 写锁忙时等待最多 5 秒，而不是立刻抛 database is locked
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


def create_schema(conn: sqlite3.Connection) -> None:
    """创建库存表与领料记录表（幂等）。

    paper_stock 以（纸宽, 纸高）为业务主键，quantity 为当前余量；
    requisitions 以领料单号 order_no 为唯一键（幂等重试的核心），
    params_json 保存规范化后的请求参数，params_hash 为其指纹，
    stock_before/after 记录扣减前后的余量快照。
    """
    conn.executescript(_SCHEMA)


def init_db(database: str | Path | None = None) -> None:
    """在目标库上幂等建表；文件库的父目录自动创建。"""
    target = Path(database or db_path())
    if str(target) != ":memory:":
        target.parent.mkdir(parents=True, exist_ok=True)
    conn = connect(target)
    try:
        create_schema(conn)
    finally:
        conn.close()


def db_session() -> Iterator[sqlite3.Connection]:
    """FastAPI 依赖：每个请求打开独立 SQLite 连接，响应后关闭。

    同步端点在线程池中运行，每个请求（线程）拿到的是自己的连接，
    BEGIN IMMEDIATE 事务彼此隔离、由 SQLite 写锁串行提交。
    """
    path = db_path()
    if str(path) != ":memory:":
        # 首次请求前若数据目录 / 表尚未创建（例如未走 lifespan），
        # 这里兜底建表；CREATE TABLE IF NOT EXISTS 在并发下也是安全的。
        path.parent.mkdir(parents=True, exist_ok=True)
    conn = connect(path)
    try:
        create_schema(conn)
        yield conn
    finally:
        conn.close()
