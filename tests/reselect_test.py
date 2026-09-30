"""改判（误选更正）端到端 + 并发测试（标准库）。

前置：服务已运行在 http://127.0.0.1:8181，且使用全新数据目录
（STAFF_PASSWORD=secret123 LF_DATA_DIR=/tmp/lfdata）。

覆盖：
  - 正常改判：原申请落选、目标获选、旧码失效、新码签发、双方凭证隔离、更正记录；
  - 拒绝项：理由空、目标=原申请、目标跨物品、目标不存在、CSRF 伪造、非工作人员；
  - 链式改判、改判与已交还/已撤下冲突；
  - 并发：改判×改判、改判×重发，至多一次状态变更生效。
"""
import http.cookiejar
import re
import sqlite3
import threading
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor

BASE = "http://127.0.0.1:8181"
PASS = 0
FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}  {detail}")


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


def client():
    cj = http.cookiejar.CookieJar()
    return cj, urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))


def _jar_of(op):
    for h in op.handlers:
        if isinstance(h, urllib.request.HTTPCookieProcessor):
            return h.cookiejar
    return http.cookiejar.CookieJar()


def req(op, method, path, form=None, raw=False):
    data = urllib.parse.urlencode(form).encode() if form is not None else None
    r = urllib.request.Request(BASE + path, data=data, method=method)
    try:
        if raw:
            opener = urllib.request.build_opener(
                NoRedirect, urllib.request.HTTPCookieProcessor(_jar_of(op))
            )
            resp = opener.open(r)
            return resp.status, resp.read().decode(), resp.headers.get("Location", "")
        resp = op.open(r)
        return resp.status, resp.read().decode(), resp.headers.get("Location", "")
    except urllib.error.HTTPError as e:
        body = e.read().decode()
        return e.code, body, e.headers.get("Location", "")


def raw_with_cookie(cookie_header, path, form):
    data = urllib.parse.urlencode(form).encode()
    r = urllib.request.Request(BASE + path, data=data, method="POST")
    r.add_header("Cookie", cookie_header)
    try:
        resp = urllib.request.build_opener(NoRedirect).open(r)
        return resp.status, resp.headers.get("Location", "")
    except urllib.error.HTTPError as e:
        return e.code, e.headers.get("Location", "")


def csrf(htmltext):
    m = re.search(r'name="csrf_token" value="([^"]+)"', htmltext)
    return m.group(1) if m else None


def claim_tokens_for(body):
    return [int(x) for x in re.findall(r'/staff/claims/(\d+)', body)]


def submit_claim(answer, contact):
    _, v = client()
    st, body, _ = req(v, "POST", "/items/1/claims", form={"answer": answer, "contact": contact})
    assert st == 200 and "查询凭证" in body, (st, body[:200])
    m = re.search(r'<div class="tokenbox">([^<]+)</div>', body)
    return m.group(1).strip()


def code_in_query(token):
    _, v = client()
    _, body, _ = req(v, "GET", f"/query?token={urllib.parse.quote(token)}")
    m = re.search(r'<div class="codebox">(\d{8})</div>', body)
    return (m.group(1) if m else None), body


db = sqlite3.connect("/tmp/lfdata/lostfound.db")

# --------------------------------------------------------------- 0 登录 + 物品
print("== 准备：工作人员登录并登记物品 ==")
sjar, staff = client()
_, body, _ = req(staff, "GET", "/staff/login")
tok = csrf(body)
st, _, loc = req(staff, "POST", "/staff/login",
                 form={"csrf_token": tok, "password": "secret123"}, raw=True)
check("工作人员登录(303)", st == 303 and loc == "/staff")
_, dash, _ = req(staff, "GET", "/staff")
SCSRF = csrf(dash)

st, _, loc = req(staff, "POST", "/staff/items",
                 form={"csrf_token": SCSRF, "public_description": "改判测试-黑色钱包",
                       "private_features": "内有张三名片"}, raw=True)
check("登记物品1(303)", st == 303 and loc == "/staff/items/1")
# 跨物品拒绝用：物品2 + 一份待核对申请
req(staff, "POST", "/staff/items",
    form={"csrf_token": SCSRF, "public_description": "另一物品-雨伞",
          "private_features": "木柄"})
_, v2 = client()
_, body2, _ = req(v2, "POST", "/items/2/claims", form={"answer": "雨伞答案", "contact": "u2"})
other_claim = db.execute("SELECT id FROM claims WHERE item_id=2").fetchone()[0]

