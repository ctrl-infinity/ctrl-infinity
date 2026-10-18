#!/usr/bin/env python3
"""
diagnose.py: run from your laptop on the same LAN as the TV. Reads the same config.toml as launcher.py.

  python diagnose.py psk                          # Bravia PSK handshake (and proves a WRONG PSK is rejected)
  python diagnose.py inputs                       # HDMI ports, connection flags, CEC device names
  python diagnose.py pair                         # one-time pairing with an Xstream box (atv_remote driver)
  python diagnose.py stb                          # driver reachability, IR codes, ADB focus/JVM cost, plug watts
  python diagnose.py cec --channel 105            # can the TV forward IRCC digits to the box over HDMI-CEC?
  python diagnose.py latency --trials 3 --cool 30 --manual   # standby -> active -> HDMI lock -> picture
  python diagnose.py digits --channels 105 210 --gaps 200 300 450 650   # inter-digit calibration
  python diagnose.py mismatch --channel 105       # TV/STB start in all 4 power-state combinations
  python diagnose.py all --channel 105

Interactive checks ask you to look at the TV. Pass --auto to skip them; those checks then show as UNVERIFIED.
"""
from __future__ import annotations

import argparse
import asyncio
import statistics
import sys
import time

import launcher as L

AUTO = False
G, R, Y, B, X = "\033[32m", "\033[31m", "\033[33m", "\033[1m", "\033[0m"


def ok(msg: str) -> None:
    print(f"  {G}PASS{X} {msg}")


def bad(msg: str) -> None:
    print(f"  {R}FAIL{X} {msg}")


def warn(msg: str) -> None:
    print(f"  {Y}WARN{X} {msg}")


def head(msg: str) -> None:
    print(f"\n{B}== {msg} =={X}")


async def ask(question: str) -> bool | None:
    """Ask the human (eyes on the TV). Returns None in --auto mode."""
    if AUTO:
        print(f"  ?    {question}  [auto: unverified]")
        return None
    ans = await asyncio.to_thread(input, f"  ?    {question} [y/n] ")
    return ans.strip().lower().startswith("y")


async def pause(msg: str) -> None:
    if not AUTO:
        await asyncio.to_thread(input, f"  >>   {msg} (Enter) ")


def target_uri(o: L.Orchestrator) -> str:
    return f"extInput:hdmi?port={o.cfg['tv']['hdmi_port']}"


async def wait_tv(o: L.Orchestrator, want: str, timeout: float = 30) -> float:
    """Wait for power state 'active'/'standby' ('unreachable' counts as standby). Returns seconds."""
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        try:
            st = await o.tv.power_status()
        except L.TvUnreachable:
            st = "standby"
        if st == want:
            return time.monotonic() - t0
        await asyncio.sleep(0.25)
    raise TimeoutError(f"TV did not reach {want} in {timeout}s")


# ---------------------------------------------------------------------------
async def t_psk(o: L.Orchestrator) -> bool:
    head("Bravia PSK handshake")
    tv, passed = o.tv, True
    try:
        info = (await tv.call("system", "getInterfaceInformation"))[0]  # needs no auth
        ok(f"REST API reachable: {info.get('modelName')} (interface {info.get('interfaceVersion')})")
    except L.TvUnreachable as e:
        bad(f"TV unreachable at {tv.ip} ({e}). TV in deep standby? Enable Settings > Network > Remote start.")
        return False
    except L.BraviaError as e:
        bad(f"REST API answered with {e}. Is IP control enabled on the TV?")
        return False
    try:
        woke = await tv.ensure_on(o.cfg["tv"]["wake_timeout_s"], 0.5)  # an authenticated write
        ok(f"correct PSK accepted (setPowerStatus{' woke the TV' if woke else ', TV already on'})")
    except L.BraviaAuthError:
        bad("correct PSK REJECTED: TV > IP control > Authentication must be 'Normal and Pre-Shared Key' "
            "and the key must match [tv] psk exactly")
        return False
    try:
        await tv.call("system", "setPowerStatus", {"status": True}, psk=tv.psk + "-wrong")
        warn("a WRONG PSK was accepted: authentication is effectively OFF, anyone on the Wi-Fi can drive the TV")
        passed = False
    except L.BraviaAuthError:
        ok("wrong PSK rejected (auth is really enforced)")
    try:
        si = (await tv.call("system", "getSystemInformation"))[0]
        ok(f"model {si.get('model')}, MAC {si.get('macAddr')}  <- put the MAC in [tv] mac for Wake-on-LAN")
        if not o.cfg["tv"]["mac"]:
            warn("[tv] mac is empty: no Wake-on-LAN fallback if the NIC sleeps")
    except L.BraviaError as e:
        warn(f"getSystemInformation: {e}")
    codes = await tv.ircc_codes()
    missing = [f"Num{i}" for i in range(10) if f"Num{i}" not in codes]
    (bad if missing else ok)(f"{len(codes)} IRCC keys; digits {'missing ' + str(missing) if missing else 'Num0-Num9 present'}")
    return passed and not missing


