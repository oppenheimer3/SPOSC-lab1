# Lab 1: Night Shift — Restore the Link (Storm Outage)

**Role:** You are the on-duty engineer at the Satellite Control Center (SCC).
**Time:** 02:14. Your phone rings. A gateway operator says only: *"GW2 link is dead. Zero."*
That is all you get. No diagnosis. A severe localized storm is sitting over VSAT7. Diagnose it yourself.

## 1. The system

- Path: `GW2 (gateway) <> GEO1 (36,000 km, bent-pipe) <> VSAT7 (user terminal)`
- Service contract: **≥ 35 Mbps** downlink, Ku-band, 36 MHz transponder.
- Initial state: **total outage, 0 Mbps**.
- You do NOT have physical access. You have two Python scripts.

## 2. Your tools

See the link status:

```bash
python status.py
```

Send a command (one at a time):

```bash
python send_command.py --cmd repoint
```

Not sure what commands exist? Each one is briefly explained here:

```bash
python send_command.py --help
```

## 3. What the shift log says (unverified rumors — trust numbers, not stories)

- "Storm cell right over VSAT7, rain like crazy." (night tech)
- "Antenna took a beating in the wind, G/T looks off." (night tech)
- "Just crank the power to max, more power fixes everything." (previous shift)
- "The fastest MODCOD is the best, leave it." (intern)
- "The safest MODCOD is bulletproof, good enough, right?" (intern)
- "Could we move to a higher frequency? Heard there's spare spectrum." (manager)
- "Why not handover to the next satellite like Starlink does?" (manager — GEO does not handover. Ignore.)

## 4. Your task

Restore the link. You have max 15 commands.
