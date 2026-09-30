"""HTML 页面渲染：所有动态值经 html.escape 转义，杜绝 XSS。"""
from __future__ import annotations

import html
from datetime import datetime, timedelta, timezone
from typing import Any

from .repository import CLAIM_STATUS_LABELS, ITEM_STATUS_LABELS

_CN = timezone(timedelta(hours=8))


def e(value: Any) -> str:
    return html.escape("" if value is None else str(value), quote=True)


def fmt_time(iso: str | None) -> str:
    """把 UTC ISO 时间显示为北京时间（UTC+8）。"""
    if not iso:
        return "—"
    try:
        dt = datetime.fromisoformat(iso)
    except ValueError:
        return e(iso)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(_CN).strftime("%Y-%m-%d %H:%M") + " (UTC+8)"


def csrf_field(token: str) -> str:
    return f'<input type="hidden" name="csrf_token" value="{e(token)}">'


def item_badge(status: str) -> str:
    return f'<span class="badge badge-{e(status)}">{e(ITEM_STATUS_LABELS.get(status, status))}</span>'


def claim_badge(status: str) -> str:
    return f'<span class="badge badge-{e(status)}">{e(CLAIM_STATUS_LABELS.get(status, status))}</span>'


_STYLE = """
:root{--bg:#f4f6f8;--card:#fff;--ink:#1f2933;--muted:#6b7280;--brand:#1d4ed8;
--brand-bg:#eff6ff;--ok:#047857;--warn:#b45309;--err:#b91c1c;--line:#e5e7eb}
*{box-sizing:border-box}
body{margin:0;font-family:-apple-system,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif;
background:var(--bg);color:var(--ink);line-height:1.6}
a{color:var(--brand);text-decoration:none}
a:hover{text-decoration:underline}
.nav{background:#0f172a;color:#fff;padding:0 20px;display:flex;align-items:center;
gap:18px;height:54px;flex-wrap:wrap}
.nav .brand{font-weight:700;font-size:17px;color:#fff}
.nav a{color:#cbd5e1}
.nav a:hover{color:#fff;text-decoration:none}
.nav .spacer{flex:1}
.wrap{max-width:900px;margin:26px auto;padding:0 16px}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;
padding:20px 22px;margin-bottom:18px}
h1{font-size:22px;margin:0 0 14px}
h2{font-size:17px;margin:0 0 10px}
.muted{color:var(--muted);font-size:13px}
.badge{display:inline-block;padding:2px 10px;border-radius:999px;font-size:12px;font-weight:600}
.badge-kept,.badge-selected{background:#dcfce7;color:var(--ok)}
.badge-pending{background:#fef9c3;color:#854d0e}
.badge-removed,.badge-rejected{background:#fee2e2;color:var(--err)}
.badge-returned{background:#dbeafe;color:#1d4ed8}
table{width:100%;border-collapse:collapse;font-size:14px}
th,td{border-bottom:1px solid var(--line);padding:9px 8px;text-align:left;vertical-align:top}
th{color:var(--muted);font-weight:600;font-size:12px}
input[type=text],input[type=password],input[type=email],textarea{
width:100%;padding:9px 11px;border:1px solid var(--line);border-radius:7px;
font:inherit;font-size:14px;background:#fff}
textarea{min-height:74px;resize:vertical}
label{display:block;font-weight:600;margin:12px 0 4px;font-size:14px}
.btn{display:inline-block;border:0;border-radius:7px;padding:9px 18px;font:inherit;
font-size:14px;font-weight:600;cursor:pointer;background:var(--brand);color:#fff}
.btn:hover{text-decoration:none;filter:brightness(1.08);color:#fff}
.btn.secondary{background:#e5e7eb;color:#1f2933}
.btn.danger{background:var(--err)}
.btn.small{padding:5px 12px;font-size:13px}
.row{display:flex;gap:10px;flex-wrap:wrap;align-items:center;margin-top:16px}
.flash{border-radius:8px;padding:10px 14px;margin-bottom:16px;font-size:14px}
.flash-ok{background:#dcfce7;color:var(--ok);border:1px solid #86efac}
.flash-err{background:#fee2e2;color:var(--err);border:1px solid #fca5a5}
.flash-info{background:var(--brand-bg);color:var(--brand);border:1px solid #bfdbfe}
.tokenbox{background:#0f172a;color:#7ef0c8;font-family:ui-monospace,Menlo,Consolas,monospace;
font-size:15px;padding:12px 14px;border-radius:8px;word-break:break-all;user-select:all}
.codebox{font-family:ui-monospace,Menlo,Consolas,monospace;font-size:30px;letter-spacing:8px;
font-weight:700;background:#f0fdf4;border:1px dashed #86efac;border-radius:8px;
padding:10px;text-align:center;color:var(--ok)}
.private{background:#fffbeb;border:1px solid #fcd34d;border-radius:8px;padding:12px 14px}
dl.info{margin:0;font-size:14px}
dl.info dt{color:var(--muted);font-size:12px;margin-top:8px}
dl.info dd{margin:2px 0 0}
"""


