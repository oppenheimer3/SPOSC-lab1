# Lab 1: Night Shift — Restore the Link (Storm Outage)

**Role:** You are the on-duty engineer at the Satellite Control Center (SCC).
**Time:** 02:14. Your phone rings. A gateway operator says only: *"GW2 link is dead. Zero."*
That is all you get. No diagnosis. A severe localized storm is sitting over VSAT7. Diagnose it yourself, stage by stage.

## 1. The system

- Path: `GW2 (gateway) <> GEO1 (36,000 km, bent-pipe) <> VSAT7 (user terminal)`
- Service contract: **≥ 35 Mbps** downlink, Ku-band, 36 MHz transponder.
- The satellite is a **transparent bent-pipe**: no onboard demod/remod, no routing. What goes up comes down, plus noise.
- Initial state: **total outage, 0 Mbps**. The link was left in a bandwidth-limited `256APSK 9/10` configuration. The storm brings heavy rain fade; wind has drifted the VSAT7 antenna.
- You do NOT have physical access. You have an API — and three stages to clear.

## 2. The API (your only window into the link)

Base URL: `https://sposc-lab1.onrender.com`
Each student gets an isolated simulator: always use your own ID (e.g. Neptun code, lowercase).
Use it on every call, otherwise you share the `shared` session with anyone who forgot their ID.

There is NO single dump of all telemetry. Like a real SCC, you pick a diagnostic tool,
form a hypothesis first, then query. Pasting one JSON blob into a chatbot will not solve this.

Step 1 — symptom (free, repeat any time):

```bash
curl -s "https://sposc-lab1.onrender.com/status?student=YOUR_ID" | python3 -m json.tool
```
Returns `thr_mbps` + `result` + your game `stage` (1–4) and a `stage_hint` telling you which domain to look at next. No RF or TCP numbers.

Step 2 — pick a diagnostic tool based on your hypothesis:

```bash
curl -s "https://sposc-lab1.onrender.com/tools/rf?student=YOUR_ID" | python3 -m json.tool
curl -s "https://sposc-lab1.onrender.com/tools/tcp?student=YOUR_ID" | python3 -m json.tool
curl -s "https://sposc-lab1.onrender.com/tools/ttc?student=YOUR_ID" | python3 -m json.tool
```
- `/tools/rf` = spectrum analyzer + demod stats (frequency, power, MODCOD, C/N, Eb/N0, BER, lock, frame loss, G/T, bandwidth).
- `/tools/tcp` = TCP trace (RTT, window, SACK, PEP, link ARQ).
- `/tools/ttc` = TT&C telemetry (orbit, station-keeping, payload health — is the satellite itself the problem?).

Step 3 — intervene. Every command MUST carry your hypothesis in `rationale`
(min 15 characters), or it is rejected. It is logged and graded.

```bash
curl -s -X POST https://sposc-lab1.onrender.com/control \
  -H 'Content-Type: application/json' \
  -d '{"student":"YOUR_ID","cmd":"set_hpa","value":80,"rationale":"testing if link is power-limited before touching transport"}'
```

Reset your own session to baseline (does not affect others, needs no rationale):

```bash
curl -s -X POST https://sposc-lab1.onrender.com/control \
  -H 'Content-Type: application/json' \
  -d '{"student":"YOUR_ID","cmd":"reset"}'
```

Budget: 30 control commands per student (reset excluded, reset does NOT refill it).
The intended solve is 6 commands — full marks require ≤ 8 with a real hypothesis each. Blind scripts will burn the budget.

Allowed `cmd` values:

| cmd | value example | what it does |
|---|---|---|
| `set_hpa` | `50`–`100` | HPA transmit power % (EIRP; >96 saturates) |
| `set_modcod` | `QPSK_1/2`, `QPSK_3/4`, `8PSK_5/6`, `16APSK_5/6`, `256APSK_9/10` | modulation + coding |
| `set_freq_ghz` | `12.2`, `14.0`, `22`, `60` | carrier frequency |
| `set_tcp_window_kb` | `64`–`8192` | TCP advertised window |
| `set_sack` | `1` / `0` | Selective Acknowledgments on/off |
| `set_pep` | `1` / `0` | Performance Enhancing Proxy on/off (spoofing + split-TCP) |
| `set_link_arq` | `1` / `0` | link-layer ARQ on/off |
| `repoint` | — | repoint ground antenna + inspect LNA (restores G/T) |
| `reboot` | — | reboot payload (8 s outage, your session only) |
| `set_payload_mode` | `BENT` / `REGEN` | payload processing mode |
| `reset` | — | reset YOUR session to baseline |

The API replies `{"ack": ..., "thr_mbps": ..., "result": "DEGRADED"|"NOMINAL", "stage": 1-4, "stage_hint": ...}`.
On success (`NOMINAL`) it also sends a `message` field confirming the restore.
`NOMINAL` (≥ 35 Mbps) means you fixed it.
Always check the `student` field in replies — if it is not your ID, you forgot to send it.

## 3. The three stages (your mission)

**Stage 1 — Re-align + overcome rain (physical channel & control segment).**
Heavy rain absorbs/scatters Ku-band energy; wind drifted the VSAT7 antenna (low G/T). Carrier power C fell below noise N, Eb/N0 below threshold, modem unlocked, 0 Mbps.
- Verify via TT&C that GEO1 orbit/payload is healthy (it is — the fault is on the ground).
- `repoint` the antenna (restores G), then raise HPA (EIRP) into the ~88–95% linear window to burn through rain + FSPL. Max power (>96%) saturates — more is not always better.
- Gate: `/tools/rf` shows `locked: true` (C/N above threshold). Throughput is still ~0 because of Stage 2.

