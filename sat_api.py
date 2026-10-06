#!/usr/bin/env python3
"""
SAT-1 Link Simulator — Chapter 1 Lab
GEO bent-pipe, Ku-band. STORM OUTAGE scenario: 3-stage restore.
  Stage 1 (PHY/power): rain fade + antenna drift -> C/N below lock. Fix: repoint + HPA boost.
  Stage 2 (PHY/MODCOD): 256APSK bandwidth-limited -> BER collapse. Fix: fallback QPSK_3/4 (QPSK_1/2 safe but slow).
  Stage 3 (transport): BDP starvation, 64KB window vs ~500ms RTT. Fix: PEP + SACK + window>=2500.
Run: python3 sat_api.py [--port 8765]
  GET  /status?student=ID      -> full dump (thr, status up/down, stage, hint, rf, tcp, ttc)
  GET  /tools/rf?student=ID    -> RF/PHY filtered view (same numbers as /status rf block)
  GET  /tools/tcp?student=ID   -> transport filtered view
  GET  /tools/ttc?student=ID   -> TT&C filtered view
  POST /control                -> {"cmd": "...", "value": ..., "student": "ID"}
  GET  /report?student=ID      -> own progress (stage, config, command log)
  GET  /report/all?key=KEY     -> whole-class results (instructor only, needs REPORT_KEY env)
Per-student isolation: each student ID gets its own simulator state on the
same shared Render service, so one student solving does not solve it for others.
Omit student -> shared fallback session ("shared").
Control budget: 30 mutating commands per student (reset excluded, survives reset).
Reach status "up" (thr >= 35 Mbps) to clear the outage.
Game guidance lives in the API, not the handout: /status always returns a
'hint' for the current stage, and /control returns a 'message' exactly when
a stage is cleared (final clear = success message).
Results persist in SQLite (DB_PATH env, default ./satlab.db): every mutation
is written through; sessions + budgets are reloaded on boot. A cloud mirror
(Upstash Redis via REST, UPSTASH_URL + UPSTASH_TOKEN env) survives even a full
disk wipe; everything fails open to in-memory play.
"""
import argparse
import copy
import json
import math
import os
import re
import sqlite3
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

def fresh_state():
    return {
        "payload": "BENT",
        "orbit_km": 35786,
        "freq_ghz": 12.2,
        "hpa_pct": 68,
        "modcod": "256APSK_9/10",
        "tcp_win_kb": 64,
        "sack": 0,
        "pep": 0,
        "link_arq": 0,
        "antenna_ok": False,   # wind drift: needs `repoint`
        "reboots": 0,
        "commands_log": [],
        "down_until": 0.0,
    }

SESSIONS = {}
SESSIONS_LOCK = threading.Lock()
SESSIONS_LAST = {}
MAX_SESSIONS = 500

def normalize_sid(raw):
    s = str(raw or "shared").strip().lower()
    s = re.sub(r"[^a-z0-9-_]", "", s)[:64]
    return s or "shared"

def get_state(sid):
    sid = normalize_sid(sid)
    with SESSIONS_LOCK:
        if sid in SESSIONS:
            SESSIONS_LAST[sid] = time.time()
            return sid, SESSIONS[sid]
        if len(SESSIONS) >= MAX_SESSIONS:
            oldest = min(SESSIONS_LAST, key=SESSIONS_LAST.get)
            SESSIONS.pop(oldest, None)
            SESSIONS_LAST.pop(oldest, None)
        restored = db_load_session(sid)  # transparent after restart/eviction
        if restored is not None:
            SESSIONS[sid], CONTROL_USED[sid] = restored
        else:
            SESSIONS[sid] = fresh_state()
        SESSIONS_LAST[sid] = time.time()
        miss = restored is None
    if miss:
        # Outside the lock (slow I/O): full disk wipe? restore from the cloud.
        cloud = cloud_load_session(sid)
        if cloud is not None:
            state, used, logrows = cloud
            state["commands_log"] = logrows  # full audit history (superset of post-reset log)
            with SESSIONS_LOCK:
                SESSIONS[sid] = state  # cloud wins: it holds the authoritative history
                CONTROL_USED[sid] = used
                SESSIONS_LAST[sid] = time.time()
            db_save_session(sid)  # refill the local cache
    with SESSIONS_LOCK:
        SESSIONS_LAST[sid] = time.time()
        return sid, SESSIONS[sid]

