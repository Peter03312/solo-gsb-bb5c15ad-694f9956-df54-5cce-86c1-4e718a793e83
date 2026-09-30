"""端到端 + 并发测试（标准库）。前置：服务已运行在 http://127.0.0.1:8181。"""
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
    """raw=True 时不跟随 303，返回 (status, body, location)；否则返回最终页 (status, body)。"""
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
        return resp.status, resp.read().decode()
    except urllib.error.HTTPError as e:
        body = e.read().decode()
        if raw:
            return e.code, body, e.headers.get("Location", "")
        return e.code, body


def raw_with_cookie(cookie_header, path, form):
    """线程内手工带 Cookie 的无跳转 POST，返回 (status, location)。"""
    data = urllib.parse.urlencode(form).encode()
    r = urllib.request.Request(BASE + path, data=data, method="POST")
    r.add_header("Cookie", cookie_header)
    try:
        resp = urllib.request.build_opener(NoRedirect).open(r)
        return resp.status, resp.headers.get("Location", "")
    except urllib.error.HTTPError as e:
        return e.code, e.headers.get("Location", "")


def raw_with_cookie_full(cookie_header, path, form):
    """同 raw_with_cookie，额外返回响应中的 Set-Cookie 头（用于读取一次性 flash）。"""
    data = urllib.parse.urlencode(form).encode()
    r = urllib.request.Request(BASE + path, data=data, method="POST")
    r.add_header("Cookie", cookie_header)
    try:
        resp = urllib.request.build_opener(NoRedirect).open(r)
        return resp.status, resp.headers.get("Location", ""), resp.headers.get_all("Set-Cookie") or []
    except urllib.error.HTTPError as e:
        return e.code, e.headers.get("Location", ""), e.headers.get_all("Set-Cookie") or []


def csrf(htmltext):
    m = re.search(r'name="csrf_token" value="([^"]+)"', htmltext)
    return m.group(1) if m else None


# --------------------------------------------------------------- 1 基本流程
print("== 访客与工作人员基本流程 ==")
_, visitor = client()
st, body = req(visitor, "GET", "/")
check("首页可访问", st == 200 and "保管中的失物" in body)

anonjar, anon = client()
st, body = req(anon, "GET", "/staff/login")
tok = csrf(body)
check("登录页含 CSRF", bool(tok))

st, _, _ = req(anon, "POST", "/staff/login", form={"csrf_token": tok, "password": "wrong"}, raw=True)
check("错误口令被拒(401)", st == 401)

st, body, _ = req(anon, "POST", "/staff/login", form={"csrf_token": tok, "password": "wrong"}, raw=True)
check("重放登录请求(CSRF已轮换)被拒", st == 400)

st, body = req(anon, "GET", "/staff/login")
tok = csrf(body)
st, _, loc = req(anon, "POST", "/staff/login", form={"csrf_token": tok, "password": "secret123"}, raw=True)
check("正确口令登录(303)", st == 303 and loc == "/staff")

st, dash = req(anon, "GET", "/staff")
staff_csrf = csrf(dash)
check("登录后进入登记台", st == 200 and "登记新物品" in dash and bool(staff_csrf))

PRIV1 = "私密特征-内袋蓝色姓名贴ZXQ"
PUB1 = "公开信息-黑色双肩包"
st, _, loc = req(anon, "POST", "/staff/items",
                 form={"csrf_token": staff_csrf, "public_description": PUB1, "private_features": PRIV1}, raw=True)
check("登记物品1(303)", st == 303 and loc == "/staff/items/1")
PRIV2 = "私密特征2-钥匙扣红色小熊"
st, _, _ = req(anon, "POST", "/staff/items",
               form={"csrf_token": staff_csrf, "public_description": "公开信息2-银色钥匙串",
                     "private_features": PRIV2}, raw=True)
check("登记物品2(303)", st == 303)
PRIV3 = "私密特征3-水壶底部划痕"
st, _, _ = req(anon, "POST", "/staff/items",
               form={"csrf_token": staff_csrf, "public_description": "公开信息3-白色保温杯",
                     "private_features": PRIV3}, raw=True)
check("登记物品3(303)", st == 303)

_, v0 = client()
st, body = req(v0, "GET", "/")
check("访客列表显示公开描述", PUB1 in body and "公开信息2" in body and "公开信息3" in body)
check("访客列表不泄露私密特征", PRIV1 not in body and PRIV2 not in body and PRIV3 not in body)

_, stranger = client()
st, _ = req(stranger, "GET", "/staff")
check("未登录访问 /staff 被拒(403)", st == 403)
st, _ = req(stranger, "GET", "/staff/items/1")
check("未登录访问物品管理页被拒(403)", st == 403)

# --------------------------------------------------------------- 2 提交认领
print("== 提交认领与凭证 ==")
_, item1_page = client()
st, body = req(item1_page, "GET", "/items/1")
check("访客物品页显示公开描述", st == 200 and PUB1 in body)
check("访客物品页不显示私密特征", PRIV1 not in body)

tokens = []
for i, ans in enumerate(["答案A-说有姓名贴", "答案B-答错了", "答案C-也答对了"]):
    _, v = client()
    st, body = req(v, "POST", "/items/1/claims", form={"answer": ans, "contact": f"1380000000{i}"})
    check(f"提交认领{i+1}", st == 200 and "查询凭证" in body)
    m = re.search(r'<div class="tokenbox">([^<]+)</div>', body)
    check(f"认领{i+1}返回凭证", bool(m))
    if m:
        tokens.append((m.group(1).strip(), ans, f"1380000000{i}"))

