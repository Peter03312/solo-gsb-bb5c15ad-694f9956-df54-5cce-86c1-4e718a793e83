"""基于 Python 标准库的 WSGI 应用与 HTTP 服务入口。

运行：
    python -m lostfound.app
环境变量见 README。
"""
from __future__ import annotations

import json
import os
import secrets as _secrets
import sqlite3
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable

from . import db as dbmod
from . import render, repository
from .security import (
    _b64d,
    _b64e,
    constant_time_equals,
    decrypt_field,
    hash_password,
    load_or_create_secret,
    make_signed_cookie,
    read_signed_cookie,
)

SESSION_COOKIE = "lf_session"
FLASH_COOKIE = "lf_flash"
SESSION_MAX_AGE = 12 * 3600

Flash = tuple[str, str]

# ----------------------------------------------------------------- 请求/响应

class Request:
    def __init__(self, environ: dict[str, Any]):
        self.environ = environ
        self.method = environ["REQUEST_METHOD"].upper()
        self.path = environ["PATH_INFO"] or "/"
        self.query = _parse_qs(environ.get("QUERY_STRING", ""))
        self.cookies = _parse_cookies(environ.get("HTTP_COOKIE", ""))
        self.session: dict[str, Any] | None = None
        self.session_dirty = False
        self.clear_session = False
        self._form: dict[str, str] | None = None

    @property
    def is_staff(self) -> bool:
        return bool(self.session and self.session.get("staff"))

    def form(self, max_bytes: int = 64 * 1024) -> dict[str, str]:
        if self._form is not None:
            return self._form
        ctype = self.environ.get("CONTENT_TYPE", "")
        length = int(self.environ.get("CONTENT_LENGTH") or 0)
        if length > max_bytes:
            raise ValueError("提交内容过大")
        body = self.environ["wsgi.input"].read(length) if length else b""
        if "multipart/form-data" in ctype:
            result = _parse_multipart(body, ctype)
        else:
            result = _parse_qs(body.decode("utf-8", "replace"))
        self._form = result
        return result


class Response:
    def __init__(
        self,
        body: str | bytes,
        status: str = "200 OK",
        content_type: str = "text/html; charset=utf-8",
        headers: list[tuple[str, str]] | None = None,
    ):
        self.body = body.encode("utf-8") if isinstance(body, str) else body
        self.status = status
        self.headers = [("Content-Type", content_type), ("Content-Length", str(len(self.body)))]
        self.headers += _SECURITY_HEADERS
        if headers:
            self.headers += headers
        # 需要随响应下发的原始 Cookie（"名称=值; 属性..."）
        self.set_cookies: list[str] = []

    @classmethod
    def redirect(cls, location: str, flash: Flash | None = None, *, clear_session: bool = False) -> "Response":
        resp = cls(b"", "303 See Other", headers=[("Location", location)])
        if flash:
            payload = _b64e(json.dumps(list(flash), separators=(",", ":")).encode())
            resp.set_cookies.append(f"{FLASH_COOKIE}={payload}; Path=/; SameSite=Lax; HttpOnly")
        if clear_session:
            resp.set_cookies.append(f"{SESSION_COOKIE}=; Path=/; Max-Age=0")
        return resp


_SECURITY_HEADERS = [
    ("X-Content-Type-Options", "nosniff"),
    ("X-Frame-Options", "DENY"),
    ("Referrer-Policy", "no-referrer"),
    ("Cache-Control", "no-store"),
    ("Content-Security-Policy", "default-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:"),
]


def _parse_qs(raw: str | bytes) -> dict[str, str]:
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", "replace")
    return {k: v[-1] for k, v in urllib.parse.parse_qs(raw, keep_blank_values=True).items()}


def _parse_cookies(header: str) -> dict[str, str]:
    cookies: dict[str, str] = {}
    for part in header.split(";"):
        if "=" in part:
            k, v = part.strip().split("=", 1)
            cookies[k] = v
    return cookies


