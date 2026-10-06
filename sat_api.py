#!/usr/bin/env python3
"""
SAT-1 Link Simulator — Chapter 1 Lab
GEO bent-pipe, Ku-band. STORM OUTAGE scenario: 3-stage restore.
  Stage 1 (PHY/power): rain fade + antenna drift -> C/N below lock. Fix: repoint + HPA boost.
  Stage 2 (PHY/MODCOD): 256APSK bandwidth-limited -> BER collapse. Fix: fallback QPSK_3/4 (QPSK_1/2 safe but slow).
  Stage 3 (transport): BDP starvation, 64KB window vs ~500ms RTT. Fix: PEP + SACK + window>=2500.
Run: python3 sat_api.py [--port 8765]
  GET  /status?student=ID      -> symptom only (thr_mbps, result)
  GET  /tools/rf?student=ID    -> RF/PHY diagnostics (demod + spectrum)
  GET  /tools/tcp?student=ID   -> transport diagnostics (RTT, window, SACK/PEP/ARQ)
  GET  /tools/ttc?student=ID   -> TT&C / space segment (orbit, payload health)
  POST /control                -> {"cmd": "...", "value": ..., "student": "ID", "rationale": "why (min 15 chars)"}
Per-student isolation: each student ID gets its own simulator state on the
same shared Render service, so one student solving does not solve it for others.
Omit student -> shared fallback session ("shared").
Control budget: 30 mutating commands per student (reset excluded, survives reset).
No full-state dump exists: diagnosis must be earned tool by tool.
"""
import argparse
import copy
import json
import math
import os
import re
import threading
import time
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
        if sid not in SESSIONS:
            if len(SESSIONS) >= MAX_SESSIONS:
                oldest = min(SESSIONS_LAST, key=SESSIONS_LAST.get)
                SESSIONS.pop(oldest, None)
                SESSIONS_LAST.pop(oldest, None)
            SESSIONS[sid] = fresh_state()
        SESSIONS_LAST[sid] = time.time()
        return sid, SESSIONS[sid]

def reset_state(sid):
    sid = normalize_sid(sid)
    with SESSIONS_LOCK:
        SESSIONS[sid] = fresh_state()
        SESSIONS_LAST[sid] = time.time()
        return sid, SESSIONS[sid]

# Anti-brute-force budgets (per student, survives session reset).
CONTROL_USED = {}
CONTROL_LOCK = threading.Lock()
MAX_CONTROLS = 30     # hard cap on mutating commands per student (grading needs <=8 for full marks)
MIN_RATIONALE = 15    # min chars of hypothesis required on every mutating command

def control_used(sid):
    with CONTROL_LOCK:
        return CONTROL_USED.get(normalize_sid(sid), 0)

def bump_controls(sid):
    sid = normalize_sid(sid)
    with CONTROL_LOCK:
        CONTROL_USED[sid] = CONTROL_USED.get(sid, 0) + 1
        return CONTROL_USED[sid]

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
    if not rf_locked(c_n) or ber >= 1e-2:
        return 1
    if frame_loss_pct(ber, c_n) > 0:
        return 2
    thr = current_throughput(s)
    if thr >= 35.0:
        return 4
    return 3

def link_state_label(thr):
    return "NOMINAL" if thr >= 35.0 else "DEGRADED"

SUCCESS_MSG = (
    "SUCCESS: Link restored — GW2<>GEO1<>VSAT7 NOMINAL (>=35 Mbps). "
    "Well done, night shift — storm outage cleared, contract met!"
)

STAGE_HINTS = {
    1: "STAGE 1/3 — RF OUTAGE: no carrier lock (thr 0). Check /tools/rf (C/N vs lock, G/T, Eb/N0) + /tools/ttc (orbit/payload). Suspects: antenna drift, rain fade, HPA.",
    2: "STAGE 2/3 — RF LOCKED but frames dropping. Check /tools/rf (MODCOD vs Eb/N0, BER, frame loss). Suspects: bandwidth-limited MODCOD under rain — fall back to robust QPSK.",
    3: "STAGE 3/3 — Frames clean but TCP stalled. Check /tools/tcp (RTT vs window = BDP, PEP/SACK). Suspects: GEO delay + small window + Reno backoff.",
    4: SUCCESS_MSG,
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
            # Symptom + game stage gate. No diagnostic numbers here by design —
            # use /tools/rf, /tools/tcp, /tools/ttc to earn the diagnosis in stages.
            sid, s = get_state(self._sid_from_get())
            thr = current_throughput(s)
            result = link_state_label(thr)
            stage = link_stage(s)
            resp = {
                "link": "GW2<>GEO1<>VSAT7",
                "student": sid,
                "thr_mbps": thr,
                "result": result,
                "stage": stage,
                "stage_hint": STAGE_HINTS[stage],
                "controls_used": control_used(sid),
                "tools": ["/tools/rf", "/tools/tcp", "/tools/ttc"],
            }
            if result == "NOMINAL":
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
        return self._send({"error": "unknown endpoint. use /status, /tools/rf, /tools/tcp, /tools/ttc or /control"}, 404)

    def do_POST(self):
        if urlparse(self.path).path != "/control":
            return self._send({"error": "unknown endpoint. use /status or /control"}, 404)
        try:
            n = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(n).decode() or "{}")
        except Exception:
            return self._send({"error": "bad JSON"}, 400)
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
        rationale = body.get("rationale", body.get("hypothesis", ""))
        if cmd == "reset":
            sid, s = reset_state(raw_sid)
            thr = current_throughput(s)
            result = link_state_label(thr)
            resp = {"ack": f"session {sid} reset to baseline", "thr_mbps": thr, "result": result, "student": sid}
            if result == "NOMINAL":
                resp["message"] = SUCCESS_MSG
            return self._send(resp)
        # Hypothesis-first: every mutating command must carry the student's reasoning.
        # This is graded (commands_log keeps it) and blocks blind copy-paste scripts.
        if not isinstance(rationale, str) or len(rationale.strip()) < MIN_RATIONALE:
            sid = normalize_sid(raw_sid)
            return self._send({"error": f"rationale required: include 'rationale' with your hypothesis (min {MIN_RATIONALE} chars) explaining why this command helps", "student": sid}, 400)
        sid, s = get_state(raw_sid)
        used = bump_controls(sid)
        if used > MAX_CONTROLS:
            return self._send({"error": f"control budget exhausted ({MAX_CONTROLS} commands per student). Reset does not refill it — review your hypothesis log.", "student": sid}, 429)
        s["commands_log"].append({"cmd": cmd, "value": val, "rationale": rationale.strip(), "t": time.time()})
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
        result = link_state_label(thr)
        stage = link_stage(s)
        resp = {"ack": msg, "thr_mbps": thr, "result": result,
                "stage": stage, "stage_hint": STAGE_HINTS[stage],
                "student": sid, "controls_used": control_used(sid)}
        if result == "NOMINAL":
            resp["message"] = SUCCESS_MSG
        self._send(resp)

if __name__ == "__main__":
    # Render injects $PORT (e.g. 10000). Use it by default, allow --port override.
    default_port = int(os.environ.get("PORT", "8765"))
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=default_port)
    a = ap.parse_args()
    print(f"SAT-1 simulator on :{a.port}  (GET /status, POST /control)", flush=True)
    ThreadingHTTPServer(("0.0.0.0", a.port), H).serve_forever()
