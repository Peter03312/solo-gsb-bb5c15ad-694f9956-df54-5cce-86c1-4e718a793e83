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


def remove_item(conn: sqlite3.Connection, item_id: int, expect_version: int | None = None) -> None:
    """撤下物品：同时拒绝其全部待核对申请（撤下后不可再申请）。

    已交还不可撤下；已撤下重复操作为幂等无变化。

    状态判断与状态翻转合并为一条条件 UPDATE：条件在写入时针对最新已提交行
    求值，不依赖事务开始时可能已过期的读快照。``expect_version`` 为打开物品
    页时的 items.version（单调递增）：页面打开后发生过选定 / 改判（version 已
    推进）时撤下被拒绝，要求刷新确认——从而与并发的选定 / 改判之间恰好一方
    的状态变更生效。不传则只按实时状态判断（兼容其他调用方与旧启动方式）。
    """
    ts = now_iso()
    with immediate_tx(conn):
        sql = "UPDATE items SET status = 'removed', updated_at = ?" \
              " WHERE id = ? AND status = 'kept'"
        params: list[Any] = [ts, item_id]
        if expect_version is not None:
            sql += " AND version = ?"
            params.append(expect_version)
        cur = conn.execute(sql, params)
        if cur.rowcount == 1:
            conn.execute(
                "UPDATE claims SET status = 'rejected', decided_at = ?"
                " WHERE item_id = ? AND status = 'pending'",
                (ts, item_id),
            )
            return
        # 未更新：区分不存在 / 已交还 / 已撤下 / 版本过期（此处读取已在写锁后）
        row = conn.execute("SELECT status, version FROM items WHERE id = ?", (item_id,)).fetchone()
        if row is None:
            raise LookupError("物品不存在")
        if row["status"] == "returned":
            raise ValueError("该物品已交还，不能撤下")
        if row["status"] == "removed":
            return  # 幂等：重复撤下无变化
        if expect_version is not None and row["version"] != expect_version:
            raise ValueError("该物品在页面打开后已发生选定或改判，请刷新页面确认最新状态后再撤下")
        raise ValueError("物品状态已被其他操作改变，请刷新页面后重试")


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
        # item_id 关联不可变，锁前读取；状态判断一律走锁内条件式 UPDATE。
        row = conn.execute("SELECT item_id FROM claims WHERE id = ?", (claim_id,)).fetchone()
        if row is None:
            raise LookupError("申请不存在")
        item_id = row["item_id"]

        # 第一条写：物品守卫 + 版本推进。物品已撤下 / 已交还（含并发刚提交）时
        # rowcount=0，整体拒绝，不会与并发撤下双重生效。
        item_upd = conn.execute(
            "UPDATE items SET updated_at = ?, version = version + 1"
            " WHERE id = ? AND status = 'kept'",
            (ts, item_id),
        )
        if item_upd.rowcount != 1:
            raise ValueError("物品已撤下或已交还，无法选定认领")

        cur = conn.execute(
            "UPDATE claims SET status = 'selected', decided_at = ?"
            " WHERE id = ? AND status = 'pending' AND item_id = ?",
            (ts, claim_id, item_id),
        )
        if cur.rowcount != 1:
            existing = get_claim(conn, claim_id)
            if existing is not None and existing["status"] == "rejected":
                raise ValueError("该申请已因他人获选而标记为未获选")
            raise ValueError("该申请已经处理，不能重复选定")

        conn.execute(
            "UPDATE claims SET status = 'rejected', decided_at = ?"
            " WHERE item_id = ? AND status = 'pending' AND id <> ?",
            (ts, item_id, claim_id),
        )
        conn.execute(
            "INSERT INTO handover_codes (claim_id, code_hash, code_enc, issued_at, expires_at)"
            " VALUES (?, ?, ?, ?, ?)",
            (claim_id, security.sha256_hex(code), security.encrypt_field(key, code), ts, expires),
        )
    return code


# ----------------------------------------------------------------- 改判（误选更正）

