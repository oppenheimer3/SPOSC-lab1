#!/usr/bin/env python3
"""Send a control command to the SAT-1 simulator.

Usage:
    python send_command.py --cmd repoint
    python send_command.py --cmd set_hpa --value 80
    python send_command.py --cmd set_tcp_window_kb --value 512
    python send_command.py --cmd reset
(The script asks for your name interactively if omitted.)

Stdlib only.
"""
import argparse
import json
import os
import sys
import urllib.request

CMD_HELP = """\
Available --cmd values (run with --value where shown):
  repoint                          repoint ground antenna + inspect LNA (no value)
  set_hpa --value 50..100          HPA transmit power %% (EIRP)
  set_modcod --value MODCOD        modulation+coding: QPSK_1/2, QPSK_3/4, 8PSK_5/6, 16APSK_5/6, 256APSK_9/10
  set_freq_ghz --value GHZ         carrier frequency, e.g. 12.2, 14.0, 22, 60
  set_tcp_window_kb --value KB     TCP advertised window, 4..8192 (e.g. 512)
  set_sack --value 1|0             Selective Acknowledgments on/off
  set_pep --value 1|0              Performance Enhancing Proxy on/off
  set_link_arq --value 1|0         link-layer ARQ on/off
  set_payload_mode --value MODE    payload mode: BENT (only working mode)
  reboot                           reboot payload, 8s outage (no value)
  reset                            reset YOUR session to baseline (no value, no note needed)
"""


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
    preset = os.environ.get("NAME", os.environ.get("STUDENT_ID", "")).strip()
    try:
        if preset:
            ans = input(f"Your name [{preset}]: ").strip()
            return ans or preset
        ans = input("Your name: ").strip()
    except EOFError:
        if preset:
            return preset
        ans = ""
    if not ans:
        print("No name given, using the 'shared' session.", file=sys.stderr)
        return "shared"
    return ans


def main():
    ap = argparse.ArgumentParser(
        description="Send a control command to the GW2 link simulator.",
        epilog=CMD_HELP,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--url", default=os.environ.get("SAT_URL", "https://sposc-lab1.onrender.com"),
                    help="simulator base URL")
    ap.add_argument("--cmd", required=True,
                    choices=["set_hpa", "set_modcod", "set_freq_ghz", "set_tcp_window_kb",
                             "set_sack", "set_pep", "set_link_arq", "repoint",
                             "reboot", "set_payload_mode", "reset"],
                    help="command to send (see list below)")
    ap.add_argument("--value", default=None,
                    help="command value, e.g. 80, 12.2, 512, 1")
    a = ap.parse_args()
    a.student = ask_student_id()

    body = {"student": a.student, "cmd": a.cmd}
    val = parse_value(a.value)
    if val is not None:
        body["value"] = val

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
        if resp.get("message"):
            print(f"\n*** {resp['message']} ***")
        if resp.get("hint"):
            print(f"Hint: {resp['hint']}")
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