async def t_inputs(o: L.Orchestrator) -> bool:
    head("HDMI inputs")
    await o.tv.ensure_on(o.cfg["tv"]["wake_timeout_s"], 0.5)
    want, found = target_uri(o), False
    for i in await o.tv.external_inputs():
        mark = "  <== target" if i.get("uri") == want else ""
        found |= bool(mark)
        print(f"     {i.get('uri'):<28} title={i.get('title')!r:<12} label={i.get('label')!r:<14} "
              f"connection={i.get('connection')} status={i.get('status')}{mark}")
    (ok if found else bad)(f"target {want} {'exists' if found else 'NOT FOUND: fix [tv] hdmi_port'}")
    print(f"     now playing: {await o.tv.playing_uri()}")
    print("     note: 'connection' is HDMI hot-plug only; many STBs keep it true in standby, so it is NOT a power signal.")
    return found


async def t_pair(o: L.Orchestrator) -> bool:
    head("Pair with the Xstream box (Android TV Remote protocol)")
    a = o.cfg["stb"]["atv_remote"]
    if not a["host"]:
        bad("[stb.atv_remote] host is not set")
        return False
    print(f"     connecting to {a['host']}:6467. A code will appear on the TV screen.")

    async def ask_code() -> str:
        return await asyncio.to_thread(input, "  >>   type the code shown on the TV: ")

    try:
        name, mac = await L.pair_atv_remote(a, ask_code)
    except L.StbError as e:
        bad(str(e))
        return False
    ok(f"paired with {name} ({mac}); credentials saved to {a['cert_file']} / {a['key_file']}")
    print("     keep those two files private and copy them with config.toml when you move to the Pi.")
    return True


async def t_stb(o: L.Orchestrator) -> bool:
    head(f"STB driver: {o.stb.name} (power: {o.cfg['stb']['power_strategy']})")
    s, passed = o.stb, True
    if isinstance(s, L.AtvRemoteStb):
        try:
            await s.connect()
            ok(f"connected to {s.a['host']} over the Android TV Remote protocol")
        except L.StbError as e:
            bad(str(e))
            return False
        await asyncio.sleep(3)  # the box reports power state and foreground app shortly after connecting
        on = await s.is_on()
        (ok if on is not None else warn)(f"power state: {'unknown (box has not reported yet)' if on is None else ('ON' if on else 'STANDBY')}")
        fg = await s.foreground()
        print(f"     foreground app: {fg or '(not reported)'}")
        print("     -> With Airtel LIVE TV showing on the box, the package above is your [stb.atv_remote] live_app.")
        if not s.live_app:
            warn("live_app not set: no home-screen bypass on the Xstream box")
        elif s.live_app not in fg:
            warn(f"live_app {s.live_app!r} is not in the foreground now; fine if the box is not on live TV")
        return True
    if isinstance(s, L.AdbStb):
        try:
            await s.connect()
            ok(f"ADB connected to {s.serial}")
        except L.StbError as e:
            bad(str(e))
            return False
        ok(f"wakefulness: {'Awake' if await s.is_on() else 'Asleep'}")
        print(f"     foreground: {await s.focused()}")
        print("     -> With Airtel LIVE TV showing on the box, the component above (pkg/.Activity) is your "
              "[stb.adb] live_component.")
        t = []
        for _ in range(3):
            t0 = time.monotonic()
            await s.shell("input keyevent 0")  # KEYCODE_UNKNOWN: does nothing
            t.append(time.monotonic() - t0)
        print(f"     `input keyevent` round-trip: median {statistics.median(t) * 1000:.0f} ms "
              f"(real inter-digit gap = this JVM cost + inter_digit_ms)")
        if not s.component:
            warn("live_component not set: no home-screen bypass on the Xstream box")
        return True
    if isinstance(s, L.Esp32Stb):
        try:
            ok(f"ESP32 blaster: {(await s.http.get(s.base + '/health')).json()}")
        except Exception as e:
            bad(f"ESP32 {s.base}: {e}")
            passed = False
    if isinstance(s, L.BroadlinkStb):
        try:
            await asyncio.to_thread(s._device)
            ok(f"Broadlink {s.host} authenticated (local protocol)")
        except Exception as e:
            bad(f"Broadlink {s.host}: {e} (device locked by the Broadlink app? unlock it there)")
            passed = False
    if isinstance(s, L.TvCecStb):
        print("     tv_cec has no hardware of its own: run `diagnose.py cec` to prove pass-through works.")
    if hasattr(s, "codes"):
        print(f"     learned IR keys: {sorted(s.codes)}")
    if s.sense:
        try:
            w, relay = await s.sense.watts(), await s.sense.relay_on()
            ok(f"power plug: relay {'ON' if relay else 'OFF'}, {w:.1f} W -> STB "
               f"{'ON' if w >= s.sense.threshold_w else 'STANDBY'} (threshold {s.sense.threshold_w} W)")
            print("     calibrate: note watts with the box ON and in STANDBY; set on_threshold_w halfway between.")
        except L.StbError as e:
            bad(str(e))
            passed = False
    else:
        warn("no closed-loop STB power state (always_on): if someone switches the box off with its remote, "
             "nothing here can tell. Disable the box's auto-standby and hide its remote, or add a Tasmota plug.")
    return passed