def _nav(staff: bool) -> str:
    links = (
        '<a href="/">公开列表</a>'
        '<a href="/query">查询认领进度</a>'
        '<span class="spacer"></span>'
    )
    if staff:
        links += (
            '<a href="/staff">登记台</a>'
            '<a href="/staff/verify">核销交还码</a>'
            '<a href="/staff/logout">退出</a>'
        )
    else:
        links += '<a href="/staff/login">工作人员入口</a>'
    return f'<nav class="nav"><span class="brand">失物认领 · 场馆服务台</span>{links}</nav>'


def page(
    title: str,
    content: str,
    *,
    staff: bool = False,
    flash: tuple[str, str] | None = None,
) -> str:
    flash_html = ""
    if flash:
        kind, msg = flash
        flash_html = f'<div class="flash flash-{e(kind)}">{e(msg)}</div>'
    return (
        "<!doctype html><html lang=\"zh-CN\"><head><meta charset=\"utf-8\">"
        "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
        f"<title>{e(title)} - 失物认领</title><style>{_STYLE}</style></head>"
        f"<body>{_nav(staff)}<main class=\"wrap\">{flash_html}{content}</main></body></html>"
    )


def error_page(message: str, status_text: str, staff: bool = False) -> str:
    body = (
        f'<div class="card"><h1>{e(status_text)}</h1><p>{e(message)}</p>'
        '<div class="row"><a class="btn" href="/">返回公开列表</a></div></div>'
    )
    return page(status_text, body, staff=staff)


# ----------------------------------------------------------------- 公开页

def public_list(items: list[Any], staff: bool, flash: tuple[str, str] | None = None) -> str:
    selected_tag = '<span class="badge badge-selected">已有人获选</span>'
    if items:
        rows = "".join(
            '<tr><td><a href="/items/%d">%s</a>%s<div class="muted">登记时间 %s</div></td></tr>'
            % (
                it["id"],
                e(it["public_description"]),
                (" " + selected_tag) if it["has_selected"] else "",
                fmt_time(it["created_at"]),
            )
            for it in items
        )
        inner = f"<table><tbody>{rows}</tbody></table>"
    else:
        inner = '<p class="muted">目前没有正在保管的物品。</p>'
    content = f'<div class="card"><h1>保管中的失物</h1>{inner}</div>'
    return page("保管中的失物", content, staff=staff, flash=flash)


def public_item(item: Any, staff: bool) -> str:
    closed = item["status"] != "kept"
    notice = ""
    if closed:
        notice = '<div class="flash flash-info">该物品已撤下或已交还，不再接受新的认领申请。</div>'
    form = (
        '<form method="post" action="/items/'
        + str(item["id"])
        + '/claims">'
        "<p class=\"muted\">只有回答与物品私密特征相符才可能获选。请只描述你知道的细节。</p>"
        '<label for="answer">辨认答案（不公开，仅工作人员可见）</label>'
        '<textarea id="answer" name="answer" required maxlength="2000" placeholder="例如：物品内的刻字、挂件颜色、开机壁纸…"></textarea>'
        '<label for="contact">联系方式（手机/微信/邮箱，仅工作人员可见）</label>'
        '<input id="contact" name="contact" type="text" required maxlength="200">'
        '<div class="row"><button class="btn" type="submit">提交认领</button>'
        '<a class="btn secondary" href="/">返回列表</a></div></form>'
    )
    body = (
        f'<div class="card"><h1>物品详情</h1>'
        f'<p style="font-size:16px">{e(item["public_description"])}</p>'
        f'<p class="muted">登记时间 {fmt_time(item["created_at"])} {item_badge(item["status"])}</p>'
        f"{notice}{'' if not closed else ''}{form if not closed else ''}</div>"
    )
    return page("物品详情", body, staff=staff)