# --------------------------------------------------------------- 1 三份申请并选定甲
print("== 选定甲，准备改判 ==")
tA = submit_claim("甲-说有张三名片", "contact-A")
tB = submit_claim("乙-答案更准确", "contact-B")
tC = submit_claim("丙-也答对了", "contact-C")
_, detail, _ = req(staff, "GET", "/staff/items/1")
ids = claim_tokens_for(detail)
check("物品1有3份申请", len(ids) == 3, str(ids))
cA, cB, cC = ids

st, _, loc = req(staff, "POST", f"/staff/claims/{cA}/select",
                 form={"csrf_token": SCSRF}, raw=True)
check("选定甲(303)", st == 303 and loc == f"/staff/claims/{cA}")
old_code, qA = code_in_query(tA)
check("甲取得初始交还码", bool(old_code))

_, pageA, _ = req(staff, "GET", f"/staff/claims/{cA}")
check("甲申请页有改判表单", f'/staff/claims/{cA}/reselect' in pageA)
check("改判目标含乙", f'value="{cB}"' in pageA)
check("改判目标含丙", f'value="{cC}"' in pageA)
check("改判目标不含甲自己", f'<option value="{cA}"' not in pageA)
check("改判理由为必填字段", 'name="reason" required' in pageA)

# --------------------------------------------------------------- 2 拒绝项
print("== 改判前置校验：非法请求一律拒绝且不改状态 ==")
st, _, loc = req(staff, "POST", f"/staff/claims/{cA}/reselect",
                 form={"csrf_token": "forged", "target_claim_id": str(cB), "reason": "x"}, raw=True)
check("伪造CSRF改判被拒(400)", st == 400)

st, _, loc = req(staff, "POST", f"/staff/claims/{cA}/reselect",
                 form={"csrf_token": SCSRF, "target_claim_id": str(cB), "reason": "   "}, raw=True)
check("理由为空被拒(303回原页)", st == 303 and loc == f"/staff/claims/{cA}")
_, follow, _ = req(staff, "GET", loc)
check("理由为空有错误提示", "更正理由不能为空" in follow)

st, _, loc = req(staff, "POST", f"/staff/claims/{cA}/reselect",
                 form={"csrf_token": SCSRF, "target_claim_id": str(cA), "reason": "改自己"}, raw=True)
check("目标=原申请被拒", st == 303 and loc == f"/staff/claims/{cA}")

st, _, loc = req(staff, "POST", f"/staff/claims/{cA}/reselect",
                 form={"csrf_token": SCSRF, "target_claim_id": str(other_claim),
                       "reason": "跨物品改判"}, raw=True)
check("目标不属同一物品被拒", st == 303 and "不属于同一物品" in req(staff, "GET", loc)[1])

st, _, loc = req(staff, "POST", f"/staff/claims/{cA}/reselect",
                 form={"csrf_token": SCSRF, "target_claim_id": "999999", "reason": "不存在"}, raw=True)
check("目标不存在被拒", st == 303)

st, _, _ = req(staff, "POST", f"/staff/claims/{cA}/reselect",
               form={"csrf_token": SCSRF, "target_claim_id": "abc", "reason": "x"}, raw=True)
check("目标非数字被拒", st == 303)

sel = db.execute("SELECT status FROM claims WHERE id=?", (cA,)).fetchone()[0]
n_sel = db.execute("SELECT COUNT(*) FROM claims WHERE item_id=1 AND status='selected'").fetchone()[0]
check("拒绝后甲仍获选、无状态变化", sel == "selected" and n_sel == 1)
active_before = db.execute(
    "SELECT COUNT(*) FROM handover_codes WHERE claim_id=? AND consumed_at IS NULL"
    " AND invalidated_at IS NULL", (cA,)).fetchone()[0]
check("拒绝后旧码仍有效", active_before == 1)

# 非工作人员不可访问改判动作
_, stranger = client()
st, _, _ = req(stranger, "POST", f"/staff/claims/{cA}/reselect",
               form={"csrf_token": "x", "target_claim_id": str(cB), "reason": "x"}, raw=True)
check("访客改判被拒(403)", st == 403)
st, _, _ = req(stranger, "GET", f"/staff/claims/{cA}")
check("访客看不到工作人员申请页(403)", st == 403)

# --------------------------------------------------------------- 3 成功改判 甲→乙
print("== 改判 甲 → 乙 ==")
REASON1 = "更正理由-误选测试-甲的细节与私密特征不符，改判给乙"
st, _, loc = req(staff, "POST", f"/staff/claims/{cA}/reselect",
                 form={"csrf_token": SCSRF, "target_claim_id": str(cB), "reason": REASON1}, raw=True)
