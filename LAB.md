# Lab 1: Night Shift — Restore the Link

**Role:** You are the on-duty engineer at the Satellite Control Center (SCC).
**Time:** 02:14. Your phone rings. A gateway operator says only: *"GW2 link is slow."*
That is all you get. No diagnosis. Diagnose it yourself.

## 1. The system

- Path: `GW2 (gateway) <> GEO1 (36,000 km, bent-pipe) <> VSAT7 (user terminal)`
- Service contract: **≥ 35 Mbps** downlink, Ku-band, 36 MHz transponder, `QPSK 3/4`.
- The satellite is a **transparent bent-pipe**: no onboard demod/remod, no routing. What goes up comes down, plus noise.
- You do NOT have physical access. You have an API.

## 2. The API (your only window into the link)

Start the simulator (instructor runs it, or run yourself):

```bash
python3 sat_api.py --port 8765
```

Get status (repeat as often as you like):

```bash
curl -s localhost:8765/status | python3 -m json.tool
```

Send a control command:

```bash
curl -s -X POST localhost:8765/control \
  -H 'Content-Type: application/json' \
  -d '{"cmd":"set_hpa","value":80}'
```

Allowed `cmd` values:

| cmd | value example | what it does |
|---|---|---|
| `set_hpa` | `50`–`100` | HPA transmit power % |
| `set_modcod` | `QPSK_3/4`, `8PSK_5/6`, `16APSK_5/6`, `256APSK_9/10` | modulation + coding |
| `set_freq_ghz` | `12.2`, `14.0`, `22`, `60` | carrier frequency |
| `set_tcp_window_kb` | `64`–`8192` | TCP advertised window |
| `set_sack` | `1` / `0` | Selective Acknowledgments on/off |
| `set_pep` | `1` / `0` | Performance Enhancing Proxy on/off |
| `set_link_arq` | `1` / `0` | link-layer ARQ on/off |
| `repoint` | — | repoint ground antenna |
| `reboot` | — | reboot payload (8 s outage) |
| `set_payload_mode` | `BENT` / `REGEN` | payload processing mode |

The API replies `{"ack": ..., "thr_mbps": ..., "result": "DEGRADED"|"NOMINAL"}`.
It will **not** tell you what is wrong. `NOMINAL` (≥ 35 Mbps) means you fixed it.

## 3. What the shift log says (unverified rumors — trust numbers, not stories)

- "Light rain near VSAT7." (night tech)
- "Maybe we just need more power?" (previous shift)
- "Just switch to 256APSK for more bits, it's faster, right?" (intern)
- "Someone turned on link ARQ last week, seemed safer." (notes)
- "Could we move to 22 GHz? Heard there's spare spectrum." (manager)
- "Why not handover to the next satellite like Starlink does?" (manager — GEO does not handover. Ignore.)

## 4. Your tasks (90 min)

1. **Baseline (15 min).** `GET /status`. Record every field. Compute:
   - Free-space path loss at 12.2 GHz / 35,786 km. Is the path loss the problem?
   - Bandwidth-Delay Product (BDP) from observed RTT and contracted rate. Compare BDP against the reported TCP window.
   - Link-budget check: is `C/N`, `Eb/N0`, `BER`, `G/T`, `bw_used vs bw_alloc`, `hpa_pct` healthy or not?
   - Classify: power-limited? bandwidth-limited? Neither?
2. **Map to layers (15 min).** For each candidate fix in the table above, state which segment (space / ground / control) and which OSI layer (PHY / link / network / transport) it acts on. Which layer do your numbers actually implicate?
3. **Intervene (45 min).** You have a limited power and spectrum budget — every command is logged and graded. Propose a hypothesis, send ONE command, re-measure, record. Repeat. Reach `NOMINAL`.
   - There are ~10 plausible actions. Only **one combination** restores the contract. The rest do nothing or make it worse. Brute-forcing without a hypothesis will be penalized.
4. **Report (15 min, 1 page max).** Submit:
   - Initial status dump + your three calculations from (1).
   - Command log with hypothesis → result for each attempt.
   - Final status dump showing `NOMINAL`.
   - Explanation: why did the working fix work, and why did at least three others fail — using Ch.1 terms: bent-pipe vs regenerative, GEO delay, FSPL, gaseous absorption, `Eb/N0` vs BER, power- vs bandwidth-limited, BDP / slow-start / Reno / PEP / SACK.

## 5. Rules

- Do not read `sat_api.py`. Treating it as the answer key is a fail.
- Do not assume the fault is where the rumors say it is.
- A healthy `C/N` with a sick throughput is data, not a contradiction. Think layers.

## Grading (10 pts)

- Correct BDP + link-budget verdict (3)
- Correct layer/segment mapping (2)
- Reaching NOMINAL with ≤ 8 commands and a logged hypothesis per command (3)
- Correct Ch.1 explanation of fix + 3 failed alternatives (2)
