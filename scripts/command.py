#!/usr/bin/env python3
"""Send a control command to the SAT-1 simulator.

Usage:
    python command.py --cmd repoint
    python command.py --cmd set_hpa --value 92
    python command.py --cmd set_modcod --value QPSK_3/4
    python command.py --cmd reset
(The script asks for your student ID and rationale interactively.)

Every mutating command needs --rationale (min 15 chars); it is logged and graded.
Stdlib only.
"""
import argparse
import json
import os
import sys
import urllib.request


def parse_value(raw):
    """Convert CLI string to int/float where possible, else keep as string."""
    if raw is None:
        return None
    try:
        return int(raw)
    except ValueError:
        pass
    try:
        return float(raw)
    except ValueError:
        pass
    low = raw.lower()
    if low in ("true", "on", "enable"):
        return 1
    if low in ("false", "off", "disable"):
        return 0
    return raw


def ask_student_id():
    preset = os.environ.get("STUDENT_ID", "").strip()
    try:
        if preset:
            ans = input(f"Student ID [{preset}]: ").strip()
            return ans or preset
        ans = input("Student ID: ").strip()
    except EOFError:
        if preset:
            return preset
        ans = ""
    if not ans:
        print("No student ID given, using the 'shared' session.", file=sys.stderr)
        return "shared"
    return ans


def main():
    ap = argparse.ArgumentParser(description="Send a control command to the GW2 link simulator.")
    ap.add_argument("--url", default=os.environ.get("SAT_URL", "https://sposc-lab1.onrender.com"),
                    help="simulator base URL")
    ap.add_argument("--cmd", required=True,
                    choices=["set_hpa", "set_modcod", "set_freq_ghz", "set_tcp_window_kb",
                             "set_sack", "set_pep", "set_link_arq", "repoint",
                             "reboot", "set_payload_mode", "reset"],
                    help="command to send")
    ap.add_argument("--value", default=None,
                    help="command value, e.g. 92, QPSK_3/4, 12.2, 4096, 1")
    ap.add_argument("--rationale", default=None,
                    help="your hypothesis, min 15 chars (asked interactively if omitted)")
    a = ap.parse_args()
    a.student = ask_student_id()

    if a.cmd != "reset" and not a.rationale:
        try:
            a.rationale = input("Rationale (your hypothesis, min 15 chars): ").strip()
        except EOFError:
            a.rationale = None
    if a.cmd != "reset" and (not a.rationale or len(a.rationale.strip()) < 15):
        ap.error("--rationale with min 15 chars is required (it is logged and graded)")

    body = {"student": a.student, "cmd": a.cmd}
    val = parse_value(a.value)
    if val is not None:
        body["value"] = val
    if a.rationale:
        body["rationale"] = a.rationale

    req = urllib.request.Request(
        a.url.rstrip("/") + "/control",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            resp = json.load(r)
        print(json.dumps(resp, indent=2))
        if "error" in resp:
            return 1
        return 0
    except Exception as e:  # noqa: BLE001 - show server error bodies too
        try:
            err = e.read().decode()  # HTTPError carries the JSON body
            print(err)
        except Exception:
            print(f"[error] {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
