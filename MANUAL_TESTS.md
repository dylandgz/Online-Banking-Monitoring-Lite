# Manual Test Checklist — Stage 5 (confirmation burst + confidence scoring)

How to use: for each test, set `TARGET_URL`/`REQUIRED_TEXT` in `.env` as shown, restart the
monitor, wait for the described behavior, then check the DB/dashboard and fill in Result.

**Restart:**
```bash
kill $(lsof -tiTCP:8080 -sTCP:LISTEN)
cd /Users/dylandominguez/VS-StudioProjects/Online_Banking_Uptime_Monitoring_Lite
source .venv/bin/activate
python -m monitor.main
```

**Check the DB directly** (most reliable — avoids stdout buffering weirdness):
```bash
sqlite3 -header -column data/monitor.db "SELECT ts, ok, fail_reason, layer, burst_id FROM checks ORDER BY id DESC LIMIT 10;"
sqlite3 -header -column data/monitor.db "SELECT * FROM incidents ORDER BY id DESC LIMIT 5;"
sqlite3 -header -column data/monitor.db "SELECT * FROM state;"
```

**Dashboard:** `http://localhost:8080` (basic auth: `DASHBOARD_USER`/`DASHBOARD_PASSWORD` in `.env`) — now shows "Watching: `<TARGET_URL>`" under the title so you can confirm what's configured.

---

## Baseline — confirm UP works

- **.env:** `TARGET_URL=https://www.google.com/`  `REQUIRED_TEXT=Google`
- **Expect:** every check `ok=1`, `fail_reason` empty, dashboard banner green "UP".
- **Logic:** sanity check that a real target with correct required text passes cleanly before testing failure paths.
- **Result:** ☐ Pass ☐ Fail — Notes: ___________________________

---

## 1. `dns` (Hard, weight 2)

- **.env:** `TARGET_URL=https://this-domain-does-not-exist-xyz123.invalid`  `REQUIRED_TEXT=` (anything)
- **Expect fail_reason:** `dns` on the first probe, `layer=pulse`.
- **Logic:** curl_cffi can't resolve the hostname at all. This is Hard evidence (weight 2). A second failing probe (the burst's render re-probe, ~15s later) pushes cumulative confidence to ≥3 → `DOWN` fires almost immediately (~15–30s), well under the 60s SLO.
- **Result:** ☐ Pass ☐ Fail — Notes: ___________________________

---

## 2. `conn_refused` (Hard, weight 2)

- **.env:** `TARGET_URL=http://localhost:9`  `REQUIRED_TEXT=` (anything)
- **Expect fail_reason:** `conn_refused`, `layer=pulse`.
- **Logic:** nothing is listening on that port — the OS actively refuses the connection. Hard evidence, same fast-page behavior as `dns`.
- **Result:** ☐ Pass ☐ Fail — Notes: ___________________________

---

## 3. `bad_status:500` (Hard, weight 2)

- **.env:** `TARGET_URL=https://httpbin.org/status/500`  `REQUIRED_TEXT=` (anything)
- **Expect fail_reason:** `bad_status:500`, `layer=pulse`.
- **Logic:** 5xx is explicitly Hard per CLAUDE.md's confidence table — the server itself is erroring, not just serving unexpected content.
- **Result:** ☐ Pass ☐ Fail — Notes: ___________________________

---

## 4. `bad_status:404` (Soft, weight 1 — judgment call, see PROGRESS.md)

- **.env:** `TARGET_URL=https://httpbin.org/status/404`  `REQUIRED_TEXT=` (anything)
- **Expect fail_reason:** `bad_status:404`, `layer=pulse`.
- **Logic:** CLAUDE.md's table only lists `bad_status:5xx` as Hard; non-5xx codes default to Soft (ambiguous evidence stays cautious). Should take 3 consecutive soft fails (weight 1 each) to reach `DOWN_CONFIDENCE=3`, i.e. all 3 burst probes (~35–40s), not 2.
- **Result:** ☐ Pass ☐ Fail — Notes: ___________________________

---

## 5. `timeout` (Soft, weight 1)

- **.env:** `TARGET_URL=https://httpbin.org/delay/15`  `REQUIRED_TEXT=` (anything)
- **Expect fail_reason:** `timeout`, `layer=pulse`.
- **Logic:** the pulse check's own timeout (10s default) fires before httpbin's artificial 15s delay responds. Soft evidence — should take all 3 burst probes to page, same as the 404 case.
- **Result:** ☐ Pass ☐ Fail — Notes: ___________________________

---

## 6. `element_missing` (Soft, weight 1) — the "cosmetic DOM change" case

- **.env:** `TARGET_URL=https://httpbin.org`  `REQUIRED_TEXT=this text will never appear on httpbin`
- **Expect fail_reason:** `element_missing`, `layer=render` (note: `http_status=200` since pulse passes — the site is genuinely up, only the content check fails).
- **Logic:** this is the exact scenario the project exists to handle correctly (Selenium's old false-positive problem). Should take 3 soft fails to page. **Watch for:** because pulse always passes here (httpbin is reachable), a pulse-only burst re-probe can't detect this content problem at all — if the burst's probe rotation lands on a pulse-only re-probe before confidence hits 3, that probe will pass and clear the burst as a "flap" even though the underlying content issue is real and persistent. Worth noting whether this happens.
- **Result:** ☐ Pass ☐ Fail — Notes: ___________________________

---

## 7. Recovery — clears cleanly, one email only

- **After any DOWN test above:** set `TARGET_URL`/`REQUIRED_TEXT` back to a real passing config (e.g. the baseline) and restart.
- **Expect:** first passing check → state flips back to `UP`, exactly one `[MONITOR] ... RECOVERED after ...` email, incident row gets `ended_at`/`duration_s`/`confidence` filled in.
- **Logic:** Rule 2 — recovery alerts exactly once, on the first success after DOWN, never again.
- **Result:** ☐ Pass ☐ Fail — Notes: ___________________________

---

## Not testable yet (need Stage 6+ login journey)

`auth_rejected`, `auth_unavailable`, `mfa_failed`, `bot_challenge`, `rate_limited`, `session_expired`, `data_plane_missing`, `api_bad_status:<code>`, `api_shape_mismatch`, `logout_failed` — none of these can be triggered by the current pre-auth check path. Revisit this checklist once Stage 6 (`journey.py`) lands.