def claim_created(item_id: int, token: str) -> str:
    body = f"""
    <div class="card">
      <h1>认领已提交，请妥善保存查询凭证</h1>
      <div class="flash flash-err">此凭证<strong>只显示这一次</strong>，系统不保存明文。
      凭它可查询本人认领进度，遗失后无法找回。</div>
      <div class="tokenbox">{e(token)}</div>
      <div class="row">
        <a class="btn" href="/query?token={e(token)}">用此凭证查询进度</a>
        <a class="btn secondary" href="/items/{item_id}">返回物品</a>
      </div>
    </div>"""
    return page("认领已提交", body)


def query_form(token: str = "", staff: bool = False, flash: tuple[str, str] | None = None) -> str:
    body = f"""
    <div class="card">
      <h1>查询我的认领进度</h1>
      <form method="get" action="/query">
        <label for="token">查询凭证</label>
        <input id="token" name="token" value="{e(token)}" required placeholder="提交认领时获得的一次性查询凭证">
        <div class="row"><button class="btn" type="submit">查询</button></div>
      </form>
      <p class="muted">凭证仅能查到你本人提交的那份申请；私密特征、他人答案与联系方式均不会显示。</p>
    </div>"""
    return page("查询认领进度", body, staff=staff, flash=flash)


def query_result(
    claim: Any, active_code: Any, staff: bool, *, plain_code: str | None = None
) -> str:
    status = claim["status"]
    lines = [
        f"<dt>认领物品（公开描述）</dt><dd>{e(claim['item_description'])}</dd>",
        f"<dt>当前状态</dt><dd>{claim_badge(status)}</dd>",
        f"<dt>提交时间</dt><dd>{fmt_time(claim['created_at'])}</dd>",
    ]
    extra = ""
    if status == "selected":
        if active_code is not None and not active_code["consumed_at"] and not active_code["invalidated_at"]:
            expires = active_code["expires_at"]
            if expires <= _now_iso():
                extra += (
                    '<div class="card"><h1>交还码已过期</h1>'
                    f'<p class="muted">原有效期至 {fmt_time(expires)}，'
                    "请联系工作人员重发新码；新码签发后旧码立即失效。</p></div>"
                )
            elif plain_code:
                extra += (
                    '<div class="card"><h1>一次性交还码</h1>'
                    '<p class="muted">向工作人员出示此码，由工作人员核码后完成交还。</p>'
                    f'<div class="codebox">{e(plain_code)}</div>'
                    f'<p class="muted">有效期至 {fmt_time(expires)}，过期可联系工作人员重发；'
                    "重发后旧码立即失效。</p></div>"
                )
            else:
                extra += (
                    '<div class="card"><h1>一次性交还码状态异常</h1>'
                    '<p class="muted">无法解密交还码，请联系工作人员重发。</p></div>'
                )
        else:
            extra += (
                '<div class="card"><h1>交还码当前不可用</h1>'
                '<p>现有交还码已使用、已过期或已被重发作废旧码，请联系工作人员重发新码。</p></div>'
            )
    elif status == "rejected":
        if claim["item_status"] != "kept":
            tip = "该物品已撤下或已交还。"
        else:
            tip = "工作人员已选定其他获选人，本次申请未获选。"
        lines.append(f"<dt>说明</dt><dd>{e(tip)}</dd>")
    body = (
        f'<div class="card"><h1>我的认领进度</h1><dl class="info">{"".join(lines)}</dl>'
        '<div class="row"><a class="btn secondary" href="/query">再查一个凭证</a></div></div>'
        + extra
    )
    return page("认领进度", body, staff=staff)


# ----------------------------------------------------------------- 工作人员

def login_form(staff: bool, csrf_token: str, flash: tuple[str, str] | None = None) -> str:
    body = f"""
    <div class="card">
      <h1>工作人员登录</h1>
      <form method="post" action="/staff/login">
        {csrf_field(csrf_token)}
        <label for="password">配置口令</label>
        <input id="password" name="password" type="password" required autocomplete="current-password">
        <div class="row"><button class="btn" type="submit">登录</button></div>
      </form>
    </div>"""
    return page("工作人员登录", body, staff=staff, flash=flash)