check("改判成功跳转到乙申请页(303)", st == 303 and loc == f"/staff/claims/{cB}", loc)
_, target_page, _ = req(staff, "GET", loc)
check("乙申请页显示已获选徽章", "已获选" in target_page)
new_code_m = re.search(r'<div class="codebox">(\d{8})</div>', target_page)
check("工作人员在乙页看到新交还码", bool(new_code_m))
staff_seen_new = new_code_m.group(1) if new_code_m else ""

rows = db.execute("SELECT id, status FROM claims WHERE item_id=1 ORDER BY id").fetchall()
check("DB: 甲=rejected 乙=selected 丙=rejected",
      dict(rows) == {cA: "rejected", cB: "selected", cC: "rejected"}, str(rows))

inv = db.execute(
    "SELECT COUNT(*) FROM handover_codes WHERE claim_id=? AND invalidated_at IS NOT NULL",
    (cA,)).fetchone()[0]
check("甲的旧码已标记失效", inv == 1)
n_new = db.execute(
    "SELECT COUNT(*) FROM handover_codes WHERE claim_id=? AND consumed_at IS NULL"
    " AND invalidated_at IS NULL", (cB,)).fetchone()[0]
check("乙名下恰有1枚有效新码", n_new == 1)

codeA_now, qA2 = code_in_query(tA)
check("甲凭证显示未获选", "未获选" in qA2)
check("甲凭证不再显示任何交还码", codeA_now is None and 'class="codebox"' not in qA2)
check("甲页看不到乙的联系方式", "contact-B" not in qA2)

codeB_now, qB = code_in_query(tB)
check("乙凭证显示已获选", "已获选" in qB)
check("乙凭证显示新8位码且与旧码不同", bool(codeB_now) and codeB_now != old_code)
check("乙看到的新码与工作人员一致", codeB_now == staff_seen_new)
check("乙页看不到甲/丙的联系方式", "contact-A" not in qB and "contact-C" not in qB)

_, qC = code_in_query(tC)
check("丙凭证仍未获选且无码", "未获选" in qC and 'class="codebox"' not in qC)

# 旧码立即无法核销
st, vpage, _ = req(staff, "GET", "/staff/verify")
vcsrf = csrf(vpage)
st, body, _ = req(staff, "POST", "/staff/verify",
                  form={"csrf_token": vcsrf, "code": old_code})
check("甲旧码核销被拒(已失效)", st == 200 and "已失效" in body)

# 更正记录可追溯
_, idetail, _ = req(staff, "GET", "/staff/items/1")
check("物品页出现改判记录区", "改判记录" in idetail)
check("改判记录含更正理由", REASON1 in idetail)
check("改判记录含双方申请编号", f"/staff/claims/{cA}" in idetail and f"/staff/claims/{cB}" in idetail)
cc = db.execute("SELECT from_claim_id, to_claim_id, reason FROM claim_corrections WHERE item_id=1").fetchall()
check("DB 改判记录=1条且双方/理由正确",
      len(cc) == 1 and tuple(cc[0]) == (cA, cB, REASON1), str(cc))

# 公开页不泄露改判理由与新码
_, vpub = client()
_, pub_list, _ = req(vpub, "GET", "/")
_, pub_item, _ = req(vpub, "GET", "/items/1")
check("公开页不泄露改判理由", REASON1 not in pub_list and REASON1 not in pub_item)
check("公开页不泄露新交还码", staff_seen_new not in pub_list and staff_seen_new not in pub_item)

# 甲已落选：其申请页不再出现改判表单，重发也被拒
_, pageA_after, _ = req(staff, "GET", f"/staff/claims/{cA}")
check("落选申请页无改判表单", f'/staff/claims/{cA}/reselect' not in pageA_after)
st, _, loc = req(staff, "POST", f"/staff/claims/{cA}/reselect",
                 form={"csrf_token": SCSRF, "target_claim_id": str(cB), "reason": "再改一次"}, raw=True)
check("对已落选的甲再次改判被拒", "不是已获选" in req(staff, "GET", loc)[1])

# --------------------------------------------------------------- 4 链式改判 乙→丙
print("== 链式改判 乙 → 丙 ==")
REASON2 = "更正理由-二次核对，丙才是物主"
st, _, loc = req(staff, "POST", f"/staff/claims/{cB}/reselect",
                 form={"csrf_token": SCSRF, "target_claim_id": str(cC), "reason": REASON2}, raw=True)