async def t_cec(o: L.Orchestrator, channel: str) -> bool | None:
    head("HDMI-CEC pass-through (TV IRCC digits -> Airtel box)")
    t = o.cfg["tv"]
    await o.tv.ensure_on(t["wake_timeout_s"], 0.5)
    await o.tv.ensure_input(t["hdmi_port"], t["input_timeout_s"], 0.5)
    await asyncio.sleep(t["hdmi_settle_s"])
    inp = next((i for i in await o.tv.external_inputs() if i.get("uri") == target_uri(o)), {})
    print(f"     TV sees on this port: title={inp.get('title')!r} label={inp.get('label')!r}")
    print("     (a device name like 'Airtel'/'Android TV' in the title/label suggests CEC is alive; "
          "a bare 'HDMI n' suggests the box doesn't speak CEC)")
    print("     also enable on the TV: Channels & Inputs > External inputs > BRAVIA Sync > BRAVIA Sync control = ON")
    await pause(f"Note the channel on screen now. I will send {channel} as TV remote keys")
    cec = L.TvCecStb(o.cfg["stb"], o.http, o.tv)
    for k in cec.pre_keys:
        if k not in cec.KEYMAP:
            cec.pre_keys = []
    await cec.select_channel(channel)
    seen = await ask(f"Did the Airtel box switch to channel {channel}?")
    if seen:
        ok("pass-through WORKS: you can use driver = \"tv_cec\" (no IR hardware). It still needs the TV on that HDMI port.")
    elif seen is False:
        bad("no pass-through: the box ignores CEC <User Control Pressed>. Use ADB (Xstream) or an IR blaster.")
    return seen


