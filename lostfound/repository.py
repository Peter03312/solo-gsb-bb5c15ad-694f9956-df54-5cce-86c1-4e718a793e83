"""数据访问层：所有 SQL 均为参数化查询；关键写操作为条件式原子更新。"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any

from . import security
from .db import immediate_tx
# 状态中文标签（单一事实来源，模板也使用）
ITEM_STATUS_LABELS = {"kept": "保管中", "removed": "已撤下", "returned": "已交还"}
CLAIM_STATUS_LABELS = {"pending": "待核对", "selected": "已获选", "rejected": "未获选"}


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


# ----------------------------------------------------------------- 物品

def create_item(conn: sqlite3.Connection, public_desc: str, private_features: str) -> int:
    ts = now_iso()
    with immediate_tx(conn):
        cur = conn.execute(
            "INSERT INTO items (public_description, private_features, status, created_at, updated_at)"
            " VALUES (?, ?, 'kept', ?, ?)",
            (public_desc, private_features, ts, ts),
        )
    return int(cur.lastrowid)


def get_item(conn: sqlite3.Connection, item_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()


def list_public_items(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """访客列表：仅公开保管中物品，不暴露任何私密信息。

    has_selected 用于提示该物品已有人获选、不再接受新申请。
    """
    return conn.execute(
        """
        SELECT i.id, i.public_description, i.created_at,
               EXISTS (SELECT 1 FROM claims c
                        WHERE c.item_id = i.id AND c.status = 'selected') AS has_selected
        FROM items i
        WHERE i.status = 'kept'
        ORDER BY i.id DESC
        """
    ).fetchall()


def list_items_for_staff(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT i.*,
               (SELECT COUNT(*) FROM claims c WHERE c.item_id = i.id) AS claim_count,
               (SELECT COUNT(*) FROM claims c
                 WHERE c.item_id = i.id AND c.status = 'pending') AS pending_count
        FROM items i
        ORDER BY i.id DESC
        """
    ).fetchall()


def set_item_status(conn: sqlite3.Connection, item_id: int, status: str) -> None:
    with immediate_tx(conn):
        conn.execute(
            "UPDATE items SET status = ?, updated_at = ? WHERE id = ?",
            (status, now_iso(), item_id),
        )


def remove_item(conn: sqlite3.Connection, item_id: int) -> None:
    """撤下物品：同时拒绝其全部待核对申请（撤下后不可再申请）。

    已交还不可撤下；已撤下重复操作为幂等无变化。
    """
    ts = now_iso()
    with immediate_tx(conn):
        row = conn.execute("SELECT status FROM items WHERE id = ?", (item_id,)).fetchone()
        if row is None:
            raise LookupError("物品不存在")
        if row["status"] == "returned":
            raise ValueError("该物品已交还，不能撤下")
        conn.execute(
            "UPDATE items SET status = 'removed', updated_at = ? WHERE id = ? AND status = 'kept'",
            (ts, item_id),
        )
        conn.execute(
            "UPDATE claims SET status = 'rejected', decided_at = ?"
            " WHERE item_id = ? AND status = 'pending'",
            (ts, item_id),
        )


def restore_item(conn: sqlite3.Connection, item_id: int) -> None:
    with immediate_tx(conn):
        conn.execute(
            "UPDATE items SET status = 'kept', updated_at = ? WHERE id = ? AND status = 'removed'",
            (now_iso(), item_id),
        )


# ----------------------------------------------------------------- 申请

def create_claim(
    conn: sqlite3.Connection, item_id: int, answer: str, contact: str
) -> str:
    """提交认领。仅当物品仍在保管且尚无人获选时接受；返回明文查询凭证（仅此一次）。"""
    token = security.new_query_token()
    token_hash = security.sha256_hex(token)
    ts = now_iso()
    with immediate_tx(conn):
        row = conn.execute(
            "SELECT status FROM items WHERE id = ?", (item_id,)
        ).fetchone()
        if row is None:
            raise LookupError("物品不存在")
        if row["status"] != "kept":
            raise ValueError("该物品已撤下或已交还，不再接受新的认领申请")
        blocked = conn.execute(
            "SELECT 1 FROM claims WHERE item_id = ? AND status = 'selected' LIMIT 1",
            (item_id,),
        ).fetchone()
        if blocked is not None:
            raise ValueError("该物品已确定获选人，不再接受新的认领申请")
        conn.execute(
            "INSERT INTO claims (item_id, answer, contact, token_hash, status, created_at)"
            " VALUES (?, ?, ?, ?, 'pending', ?)",
            (item_id, answer, contact, token_hash, ts),
        )
    return token