def rejudge_claim(
    conn: sqlite3.Connection,
    from_claim_id: int,
    to_claim_id: int,
    reason: str,
    code_ttl_seconds: int,
    key: bytes,
    expect_item_version: int | None = None,
) -> None:
    """把获选资格从原申请改判给同一物品的另一份「未获选」申请。

    前置条件（任一不满足即拒绝且不改动任何状态）：
      - 原申请当前必须仍为 selected；
      - 目标申请必须存在、属于同一物品且当前为 rejected；
      - 物品必须仍为 kept（未撤下、未交还）；
      - 更正理由非空（由调用方裁剪后保证）。

    ``expect_item_version`` 为打开原申请页时物品的 version（单调递增整数）。
    物品行守卫 UPDATE 同时要求 ``status='kept' AND version=?`` 并推进版本，
    因此与并发撤下 / 选定 / 另一改判之间对称互斥：先提交者推进版本，后到者
    条件不匹配而整体回滚，只允许一次状态变更生效。不传则跳过版本校验
    （兼容其他调用方与旧启动方式）。

    成功后在单个 IMMEDIATE 事务内：
      1. 原申请 selected -> rejected，其全部未核销交还码立即失效；
      2. 目标申请 rejected -> selected，并签发一枚新的一次性交还码；
      3. 写入 corrections 留痕（双方、理由、时间）。
    """
    ts = now_iso()
    if not reason or not reason.strip():
        raise ValueError("更正理由不能为空")
    expires = (
        datetime.now(timezone.utc) + timedelta(seconds=code_ttl_seconds)
    ).replace(microsecond=0).isoformat()
    code = security.new_handover_code()
    with immediate_tx(conn):
        # 锁前只读校验（此时连接在自动提交态，读到的是最新已提交数据）：
        # 申请双方必须存在、目标必须是同一物品的另一份 rejected 申请。
        # 锁内条件式 UPDATE 再兜底并发变化。
        src = conn.execute("SELECT id, item_id FROM claims WHERE id = ?", (from_claim_id,)).fetchone()
        if src is None:
            raise LookupError("原获选申请不存在")
        dst = conn.execute(
            "SELECT id, item_id, status FROM claims WHERE id = ?", (to_claim_id,)
        ).fetchone()
        if dst is None:
            raise ValueError("目标申请不存在")
        if dst["id"] == src["id"]:
            raise ValueError("改判目标不能是原申请本身")
        if dst["item_id"] != src["item_id"]:
            raise ValueError("目标申请不属于同一物品，不能改判")
        if dst["status"] != "rejected":
            raise ValueError("目标申请必须为未获选状态")

        # 第一条写：原子守卫 + 版本推进。条件在写入时针对最新已提交行求值；
        # 物品已撤下 / 已交还，或版本已被并发撤下 / 选定 / 改判推进时 rowcount=0。
        if expect_item_version is not None:
            item_upd = conn.execute(
                "UPDATE items SET updated_at = ?, version = version + 1"
                " WHERE id = ? AND status = 'kept' AND version = ?",
                (ts, src["item_id"], expect_item_version),
            )
        else:
            item_upd = conn.execute(
                "UPDATE items SET updated_at = ?, version = version + 1"
                " WHERE id = ? AND status = 'kept'",
                (ts, src["item_id"]),
            )
        if item_upd.rowcount != 1:
            cur = conn.execute("SELECT status FROM items WHERE id = ?", (src["item_id"],)).fetchone()
            if cur is None or cur["status"] != "kept":
                raise ValueError("物品已撤下或已交还，不能改判")
            raise ValueError("该物品状态刚被其他操作改变（撤下/选定/改判），请刷新页面确认后再改判")

        down = conn.execute(
            "UPDATE claims SET status = 'rejected', decided_at = ?"
            " WHERE id = ? AND status = 'selected' AND item_id = ?",
            (ts, from_claim_id, src["item_id"]),
        )
        if down.rowcount != 1:
            raise ValueError("原申请已非获选状态，不能改判")
        up = conn.execute(
            "UPDATE claims SET status = 'selected', decided_at = ?"
            " WHERE id = ? AND status = 'rejected' AND item_id = ?",
            (ts, to_claim_id, src["item_id"]),
        )
        if up.rowcount != 1:
            raise ValueError("目标申请必须为未获选状态")

        # 原获选人的未核销交还码（含已过期但未核销者）立即失效；已核销码保持原样。
        conn.execute(
            "UPDATE handover_codes SET invalidated_at = ?"
            " WHERE claim_id = ? AND consumed_at IS NULL AND invalidated_at IS NULL",
            (ts, from_claim_id),
        )
        conn.execute(
            "INSERT INTO handover_codes (claim_id, code_hash, code_enc, issued_at, expires_at)"
            " VALUES (?, ?, ?, ?, ?)",
            (to_claim_id, security.sha256_hex(code), security.encrypt_field(key, code), ts, expires),
        )
        conn.execute(
            "INSERT INTO corrections (item_id, from_claim_id, to_claim_id, reason, created_at)"
            " VALUES (?, ?, ?, ?, ?)",
            (src["item_id"], from_claim_id, to_claim_id, reason, ts),
        )