def reset_state(sid):
    sid = normalize_sid(sid)
    with SESSIONS_LOCK:
        SESSIONS[sid] = fresh_state()
        SESSIONS_LAST[sid] = time.time()
        return sid, SESSIONS[sid]

# Instructor results: whole-class view at GET /report/all is gated by this key.
# Set REPORT_KEY in the Render dashboard (Environment); never share it with students.
REPORT_KEY = os.environ.get("REPORT_KEY", "")

# Anti-brute-force budgets (per student, survives session reset).
CONTROL_USED = {}
CONTROL_LOCK = threading.Lock()
MAX_CONTROLS = 15     # hard cap on mutating commands per student (reset excluded, survives reset)

def control_used(sid):
    with CONTROL_LOCK:
        return CONTROL_USED.get(normalize_sid(sid), 0)

def bump_controls(sid):
    sid = normalize_sid(sid)
    with CONTROL_LOCK:
        CONTROL_USED[sid] = CONTROL_USED.get(sid, 0) + 1
        return CONTROL_USED[sid]

# Persistence (SQLite, stdlib only): every mutation is written through, so results
# survive a process crash/restart as long as the DB file survives with the disk.
# Path from DB_PATH (default ./satlab.db). Every DB error fails open — the game
# keeps running in memory and the failure is only logged.
DB_PATH = os.environ.get("DB_PATH", os.path.join(os.path.dirname(os.path.abspath(__file__)), "satlab.db"))
DB = None
DB_LOCK = threading.Lock()

def db_init():
    """Open DB, create tables, rehydrate in-memory sessions (called once at boot)."""
    global DB
    try:
        DB = sqlite3.connect(DB_PATH, check_same_thread=False)
        DB.execute("PRAGMA journal_mode=WAL")
        DB.execute("PRAGMA synchronous=NORMAL")
        DB.execute("""CREATE TABLE IF NOT EXISTS sessions(
            student TEXT PRIMARY KEY, state_json TEXT NOT NULL,
            controls_used INTEGER NOT NULL DEFAULT 0, updated REAL NOT NULL)""")
        DB.execute("""CREATE TABLE IF NOT EXISTS commands(
            id INTEGER PRIMARY KEY AUTOINCREMENT, student TEXT NOT NULL,
            cmd TEXT NOT NULL, value TEXT, t REAL NOT NULL)""")
        DB.execute("CREATE INDEX IF NOT EXISTS idx_commands_student ON commands(student)")
        DB.commit()
        n = 0
        with SESSIONS_LOCK:
            for sid, state_json, used in DB.execute("SELECT student, state_json, controls_used FROM sessions"):
                try:
                    SESSIONS[sid] = json.loads(state_json)
                    CONTROL_USED[sid] = int(used)
                    SESSIONS_LAST[sid] = time.time()
                    n += 1
                except Exception:
                    continue
        print(f"persistence: {DB_PATH} ({n} sessions restored)", flush=True)
    except Exception as e:  # noqa: BLE001 - fail open, game works in-memory
        print(f"persistence disabled ({e}); running in-memory only", flush=True)
        DB = None

def db_save_session(sid):
    """Upsert one student's full state + budget (mirrors in-memory exactly)."""
    if DB is None:
        return
    try:
        with SESSIONS_LOCK:
            s = SESSIONS.get(sid)
            snap = copy.deepcopy(s) if s is not None else None
            used = CONTROL_USED.get(sid, 0)
        if snap is None:
            return
        with DB_LOCK:
            DB.execute("INSERT INTO sessions(student, state_json, controls_used, updated)"
                       " VALUES(?,?,?,?) ON CONFLICT(student) DO UPDATE SET"
                       " state_json=excluded.state_json, controls_used=excluded.controls_used,"
                       " updated=excluded.updated",
                       (sid, json.dumps(snap), used, time.time()))
            DB.commit()
    except Exception as e:  # noqa: BLE001 - fail open
        print(f"db save failed: {e}", flush=True)

def db_log_command(sid, cmd, val):
    """Append one row to the audit log."""
    if DB is None:
        return
    try:
        with DB_LOCK:
            DB.execute("INSERT INTO commands(student, cmd, value, t) VALUES(?,?,?,?)",
                       (sid, cmd, json.dumps(val), time.time()))
            DB.commit()
    except Exception as e:  # noqa: BLE001 - fail open
        print(f"db log failed: {e}", flush=True)

