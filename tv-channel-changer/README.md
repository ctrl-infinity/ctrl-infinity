# One-tap TV for Nani

Tap a big channel card on an old tablet, or press a smart button. The Sony Bravia wakes up, jumps
straight to the Airtel box's HDMI port, and the box tunes to the channel. It all runs on the LAN.
Nothing needs the internet.

```
 tablet (kiosk PWA) ─┐                       ┌─ Sony Bravia   REST /sony/* (X-Auth-PSK)
 smart button ───────┼─ HTTP ─► launcher.py ─┤
                     │   (Pi / old laptop)    └─ Airtel STB    ADB (Xstream)  |  IR via ESP32/Broadlink  |  CEC via TV
```

| File | What it is |
|---|---|
| `launcher.py` | **Single-file backend**: Bravia client, STB drivers, state machine, FastAPI API, embedded PWA, button webhook |
| `diagnose.py` | Verification suite you run from your laptop |
| `learn_ir.py` | Captures your Airtel remote's codes into `ir_codes.json` (Broadlink or ESP32) |
| `esp32_ir_blaster/esp32_ir_blaster.ino` | HTTP IR blaster firmware |
| `fake_devices.py` + `fake_config.toml` | Simulated TV + box + plug, so you can work without the real TV |
| `tvlauncher.service` | systemd unit (expects this folder copied to `/opt/tvlauncher`) |
| `TESTING.md` | Step-by-step test plan for the real TV, box and tablet |

## Read this first: where the plan is weak

1. **A classic DTH box gives no feedback.** Over IR you can't read back the channel or the power
   state. "Deterministic" only holds where the code can *measure* something. This design
   measures everything it can: TV power, TV input, Xstream wakefulness and foreground app, STB power
   draw through a Tasmota plug. Every step it can't measure is made idempotent instead: EXIT
   before the digits clears menus and half-typed numbers, and re-sending a channel number is
   harmless. The one dangerous key is the STB's **power toggle**. It is never pressed unless a plug
   reading proves the box is off.
2. **The power toggle is the real desync source, and auto-standby is what causes it.** Many DTH
   boxes go into standby by themselves after a few hours without a key press. Then the "always on"
   assumption silently breaks. Either turn auto-standby off in the box's settings (if your firmware
   allows it), or buy a **Tasmota energy-monitoring plug** (about ₹800–1,200) and set
   `power_strategy = "tasmota_sense"`. Don't try to infer box power from the TV's HDMI `connection`
   flag. Most boxes keep HDMI hot-plug asserted in standby, so that flag tells you nothing.
3. **Nani will still use the original remotes.** That's fine: every job starts by reading TV state
   and forces the input. But if she switches the box off with the Airtel remote, only the plug can
   detect it. Put tape over that remote's power button. That's not a joke.
4. **Use an IR LED taped over the box's IR window, not a room blaster.** RM4-style blasters depend
   on bounces and aim. Boxes often sit behind or under the TV, and the IR gets missed. An ESP32 with
   a wired LED stuck 1–2 cm from the receiver doesn't miss, and it can't spill codes into anything
   else. Recommendation: **ESP32 + taped LED** for a classic box. Use the Broadlink only if you
   already own one.
5. **CEC pass-through probably won't work with a classic Airtel box.** Most classic DTH boxes don't
   implement CEC remote keys. `diagnose.py cec` settles it in 30 seconds. Don't build on it until
   that test says yes.
6. **Fixed IPs or nothing.** Give the TV, box, blaster, plug, server and tablet DHCP reservations.
   A router reboot that reshuffles addresses is the #1 way this "stops working for no reason".
7. **The PWA won't install over plain `http://192.168.x.x`.** Service workers and Wake Lock need a
   secure context. Options: (a) use **Fully Kiosk Browser** (the simplest, and it keeps the screen on);
   (b) in Chrome on the tablet, add the server URL under `chrome://flags/#unsafely-treat-insecure-origin-as-secure`,
   then "Add to Home screen"; (c) put a local TLS cert on it. The page works fine without the SW.
   The SW only adds an offline "server down, call X" screen.