_, v = client()
st, _ = req(v, "POST", "/items/1/claims", form={"answer": "x", "contact": ""})
check("缺少联系方式被拒(400)", st == 400)

_, v = client()
st, _ = req(v, "GET", "/query")
check("查询页可打开", st == 200)
st, body = req(v, "GET", "/query?token=not-a-real-token")
check("错误凭证查询失败", "凭证不正确" in body)

tA, ansA, conA = tokens[0]
tB, ansB, conB = tokens[1]
tC = tokens[2][0]
_, v = client()
st, qA = req(v, "GET", f"/query?token={urllib.parse.quote(tA)}")
check("凭证持有人看到本人状态", "待核对" in qA and PUB1 in qA)
check("查询页不泄露私密特征", PRIV1 not in qA)
check("查询页不泄露他人答案/联系方式/自身联系方式", ansB not in qA and conB not in qA and conA not in qA)

# --------------------------------------------------------------- 3 工作人员视图
print("== 工作人员可见全部信息 ==")
st, body = req(anon, "GET", "/staff/items/1")
check("工作人员能看到私密特征", PRIV1 in body)
check("工作人员能看到答案与联系方式", ansA in body and conA in body)
claim_ids = [int(x) for x in re.findall(r'/staff/claims/(\d+)', body)]
check("物品1有3个申请", len(claim_ids) == 3, str(claim_ids))
cA, cB, cC = claim_ids

st, _, _ = req(anon, "POST", f"/staff/claims/{cA}/select", form={"csrf_token": "bad"}, raw=True)
check("伪造CSRF选定被拒(400)", st == 400)

# --------------------------------------------------------------- 4 并发选定
print("== 并发确认最多成功一次 ==")
cookie_header = "; ".join(f"{c.name}={c.value}" for c in anonjar)
barrier = threading.Barrier(3)


def select_task(cid):
    barrier.wait()
    return cid, raw_with_cookie(cookie_header, f"/staff/claims/{cid}/select",
                                {"csrf_token": staff_csrf})


with ThreadPoolExecutor(max_workers=3) as ex:
    outcomes = [f.result() for f in [ex.submit(select_task, c) for c in (cA, cB, cC)]]

n303 = sum(1 for _, (code, _loc) in outcomes if code == 303)
check("并发选定三个请求都被接受处理(303)", n303 == 3, str(outcomes))

db = sqlite3.connect("/tmp/lfdata/lostfound.db")
rows = db.execute("SELECT id, status FROM claims WHERE item_id=1 ORDER BY id").fetchall()
selected_ids = [cid for cid, s in rows if s == "selected"]
nrej = sum(1 for _, s in rows if s == "rejected")
check("DB 中 selected 恰好1个", selected_ids == [cA] or len(selected_ids) == 1, str(rows))
check("DB 中其余 rejected=2", nrej == 2, str(rows))
winner = selected_ids[0]
winner_token = {cA: tA, cB: tB, cC: tC}[winner]
loser_token = {cA: tA, cB: tB, cC: tC}[cA if winner != cA else cB]

_, v = client()
st, body = req(v, "GET", f"/query?token={urllib.parse.quote(loser_token)}")
check("落选者看到未获选", "未获选" in body)
check("落选者页面无交还码展示", '<div class="codebox">' not in body)

_, v = client()
st, wq = req(v, "GET", f"/query?token={urllib.parse.quote(winner_token)}")
check("获选者看到已获选", "已获选" in wq)
m = re.search(r'<div class="codebox">(\d{8})</div>', wq)
check("获选者取得8位交还码", bool(m))
code1 = m.group(1) if m else ""
check("查询页不泄露私密特征", PRIV1 not in wq)

_, v = client()
st, body = req(v, "POST", "/items/1/claims", form={"answer": "后来者", "contact": "x@y.z"})
check("已获选物品拒绝新申请", st == 400 and "已确定获选人" in body)

# 成功的那个再次选定（跟随跳转到申请页，应带错误提示）
st, body = req(anon, "POST", f"/staff/claims/{winner}/select", form={"csrf_token": staff_csrf})
check("重复选定不改变状态并提示", st == 200 and "已经处理" in body)
nsel2 = db.execute("SELECT COUNT(*) FROM claims WHERE item_id=1 AND status='selected'").fetchone()[0]
check("重复选定后 selected 仍为1", nsel2 == 1)

# --------------------------------------------------------------- 5 撤下
print("== 撤下物品 ==")
_, v2 = client()
st, body = req(v2, "POST", "/items/2/claims", form={"answer": "待核对答案", "contact": "wechat-xyz"})
t2 = re.search(r'<div class="tokenbox">([^<]+)</div>', body).group(1).strip()
st, _, loc = req(anon, "POST", "/staff/items/2/remove", form={"csrf_token": staff_csrf}, raw=True)
check("撤下物品(303)", st == 303 and loc == "/staff/items/2")
_, v = client()
st, body = req(v, "GET", "/")
check("撤下后不在公开列表", "公开信息2" not in body)
st, body = req(v, "GET", "/items/2")
check("撤下物品页无申请表单", 'action="/items/2/claims"' not in body)
st, body = req(v, "POST", "/items/2/claims", form={"answer": "a", "contact": "b"})
check("撤下物品拒绝新申请", st == 400 and "撤下或已交还" in body)
st, body = req(v, "GET", f"/query?token={urllib.parse.quote(t2)}")
check("撤下后待核对申请变为未获选", "未获选" in body)
st, _, _ = req(anon, "POST", "/staff/items/2/restore", form={"csrf_token": staff_csrf}, raw=True)
check("恢复物品(303)", st == 303)
st, body = req(v, "GET", "/")
check("恢复后重回公开列表", "公开信息2" in body)

