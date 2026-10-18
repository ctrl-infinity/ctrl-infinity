# Test log: real devices

Running record of what works and what doesn't while following [TESTING.md](TESTING.md).
Newest findings go at the bottom of each phase. The PSK is deliberately not recorded here.

## Setup under test

| Item | Value |
|---|---|
| TV | Sony KD-49X8000H (2020 Android TV), `192.168.1.8` |
| Box | Airtel Xstream (Android), `192.168.1.5`, plugged into HDMI 1 |
| Test machine | Windows 11 laptop `192.168.1.14`, venv at `E:\ctrl-infinity\.tvenv`, adb at `E:\ctrl-infinity\platform-tools\adb.exe` |
| Server / tablet / blaster | not yet set up |

## Status at a glance

| Phase | Result |
|---|---|
| 0 Preparation | Done (box type, IPs, venv, config) |
| 1 TV settings + raw API | **PASS** |
| 2 `diagnose.py psk` / `inputs` | **PASS** |
| 3 CEC test | **FAIL**: digits arrive but box commits after 2 digits, whatever the timing |
| 4A Xstream ADB | **BLOCKED**: network ADB unavailable on this firmware |
| 4C Android TV Remote (6466/6467) | Open ports found; driver not written yet |
| 4B IR blaster | Not started (fallback) |
| 5 Digit timing | Waiting on a working box driver |
| 6-10 | Not started |

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

## Decision pending

Box control path, after CEC and ADB failed:
1. **Android TV Remote protocol (ports 6466/6467)**: no hardware; the box is reachable and the port is open. Needs a new driver in `launcher.py` (e.g. the `androidtvremote2` library) plus a one-time pairing code shown on the TV. Untested. Open question: does it send 3 digits without the 2-digit commit?
2. **IR blaster** (ESP32 recommended, Broadlink RM4 Mini alternative). Most robust and uses the same keys as Nani's remote, but needs hardware and a clear IR window on the box.

Note: the example channel numbers (105, 210, 118, 600) are placeholders; real Aaj Tak/Star Plus/DD National/Aastha numbers on this box are still to be recorded (Phase 0 step 4).