def staff_dashboard(items: list[Any], csrf_token: str) -> str:
    rows = "".join(
        "<tr><td>"
        f'<a href="/staff/items/{it["id"]}">#{it["id"]} {e(it["public_description"])}</a>'
        f'</td><td>{item_badge(it["status"])}</td>'
        f'<td>{it["pending_count"]} / {it["claim_count"]}</td>'
        f'<td>{fmt_time(it["created_at"])}</td></tr>'
        for it in items
    ) or '<tr><td colspan="4" class="muted">还没有登记物品。</td></tr>'
    body = f"""
    <div class="card">
      <h1>登记新物品</h1>
      <form method="post" action="/staff/items">
        {csrf_field(csrf_token)}
        <label for="public_description">公开描述（访客可见）</label>
        <textarea id="public_description" name="public_description" required maxlength="2000"
          placeholder="例如：黑色双肩包，于 9 月 28 日在 B 区更衣室外拾获"></textarea>
        <label for="private_features">私密辨认特征（仅工作人员可见）</label>
        <textarea id="private_features" name="private_features" required maxlength="2000"
          placeholder="不公开的核验细节，例如：内袋有姓名贴、挂件、夹层位置"></textarea>
        <div class="row"><button class="btn" type="submit">登记物品</button></div>
      </form>
    </div>
    <div class="card">
      <h1>物品与认领总览（待核对 / 总申请）</h1>
      <table><thead><tr><th>物品</th><th>保管状态</th><th>申请数</th><th>登记时间</th></tr></thead>
      <tbody>{rows}</tbody></table>
    </div>
    <div class="card"><h1>当面交还</h1>
      <p class="muted">获选人出示一次性交还码后，到
      <a href="/staff/verify">核销交还码</a>页面完成交还登记。</p></div>"""
    return page("工作人员登记台", body, staff=True)


def staff_item_detail(
    item: Any, claims: list[Any], csrf_token: str, flash: tuple[str, str] | None = None
) -> str:
    actions = ""
    if item["status"] == "kept":
        actions = (
            f'<form method="post" action="/staff/items/{item["id"]}/remove" '
            'onsubmit="return confirm(\'撤下后该物品将不再接受新申请，待核对申请会被拒绝。确定？\')">'
            f'{csrf_field(csrf_token)}<button class="btn danger" type="submit">撤下物品</button></form>'
        )
    elif item["status"] == "removed":
        actions = (
            f'<form method="post" action="/staff/items/{item["id"]}/restore">'
            f'{csrf_field(csrf_token)}<button class="btn secondary" type="submit">恢复为保管中</button></form>'
        )
    rows = ""
    for c in claims:
        rows += (
            "<tr>"
            f'<td><a href="/staff/claims/{c["id"]}">#{c["id"]}</a></td>'
            f"<td>{claim_badge(c['status'])}</td>"
            f"<td>{e(c['answer'])}</td>"
            f"<td>{e(c['contact'])}</td>"
            f"<td>{fmt_time(c['created_at'])}</td>"
            "</tr>"
        )
    rows = rows or '<tr><td colspan="5" class="muted">暂无认领申请。</td></tr>'
    body = f"""
    <div class="card">
      <h1>物品 #{item['id']} {item_badge(item['status'])}</h1>
      <dl class="info">
        <dt>公开描述</dt><dd>{e(item['public_description'])}</dd>
        <dt>私密辨认特征</dt><dd><div class="private">{e(item['private_features'])}</div></dd>
        <dt>登记时间</dt><dd>{fmt_time(item['created_at'])}</dd>
      </dl>
      <div class="row">{actions}<a class="btn secondary" href="/staff">返回登记台</a></div>
    </div>
    <div class="card">
      <h1>认领申请（含私密答案与联系方式，仅工作人员可见）</h1>
      <table><thead><tr><th>申请</th><th>状态</th><th>辨认答案</th><th>联系方式</th><th>提交时间</th></tr></thead>
      <tbody>{rows}</tbody></table>
    </div>"""
    return page(f"物品 #{item['id']}", body, staff=True, flash=flash)


