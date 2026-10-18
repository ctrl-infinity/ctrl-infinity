# Testing with real devices

The order tests one layer at a time, cheapest first: TV, then the box, then timing, then
recovery, then the tablet. Don't start by tapping the tablet. When that fails you won't know
which of five layers broke.

All commands run from this folder (`ctrl-infinity/tv-channel-changer`) on a laptop on the same
Wi-Fi as the TV.

---

## Phase 0: Preparation (30 min)

1. **Find out which box you have.** If its home screen is an Android/Google-style launcher with
   apps, it's an **Xstream box**. If it's the blue Airtel menu with no apps, it's a **classic box**.
   This decides Phase 4.
2. **Reserve fixed IPs in the router** (DHCP reservation by MAC) for the TV, the box (Xstream only),
   the ESP32 or Broadlink, the Tasmota plug, the server (Pi/laptop) and the tablet. Write them down.
3. **Laptop setup:**
   ```bash
   python3 --version                      # must be 3.11 or newer
   python3 -m venv .venv && . .venv/bin/activate
   pip install -r requirements.txt
   cp config.example.toml config.toml
   ```
   For now, fill in only `[tv] ip`, `psk` and `hdmi_port`, plus `[stb] driver`.
4. **Write down the box's real numbers for 3–4 channels** by changing channels with the Airtel
   remote. Don't trust the numbers in the example config.

## Phase 1: TV settings and a raw API check (15 min)

1. On the TV, set the following. Menu names vary slightly by model year.
   - Settings → Network & Internet → Home network → IP control → **Authentication = Normal and
     Pre-Shared Key**, then set the **Pre-Shared Key** (for example `4821`).
   - Settings → Network & Internet → **Remote start = On**.
   - Power/Eco settings → **Idle TV standby = Off**.
   - Channels & Inputs → External inputs → BRAVIA Sync → **BRAVIA Sync control = On** (only
     matters for the CEC test).
2. **Test with plain curl before any of this code runs**, to isolate the TV:
   ```bash
   TV=192.168.1.20; PSK=4821
   curl -s -H "X-Auth-PSK: $PSK" -d '{"method":"getPowerStatus","id":1,"params":[],"version":"1.0"}' http://$TV/sony/system
   # expected: {"result":[{"status":"active"}],"id":1}
   ```
3. **Put the TV in standby with its remote. Wait 2 minutes, then run the same curl again.**
   - **Pass:** `{"status":"standby"}`.
   - **Fail (connection refused or timeout):** Remote start isn't really on, or the TV drops into
     deep standby. Nothing can wake it over the network, so fix this before continuing.
4. Wake the TV and switch its input:
   ```bash
   curl -s -H "X-Auth-PSK: $PSK" -d '{"method":"setPowerStatus","id":2,"params":[{"status":true}],"version":"1.0"}' http://$TV/sony/system
   curl -s -H "X-Auth-PSK: $PSK" -d '{"method":"setPlayContent","id":3,"params":[{"uri":"extInput:hdmi?port=2"}],"version":"1.0"}' http://$TV/sony/avContent
   ```
   **Pass:** the TV turns on and lands directly on the box's HDMI port, without cycling through
   inputs.

## Phase 2: TV through the diagnostic script (10 min)

```bash
python diagnose.py psk
python diagnose.py inputs
```
- **`psk`**
  - All lines should be **PASS**.
  - If it says *"a WRONG PSK was accepted"*, authentication is effectively off. Go back to
    Phase 1, step 1.
  - Copy the printed MAC into `[tv] mac`.
- **`inputs`**
  - The `<== target` marker must sit on the port the box is plugged into. If not, fix
    `hdmi_port`.
  - Note whether the label/title for that port shows a device name. That is a hint for Phase 3.

## Phase 3: CEC test (5 min; it may save you buying hardware)

```bash
python diagnose.py cec --channel 105
```
Note the current channel, press Enter, watch the TV, then answer y/n.
- **Yes:** set `driver = "tv_cec"`, keep `power_strategy = "always_on"` and skip Phase 4.
- **No (expected for a classic box):** continue to Phase 4.

## Phase 4: Set-top box pathway

### 4A: Xstream box (ADB)
1. On the box: Settings → Device Preferences → About → click **Build** 7 times. Then Developer
   options → **USB debugging On**, and **Network debugging On** if that option exists.
2. On the laptop: install adb (`sudo apt install adb`, or `brew install android-platform-tools`),
   then:
   ```bash
   adb connect 192.168.1.30:5555
   ```
   The TV shows "Allow debugging?". Tick **Always allow**, then OK.
   - **Fail ("connection refused"):** this firmware doesn't support network ADB. Stop here and use
     4B with an IR blaster. Don't waste an evening on this.