# --------------------------------------------------------------- 6 核销与并发
print("== 核码交还 / 重复核码 / 并发核码 ==")
st, body = req(anon, "GET", "/staff/verify")
vcsrf = csrf(body)
st, body = req(anon, "POST", "/staff/verify", form={"csrf_token": vcsrf, "code": "00000000"})
check("错误交还码被拒(200提示)", st == 200 and "交还码不正确" in body)

barrier2 = threading.Barrier(2)


def verify_attempt(code):
    barrier2.wait()
    return raw_with_cookie(cookie_header, "/staff/verify", {"csrf_token": vcsrf, "code": code})


with ThreadPoolExecutor(max_workers=2) as ex:
    o1 = ex.submit(verify_attempt, code1)
    o2 = ex.submit(verify_attempt, code1)
    r1, r2 = o1.result(), o2.result()

success = [o for o in (r1, r2) if o[0] == 303 and o[1] == "/staff/items/1"]
check("并发核码恰好1次成功(303到物品页)", len(success) == 1, f"{r1} {r2}")
st = db.execute("SELECT status FROM items WHERE id=1").fetchone()[0]
check("物品1已交还", st == "returned")
nconsumed = db.execute("SELECT COUNT(*) FROM handover_codes WHERE consumed_at IS NOT NULL").fetchone()[0]
check("恰好1枚码被核销", nconsumed == 1)

st, body = req(anon, "GET", "/staff/verify")
vcsrf = csrf(body)
st, body = req(anon, "POST", "/staff/verify", form={"csrf_token": vcsrf, "code": code1})
check("重复核码被拒并提示已使用", st == 200 and "已使用" in body)

_, v = client()
st, body = req(v, "POST", "/items/1/claims", form={"answer": "a", "contact": "b"})
check("已交还物品拒绝新申请", st == 400 and "撤下或已交还" in body)

st, body = req(v, "GET", f"/query?token={urllib.parse.quote(winner_token)}")
check("获选者查询页不再显示可用码", 'class="codebox"' not in body)

# --------------------------------------------------------------- 7 重发/过期
print("== 交还码过期与重发 ==")
_, v3a = client()
req(v3a, "POST", "/items/3/claims", form={"answer": "甲", "contact": "甲-contact"})
_, v3b = client()
st, body = req(v3b, "POST", "/items/3/claims", form={"answer": "乙", "contact": "乙-contact"})
t3 = re.search(r'<div class="tokenbox">([^<]+)</div>', body).group(1).strip()

st, body = req(anon, "GET", "/staff/items/3")
c3 = [int(x) for x in re.findall(r'/staff/claims/(\d+)', body)][-1]
st, _, _ = req(anon, "POST", f"/staff/claims/{c3}/select", form={"csrf_token": staff_csrf}, raw=True)
check("选定物品3申请(303)", st == 303)

_, v = client()
st, q3 = req(v, "GET", f"/query?token={urllib.parse.quote(t3)}")
code_a = re.search(r'<div class="codebox">(\d{8})</div>', q3).group(1)
check("物品3获选者取得码", bool(code_a))

# 直接把码改过期
db.execute("UPDATE handover_codes SET expires_at='2000-01-01T00:00:00+00:00'"
           " WHERE claim_id=? AND consumed_at IS NULL", (c3,))
db.commit()
st, q3 = req(v, "GET", f"/query?token={urllib.parse.quote(t3)}")
check("过期后查询页提示过期", "交还码已过期" in q3 and 'class="codebox"' not in q3)
st, body = req(anon, "GET", "/staff/verify")
vcsrf = csrf(body)
st, body = req(anon, "POST", "/staff/verify", form={"csrf_token": vcsrf, "code": code_a})
check("过期码核销被拒", "已过期" in body)

st, body = req(anon, "GET", f"/staff/claims/{c3}")
ccsrf = csrf(body)
st, _, _ = req(anon, "POST", f"/staff/claims/{c3}/resend", form={"csrf_token": ccsrf}, raw=True)
check("重发新码(303)", st == 303)
ninvalid = db.execute("SELECT COUNT(*) FROM handover_codes WHERE invalidated_at IS NOT NULL").fetchone()[0]
check("旧码已标记失效", ninvalid >= 1)

st, q3 = req(v, "GET", f"/query?token={urllib.parse.quote(t3)}")
m3 = re.search(r'<div class="codebox">(\d{8})</div>', q3)
check("重发后查询页显示新码", bool(m3) and m3.group(1) != code_a)
code_b = m3.group(1)

st, body = req(anon, "GET", "/staff/verify")
vcsrf = csrf(body)
st, body = req(anon, "POST", "/staff/verify", form={"csrf_token": vcsrf, "code": code_a})
check("旧码(已重发)核销被拒", "已失效" in body)

