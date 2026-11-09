"""
Doorman - the small approval server for Excel Auto Typer.

A new PC files a request here. You open the Doorman web app on your phone
(added to the Home Screen) and tap Approve once / Always allow / Deny.

Runs on Vercel (data kept in Upstash Redis) and on ordinary hosts (SQLite file).

Settings come from environment variables (or config.json when you run it yourself):
  APP_KEY          shared secret the Windows app sends with each request
  OWNER_PASSWORD   the password you type on your phone to sign in
  SECRET_KEY       (optional) signs your sign-in token
  VAPID_PRIVATE_KEY / VAPID_PUBLIC_KEY / VAPID_SUBJECT   (optional) lock-screen alerts
  KV_REST_API_URL / KV_REST_API_TOKEN   set automatically by Vercel's Upstash Redis
  DB_PATH          (optional) SQLite location when Redis isn't used
"""
import contextlib
import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
import threading
import time
import urllib.request
from pathlib import Path

from flask import Flask, jsonify, request, send_from_directory
from werkzeug.exceptions import HTTPException

BASE = Path(__file__).resolve().parent
PUBLIC = BASE / "public"
try:
    FILE_CFG = json.loads((BASE / "config.json").read_text(encoding="utf-8"))
except Exception:
    FILE_CFG = {}


def cfg(name, default=""):
    return os.environ.get(name) or FILE_CFG.get(name) or default


ON_VERCEL = bool(os.environ.get("VERCEL"))
APP_KEY = cfg("APP_KEY")
OWNER_PASSWORD = cfg("OWNER_PASSWORD")
SECRET = (cfg("SECRET_KEY")
          or hashlib.sha256(f"{OWNER_PASSWORD}|{APP_KEY}".encode()).hexdigest()).encode()
VAPID_PRIVATE = cfg("VAPID_PRIVATE_KEY")
VAPID_PUBLIC = cfg("VAPID_PUBLIC_KEY")
VAPID_SUBJECT = cfg("VAPID_SUBJECT", "mailto:owner@example.com")
KV_URL = (cfg("KV_REST_API_URL") or cfg("UPSTASH_REDIS_REST_URL")).strip().strip("\"'")
KV_TOKEN = (cfg("KV_REST_API_TOKEN") or cfg("UPSTASH_REDIS_REST_TOKEN")).strip().strip("\"'")
DB_PATH = cfg("DB_PATH", "/tmp/doorman.db" if ON_VERCEL else str(BASE / "doorman.db"))

TTL = 300           # seconds a request stays open
KEEP = 3600         # seconds finished requests are remembered
TOKEN_DAYS = 30     # how long a phone stays signed in
MAX_PENDING = 25
MAX_PENDING_PER_IP = 6

app = Flask(__name__, static_folder=None)


