"""SQLite 连接管理与初始化。

使用 WAL 并开启外键约束；写操作通过 ``immediate_tx`` 先获取保留锁，
配合条件 UPDATE 保证「同一物品并发确认最多成功一次」。
"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path

SCHEMA_PATH = Path(__file__).with_name("schema.sql")


def connect(db_path: str | Path) -> sqlite3.Connection:
    conn = sqlite3.connect(
        str(db_path), timeout=10, isolation_level=None, check_same_thread=False
    )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=10000")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


@contextmanager
def immediate_tx(conn: sqlite3.Connection):
    """开启 IMMEDIATE 事务：进入即取写锁，避免并发写互相饿死。

    正常退出提交，异常回滚。
    """
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield
    except Exception:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")


def init_db(db_path: str | Path) -> None:
    """创建库文件并应用幂等建表脚本（即数据初始化）。"""
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    ddl = SCHEMA_PATH.read_text(encoding="utf-8")
    conn = connect(path)
    try:
        conn.executescript(ddl)
    finally:
        conn.close()