# 再重发一次：新新码也使上一枚失效
st, body = req(anon, "GET", f"/staff/claims/{c3}")
ccsrf = csrf(body)
req(anon, "POST", f"/staff/claims/{c3}/resend", form={"csrf_token": ccsrf}, raw=True)
st, q3 = req(v, "GET", f"/query?token={urllib.parse.quote(t3)}")
code_c = re.search(r'<div class="codebox">(\d{8})</div>', q3).group(1)
st, body = req(anon, "GET", "/staff/verify")
vcsrf = csrf(body)
st, body = req(anon, "POST", "/staff/verify", form={"csrf_token": vcsrf, "code": code_b})
check("第二枚码被重发后也失效", "已失效" in body)
st, body, loc = req(anon, "POST", "/staff/verify", form={"csrf_token": vcsrf, "code": code_c}, raw=True)
check("最新码核销成功(303)", st == 303 and loc == "/staff/items/3")
st3 = db.execute("SELECT status FROM items WHERE id=3").fetchone()[0]
check("物品3已交还", st3 == "returned")

# 已交还后：申请页不再提供重发按钮
st, body = req(anon, "GET", f"/staff/claims/{c3}")
check("已交还申请页不再提供重发按钮", f"/staff/claims/{c3}/resend" not in body)
codes_after = db.execute("SELECT COUNT(*) FROM handover_codes WHERE claim_id=?", (c3,)).fetchone()[0]
check("已交还未产生额外交还码", codes_after == 3)

# --------------------------------------------------------------- 8 凭证强度与健康检查
print("== 凭证强度与杂项 ==")
check("查询凭证长度>=40", len(tA) >= 40, tA)
check("两枚凭证不同", tA != tB)
_, v = client()
st, body = req(v, "GET", "/healthz")
check("健康检查200", st == 200 and body == "ok")
st, _ = req(v, "GET", "/no-such-page")
check("未知公共路径404", st == 404)
st, _ = req(v, "GET", "/staff/no-such-page")
check("未知工作人员路径对访客403", st == 403)

# --------------------------------------------------------------- 9 误选改判
print("== 误选改判 ==")
PRIV4 = "私密特征4-钱包内借书卡"
PUB4 = "公开信息4-棕色短款钱包"
st, _, _ = req(anon, "POST", "/staff/items",
               form={"csrf_token": staff_csrf, "public_description": PUB4,
                     "private_features": PRIV4}, raw=True)
check("登记物品4(303)", st == 303)
st, body = req(anon, "GET", "/staff")
item4_id = max(int(x) for x in re.findall(r'/staff/items/(\d+)', body))

t4 = []
for i, ans in enumerate(["改-答案甲-说有现金", "改-答案乙-提到借书卡", "改-答案丙-答错"]):
    _, v = client()
    st, body = req(v, "POST", f"/items/{item4_id}/claims",
                   form={"answer": ans, "contact": f"1391111000{i}"})
    m = re.search(r'<div class="tokenbox">([^<]+)</div>', body)
    t4.append((m.group(1).strip(), ans))
    check(f"物品4提交认领{i+1}", st == 200 and bool(m))

st, body = req(anon, "GET", f"/staff/items/{item4_id}")
r4 = [int(x) for x in re.findall(r'/staff/claims/(\d+)', body)]
check("物品4有3个申请", len(r4) == 3, str(r4))
rA, rB, rC = r4
tokA, tokB, tokC = t4[0][0], t4[1][0], t4[2][0]
ansA4, ansB4 = t4[0][1], t4[1][1]

# 先选定甲
st, _, _ = req(anon, "POST", f"/staff/claims/{rA}/select",
               form={"csrf_token": staff_csrf}, raw=True)
check("选定物品4甲(303)", st == 303)

# 未获选申请页不应出现改判表单
st, body = req(anon, "GET", f"/staff/claims/{rB}")
check("未获选申请页无改判表单", f"/staff/claims/{rB}/rejudge" not in body)
# 原获选申请页出现改判表单，目标只列同物品未获选申请
st, body = req(anon, "GET", f"/staff/claims/{rA}")
check("获选申请页有改判表单", f'action="/staff/claims/{rA}/rejudge"' in body)
check("改判表单带物品版本号", 'name="version"' in body)
check("改判目标含同物品申请B", f'value="{rB}"' in body)
check("改判目标含同物品申请C", f'value="{rC}"' in body)
check("改判目标不含自己", f'<option value="{rA}">' not in body)
ver4 = db.execute("SELECT version FROM items WHERE id=?",
                  (item4_id,)).fetchone()[0]

# 拒绝项：理由为空（表单级校验，直接 400，不进入事务）
st, _, loc = req(anon, "POST", f"/staff/claims/{rA}/rejudge",
                 form={"csrf_token": staff_csrf, "to_claim_id": str(rB),
                       "reason": "   ", "item_id": str(item4_id),
                       "version": ver4}, raw=True)
check("空理由改判被拒(400)", st == 400)
st, body = req(anon, "GET", f"/staff/claims/{rA}")
check("空理由被拒后仍有改判表单", f'action="/staff/claims/{rA}/rejudge"' in body)

# 拒绝项：目标是另一物品的申请
r_other = c3  # 物品3（已交还）的获选申请
st, _, loc = req(anon, "POST", f"/staff/claims/{rA}/rejudge",
                 form={"csrf_token": staff_csrf, "to_claim_id": str(r_other),
                       "reason": "跨物品", "item_id": str(item4_id),
                       "version": ver4}, raw=True)