def get_claim_by_token(conn: sqlite3.Connection, token: str) -> sqlite3.Row | None:
    return conn.execute(
        """
        SELECT c.*, i.public_description AS item_description, i.status AS item_status
        FROM claims c JOIN items i ON i.id = c.item_id
        WHERE c.token_hash = ?
        """,
        (security.sha256_hex(token),),
    ).fetchone()


def list_claims_for_item(conn: sqlite3.Connection, item_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM claims WHERE item_id = ? ORDER BY id ASC", (item_id,)
    ).fetchall()


def get_claim(conn: sqlite3.Connection, claim_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM claims WHERE id = ?", (claim_id,)).fetchone()


def select_claim(conn: sqlite3.Connection, claim_id: int, code_ttl_seconds: int, key: bytes) -> str:
    """选定一份认领并签发首个一次性交还码。

    原子性保证：
      1. 条件 UPDATE 仅在物品处于 kept 且申请仍为 pending 时生效；
      2. 同事务内将同一物品的其他 pending 申请标记为 rejected。
    并发下至多一个请求 rowcount=1，其余得到 None（未获选）。
    """
    ts = now_iso()
    expires = (
        datetime.now(timezone.utc) + timedelta(seconds=code_ttl_seconds)
    ).replace(microsecond=0).isoformat()
    code = security.new_handover_code()
    with immediate_tx(conn):
        row = conn.execute(
            """
            SELECT c.id AS claim_id, c.item_id AS item_id, i.status AS item_status
            FROM claims c JOIN items i ON i.id = c.item_id
            WHERE c.id = ?
            """,
            (claim_id,),
        ).fetchone()
        if row is None:
            raise LookupError("申请不存在")
        if row["item_status"] != "kept":
            raise ValueError("物品已撤下或已交还，无法选定认领")

        cur = conn.execute(
            "UPDATE claims SET status = 'selected', decided_at = ?"
            " WHERE id = ? AND status = 'pending'",
            (ts, claim_id),
        )
        if cur.rowcount != 1:
            existing = get_claim(conn, claim_id)
            if existing is not None and existing["status"] == "rejected":
                raise ValueError("该申请已因他人获选而标记为未获选")
            raise ValueError("该申请已经处理，不能重复选定")

        conn.execute(
            "UPDATE claims SET status = 'rejected', decided_at = ?"
            " WHERE item_id = ? AND status = 'pending' AND id <> ?",
            (ts, row["item_id"], claim_id),
        )
        conn.execute(
            "INSERT INTO handover_codes (claim_id, code_hash, code_enc, issued_at, expires_at)"
            " VALUES (?, ?, ?, ?, ?)",
            (claim_id, security.sha256_hex(code), security.encrypt_field(key, code), ts, expires),
        )
    return code


# ----------------------------------------------------------------- 交还码

def get_active_code_for_claim(conn: sqlite3.Connection, claim_id: int) -> sqlite3.Row | None:
    """未核销、未作废旧码的最新一枚交还码（可能已过期）。"""
    return conn.execute(
        "SELECT * FROM handover_codes WHERE claim_id = ? AND consumed_at IS NULL"
        " AND invalidated_at IS NULL ORDER BY id DESC LIMIT 1",
        (claim_id,),
    ).fetchone()


def get_claim_with_context(conn: sqlite3.Connection, claim_id: int) -> sqlite3.Row | None:
    return conn.execute(
        """
        SELECT c.*, i.public_description, i.private_features, i.status AS item_status
        FROM claims c JOIN items i ON i.id = c.item_id
        WHERE c.id = ?
        """,
        (claim_id,),
    ).fetchone()


def resend_code(conn: sqlite3.Connection, claim_id: int, ttl_seconds: int, key: bytes) -> str:
    """重发交还码：旧码立即失效，签发一枚新码。"""
    ts = now_iso()
    expires = (
        datetime.now(timezone.utc) + timedelta(seconds=ttl_seconds)
    ).replace(microsecond=0).isoformat()
    code = security.new_handover_code()
    with immediate_tx(conn):
        claim = conn.execute(
            "SELECT c.id, c.status, i.status AS item_status FROM claims c"
            " JOIN items i ON i.id = c.item_id WHERE c.id = ?",
            (claim_id,),
        ).fetchone()
        if claim is None:
            raise LookupError("申请不存在")
        if claim["status"] != "selected":
            raise ValueError("只有已获选的申请可以重发交还码")
        if claim["item_status"] == "returned":
            raise ValueError("物品已交还，无需重发")
        conn.execute(
            "UPDATE handover_codes SET invalidated_at = ?"
            " WHERE claim_id = ? AND consumed_at IS NULL AND invalidated_at IS NULL",
            (ts, claim_id),
        )
        conn.execute(
            "INSERT INTO handover_codes (claim_id, code_hash, code_enc, issued_at, expires_at)"
            " VALUES (?, ?, ?, ?, ?)",
            (claim_id, security.sha256_hex(code), security.encrypt_field(key, code), ts, expires),
        )
    return code


def decrypt_code(key: bytes, code_row: sqlite3.Row) -> str | None:
    """授权页面回显交还码明文（数据库只存哈希与密文）。"""
    return security.decrypt_field(key, code_row["code_enc"])


def consume_code(conn: sqlite3.Connection, code: str) -> dict[str, Any]:
    """工作人员核码并完成交还。

    单次 IMMEDIATE 事务内做存在性/状态判断与核销，保证：
      - 重复核码（同一码两次提交、两枚码并发核销）不得再次交还；
      - 已交还、已过期、已作废旧码均拒绝。
    返回 {'ok': True, 'claim_id': ..., 'item_id': ...} 或
    {'ok': False, 'reason': '...'}。
    """
    code_hash = security.sha256_hex(code.strip())
    ts = now_iso()
    result: dict[str, Any] = {"ok": False, "reason": "交还码不正确"}
    with immediate_tx(conn):
        hc = conn.execute(
            """
            SELECT hc.*, c.item_id, c.status AS claim_status, i.status AS item_status
            FROM handover_codes hc
            JOIN claims c ON c.id = hc.claim_id
            JOIN items i ON i.id = c.item_id
            WHERE hc.code_hash = ?
            """,
            (code_hash,),
        ).fetchone()
        if hc is None:
            return result
        if hc["consumed_at"] is not None:
            return {"ok": False, "reason": "该交还码已使用，不能重复核销"}
        if hc["invalidated_at"] is not None:
            return {"ok": False, "reason": "该交还码已失效（已重发新码）"}
        if hc["expires_at"] < ts:
            return {"ok": False, "reason": "该交还码已过期，请为获选人重发新码"}
        if hc["item_status"] != "kept" or hc["claim_status"] != "selected":
            return {"ok": False, "reason": "物品或认领状态异常，无法交还"}
        cur = conn.execute(
            "UPDATE handover_codes SET consumed_at = ? WHERE id = ? AND consumed_at IS NULL",
            (ts, hc["id"]),
        )
        if cur.rowcount != 1:
            return {"ok": False, "reason": "该交还码已被核销（并发请求）"}
        upd = conn.execute(
            "UPDATE items SET status = 'returned', updated_at = ?"
            " WHERE id = ? AND status = 'kept'",
            (ts, hc["item_id"]),
        )
        if upd.rowcount != 1:
            raise RuntimeError("物品状态异常，回滚本次核销")
        result = {"ok": True, "claim_id": hc["claim_id"], "item_id": hc["item_id"]}
    return result