def _parse_multipart(body: bytes, content_type: str) -> dict[str, str]:
    """极简 multipart 解析，只取文本字段（本应用无文件上传）。"""
    marker = "boundary="
    if marker not in content_type:
        return {}
    boundary = content_type.split(marker, 1)[1].strip().strip('"')
    delim = b"--" + boundary.encode()
    fields: dict[str, str] = {}
    for chunk in body.split(delim):
        if not chunk or chunk in (b"--", b"--\r\n", b"\r\n") or b"\r\n\r\n" not in chunk:
            continue
        head, value = chunk.split(b"\r\n\r\n", 1)
        value = value.rstrip(b"\r\n")
        if b'filename=' in head:
            continue
        for line in head.decode("utf-8", "replace").split("\r\n"):
            if line.lower().startswith("content-disposition:") and 'name="' in line:
                name = line.split('name="', 1)[1].split('"', 1)[0]
                fields[name] = value.decode("utf-8", "replace")
    return fields


def _match(template: str, path: str) -> list[str] | None:
    t_parts = [p for p in template.strip("/").split("/") if p]
    p_parts = [p for p in path.strip("/").split("/") if p]
    if len(t_parts) != len(p_parts):
        return None
    out: list[str] = []
    for t, v in zip(t_parts, p_parts):
        if t.startswith("{") and t.endswith("}"):
            if not v.isdigit():
                return None
            out.append(v)
        elif t != v:
            return None
    return out


# ----------------------------------------------------------------- 应用