check("跨物品目标改判被拒", st == 303 and loc == f"/staff/claims/{rA}")

# 拒绝项：目标是原申请自己
st, _, _ = req(anon, "POST", f"/staff/claims/{rA}/rejudge",
               form={"csrf_token": staff_csrf, "to_claim_id": str(rA),
                     "reason": "自己", "item_id": str(item4_id),
                     "version": ver4}, raw=True)
check("目标为自己改判被拒", st == 303)

# 拒绝项：从非获选申请发起（以B为原申请）
st, _, loc = req(anon, "POST", f"/staff/claims/{rB}/rejudge",
                 form={"csrf_token": staff_csrf, "to_claim_id": str(rC),
                       "reason": "B非获选", "item_id": str(item4_id),
                       "version": ver4}, raw=True)
check("非获选原申请发起改判被拒", st == 303 and loc == f"/staff/claims/{rB}")

# 上述拒绝均未改动状态
sel4 = db.execute("SELECT id FROM claims WHERE item_id=? AND status='selected'",
                  (item4_id,)).fetchall()
check("拒绝后仍只有甲获选", [r[0] for r in sel4] == [rA], str(sel4))

# 甲当前的码（改判前）
_, v = client()
st, qA0 = req(v, "GET", f"/query?token={urllib.parse.quote(tokA)}")
m = re.search(r'<div class="codebox">(\d{8})</div>', qA0)
check("甲改判前持有交还码", bool(m))
codeA0 = m.group(1) if m else ""

# 成功改判：甲 -> 乙
REASON = "现场二次核对：甲无法说出借书卡姓名，乙的答案与私密特征吻合"
st, _, loc = req(anon, "POST", f"/staff/claims/{rA}/rejudge",
                 form={"csrf_token": staff_csrf, "to_claim_id": str(rB),
                       "reason": REASON, "item_id": str(item4_id),
                       "version": ver4}, raw=True)
check("改判成功(303到物品页)", st == 303 and loc == f"/staff/items/{item4_id}", loc)

rows4 = db.execute("SELECT id, status FROM claims WHERE item_id=? ORDER BY id",
                   (item4_id,)).fetchall()
check("改判后甲/丙未获选、乙获选",
      dict(rows4) == {rA: "rejected", rB: "selected", rC: "rejected"}, str(rows4))

# 原码全部失效 + 新码签发
ninv = db.execute("SELECT COUNT(*) FROM handover_codes hc WHERE hc.claim_id=?"
                  " AND hc.invalidated_at IS NOT NULL", (rA,)).fetchone()[0]
nnew = db.execute("SELECT COUNT(*) FROM handover_codes WHERE claim_id=?"
                  " AND consumed_at IS NULL AND invalidated_at IS NULL", (rB,)).fetchone()[0]
check("甲的旧码已失效", ninv >= 1)
check("乙获得一枚新有效码", nnew == 1)
check("改判留痕1条",
      db.execute("SELECT COUNT(*) FROM corrections WHERE item_id=?",
                 (item4_id,)).fetchone()[0] == 1)

# 甲的凭证：未获选，看不到任何码
_, v = client()
st, qA1 = req(v, "GET", f"/query?token={urllib.parse.quote(tokA)}")
check("甲查询页变为未获选", "未获选" in qA1 and "已获选" not in qA1)
check("甲查询页无交还码", '<div class="codebox">' not in qA1)
check("甲查询页不泄露改判理由与乙的信息", REASON not in qA1 and ansB4 not in qA1)
# 乙的凭证：已获选 + 新码（与旧码不同）
_, v = client()
st, qB1 = req(v, "GET", f"/query?token={urllib.parse.quote(tokB)}")
check("乙查询页变为已获选", "已获选" in qB1)
m = re.search(r'<div class="codebox">(\d{8})</div>', qB1)
check("乙取得新交还码且不同于甲旧码", bool(m) and m.group(1) != codeA0)
codeB1 = m.group(1) if m else ""
check("乙查询页不泄露改判理由与甲的信息", REASON not in qB1 and ansA4 not in qB1)
# 丙无变化
_, v = client()
st, qC1 = req(v, "GET", f"/query?token={urllib.parse.quote(tokC)}")
check("丙仍为未获选且无码", "未获选" in qC1 and '<div class="codebox">' not in qC1)

# 甲旧码核销被拒
st, body = req(anon, "GET", "/staff/verify")
vcsrf = csrf(body)
st, body = req(anon, "POST", "/staff/verify", form={"csrf_token": vcsrf, "code": codeA0})
check("甲旧码核销被拒(已失效)", "已失效" in body)
st4 = db.execute("SELECT status FROM items WHERE id=?", (item4_id,)).fetchone()[0]
check("旧码被拒后物品仍保管中", st4 == "kept")

# 乙的新码可核销成功（但为继续测试，先改用另一物品；此处直接验证新码正确）
# 留痕在物品页与双方申请页均可见
st, body = req(anon, "GET", f"/staff/items/{item4_id}")
check("物品页展示改判记录", "改判记录" in body and REASON in body
      and f'/staff/claims/{rA}' in body and f'/staff/claims/{rB}' in body)
