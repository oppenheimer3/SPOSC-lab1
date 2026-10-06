#!/usr/bin/env python3
"""Poll the SAT-1 link status in a loop.

Usage:
    python status.py
    python status.py --interval 5 --once
    NAME=alice python status.py   (skips the prompt)

Stdlib only. Ctrl+C to stop.
"""
import argparse
import json
import os
import sys
import time
import urllib.parse
import urllib.request


def fetch_status(base_url, student):
    qs = urllib.parse.urlencode({"student": student})
    url = base_url.rstrip("/") + "/status?" + qs
    with urllib.request.urlopen(url, timeout=15) as r:
        return json.load(r)


def resolve_name(cli_name=None):
    """Ask for the name only once: --name > $NAME/$STUDENT_ID > saved .player file > prompt (then save)."""
    try:
        base = os.path.dirname(os.path.abspath(__file__))
    except NameError:
        base = os.getcwd()
    path = os.path.join(base, ".player")
    if cli_name and cli_name.strip():
        name = cli_name.strip()
        try:
            with open(path, "w") as f:
                f.write(name + "\n")
        except OSError:
            pass
        return name
    name = os.environ.get("NAME", "").strip() or os.environ.get("STUDENT_ID", "").strip()
    if not name:
        try:
            with open(path) as f:
                name = f.read().strip()
        except OSError:
            name = ""
    if not name:
        try:
            name = input("Your name (asked once, then remembered): ").strip()
        except EOFError:
            name = ""
        if name:
            try:
                with open(path, "w") as f:
                    f.write(name + "\n")
            except OSError:
                pass
    if not name:
        print("No name given, using the 'shared' session.", file=sys.stderr, flush=True)
        return "shared"
    return name


def main():
    ap = argparse.ArgumentParser(description="Keep printing the GW2 link status.")
    ap.add_argument("--url", default=os.environ.get("SAT_URL", "https://sposc-lab1.onrender.com"),
                    help="simulator base URL")
    ap.add_argument("--interval", type=float, default=2.0,
                    help="seconds between polls (default: 2.0)")
    ap.add_argument("--once", action="store_true",
                    help="print once and exit")
    ap.add_argument("--name", default=None,
                    help="your name (saved, so you only type it once)")
    a = ap.parse_args()
    a.student = resolve_name(a.name)

    if a.student == "shared":
        print("WARNING: using the 'shared' session. Type your name for your own simulator.",
              file=sys.stderr, flush=True)

    while True:
        try:
            st = fetch_status(a.url, a.student)
            ts = time.strftime("%H:%M:%S")
            line = f"[{ts}] thr={st.get('thr_mbps')} Mbps status={st.get('status')} stage={st.get('stage')}"
            if st.get("controls_used") is not None:
                line += f" controls_used={st.get('controls_used')}"
            print(line, flush=True)
            rf = st.get("rf", {})
            print(f"  rf: C/N {rf.get('c_n_db')} dB (lock at {rf.get('lock_threshold_db')}, locked={rf.get('locked')}) "
                  f"G/T {rf.get('g_t')} MODCOD {rf.get('modcod')} Eb/N0 {rf.get('eb_n0_db')}/{rf.get('eb_n0_required_db')} "
                  f"BER {rf.get('ber')} frames lost {rf.get('frame_loss_pct')}%", flush=True)
            tcp = st.get("tcp", {})
            print(f"  tcp: RTT {tcp.get('rtt_ms')} ms win {tcp.get('tcp_win_kb')} KB (BDP {tcp.get('bdp_kb')} KB) "
                  f"PEP {tcp.get('pep')} SACK {tcp.get('sack')} ARQ {tcp.get('link_arq')}", flush=True)
            if st.get("hint"):
                print(f"  hint: {st['hint']}", flush=True)
            if st.get("message"):
                print(f"  *** {st['message']} ***", flush=True)
        except Exception as e:  # noqa: BLE001 - keep polling through blips (cold starts, network)
            print(f"[error] {e} (retrying...)", file=sys.stderr, flush=True)
        if a.once:
            break
        try:
            time.sleep(a.interval)
        except KeyboardInterrupt:
            break
    print("stopped.")


if __name__ == "__main__":
    main()