3. Test a key by hand while live TV is showing:
   `adb -s 192.168.1.30:5555 shell input keyevent 8`. Expected: the box shows "1".
4. Put `host` in `[stb.adb]` and open Airtel **live TV** on the box with its remote, then run:
   ```bash
   python diagnose.py stb
   ```
   Copy the `pkg/.Activity` from the `foreground:` line into `live_component`. Also note the
   "`input keyevent` round-trip" time.
5. Press Home on the box remote and run `diagnose.py stb` again. The foreground app must now be the
   launcher. This proves the home-screen bypass has something to fix.

### 4C: Xstream box without ADB (Android TV Remote protocol)
Use this when `adb connect` is refused (Airtel often locks network ADB) or `diagnose.py cec` shows digits
being committed after two. It uses the protocol of the Google TV phone app: no hardware, no developer options.
1. Check the ports are open: `Test-NetConnection <box-ip> -Port 6466` (and 6467) should say `True`.
2. `pip install -r requirements.txt` (adds `androidtvremote2`), then set `driver = "atv_remote"` and
   `[stb.atv_remote] host = "<box-ip>"`.
3. `python diagnose.py pair`: the TV shows a code; type it in. This writes `atv_cert.pem` and `atv_key.pem`
   next to `config.toml`. Keep them private and copy them to the Pi with the config.
4. Put the box on Airtel live TV and run `python diagnose.py stb`. Expect PASS for connection and power
   state; copy the foreground package (for example `com.airtel.tv`) into `live_app`.
5. Continue with Phase 5 (digit timing).

### 4B: Classic box with ESP32 (recommended)
1. Arduino IDE setup: Boards Manager → esp32 by Espressif, **version 2.0.17** exactly. Library
   Manager → **IRremoteESP8266**.
2. Edit the Wi-Fi name, password and IP at the top of `esp32_ir_blaster/esp32_ir_blaster.ino`,
   then flash it. Open the Serial Monitor at 115200 baud. Expected:
   `IR blaster at http://192.168.1.41`.
   - The sketch hasn't been compiled against real hardware yet. If it fails to compile, check the
     ESP32 core version first.
3. Run `curl http://192.168.1.41/health` and expect `{"ok":true,...}`.
4. Wire the receiver (TSOP38238 or VS1838B on GPIO14), then learn the keys:
   ```bash
   python learn_ir.py esp32 --url http://192.168.1.41
   ```
   - Press each key **twice** when asked, from about 10 cm away.
   - If it says "captures differ", aim better and retry that key with `--keys 5`.
   - Note the protocol it prints (probably NEC).
5. Mount the IR LED 1–2 cm in front of the box's IR window. Find the window by shining a phone
   torch at the box's front panel; it's the dark spot. Then:
   ```bash
   python learn_ir.py esp32 --url http://192.168.1.41 --test 1
   ```
   **Pass:** the box shows "1 _ _". Test `--test exit` and `--test power` too. Power should toggle
   the box: press it twice to restore.
6. Set `driver = "esp32"` and `base_url`, then run `python diagnose.py stb` and expect PASS.

### 4B-alt: Broadlink RM4 Mini
1. **Do not add it to the Broadlink app.** Newer firmware locks local control after that.
2. Factory reset it (hold reset until it blinks fast). Join your laptop to its `BroadlinkProv`
   Wi-Fi, then:
   ```bash
   python learn_ir.py broadlink --setup-wifi "YourSSID" "YourPassword"
   ```
3. Rejoin your normal Wi-Fi, find the RM4's IP in the router and reserve it. Then:
   ```bash
   python learn_ir.py broadlink --host 192.168.1.40
   ```
4. Run `--test 1` from where the RM4 will actually sit. **If it misses even once in 10 tries, move
   it or switch to the ESP32.**

## Phase 5: Digit timing (15 min)

Put the TV and box on, showing Airtel, then run:
```bash
python diagnose.py digits --channels 105 210 --gaps 150 250 350 450 650
```
1. Answer honestly for each gap. If it lands on "15", that's a fail.
2. Put the recommended value into `inter_digit_ms`.
3. Set `commit_wait_ms`: press "1" on the Airtel remote and time with your phone stopwatch how long
   the "1 _ _" banner stays before it gives up. Set `commit_wait_ms` to that time plus 500.
4. Re-run with only the recommended gap, 5 times in a row. **Pass: 5 out of 5.** 4 out of 5 is a
   fail for a grandmother's system.
5. Menu escape: open the Airtel guide or menu with the remote, then run the digits test again. If
   it doesn't escape, add a third `"exit"` (or `"back"`) to `pre_tune_keys`.