check("第二次改判成功", st == 303 and loc == f"/staff/claims/{cC}")
rows = db.execute("SELECT id, status FROM claims WHERE item_id=1 ORDER BY id").fetchall()
check("DB: 乙=rejected 丙=selected", dict(rows)[cB] == "rejected" and dict(rows)[cC] == "selected")
invB = db.execute(
    "SELECT COUNT(*) FROM handover_codes WHERE claim_id=? AND invalidated_at IS NOT NULL",
    (cB,)).fetchone()[0]
check("乙的新码在链式改判后也失效", invB == 1)
codeC_now, qC2 = code_in_query(tC)
check("丙凭证显示新码", "已获选" in qC2 and bool(codeC_now))
check("丙新码不同于乙的码", codeC_now != codeB_now)
_, qB2 = code_in_query(tB)
check("乙凭证变为未获选且无码", "未获选" in qB2 and 'class="codebox"' not in qB2)
n_cc = db.execute("SELECT COUNT(*) FROM claim_corrections WHERE item_id=1").fetchone()[0]
check("改判记录累计2条", n_cc == 2)

# 物品仍保管时可用；核码交还后拒绝改判
st, vpage, _ = req(staff, "GET", "/staff/verify")
vcsrf = csrf(vpage)
st, _, loc = req(staff, "POST", "/staff/verify",
                 form={"csrf_token": vcsrf, "code": codeC_now}, raw=True)
check("丙的新码核销成功", st == 303 and loc == "/staff/items/1")
check("物品1已交还", db.execute("SELECT status FROM items WHERE id=1").fetchone()[0] == "returned")
st, _, loc = req(staff, "POST", f"/staff/claims/{cC}/reselect",
                 form={"csrf_token": SCSRF, "target_claim_id": str(cA), "reason": "交还后改判"}, raw=True)
check("已交还物品改判被拒", st == 303 and "已撤下或已交还" in req(staff, "GET", loc)[1])
after = db.execute("SELECT COUNT(*) FROM claim_corrections WHERE item_id=1").fetchone()[0]
check("被拒后改判记录不增加", after == 2)
_, pageC_ret, _ = req(staff, "GET", f"/staff/claims/{cC}")
check("已交还申请页无改判表单", f'/staff/claims/{cC}/reselect' not in pageC_ret)

# --------------------------------------------------------------- 5 已撤下物品拒绝改判
print("== 已撤下物品拒绝改判 ==")
# 物品2 已有一份待核对申请，再补一份并选定第一份
_, vx = client()
req(vx, "POST", "/items/2/claims", form={"answer": "第二份", "contact": "u2b"})
c2a, c2b = [r[0] for r in db.execute("SELECT id FROM claims WHERE item_id=2 ORDER BY id").fetchall()]
req(staff, "POST", f"/staff/claims/{c2a}/select", form={"csrf_token": SCSRF})
req(staff, "POST", "/staff/items/2/remove", form={"csrf_token": SCSRF})
st, _, loc = req(staff, "POST", f"/staff/claims/{c2a}/reselect",
                 form={"csrf_token": SCSRF, "target_claim_id": str(c2b), "reason": "撤下后改判"}, raw=True)
check("撤下后改判被拒", st == 303 and "已撤下或已交还" in req(staff, "GET", loc)[1])
check("撤下后状态不变",
      db.execute("SELECT status FROM claims WHERE id=?", (c2a,)).fetchone()[0] == "selected")

# --------------------------------------------------------------- 6 并发：改判 × 改判
print("== 并发：同一获选申请两个改判目标，至多一次生效 ==")
req(staff, "POST", "/staff/items",
    form={"csrf_token": SCSRF, "public_description": "并发改判-手表", "private_features": "表带刻字LF"})
iid = db.execute("SELECT MAX(id) FROM items").fetchone()[0]
toks = []
for ans, con in [("并发-甲", "pA"), ("并发-乙", "pB"), ("并发-丙", "pC")]:
    _, vv = client()
    _, b, _ = req(vv, "POST", f"/items/{iid}/claims", form={"answer": ans, "contact": con})
    toks.append(re.search(r'<div class="tokenbox">([^<]+)</div>', b).group(1).strip())
x, y, z = [r[0] for r in db.execute(
    "SELECT id FROM claims WHERE item_id=? ORDER BY id", (iid,)).fetchall()]
req(staff, "POST", f"/staff/claims/{x}/select", form={"csrf_token": SCSRF})
cookie_header = "; ".join(f"{c.name}={c.value}" for c in sjar)
barrier = threading.Barrier(2)


def corr_task(target):
    barrier.wait()
    return raw_with_cookie(cookie_header, f"/staff/claims/{x}/reselect",
                           {"csrf_token": SCSRF, "target_claim_id": str(target),
                            "reason": f"并发改判给{target}"})


