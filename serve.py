#!/usr/bin/env python3
"""
opencode-quota-dashboard
========================
A tiny dashboard for your **opencode go** quota, with a burn-rate projection.

Zero dependencies beyond the Python 3.9+ standard library. Works anywhere
Python runs: no OpenClaw, no Node, no pip, no build step.

Everything is served on one port:

  /            -> the widget (index.html)
  /quota.json  -> live quota, cached for 60 s; `stale` if the upstream fails
  /healthz     -> {ok, age_s, stale, error}

Getting your API key
--------------------
Provide it any of these ways, in priority order:

  1. `OPENCODE_GO_API_KEY` environment variable
  2. `OPENCODE_GO_KEY_FILE` pointing at a file containing the key
  3. A file named `api_key` (or `.opencode-key`) next to this script
  4. An existing OpenClaw install (`openclaw.json`, plain key or SecretRef)

Only the first match is used. If you do not have OpenClaw, use 1, 2 or 3 --
they are completely independent of it.

Run `python3 serve.py --check` to see which source was picked, without
starting the server.

Design notes
------------
* The API key NEVER leaves the server. It is read locally and used only in an
  outbound request header. It is never sent to the browser, never logged.
* Querying `/usage` does not consume quota: it is a billing endpoint, not an
  inference endpoint. No LLM is involved in serving this page.
* The payload is account-wide: three buckets (rolling 5 h, weekly, monthly)
  with status, percent and reset time. There is no per-model breakdown.
* Model-agnostic: no model names and no pricing tables anywhere, so the
  dashboard behaves the same whatever model the account uses.
"""
import argparse
import base64
import ipaddress
import json
import os
import socket
import sqlite3
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from zoneinfo import ZoneInfo

HERE = os.path.dirname(os.path.abspath(__file__))

USAGE_URL = os.environ.get("OPENCODE_GO_USAGE_URL", "https://opencode.ai/zen/go/v1/usage")
UA = "opencode-quota-dashboard/1.0"
TZ = ZoneInfo(os.environ.get("GO_TZ", "UTC"))

PORT = int(os.environ.get("PORT", "8099"))
# "auto" picks loopback on a machine with a display, LAN on a headless host
# (server, container, VPS) where loopback would be unreachable for the user.
BIND = os.environ.get("BIND", "auto")
CACHE_TTL = int(os.environ.get("CACHE_TTL", "60"))     # upstream caching, seconds
STALE_AFTER = int(os.environ.get("STALE_AFTER", "300"))  # past this age -> stale

STATE_FILE = os.environ.get("STATE_FILE", os.path.join(HERE, "state.json"))

# Optional OpenClaw integration. Purely a convenience: if it is not there,
# everything above still works.
_OC_HOME = os.environ.get("OPENCLAW_HOME", os.path.expanduser("~/.openclaw"))
OC_CONFIG = os.environ.get("OPENCLAW_CONFIG", os.path.join(_OC_HOME, "openclaw.json"))
OC_SECRET_DB = os.environ.get("OPENCLAW_SECRET_DB", os.path.join(_OC_HOME, "state", "openclaw.sqlite"))

KEY_FILENAMES = ("api_key", ".opencode-key")

ROUTES = {
    "/": "index.html",
    "/index.html": "index.html",
}

_lock = threading.Lock()
# One cache entry per provider: {provider: {ts, usage, error}}.
_cache = {}


# ------------------------------------------------------------------ key