# ------------------------------------------------------------------ storage
class RedisStore:
    """Upstash Redis over its REST API (what Vercel's Storage tab creates)."""
    kind = "redis"

    def __init__(self, url, token):
        self.url, self.token = url.rstrip("/"), token

    def _cmd(self, *cmd):
        req = urllib.request.Request(
            self.url, json.dumps(list(cmd)).encode(),
            {"Authorization": "Bearer " + self.token, "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=8) as r:
            data = json.loads(r.read().decode())
        if isinstance(data, dict) and data.get("error"):
            raise RuntimeError(data["error"])
        return data.get("result") if isinstance(data, dict) else data

    @staticmethod
    def _pairs(res):
        if isinstance(res, dict):
            return res
        res = res or []
        return {res[i]: res[i + 1] for i in range(0, len(res) - 1, 2)}

    def req_put(self, rid, obj):
        self._cmd("HSET", "reqs", rid, json.dumps(obj))

    def req_get(self, rid):
        v = self._cmd("HGET", "reqs", rid)
        return json.loads(v) if v else None

    def req_all(self):
        return {k: json.loads(v) for k, v in self._pairs(self._cmd("HGETALL", "reqs")).items()}

    def req_del(self, ids):
        if ids:
            self._cmd("HDEL", "reqs", *ids)

    def sub_put(self, endpoint, data):
        self._cmd("HSET", "subs", endpoint, data)

    def sub_all(self):
        return self._pairs(self._cmd("HGETALL", "subs"))

    def sub_del(self, endpoint):
        self._cmd("HDEL", "subs", endpoint)

    def fail_count(self, ip):
        return int(self._cmd("GET", f"fail:{ip}") or 0)

    def fail_add(self, ip):
        self._cmd("INCR", f"fail:{ip}")
        self._cmd("EXPIRE", f"fail:{ip}", 600)

    def fail_clear(self, ip):
        self._cmd("DEL", f"fail:{ip}")


class SqliteStore:
    """Plain file database for hosts with a real disk."""
    kind = "sqlite"

    def __init__(self, path):
        self.path = path
        self.fails = {}
        with self._c() as c:
            c.executescript("""
                CREATE TABLE IF NOT EXISTS reqs (id TEXT PRIMARY KEY, data TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS subs (endpoint TEXT PRIMARY KEY, data TEXT NOT NULL);
            """)

    @contextlib.contextmanager
    def _c(self):
        c = sqlite3.connect(self.path, timeout=15)
        try:
            yield c
            c.commit()
        finally:
            c.close()

    def req_put(self, rid, obj):
        with self._c() as c:
            c.execute("INSERT OR REPLACE INTO reqs VALUES (?,?)", (rid, json.dumps(obj)))

    def req_get(self, rid):
        with self._c() as c:
            r = c.execute("SELECT data FROM reqs WHERE id=?", (rid,)).fetchone()
        return json.loads(r[0]) if r else None

    def req_all(self):
        with self._c() as c:
            return {k: json.loads(v) for k, v in c.execute("SELECT id, data FROM reqs")}

    def req_del(self, ids):
        with self._c() as c:
            c.executemany("DELETE FROM reqs WHERE id=?", [(i,) for i in ids])

    def sub_put(self, endpoint, data):
        with self._c() as c:
            c.execute("INSERT OR REPLACE INTO subs VALUES (?,?)", (endpoint, data))

    def sub_all(self):
        with self._c() as c:
            return dict(c.execute("SELECT endpoint, data FROM subs").fetchall())

    def sub_del(self, endpoint):
        with self._c() as c:
            c.execute("DELETE FROM subs WHERE endpoint=?", (endpoint,))

    def fail_count(self, ip):
        now = time.time()
        self.fails[ip] = [t for t in self.fails.get(ip, []) if now - t < 600]
        return len(self.fails[ip])

    def fail_add(self, ip):
        self.fails.setdefault(ip, []).append(time.time())

    def fail_clear(self, ip):
        self.fails.pop(ip, None)


STORE = RedisStore(KV_URL, KV_TOKEN) if (KV_URL and KV_TOKEN) else None
if STORE is None and not ON_VERCEL:
    STORE = SqliteStore(DB_PATH)

NO_DB_MSG = ("The server has no database yet. In Vercel open Storage, connect "
             "Upstash Redis to this project, then redeploy.")


def storage_ready():
    return STORE is not None


# ------------------------------------------------------------------ helpers
def clean(value, n=60):
    return re.sub(r"[\x00-\x1f\x7f<>]", "", str(value or "")).strip()[:n]


def client_ip():
    fwd = request.headers.get("X-Forwarded-For", "")
    return (fwd.split(",")[0].strip() or request.remote_addr or "?")[:45]


def make_token():
    exp = int(time.time()) + TOKEN_DAYS * 86400
    body = f"owner.{exp}"
    return f"{body}.{hmac.new(SECRET, body.encode(), hashlib.sha256).hexdigest()}"


def token_ok(tok):
    try:
        role, exp, sig = tok.split(".")
        good = hmac.new(SECRET, f"{role}.{exp}".encode(), hashlib.sha256).hexdigest()
        return role == "owner" and int(exp) > time.time() and hmac.compare_digest(sig, good)
    except Exception:
        return False


def owner_ok():
    h = request.headers.get("Authorization", "")
    return h.startswith("Bearer ") and token_ok(h[7:])


def app_key_ok():
    return bool(APP_KEY) and hmac.compare_digest(
        request.headers.get("X-App-Key", "").encode(), APP_KEY.encode())


def fail(msg, code):
    return jsonify(error=msg), code


def effective(r, now=None):
    """Status with expiry applied (nothing is written for expiry)."""
    now = now or time.time()
    if r["status"] == "pending" and now - r["created"] > TTL:
        return "expired"
    return r["status"]


@app.errorhandler(Exception)
def crash(e):
    if isinstance(e, HTTPException):  # normal web errors (404, 405 ...)
        return fail(e.description, e.code)
    app.logger.exception("Unhandled server error")
    return fail("The server hit a problem talking to its database. Try again.", 500)


@app.after_request
def headers(resp):
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["Referrer-Policy"] = "no-referrer"
    if request.path.startswith("/api/"):
        resp.headers["Cache-Control"] = "no-store"
    return resp


# ------------------------------------------------------------------ the web app files
# (On Vercel the files in /public are served by its CDN; these routes serve them elsewhere.)
@app.get("/")
def index():
    resp = send_from_directory(PUBLIC, "index.html")
    resp.headers["Cache-Control"] = "no-cache"
    return resp


@app.get("/sw.js")
def service_worker():
    resp = send_from_directory(PUBLIC, "sw.js", mimetype="application/javascript")
    resp.headers["Cache-Control"] = "no-cache"
    return resp


@app.get("/manifest.json")
def manifest():
    return send_from_directory(PUBLIC, "manifest.json", mimetype="application/manifest+json")


@app.get("/static/<path:name>")
def static_files(name):
    return send_from_directory(PUBLIC / "static", name)


# ------------------------------------------------------------------ owner API
@app.post("/api/login")
def login():
    if not OWNER_PASSWORD:
        return fail("The server isn't set up yet (no OWNER_PASSWORD).", 503)
    if not storage_ready():
        return fail(NO_DB_MSG, 503)
    ip = client_ip()
    if STORE.fail_count(ip) >= 5:
        return fail("Too many wrong tries. Wait 10 minutes.", 429)
    pw = (request.get_json(silent=True) or {}).get("password", "")
    if hmac.compare_digest(str(pw).encode(), OWNER_PASSWORD.encode()):
        STORE.fail_clear(ip)
        return jsonify(token=make_token())
    STORE.fail_add(ip)
    time.sleep(0.8)
    return fail("Wrong password.", 401)


@app.get("/api/pending")
def pending():
    if not owner_ok():
        return fail("Sign in first.", 401)
    if not storage_ready():
        return fail(NO_DB_MSG, 503)
    now = time.time()
    pend, done = [], []
    for rid, r in STORE.req_all().items():
        st = effective(r, now)
        row = {"id": rid, "computer": r["computer"], "user": r["user"], "code": r["code"],
               "ip": r["ip"], "status": st, "age": int(now - r["created"]),
               "_t": r.get("decided") or r["created"]}
        (pend if st == "pending" else done).append(row)
    pend.sort(key=lambda x: -x["_t"])
    done.sort(key=lambda x: -x["_t"])
    for row in pend + done:
        row.pop("_t")
    return jsonify(pending=pend, recent=done[:12], ttl=TTL,
                   push=bool(VAPID_PUBLIC and VAPID_PRIVATE))


@app.post("/api/decide")
def decide():
    if not owner_ok():
        return fail("Sign in first.", 401)
    if not storage_ready():
        return fail(NO_DB_MSG, 503)
    data = request.get_json(silent=True) or {}
    action = data.get("action")
    if action not in ("once", "always", "deny"):
        return fail("Unknown action.", 400)
    r = STORE.req_get(str(data.get("id", "")))
    if not r or effective(r) != "pending":
        return fail("That request is no longer waiting.", 409)
    r["status"], r["decided"] = action, time.time()
    STORE.req_put(str(data["id"]), r)
    return jsonify(ok=True)


# ------------------------------------------------------------------ Windows app API
@app.get("/api/ping")
def ping():
    if not app_key_ok():
        return fail("Wrong app key.", 401)
    if not storage_ready():
        return fail(NO_DB_MSG, 503)
    return jsonify(ok=True, push=bool(VAPID_PUBLIC and VAPID_PRIVATE), storage=STORE.kind)


@app.post("/api/request")
def create_request():
    if not APP_KEY:
        return fail("The server isn't set up yet (no APP_KEY).", 503)
    if not app_key_ok():
        return fail("Wrong app key.", 401)
    if not storage_ready():
        return fail(NO_DB_MSG, 503)
    data = request.get_json(silent=True) or {}
    ip = client_ip()
    now = time.time()
    existing = STORE.req_all()
    pend = [r for r in existing.values() if effective(r, now) == "pending"]
    if len(pend) >= MAX_PENDING or sum(1 for r in pend if r["ip"] == ip) >= MAX_PENDING_PER_IP:
        return fail("Too many open requests. Try again in a few minutes.", 429)
    STORE.req_del([k for k, r in existing.items() if now - r["created"] > KEEP])
    rid, secret = secrets.token_urlsafe(8), secrets.token_urlsafe(16)
    computer = clean(data.get("computer")) or "Unknown PC"
    code = clean(data.get("code"), 8)
    STORE.req_put(rid, {"poll_secret": secret, "computer": computer,
                        "user": clean(data.get("user")) or "?", "code": code, "ip": ip,
                        "created": now, "status": "pending", "decided": None})
    payload = {"title": "Access request",
               "body": f"{computer} wants to use Excel Auto Typer. Code {code}", "id": rid}
    if ON_VERCEL:   # serverless: must finish before we answer
        send_push(payload)
    else:
        threading.Thread(target=send_push, args=(payload,), daemon=True).start()
    return jsonify(id=rid, poll_secret=secret, expires_in=TTL)


def _own(rid):
    r = STORE.req_get(rid)
    if not r or not hmac.compare_digest(request.args.get("s", "").encode(),
                                        r["poll_secret"].encode()):
        return None
    return r


@app.get("/api/status/<rid>")
def status(rid):
    if not storage_ready():
        return fail(NO_DB_MSG, 503)
    r = _own(rid)
    if not r:
        return fail("Unknown request.", 404)
    return jsonify(status=effective(r))


@app.post("/api/cancel/<rid>")
def cancel(rid):
    if not storage_ready():
        return fail(NO_DB_MSG, 503)
    r = _own(rid)
    if not r:
        return fail("Unknown request.", 404)
    if r["status"] == "pending":
        r["status"], r["decided"] = "expired", time.time()
        STORE.req_put(rid, r)
    return jsonify(ok=True)


# ------------------------------------------------------------------ lock-screen alerts
@app.get("/api/push/key")
def push_key():
    return jsonify(key=VAPID_PUBLIC if (VAPID_PUBLIC and VAPID_PRIVATE) else "")


@app.post("/api/push/subscribe")
def push_subscribe():
    if not owner_ok():
        return fail("Sign in first.", 401)
    if not storage_ready():
        return fail(NO_DB_MSG, 503)
    sub = (request.get_json(silent=True) or {}).get("subscription") or {}
    endpoint = sub.get("endpoint", "")
    if not endpoint.startswith("https://") or "keys" not in sub:
        return fail("Bad subscription.", 400)
    STORE.sub_put(endpoint, json.dumps(sub))
    return jsonify(ok=True)


def send_push(payload):
    if not (VAPID_PRIVATE and VAPID_PUBLIC and STORE):
        return
    try:
        from pywebpush import WebPushException, webpush
    except ImportError:
        return
    try:
        subs = STORE.sub_all()
    except Exception:
        return
    for endpoint, data in subs.items():
        try:
            webpush(subscription_info=json.loads(data), data=json.dumps(payload),
                    vapid_private_key=VAPID_PRIVATE, vapid_claims={"sub": VAPID_SUBJECT},
                    ttl=120, timeout=6)
        except WebPushException as e:
            code = getattr(getattr(e, "response", None), "status_code", None)
            if code in (404, 410):  # the phone unsubscribed
                try:
                    STORE.sub_del(endpoint)
                except Exception:
                    pass
        except Exception:
            pass


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5000"))
    print(f"Doorman running on http://localhost:{port}  (storage: {STORE.kind if STORE else 'none'})")
    app.run(host="0.0.0.0", port=port)