def db_load_session(sid):
    """Return (state, controls_used) for an evicted/restarted session, else None."""
    if DB is None:
        return None
    try:
        with DB_LOCK:
            row = DB.execute("SELECT state_json, controls_used FROM sessions WHERE student=?",
                             (sid,)).fetchone()
        if not row:
            return None
        return json.loads(row[0]), int(row[1])
    except Exception:  # noqa: BLE001 - fail open
        return None

# Cloud mirror (Upstash Redis via REST, stdlib urllib only): survives even a full
# Render wipe (memory + SQLite file gone). Set UPSTASH_URL + UPSTASH_TOKEN in the
# Render dashboard (Environment). Every call fails open with a short timeout —
# the game never depends on the cloud answering.
UPSTASH_URL = os.environ.get("UPSTASH_URL", "").rstrip("/")
UPSTASH_TOKEN = os.environ.get("UPSTASH_TOKEN", "")
UPSTASH_TIMEOUT = 4
CLOUD_PREFIX = "sat1"

def upstash_pipeline(cmds):
    """Run [[CMD, args...], ...] via REST /pipeline. Returns results list or None."""
    if not UPSTASH_URL or not UPSTASH_TOKEN:
        return None
    try:
        req = urllib.request.Request(
            UPSTASH_URL + "/pipeline",
            data=json.dumps(cmds).encode(),
            headers={"Content-Type": "application/json",
                     "Authorization": f"Bearer {UPSTASH_TOKEN}"},
            method="POST")
        with urllib.request.urlopen(req, timeout=UPSTASH_TIMEOUT) as r:
            return json.load(r)
    except Exception:  # noqa: BLE001 - fail open
        return None

def cloud_persist(sid, cmd, val):
    """One REST call: snapshot current state + append audit row."""
    if not UPSTASH_URL or not UPSTASH_TOKEN:
        return
    try:
        with SESSIONS_LOCK:
            s = SESSIONS.get(sid)
            snap = copy.deepcopy(s) if s is not None else None
            used = CONTROL_USED.get(sid, 0)
        if snap is None:
            return
        upstash_pipeline([
            ["SET", f"{CLOUD_PREFIX}:sess:{sid}",
             json.dumps({"state": snap, "used": used})],
            ["RPUSH", f"{CLOUD_PREFIX}:log:{sid}",
             json.dumps({"cmd": cmd, "value": val, "t": time.time()})],
        ])
    except Exception:  # noqa: BLE001 - fail open
        pass

def cloud_save_session(sid):
    """Snapshot current state (no audit row)."""
    if not UPSTASH_URL or not UPSTASH_TOKEN:
        return
    try:
        with SESSIONS_LOCK:
            s = SESSIONS.get(sid)
            snap = copy.deepcopy(s) if s is not None else None
            used = CONTROL_USED.get(sid, 0)
        if snap is None:
            return
        upstash_pipeline([
            ["SET", f"{CLOUD_PREFIX}:sess:{sid}",
             json.dumps({"state": snap, "used": used})],
        ])
    except Exception:  # noqa: BLE001 - fail open
        pass

def cloud_load_session(sid):
    """Return (state, used, logrows) from the cloud, or None. Log = full audit history."""
    if not UPSTASH_URL or not UPSTASH_TOKEN:
        return None
    try:
        res = upstash_pipeline([
            ["GET", f"{CLOUD_PREFIX}:sess:{sid}"],
            ["LRANGE", f"{CLOUD_PREFIX}:log:{sid}", 0, -1],
        ])
        if not res or not res[0].get("result"):
            return None
        payload = json.loads(res[0]["result"])
        state, used = payload.get("state"), int(payload.get("used", 0))
        if not isinstance(state, dict) or "modcod" not in state:
            return None
        logrows = []
        for raw in res[1].get("result") or []:
            try:
                row = json.loads(raw)
                if isinstance(row, dict) and "cmd" in row:
                    logrows.append(row)
            except Exception:
                continue
        return state, used, logrows
    except Exception:  # noqa: BLE001 - fail open
        return None

def cloud_students():
    """All sids ever stored (for /report/all after a cold start)."""
    if not UPSTASH_URL or not UPSTASH_TOKEN:
        return []
    out = []
    try:
        cursor = "0"
        for _ in range(50):
            res = upstash_pipeline([
                ["SCAN", cursor, "MATCH", f"{CLOUD_PREFIX}:sess:*", "COUNT", "200"],
            ])
            if not res:
                break
            cursor, keys = res[0].get("result", ["0", []])
            for k in keys or []:
                sid = normalize_sid(k.rsplit(":", 1)[-1])
                if sid and sid not in out:
                    out.append(sid)
            if str(cursor) == "0":
                break
    except Exception:  # noqa: BLE001 - fail open
        pass
    return out