def _read_file(path):
    try:
        with open(os.path.expanduser(path), encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                # Tolerate KEY=value lines as well as a bare key.
                if "=" in line and not line.startswith("sk-"):
                    line = line.split("=", 1)[1].strip().strip("'\"")
                if line:
                    return line
    except OSError:
        return None
    return None


def _from_openclaw():
    """Read the key from an OpenClaw install, if one is present.

    Supports a plain string and a SecretRef pointing into the gateway's
    SQLite secret store. Returns (key, source) or (None, None).
    """
    try:
        with open(os.path.expanduser(OC_CONFIG), encoding="utf-8") as f:
            cfg = json.load(f)
    except (OSError, ValueError):
        return None, None

    try:
        provider = cfg["models"]["providers"]["opencode-go"]
    except (KeyError, TypeError):
        return None, None

    raw = provider.get("apiKey")
    if isinstance(raw, str) and raw:
        return raw, "openclaw config"

    if isinstance(raw, dict) and raw.get("id"):
        try:
            con = sqlite3.connect(f"file:{os.path.expanduser(OC_SECRET_DB)}?mode=ro", uri=True)
            row = con.execute(
                "SELECT value FROM secret_store_entries "
                "WHERE name = ? AND deleted_at_ms IS NULL "
                "ORDER BY updated_at_ms DESC LIMIT 1", (raw["id"],)).fetchone()
            con.close()
            if row and row[0]:
                return row[0], "openclaw secret store"
        except Exception:
            pass
    return None, None


def resolve_key():
    """Return (key, source). The source is only ever shown by --check."""
    env = os.environ.get("OPENCODE_GO_API_KEY", "").strip()
    if env:
        return env, "OPENCODE_GO_API_KEY"

    key_file = os.environ.get("OPENCODE_GO_KEY_FILE", "").strip()
    if key_file:
        key = _read_file(key_file)
        if key:
            return key, f"file {key_file}"
        return None, None

    for name in KEY_FILENAMES:
        path = os.path.join(HERE, name)
        if os.path.exists(path):
            key = _read_file(path)
            if key:
                return key, f"file {name}"

    return _from_openclaw()


# --------------------------------------------------------------- adapters
#
# One entry per upstream. Every adapter returns (usage, error) where usage is
# the same three-bucket shape the widget already understands:
#   {"rolling": {status, percent, resetsAt}, "weekly": {...}, "monthly": {...}}
# A bucket may be None; the widget prints "no data" for it rather than lying.

# opencode-go stays the default, so an existing install is unaffected.
PROVIDER = os.environ.get("QUOTA_PROVIDER", "opencode-go")

CODEX_AUTH = os.environ.get("CODEX_AUTH_FILE",
                            os.path.expanduser("~/.codex/auth.json"))
OAUTH_TOKEN_URL = "https://auth.openai.com/oauth/token"
OAUTH_CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
WHAM_URL = "https://chatgpt.com/backend-api/wham/usage"
REFRESH_SKEW = 300          # refresh when this close to expiry, in seconds


def _iso(epoch):
    """Unix seconds -> the ISO-8601 'Z' form the widget parses."""
    try:
        return (datetime.fromtimestamp(float(epoch), timezone.utc)
                .isoformat(timespec="seconds").replace("+00:00", "Z"))
    except (TypeError, ValueError, OSError):
        return None


def _jwt_exp(token):
    """Read 'exp' from a JWT without verifying it. None if unreadable."""
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        return json.loads(base64.urlsafe_b64decode(payload)).get("exp")
    except Exception:
        return None


def _load_codex_auth():
    try:
        with open(CODEX_AUTH, encoding="utf-8") as f:
            st = json.load(f)
        return st if isinstance(st, dict) else None
    except (OSError, ValueError):
        return None


def _save_codex_auth(st):
    """Atomic write, mode 0600. Codex reads this same file, so a partial
    write would log it out."""
    tmp = CODEX_AUTH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(st, f)
    os.chmod(tmp, 0o600)
    os.replace(tmp, CODEX_AUTH)


def _refresh_codex(st):
    """Swap the refresh token for a fresh access token. Returns error or None.

    The refresh token can rotate: whatever comes back is persisted, otherwise
    the next refresh would replay a dead token and Codex CLI would log out.
    """
    tokens = st.get("tokens") or {}
    rt = tokens.get("refresh_token")
    if not rt:
        return "no refresh_token in %s" % CODEX_AUTH

    body = urllib.parse.urlencode({
        "grant_type": "refresh_token",
        "refresh_token": rt,
        "client_id": OAUTH_CLIENT_ID,
        "scope": "openid profile email",
    }).encode()
    req = urllib.request.Request(OAUTH_TOKEN_URL, data=body, headers={
        "Content-Type": "application/x-www-form-urlencoded",
        "Accept": "application/json",
        "User-Agent": UA,
    })
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            out = json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        try:
            detail = e.read().decode()[:120]
        except Exception:
            detail = ""
        return "HTTP %s %s" % (e.code, detail)
    except Exception as e:
        return "%s: %s" % (type(e).__name__, e)

    access = out.get("access_token")
    if not access:
        return "refresh response has no access_token"

    tokens["access_token"] = access
    if out.get("refresh_token"):
        tokens["refresh_token"] = out["refresh_token"]
    if out.get("id_token"):
        tokens["id_token"] = out["id_token"]
    st["tokens"] = tokens
    st["last_refresh"] = (datetime.now(timezone.utc)
                          .isoformat(timespec="seconds")
                          .replace("+00:00", "Z"))
    try:
        _save_codex_auth(st)
    except OSError as e:
        return "could not persist refreshed token: %s" % e
    return None


def _window(w):
    """Map one ChatGPT rate-limit window onto a widget bucket."""
    if not isinstance(w, dict):
        return None
    pct = w.get("used_percent")
    if pct is None:
        return None
    return {
        "status": "ok",
        "percent": round(float(pct), 2),
        "resetsAt": _iso(w.get("reset_at")),
    }


def fetch_openai():
    """ChatGPT / Codex plan usage.

    This is the backend endpoint the Codex CLI itself reads, not a documented
    public API, so it is the adapter most likely to break without notice.
    The token is refreshed here instead of being left to expire.
    """
    st = _load_codex_auth()
    if not st:
        return None, "no Codex login at %s" % CODEX_AUTH

    tokens = st.get("tokens") or {}
    access = tokens.get("access_token")
    if not access:
        return None, "no access_token in %s" % CODEX_AUTH

    exp = _jwt_exp(access)
    if exp is not None and exp - time.time() < REFRESH_SKEW:
        err = _refresh_codex(st)
        if err:
            return None, "token refresh failed: %s" % err
        tokens = st.get("tokens") or {}
        access = tokens.get("access_token") or access

    headers = {
        "Authorization": "Bearer " + access,
        "Accept": "application/json",
        "User-Agent": UA,
    }
    if tokens.get("account_id"):
        headers["ChatGPT-Account-Id"] = str(tokens["account_id"])

    req = urllib.request.Request(WHAM_URL, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            body = json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        try:
            detail = e.read().decode()[:120]
        except Exception:
            detail = ""
        return None, "HTTP %s %s" % (e.code, detail)
    except Exception as e:
        return None, "%s: %s" % (type(e).__name__, e)

    rl = body.get("rate_limit") or {}
    return {
        "rolling": _window(rl.get("primary_window")),
        "weekly": _window(rl.get("secondary_window")),
        # A ChatGPT plan exposes no monthly quota window, only credits.
        "monthly": None,
    }, None


# ------------------------------------------------------------- projection

def _load_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            st = json.load(f)
        if isinstance(st, dict) and isinstance(st.get("samples"), list):
            return st
    except (OSError, ValueError):
        pass
    return {"samples": []}


def _save_state(st):
    try:
        tmp = STATE_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(st, f)
        os.replace(tmp, STATE_FILE)
    except OSError:
        pass


def record_sample(usage, ts):
    """Store the monthly percentage so the real burn rate can be measured.

    One sample per 10 minutes, kept for 7 days.
    """
    m = (usage or {}).get("monthly") or {}
    pct = m.get("percent")
    if pct is None:
        return _load_state()
    st = _load_state()
    s = [x for x in st["samples"] if isinstance(x, list) and len(x) == 2]
    s.append([int(ts), float(pct)])
    cutoff = ts - 7 * 86400
    s = [x for x in s if x[0] >= cutoff]
    thinned = []
    for x in s:
        if not thinned or x[0] - thinned[-1][0] >= 600:
            thinned.append(x)
    st["samples"] = thinned
    _save_state(st)
    return st


def projection(usage, st, now):
    """Extrapolate the monthly bucket to its reset date."""
    m = (usage or {}).get("monthly") or {}
    pct, resets = m.get("percent"), m.get("resetsAt")
    if pct is None or not resets:
        return None
    try:
        reset_dt = datetime.fromisoformat(resets.replace("Z", "+00:00"))
    except ValueError:
        return None
    hours_left = (reset_dt - now).total_seconds() / 3600.0
    if hours_left <= 0:
        return None

    rate = basis = None
    samples = st.get("samples") or []
    if len(samples) >= 2:
        span_h = (samples[-1][0] - samples[0][0]) / 3600.0
        delta = samples[-1][1] - samples[0][1]
        if span_h >= 2 and delta >= 0:
            rate = delta / span_h
            basis = "history %s h" % ("%.1f" % span_h)

    # Start of the monthly cycle: the same day of the previous month.
    # Needed even with history, because the bar is drawn from start to reset.
    try:
        cycle_start = reset_dt.replace(month=reset_dt.month - 1)
    except ValueError:
        cycle_start = reset_dt - timedelta(days=30)

    if rate is None:
        # No history yet: assume the cycle started on the same day last month.
        elapsed_h = (now - cycle_start).total_seconds() / 3600.0
        if elapsed_h <= 6:
            return None
        rate = pct / elapsed_h
        basis = "estimated cycle"

    projected = pct + rate * hours_left

    # Date the bucket hits 100 %. If the current rate survives until the reset
    # this stays None and the widget says it does not run out. rate is %/h.
    exhaust_at = None
    days_before_reset = None
    hours_to_100 = None
    if rate > 0 and pct < 100:
        hours_to_100 = (100.0 - pct) / rate
        if hours_to_100 <= hours_left:
            hit = now + timedelta(hours=hours_to_100)
            exhaust_at = hit.isoformat(timespec="seconds")
            days_before_reset = round((hours_left - hours_to_100) / 24.0, 1)

    return {
        "current": round(pct, 1),
        "projected": round(min(projected, 999), 1),
        "margin": round(100 - projected, 1),
        "rate_per_day": round(rate * 24, 2),
        "hours_left": round(hours_left, 1),
        "basis": basis,
        "over": projected >= 100,
        "resets_at": resets,
        "exhaust_at": exhaust_at,
        "hours_to_exhaust": (round(hours_to_100, 1) if exhaust_at else None),
        "days_to_exhaust": (round((100.0 - pct) / rate / 24.0, 1) if exhaust_at else None),
        "days_before_reset": days_before_reset,
        "days_to_reset": round(hours_left / 24.0, 1),
        # Timeline for the bar: cycle start -> reset.
        "cycle_start": cycle_start.isoformat(timespec="seconds"),
        "hours_total": round((reset_dt - cycle_start).total_seconds() / 3600.0, 1),
        "hours_elapsed": round((now - cycle_start).total_seconds() / 3600.0, 1),
    }


# ----------------------------------------------------------------- fetch

def fetch_opencode_go():
    """Return (usage, None) or (None, 'reason')."""
    key, _source = resolve_key()
    if not key:
        return None, ("no API key found: set OPENCODE_GO_API_KEY, "
                      "OPENCODE_GO_KEY_FILE, or an OpenClaw install")
    req = urllib.request.Request(USAGE_URL, headers={
        "User-Agent": UA, "Authorization": f"Bearer {key}"})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            body = json.loads(r.read().decode())
        usage = body.get("usage")
        if not isinstance(usage, dict):
            return None, "response has no usage field"
        return usage, None
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode()[:160]
        except Exception:
            pass
        return None, f"HTTP {e.code} {detail}".strip()
    except Exception as e:
        return None, f"{type(e).__name__}: {e}"


ADAPTERS = {
    "opencode-go": fetch_opencode_go,
    "openai": fetch_openai,
}


def fetch_usage(provider=None):
    """Dispatch to the adapter for `provider` (default: QUOTA_PROVIDER)."""
    adapter = ADAPTERS.get(provider or PROVIDER)
    if not adapter:
        return None, "unknown provider %r" % (provider or PROVIDER)
    return adapter()


def current(provider=None):
    """Cache the last good reading per provider.

    A provider that fails keeps its week-old-good reading and reports the
    error, rather than blanking the other providers on the same page.
    """
    provider = provider or PROVIDER
    now = time.time()
    with _lock:
        c = _cache.setdefault(provider, {"ts": 0.0, "usage": None, "error": None})
        if c["usage"] is not None and (now - c["ts"]) < CACHE_TTL:
            return _payload(now, provider)

        usage, err = fetch_usage(provider)
        if usage is not None:
            c.update(ts=now, usage=usage, error=None)
            record_sample(usage, now)
        else:
            c["error"] = err  # keep the last good reading if we have one
        return _payload(now, provider)


def _payload(now, provider=None):
    provider = provider or PROVIDER
    c = _cache.get(provider) or {"ts": 0.0, "usage": None, "error": "not fetched"}
    age = int(now - c["ts"]) if c["usage"] is not None else None
    utc_now = datetime.now(timezone.utc)
    return {
        "ok": c["usage"] is not None and c["error"] is None,
        "provider": provider,
        "generated": utc_now.isoformat(timespec="seconds"),
        "age_s": age,
        "stale": c["usage"] is None or age is None or age > STALE_AFTER,
        "error": c["error"],
        "usage": c["usage"],
        "projection": projection(c["usage"], _load_state(), utc_now),
    }


class Handler(BaseHTTPRequestHandler):
    server_version = "opencode-quota-dashboard/1.0"
    sys_version = ""   # do not advertise the Python version

    def log_message(self, fmt, *args):
        # Low volume (60 s cache): one line per request in the journal.
        print("[%s] %s" % (datetime.now(TZ).strftime("%H:%M:%S"), fmt % args), flush=True)

    def _send(self, code, body, ctype, cache="no-store"):
        data = body if isinstance(body, bytes) else body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", cache)
        self.end_headers()
        try:
            self.wfile.write(data)
        except BrokenPipeError:
            pass

    def _json(self, code, obj):
        self._send(code, json.dumps(obj, ensure_ascii=False),
                   "application/json; charset=utf-8")

    def do_GET(self):
        raw = self.path.split("?")[0]
        query = urllib.parse.urlparse(self.path).query
        q = urllib.parse.parse_qs(query)
        provider = (q.get("provider") or [PROVIDER])[0]

        if raw == "/quota.json":
            self._json(200, current(provider))
            return
        if raw == "/providers.json":
            self._json(200, {"providers": list(ADAPTERS), "default": PROVIDER})
            return
        if raw == "/healthz":
            p = current(provider)
            self._json(200, {k: p[k] for k in ("ok", "provider", "age_s",
                                               "stale", "error")})
            return

        name = ROUTES.get(raw)
        if not name:
            self._send(404, "not found", "text/plain; charset=utf-8")
            return
        try:
            with open(os.path.join(HERE, name), "rb") as f:
                self._send(200, f.read(), "text/html; charset=utf-8")
        except OSError:
            self._send(500, f"{name} not found", "text/plain; charset=utf-8")


def check():
    """Print where the key comes from and whether the endpoint answers."""
    key, source = resolve_key()
    if not key:
        print("key    : NOT FOUND")
        print("         Set OPENCODE_GO_API_KEY, or OPENCODE_GO_KEY_FILE, or put")
        print("         the key in a file named api_key next to this script.")
        return 1
    print(f"key    : found via {source} ({len(key)} chars, ending "
          f"...{key[-4:] if len(key) >= 4 else '****'})")
    usage, err = fetch_usage()
    if err:
        print(f"upstream: FAILED - {err}")
        return 1
    for bucket in ("rolling", "weekly", "monthly"):
        b = usage.get(bucket) or {}
        print(f"{bucket:<8}: {b.get('percent')}%  status={b.get('status')}  "
              f"resets={b.get('resetsAt')}")
    return 0


def find_free_port(bind, start, attempts=50):
    """Return the first free TCP port at or after `start`.

    Agents should not have to guess a port or resolve a collision by hand.
    """
    host = "" if bind in ("0.0.0.0", "::") else bind
    for port in range(start, start + attempts):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            # Match what HTTPServer does (allow_reuse_address). Without this a
            # socket in TIME_WAIT looks busy, so the probe would report a
            # different port than the one the server can actually bind, and the
            # chosen port would drift upward on every run.
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                s.bind((host, port))
                return port
            except OSError:
                continue
    raise SystemExit(f"no free port in {start}-{start + attempts - 1}")


def _lan_ip():
    """Best-effort LAN address of this host, for human-facing URLs only."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        # TEST-NET-1: routing lookup only, no packet is sent.
        s.connect(("192.0.2.1", 9))
        return s.getsockname()[0]
    except OSError:
        return None
    finally:
        s.close()


def public_url(bind, port):
    """Turn a bind address into a URL someone can actually click.

    A wildcard bind is not a destination: reporting http://0.0.0.0:8099/ hands
    the user a dead link. Resolve it to the machine's LAN address instead.
    """
    if bind in ("0.0.0.0", "::", ""):
        return f"http://{_lan_ip() or 'localhost'}:{port}/"
    return f"http://{bind}:{port}/"


def resolve_bind(bind):
    """Turn "auto" into a concrete address, so nobody has to decide.

    On a machine with a graphical session, the user opens the dashboard in a
    browser on that same machine, so loopback is correct and safest. On a
    headless host (server, LXC, VPS, container) loopback is useless: there is no
    browser there to open it. Binding to the LAN address is the only setting
    that actually works, so pick it instead of stopping to ask.

    The exception is a host whose only address is public (a VPS): there LAN and
    "the internet" are the same thing, and this dashboard has no authentication.
    Rather than expose it silently, stay on loopback and let the notes say so.
    """
    if bind != "auto":
        return bind
    if os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"):
        return "127.0.0.1"
    ip = _lan_ip()
    if ip and not ipaddress.ip_address(ip).is_private:
        return "127.0.0.1"   # public-only host: never expose an open dashboard
    return "0.0.0.0"


def is_loopback(bind):
    return bind in ("127.0.0.1", "localhost", "::1")


def bind_warning(bind):
    """One honest sentence about what this binding exposes, or None."""
    if not is_loopback(bind):
        return ("bound to a non-loopback address and there is no authentication; "
                "keep it on a trusted network")
    ip = _lan_ip()
    if ip and not ipaddress.ip_address(ip).is_private:
        return ("this host appears to have a public address, so it stayed on "
                "loopback to avoid exposing an unauthenticated dashboard; "
                "use WireGuard or an SSH tunnel to reach it")
    return None


def check_json(port_arg, bind):
    """Machine-readable variant of --check, for agent-driven installs.

    Never contains the key itself: only its source and last 4 characters.
    """
    key, source = resolve_key()
    bind = resolve_bind(bind)
    out = {
        "ok": False,
        "key_found": bool(key),
        "key_source": source,
        "key_tail": (key[-4:] if key and len(key) >= 4 else None),
        "bind": bind,
        "port": None,
        "url": None,
        "upstream": None,
        "buckets": None,
        "error": None,
        "next_action": None,
    }

    try:
        out["port"] = (find_free_port(bind, PORT) if port_arg == "auto"
                       else int(port_arg))
        out["url"] = public_url(bind, out["port"])
        out["local_url"] = f"http://127.0.0.1:{out['port']}/"
    except SystemExit as e:
        out["error"] = str(e)
        out["next_action"] = "pass --port with a free port"
        print(json.dumps(out))
        return 1

    if not key:
        out["error"] = "no API key found"
        out["next_action"] = ("set OPENCODE_GO_API_KEY, or OPENCODE_GO_KEY_FILE, "
                              "or write the key to ./api_key")
        print(json.dumps(out))
        return 1

    usage, err = fetch_usage()
    if err:
        out["error"] = err
        out["next_action"] = ("the key was found but rejected; check it is a "
                              "valid opencode go key")
        print(json.dumps(out))
        return 1

    out["ok"] = True
    out["upstream"] = "ok"
    out["notes"] = bind_warning(bind)
    out["buckets"] = {
        b: {"percent": (usage.get(b) or {}).get("percent"),
            "resets_at": (usage.get(b) or {}).get("resetsAt")}
        for b in ("rolling", "weekly", "monthly")
    }
    print(json.dumps(out))
    return 0


def main():
    ap = argparse.ArgumentParser(
        description="opencode go quota dashboard",
        epilog="The API key is read from OPENCODE_GO_API_KEY, OPENCODE_GO_KEY_FILE, "
               "an api_key file next to this script, or an OpenClaw install.")
    ap.add_argument("--check", action="store_true",
                    help="verify the key and the upstream endpoint, then exit")
    ap.add_argument("--json", action="store_true",
                    help="emit JSON (as the only output) instead of prose")
    ap.add_argument("--port", default=str(PORT),
                    help=f"listen port, or 'auto' to pick the first free one "
                         f"(default {PORT})")
    ap.add_argument("--bind", default=BIND,
                    help=f"listen address, or 'auto' (default {BIND}): loopback "
                         f"when there is a display, LAN on a headless host")
    args = ap.parse_args()

    if args.check:
        sys.exit(check_json(args.port, args.bind) if args.json else check())

    bind = resolve_bind(args.bind)

    if args.port == "auto":
        port = find_free_port(bind, PORT)
    else:
        try:
            port = int(args.port)
        except ValueError:
            raise SystemExit(f"--port must be an integer or 'auto', got {args.port!r}")

    srv = ThreadingHTTPServer((bind, port), Handler)
    srv.daemon_threads = True

    url = public_url(bind, port)
    if args.json:
        print(json.dumps({"event": "listening", "url": url,
                          "port": port, "bind": bind}), flush=True)
    else:
        print(f"opencode-quota-dashboard on {url}  (TZ {TZ})", flush=True)
    warn = bind_warning(bind)
    if warn:
        print(f"  note: {warn}", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