with ThreadPoolExecutor(max_workers=2) as ex:
    r_xy, r_xz = [f.result() for f in [ex.submit(corr_task, y), ex.submit(corr_task, z)]]
success_locs = [loc for code, loc in (r_xy, r_xz) if code == 303 and loc in (f"/staff/claims/{y}", f"/staff/claims/{z}")]
back_locs = [loc for code, loc in (r_xy, r_xz) if code == 303 and loc == f"/staff/claims/{x}"]
check("并发改判：恰好1个跳转到目标页、1个退回原页",
      len(success_locs) == 1 and len(back_locs) == 1, f"{r_xy} {r_xz}")
n_sel = db.execute("SELECT COUNT(*) FROM claims WHERE item_id=? AND status='selected'", (iid,)).fetchone()[0]
check("并发后 selected 恰好1个", n_sel == 1)
winner_id = y if success_locs[0].endswith(str(y)) else z
sel_id = db.execute("SELECT id FROM claims WHERE item_id=? AND status='selected'", (iid,)).fetchone()[0]
check("获胜者与DB状态一致", sel_id == winner_id, f"{sel_id} vs {winner_id}")
n_corr = db.execute("SELECT COUNT(*) FROM claim_corrections WHERE item_id=?", (iid,)).fetchone()[0]
check("并发改判只写入1条记录", n_corr == 1)
active_codes = db.execute(
    "SELECT claim_id FROM handover_codes WHERE consumed_at IS NULL AND invalidated_at IS NULL").fetchall()
item_active = [r[0] for r in active_codes if r[0] in (x, y, z)]
check("有效交还码恰好1枚且属于新获选人", item_active == [winner_id], str(active_codes))
# 甲（原获选）的码必然失效
inv_x = db.execute(
    "SELECT COUNT(*) FROM handover_codes hc JOIN claims c ON c.id=hc.claim_id"
    " WHERE c.item_id=? AND hc.invalidated_at IS NOT NULL", (iid,)).fetchone()[0]
check("原获选甲的旧码已失效", inv_x >= 1)

# --------------------------------------------------------------- 7 并发：改判 × 重发
print("== 并发：改判 × 重发 ==")
req(staff, "POST", "/staff/items",
    form={"csrf_token": SCSRF, "public_description": "并发改判重发-钥匙", "private_features": "红绳"})
iid2 = db.execute("SELECT MAX(id) FROM items").fetchone()[0]
for ans, con in [("钥匙-甲", "kA"), ("钥匙-乙", "kB")]:
    _, vv = client()
    req(vv, "POST", f"/items/{iid2}/claims", form={"answer": ans, "contact": con})
kx, ky = [r[0] for r in db.execute(
    "SELECT id FROM claims WHERE item_id=? ORDER BY id", (iid2,)).fetchall()]
req(staff, "POST", f"/staff/claims/{kx}/select", form={"csrf_token": SCSRF})
barrier2 = threading.Barrier(2)


def mixed_corr():
    barrier2.wait()
    return raw_with_cookie(cookie_header, f"/staff/claims/{kx}/reselect",
                           {"csrf_token": SCSRF, "target_claim_id": str(ky), "reason": "并发-改判"})


def mixed_resend():
    barrier2.wait()
    return raw_with_cookie(cookie_header, f"/staff/claims/{kx}/resend", {"csrf_token": SCSRF})


with ThreadPoolExecutor(max_workers=2) as ex:
    rc, rr = [f.result() for f in [ex.submit(mixed_corr), ex.submit(mixed_resend)]]
print(f"   改判结果={rc} 重发结果={rr}")
n_sel = db.execute("SELECT COUNT(*) FROM claims WHERE item_id=? AND status='selected'", (iid2,)).fetchone()[0]
check("改判×重发后 selected 恰好1个", n_sel == 1)
sel_id = db.execute("SELECT id FROM claims WHERE item_id=? AND status='selected'", (iid2,)).fetchone()[0]
valid = db.execute(
    "SELECT c.id FROM handover_codes hc JOIN claims c ON c.id=hc.claim_id"
    " WHERE c.item_id=? AND hc.consumed_at IS NULL AND hc.invalidated_at IS NULL",
    (iid2,)).fetchall()
check("有效交还码恰好1枚且属于当前获选人", [r[0] for r in valid] == [sel_id], str(valid))
check("物品仍为保管中",
      db.execute("SELECT status FROM items WHERE id=?", (iid2,)).fetchone()[0] == "kept")

print(f"\n结果：{PASS} 通过, {FAIL} 失败")
raise SystemExit(1 if FAIL else 0)