8. **Old tablet + 24×7 charging = swollen battery.** Turn on the battery protection limit (e.g.
   Samsung "Protect battery", 85%), or put the charger on a smart-plug schedule.
9. **Airtel renumbers channels** occasionally. Re-check the numbers in `config.toml` every few
   months, and keep logos on the cards: she'll recognise logos faster than numbers.
10. **Things this can't fix:** an expired recharge screen, a "no signal" rain fade, or the router
    being down. The UI shows a clear failure and tells her who to call (`help_text`).

## How a tap works (the state machine)

```
tap / button ─► job
   ├─ TV path (Bravia REST) ───────────────────────────────┐   run in PARALLEL for ADB/IR:
   │  getPowerStatus ─ standby? ─► setPowerStatus(true)    │   the box tunes whether or not
   │     (re-asserted every 5 s; WoL if the NIC is asleep) │   the TV is showing it, so total
   │  getPlayingContentInfo != hdmi?port=N                 │   time = max(TV boot, STB tune)
   │     ─► setPlayContent(extInput:hdmi?port=N) ─► verify  │
   │  settle hdmi_settle_s (cold) / hdmi_switch_settle_s   │
   └─ STB path ────────────────────────────────────────────┘
      ensure_on   adb: dumpsys power ─► KEYCODE_WAKEUP (discrete)
                  plug: watts < threshold ─► POWER once ─► verify watts rose ─► boot_s
      home bypass adb: foreground != live_component ─► am start -n <live_component> ─► verify focus
                  IR:  EXIT, EXIT  (closes guide/menu/banner, clears half-typed digits)
      digits      start-to-start paced at inter_digit_ms (1 ─ 0 ─ 5), then commit_wait_ms
```

- **Latest request wins.** A new tap cancels the running job and starts a new one. That's safe
  because each job re-reads state and the EXIT keys wipe partial digit entry.
- **Same request twice** (double tap, button bounce) joins the running job. It's a no-op for
  `debounce_s` after success.
- TV power uses only `setPowerStatus(true/false)`. Those calls are discrete, never toggles, so
  repeating them is safe.

## Setup

### 0. Server
Any always-on Linux box on the LAN: a Raspberry Pi 3/4/5, an old laptop, or an old Android phone
running Termux.
```bash
git clone https://github.com/ctrl-infinity/ctrl-infinity && cd ctrl-infinity/tv-channel-changer
sudo apt install python3-venv android-tools-adb      # adb only for the Xstream pathway
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp config.example.toml config.toml                   # edit IPs, PSK, HDMI port, channels
```
Python 3.11+ (uses `tomllib`).

### 1. Sony Bravia (Google TV / Android TV)
Menu names vary a little by model year:
- Settings → Network & Internet → Home network → **IP control** → Authentication = **Normal and
  Pre-Shared Key**, then set the **Pre-Shared Key** (= `[tv] psk`).
- Settings → Network & Internet → **Remote start = On**. Without it the network card sleeps in
  standby and nothing can wake the TV. It costs a little standby power.
- Settings → Power/Eco → **Idle TV standby = Off**. Otherwise the TV turns itself off during a long
  serial.
- Run `python diagnose.py psk`. It also prints the MAC address for `[tv] mac` (Wake-on-LAN fallback).

The exact calls are in `launcher.py` → `class Bravia`:
`POST /sony/system getPowerStatus`, `setPowerStatus {"status": true}`,
`POST /sony/avContent setPlayContent {"uri": "extInput:hdmi?port=N"}`, verified with
`getPlayingContentInfo` (error 7 "Illegal State" means an app or the home screen is in front).

### 2a. Airtel Xstream box (Android TV): ADB over LAN
1. Box: Settings → Device Preferences → About → click **Build** 7×. Then Developer options →
   **USB debugging = On** (and **Network debugging**, if your firmware has it).