async def t_latency(o: L.Orchestrator, trials: int, cool: float, manual: bool) -> bool:
    head("Boot-to-HDMI-lock latency")
    tv, t, want = o.tv, o.cfg["tv"], target_uri(o)
    rows = []
    for n in range(1, trials + 1):
        await tv.ensure_on(t["wake_timeout_s"], 0.5)
        await tv.set_power(False)
        await wait_tv(o, "standby")
        print(f"     trial {n}: TV in standby, cooling {cool:.0f}s (longer = deeper standby = colder boot)")
        await asyncio.sleep(cool)
        t0 = time.monotonic()
        try:
            await tv.set_power(True)
        except L.TvUnreachable:
            tv.send_wol()
            print("     (REST unreachable in standby -> sent Wake-on-LAN)")
        t_active = None
        while t_active is None:
            try:
                if await tv.power_status() == "active":
                    t_active = time.monotonic() - t0
            except (L.TvUnreachable, L.BraviaError):
                pass
            if time.monotonic() - t0 > t["wake_timeout_s"]:
                raise TimeoutError("TV never reported active")
            await asyncio.sleep(0.2)
        t_set = t_lock = None
        while t_lock is None:
            try:
                if await tv.playing_uri() == want:
                    t_lock = time.monotonic() - t0
                    t_set = t_set or t_lock  # TV woke straight onto the right input
                else:
                    await tv.call("avContent", "setPlayContent", {"uri": want})
                    t_set = t_set or time.monotonic() - t0
            except (L.TvUnreachable, L.BraviaError):
                pass
            if time.monotonic() - t0 > t["wake_timeout_s"] + t["input_timeout_s"]:
                raise TimeoutError("TV never locked to the HDMI input")
            await asyncio.sleep(0.2)
        t_pic = None
        if manual and not AUTO:
            await asyncio.to_thread(input, "  >>   press Enter THE MOMENT the Airtel picture appears ")
            t_pic = time.monotonic() - t0
        rows.append((t_active, t_set, t_lock, t_pic))
        print(f"     active {t_active:5.1f}s | setPlayContent ok {t_set:5.1f}s | API reports HDMI {t_lock:5.1f}s"
              + (f" | picture {t_pic:5.1f}s" if t_pic else ""))

    # warm path: Google TV home -> HDMI
    await tv.send_ircc("Home")
    await asyncio.sleep(3)
    t0 = time.monotonic()
    await tv.ensure_input(t["hdmi_port"], t["input_timeout_s"], 0.2)
    warm = time.monotonic() - t0
    warm_pic = None
    if manual and not AUTO:
        await asyncio.to_thread(input, "  >>   press Enter THE MOMENT the Airtel picture appears ")
        warm_pic = time.monotonic() - t0
    print(f"     warm switch (Google TV home -> HDMI {t['hdmi_port']}): API {warm:.1f}s"
          + (f" | picture {warm_pic:.1f}s. Suggest [tv] hdmi_switch_settle_s = "
             f"{max(0.5, warm_pic - warm + 0.5):.1f}" if warm_pic else ""))

    locks = [r[2] for r in rows]
    print(f"     API lock: min {min(locks):.1f}s  median {statistics.median(locks):.1f}s  max {max(locks):.1f}s")
    if any(r[3] for r in rows):
        gap = statistics.median([r[3] - r[2] for r in rows if r[3]])
        print(f"     picture lags API by median {gap:.1f}s (HDCP + STB output). "
              f"Suggest [tv] hdmi_settle_s = {max(0.5, gap + 0.5):.1f}")
    need = max(locks) * 1.5
    (ok if t["wake_timeout_s"] >= need else warn)(
        f"wake_timeout_s = {t['wake_timeout_s']} (recommend >= {need:.0f})")
    o.timings.record("tv_cold", statistics.median(locks) + t["hdmi_settle_s"])
    return True


async def t_digits(o: L.Orchestrator, channels: list[str], gaps: list[int]) -> bool:
    head("Inter-digit calibration")
    t = o.cfg["tv"]
    await o.tv.ensure_on(t["wake_timeout_s"], 0.5)
    await o.tv.ensure_input(t["hdmi_port"], t["input_timeout_s"], 0.5)
    await o.stb.ensure_on()
    results: dict[int, bool | None] = {}
    for gap in sorted(gaps):
        o.stb.gap = gap / 1000
        res = []
        for ch in channels:  # alternate channels so a failure is visible
            await o.stb.select_channel(ch)
            res.append(await ask(f"[{gap} ms] is the box on {ch}?"))
        results[gap] = None if None in res else all(res)
        print(f"     {gap:4d} ms -> {results[gap]}")
    good = [g for g in sorted(results) if all(results[h] for h in results if h >= g)]
    if good:
        rec = int(min(good) * 1.5)
        ok(f"smallest reliable gap {min(good)} ms. Set [stb] inter_digit_ms = {rec} (1.5x margin), "
           "and keep commit_wait_ms above the box's digit timeout (watch how long the '1 0 _' banner stays).")
        return True
    if any(v is False for v in results.values()):
        bad("no gap was reliable: check IR aim/codes before tuning timing")
    return False


async def force_stb(o: L.Orchestrator, on: bool) -> str:
    s = o.stb
    if s.native_power or s.sense:
        try:
            await (s.ensure_on() if on else s.ensure_off())
            return "forced"
        except L.StbError as e:
            warn(f"could not put the box {'ON' if on else 'in STANDBY'} automatically: {e}")
    await pause(f"Use the Airtel remote to put the box {'ON' if on else 'in STANDBY'}")
    return "manual"


