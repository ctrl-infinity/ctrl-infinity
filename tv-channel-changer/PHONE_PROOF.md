# Phone-only proof (before building an Android app)

**The question:** can a phone, with no laptop, Pi or server anywhere, wake the TV, switch to the box's
HDMI port and tune the Airtel box, reliably?

**How:** run the same tested Python code on the phone itself, in Termux (a free Linux terminal for
Android), and tap the same kiosk page in Chrome on that phone. `phone_proof.py` reuses
`launcher.py`'s Orchestrator, Bravia client, `atv_remote` driver, handlers and page. It only swaps
FastAPI for a small built-in web server, because FastAPI needs `pydantic-core`, a Rust build that
rarely succeeds on a phone. No Kotlin or Android Studio is needed.

Allow about an hour for setup and an evening of use.

## Setup (on the phone that will do the controlling)

1. **Install Termux from F-Droid** (or from the GitHub releases of `termux/termux-app`). Avoid the
   Play Store listing, which has lagged behind.
2. In Termux:
   ```bash
   pkg update -y && pkg upgrade -y
   pkg install -y python git clang python-cryptography
   git clone -b tv-channel-changer https://github.com/ctrl-infinity/ctrl-infinity
   cd ctrl-infinity/tv-channel-changer
   pip install -r requirements-phone.txt
   ```
   - `clang` is needed because `protobuf` compiles C code here. That takes a few minutes.
   - If `python-cryptography` isn't found, run `pkg install rust` and let pip build it. That's slow,
     possibly 20+ minutes.
   - If any install fails, keep the full error text.
3. **Copy your config and logos onto the phone.** Send `config.toml` and the files in `logos/` from
   the laptop to the phone's Download folder (USB, Drive, or a chat to yourself). Then:
   ```bash
   termux-setup-storage                 # allow Termux to read Download
   cp ~/storage/downloads/config.toml .
   mkdir -p logos && cp ~/storage/downloads/<logo files> logos/
   ```
   In `config.toml`, set `[server] host = "127.0.0.1"` (this phone only), or `"0.0.0.0"` if the
   caretaker's phone should reach it too.
4. **Pair this phone with the box.** It gets its own pairing; the laptop's stays valid:
   ```bash
   python phone_proof.py diag pair      # type the code shown on the TV
   python phone_proof.py diag stb       # expect: connected, power ON, foreground com.airtel.tv
   ```
5. **Keep Termux alive while Chrome is in front:**
   - Run `termux-wake-lock`.
   - Set Android Settings → Apps → Termux → Battery → **Unrestricted**.
6. **Start it:**
   ```bash
   python phone_proof.py serve 2>&1 | tee proof.log
   ```
   Open **http://127.0.0.1:8000** in Chrome, then menu → **Add to Home screen**. Chrome treats
   `127.0.0.1` as a secure address, so the home-screen app and screen wake lock work here. They
   don't work over plain `http://<laptop-ip>`.
7. **Shut the laptop down**, not just sleep, so nothing else on the network can be answering.

## Checklist

| # | Test | Pass |
|---|---|---|
| P1 | `diag pair` on the phone | Paired first time |
| P2 | `diag stb` | Connected, power and foreground app reported |
| P3 | `diag mismatch --channel 725` with the laptop off | 4 power cases and the menu escape pass |
| P4 | From the home-screen icon: cold tap (TV off), warm tap, TV off, volume ± | All work; countdown and ✓ show |
| P5 | 20 alternating taps 723 / 725 | **20/20** on the right channel |
| P6 | Leave Chrome in front, lock the phone for 30 min, unlock, tap | Works first time (Termux was not killed) |
| P7 | Battery: note the % before and after 2 h of `serve` running | Recorded. Tells us whether "phone as always-on server" is viable |
| P8 | Optional, `host = "0.0.0.0"`: caretaker's phone opens `http://<this phone's IP>:8000` | Taps work from the second phone |

## What this does and doesn't prove

**It proves:**
- A phone alone can reach the TV and the box on home Wi-Fi.
- The box pairing and its encrypted connection work from Android's network stack.
- The full tested flow is fast enough on phone hardware.
- The one-tap experience works on the phone it will actually live on.

**It does not prove:**
- That a Kotlin rewrite of the box protocol is correct. That's what the app's first milestone (M0)
  is for.
- That a *newly built* app gets the same network access. Termux targets an older Android API level,
  so newer local-network restrictions may not apply to it but would apply to a new app. Treat P1–P5
  as slightly optimistic on that one point.

**Decision rule:**
- **P1–P5 pass:** the Android app is a port of proven behaviour, not research. Start M0 of
  [PLAN_ANDROID.md](PLAN_ANDROID.md).
- **P1 or P2 fails:** stop and investigate. Something specific to Android networking or the box
  pairing would block the app too.
- **P6 or P7 fails:** this only rules out "phone as a 24/7 server". The planned app doesn't need
  that, because it connects only while it's open.

**Interim use:** if this passes, it can remove the laptop *today*. It's fragile, though: Android
updates, battery savers and Termux itself can stop it. Fine for a trial with the caretaker; not
something to leave Nani depending on long-term.

## Results

| # | Result | Notes |
|---|---|---|
| P1 | | |
| P2 | | |
| P3 | | |
| P4 | | |
| P5 | | |
| P6 | | |
| P7 | | |
| P8 | | |