**Stage 2 — Fall back MODCOD (PHY & link layer).**
Carrier is locked but frames drop: the link is stuck in bandwidth-limited `256APSK 9/10`, which needs ~18 dB Eb/N0 you do not have under rain.
- Shift to a power-limited regime: robust QPSK. `QPSK_1/2` always decodes (safe) but caps at 28 Mbps — too slow for the contract. `QPSK_3/4` is the sweet spot: decodes with margin and delivers 42 Mbps.
- Higher orders (8PSK and up) stay on the BER cliff under rain.
- Gate: `/tools/rf` shows `frame_loss_pct: 0` and clean BER. Throughput returns but stalls at a few Mbps because of Stage 3.

**Stage 3 — PEP + window scaling (transport & network).**
Frames are clean but TCP stalls: GEO RTT ~542 ms × rate = BDP ≈ 2.8 MB, while the default 64 KB window exhausts long before ACKs return; residual errors halve cwnd (Reno).
- Activate `PEP` (spoofing + split-TCP), enable `SACK` (isolate wireless loss from congestion), scale the window ≥ 2500 KB (4096 recommended).
- Window alone caps ~5.5 Mbps; PEP without SACK caps ~12–14 Mbps. All three together fill the pipe.
- Gate: throughput ramps smoothly to ≥ 35 Mbps. API returns `NOMINAL` + success `message`. You are done.

## 4. What the shift log says (unverified rumors — trust numbers, not stories)

- "Storm cell right over VSAT7, rain like crazy." (night tech — this one is true)
- "Antenna took a beating in the wind, G/T looks off." (night tech — also true)
- "Just crank HPA to 100, more power fixes everything." (previous shift — saturation says hi)
- "256APSK is the fastest, leave it." (intern — fastest way to stay at 0 Mbps)
- "QPSK 1/2 is bulletproof, good enough, right?" (intern — bulletproof but 28 < 35)
- "Could we move to 22 GHz? Heard there's spare spectrum." (manager — absorption peak)
- "Why not handover to the next satellite like Starlink does?" (manager — GEO does not handover. Ignore.)

## 5. Your tasks (90 min)

1. **Baseline (15 min).** Query `/status` (note `stage: 1`, 0 Mbps), then `/tools/ttc` (is the satellite the problem?), `/tools/rf` (C/N vs lock? G/T? Eb/N0 vs MODCOD requirement? BER? frame loss?), `/tools/tcp` (RTT? window vs BDP?). Record every field. Compute:
   - Free-space path loss at 12.2 GHz / 35,786 km (needs `orbit_km` + `freq_ghz` from `/tools/rf`). Is path loss the problem, or rain + pointing on top of it?
   - Bandwidth-Delay Product (BDP) from observed RTT and contracted rate (needs `rtt_ms` from `/tools/tcp`). Compare BDP against the 64 KB window.
   - Link-budget check (needs `/tools/rf`): is `C/N` vs lock threshold, `Eb/N0` vs MODCOD requirement, `BER`, `frame_loss_pct`, `G/T`, `hpa_pct` healthy or not? Which stage does each number implicate?
   - Classify per stage: Stage 1 power-limited (C/N)? Stage 2 bandwidth- vs power-limited (MODCOD)? Stage 3 transport-limited (BDP)?
2. **Map to layers (15 min).** For each candidate fix in the table above, state which segment (space / ground / control) and which OSI layer (PHY / link / network / transport) it acts on. Which layer do your numbers actually implicate at each stage?
3. **Intervene (45 min).** You have 30 commands and every one needs a `rationale` — it is logged and graded. Follow the stages: (1) repoint + HPA → RF lock; (2) MODCOD fallback → 0% frame loss; (3) PEP + SACK + window → NOMINAL. Propose a hypothesis, send ONE command, re-measure with the right tool, record. Repeat. Reach `NOMINAL`.
   - Only **one combination** restores the contract. The rest do nothing or make it worse (saturation, absorption peaks, ARQ latency, outage, slow-but-capped QPSK_1/2). Brute-forcing without a hypothesis is rejected by the API and penalized in grading.
4. **Report (15 min, 1 page max).** Submit:
   - Initial status dump + your three calculations from (1).
   - Command log with hypothesis → result for each attempt, labeled by stage.
   - Final status dump showing `NOMINAL` + success message.
   - Explanation: why did each stage's fix work, and why did at least three alternatives fail — using Ch.1 terms: bent-pipe vs regenerative, GEO delay, FSPL, rain fade, gaseous absorption, `Eb/N0` vs BER, power- vs bandwidth-limited, BDP / slow-start / Reno / PEP / SACK.

## 6. Rules

- Do not read `sat_api.py`. Treating it as the answer key is a fail.
- Do not assume the fault is where the rumors say it is — except the storm, which is real.
- A healthy TT&C with a dead throughput is data, not a contradiction. Think layers and stages.
- AI tools are permitted, but a single paste cannot solve this: telemetry is split across
  tools and every command needs your own stated hypothesis. An AI that tells you *what
  to measure next* is being used well; one you ask for the final answer will guess.
- Use only YOUR student ID; solving in another session does not count.

## Grading (10 pts)

- Correct BDP + link-budget verdict per stage (3)
- Correct layer/segment mapping (2)
- Reaching NOMINAL with ≤ 8 commands and a logged hypothesis per command (3)
- Correct Ch.1 explanation of each stage fix + 3 failed alternatives (2)
