import os
import json
import base64
import time
import urllib.request
from flask import Flask, send_from_directory, make_response
from waitress import serve

app = Flask(__name__, static_folder=None)

GITHUB_OWNER    = "shadyy000777-commits"
GITHUB_REPO     = "AFTERSHOCK-TIERS"
GITHUB_API_BASE = f"https://api.github.com/repos/{GITHUB_OWNER}/{GITHUB_REPO}"
GITHUB_RAW_BASE = f"https://raw.githubusercontent.com/{GITHUB_OWNER}/{GITHUB_REPO}/main"
WEBSITE_DIR     = os.path.join(os.path.dirname(os.path.abspath(__file__)), "website")
STATIC_DIR      = os.path.join(WEBSITE_DIR, "static")

# GitHub fallback cache — only used when DATABASE_URL is not set
_tiers_cache: dict = {"data": None, "ts": 0.0}
TIERS_CACHE_TTL = 30  # seconds


def _fetch(url: str, timeout: int = 10, nocache: bool = False):
    headers = {"User-Agent": "railway-server/1.0"}
    if nocache:
        headers["Cache-Control"] = "no-cache, no-store"
        headers["Pragma"] = "no-cache"
        sep = "&" if "?" in url else "?"
        url = f"{url}{sep}_={int(__import__('time').time())}"
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read(), r.headers.get("Content-Type", "application/octet-stream")


def _fetch_via_api(path: str, timeout: int = 10):
    """Fetch a file from GitHub via the Contents API (always returns current data, no CDN cache)."""
    url = f"{GITHUB_API_BASE}/contents/{path}"
    headers = {
        "User-Agent": "railway-server/1.0",
        "Accept": "application/vnd.github.v3+json",
    }
    token = os.getenv("GITHUB_TOKEN")
    if token:
        headers["Authorization"] = f"token {token}"
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = json.loads(r.read())
    return base64.b64decode(data["content"].replace("\n", ""))


def _ensure_db_table(conn):
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS tiers_data (
            id INTEGER PRIMARY KEY DEFAULT 1,
            data JSONB NOT NULL,
            updated_at TIMESTAMPTZ DEFAULT NOW()
        )
    """)
    conn.commit()
    cur.close()


def _fetch_from_db() -> bytes | None:
    """Read tiers_data from the shared PostgreSQL database. Returns None if unavailable."""
    db_url = os.getenv("DATABASE_URL")
    if not db_url:
        return None
    # psycopg2 needs postgresql://, Railway provides postgres://
    db_url = db_url.replace("postgres://", "postgresql://", 1)
    try:
        import psycopg2
        import psycopg2.extras
        conn = psycopg2.connect(db_url)
        _ensure_db_table(conn)
        cur = conn.cursor()
        cur.execute("SELECT data FROM tiers_data WHERE id = 1")
        row = cur.fetchone()
        cur.close()
        conn.close()
        if row:
            return json.dumps(row[0]).encode()
        return None
    except Exception as e:
        print(f"[DB] Read error: {e}")
        return None


@app.route("/")
def index():
    html_path = os.path.join(WEBSITE_DIR, "index.html")
    with open(html_path, "r", encoding="utf-8") as f:
        content = f.read()
    resp = make_response(content, 200)
    resp.headers["Content-Type"] = "text/html; charset=utf-8"
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    return resp


@app.route("/tiers_data.json")
def tiers_data():
    # Try database first — instant, always current
    db_data = _fetch_from_db()
    if db_data:
        resp = make_response(db_data, 200)
        resp.headers["Content-Type"] = "application/json"
        resp.headers["Access-Control-Allow-Origin"] = "*"
        resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        return resp

    # Fall back to GitHub API (used when DATABASE_URL not set)
    now = time.time()
    if _tiers_cache["data"] is None or now - _tiers_cache["ts"] > TIERS_CACHE_TTL:
        try:
            fresh = _fetch_via_api("tiers_data.json")
            _tiers_cache["data"] = fresh
            _tiers_cache["ts"] = now
            print(f"[Railway] tiers_data cache refreshed ({len(fresh)} bytes)")
        except Exception as e:
            print(f"[Railway] tiers_data fetch error: {e}")
            if _tiers_cache["data"] is None:
                # Try local seed file before giving up
                local_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tiers_data.json")
                if os.path.exists(local_path):
                    with open(local_path, "rb") as f:
                        _tiers_cache["data"] = f.read()
                    print("[Railway] Serving local tiers_data.json seed")
                else:
                    return make_response('{"players":{}}', 502)
            # Serve stale/seed cache rather than a hard error

    resp = make_response(_tiers_cache["data"], 200)
    resp.headers["Content-Type"] = "application/json"
    resp.headers["Access-Control-Allow-Origin"] = "*"
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    return resp


@app.route("/static/skins/<path:filename>")
@app.route("/skins/<path:filename>")
def serve_skin(filename):
    """Skins pushed by the bot to AFTERSHOCK-TIERS/static/skins/ — proxy them live."""
    url = f"{GITHUB_RAW_BASE}/static/skins/{filename}"
    try:
        data, content_type = _fetch(url)
        resp = make_response(data, 200)
        resp.headers["Content-Type"] = content_type
        resp.headers["Cache-Control"] = "public, max-age=300"
        return resp
    except Exception:
        return make_response("", 404)


@app.route("/static/<path:filename>")
def serve_static(filename):
    """Logo, bg.gif and other static assets — served locally from the repo."""
    return send_from_directory(STATIC_DIR, filename)


@app.route("/tiers_data.json", methods=["OPTIONS"])
def cors_preflight():
    resp = make_response("", 204)
    resp.headers["Access-Control-Allow-Origin"] = "*"
    resp.headers["Access-Control-Allow-Methods"] = "GET, OPTIONS"
    resp.headers["Access-Control-Allow-Headers"] = "Content-Type"
    return resp


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    print(f"[Railway] Web server starting on port {port}")
    serve(app, host="0.0.0.0", port=port, threads=8)