st, bodyA = req(anon, "GET", f"/staff/claims/{rA}")
st, bodyB = req(anon, "GET", f"/staff/claims/{rB}")
check("甲申请页可追溯改判记录", REASON in bodyA)
check("乙申请页可追溯改判记录", REASON in bodyB)
check("乙申请页显示新码明文", codeB1 in bodyB)
check("甲申请页不显示乙新码明文", codeB1 not in bodyA)

# 甲被改判后不能再对其重发（页面无重发表单；用有效 CSRF 直打接口亦被业务拒绝）
st, _, _ = req(anon, "POST", f"/staff/claims/{rA}/resend",
               form={"csrf_token": staff_csrf}, raw=True)
check("对未获选的甲重发被拒(303业务错误)", st == 303)

# 再改判一次：乙 -> 甲（双重改判），版本号已被第一次改判推进
ver4b = db.execute("SELECT version FROM items WHERE id=?",
                   (item4_id,)).fetchone()[0]
check("第一次改判推进了物品版本号", ver4b != ver4)
st, _, _ = req(anon, "POST", f"/staff/claims/{rB}/rejudge",
               form={"csrf_token": staff_csrf, "to_claim_id": str(rA),
                     "reason": "再次复核后确认甲才是失主", "item_id": str(item4_id),
                     "version": ver4b}, raw=True)
check("二次改判(新版本号)被接受", st == 303)
# 旧版本号再改判会被拒绝（版本已推进）
st, body = req(anon, "GET", f"/staff/claims/{rA}")
st, _, loc = req(anon, "POST", f"/staff/claims/{rA}/rejudge",
                 form={"csrf_token": staff_csrf, "to_claim_id": str(rB),
                       "reason": "旧版本号", "item_id": str(item4_id),
                       "version": ver4}, raw=True)
check("旧版本号改判被拒(303)", st == 303 and loc == f"/staff/claims/{rA}")
rows4 = db.execute("SELECT id, status FROM claims WHERE item_id=? ORDER BY id",
                   (item4_id,)).fetchall()
check("二次改判后甲获选、乙未获选",
      dict(rows4) == {rA: "selected", rB: "rejected", rC: "rejected"}, str(rows4))
ninv_b = db.execute("SELECT COUNT(*) FROM handover_codes WHERE claim_id=?"
                    " AND invalidated_at IS NOT NULL", (rB,)).fetchone()[0]
nnew_a = db.execute("SELECT COUNT(*) FROM handover_codes WHERE claim_id=?"
                    " AND consumed_at IS NULL AND invalidated_at IS NULL", (rA,)).fetchone()[0]
check("乙的码随二次改判失效", ninv_b >= 1)
check("甲重新获得新有效码", nnew_a == 1)
check("改判留痕共2条",
      db.execute("SELECT COUNT(*) FROM corrections WHERE item_id=?",
                 (item4_id,)).fetchone()[0] == 2)
_, v = client()
st, qB2 = req(v, "GET", f"/query?token={urllib.parse.quote(tokB)}")
check("乙二次改判后查询页无码", '<div class="codebox">' not in qB2 and "未获选" in qB2)

# 物品已交还后拒绝改判：用乙最后那枚新码核销
st, body = req(anon, "GET", f"/staff/claims/{rA}")
m = re.search(r'<div class="codebox">(\d{8})</div>', body)
codeA2 = m.group(1) if m else ""
st, body = req(anon, "GET", "/staff/verify")
vcsrf = csrf(body)
st, _, loc = req(anon, "POST", "/staff/verify",
                form={"csrf_token": vcsrf, "code": codeA2}, raw=True)
check("甲最新码核销成功", st == 303 and loc == f"/staff/items/{item4_id}")
st, _, _ = req(anon, "POST", f"/staff/claims/{rA}/rejudge",
               form={"csrf_token": staff_csrf, "to_claim_id": str(rB),
                     "reason": "交还后", "item_id": str(item4_id),
                     "version": ver4b}, raw=True)
check("已交还物品改判被拒", st == 303)
check("已交还后仍为2条留痕",
      db.execute("SELECT COUNT(*) FROM corrections WHERE item_id=?",
                 (item4_id,)).fetchone()[0] == 2)

# --------------------------------------------------------------- 10 改判并发
print("== 改判并发：只允许一次状态变更生效 ==")
PUB5 = "公开信息5-黑色雨伞"
st, _, _ = req(anon, "POST", "/staff/items",
               form={"csrf_token": staff_csrf, "public_description": PUB5,
                     "private_features": "私密特征5-伞柄刻字LF"}, raw=True)
st, body = req(anon, "GET", "/staff")
item5_id = max(int(x) for x in re.findall(r'/staff/items/(\d+)', body))
t5 = []
for i in range(3):
    _, v = client()
    st, b = req(v, "POST", f"/items/{item5_id}/claims",
                form={"answer": f"伞答案{i}", "contact": f"u{i}"})
    t5.append(re.search(r'<div class="tokenbox">([^<]+)</div>', b).group(1).strip())
r5 = [r[0] for r in db.execute("SELECT id FROM claims WHERE item_id=? ORDER BY id",
                               (item5_id,)).fetchall()]
s5, t5a, t5b = r5
req(anon, "POST", f"/staff/claims/{s5}/select", form={"csrf_token": staff_csrf}, raw=True)
ver5 = db.execute("SELECT version FROM items WHERE id=?",
                  (item5_id,)).fetchone()[0]


