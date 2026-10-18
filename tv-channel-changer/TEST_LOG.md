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
| 8 Mismatched states | **In progress**: 1 of 5 cases passed; box sleep key has no effect, see Phase 8 |
| 9-10 | Not started |

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

## Phase 8: mismatched states (in progress)

`diagnose.py mismatch --channel 725`:
- **TV on / box on: PASS** (30.5 s; job, TV active, HDMI, box on, and eyes all OK).
- **TV on / box off: not testable yet.** The test's setup step calls `ensure_off()`, which sends `KEYCODE_SLEEP` (223)
  and waits up to 10 s for `is_on` to become False. It never did, so the run aborted with
  `StbError: Xstream box did not sleep via KEYCODE_SLEEP` before any tune was attempted.
- Follow-up probe: sending 223 alone and polling for 15 s left `is_on = True` and the connection up the whole time.
  What the screen did was not recorded. Open question: does the box ignore the sleep key, or does it sleep while
  `is_on` fails to track it?
- Candidate fixes to try: `KEYCODE_SOFT_SLEEP` (276), `KEYCODE_POWER` (26, a toggle, so only safe with a reliable
  state read), and checking what the box does on the Airtel remote's own power key. Also verify whether the box accepts
  Remote-protocol connections and a wake key while asleep.
- The remaining Phase 8 cases (TV on/box off, TV off/box on, TV off/box off, box stuck in a menu) are not run.
- Side finding (fixed): `diagnose.py mismatch` crashed when it could not force the box's power state. It now warns and
  asks you to set the box state with the Airtel remote, so the remaining cases can run.

**Still to do**
1. Phase 7 cold trial (`--cool 1800`, planned for the next morning), finish Phase 8, Phase 9 server/tablet/buttons, Phase 10 real-world tests.
2. Wake the box from real standby with the `atv_remote` driver (does the Remote service still accept connections while asleep?).
3. Check pairing survives a box reboot and a power cut (Phase 10).
4. Record real channel numbers for the favourites (the config still has placeholders; 723 and 725 are known real channels).

## Decision

Box control path: **Android TV Remote protocol** (CEC failed, ADB blocked). An IR blaster (ESP32 / Broadlink RM4 Mini) remains the fallback if the pairing proves unreliable across reboots.

Note: the example channel numbers (105, 210, 118, 600) are placeholders; real Aaj Tak/Star Plus/DD National/Aastha numbers on this box are still to be recorded (Phase 0 step 4).