# MODCOD table: (capacity_mbps on 36MHz, required Eb/N0 dB)
MODCODS = {
    "QPSK_1/2":     (28.0, 2.5),
    "QPSK_3/4":     (42.0, 5.5),
    "8PSK_5/6":     (68.0, 9.0),
    "16APSK_5/6":   (90.0, 11.5),
    "256APSK_9/10": (135.0, 18.0),
}

BASE_EB_N0 = 8.1   # dB, clear-sky reference for QPSK_3/4 with 2.6 dB margin
BASE_C_N = 12.4    # dB, clear-sky reference
BASE_GT = 21.5
BASE_RTT_MS = 542
RAIN_DB = 7.0        # severe localized storm cell (Ku-band absorption+scattering)
POINT_LOSS_DB = 7.0  # wind drift: antenna misaligned, G/T degraded until `repoint`
LOCK_CN_DB = 6.0     # modem C/N lock threshold — Stage-1 gate

def current_physics(s):
    """Returns (c_n, eb_n0, ber, rtt_ms, capacity)."""
    c_n = BASE_C_N
    eb_n0 = BASE_EB_N0
    rtt = BASE_RTT_MS

    # --- Stage-1 fault: storm rain fade (Ku absorption+scattering) ---
    c_n -= RAIN_DB
    eb_n0 -= RAIN_DB

    # --- Stage-1 fault: wind drift, antenna misaligned, G/T degraded ---
    if not s.get("antenna_ok", True):
        c_n -= POINT_LOSS_DB
        eb_n0 -= POINT_LOSS_DB

    # --- frequency trap: gaseous absorption peaks (Ch 1.2) ---
    if abs(s["freq_ghz"] - 22) < 1.0:
        c_n -= 18.0
        eb_n0 -= 18.0
    elif abs(s["freq_ghz"] - 60) < 5.0:
        c_n -= 25.0
        eb_n0 -= 25.0
    elif s["freq_ghz"] not in (12.2, 14.0):
        c_n -= 3.0
        eb_n0 -= 3.0

    # --- HPA: linear boost to fight rain, saturation if overdriven ---
    if s["hpa_pct"] > 96:
        c_n -= 2.5
        eb_n0 -= 2.5
    elif s["hpa_pct"] < 30:
        drop = (30 - s["hpa_pct"]) * 0.15
        c_n -= drop
        eb_n0 -= drop
    else:
        # EIRP boost within linear region: +0.22 dB per point above 68
        c_n += (s["hpa_pct"] - 68) * 0.22
        eb_n0 += (s["hpa_pct"] - 68) * 0.22

    # --- MODCOD: capacity up, but required Eb/N0 up (Stage-2 gate) ---
    cap, req = MODCODS.get(s["modcod"], MODCODS["QPSK_3/4"])
    margin = eb_n0 - req
    if margin >= 0:
        ber = 2e-7
    elif margin > -1.5:
        ber = 1e-4  # cliff starting
    elif margin > -3.0:
        ber = 1e-3
    else:
        ber = 1e-2  # link essentially dead

    # --- ARQ trap: extra hold-and-wait latency ---
    if s["link_arq"]:
        rtt += 320

    return round(c_n, 1), round(eb_n0, 1), ber, int(rtt), cap

def rf_locked(c_n):
    """Stage-1 gate: modem carrier lock."""
    return c_n >= LOCK_CN_DB

def frame_loss_pct(ber, c_n):
    """Stage-2 gate: link-layer frame loss derived from BER + lock."""
    if not rf_locked(c_n):
        return 100.0
    if ber >= 1e-2:
        return 100.0
    if ber >= 1e-3:
        return 35.0
    if ber >= 1e-4:
        return 8.0
    return 0.0

def current_gt(s):
    return BASE_GT if s.get("antenna_ok", True) else round(BASE_GT - 7.5, 1)