def staff_claim_detail(
    claim: Any,
    active_code: Any,
    csrf_token: str,
    flash: tuple[str, str] | None = None,
    *,
    plain_code: str | None = None,
) -> str:
    select_btn = ""
    if claim["status"] == "pending" and claim["item_status"] == "kept":
        select_btn = (
            f'<form method="post" action="/staff/claims/{claim["id"]}/select" '
            'onsubmit="return confirm(\'选定后其他申请将标记为未获选，并生成一次性交还码。确定？\')">'
            f'{csrf_field(csrf_token)}<button class="btn" type="submit">选定此申请并签发交还码</button></form>'
        )
    code_panel = "<p class=\"muted\">尚未选定，暂无交还码。</p>"
    if claim["status"] == "selected":
        code_panel = _code_panel(claim, active_code, csrf_token, plain_code)
    body = f"""
    <div class="card">
      <h1>申请 #{claim['id']} {claim_badge(claim['status'])}</h1>
      <dl class="info">
        <dt>所属物品</dt>
        <dd><a href="/staff/items/{claim['item_id']}">#{claim['item_id']} {e(claim['public_description'])}</a>
        {item_badge(claim['item_status'])}</dd>
        <dt>辨认答案</dt><dd>{e(claim['answer'])}</dd>
        <dt>联系方式</dt><dd>{e(claim['contact'])}</dd>
        <dt>提交时间</dt><dd>{fmt_time(claim['created_at'])}</dd>
        <dt>处理时间</dt><dd>{fmt_time(claim['decided_at'])}</dd>
      </dl>
      <div class="row">{select_btn}<a class="btn secondary" href="/staff/items/{claim['item_id']}">返回物品</a></div>
    </div>
    <div class="card"><h1>一次性交还码</h1>{code_panel}</div>"""
    return page(f"申请 #{claim['id']}", body, staff=True, flash=flash)


def _code_panel(claim: Any, active_code: Any, csrf_token: str, plain_code: str | None) -> str:
    resend = (
        f'<form method="post" action="/staff/claims/{claim["id"]}/resend" '
        'onsubmit="return confirm(\'重发后旧码立即失效。确定？\')">'
        f'{csrf_field(csrf_token)}<button class="btn secondary" type="submit">重发新码（旧码失效）</button></form>'
    )
    if claim["item_status"] == "returned":
        return "<p>物品已登记交还。</p>"
    if active_code is None:
        return '<p class="muted">没有有效交还码，可重发一枚新码。</p>' + resend
    if active_code["consumed_at"]:
        return f"<p>交还码已于 {fmt_time(active_code['consumed_at'])} 核销，物品完成交还。</p>"
    if active_code["invalidated_at"]:
        return "<p>该码已被重发作废旧码。</p>" + resend
    expired = active_code["expires_at"] <= _now_iso()
    if plain_code and not expired:
        shown = f'<div class="codebox">{e(plain_code)}</div>'
        note = f"有效期至 {fmt_time(active_code['expires_at'])}。获选人也可凭查询凭证在查询页查看。"
    elif plain_code and expired:
        shown = '<div class="flash flash-err">当前交还码已过期，重发后旧码立即失效。</div>'
        note = f"原有效期至 {fmt_time(active_code['expires_at'])}。"
    else:
        shown = '<div class="flash flash-err">交还码无法解密，请重发新码。</div>'
        note = ""
    return shown + f"<p class=\"muted\">{note}</p><div class=\"row\">{resend}</div>"


def _now_iso() -> str:
    from .repository import now_iso

    return now_iso()


def staff_verify_form(csrf_token: str, flash: tuple[str, str] | None) -> str:
    body = f"""
    <div class="card">
      <h1>核销一次性交还码</h1>
      <form method="post" action="/staff/verify">
        {csrf_field(csrf_token)}
        <label for="code">获选人出示的 8 位交还码</label>
        <input id="code" name="code" type="text" inputmode="numeric" pattern="[0-9]{{8}}"
          maxlength="8" required placeholder="例如 04821357" style="font-family:monospace;letter-spacing:4px">
        <div class="row"><button class="btn" type="submit">核码并登记交还</button>
        <a class="btn secondary" href="/staff">返回登记台</a></div>
      </form>
      <p class="muted">核销成功后物品立即标记为「已交还」并拒绝一切新申请；
      重复核码、过期码、被重发作废旧码均会被拒绝。</p>
    </div>"""
    return page("核销交还码", body, staff=True, flash=flash)
