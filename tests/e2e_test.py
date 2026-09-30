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

print(f"\n结果：{PASS} 通过, {FAIL} 失败")
raise SystemExit(1 if FAIL else 0)
