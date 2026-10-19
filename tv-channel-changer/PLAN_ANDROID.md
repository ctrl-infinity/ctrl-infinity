# Plan: Android app v0.1

**Goal:** remove the laptop. One app on the phone does everything `launcher.py` does today: wake
the Sony TV, switch to the Airtel box's HDMI port, wake the box, bring up Airtel live TV and type
the channel number. There's no server, no Pi and no laptop.

**Status:** planned, not started. **Gate 0** (the phone-only proof, [PHONE_PROOF.md](PHONE_PROOF.md))
comes first. Don't set up Android Studio until P1–P5 there pass.

**Scope:** our household only (Nani's phone, and optionally the caretaker's). Distribution to
other families comes later and is out of scope here.

---

## What changes

| Today | App |
|---|---|
| A laptop runs `launcher.py`; the phone shows its web page | One APK on the phone does everything. No server |
| The laptop must stay on, awake and on the same IP; a power cut stops it | Nothing to keep running. The app works whenever it's open |
| Smart-button webhook | Deferred: no server means nothing listening for the button |
| Server logs in a terminal | In-app log screen with a "share log" button |
| `diagnose.py` | Stays as the lab tool on the laptop. The app is the product |
| One server, many phones | Each phone pairs with the box and controls it directly |

## Scope

**In v0.1:**
- Channel cards with logos; TV off; volume ±.
- The full cold/warm tune flow: countdown, haptics, one silent retry.
- A "not on home Wi-Fi" screen with a big Wi-Fi symbol instead of a failure.
- A hidden settings screen (long-press + PIN) for:
  - TV IP, PSK and HDMI port;
  - box IP, pairing and `live_app`;
  - the channel list (number, name, logo from the gallery);
  - the timing values from [TEST_LOG.md](TEST_LOG.md).

**Out of v0.1:**
- The IR, ADB, CEC, ESP32 and Tasmota drivers.
- The smart-button webhook, cloud sync and automatic discovery.
- TV brands other than Sony Bravia.
- Device-owner kiosk mode.