def rejudge_task(src, dst):
    return raw_with_cookie(
        cookie_header, f"/staff/claims/{src}/rejudge",
        {"csrf_token": staff_csrf, "to_claim_id": str(dst),
         "reason": f"并发改判 {src}->{dst}", "item_id": str(item5_id),
         "version": ver5},
    )


# 两个改判方向同时提交（同一物品），只有一次生效
barrier3 = threading.Barrier(2)


def rj_task(src, dst):
    barrier3.wait()
    return rejudge_task(src, dst)


with ThreadPoolExecutor(max_workers=2) as ex:
    o1 = ex.submit(rj_task, s5, t5a)
    o2 = ex.submit(rj_task, t5a, s5)
    cr1, cr2 = o1.result(), o2.result()

sel5 = [r[0] for r in db.execute("SELECT id FROM claims WHERE item_id=? AND status='selected'",
                                 (item5_id,)).fetchall()]
check("并发改判后 selected 仍恰好1个", len(sel5) == 1, str(sel5))
check("获选人只可能是原申请或首个目标", sel5[0] in (s5, t5a), str(sel5))
ncorr5 = db.execute("SELECT COUNT(*) FROM corrections WHERE item_id=?",
                    (item5_id,)).fetchone()[0]
check("并发改判只产生1条留痕", ncorr5 == 1, str(ncorr5))
nactive = db.execute(
    "SELECT COUNT(*) FROM handover_codes hc JOIN claims c ON c.id=hc.claim_id"
    " WHERE c.item_id=? AND hc.consumed_at IS NULL AND hc.invalidated_at IS NULL",
    (item5_id,)).fetchone()[0]
check("并发改判后全物品仅1枚有效交还码", nactive == 1, str(nactive))

# 改判与撤下并发：状态只能朝一个方向变化，不得出现「撤下却签发新码」
PUB6 = "公开信息6-蓝色头盔"
st, _, _ = req(anon, "POST", "/staff/items",
               form={"csrf_token": staff_csrf, "public_description": PUB6,
                     "private_features": "私密特征6-内衬贴纸"}, raw=True)
st, body = req(anon, "GET", "/staff")
item6_id = max(int(x) for x in re.findall(r'/staff/items/(\d+)', body))
for i in range(2):
    _, v = client()
    req(v, "POST", f"/items/{item6_id}/claims",
        form={"answer": f"盔答案{i}", "contact": f"h{i}"})
r6 = [r[0] for r in db.execute("SELECT id FROM claims WHERE item_id=? ORDER BY id",
                               (item6_id,)).fetchall()]
s6, t6 = r6
req(anon, "POST", f"/staff/claims/{s6}/select", form={"csrf_token": staff_csrf}, raw=True)
# 记录选定前页面看到的版本号（模拟工作人员在改判发生前已打开撤下表单）
ver_before_select = db.execute("SELECT version FROM items WHERE id=?",
                               (item6_id,)).fetchone()[0]
# 选定会推进版本号；并发测试使用「当前页面版本」才公平，因此这里取选定后的最新版本
ver6 = db.execute("SELECT version FROM items WHERE id=?",
                  (item6_id,)).fetchone()[0]
barrier4 = threading.Barrier(2)


def mixed_task(kind):
    barrier4.wait()
    if kind == "rj":
        code, loc, sc = raw_with_cookie_full(
            cookie_header, f"/staff/claims/{s6}/rejudge",
            {"csrf_token": staff_csrf, "to_claim_id": str(t6),
             "reason": "并发改判：撤下同时发起", "item_id": str(item6_id),
             "version": ver6})
        return ("rj", code, loc, sc)
    code, loc, sc = raw_with_cookie_full(
        cookie_header, f"/staff/items/{item6_id}/remove",
        {"csrf_token": staff_csrf, "version": ver6})
    return ("rm", code, loc, sc)


with ThreadPoolExecutor(max_workers=2) as ex:
    m1 = ex.submit(mixed_task, "rj")
    m2 = ex.submit(mixed_task, "rm")
    mr1, mr2 = m1.result(), m2.result()


def flash_from(set_cookies):
    """从 303 响应各自的 Set-Cookie 中解出一次性 flash（不经过共享 Cookie 罐）。"""
    import base64 as _b64
    import json as _json
    for header in set_cookies:
        if not header.startswith("lf_flash="):
            continue
        raw = header.split(";", 1)[0][len("lf_flash="):]
        try:
            kind, msg = _json.loads(_b64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)))
            return kind, msg
        except Exception:
            return "", ""
    return "", ""


rm_outcome = next(x for x in (mr1, mr2) if x[0] == "rm")
rj_outcome = next(x for x in (mr1, mr2) if x[0] == "rj")
rm_flash = flash_from(rm_outcome[3])
rj_flash = flash_from(rj_outcome[3])
rm_won = "物品已撤下" in rm_flash[1]
rj_won = "改判完成" in rj_flash[1]
# 写事务串行 + 条件式原子守卫：屏障保证同时发起，HTTP 端可能近串行处理，
# 但无论如何恰一方成功、另一方收到错误 flash。
check("撤下/改判并发恰好一方成功",
      rm_won != rj_won,
      f"rmflash={rm_flash} rjflash={rj_flash}")
check("双方都不会同时成功（无双重状态变更）", not (rm_won and rj_won))

istat6 = db.execute("SELECT status FROM items WHERE id=?", (item6_id,)).fetchone()[0]
nsel6 = db.execute("SELECT COUNT(*) FROM claims WHERE item_id=? AND status='selected'",
                   (item6_id,)).fetchone()[0]