class App:
    def __init__(self, data_dir: str | None = None):
        self.data_dir = data_dir or os.environ.get("LF_DATA_DIR", "/data")
        os.makedirs(self.data_dir, exist_ok=True)
        self.db_path = os.path.join(self.data_dir, "lostfound.db")
        dbmod.init_db(self.db_path)
        self.secret = load_or_create_secret(os.path.join(self.data_dir, "secret.key"))
        self.staff_password_hash = hash_password(os.environ.get("STAFF_PASSWORD", "change-me"))
        self.code_ttl = int(os.environ.get("HANDOVER_CODE_TTL", str(24 * 3600)))
        self.tls = threading.local()

    def conn(self) -> sqlite3.Connection:
        conn = getattr(self.tls, "conn", None)
        if conn is None:
            conn = dbmod.connect(self.db_path)
            self.tls.conn = conn
        return conn

    def __call__(self, environ: dict[str, Any], start_response: Callable) -> Any:
        req = Request(environ)
        req.session = read_signed_cookie(self.secret, req.cookies.get(SESSION_COOKIE))
        flash = self._read_flash(req.cookies.get(FLASH_COOKIE))
        try:
            resp = self.dispatch(req, flash)
        except ValueError as exc:  # 表单校验 / 业务冲突
            resp = self.html(render.error_page(str(exc), "请求无法处理", req.is_staff), "400 Bad Request")
        except LookupError as exc:
            resp = self.html(render.error_page(str(exc), "未找到", req.is_staff), "404 Not Found")

        if req.session_dirty and req.session is not None:
            resp.set_cookies.append(
                make_signed_cookie(SESSION_COOKIE, self.secret, req.session, SESSION_MAX_AGE)
            )
        for cookie in resp.set_cookies:
            resp.headers.append(("Set-Cookie", cookie))
        # 一次性 flash：本次响应没有新 flash 时清除入站 flash
        if flash is not None and not any(c.startswith(FLASH_COOKIE + "=") for c in resp.set_cookies):
            resp.headers.append(("Set-Cookie", f"{FLASH_COOKIE}=; Path=/; Max-Age=0"))

        start_response(resp.status, resp.headers)
        return [resp.body]

    @staticmethod
    def _read_flash(raw: str | None) -> Flash | None:
        if not raw:
            return None
        try:
            kind, msg = json.loads(_b64d(raw))
        except Exception:
            return None
        if kind in ("ok", "err", "info") and isinstance(msg, str):
            return kind, msg[:300]
        return None

    def html(self, body: str, status: str = "200 OK") -> Response:
        return Response(body, status)

    def bad_csrf(self) -> Response:
        return self.html(render.error_page("会话已过期或请求非法，请返回上一页重试。", "校验失败"), "400 Bad Request")

    def require_staff(self, req: Request) -> Response | None:
        if not req.is_staff:
            return self.html(
                render.error_page("请先使用工作人员口令登录。", "需要工作人员权限"),
                "403 Forbidden",
            )
        return None

    def csrf_token(self, req: Request) -> str:
        """工作人员会话内的 CSRF 令牌（登录时已生成）。"""
        token = (req.session or {}).get("csrf")
        if not token:
            token = _secrets.token_urlsafe(24)
            req.session = {"staff": True, "csrf": token}
            req.session_dirty = True
        return str(token)

    def check_csrf(self, req: Request) -> bool:
        submitted = req.form().get("csrf_token", "")
        expected = (req.session or {}).get("csrf")
        return bool(expected) and constant_time_equals(submitted, str(expected))

    def _anon_session(self, req: Request) -> str:
        """为登录页建立只含 CSRF 的匿名会话并返回令牌。"""
        token = _secrets.token_urlsafe(24)
        req.session = {"csrf": token}
        req.session_dirty = True
        return token

    def _plain_code(self, code_row: Any) -> str | None:
        if code_row is None or code_row["consumed_at"] or code_row["invalidated_at"]:
            return None
        return decrypt_field(self.secret, code_row["code_enc"])

    # ------------------------------------------------------------- 路由

    def dispatch(self, req: Request, flash: Flash | None) -> Response:
        p, m = req.path, req.method

        if p == "/healthz":
            return Response("ok", content_type="text/plain; charset=utf-8")

        if p == "/" and m == "GET":
            return self.html(render.public_list(repository.list_public_items(self.conn()), req.is_staff, flash))

        if p == "/query" and m == "GET":
            token = req.query.get("token", "").strip()
            if not token:
                return self.html(render.query_form(staff=req.is_staff, flash=flash))
            claim = repository.get_claim_by_token(self.conn(), token)
            if claim is None:
                return self.html(render.query_form(token, req.is_staff, ("err", "查询凭证不正确，请核对后重试。")))
            active = repository.get_active_code_for_claim(self.conn(), claim["id"])
            return self.html(render.query_result(claim, active, req.is_staff, plain_code=self._plain_code(active)))

        if m == "GET" and (parts := _match("/items/{id}", p)):
            item = repository.get_item(self.conn(), int(parts[0]))
            if item is None:
                raise LookupError("物品不存在")
            return self.html(render.public_item(item, req.is_staff))

        if m == "POST" and (parts := _match("/items/{id}/claims", p)):
            return self.submit_claim(req, int(parts[0]))

        # ---- 工作人员登录 ----
        if p == "/staff/login" and m == "GET":
            if req.is_staff:
                return Response.redirect("/staff")
            token = self._anon_session(req)
            return self.html(render.login_form(False, token, flash))

        if p == "/staff/login" and m == "POST":
            return self.staff_login(req)

        if p == "/staff/logout" and m in ("GET", "POST"):
            return Response.redirect("/", ("info", "已退出工作人员会话。"), clear_session=True)

        # 工作人员路由统一鉴权；非 /staff 未知路径不受影响（返回 404）
        if p == "/staff" or p.startswith("/staff/"):
            denied = self.require_staff(req)
            if denied is not None:
                return denied

        if p == "/staff" and m == "GET":
            return self.html(render.staff_dashboard(repository.list_items_for_staff(self.conn()), self.csrf_token(req)))

        if p == "/staff/items" and m == "POST":
            return self.create_item(req)

        if m == "GET" and (parts := _match("/staff/items/{id}", p)):
            item = repository.get_item(self.conn(), int(parts[0]))
            if item is None:
                raise LookupError("物品不存在")
            claims = repository.list_claims_for_item(self.conn(), int(parts[0]))
            return self.html(render.staff_item_detail(item, claims, self.csrf_token(req), flash))

        if m == "POST" and (parts := _match("/staff/items/{id}/remove", p)):
            if not self.check_csrf(req):
                return self.bad_csrf()
            iid = int(parts[0])
            repository.remove_item(self.conn(), iid)
            return Response.redirect(f"/staff/items/{iid}", ("ok", "物品已撤下，其待核对申请已标记为未获选。"))

        if m == "POST" and (parts := _match("/staff/items/{id}/restore", p)):
            if not self.check_csrf(req):
                return self.bad_csrf()
            iid = int(parts[0])
            repository.restore_item(self.conn(), iid)
            return Response.redirect(f"/staff/items/{iid}", ("ok", "物品已恢复为保管中。"))

        if m == "GET" and (parts := _match("/staff/claims/{id}", p)):
            return self.staff_claim(req, int(parts[0]), flash)

        if m == "POST" and (parts := _match("/staff/claims/{id}/select", p)):
            return self.select_claim(req, int(parts[0]))

        if m == "POST" and (parts := _match("/staff/claims/{id}/resend", p)):
            return self.resend_code(req, int(parts[0]))

        if p == "/staff/verify" and m == "GET":
            return self.html(render.staff_verify_form(self.csrf_token(req), flash))

        if p == "/staff/verify" and m == "POST":
            return self.verify_code(req)

        return self.html(render.error_page("页面不存在。", "404", req.is_staff), "404 Not Found")

    # ------------------------------------------------------------- 动作

    def submit_claim(self, req: Request, item_id: int) -> Response:
        form = req.form()
        answer = form.get("answer", "").strip()
        contact = form.get("contact", "").strip()
        if not answer or not contact:
            raise ValueError("辨认答案与联系方式均为必填项。")
        if len(answer) > 2000 or len(contact) > 200:
            raise ValueError("填写内容超出长度限制。")
        token = repository.create_claim(self.conn(), item_id, answer, contact)
        return self.html(render.claim_created(item_id, token))

    def staff_login(self, req: Request) -> Response:
        form = req.form()
        expected = (req.session or {}).get("csrf")
        if not expected or not constant_time_equals(form.get("csrf_token", ""), str(expected)):
            token = self._anon_session(req)
            return self.html(render.login_form(False, token, ("err", "会话已过期，请重新提交。")), "400 Bad Request")
        password = form.get("password", "")
        if not constant_time_equals(hash_password(password), self.staff_password_hash):
            time.sleep(0.5)  # 提高在线暴力猜测口令的成本
            token = self._anon_session(req)
            return self.html(render.login_form(False, token, ("err", "口令不正确。")), "401 Unauthorized")
        # 登录成功：升级为工作人员会话并轮换 CSRF
        token = _secrets.token_urlsafe(24)
        req.session = {"staff": True, "csrf": token}
        req.session_dirty = True
        return Response.redirect("/staff", ("ok", "登录成功。"))

    def create_item(self, req: Request) -> Response:
        if not self.check_csrf(req):
            return self.bad_csrf()
        form = req.form()
        public_desc = form.get("public_description", "").strip()
        private_features = form.get("private_features", "").strip()
        if not public_desc or not private_features:
            raise ValueError("公开描述与私密辨认特征均为必填项。")
        if len(public_desc) > 2000 or len(private_features) > 2000:
            raise ValueError("填写内容超出长度限制。")
        item_id = repository.create_item(self.conn(), public_desc, private_features)
        return Response.redirect(f"/staff/items/{item_id}", ("ok", f"物品 #{item_id} 已登记。"))

    def staff_claim(self, req: Request, claim_id: int, flash: Flash | None) -> Response:
        claim = repository.get_claim_with_context(self.conn(), claim_id)
        if claim is None:
            raise LookupError("申请不存在")
        active = repository.get_active_code_for_claim(self.conn(), claim_id)
        return self.html(
            render.staff_claim_detail(
                claim, active, self.csrf_token(req), flash, plain_code=self._plain_code(active)
            )
        )

    def select_claim(self, req: Request, claim_id: int) -> Response:
        if not self.check_csrf(req):
            return self.bad_csrf()
        try:
            repository.select_claim(self.conn(), claim_id, self.code_ttl, self.secret)
        except ValueError as exc:  # 并发落选 / 重复处理等业务冲突
            return Response.redirect(f"/staff/claims/{claim_id}", ("err", str(exc)))
        return Response.redirect(f"/staff/claims/{claim_id}", ("ok", "已选定该申请并签发一次性交还码。"))

    def resend_code(self, req: Request, claim_id: int) -> Response:
        if not self.check_csrf(req):
            return self.bad_csrf()
        try:
            repository.resend_code(self.conn(), claim_id, self.code_ttl, self.secret)
        except ValueError as exc:
            return Response.redirect(f"/staff/claims/{claim_id}", ("err", str(exc)))
        return Response.redirect(f"/staff/claims/{claim_id}", ("ok", "已重发新交还码，旧码立即失效。"))

    def verify_code(self, req: Request) -> Response:
        if not self.check_csrf(req):
            return self.bad_csrf()
        code = req.form().get("code", "").strip()
        if not (len(code) == 8 and code.isdigit()):
            return self.html(render.staff_verify_form(self.csrf_token(req), ("err", "请输入 8 位数字交还码。")))
        result = repository.consume_code(self.conn(), code)
        if result["ok"]:
            return Response.redirect(
                f"/staff/items/{result['item_id']}",
                ("ok", f"核码成功：物品 #{result['item_id']} 已登记交还，申请 #{result['claim_id']} 完成。"),
            )
        return self.html(render.staff_verify_form(self.csrf_token(req), ("err", result["reason"])))