def current_throughput(s):
    if time.time() < s["down_until"]:
        return 0.0
    c_n, eb_n0, ber, rtt_ms, cap = current_physics(s)
    # Total outage while RF unlocked or PHY dead
    if not rf_locked(c_n):
        return 0.0
    rtt = rtt_ms / 1000.0
    win_bytes = s["tcp_win_kb"] * 1024.0
    FLOWS = 4

    # Dead PHY -> near zero regardless of TCP
    if ber >= 1e-2:
        return 0.0
    if ber >= 1e-3:
        return round(min(cap * 0.12, 5.0), 2)

    ber_penalty = 1.0 if ber <= 1e-6 else 0.85

    if s["pep"] == 0:
        # Classic GEO TCP: slow-start + Reno halves cwnd on wireless loss.
        # Large BDP can never be filled; ceiling ~5.5 Mbps.
        raw = FLOWS * win_bytes * 8.0 / rtt / 1e6
        thr = min(raw, 5.5 * ber_penalty, cap * ber_penalty)
    else:
        if s["sack"] == 0:
            # PEP splitting without SACK: partial recovery
            raw = FLOWS * win_bytes * 8.0 / 0.18 / 1e6  # spoofed short RTT
            thr = min(raw, 12.0 * ber_penalty, cap * ber_penalty)
        else:
            if s["tcp_win_kb"] >= 2500:
                thr = cap * ber_penalty  # SOLVED: window >= BDP, errors hidden
            else:
                raw = FLOWS * win_bytes * 8.0 / 0.06 / 1e6
                thr = min(raw, 14.0, cap * ber_penalty)
    return round(max(thr, 0.15), 2)

def link_stage(s):
    """Game progress: 1=RF outage, 2=frames bad, 3=transport capped, 4=solved."""
    c_n, _, ber, _, _ = current_physics(s)
    if not rf_locked(c_n):
        return 1
    if frame_loss_pct(ber, c_n) > 0:
        return 2
    thr = current_throughput(s)
    if thr >= 35.0:
        return 4
    return 3

def link_state_label(thr):
    return "up" if thr >= 35.0 else "down"

SUCCESS_MSG = "Success, you restored the link, well done engineer! GW2<>GEO1<>VSAT7 is back at >=35 Mbps."

# In-game guidance only (kept OUT of the PDF handout on purpose).
# /status always carries the hint for the player's current stage.
STAGE_HINTS = {
    1: ("Stage 1 — signal is not locked (C/N below threshold). "
        "Check 'rf' (C/N vs lock, G/T, HPA) and 'ttc' (is the satellite itself OK?). "
        "Fix the physical channel first: antenna + power."),
    2: ("Stage 1 cleared — carrier is locked! Now frames are still dropping "
        "(BER / frame_loss_pct bad). The MODCOD is too fragile for this rain. "
        "Check 'rf' (Eb/N0 vs required) and fall back to something more robust."),
    3: ("Stage 2 cleared — frames are clean! Now throughput is still capped. "
        "This is transport, not RF: check 'tcp' (RTT, window vs BDP, PEP, SACK). "
        "Scale the pipe to fill the long GEO delay."),
    4: SUCCESS_MSG,
}

# Short acknowledgment sent back by POST /control exactly when a stage is cleared.
STAGE_CLEAR_MSGS = {
    1: ("Stage 1 cleared — RF locked! Well done. "
        "Next: fix the falling frames (check Eb/N0 vs MODCOD requirement)."),
    2: ("Stage 2 cleared — frames are clean (0% loss)! Well done. "
        "Next: fix the capped throughput (check window vs BDP, PEP, SACK)."),
    3: SUCCESS_MSG,
}

def full_status(sid, s):
    """Everything at once: symptom + RF + transport + TT&C."""
    c_n, eb_n0, ber, rtt, cap = current_physics(s)
    _, req = MODCODS.get(s["modcod"], MODCODS["QPSK_3/4"])
    thr = current_throughput(s)
    bdp_kb = round(rtt / 1000.0 * 42.0 * 1e6 / 8.0 / 1024.0)
    if thr <= 0:
        bw_used = 0.0
    else:
        bw_used = min(36.0, thr / MODCODS[s["modcod"]][0] * 36.0 * 0.95 + 1.2)
    stage = link_stage(s)
    resp = {
        "link": "GW2<>GEO1<>VSAT7",
        "student": sid,
        "thr_mbps": thr,
        "status": link_state_label(thr),
        "stage": stage,
        "hint": STAGE_HINTS[stage],
        "rf": {
            "c_n_db": c_n,
            "lock_threshold_db": LOCK_CN_DB,
            "locked": rf_locked(c_n),
            "g_t": current_gt(s),
            "g_t_nominal": BASE_GT,
            "hpa_pct": s["hpa_pct"],
            "freq_ghz": s["freq_ghz"],
            "modcod": s["modcod"],
            "eb_n0_db": eb_n0,
            "eb_n0_required_db": req,
            "ber": ber,
            "frame_loss_pct": frame_loss_pct(ber, c_n),
            "bw_used_mhz": round(bw_used, 1),
            "bw_alloc_mhz": 36,
        },
        "tcp": {
            "rtt_ms": rtt if time.time() >= s["down_until"] else 0,
            "tcp_win_kb": s["tcp_win_kb"],
            "bdp_kb": bdp_kb,
            "sack": s["sack"],
            "pep": s["pep"],
            "link_arq": s["link_arq"],
        },
        "ttc": {
            "orbit_km": s["orbit_km"],
            "station_keeping": "NOMINAL" if s["orbit_km"] == 35786 else "DRIFT",
            "payload": s["payload"],
            "payload_health": "NOMINAL",
        },
        "controls_used": control_used(sid),
        "tools": ["/tools/rf", "/tools/tcp", "/tools/ttc"],
    }
    if resp["status"] == "up":
        resp["message"] = SUCCESS_MSG
    return resp