**v0.2 candidates:**
- Voice prompts in Bengali or Hindi (recorded in a family member's voice, or text-to-speech).
- A home-screen widget for the caretaker.
- Config import/export (e.g. from `config.toml`).
- A setup wizard that finds the TV and box on Wi-Fi by itself.

## Architecture

- **Kotlin + Jetpack Compose, one Activity, Android 8 (API 26) or newer.** Check the actual phones'
  Android version first.
- **Dependencies:** coroutines, `protobuf-javalite`, OkHttp. Target APK size: under 5 MB.
- **Runs only while open.**
  - It connects to the box when the app opens, not on the first tap, so the tap doesn't pay the
    connection delay (edge case #3).
  - It disconnects when the app closes. No background service, no battery drain, and phone makers'
    aggressive app-killing (Xiaomi, Oppo) doesn't matter.
- **Storage:**
  - Settings and learned timings: DataStore, Android's small key-value store.
  - The box pairing key: **AndroidKeyStore**. It generates the RSA key and self-signed certificate
    itself, so no bundled crypto library (BouncyCastle) is needed.
- **Wi-Fi:** bind the app's connections to the Wi-Fi network through Android's network API. Android
  can otherwise send traffic over mobile data when it decides the Wi-Fi "has no internet".

### Python → Kotlin map

| Python today (`launcher.py`) | Kotlin package | Size of job |
|---|---|---|
| `Bravia` | `bravia/BraviaClient`: JSON-RPC + `X-Auth-PSK`, Wake-on-LAN over UDP | Small; plain HTTP calls, already proven |
| `androidtvremote2` library (≈700 lines) | `atv/`: pairing (port 6467), remote control (port 6466), two `.proto` files | **Main risk:** see below |
| `Orchestrator`, `Job`, `Timings`, `AtvRemoteStb`, `StbDriver` pacing | `core/`: coroutine state machine, `Mutex`, latest-wins cancellation | Medium; direct port, with `TEST_LOG.md` as the spec |
| Embedded HTML/JS kiosk page | `ui/`: Compose cards, overlay, settings | Medium |

### Why the box protocol is the risk

It's small (pairing ≈ 214 lines, remote channel ≈ 400 lines in `androidtvremote2`), but these
details have to be byte-exact:
- **Pairing hash.** SHA-256 over the client and server keys' modulus and exponent, written in hex,
  with the exponent prefixed by `0`. The first byte of the hash must equal the first two hex digits
  of the code shown on the TV.
- **Framing.** Each message is protobuf, prefixed by its length.
- **Keep-alive.** The box sends ping requests that must be answered.
- **Handshake.** The box sends a feature negotiation (`remote_configure`, then `remote_set_active`)
  that the app must answer.
- **TLS.** Client-certificate TLS against the box's self-signed certificate. That needs a custom
  trust manager; pin the box's certificate after pairing.

The `.proto` files aren't shipped in the Python package (only the generated `_pb2` files). Take them
from the `androidtvremote2` source repository.

## Behaviours to port exactly

All of these were measured on the real Sony KD-49X8000H and Airtel Xstream box (see
[TEST_LOG.md](TEST_LOG.md); `#n` = its edge-case register).

1. **TV wake:**
   - `getPowerStatus`; if not active, `setPowerStatus(true)`, re-sent every 5 s.
   - Wake-on-LAN if the REST API doesn't answer.
   - Tolerate a 404 on `/sony/system` for a few seconds after the TV is turned off (#23).
2. **TV input:** `setPlayContent extInput:hdmi?port=N`, then read it back with
   `getPlayingContentInfo`. Error 7 means an app or the home screen is in front.
3. **Settle:** `hdmi_settle_s = 25` after a cold wake; `hdmi_switch_settle_s = 3` after an input
   switch on a TV that was already on. Keep the adaptive-settle idea (#21) for later.
4. **Box power:**
   - The box ignores `KEYCODE_SLEEP`/`KEYCODE_WAKEUP`; `KEYCODE_POWER` (26) is a toggle.
   - Read `is_on`, press once only if it's wrong, verify, **never retry blind**.
   - Wait 15 s between power changes (#4, #14).
5. **Live-app bypass:**
   - Wait up to 4 s for the box to report its foreground app (#3).
   - If `com.airtel.tv` isn't in front: Home, wait 1 s, launch the app, wait up to 8 s; retry once (#1).
   - Then wait `live_settle_s`, which still needs measuring (#2).
6. **Digits:**
   - `inter_digit_ms = 150`, start-to-start pacing; `commit_wait_ms = 2500`.
   - **Wait out the previous entry's commit window before typing**, so the last tap wins (`ee1ec10`).
7. **Job:**
   - The TV and box paths run in parallel.
   - Latest request wins; the same request joins the running job.
   - One silent retry from the UI (`622b830`).
   - The countdown learns real wake times per phone (EWMA).

## Milestones (part-time, rough)

| # | Deliverable | Exit test | Estimate |
|---|---|---|---|
| **Gate 0** | Phone-only proof in Termux ([PHONE_PROOF.md](PHONE_PROOF.md)) | P1–P5 pass with the laptop shut down | 1 evening |
| **M0 spike** | Bare app: pair with the box, send 7-2-3 | Box lands on 723; pairing survives an app restart. **Not working within a week → switch to the Chaquopy route** (reuse the Python inside the app) | 2–5 days |
| M1 | Bravia client + state machine + debug screen | The 4 TV/box power cases from Phase 8 pass from the phone, laptop off | ~1 week |
| M2 | Nani's UI + settings screen | The Phase 9 phone checklist passes with no laptop | ~1 week |
| M3 | Hardening: Wi-Fi screen, log export, signed APK on both phones | One week of daily use + the remaining Phase 10 tests (morning cold start, power cut, router reboot, watching Nani) | ~1 week |

That's about **4 weeks part-time to a usable v0.1**. M0 decides whether that estimate holds.

## Distribution (v0.1)

A signed APK installed directly onto the phones ("sideloaded"), not the Play Store. Keep the
signing key backed up: every update must be signed with the same key, or the app has to be
uninstalled and paired again.

## Risks

| Risk | Mitigation |
|---|---|
| Box protocol port has a subtle bug (pairing hash, framing, ping) | M0 tests it alone first; the Python library is the reference; fall back to Chaquopy |
| Newer Android versions restrict local-network access for new apps (Termux may be exempt, so Gate 0 can be optimistic) | Check on the real phones during M0 |
| Android routes traffic over mobile data when Wi-Fi "has no internet" | Bind connections to the Wi-Fi network; show the Wi-Fi screen otherwise |
| Two phones tapping at the same moment interleave digits | Rare at home; accept for v0.1 |
| Pairing lost after a box reset or firmware update (#9) | Settings shows "pair again"; IR blaster remains the documented fallback |
| Old or slow phone | Compose + no background work; test on the actual phones in M2 |

## Open decisions

1. **Native Kotlin**, with M0 as the go/no-go (recommended), or Chaquopy from the start.
2. **Language:** the channels are Bengali (Zee 24 Ghanta, News18 Bangla) but the UI text is Hindi.
   Which one first, for the UI and later the voice prompts?
3. **Android versions** of Nani's phone and the caretaker's phone (Settings → About phone).
4. **Where the app code lives:** this repo is public and is the GitHub profile repo. A separate
   (possibly private) repo is cleaner; otherwise `ctrl-infinity/tv-android/`.
5. **Who builds it:** Android Studio on the Windows laptop. Code can be written anywhere, but testing
   needs the real TV and box.
