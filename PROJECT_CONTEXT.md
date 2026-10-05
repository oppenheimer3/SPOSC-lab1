# SPOSC Lab 1 — Project Context (Render-publishable)

## What this is
GEO bent-pipe link simulator for a night-shift troubleshooting lab.
Students get only `GET /status` + `POST /control`, must reach `NOMINAL (>=35 Mbps)`.
Current hidden fault: healthy PHY, transport BDP starvation (see INSTRUCTOR_KEY.md, never public).

Live: https://sposc-lab1.onrender.com/
GitHub: https://github.com/oppenheimer3/SPOSC-lab1 (public, answer key excluded)

## Repo layout
- `sat_api.py` — whole simulator, stdlib only (`http.server`, `argparse`, `os`, `json`, `math`, `time`)
- `LAB.md` — student handout (source of truth, PDF built from it)
- `Lab1_Night_Shift.pdf` — generated handout
- `requirements.txt` — intentionally dependency-free, keeps Render Python detection working
- `render.yaml` — Blueprint: `pip install -r requirements.txt` / `python sat_api.py`, health check `/status`
- `.gitignore` — must include `INSTRUCTOR_KEY.md` + `__pycache__/`, `*.pyc`
- `INSTRUCTOR_KEY.md` — LOCAL ONLY, never commit/push/deploy

## Render contract (keep this to stay deployable)
1. Bind `0.0.0.0` + port from `$PORT`: `default_port = int(os.environ.get("PORT", "8765"))`
2. `GET /` returns `{"ok": True}` — Render health checks hit `/`
3. `GET /status` is the healthCheckPath in `render.yaml`
4. CORS headers on every response (`Access-Control-Allow-Origin: *` + `do_OPTIONS`) — students use browser fetch
5. No external deps, no files, no DB — state is in-memory `SESSIONS` dict (one `fresh_state()` per student); Render restart wipes all sessions
6. No secrets in code/env needed; `print(..., flush=True)` so Render logs show startup
7. Free tier sleeps after idle (~50s cold start) — warn students to retry once

## Session model (per-student isolation)
- `SESSIONS: dict[sid -> state]` + `fresh_state()` factory; `get_state(sid)` / `reset_state(sid)` under `SESSIONS_LOCK`
- SID sources (priority): POST JSON `student`/`id`/`sid` → query `?student=` → `X-Student-ID` header → `"shared"` fallback
- SIDs normalized: lowercase, alnum/`-`/`_` only, max 64 chars; cap `MAX_SESSIONS=500` with oldest-eviction via `SESSIONS_LAST`
- `ThreadingHTTPServer` (not `HTTPServer`) so concurrent students don't block each other
- `POST {"cmd":"reset"}` resets only the caller's session; grading uses per-session `commands_log`
- `/status` echoes `"student": sid` so students can verify they are on their own session

## Where the exercise logic lives (`sat_api.py`)
- `fresh_state()` — defaults = baseline students see (orbit, freq, hpa, modcod, tcp_win, sack, pep, arq)
- `MODCODS` — `(capacity_mbps, required_EbN0)`: raising capacity raises required Eb/N0 (cliff effect)
- `current_physics(s)` — C/N, Eb/N0, BER, RTT, capacity; owns RF traps (freq absorption at ~22/60 GHz, HPA saturation >90%, ARQ +320ms)
- `current_throughput(s)` — owns transport model (BDP cap 5.5 Mbps without PEP, 12–14 partial, full capacity only if pep+sack+window>=BDP)
- `link_state_label()` — `NOMINAL if thr >= 35.0`
- `H.do_GET / do_POST` — keep `/status` output minimal/terse (no hints); every `POST cmd` is logged to `commands_log` for grading

## How to alter the exercise safely
- New fault: change `STATE` defaults + corresponding model branch (e.g. make PHY sick: lower `BASE_EB_N0`, raise `BASE_RTT_MS`, set `hpa_pct` low)
- Keep exactly one working combination; update `INSTRUCTOR_KEY.md` (local) with new numbers + why each decoy fails
- Keep `/status` field names stable unless you also update `LAB.md` tasks/grading (BDP math depends on `rtt_ms` + `tcp_win_kb` fields existing)
- Test matrix after any change: baseline `thr` DEGRADED → each decoy behaves as documented → intended fix reaches NOMINAL ≤8 cmds
- Never reveal diagnosis in `ack`/`status`; keep `bw_used` derivation consistent with `MODCODS[modcod]`

## Local test before pushing (must pass)
```bash
PORT=8766 python3 sat_api.py & pid=$!
sleep 1
curl -s localhost:8766/                    # {"ok": true, ...}
curl -s localhost:8766/status | python3 -m json.tool   # baseline DEGRADED
curl -s -X POST localhost:8766/control -H 'Content-Type: application/json' -d '{"cmd":"set_hpa","value":68}'
kill $pid
```

## Publish flow
```bash
git add sat_api.py LAB.md Lab1_Night_Shift.pdf requirements.txt render.yaml .gitignore PROJECT_CONTEXT.md
git commit -m "..."; git push origin main   # Render autoDeploys (or Manual Deploy in dashboard)
# Verify: curl -s https://sposc-lab1.onrender.com/status
# Reset between groups: Render dashboard → Restart / Redeploy
```