_CORRECTION_SELECT = """
    SELECT cr.*,
           fc.answer AS from_answer, fc.contact AS from_contact,
           tc.answer AS to_answer,   tc.contact AS to_contact
    FROM corrections cr
    JOIN claims fc ON fc.id = cr.from_claim_id
    JOIN claims tc ON tc.id = cr.to_claim_id
"""


def list_corrections_for_item(conn: sqlite3.Connection, item_id: int) -> list[sqlite3.Row]:
    """该物品的全部改判记录（工作人员追溯），按时间正序。"""
    return conn.execute(
        _CORRECTION_SELECT + " WHERE cr.item_id = ? ORDER BY cr.id ASC", (item_id,)
    ).fetchall()


def list_corrections_for_claim(conn: sqlite3.Connection, claim_id: int) -> list[sqlite3.Row]:
    """涉及某份申请（作为原申请或目标申请）的改判记录，按时间正序。"""
    return conn.execute(
        _CORRECTION_SELECT
        + " WHERE cr.from_claim_id = ? OR cr.to_claim_id = ? ORDER BY cr.id ASC",
        (claim_id, claim_id),
    ).fetchall()


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
        SELECT c.*, i.public_description, i.private_features,
               i.status AS item_status, i.version AS item_version
        FROM claims c JOIN items i ON i.id = c.item_id
        WHERE c.id = ?
        """,
        (claim_id,),
    ).fetchone()


def resend_code(conn: sqlite3.Connection, claim_id: int, ttl_seconds: int, key: bytes) -> str:
    """重发交还码：旧码立即失效，签发一枚新码。

    守卫均为锁内条件式 UPDATE（不依赖等锁期间的旧读快照）：申请在签发时
    必须仍为 selected、物品未交还；否则整体拒绝。因此与并发改判（原申请变
    rejected）或核码交还（物品变 returned）之间，不会给已失效状态签发新码。
    """
    ts = now_iso()
    expires = (
        datetime.now(timezone.utc) + timedelta(seconds=ttl_seconds)
    ).replace(microsecond=0).isoformat()
    code = security.new_handover_code()
    with immediate_tx(conn):
        claim = conn.execute("SELECT id, item_id FROM claims WHERE id = ?", (claim_id,)).fetchone()
        if claim is None:
            raise LookupError("申请不存在")
        guard = conn.execute(
            "UPDATE claims SET decided_at = decided_at WHERE id = ? AND status = 'selected'"
            " RETURNING id",
            (claim_id,),
        )
        if guard.fetchone() is None:
            raise ValueError("只有已获选的申请可以重发交还码")
        item_guard = conn.execute(
            "UPDATE items SET updated_at = updated_at WHERE id = ? AND status != 'returned'"
            " RETURNING id",
            (claim["item_id"],),
        )
        if item_guard.fetchone() is None:
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
            SELECT hc.*, c.item_id, i.version AS item_version
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
            return {"ok": False, "reason": "该交还码已失效（已重发新码或已改判）"}
        if hc["expires_at"] < ts:
            return {"ok": False, "reason": "该交还码已过期，请为获选人重发新码"}
        # 核销 UPDATE 同时要求码仍未核销 / 未作废、申请仍为 selected：
        # 若并发改判已将旧码作废或把该申请改为 rejected，则 rowcount=0。
        cur = conn.execute(
            "UPDATE handover_codes SET consumed_at = ?"
            " WHERE id = ? AND consumed_at IS NULL AND invalidated_at IS NULL",
            (ts, hc["id"]),
        )
        if cur.rowcount != 1:
            return {"ok": False, "reason": "该交还码已失效或已被核销（并发请求）"}
        claim_guard = conn.execute(
            "UPDATE claims SET decided_at = decided_at WHERE id = ? AND status = 'selected'"
            " RETURNING id",
            (hc["claim_id"],),
        )
        if claim_guard.fetchone() is None:
            raise RuntimeError("申请状态已变更，回滚本次核销")
        upd = conn.execute(
            "UPDATE items SET status = 'returned', updated_at = ?"
            " WHERE id = ? AND status = 'kept' AND version = ?",
            (ts, hc["item_id"], hc["item_version"]),
        )
        if upd.rowcount != 1:
            raise RuntimeError("物品状态异常，回滚本次核销")
        result = {"ok": True, "claim_id": hc["claim_id"], "item_id": hc["item_id"]}
    return result