def student_report(sid, s):
    """Progress summary for results: stage, score inputs, final config, full command log."""
    thr = current_throughput(s)
    return {
        "student": sid,
        "stage": link_stage(s),
        "status": link_state_label(thr),
        "thr_mbps": thr,
        "solved": thr >= 35.0,
        "controls_used": control_used(sid),
        "config": {
            "hpa_pct": s["hpa_pct"],
            "modcod": s["modcod"],
            "freq_ghz": s["freq_ghz"],
            "tcp_win_kb": s["tcp_win_kb"],
            "sack": s["sack"],
            "pep": s["pep"],
            "antenna_ok": s.get("antenna_ok"),
        },
        "commands_log": s["commands_log"],
    }

class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, obj, code=200):
        b = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, X-Student-ID")
        self.end_headers()
        self.wfile.write(b)

    def do_OPTIONS(self):
        self._send({}, 200)

    def _sid_from_get(self):
        try:
            q = parse_qs(urlparse(self.path).query)
            for key in ("student", "id", "sid"):
                if q.get(key):
                    return q[key][0]
        except Exception:
            pass
        return self.headers.get("X-Student-ID", "shared")

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/":
            # Health check for Render / load balancers
            return self._send({"ok": True, "service": "sat-1-simulator"})
        if path == "/status":
            # Full dump: symptom + RF + transport + TT&C in one call.
            sid, s = get_state(self._sid_from_get())
            resp = full_status(sid, s)
            if resp["status"] == "up":
                resp["message"] = SUCCESS_MSG
            return self._send(resp)
        if path == "/tools/rf":
            # Virtual spectrum analyzer + demod stats (RF / PHY domain).
            sid, s = get_state(self._sid_from_get())
            c_n, eb_n0, ber, rtt, cap = current_physics(s)
            thr = current_throughput(s)
            if thr <= 0:
                bw_used = 0.0
            else:
                bw_used = min(36.0, thr / MODCODS[s["modcod"]][0] * 36.0 * 0.95 + 1.2)
            return self._send({
                "tool": "rf",
                "student": sid,
                "payload": s["payload"],
                "orbit_km": s["orbit_km"],
                "freq_ghz": s["freq_ghz"],
                "c_n_db": c_n,
                "eb_n0_db": eb_n0,
                "ber": ber,
                "locked": rf_locked(c_n),
                "lock_threshold_cn_db": LOCK_CN_DB,
                "frame_loss_pct": frame_loss_pct(ber, c_n),
                "g_t": current_gt(s),
                "hpa_pct": s["hpa_pct"],
                "modcod": s["modcod"],
                "bw_alloc_mhz": 36,
                "bw_used_mhz": round(bw_used, 1),
            })
        if path == "/tools/tcp":
            # Virtual TCP trace (transport domain).
            sid, s = get_state(self._sid_from_get())
            _, _, _, rtt, _ = current_physics(s)
            return self._send({
                "tool": "tcp",
                "student": sid,
                "rtt_ms": rtt if time.time() >= s["down_until"] else 0,
                "tcp_win_kb": s["tcp_win_kb"],
                "sack": s["sack"],
                "pep": s["pep"],
                "link_arq": s["link_arq"],
            })
        if path == "/tools/ttc":
            # TT&C telemetry (space + control segment): is the satellite itself healthy?
            sid, s = get_state(self._sid_from_get())
            return self._send({
                "tool": "ttc",
                "student": sid,
                "satellite": "GEO1",
                "orbit_km": s["orbit_km"],
                "orbit_nominal_km": 35786,
                "station_keeping": "NOMINAL" if s["orbit_km"] == 35786 else "DRIFT",
                "payload": s["payload"],
                "payload_health": "NOMINAL",
                "note": "bent-pipe: no onboard demod/remod",
            })
        if path == "/report":
            # Own progress incl. command log (any student can check their own run).
            sid, s = get_state(self._sid_from_get())
            return self._send(student_report(sid, s))
        if path == "/report/all":
            # Whole-class results for the instructor. Gated by REPORT_KEY env var
            # (set it in the Render dashboard; never share it with students).
            if not REPORT_KEY:
                return self._send({"error": "instructor: set REPORT_KEY env var on the server first"}, 403)
            try:
                q = parse_qs(urlparse(self.path).query)
                want = (q.get("key") or [""])[0]
            except Exception:
                want = ""
            if want != REPORT_KEY:
                return self._send({"error": "wrong key"}, 403)
            with SESSIONS_LOCK:
                sids = sorted(SESSIONS.keys())
            for rid in cloud_students():  # sessions known only to the cloud (cold start)
                if rid not in sids:
                    sids.append(rid)
            sids.sort()
            out = []
            for rid in sids:
                _, rs = get_state(rid)  # lazily restores from cloud on miss
                out.append(student_report(rid, rs))
            return self._send({"count": len(out), "reports": out})
        return self._send({"error": "unknown endpoint. use /status, /tools/rf, /tools/tcp, /tools/ttc or /control"}, 404)

    def do_POST(self):
        rpath = urlparse(self.path).path
        try:
            n = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(n).decode() or "{}")
        except Exception:
            return self._send({"error": "bad JSON"}, 400)
        if rpath == "/wipe":
            # Instructor: clear everything (memory + SQLite + cloud) between groups.
            # Same key as /report/all. POST only.
            try:
                q = parse_qs(urlparse(self.path).query)
                want = (body.get("key", "") if isinstance(body, dict) else "")
                if not want:
                    want = (q.get("key") or [""])[0]
            except Exception:
                want = ""
            if not REPORT_KEY or want != REPORT_KEY:
                return self._send({"error": "wrong key"}, 403)
            with SESSIONS_LOCK:
                SESSIONS.clear()
                SESSIONS_LAST.clear()
            with CONTROL_LOCK:
                CONTROL_USED.clear()
            if DB is not None:
                try:
                    with DB_LOCK:
                        DB.execute("DELETE FROM sessions")
                        DB.execute("DELETE FROM commands")
                        DB.commit()
                except Exception:
                    pass
            try:
                cursor = "0"
                for _ in range(50):
                    res = upstash_pipeline([
                        ["SCAN", cursor, "MATCH", f"{CLOUD_PREFIX}:*", "COUNT", "500"],
                    ])
                    if not res:
                        break
                    cursor, keys = res[0].get("result", ["0", []])
                    keys = keys or []
                    for i in range(0, len(keys), 100):
                        upstash_pipeline([["DEL"] + keys[i:i + 100]])
                    if str(cursor) == "0":
                        break
            except Exception:
                pass
            return self._send({"wiped": True})
        if rpath != "/control":
            return self._send({"error": "unknown endpoint. use /status or /control"}, 404)
        raw_sid = body.get("student", body.get("id", body.get("sid", None)))
        if raw_sid is None:
            try:
                q = parse_qs(urlparse(self.path).query)
                for key in ("student", "id", "sid"):
                    if q.get(key):
                        raw_sid = q[key][0]
                        break
            except Exception:
                pass
        if raw_sid is None:
            raw_sid = self.headers.get("X-Student-ID", "shared")
        cmd = str(body.get("cmd", "")).strip().lower()
        val = body.get("value", None)
        if cmd == "reset":
            sid, s = reset_state(raw_sid)
            db_save_session(sid)
            db_log_command(sid, "reset", None)
            cloud_persist(sid, "reset", None)
            thr = current_throughput(s)
            stage = link_stage(s)
            resp = {"ack": f"session {sid} reset to baseline", "thr_mbps": thr,
                    "status": link_state_label(thr), "stage": stage, "student": sid,
                    "hint": STAGE_HINTS[stage]}
            if resp["status"] == "up":
                resp["message"] = SUCCESS_MSG
            return self._send(resp)
        sid, s = get_state(raw_sid)
        stage_before = link_stage(s)
        used = bump_controls(sid)
        if used > MAX_CONTROLS:
            return self._send({"error": f"control budget exhausted ({MAX_CONTROLS} commands per student). Reset does not refill it.", "student": sid}, 429)
        s["commands_log"].append({"cmd": cmd, "value": val, "t": time.time()})
        db_save_session(sid)
        db_log_command(sid, cmd, val)
        cloud_persist(sid, cmd, val)
        msg = ""

        if cmd == "set_hpa":
            try:
                v = float(val)
                if not 5 <= v <= 100:
                    return self._send({"error": "hpa_pct out of range 5-100"}, 400)
                s["hpa_pct"] = round(v)
                msg = f"HPA set {s['hpa_pct']}%"
            except (TypeError, ValueError):
                return self._send({"error": "value must be number 5-100"}, 400)
        elif cmd == "set_modcod":
            if str(val).upper() not in MODCODS:
                return self._send({"error": f"unknown modcod. options: {list(MODCODS)}"}, 400)
            s["modcod"] = str(val).upper()
            msg = f"MODCOD {s['modcod']}"
        elif cmd == "set_freq_ghz":
            try:
                s["freq_ghz"] = float(val)
                msg = f"freq {s['freq_ghz']} GHz"
            except (TypeError, ValueError):
                return self._send({"error": "value must be number"}, 400)
        elif cmd == "set_tcp_window_kb":
            try:
                v = int(val)
                if not 4 <= v <= 8192:
                    return self._send({"error": "window out of range 4-8192 KB"}, 400)
                s["tcp_win_kb"] = v
                msg = f"tcp window {v} KB"
            except (TypeError, ValueError):
                return self._send({"error": "value must be int KB"}, 400)
        elif cmd == "set_sack":
            s["sack"] = 1 if val in (1, True, "1", "on", "enable") else 0
            msg = f"SACK {'on' if s['sack'] else 'off'}"
        elif cmd == "set_pep":
            s["pep"] = 1 if val in (1, True, "1", "on", "enable") else 0
            msg = f"PEP {'on' if s['pep'] else 'off'}"
        elif cmd == "set_link_arq":
            s["link_arq"] = 1 if val in (1, True, "1", "on", "enable") else 0
            msg = f"link ARQ {'on' if s['link_arq'] else 'off'}"
        elif cmd == "repoint":
            s["antenna_ok"] = True
            msg = "antenna repoint complete. LNA inspected, G/T nominal."
        elif cmd == "reboot":
            s["down_until"] = time.time() + 8
            s["reboots"] += 1
            msg = "payload reboot initiated. 8s outage."
        elif cmd == "set_payload_mode":
            if str(val).upper() == "REGEN":
                return self._send({"error": "REGEN rejected: GEO1 is bent-pipe hardware. No onboard demod/remod."}, 400)
            s["payload"] = "BENT"
            msg = "payload BENT"
        else:
            opts = ["set_hpa","set_modcod","set_freq_ghz","set_tcp_window_kb",
                    "set_sack","set_pep","set_link_arq","repoint","reboot","set_payload_mode","reset"]
            return self._send({"error": f"unknown cmd. options: {opts}"}, 400)

        thr = current_throughput(s)
        stage_after = link_stage(s)
        db_save_session(sid)  # second save: snapshot now includes this command's effect
        cloud_save_session(sid)
        resp = {"ack": msg, "thr_mbps": thr, "status": link_state_label(thr),
                "stage": stage_after, "student": sid, "controls_used": control_used(sid),
                "hint": STAGE_HINTS[stage_after]}
        if stage_after > stage_before:
            # Acknowledge exactly what was cleared + what to do next.
            # A single command can clear two stages at once (e.g. MODCOD
            # fallback both locks RF and cleans frames) — acknowledge both.
            parts = [STAGE_CLEAR_MSGS[i] for i in range(stage_before, stage_after) if i in STAGE_CLEAR_MSGS]
            if parts:
                resp["message"] = " ".join(parts)
        if resp["status"] == "up":
            resp["message"] = SUCCESS_MSG
        self._send(resp)

if __name__ == "__main__":
    # Render injects $PORT (e.g. 10000). Use it by default, allow --port override.
    default_port = int(os.environ.get("PORT", "8765"))
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=default_port)
    a = ap.parse_args()
    db_init()
    print(f"cloud mirror: {'on' if UPSTASH_URL and UPSTASH_TOKEN else 'off (set UPSTASH_URL + UPSTASH_TOKEN)'}",
          flush=True)
    print(f"SAT-1 simulator on :{a.port}  (GET /status, POST /control)", flush=True)
    ThreadingHTTPServer(("0.0.0.0", a.port), H).serve_forever()