## Phase 6: Box power-state detection (classic box only)

1. **Check whether the box sleeps by itself.** Leave it on with no remote presses for 4–5 hours
   (a weekend afternoon). If it showed an "auto standby" warning or went to sleep, `always_on` is
   unsafe. Look in the box settings for an auto-standby or energy option to turn off.
2. **If you use a Tasmota plug:**
   - Plug the box into it, put `tasmota_url` in the config and leave `on_threshold_w` at its
     default for now.
   - Leave the Tasmota web password unset; the launcher doesn't send credentials.
   - Run `python diagnose.py stb` with the box ON and note the watts. Put it in standby with the
     remote and run it again.
   - Set `on_threshold_w` halfway between the two (e.g. 2 W and 9 W → 5.5).
   - Set `power_strategy = "tasmota_sense"`.
   - If the two readings are within about 2 W of each other, this method isn't reliable for your
     box.

## Phase 7: Latency (20 min; the TV switches on and off repeatedly)

```bash
python diagnose.py latency --trials 3 --cool 60 --manual
```
Press Enter **the instant the Airtel picture appears**.
- Put the suggested `hdmi_settle_s` into the config, and raise `wake_timeout_s` if it warns.
- Then run one trial with `--cool 1800`, i.e. 30 minutes of standby. A fully cold TV can take much
  longer than one that just went to standby, and that is what Nani will hit every morning.

## Phase 8: Recovery from mismatched states (20 min)

```bash
python diagnose.py mismatch --channel 105
```
It runs the real job from all four combinations of TV on/off × box on/off, plus "box stuck in a
menu". It asks you to confirm by eye each time.
- **ADB or `tasmota_sense`:** all 5 must PASS.
- **`always_on`:** the two "STB off" cases will FAIL. That is the honest result, not a bug. Decide
  whether you accept that risk.

## Phase 9: Server, then tablet, then buttons

1. **Server.** On the Pi or always-on machine:
   ```bash
   python launcher.py
   curl -s localhost:8000/api/health | python3 -m json.tool
   curl -s -XPOST localhost:8000/api/tune/105
   curl -s -XPOST localhost:8000/api/off
   ```
   - **Xstream:** the Pi has a different ADB key from your laptop, so run `adb connect` once from
     the Pi and accept "Always allow" on the TV again.
   - Copy `ir_codes.json` and `config.toml` to the Pi.
   - Install the systemd unit (`tvlauncher.service`), reboot the Pi, and confirm that
     `/api/health` responds without you logging in.
2. **Tablet.** In Fully Kiosk Browser, open `http://<pi-ip>:8000/`.
   - Tap each card from TV off, then from TV on. Check the countdown and that ✓ appears.
   - Tap "TV बंद" (TV off).
   - Unplug the Pi: within about 10 seconds the red "server down" screen with your phone number
     should appear. Plug it back in and it should recover.
3. **Buttons** (e.g. Shelly Button1): set its action URL to
   `http://<pi-ip>:8000/api/button/b1?press=single`.
   - Press the button with the tablet on. The countdown should appear on the tablet too.
   - Press the button twice quickly. It should start only one job; check the log with
     `journalctl -u tvlauncher`.

## Phase 10: Real-world tests (over 2–3 days; this is what actually breaks)

| Test | How | Pass |
|---|---|---|
| Morning cold start | Use it first thing after the TV has been off all night | Correct channel; countdown reasonably close |
| **Power cut** (common in India) | Turn off the mains for the whole setup, then back on after 1 minute | After about 3 minutes, one tap works without you touching anything (the Pi restarts the server, the ESP32 reconnects, the box comes back up) |
| Router reboot | Restart the router | All IPs unchanged; one tap works |
| Nani's own remote | Use the Sony remote to switch to YouTube, then tap a card | Back on Airtel, correct channel |
| Wrong channel by hand | Change channel with the Airtel remote, then tap a card | Correct channel |
| Rapid taps | Tap a card, then tap the same card again 2 seconds later | One job; no garbled digits like "1055" |
| Watch Nani | Give it to her with **no explanation** beyond "press the picture" | She succeeds without asking you |

The last row is the real acceptance test. If she hesitates, the problem is the UI, not the code:
card labels, logos, too many cards.

## When something fails

Run the server with `LOG_LEVEL=DEBUG python launcher.py` and keep:
- the failing `diagnose.py` output;
- the log lines around it;
- the TV model, box type and driver.

The Bravia API differs a little between model years, especially error codes during boot, and
this code has only been tested against `fake_devices.py`. Expect one or two fixes in Phases 2, 4
and 7.
