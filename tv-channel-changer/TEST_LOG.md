# Test log: real devices

Running record of what works and what doesn't while following [TESTING.md](TESTING.md).
Newest findings go at the bottom of each phase. The PSK is deliberately not recorded here.

## Setup under test

| Item | Value |
|---|---|
| TV | Sony KD-49X8000H (2020 Android TV), `192.168.1.8` |
| Box | Airtel Xstream (Android), `192.168.1.5`, plugged into HDMI 1 |
| Test machine | Windows 11 laptop `192.168.1.14`, project venv, platform-tools adb |
| Server / tablet / blaster | not yet set up |

## Status at a glance

| Phase | Result |
|---|---|
| 0 Preparation | Done (box type, IPs, venv, config) |
| 1 TV settings + raw API | **PASS** |
| 2 `diagnose.py psk` / `inputs` | **PASS** |
| 3 CEC test | **FAIL**: digits arrive but box commits after 2 digits, whatever the timing |
| 4A Xstream ADB | **BLOCKED**: network ADB unavailable on this firmware |
| 4C Android TV Remote (6466/6467) | **PASS**: `atv_remote` driver written and working on the real box |
| 4B IR blaster | Not needed unless 4C proves unreliable |
| 5 Digit timing | **PASS**: `inter_digit_ms = 150`, 10/10 tunes, menu escape OK |
| 6 Box power detection | Native on `atv_remote` (`is_on`, wake/sleep keys); wake from real standby untested |
| 7 TV latency | **PASS** with a caveat: picture takes 9-28 s after the API says ready; `hdmi_settle_s = 25`. The 30-minute cold-standby trial is still to do |
| 8 Mismatched states | **PASS** (4 of 5 cases in the full run, case 1 passed on re-run): box power via POWER key, see Phase 8 |
| 9 Server, tablet, buttons | **PASS** on the laptop plus a real phone over Wi-Fi (cards, off, tunes, haptics, down screen, recovery); Pi/systemd and a real Shelly button untested |
| 10 Real-world tests | **In progress**: 2 of 7 done (Nani's own remote, wrong channel by hand); morning cold start, power cut, router reboot, rapid taps on a button, and the Nani test remain |

## Phase 0: Preparation

- Box is an Xstream (Android), so Phase 4A applies first.
- TV IP changed from `.128` to `.8` while testing. Confirm the DHCP reservation is on `.8`.
- Python 3.15 in `.tvenv`; fastapi, uvicorn, httpx and broadlink all import fine.
- `config.toml` now has TV ip, psk, mac, `hdmi_port = 1`, `driver = "adb"`, box host, adb_path.

## Phase 1: TV settings and raw API

**Works**
- TV answers on the LAN while in standby (`getPowerStatus` -> `standby` after 2 min). Remote start is effectively working.
- With the PSK set: `setPowerStatus` wakes the TV (active after ~0.6 s from standby) and `setPlayContent extInput:hdmi?port=1` lands on the Airtel input directly. Repeatable.

**Findings / gotchas**
- Read calls (`getPowerStatus`) work with no auth. Control calls return `403 Forbidden` until the PSK is configured.
- Initial state: no PSK had ever been set on the TV, so even the right key was rejected. The PSK entry only appears after choosing **Authentication = Normal and Pre-Shared Key** under IP control. It was hard to find on this model.
- Windows/PowerShell: `curl.exe -d '{\"..\"}'` mangles the JSON (`JSON Format Error`). Send the body from a file with `--data-binary @file`.

## Phase 2: Diagnostic script (TV)

**Works**
- `diagnose.py psk`: all PASS. Correct PSK accepted, wrong PSK rejected (auth really enforced), 146 IRCC keys including Num0-Num9.
- `diagnose.py inputs`: target `extInput:hdmi?port=1` exists and is the box. The TV also lists a CEC device `XStream_Smart_` on port 1.

**Findings**
- HDMI 2 is labelled "TV box" and shows connected. Unknown what is plugged in there. Doesn't affect the config.
- The script's CEC hint (bare "HDMI 1" title means no CEC) is misleading on this TV: the box shows up as a *separate* `extInput:cec?...` entry, not in the `hdmi` entry's title.

## Phase 3: CEC pass-through

**Result: partial.** `diagnose.py cec --channel 105` makes digits reach the box, but it ends up on the wrong channel.

- Observed: box switched to channel 42, then showed `10` and then `5` (not `105`). Same on a second run.
- Meaning: CEC forwarding works, but the gap before the last digit exceeds the box's digit-entry timeout, so the entry committed as `10` and `5` started a new one. Each digit is its own HTTP request -> TV -> CEC hop, so the gap is long and uneven.
- Open question: why it went to 42 first (it may simply have been the starting channel).
- Limits that apply even if timing is fixed: TV must be on and showing HDMI 1; no visibility of box state; no box wake/sleep.

### Phase 3/5 follow-up: digit-timing runs and single-key probes

- `diagnose.py digits --channels 105 210 --gaps 150 250 350 450 650` with `driver = "tv_cec"`: **every gap failed for both channels** (the gap value makes no difference).
- `pre_tune_keys` was set to `[]` for the CEC runs, because Exit/Back on an Android TV box can leave live TV.
- Single-key probes (Bravia IRCC `Num*`, sent one by one with a 300 ms gap):
  - `1` alone: box shows a bare `1` (no `1 _ _` dashes; Android-TV-style entry).
  - `1`, `0`: shows `10` as one entry.
  - `1`, `0`, `5`: shows `10`, then `5` as a separate entry.
  - `7`, `2`, `3`: same split (`72`, then `3`), so this is not about channel 105 being missing from the lineup.
- **Conclusion: through HDMI-CEC this box commits after two digits regardless of timing. `tv_cec` cannot tune 3-digit channels. Dropped as a driver.**
- Ruled out: gap length, `pre_tune_keys`, non-existent channel number.
- **Key observation (user):** with the Airtel remote, the entry overlay is centered at the top edge and builds `7_`, `72_`, `723`. With our CEC digits the overlay is in the top-right corner and commits after 2 digits. So CEC digits are handled by a different layer (not the Airtel app's own channel entry). This is why timing is irrelevant. A driver that injects keys into the foreground app (Android TV Remote protocol, or IR) should behave like the Airtel remote.

## Phase 4A: Xstream ADB

**Result: blocked.**
- The box is reachable (ping OK), but port 5555 actively refuses connections (`adb connect 192.168.1.5:5555` -> error 10061; `Test-NetConnection` -> False).
- Developer options only lets USB debugging be enabled with a USB cable attached, so Airtel has locked network ADB on this firmware. TESTING.md says to stop here and use an IR blaster.
- `adb` itself is fine (v37.0.1) and the daemon starts.

- Port scan of the box (`192.168.1.5`): **6466 and 6467 open** (Android TV Remote service v2), 8008 and 8009 open (Cast), 9000 open (unknown), 5555 closed (ADB).

## Phase 4C: Android TV Remote protocol (ports 6466/6467)

**Result: PASS. This is the working path.**

- Installed `androidtvremote2` 0.3.2 into the venv and proved the path with a throwaway probe script
  (`atv_probe.py`, since removed: `diagnose.py pair` and the `atv_remote` driver replace it).
- Pairing: the box (`XStream_Smart_Box_001`) showed a 6-character code on the TV; accepted first time.
- **Sending `7`, `2`, `3` at 300 ms landed on channel 723**, with the same centered overlay (`7_`, `72_`, `723`) as the Airtel remote. The keys are injected into the foreground app, unlike CEC.
- The connection reported `is_on = True`; `current_app` came back **empty** (unresolved: check what it reports while Airtel live TV is up).
- Advantages over CEC: works with the TV off or on another input, exposes box state, no extra hardware.

**Driver (done).** `AtvRemoteStb` in `launcher.py` (`driver = "atv_remote"`, section `[stb.atv_remote]`):
pairs once with `python diagnose.py pair`, keeps one connection with auto-reconnect, sends digits with the base class's
start-to-start pacing, reads `is_on`/`current_app`, wakes and sleeps the box with discrete keys (KEYCODE_WAKEUP/SLEEP),
and returns to live TV by launching `live_app` when another app is in the foreground. `diagnose.py` got a `pair` command
and an `atv_remote` branch in `stb`; its `AdbStb` special cases now use a `native_power` flag. Pairing material lives in
`atv_cert.pem`/`atv_key.pem` next to `config.toml` (gitignored; copy them to the Pi with the config).

**Results on the real box**
- `diagnose.py stb`: connected, power ON, foreground app `com.airtel.tv` (so `live_app = "com.airtel.tv"`).
- Home-screen bypass works: from the box's home screen the driver launched `com.airtel.tv` and tuned correctly. The box
  replies to the launch with an "error from the device" log line, but the launch succeeds. Treat that line as noise.
- `current_app` is empty for the first moments of a new connection; the driver then relaunches Airtel (harmless, ~4 s extra).
  A long-lived connection (the server) knows the foreground app, so it skips the relaunch.

## Phase 5 (on atv_remote)

- `diagnose.py digits --channels 723 725 --gaps 100 150 200 300 450`: **all gaps passed for both channels** (the first run
  used 726 by mistake; 726 is not a real channel, so its "no" answers were not timing).
- Config: `inter_digit_ms = 150` (script recommendation, 1.5x the smallest reliable gap of 100 ms).
- Reliability: 10 alternating tunes (723/725) at 150 ms landed correctly **10/10**. Each takes ~2.8 s (0.3 s keys + 2.5 s
  `commit_wait_ms`); the channel visibly changed 1-2 s after the last digit, so `commit_wait_ms = 2500` has margin. First tune
  of a fresh connection takes 7-8 s (connect + live-TV bring-up).
- Menu escape: with Airtel settings open and the same connection (foreground `com.airtel.tv`, no relaunch), tuning 725 still
  landed correctly. Caveat: only the settings screen was tried; deeper menus, the guide, and an open search box are untested.

## Phase 7: TV latency

`diagnose.py latency --trials 3 --cool 60 --manual` (TV in standby for 60 s before each wake, Enter pressed when the picture appeared):

| Trial | API `active` | API reports HDMI 1 | Picture |
|---|---|---|---|
| 1 | 0.1 s | 1.6 s | 19.8 s |
| 2 | 0.1 s | 1.3 s | 28.1 s |
| 3 | 0.6 s | 2.1 s | 9.1 s |

- Warm switch (Google TV home -> HDMI 1): 3.8 s.
- The Bravia API is optimistic: `active` and the HDMI lock come within about 2 s, but the picture appears 9-28 s later (HDCP plus the box's HDMI output). The picture lag is highly variable, so the script's median-based suggestion (18.7 s) is too tight for the slow wakes: **`hdmi_settle_s = 25`** is set instead. `wake_timeout_s = 60` is fine.
- Because `atv_remote` reaches the box over the network, the driver is TV-independent: digits can be sent while the TV boots.
- Not done yet: one trial with `--cool 1800` (a fully cold TV, like the first tap of the morning).
- Follow-up fix: `hdmi_settle_s` used to apply to every input change, so a tap with the TV already on (e.g. on
  YouTube) also waited 25 s; that is likely why the easy Phase 8 case took 30.5 s. It now applies only to cold wakes;
  input switches on a TV that was already on use `hdmi_switch_settle_s` (default 3 s). Re-run `latency --manual`:
  it now asks for the picture after the warm switch too and suggests a value.

## Phase 8: mismatched states

### Finding: how to put the Xstream box in and out of standby

- `is_on` (from the Remote protocol) **tracks standby reliably**: it flipped to False when the Airtel remote's power
  button was pressed, and the Remote service **stays reachable while the box sleeps** (connection and port 6466 stayed up).
- `KEYCODE_SLEEP` (223) and `KEYCODE_WAKEUP` (224) are **ignored** by this box (is_on unchanged for 15-20 s).
- A short **`KEYCODE_POWER` (26) toggles standby in both directions**: sleep took 1.3-2.4 s, wake 1.0-2.0 s to show in `is_on`.
- The box **ignores POWER for a few seconds after a power change**: a press about 1 s after a wake did nothing, one 22 s
  after worked. The driver now waits 15 s since the last power change (a guess inside that range), reads `is_on` first,
  presses once, verifies, and never retries blind (same rule as the IR boxes). Cycle test on the real box:
  wake, no-op, sleep, no-op, wake, all as expected.
- Driver change: `AtvRemoteStb.ensure_on/ensure_off` use POWER with that guard (the 223/224 keys were removed).

### `diagnose.py mismatch --channel 725`

| Case | Result | Time |
|---|---|---|
| TV on / box on | first full run: **FAIL** (`job=ok`, but `tv active=NO hdmi=NO eyes=NO`); alone, with raw API output: **PASS** | 3.6 s (fail) / 6.1 s (alone) |
| TV on / box off | PASS | 40.8 s |
| TV off / box on | PASS | 27.5 s |
| TV off / box off | PASS | 38.7 s |
| Box stuck in a menu | PASS (the Settings escape works end to end) | |

- Case 1 could not be reproduced: alone, power was `active` and the input was HDMI 1 right after the job and 3 s later.
  Possible cause (unproven): earlier in the session the box was put to sleep while the TV sat on HDMI 1, and the TV may
  have gone to standby on its own (no-signal timeout) before that run. Logged as edge case #13; re-run the full mismatch
  once more to see if it recurs.
- The first mismatch attempt had crashed in setup when the box could not be forced to sleep (since fixed by the POWER
  approach above; the harness also now falls back to asking for the Airtel remote instead of crashing).

## Cold start via the real job (TV in standby for hours, box left on a Settings screen)

Ran the actual launcher job for channel 725 without touching the TV first (TV reported `standby`; ICMP ping gets no
reply in this state but the REST API still answers, so network wake works).

| Time | Event |
|---|---|
| 0.6 s | TV waking, box waking |
| 1.7 s | TV input switch to HDMI 1 |
| 4.3 s | TV in the 25 s cold settle |
| ~16 s | **Box side failed**: `live TV app com.airtel.tv did not come to the foreground` (15 s wait) |
| 29.3 s | Job reported failed (waited for the TV settle to finish) |

- The TV half behaved. The failure was the box: its foreground app was `com.android.tv.settings` and the
  `send_launch_app_command` request for Airtel was ignored for a full 20 s (probe), so no digits were sent.
- Same request works from the home screen. **Home key, then launch** put Airtel in the foreground in about 1 s.
  `AtvRemoteStb.goto_live` now does that (Home, 1 s, launch, up to 8 s wait, one retry). The probe confirmed Home+launch
  by hand; the new `goto_live` was confirmed to be a no-op when Airtel is already in front, but its Settings-escape path
  has not been re-run end to end yet.
- Fresh connections report an empty foreground app for the first moments; `goto_live` now waits up to 4 s for a value
  instead of deciding on the empty string (previously it relaunched Airtel needlessly).
- Not captured: the picture time for this cold wake (the job failed before the Enter was used). The planned 30-minute
  cold trial still stands.

## Phase 9: server, tablet, buttons (run on the Windows laptop, tablet UI in the built-in browser)

- Server: `python launcher.py` starts with the `atv_remote` driver. `GET /api/health` reports TV `active` on HDMI 1, the
  box on, and the learned timings (`tv_cold` / `tv_warm`) that feed the countdown.
- `POST /api/tune/723`: job done in 2.8 s (0.3 s of keys plus the 2.5 s commit wait) with the TV already on.
- `POST /api/off`: done in 0.1 s; TV went to standby, box left on (`off_with_tv = false`).
- **Rapid taps:** the same card twice, 2 s apart, returned the same job id (one job, no garbled digits).
- **Bug found and fixed: a different card tapped mid-job did not win.** 725 then 723 0.5 s later left the box on 725:
  the cancelled 725 job had already typed its digits and was only waiting out `commit_wait`, and the box ignored the
  digits typed into its commit window. `StbDriver.select_channel` now waits out the previous entry's commit window
  (`_commit_until`) before typing, so the last tap wins; re-test gave 723 (job 5.1 s instead of 2.8 s). The ADB driver has its
  own `select_channel` and does not have this guard.
- Tablet UI (768 px wide emulation): four large cards, "TV बंद" and volume buttons render legibly. Config now has
  two real cards, 723 and 725 (names are placeholders until the real channel names are known).
- Tablet tap from TV standby (723): countdown started at 23 s, status lines "टीवी तैयार हो रहा है…" and "चैनल ✓" appeared
  separately, the page returned to the cards on its own. The job took 30.7 s; the **picture was visible at about 21 s**
  (when the countdown showed 1), so the 25 s cold settle ran about 9 s longer than needed this time. The countdown then sat
  on "1" for about 9 s. The learned `tv_cold` moved from 22.1 s to 24.7 s, which will make the next countdown closer.
- Smart button webhook (GET): `b2` single mapped to 725 with source `button:b2:single`; the same press 0.4 s later returned the
  same job id (bounce handled); an unknown button and an unmapped press return 404. A **cold** button press showed on the
  tablet (red 725 card with a countdown); warm presses finish between the page's 3 s heartbeats so are not shown, which is fine.
- Server-down screen: with the server stopped, the page showed the dark red "सर्वर से संपर्क नहीं" screen with the help line
  after about 18 s (TESTING.md says about 10 s); it cleared by itself a few seconds after the server returned.

**Not tested here:** the Raspberry Pi install (systemd unit, boot without login, copying `config.toml` and the two pem
files), the real tablet in Fully Kiosk Browser (wake lock, vibration), a real Shelly button, and unplugging the Pi.

### Phase 9 on a real phone (page served from the laptop at its LAN address)

| Check | Result |
|---|---|
| Layout | Buttons visible |
| "TV बंद" | Worked |
| 723 from TV standby | Worked; the picture was up with the countdown still reading about 20, so the 25 s settle kept counting for a long time after the picture (TV had only been off briefly) |
| 725 while the TV is on | Switched correctly |
| Same card tapped twice | Haptic on the first tap only (the second tap joins the running job, so expected) |
| Haptics and screen stays awake | Both work |

- **Bug found and fixed: no request timeout in the page.** With the server stopped, tapping a card left the overlay on "723 ..."
  indefinitely. `req()` called `fetch()` with no deadline, so a vanished server (silent packet drop) makes taps hang and keeps
  the heartbeat from ever failing, which is what shows the "server down" screen. Every request now has a deadline (6 s, heartbeat
  3 s). With the fix: a tap while the server is down fails clearly after about 2 s and the red "server down" screen appears
  about 4-5 s after that. The 2 s result means the laptop refused the connection, so the new 6 s timeout itself was not exercised;
  that needs a Pi that is actually unplugged.
- **Bug found and fixed: first tap after a server restart failed.** The browser's first request went down a dead keep-alive
  connection and failed, so the first tap needed a second one. `start()` now retries once after 400 ms (tune and off are
  idempotent). Re-test: one tap worked.
- The recovery from the red screen needs no reload.

**Decision:** the phone is the control surface for now (always at hand, no kiosk setup). The Pi / systemd, a dedicated tablet in
Fully Kiosk Browser and a Shelly button are deferred, not dropped. While the server runs on a laptop, that laptop has to stay on
and awake and keep the same LAN address (reserve it in the router).

**Not tested:** the Raspberry Pi (systemd unit, boot without login, unplug and replug), a real Shelly button (only curl against
the webhook), Fully Kiosk Browser specifically, and a true silent-drop network failure.

## Phase 10: real-world tests (in progress; phone as the control surface)

| Test | Result |
|---|---|
| Nani's own remote (Sony remote to YouTube, then tap 725 on the phone) | **PASS**: back on Airtel and on 725 in a couple of seconds (server: job done in 6.1 s, no failure), no odd flashes beyond the expected ones |
| Wrong channel by hand (Airtel remote, then tap a card), both directions | **PASS**: landed on the tapped channel first time |
| Rapid taps | Covered by the HTTP tests in Phase 9 (same card joins one job; a different card wins after the old entry commits). The phone page also blocks a second tap while a job is showing |
| Morning cold start | Not yet: needs the TV off all night (also covers the 30-minute cold trial) |
| Power cut (mains off for the whole setup, back on after 1 minute) | Not yet; with the server on a laptop this tests the laptop restarting the server by itself, which it will not unless it is set up to |
| Router reboot | Not yet; check that no device changes IP (reserve TV, box and laptop in the router) |
| Watch Nani (no explanation beyond "press the picture") | Not yet; needs the real channel names and logos on the cards first |

### Kiosk page restyle (separate session) and logos

- Real logos now sit on the cards (`logo = ...` in the gitignored `config.toml`, files in the gitignored `logos/`): News18 Bangla
  as an SVG (about 2.5 KB, inspected: paths only, no scripts) and Zee 24 Ghanta as a PNG trimmed from 6667x4167 to 875x307
  (36 KB; the original was 258 KB and 28 megapixels, heavy to decode on a phone). The card for 723 is named "Zee 24 Ghanta".
- A separate polish session restyled the page (CSS and markup only, no logic): inline SVG icons instead of emoji, softer card
  edges, a visible focus ring, `text-wrap: balance`, `prefers-reduced-motion`, ARIA labels, and `role="alert"` on the
  server-down screen. Reviewed on a phone-sized view: text contrast 6.6:1 to 16.9:1 (AA needs 4.5:1), bar buttons 110 px
  tall, cards 343 x 323 px, no horizontal scroll, both logos load. The cold-start overlay settles to a solid screen after a
  0.35 s fade (the fade briefly shows the cards through it).
- Decision: the restyle had dropped `maximum-scale=1, user-scalable=no` from the viewport meta, which let a pinch gesture zoom
  the page. An accidental pinch could leave Nani with a zoomed page and no obvious way back, so the lock is restored.
  (The phone's own accessibility zoom can still override it.)

## Edge cases register (revisit later)

Things seen or suspected during testing that are not blocking the happy path. Status: **fixed**, **open**, or **watch**.

| # | Edge case | Status | Notes |
|---|---|---|---|
| 1 | Box left on a Settings screen; Airtel launch request is ignored | fixed (partly verified) | Home, then launch. Re-test the full `goto_live` from Settings |
| 2 | Airtel app cold-starts after launch (loading screen seen); foreground flips to `com.airtel.tv` immediately but the UI may still be loading | open | `live_settle_s = 3` may be too short; digits could land on the loading screen. Measure the load time, then tune `live_settle_s` |
| 3 | Fresh connection reports no foreground app for the first moments | fixed | `goto_live` waits up to 4 s |
| 4 | Box ignores `KEYCODE_SLEEP` (223) and `KEYCODE_WAKEUP` (224) | fixed | Use `KEYCODE_POWER` (26) as a verified toggle; see Phase 8 |
| 5 | Does the box accept connections and a wake key while asleep? | fixed | Yes: the Remote service stays up in standby and POWER wakes it in about 1-2 s |
| 6 | TV wakes onto whatever screen it was last on (once a Developer options screen) before the HDMI switch | watch | Harmless if `setPlayContent` always follows; seen once |
| 7 | Launch request makes the box send an "error from the device" log line even when the launch works | watch | Cosmetic; consider silencing the library logger |
| 8 | Menu escape only tested for the Airtel settings screen | open | Guide, search box and deeper menus untested |
| 9 | Pairing survival across a box reboot or power cut | open | Phase 10 |
| 10 | Job takes up to the full TV cold settle (25 s) to report failure even when the box failed at about 16 s | watch | Consider failing fast, or showing the box error sooner |
| 11 | HDMI 2 is labelled "TV box" and shows connected; unknown device | watch | Not used; confirm what it is |
| 12 | Cold-start picture time after a long standby | open | Planned 30-minute trial (`latency --trials 1 --cool 1800 --manual`) |
| 13 | Mismatch case 1 (TV on / box on) failed once in the full run, passed alone | watch | Possible TV no-signal auto-standby after the box was asleep; re-run the full mismatch to see if it recurs |
| 14 | Box ignores POWER for a few seconds after a power change | watch | Driver waits 15 s between power changes; the real window is between 1 s and 22 s and could be narrowed |
| 15 | Countdown starts at the cold estimate (about 22 s) and drops when the TV turns out to be on; on cold wakes it can sit on "1" for several seconds while the settle finishes | watch | Improves with the learned timings; consider ending the countdown on the picture estimate |
| 16 | `/api/health` shows `connected: false` on the first call even though `on` is true (the key is built before the first `is_on` opens the connection) | open | Cosmetic; reorder in `AtvRemoteStb.health` |
| 17 | Server-down screen appears after about 18 s, not about 10 s | watch | 3 s heartbeat, 3 misses, plus request timeouts |
| 18 | Placeholder help-line phone number and placeholder card names (channel numbers) | open | Fill in the real number and channel names |
| 19 | `hdmi_settle_s = 25` was longer than the picture needed on one cold wake (picture at about 21 s, job end 30.7 s) | watch | Wait for the 30-minute cold trial and more samples before shortening |
| 20 | Pi / systemd, real tablet in Fully Kiosk, real Shelly button, unplugging the Pi | open | Phase 9 on real hardware |
| 21 | Picture appears quickly after a short standby (countdown still near 20) but the fixed 25 s cold settle keeps counting | open | Adaptive settle idea: scale `hdmi_settle_s` by how long the TV was in standby |
| 22 | Page request timeout never exercised against a silently dropped network | open | Needs the Pi unplug test |
| 23 | For a few seconds after the TV is turned off, its API answers 404 on `/sony/system`; `/api/health` then shows an error instead of `standby` | watch | A cold tap during that window still succeeded (27.2 s), so the job tolerates it. Cosmetic in health |
| 24 | Restyle dropped the pinch-zoom lock from the viewport meta | fixed | Lock restored (`maximum-scale=1, user-scalable=no`); OS-level accessibility zoom still works |

**Still to do**
1. Phase 7 cold trial (`--cool 1800`, planned for the next morning), finish Phase 8, Phase 9 server/tablet/buttons, Phase 10 real-world tests.
2. Wake the box from real standby with the `atv_remote` driver (does the Remote service still accept connections while asleep?).
3. Check pairing survives a box reboot and a power cut (Phase 10).
4. Record real channel numbers for the favourites (the config still has placeholders; 723 and 725 are known real channels).

## Decision

Box control path: **Android TV Remote protocol** (CEC failed, ADB blocked). An IR blaster (ESP32 / Broadlink RM4 Mini) remains the fallback if the pairing proves unreliable across reboots.

Note: the example channel numbers (105, 210, 118, 600) are placeholders; real Aaj Tak/Star Plus/DD National/Aastha numbers on this box are still to be recorded (Phase 0 step 4).