# ----------------------------------------------------------------- HTTP 入口

def run(host: str = "0.0.0.0", port: int | None = None) -> None:
    port = port or int(os.environ.get("PORT", "8080"))
    app = App()
    server = ThreadingHTTPServer((host, port), _make_handler(app))
    print(f"失物认领系统已启动：http://{host}:{port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


def _make_handler(app: App) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_GET(self) -> None:  # noqa: N802
            self._handle()

        def do_POST(self) -> None:  # noqa: N802
            self._handle()

        def _handle(self) -> None:
            parsed = urllib.parse.urlsplit(self.path)
            environ = {
                "REQUEST_METHOD": self.command,
                "PATH_INFO": urllib.parse.unquote(parsed.path),
                "QUERY_STRING": parsed.query,
                "CONTENT_TYPE": self.headers.get("Content-Type", ""),
                "CONTENT_LENGTH": self.headers.get("Content-Length", "0") or "0",
                "HTTP_COOKIE": self.headers.get("Cookie", ""),
                "wsgi.input": self.rfile,
                "wsgi.errors": sys.stderr,
            }
            status_holder: dict[str, Any] = {}

            def start_response(status: str, headers: list[tuple[str, str]]) -> None:
                status_holder["status"] = status
                status_holder["headers"] = headers

            try:
                chunks = list(app(environ, start_response))
            except (BrokenPipeError, ConnectionResetError):
                return
            code = int(status_holder["status"].split(" ", 1)[0])
            self.send_response(code)
            for k, v in status_holder["headers"]:
                self.send_header(k, v)
            self.end_headers()
            for chunk in chunks:
                self.wfile.write(chunk)

        def log_message(self, fmt: str, *args: Any) -> None:
            sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    return Handler


if __name__ == "__main__":
    run()
