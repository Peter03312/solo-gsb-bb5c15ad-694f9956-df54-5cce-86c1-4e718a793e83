"""并发压力：同一物品 N 份申请同时被选定；重复多轮，每轮必须只有 1 份获选。"""
import http.cookiejar
import os
import re
import sqlite3
import sys
import threading
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

BASE = "http://127.0.0.1:8181"
ROUNDS = 12
N = 8


def cj():
    j = http.cookiejar.CookieJar()
    return j, urllib.request.build_opener(urllib.request.HTTPCookieProcessor(j))


def go(op, path, form=None, raw=False, cookie=None):
    data = urllib.parse.urlencode(form).encode() if form else None
    r = urllib.request.Request(BASE + path, data=data, method="POST" if form else "GET")
    if cookie:
        r.add_header("Cookie", cookie)
    class NR(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *a, **k):
            return None
    opener = urllib.request.build_opener(NR) if raw else op
    try:
        x = opener.open(r)
        body = x.read().decode() if not raw else ""
        return x.status, (x.headers.get("Location", "") if raw else body)
    except urllib.error.HTTPError as e:
        return e.code, (e.headers.get("Location", "") if raw else e.read().decode())


jar, staff = cj()
_, b = go(staff, "/staff/login")
tok = re.search(r'name="csrf_token" value="([^"]+)"', b).group(1)
go(staff, "/staff/login", {"csrf_token": tok, "password": "secret123"})
_, dash = go(staff, "/staff")
csrf = re.search(r'name="csrf_token" value="([^"]+)"', dash).group(1)
cookie = "; ".join(f"{c.name}={c.value}" for c in jar)

db = sqlite3.connect("/tmp/lfdata/lostfound.db")
fails = 0
barrier = threading.Barrier(N)

for rnd in range(1, ROUNDS + 1):
    iid = db.execute("SELECT MAX(id) FROM items").fetchone()[0] or 0
    iid += 1
    db.execute("INSERT INTO items(public_description,private_features,status,created_at,updated_at)"
               " VALUES('stress','stress','kept','2026-01-01T00:00:00+00:00','2026-01-01T00:00:00+00:00')")
    db.commit()
    claim_ids = []
    for k in range(N):
        _, vis = cj()
        _, body = go(vis, f"/items/{iid}/claims",
                     {"answer": f"a{k}", "contact": f"c{k}"})
        assert "查询凭证" in body
    rows = db.execute("SELECT id FROM claims WHERE item_id=?", (iid,)).fetchall()
    claim_ids = [x[0] for x in rows]

    def hit(cid):
        barrier.wait()
        return go(None, f"/staff/claims/{cid}/select", {"csrf_token": csrf}, raw=True, cookie=cookie)

    with ThreadPoolExecutor(max_workers=N) as ex:
        results = [f.result() for f in [ex.submit(hit, c) for c in claim_ids]]

    sel = db.execute("SELECT COUNT(*) FROM claims WHERE item_id=? AND status='selected'", (iid,)).fetchone()[0]
    rej = db.execute("SELECT COUNT(*) FROM claims WHERE item_id=? AND status='rejected'", (iid,)).fetchone()[0]
    codes = db.execute("SELECT COUNT(*) FROM handover_codes hc JOIN claims c ON c.id=hc.claim_id"
                       " WHERE c.item_id=?", (iid,)).fetchone()[0]
    ok = sel == 1 and rej == N - 1 and codes == 1
    fails += 0 if ok else 1
    print(f"round {rnd:2d}: 303/200={[c for c,_ in results]} selected={sel} rejected={rej} codes={codes} -> {'OK' if ok else 'BAD'}")

# 同一交还码 6 线程并发核销：最多 1 次成功
print("\n并发核销同一枚码 x6:")
iid = (db.execute("SELECT MAX(id) FROM items").fetchone()[0] or 0) + 1
db.execute("INSERT INTO items(public_description,private_features,status,created_at,updated_at)"
           " VALUES('code','code','kept','2026-01-01T00:00:00+00:00','2026-01-01T00:00:00+00:00')")
db.commit()
_, vis = cj()
_, body = go(vis, f"/items/{iid}/claims", {"answer": "x", "contact": "y"})
cid = db.execute("SELECT id FROM claims WHERE item_id=?", (iid,)).fetchone()[0]
go(None, f"/staff/claims/{cid}/select", {"csrf_token": csrf}, raw=True, cookie=cookie)
code_row = db.execute("SELECT code_hash FROM handover_codes WHERE claim_id=?", (cid,)).fetchone()
# 从查询页取明文
token = db.execute("SELECT token_hash FROM claims WHERE id=?", (cid,)).fetchone()[0]
# 直接由密文解密
from lostfound.security import decrypt_field, load_or_create_secret
secret = load_or_create_secret("/tmp/lfdata/secret.key")
enc = db.execute("SELECT code_enc FROM handover_codes WHERE claim_id=?", (cid,)).fetchone()[0]
plain = decrypt_field(secret, enc)

b2 = threading.Barrier(6)


def hit_code():
    b2.wait()
    return go(None, "/staff/verify", {"csrf_token": csrf, "code": plain}, raw=True, cookie=cookie)


with ThreadPoolExecutor(max_workers=6) as ex:
    res = [f.result() for f in [ex.submit(hit_code) for _ in range(6)]]
ok303 = sum(1 for code, loc in res if code == 303 and loc == f"/staff/items/{iid}")
consumed = db.execute("SELECT COUNT(*) FROM handover_codes WHERE consumed_at IS NOT NULL AND claim_id=?", (cid,)).fetchone()[0]
istat = db.execute("SELECT status FROM items WHERE id=?", (iid,)).fetchone()[0]
print("statuses:", sorted(c for c, _ in res), "303成功:", ok303, "consumed:", consumed, "item:", istat)
bad = not (ok303 == 1 and consumed == 1 and istat == "returned")
fails += 1 if bad else 0
print("\n全部通过" if fails == 0 else f"\n{fails} 轮失败")
raise SystemExit(1 if fails else 0)