2. Laptop/Pi: `adb connect <box-ip>:5555` → accept **"Always allow"** on the TV screen.
   If the connection is refused, your firmware doesn't listen on TCP. Some Airtel builds lock this
   down. Use the IR pathway instead of fighting it.
3. Open Airtel live TV on the box, then run `python diagnose.py stb`. Copy the printed
   `pkg/.Activity` into `[stb.adb] live_component`. That is the home-screen bypass: the launcher
   brings that activity to the front (checked via `dumpsys window`) before sending digits.
4. Keyevents: `KEYCODE_0..9 = 7..16`, wake = `224` (KEYCODE_WAKEUP, discrete), sleep = `223`.
   All the digits go in **one** `adb shell` call (`input keyevent 9; sleep 0.45; input keyevent 7; …`)
   so Wi-Fi jitter can't stretch the gaps. Each `input` call starts a JVM (~0.2–0.6 s on a weak
   box). `diagnose.py stb` measures that cost.
5. Keep **"Device auto power off"** (BRAVIA Sync) on: the box sleeps with the TV, and the launcher
   wakes it with a discrete keycode.

### 2b. Classic Airtel DTH box: IR blaster
**ESP32 (recommended):** flash `esp32_ir_blaster/esp32_ir_blaster.ino` (Arduino IDE, IRremoteESP8266,
ESP32 core **2.0.17**). Set the Wi-Fi and fixed-IP values at the top. Wiring is in the file header.
```bash
python learn_ir.py esp32 --url http://192.168.1.41          # each key is captured twice and must match
python learn_ir.py esp32 --url http://192.168.1.41 --test 1 # box shows "1 _ _"
```
**Broadlink RM4 Mini:** provision it onto Wi-Fi *without* the Broadlink app (newer firmware locks
local control once the device is added to the cloud app):
```bash
python learn_ir.py broadlink --setup-wifi "SSID" "PASSWORD"  # laptop joined to the RM4's AP
python learn_ir.py broadlink --host 192.168.1.40
```
**Timing.** DTH boxes collect digits until a ~2–3 s timeout. Digits sent closer together than the
IR frame plus the box's debounce get dropped ("105" becomes "15"). The launcher paces digits
**start-to-start**, so transport latency doesn't shift them. Calibrate:
```bash
python diagnose.py digits --channels 105 210 --gaps 200 300 450 650
```
It recommends `inter_digit_ms` (1.5× the smallest reliable gap). Keep `commit_wait_ms` longer
than the time the box's number banner stays on screen. Set `commit_with_ok = true` only if your box
tunes immediately on OK. On some boxes OK opens the channel list instead, and then the job isn't
idempotent any more.

### 2c. No hardware: CEC through the TV
`python diagnose.py cec --channel 105` sends Bravia IRCC `Num1/Num0/Num5` while the TV is on the
box's HDMI port, and asks whether the box changed channel. If it did, set `driver = "tv_cec"`.
It runs sequentially, because keys only reach the box while the TV is on that input. STB power
isn't available on this path, because IRCC `Power` turns off the *TV*.

### 3. Power desync prevention (classic box)
| Strategy | How | Recovers if the box was switched off? |
|---|---|---|
| `always_on` (default) | Never send POWER. Disable the box's auto-standby, tape the remote's power key | **No.** `diagnose.py mismatch` will show it |
| `tasmota_sense` | Tasmota plug reads watts (standby ~1–3 W, on ~6–12 W). POWER is pressed **only** when the reading says standby, then the reading must rise within 10 s or the job fails without toggling again | **Yes** |

With `tasmota_sense` the plug's relay is also checked: if someone turned the plug off, the launcher
turns it on and waits `cold_boot_s`. Set `on_threshold_w` halfway between the box's measured
standby and on wattage (`diagnose.py stb` prints the current reading).

