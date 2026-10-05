#!/usr/bin/env python3
"""
SAT-1 Link Simulator — Chapter 1 Lab
GEO bent-pipe, Ku-band. Hidden fault is TRANSPORT (BDP), not RF.
Run: python3 sat_api.py [--port 8765]
  GET  /status  -> minimal telemetry (intentionally terse)
  POST /control -> {"cmd": "...", "value": ...}
No hints are returned. Students must diagnose from numbers.
"""
import argparse
import json
import math
import os
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

STATE = {
    "payload": "BENT",
    "orbit_km": 35786,
    "freq_ghz": 12.2,
    "hpa_pct": 68,
    "modcod": "QPSK_3/4",
    "tcp_win_kb": 64,
    "sack": 0,
    "pep": 0,
    "link_arq": 0,
    "reboots": 0,
    "commands_log": [],
    "down_until": 0.0,
}

# MODCOD table: (capacity_mbps on 36MHz, required Eb/N0 dB)
MODCODS = {
    "QPSK_3/4":    (42.0, 5.5),
    "8PSK_5/6":    (68.0, 9.0),
    "16APSK_5/6":  (90.0, 11.5),
    "256APSK_9/10": (135.0, 18.0),
}

BASE_EB_N0 = 8.1   # dB, healthy for QPSK_3/4 with 2.6 dB margin
BASE_C_N = 12.4    # dB
BASE_GT = 21.5
BASE_RTT_MS = 542

def current_physics():
    """Returns (c_n, eb_n0, ber, rtt_ms, capacity)."""
    s = STATE
    c_n = BASE_C_N
    eb_n0 = BASE_EB_N0
    rtt = BASE_RTT_MS

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

    # --- HPA trap: overdrive -> saturation, distortion ---
    if s["hpa_pct"] > 90:
        c_n -= 2.5
        eb_n0 -= 2.5
    elif s["hpa_pct"] < 30:
        drop = (30 - s["hpa_pct"]) * 0.15
        c_n -= drop
        eb_n0 -= drop
    else:
        # small benefit for raising power within linear region
        c_n += (s["hpa_pct"] - 68) * 0.02
        eb_n0 += (s["hpa_pct"] - 68) * 0.02

    # --- MODCOD: capacity up, but required Eb/N0 up ---
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

def current_throughput():
    s = STATE
    if time.time() < s["down_until"]:
        return 0.0
    c_n, eb_n0, ber, rtt_ms, cap = current_physics()
    rtt = rtt_ms / 1000.0
    win_bytes = s["tcp_win_kb"] * 1024.0
    FLOWS = 4

    # Dead PHY -> near zero regardless of TCP
    if ber >= 1e-2:
        return round(min(cap * 0.02, 0.4), 2)
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

def link_state_label(thr):
    return "NOMINAL" if thr >= 35.0 else "DEGRADED"

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
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()
        self.wfile.write(b)

    def do_OPTIONS(self):
        self._send({}, 200)

    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/":
            # Health check for Render / load balancers
            return self._send({"ok": True, "service": "sat-1-simulator"})
        if path != "/status":
            return self._send({"error": "unknown endpoint. use /status or /control"}, 404)
        c_n, eb_n0, ber, rtt, cap = current_physics()
        thr = current_throughput()
        bw_used = min(36.0, thr / MODCODS[STATE["modcod"]][0] * 36.0 * 0.95 + 1.2)
        # Intentionally minimal. No diagnosis, no units lecture, no hints.
        self._send({
            "link": "GW2<>GEO1<>VSAT7",
            "payload": STATE["payload"],
            "orbit_km": STATE["orbit_km"],
            "freq_ghz": STATE["freq_ghz"],
            "rtt_ms": rtt if time.time() >= STATE["down_until"] else 0,
            "c_n_db": c_n,
            "eb_n0_db": eb_n0,
            "ber": ber,
            "g_t": BASE_GT,
            "hpa_pct": STATE["hpa_pct"],
            "bw_alloc_mhz": 36,
            "bw_used_mhz": round(bw_used, 1),
            "modcod": STATE["modcod"],
            "thr_mbps": thr,
            "tcp_win_kb": STATE["tcp_win_kb"],
            "sack": STATE["sack"],
            "pep": STATE["pep"],
            "link_arq": STATE["link_arq"],
            "result": link_state_label(thr),
        })

    def do_POST(self):
        if self.path.split("?")[0] != "/control":
            return self._send({"error": "unknown endpoint. use /status or /control"}, 404)
        try:
            n = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(n).decode() or "{}")
        except Exception:
            return self._send({"error": "bad JSON"}, 400)
        cmd = str(body.get("cmd", "")).strip().lower()
        val = body.get("value", None)
        s = STATE
        s["commands_log"].append({"cmd": cmd, "value": val, "t": time.time()})
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
            msg = "antenna repoint complete. G/T nominal."
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
                    "set_sack","set_pep","set_link_arq","repoint","reboot","set_payload_mode"]
            return self._send({"error": f"unknown cmd. options: {opts}"}, 400)

        thr = current_throughput()
        self._send({"ack": msg, "thr_mbps": thr, "result": link_state_label(thr)})

if __name__ == "__main__":
    # Render injects $PORT (e.g. 10000). Use it by default, allow --port override.
    default_port = int(os.environ.get("PORT", "8765"))
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=default_port)
    a = ap.parse_args()
    print(f"SAT-1 simulator on :{a.port}  (GET /status, POST /control)", flush=True)
    HTTPServer(("0.0.0.0", a.port), H).serve_forever()