nnew6 = db.execute(
    "SELECT COUNT(*) FROM handover_codes hc JOIN claims c ON c.id=hc.claim_id"
    " WHERE c.item_id=? AND hc.consumed_at IS NULL AND hc.invalidated_at IS NULL",
    (item6_id,)).fetchone()[0]
ncorr6 = db.execute("SELECT COUNT(*) FROM corrections WHERE item_id=?",
                    (item6_id,)).fetchone()[0]
if rm_won:
    check("撤下方成功：物品已撤下", istat6 == "removed")
    check("撤下方成功：改判方收到错误提示", rj_flash[0] == "err")
    check("撤下方成功：仍只有1位获选人、1枚有效码", nsel6 == 1 and nnew6 == 1)
    check("撤下方成功：改判未留痕", ncorr6 == 0, str(ncorr6))
else:
    check("改判方成功：物品仍保管中（撤下方未实际生效）", istat6 == "kept", istat6)
    check("改判方成功：恰好1条留痕", ncorr6 == 1, str(ncorr6))
    check("改判方成功：撤下方收到版本/状态冲突错误", rm_flash[0] == "err", str(rm_flash))
    check("改判方成功：目标获选且全物品1枚有效码", nsel6 == 1 and nnew6 == 1)
    # 工作人员刷新确认后可以再撤（冲突不会永久阻塞正常撤下流程）
    ver_fresh = db.execute("SELECT version FROM items WHERE id=?",
                           (item6_id,)).fetchone()[0]
    st, _, loc = req(anon, "POST", f"/staff/items/{item6_id}/remove",
                     form={"csrf_token": staff_csrf, "version": ver_fresh}, raw=True)
    check("冲突后凭新版本可正常撤下", st == 303 and loc == f"/staff/items/{item6_id}")
    # 清掉成功 flash，再确认最终态
    req(anon, "GET", f"/staff/items/{item6_id}")
    istat6 = db.execute("SELECT status FROM items WHERE id=?",
                        (item6_id,)).fetchone()[0]
check("并发结局后物品为撤下态", istat6 == "removed")

# 旧版本号撤下会被明确拒绝（即使无并发，页面过期也不改状态）
st, _, loc = req(anon, "POST", f"/staff/items/{item6_id}/remove",
                 form={"csrf_token": staff_csrf, "version": ver_before_select}, raw=True)
check("过期版本号撤下被拒", st == 303 and loc == f"/staff/items/{item6_id}")
req(anon, "GET", f"/staff/items/{item6_id}")  # 清除错误 flash，避免污染后续页面断言

# 改判与重发并发（同向）：原获选人的码不得被重发成新码，目标拿到唯一新码
PUB7 = "公开信息7-银色手表"
st, _, _ = req(anon, "POST", "/staff/items",
               form={"csrf_token": staff_csrf, "public_description": PUB7,
                     "private_features": "私密特征7-表背划痕"}, raw=True)
st, body = req(anon, "GET", "/staff")
item7_id = max(int(x) for x in re.findall(r'/staff/items/(\d+)', body))
for i in range(2):
    _, v = client()
    req(v, "POST", f"/items/{item7_id}/claims",
        form={"answer": f"表答案{i}", "contact": f"w{i}"})
r7 = [r[0] for r in db.execute("SELECT id FROM claims WHERE item_id=? ORDER BY id",
                               (item7_id,)).fetchall()]
s7, t7 = r7
req(anon, "POST", f"/staff/claims/{s7}/select", form={"csrf_token": staff_csrf}, raw=True)
ver7 = db.execute("SELECT version FROM items WHERE id=?",
                  (item7_id,)).fetchone()[0]
barrier5 = threading.Barrier(2)


def mixed2(kind):
    barrier5.wait()
    if kind == "rj":
        return ("rj",) + raw_with_cookie(
            cookie_header, f"/staff/claims/{s7}/rejudge",
            {"csrf_token": staff_csrf, "to_claim_id": str(t7),
             "reason": "改判与重发并发", "item_id": str(item7_id),
             "version": ver7})
    return ("rs",) + raw_with_cookie(
        cookie_header, f"/staff/claims/{s7}/resend", {"csrf_token": staff_csrf})


with ThreadPoolExecutor(max_workers=2) as ex:
    x1 = ex.submit(mixed2, "rj")
    x2 = ex.submit(mixed2, "rs")
    xr1, xr2 = x1.result(), x2.result()

nsel7 = db.execute("SELECT COUNT(*) FROM claims WHERE item_id=? AND status='selected'",
                   (item7_id,)).fetchone()[0]
codes7 = db.execute(
    "SELECT c.id AS cid, COUNT(*) AS n FROM handover_codes hc JOIN claims c ON c.id=hc.claim_id"
    " WHERE c.item_id=? AND hc.consumed_at IS NULL AND hc.invalidated_at IS NULL"
    " GROUP BY c.id", (item7_id,)).fetchall()
check("改判/重发并发后仍1位获选人", nsel7 == 1)
check("唯一有效码只属于新获选人(目标)",
      len(codes7) == 1 and codes7[0][0] == t7 and codes7[0][1] == 1,
      str(codes7))

print(f"\n结果：{PASS} 通过, {FAIL} 失败")
raise SystemExit(1 if FAIL else 0)