### 4. The tablet (kiosk)
- Open `http://<server-ip>:8000/` in **Fully Kiosk Browser** (start URL, keep screen on, launch on boot),
  or use Chrome + "Add to Home screen" + Android **screen pinning** (Settings → Security → App pinning).
- The cards are large and high-contrast, with Hindi labels, haptic feedback and no confirm dialogs.
  After a tap, a full-screen countdown shows the channel colour, then ✓ or "फिर से दबाएँ".
- The countdown is an estimate. It learns real TV wake times (EWMA in `timings.json`), so it gets
  more accurate after a few days.
- Logos: put PNGs in `./logos/` and set `logo = "aajtak.png"` on a channel.

### 5. Smart buttons
`GET|POST /api/button/<id>?press=single|double|long[&token=…]` returns 202 immediately.
Shelly Button1: Actions → "short press" URL `http://<server>:8000/api/button/b1?press=single&token=…`.
Map buttons in `[buttons]`. Values can be a channel number, `off`, `vol_up` or `vol_down`.
The tablet shows the countdown for button-started jobs too.
About `webhook_token`: the UI itself is unauthenticated on the LAN (anyone on your Wi-Fi can
already change the channel), so the token only stops accidental hits. It isn't real security.
Keep the server off the internet.

### 6. Run it
```bash
.venv/bin/python launcher.py                 # or install tvlauncher.service
curl -s localhost:8000/api/health | python -m json.tool
```

## Verification suite (`diagnose.py`)
For the full hands-on test plan with real devices, see [TESTING.md](TESTING.md). Run these in order, from your laptop, sitting in front of the TV:

| Command | Proves |
|---|---|
| `psk` | REST reachable; correct PSK accepted on a write; **a wrong PSK is rejected** (catches auth = None); IRCC digit keys exist; prints the MAC |
| `inputs` | target HDMI port exists; shows CEC device names; reminds you `connection` ≠ power |
| `stb` | driver reachable; learned keys; ADB foreground app + JVM cost; plug relay/watts |
| `cec --channel 105` | whether the TV forwards IRCC digits to the box over HDMI-CEC |
| `latency --trials 3 --cool 30 --manual` | standby → `active` → `setPlayContent` accepted → input verified → **picture visible** (you press Enter); recommends `hdmi_settle_s` and `wake_timeout_s`; also times the warm Google TV home → HDMI switch and recommends `hdmi_switch_settle_s` |
| `digits` | inter-digit calibration |
| `mismatch --channel 105` | runs the real job from all 4 combinations of TV on/off × STB on/off (starting on the wrong input), plus "box stuck in a menu"; checks TV power, input, measured STB power and your eyes |
| `all --channel 105` | the lot |

`--auto` skips the questions (results show as unverified). That's useful against the simulator:
```bash
python fake_devices.py &                          # TV + ESP32 + box + Tasmota on 127.0.0.1:8081
LAUNCHER_CONFIG=fake_config.toml python launcher.py
LAUNCHER_CONFIG=fake_config.toml python diagnose.py mismatch --auto --channel 105
```
The simulator drops digits sent closer than 120 ms together, so you can watch `105` become `15`.

## Troubleshooting
| Symptom | Likely cause |
|---|---|
| `TvUnreachable` only when the TV is off | Remote start off, or the TV dropped to deep standby. Set `[tv] mac` for WoL |
| `PSK rejected` | Auth mode isn't "Normal and Pre-Shared Key", or the key has a typo |
| TV on, wrong input | `hdmi_port` wrong: `diagnose.py inputs` |
| Box shows "1 5" instead of 105 | `inter_digit_ms` too low, or the blaster is weak or badly aimed |
| Box shows menu / channel list after tuning | `commit_with_ok = true` on a box that doesn't want it |
| Countdown way off | Run `diagnose.py latency`; it seeds `timings.json` |
| Works for hours then "nothing happens" | Box auto-standby with `always_on`: switch to `tasmota_sense` |