async def t_mismatch(o: L.Orchestrator, channel: str) -> bool:
    head("Mismatched power-state recovery")
    tv, t, want = o.tv, o.cfg["tv"], target_uri(o)
    if o.stb.sense is None and not o.stb.native_power:
        warn("always_on strategy: the STB-off cases are EXPECTED to fail; that is the point of this test")
    matrix = []
    for tv_on, stb_on in [(True, True), (True, False), (False, True), (False, False)]:
        label = f"TV {'on ' if tv_on else 'off'} / STB {'on ' if stb_on else 'off'}"
        print(f"\n   {B}{label}{X}")
        await tv.ensure_on(t["wake_timeout_s"], 0.5)
        if tv_on:
            await tv.send_ircc("Home")  # also start from the wrong input
        await force_stb(o, stb_on)
        if not tv_on:
            await tv.set_power(False)
            await wait_tv(o, "standby")
        t0 = time.monotonic()
        job = await o.wait(o.submit(channel, "diagnose"))
        dt = time.monotonic() - t0
        checks = {"job": job.state == "done"}
        try:
            checks["tv active"] = await tv.power_status() == "active"
            checks["hdmi"] = await tv.playing_uri() == want
        except Exception:
            checks["tv active"] = checks["hdmi"] = False
        st = await o.stb.is_on() if (o.stb.sense or o.stb.native_power) else None
        if st is not None:
            checks["stb on"] = st
        checks["eyes"] = await ask(f"Is the TV showing Airtel channel {channel}?")
        passed = all(v is not False for v in checks.values())
        detail = " ".join(f"{k}={'?' if v is None else ('ok' if v else 'NO')}" for k, v in checks.items())
        (ok if passed else bad)(f"{label}: {dt:.1f}s  {detail}" + (f"  ({job.message})" if job.message else ""))
        matrix.append((label, passed))
        o.current = None  # bypass the debounce so the next combination runs for real
    print(f"\n   {B}Bonus: STB stuck in a menu{X}")
    await pause("Open the Airtel menu / guide / any settings screen with the remote")
    o.current = None
    job = await o.wait(o.submit(channel, "diagnose"))
    seen = await ask(f"Did it escape the menu and land on {channel}?")
    (ok if seen is not False and job.state == "done" else bad)(
        "menu escape" + ("" if seen is not False else ": add another 'exit' (or 'back') to pre_tune_keys"))
    return all(p for _, p in matrix)


async def main() -> int:
    global AUTO
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("test", choices=["psk", "inputs", "pair", "stb", "cec", "latency", "digits", "mismatch", "all"])
    ap.add_argument("--channel", default=None, help="test channel number, e.g. 105")
    ap.add_argument("--channels", nargs="+", default=None, help="two channels to alternate for digits")
    ap.add_argument("--gaps", nargs="+", type=int, default=[200, 300, 450, 650])
    ap.add_argument("--trials", type=int, default=3)
    ap.add_argument("--cool", type=float, default=20.0, help="seconds in standby before each wake")
    ap.add_argument("--manual", action="store_true", help="stopwatch the real picture with Enter")
    ap.add_argument("--auto", action="store_true", help="skip questions (results show as unverified)")
    a = ap.parse_args()
    AUTO = a.auto
    o = L.Orchestrator(L.load_config())
    ch = a.channel or (o.cfg["channels"][0]["number"] if o.cfg["channels"] else None)
    try:
        if a.test in ("cec", "digits", "mismatch") and not ch:
            ap.error("--channel is required")
        if a.test == "psk":
            return 0 if await t_psk(o) else 1
        if a.test == "inputs":
            return 0 if await t_inputs(o) else 1
        if a.test == "pair":
            return 0 if await t_pair(o) else 1
        if a.test == "stb":
            return 0 if await t_stb(o) else 1
        if a.test == "cec":
            return 0 if await t_cec(o, ch) is not False else 1
        if a.test == "latency":
            return 0 if await t_latency(o, a.trials, a.cool, a.manual) else 1
        if a.test == "digits":
            chans = a.channels or [c["number"] for c in o.cfg["channels"][:2]] or [ch]
            return 0 if await t_digits(o, chans, a.gaps) else 1
        if a.test == "mismatch":
            return 0 if await t_mismatch(o, ch) else 1
        results = {"psk": await t_psk(o)}
        if not results["psk"]:
            return 1
        results["inputs"] = await t_inputs(o)
        results["stb"] = await t_stb(o)
        if ch and o.stb.name == "tv_cec":  # informational otherwise: IR/ADB users don't need CEC
            results["cec"] = await t_cec(o, ch) is not False
        elif ch:
            await t_cec(o, ch)
        results["latency"] = await t_latency(o, 1, a.cool, a.manual)
        if ch:
            results["mismatch"] = await t_mismatch(o, ch)
        head("Summary")
        for k, v in results.items():
            (ok if v else bad)(k)
        return 0 if all(results.values()) else 1
    finally:
        await o.aclose()


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except KeyboardInterrupt:
        sys.exit(130)
