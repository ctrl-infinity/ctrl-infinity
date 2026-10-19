#!/usr/bin/env python3
"""
launcher.py: one tap -> Sony Bravia awake on the right HDMI -> Airtel STB on the right channel.

One file holds all of it: the Bravia REST client, the Airtel STB drivers (ADB / Broadlink /
ESP32 / TV-CEC), the job state machine, the FastAPI backend, the kiosk PWA and the
smart-button webhook. It only talks to devices on the LAN and never to the internet.

    pip install -r requirements.txt
    cp config.example.toml config.toml        # then edit it
    python launcher.py                        # or: uvicorn launcher:app --host 0.0.0.0 --port 8000

Design rule: every step means "make it so", never "toggle it". A step reads the device state,
acts only if the state is wrong, then reads it again to verify. You can re-run any job from any
starting state and it won't break anything. The one exception is the power key on a classic
IR-only DTH box, which can only toggle. That key is only ever pressed after a power-meter
reading proves the box is off (see PowerSense).
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import secrets
import shlex
import socket
import threading
import time
import tomllib
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response

log = logging.getLogger("launcher")
HERE = Path(__file__).resolve().parent
CONFIG_PATH = Path(os.environ.get("LAUNCHER_CONFIG", HERE / "config.toml"))

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
DEFAULTS: dict[str, Any] = {
    "server": {
        "host": "0.0.0.0",
        "port": 8000,
        "title": "नानी का टीवी",
        "help_text": "",            # shown when the server is unreachable, e.g. "Call Vinayak: 98xxxxxx"
        "webhook_token": "",        # empty = webhook open to the LAN (same as the UI)
        "debounce_s": 3.0,          # same target re-requested within this window after success = no-op
    },
    "tv": {
        "ip": "",
        "psk": "",
        "mac": "",                  # optional, enables Wake-on-LAN fallback
        "wol_broadcast": "255.255.255.255",
        "hdmi_port": 1,
        "wake_timeout_s": 60.0,
        "input_timeout_s": 25.0,
        "hdmi_settle_s": 2.0,       # picture lock after a COLD wake (TV was in standby); calibrate with diagnose.py latency
        "hdmi_switch_settle_s": 3.0,  # picture lock after switching input on a TV that was already on
        "poll_s": 0.7,
        "http_timeout_s": 3.0,
        "volume_target": "speaker",
    },
    "stb": {
        "driver": "esp32",          # atv_remote | adb | broadlink | esp32 | tv_cec
        "ir_codes_file": "ir_codes.json",
        "inter_digit_ms": 450,      # start-to-start; calibrate with diagnose.py digits
        "commit_wait_ms": 2500,     # box's digit-entry timeout before it tunes
        "commit_with_ok": False,    # press OK after the digits instead of waiting (only if your box tunes on OK)
        "pre_tune_keys": ["exit", "exit"],  # closes menus/guide/banners; harmless on live TV
        "pre_tune_gap_ms": 600,
        "power_strategy": "always_on",      # always_on | tasmota_sense   (adb and atv_remote have native wake/sleep)
        "boot_s": 25.0,             # wait after the box wakes from standby before it accepts digits
        "cold_boot_s": 75.0,        # wait after the smart plug restores mains power
        "off_with_tv": False,       # put the STB to sleep when the TV is turned off (needs a closed-loop power state)
        "adb": {"host": "", "adb_path": "adb", "live_component": "", "live_settle_s": 3.0},
        "atv_remote": {"host": "", "client_name": "tvlauncher", "cert_file": "atv_cert.pem",
                       "key_file": "atv_key.pem", "live_app": "", "live_settle_s": 3.0},
        "broadlink": {"host": ""},
        "esp32": {"base_url": ""},
        "power_sense": {"tasmota_url": "", "on_threshold_w": 5.0},
    },
    "channels": [],
    "buttons": {},
}
STB_DRIVERS = {"atv_remote", "adb", "broadlink", "esp32", "tv_cec"}
NATIVE_POWER_DRIVERS = {"adb", "atv_remote"}  # Android boxes: discrete wake/sleep keycodes, no toggle problem
POWER_STRATEGIES = {"always_on", "tasmota_sense", "native"}
# White text on these colours passes WCAG AA (>= 4.5:1).
PALETTE = ["#B71C1C", "#0D47A1", "#1B5E20", "#4A148C", "#BF360C", "#006064", "#880E4F", "#3E2723"]
BUTTON_ACTIONS = {"off", "vol_up", "vol_down"}


class ConfigError(ValueError):
    pass


def _merge(base: dict, over: dict) -> dict:
    out = dict(base)
    for k, v in over.items():
        out[k] = _merge(base[k], v) if isinstance(v, dict) and isinstance(base.get(k), dict) else v
    return out


def load_config(path: Path = CONFIG_PATH) -> dict:
    with open(path, "rb") as f:
        cfg = _merge(DEFAULTS, tomllib.load(f))
    cfg["_dir"] = str(Path(path).resolve().parent)
    tv, stb = cfg["tv"], cfg["stb"]
    if not tv["ip"] or not tv["psk"]:
        raise ConfigError("[tv] ip and psk are required")
    if stb["driver"] not in STB_DRIVERS:
        raise ConfigError(f"[stb] driver must be one of {sorted(STB_DRIVERS)}")
    if stb["driver"] in NATIVE_POWER_DRIVERS:
        stb["power_strategy"] = "native"
    if stb["power_strategy"] not in POWER_STRATEGIES or stb["power_strategy"] == "native" and stb["driver"] not in NATIVE_POWER_DRIVERS:
        raise ConfigError("[stb] power_strategy must be always_on or tasmota_sense for IR drivers")
    atv = stb["atv_remote"]
    if stb["driver"] == "atv_remote" and not atv["host"]:
        raise ConfigError("[stb.atv_remote] host is required")
    for k in ("cert_file", "key_file"):  # pairing material lives next to config.toml unless absolute
        p = Path(atv[k])
        atv[k] = str(p if p.is_absolute() else Path(cfg["_dir"]) / p)
    if stb["driver"] == "tv_cec" and stb["power_strategy"] != "always_on":
        raise ConfigError("tv_cec has no STB power key (IRCC 'Power' turns the TV off); use always_on")
    if stb["power_strategy"] == "tasmota_sense" and not stb["power_sense"]["tasmota_url"]:
        raise ConfigError("[stb.power_sense] tasmota_url is required for tasmota_sense")
    if stb["off_with_tv"] and stb["power_strategy"] == "always_on":
        raise ConfigError("off_with_tv needs tasmota_sense or adb; with always_on it would toggle blind")
    for i, ch in enumerate(cfg["channels"]):
        num = str(ch.get("number", ""))
        if not re.fullmatch(r"\d{1,4}", num):
            raise ConfigError(f"channel #{i + 1}: number must be 1-4 digits, got {num!r}")
        ch["number"] = num
        ch.setdefault("name", num)
        ch.setdefault("color", PALETTE[i % len(PALETTE)])
        ch.setdefault("logo", "")
    buttons = {}
    for bid, m in cfg["buttons"].items():
        m = {"single": m} if isinstance(m, str) else dict(m)
        for press, target in m.items():
            target = str(target)
            if target not in BUTTON_ACTIONS and not re.fullmatch(r"\d{1,4}", target):
                raise ConfigError(f"button {bid}.{press}: {target!r} is not a channel number or {sorted(BUTTON_ACTIONS)}")
            m[press] = target
        buttons[bid] = m
    cfg["buttons"] = buttons
    return cfg


# ---------------------------------------------------------------------------
# Sony Bravia REST API  (POST http://<tv>/sony/<service>, header X-Auth-PSK)
# TV setup: Settings > Network & Internet > Home network > IP control >
#   Authentication = "Normal and Pre-Shared Key", set the PSK;  and
#   Settings > Network & Internet > Remote start = ON  (otherwise the NIC sleeps in standby).
# ---------------------------------------------------------------------------
class TvUnreachable(RuntimeError):
    pass


class BraviaError(RuntimeError):
    def __init__(self, code: int, message: str):
        super().__init__(f"Bravia error {code}: {message}")
        self.code = code


class BraviaAuthError(BraviaError):
    pass


IRCC_SOAP = (
    '<?xml version="1.0"?>'
    '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" '
    's:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/"><s:Body>'
    '<u:X_SendIRCC xmlns:u="urn:schemas-sony-com:service:IRCC:1">'
    "<IRCCCode>{code}</IRCCCode></u:X_SendIRCC></s:Body></s:Envelope>"
)
ILLEGAL_STATE = 7  # getPlayingContentInfo: an app / the Google TV home screen is in front


class Bravia:
    def __init__(self, ip: str, psk: str, mac: str = "", http_timeout: float = 3.0,
                 wol_broadcast: str = "255.255.255.255"):
        self.ip, self.psk, self.mac, self.wol_broadcast = ip, psk, mac, wol_broadcast
        self.base = f"http://{ip}/sony"
        self._http = httpx.AsyncClient(timeout=http_timeout)
        self._ircc: dict[str, str] | None = None
        self._id = 0

    async def aclose(self) -> None:
        await self._http.aclose()

    async def call(self, service: str, method: str, params: dict | None = None,
                   version: str = "1.0", psk: str | None = None) -> list:
        self._id = self._id % 9999 + 1
        body = {"method": method, "id": self._id, "params": [params] if params is not None else [],
                "version": version}
        try:
            r = await self._http.post(f"{self.base}/{service}", json=body,
                                      headers={"X-Auth-PSK": self.psk if psk is None else psk})
        except httpx.TransportError as e:
            raise TvUnreachable(f"{self.ip}: {type(e).__name__}") from e
        if r.status_code in (401, 403):
            raise BraviaAuthError(r.status_code, "PSK rejected")
        r.raise_for_status()
        data = r.json()
        if "error" in data:
            err = data["error"] or [-1]
            code, msg = int(err[0]), str(err[1]) if len(err) > 1 else ""
            raise (BraviaAuthError if code in (401, 403) else BraviaError)(code, msg)
        return data.get("result") or []

    # -- power ---------------------------------------------------------------
    async def power_status(self) -> str:
        """'active' or 'standby'. Raises TvUnreachable if the NIC is asleep."""
        return (await self.call("system", "getPowerStatus"))[0]["status"]

    async def set_power(self, on: bool) -> None:
        # Discrete, not a toggle: setPowerStatus(true) on an active TV is a no-op.
        await self.call("system", "setPowerStatus", {"status": on})

    def send_wol(self) -> bool:
        if not self.mac:
            return False
        mac = bytes.fromhex(re.sub(r"[^0-9A-Fa-f]", "", self.mac))
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            s.sendto(b"\xff" * 6 + mac * 16, (self.wol_broadcast, 9))
        return True

    async def ensure_on(self, timeout: float, poll: float) -> bool:
        """Poll until the TV reports active. Returns True if it had to be woken."""
        deadline = time.monotonic() + timeout
        woke, last_kick, last_err = False, -1e9, None
        while True:
            try:
                if await self.power_status() == "active":
                    return woke
                if time.monotonic() - last_kick > 5:  # re-assert: safe because the call is discrete
                    await self.set_power(True)
                    woke, last_kick = True, time.monotonic()
            except BraviaAuthError:
                raise
            except TvUnreachable as e:
                last_err = e
                if time.monotonic() - last_kick > 5:
                    woke, last_kick = self.send_wol() or woke, time.monotonic()
            except BraviaError as e:  # transient while the TV boots
                last_err = e
            if time.monotonic() > deadline:
                raise TimeoutError(f"TV not active after {timeout:.0f}s ({last_err})")
            await asyncio.sleep(poll)

    # -- input ---------------------------------------------------------------
    async def playing_uri(self) -> str | None:
        try:
            res = await self.call("avContent", "getPlayingContentInfo")
            return res[0].get("uri") if res else None
        except BraviaError as e:
            if e.code == ILLEGAL_STATE:
                return None
            raise

    async def ensure_input(self, port: int, timeout: float, poll: float) -> bool:
        """Jump straight to extInput:hdmi?port=N (no input cycling), verify by reading it back.
        Returns True if the input had to be changed."""
        uri = f"extInput:hdmi?port={port}"
        deadline = time.monotonic() + timeout
        changed, last_err = False, None
        while True:
            try:
                if await self.playing_uri() == uri:
                    return changed
                await self.call("avContent", "setPlayContent", {"uri": uri})
                changed = True
            except BraviaAuthError:
                raise
            except (TvUnreachable, BraviaError) as e:  # e.g. input service not ready right after boot
                last_err = e
            if time.monotonic() > deadline:
                raise TimeoutError(f"TV did not lock to HDMI {port} after {timeout:.0f}s ({last_err})")
            await asyncio.sleep(poll)

    async def external_inputs(self) -> list[dict]:
        res = await self.call("avContent", "getCurrentExternalInputsStatus")
        return res[0] if res else []

    # -- IRCC (virtual remote keys) -----------------------------------------
    async def ircc_codes(self) -> dict[str, str]:
        if self._ircc is None:
            res = await self.call("system", "getRemoteControllerInfo")
            self._ircc = {c["name"]: c["value"] for c in res[1]}
        return self._ircc

    async def send_ircc(self, name: str) -> None:
        codes = await self.ircc_codes()
        if name not in codes:
            raise KeyError(f"TV has no IRCC key {name!r}")
        try:
            r = await self._http.post(
                f"{self.base}/IRCC", content=IRCC_SOAP.format(code=codes[name]),
                headers={"X-Auth-PSK": self.psk, "Content-Type": "text/xml; charset=UTF-8",
                         "SOAPACTION": '"urn:schemas-sony-com:service:IRCC:1#X_SendIRCC"'})
        except httpx.TransportError as e:
            raise TvUnreachable(f"{self.ip}: {type(e).__name__}") from e
        if r.status_code in (401, 403):
            raise BraviaAuthError(r.status_code, "PSK rejected")
        r.raise_for_status()

    async def volume_step(self, delta: int, target: str = "speaker") -> None:
        await self.call("audio", "setAudioVolume", {"target": target, "volume": f"{delta:+d}"})


# ---------------------------------------------------------------------------
# Airtel set-top box drivers
# ---------------------------------------------------------------------------
class StbError(RuntimeError):
    pass


class PowerSense:
    """Tasmota energy-monitoring plug in front of the STB. A classic DTH box draws ~1-3 W in
    standby and ~6-12 W when on. That reading is the only reliable way to see the box's power
    state, so it is what keeps the IR power toggle in sync with reality."""

    def __init__(self, url: str, threshold_w: float, http: httpx.AsyncClient):
        self.url, self.threshold_w, self.http = url.rstrip("/"), threshold_w, http

    async def _cmnd(self, cmnd: str) -> dict:
        try:
            r = await self.http.get(f"{self.url}/cm", params={"cmnd": cmnd})
            r.raise_for_status()
            return r.json()
        except (httpx.HTTPError, ValueError) as e:
            raise StbError(f"power plug {self.url}: {e}") from e

    async def watts(self) -> float:
        return float((await self._cmnd("Status 8"))["StatusSNS"]["ENERGY"]["Power"])

    async def relay_on(self) -> bool:
        d = await self._cmnd("Power")
        return (d.get("POWER") or d.get("POWER1")) == "ON"

    async def set_relay(self, on: bool) -> None:
        await self._cmnd(f"Power {'On' if on else 'Off'}")


class StbDriver:
    name = "base"
    tv_independent = True  # True: can tune while the TV is still booting (the box doesn't care)
    native_power = False   # True: the driver itself can read the box's power state and wake/sleep it

    def __init__(self, scfg: dict, http: httpx.AsyncClient):
        self.c = scfg
        self.http = http
        self.gap = scfg["inter_digit_ms"] / 1000
        self.pre_gap = scfg["pre_tune_gap_ms"] / 1000
        self.commit_wait = scfg["commit_wait_ms"] / 1000
        self.pre_keys: list[str] = list(scfg["pre_tune_keys"])
        ps = scfg["power_sense"]
        self.sense = (PowerSense(ps["tasmota_url"], ps["on_threshold_w"], http)
                      if scfg["power_strategy"] == "tasmota_sense" else None)
        self._lock = asyncio.Lock()  # one key sequence on the wire at a time

    async def press(self, key: str) -> None:
        raise NotImplementedError

    async def is_on(self) -> bool | None:
        """True/False if we can measure it, None if we can't (always_on: trust the box is on)."""
        if self.sense is None:
            return None
        return await self.sense.watts() >= self.sense.threshold_w

    async def _wait_power(self, want_on: bool, timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            await asyncio.sleep(1.0)
            if await self.is_on() == want_on:
                return True
        return False

    async def ensure_on(self, notify: Callable[[float], None] | None = None) -> bool:
        """Returns True if the box had to be woken."""
        if self.sense is None:
            return False
        woke = False
        if not await self.sense.relay_on():
            if notify:
                notify(self.c["cold_boot_s"])
            await self.sense.set_relay(True)
            await asyncio.sleep(self.c["cold_boot_s"])
            woke = True
        if await self.is_on():
            return woke
        if notify:
            notify(self.c["boot_s"])
        await self.press("power")  # toggle, but only after measuring that the box is off
        if not await self._wait_power(True, 10):
            raise StbError("STB still in standby after one power press; not pressing again blind "
                           "(check IR aim/codes or the on_threshold_w)")
        await asyncio.sleep(self.c["boot_s"])
        return True

    async def ensure_off(self) -> None:
        if self.sense is None or not await self.is_on():
            return
        await self.press("power")
        if not await self._wait_power(False, 10):
            raise StbError("STB still drawing 'on' power after one power press")

    async def _paced(self, keys: list[str], gap: float) -> None:
        """Start-to-start pacing: key i fires at t0 + i*gap whatever the transport latency is."""
        t0 = time.monotonic()
        for i, k in enumerate(keys):
            delay = t0 + i * gap - time.monotonic()
            if delay > 0:
                await asyncio.sleep(delay)
            elif i and delay < -0.2 * gap:
                log.warning("STB key %r sent %.0f ms late: transport slower than inter_digit_ms", k, -delay * 1000)
            await self.press(k)

    async def goto_live(self) -> None:
        if self.pre_keys:
            await self._paced(self.pre_keys, self.pre_gap)
            await asyncio.sleep(self.pre_gap)

    _commit_until = 0.0  # monotonic time at which the digits sent last will have been committed by the box

    async def select_channel(self, number: str) -> None:
        async with self._lock:
            # A superseded job is cancelled while it sits in commit_wait, but its digits are already on
            # the box. Typing a new number into a box that is still committing the old one is ignored
            # (measured: tap 725, then 723 0.5 s later, left the box on 725). Let the old entry finish.
            wait = self._commit_until - time.monotonic()
            if wait > 0:
                await asyncio.sleep(wait)
            await self.goto_live()
            await self._paced(list(number) + (["ok"] if self.c["commit_with_ok"] else []), self.gap)
            self._commit_until = time.monotonic() + self.commit_wait
            await asyncio.sleep(self.commit_wait)

    def estimate(self, number: str) -> float:
        keys = len(number) + (1 if self.c["commit_with_ok"] else 0)
        bypass = (len(self.pre_keys) * self.pre_gap) if self.pre_keys else 0.0
        return bypass + (keys - 1) * self.gap + self.commit_wait + 0.5

    async def health(self) -> dict:
        out: dict[str, Any] = {"driver": self.name, "power_strategy": self.c["power_strategy"]}
        try:
            out["on"] = await self.is_on()
            if self.sense:
                out["watts"] = await self.sense.watts()
        except Exception as e:
            out["error"] = str(e)
        return out

    async def aclose(self) -> None:
        pass


def load_ir_codes(cfg: dict) -> dict:
    p = Path(cfg["stb"]["ir_codes_file"])
    p = p if p.is_absolute() else Path(cfg["_dir"]) / p
    if not p.is_file():
        raise ConfigError(f"{p} not found: run learn_ir.py to capture your Airtel remote first")
    codes = json.loads(p.read_text())
    need = [str(d) for d in range(10)] + [k for k in cfg["stb"]["pre_tune_keys"]]
    if cfg["stb"]["commit_with_ok"]:
        need.append("ok")
    if cfg["stb"]["power_strategy"] == "tasmota_sense":
        need.append("power")
    missing = sorted(set(need) - set(codes))
    if missing:
        raise ConfigError(f"{p} is missing keys {missing}: re-run learn_ir.py")
    return codes


class BroadlinkStb(StbDriver):
    """Broadlink RM4 Mini, local protocol only (python-broadlink); no Broadlink cloud involved."""
    name = "broadlink"

    def __init__(self, scfg: dict, http: httpx.AsyncClient, codes: dict):
        super().__init__(scfg, http)
        self.host = scfg["broadlink"]["host"]
        self.codes = {k: bytes.fromhex(v) for k, v in codes.items()}
        self._dev = None
        self._tlock = threading.Lock()

    def _device(self):
        import broadlink  # optional dependency
        if self._dev is None:
            dev = broadlink.hello(self.host)
            dev.auth()
            self._dev = dev
        return self._dev

    def _send_sync(self, pkt: bytes) -> None:
        with self._tlock:
            try:
                self._device().send_data(pkt)
            except Exception:
                self._dev = None  # device rebooted / session expired: re-auth once
                self._device().send_data(pkt)

    async def press(self, key: str) -> None:
        if key not in self.codes:
            raise StbError(f"no learned IR code for {key!r}")
        try:
            await asyncio.to_thread(self._send_sync, self.codes[key])
        except Exception as e:
            raise StbError(f"Broadlink {self.host}: {e}") from e


class Esp32Stb(StbDriver):
    """ESP32 running esp32_ir_blaster.ino: GET /ir?proto=NEC&code=0x..&bits=32, POST /raw."""
    name = "esp32"

    def __init__(self, scfg: dict, http: httpx.AsyncClient, codes: dict):
        super().__init__(scfg, http)
        self.base = scfg["esp32"]["base_url"].rstrip("/")
        self.codes = codes

    async def press(self, key: str) -> None:
        c = self.codes.get(key)
        if c is None:
            raise StbError(f"no learned IR code for {key!r}")
        try:
            if "raw" in c:
                r = await self.http.post(f"{self.base}/raw", params={"hz": c.get("hz", 38000)},
                                         content=",".join(map(str, c["raw"])))
            else:
                r = await self.http.get(f"{self.base}/ir", params={
                    "proto": c["proto"], "code": c["code"], "bits": c["bits"], "repeat": c.get("repeat", 0)})
            r.raise_for_status()
        except httpx.HTTPError as e:
            raise StbError(f"ESP32 {self.base}: {e}") from e

    async def health(self) -> dict:
        out = await super().health()
        try:
            out["blaster"] = (await self.http.get(f"{self.base}/health")).json()
        except Exception as e:
            out["blaster_error"] = str(e)
        return out


class TvCecStb(StbDriver):
    """No extra hardware: Bravia IRCC number keys, forwarded by the TV to the box over HDMI-CEC.
    Only works if diagnose.py cec says so (most classic DTH boxes ignore CEC remote keys)."""
    name = "tv_cec"
    tv_independent = False  # keys only reach the box while the TV is on and showing its HDMI port
    KEYMAP = {**{str(i): f"Num{i}" for i in range(10)}, "exit": "Exit", "ok": "Confirm", "back": "Return"}

    def __init__(self, scfg: dict, http: httpx.AsyncClient, tv: Bravia):
        super().__init__(scfg, http)
        self.tv = tv

    async def press(self, key: str) -> None:
        if key not in self.KEYMAP:
            raise StbError(f"key {key!r} not available over CEC")
        await self.tv.send_ircc(self.KEYMAP[key])


class AdbStb(StbDriver):
    """Airtel Xstream (Android TV) box over ADB-on-LAN. Gives discrete wake/sleep and a
    readable foreground app, so this pathway can see what state the box is in."""
    name = "adb"
    native_power = True
    KEYCODES = {**{str(i): str(7 + i) for i in range(10)},  # KEYCODE_0..9 = 7..16
                "ok": "23", "back": "4", "exit": "4", "home": "3", "power": "26",
                "wakeup": "224", "sleep": "223"}

    def __init__(self, scfg: dict, http: httpx.AsyncClient):
        super().__init__(scfg, http)
        a = scfg["adb"]
        if not a["host"]:
            raise ConfigError("[stb.adb] host is required")
        self.serial = a["host"] if ":" in a["host"] else f"{a['host']}:5555"
        self.adb_path = a["adb_path"]
        self.component = a["live_component"]
        # BACK on Android leaves the live-TV app, so it is NOT an idempotent "exit" here; the
        # home-screen bypass is "bring live_component to the foreground" instead.
        self.pre_keys = []
        if not self.component:
            log.warning("stb.adb.live_component not set: home-screen bypass disabled (run diagnose.py stb)")

    async def _adb(self, *args: str, timeout: float = 10.0) -> tuple[str, int]:
        try:
            proc = await asyncio.create_subprocess_exec(
                self.adb_path, *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
        except FileNotFoundError as e:
            raise StbError(f"adb binary not found ({self.adb_path}); apt install android-tools-adb") from e
        try:
            out, _ = await asyncio.wait_for(proc.communicate(), timeout)
        except TimeoutError:
            proc.kill()
            await proc.wait()
            raise StbError(f"adb {' '.join(args[:3])} timed out")
        return out.decode(errors="replace").strip(), proc.returncode or 0

    async def connect(self) -> None:
        state, _ = await self._adb("-s", self.serial, "get-state", timeout=4)
        if state == "device":
            return
        out, _ = await self._adb("connect", self.serial, timeout=8)
        state, _ = await self._adb("-s", self.serial, "get-state", timeout=4)
        if state != "device":
            if "unauthorized" in state + out:
                raise StbError("ADB unauthorized: accept 'Allow debugging?' on the TV with 'Always allow' ticked")
            raise StbError(f"ADB cannot reach {self.serial}: {out or state}")

    async def shell(self, cmd: str, timeout: float = 15.0) -> str:
        await self.connect()
        out, _ = await self._adb("-s", self.serial, "shell", cmd, timeout=timeout)
        return out

    async def is_on(self) -> bool:
        return "=Awake" in await self.shell("dumpsys power | grep -m1 mWakefulness=")

    async def _wait_awake(self, want: bool, timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if await self.is_on() == want:
                return True
            await asyncio.sleep(0.5)
        return False

    async def ensure_on(self, notify: Callable[[float], None] | None = None) -> bool:
        if await self.is_on():
            return False
        if notify:
            notify(self.c["boot_s"])
        await self.shell(f"input keyevent {self.KEYCODES['wakeup']}")  # discrete: never turns it off
        if not await self._wait_awake(True, 10):
            raise StbError("Xstream box did not wake via KEYCODE_WAKEUP")
        await asyncio.sleep(self.c["boot_s"])
        return True

    async def ensure_off(self) -> None:
        if await self.is_on():
            await self.shell(f"input keyevent {self.KEYCODES['sleep']}")
            if not await self._wait_awake(False, 10):
                raise StbError("Xstream box did not sleep via KEYCODE_SLEEP")

    async def focused(self) -> str:
        return await self.shell("dumpsys window | grep -E 'mCurrentFocus|mFocusedApp'")

    async def goto_live(self) -> None:
        if not self.component:
            return
        pkg = self.component.split("/")[0]
        if pkg in await self.focused():
            return
        await self.shell(f"am start -n {shlex.quote(self.component)}")
        deadline = time.monotonic() + 15
        while pkg not in await self.focused():
            if time.monotonic() > deadline:
                raise StbError(f"live TV app {self.component} did not come to the foreground")
            await asyncio.sleep(0.5)
        await asyncio.sleep(self.c["adb"]["live_settle_s"])

    async def press(self, key: str) -> None:
        await self.shell(f"input keyevent {self.KEYCODES[key]}")

    async def select_channel(self, number: str) -> None:
        async with self._lock:
            await self.goto_live()
            # All digits go in ONE adb round-trip so LAN jitter can't stretch the gaps. Each
            # `input` call also starts a JVM on the box (~0.2-0.6 s); diagnose.py stb measures it
            # and inter_digit_ms here is the sleep *between* calls.
            keys = list(number) + (["ok"] if self.c["commit_with_ok"] else [])
            script = f"; sleep {self.gap:.3f}; ".join(f"input keyevent {self.KEYCODES[k]}" for k in keys)
            await self.shell(script, timeout=30)
            await asyncio.sleep(self.commit_wait)

    def estimate(self, number: str) -> float:
        return len(number) * (self.gap + 0.4) + self.commit_wait + (1.0 if self.component else 0.0)

    async def health(self) -> dict:
        out: dict[str, Any] = {"driver": self.name, "serial": self.serial}
        try:
            out["on"] = await self.is_on()
            out["focus"] = await self.focused()
        except Exception as e:
            out["error"] = str(e)
        return out


async def pair_atv_remote(a: dict, ask_code: Callable[[], Any]) -> tuple[str, str]:
    """One-time pairing with an Android TV box. The box shows a code on screen; `ask_code` is an
    async callable that returns what the user read off it. Returns the box's (name, mac).
    The client certificate written to a['cert_file'] / a['key_file'] is the credential afterwards."""
    from androidtvremote2 import AndroidTVRemote, CannotConnect, InvalidAuth
    remote = AndroidTVRemote(a["client_name"], a["cert_file"], a["key_file"], a["host"])
    await remote.async_generate_cert_if_missing()
    try:
        name_mac = await remote.async_get_name_and_mac()
        await remote.async_start_pairing()
        await remote.async_finish_pairing((await ask_code()).strip())
    except CannotConnect as e:
        raise StbError(f"cannot reach {a['host']} on the Android TV Remote ports (6466/6467): {e}") from e
    except InvalidAuth as e:
        raise StbError("the box rejected the pairing code: run pairing again and type the new code") from e
    finally:
        remote.disconnect()
    return name_mac


class AtvRemoteStb(StbDriver):
    """Airtel Xstream (Android TV) box over the Android TV Remote protocol v2 (TLS, ports
    6466/6467: the protocol of the Google TV phone app). Keys are injected like a real remote's, so
    Airtel's own channel-number entry receives them. HDMI-CEC digits do NOT: that path lands in a
    system handler that commits after two digits. Needs no ADB and no extra hardware; pair once
    with `python diagnose.py pair`. Also reports power state and the foreground app."""
    name = "atv_remote"
    native_power = True
    KEYCODES = {**{str(i): 7 + i for i in range(10)},  # KEYCODE_0..9 = 7..16
                "ok": 23, "back": 4, "exit": 4, "home": 3, "power": 26}

    def __init__(self, scfg: dict, http: httpx.AsyncClient):
        super().__init__(scfg, http)
        self.a = scfg["atv_remote"]
        if not self.a["host"]:
            raise ConfigError("[stb.atv_remote] host is required")
        self.live_app = self.a["live_app"]
        # BACK leaves the live-TV app on Android, so (like adb) the home-screen bypass is
        # "bring live_app to the foreground", not blind exit keys.
        self.pre_keys = []
        self._remote: Any = None
        self._available = False
        self._conn_lock = asyncio.Lock()
        if not self.live_app:
            log.warning("stb.atv_remote.live_app not set: home-screen bypass disabled (run diagnose.py stb)")

    def _on_available(self, available: bool) -> None:
        self._available = available
        log.info("Xstream box connection %s", "up" if available else "lost; reconnecting")

    def _on_auth_lost(self) -> None:
        self._available = False
        log.error("Xstream box no longer accepts our pairing: run `python diagnose.py pair` again")

    async def connect(self):
        """Idempotent. After the first success the library reconnects by itself."""
        from androidtvremote2 import AndroidTVRemote, CannotConnect, ConnectionClosed, InvalidAuth
        async with self._conn_lock:
            if self._remote is not None:
                return self._remote
            r = AndroidTVRemote(self.a["client_name"], self.a["cert_file"], self.a["key_file"], self.a["host"])
            try:
                await asyncio.wait_for(r.async_connect(), 10)
            except InvalidAuth as e:
                r.disconnect()
                raise StbError("not paired with the Xstream box: run `python diagnose.py pair`") from e
            except (CannotConnect, ConnectionClosed, TimeoutError, OSError) as e:
                r.disconnect()
                raise StbError(f"Xstream box {self.a['host']} (Android TV Remote): {e or type(e).__name__}") from e
            r.add_is_available_updated_callback(self._on_available)
            r.keep_reconnecting(self._on_auth_lost)
            self._available = True
            self._remote = r
            return r

    async def _wait_available(self, timeout: float = 8.0) -> None:
        deadline = time.monotonic() + timeout
        while not self._available:
            if time.monotonic() > deadline:
                raise StbError("lost the connection to the Xstream box and it did not come back")
            await asyncio.sleep(0.2)

    async def press(self, key: str) -> None:
        from androidtvremote2 import ConnectionClosed
        if key not in self.KEYCODES:
            raise StbError(f"key {key!r} not available on the Xstream box")
        r = await self.connect()
        await self._wait_available()
        try:
            r.send_key_command(self.KEYCODES[key])
        except ConnectionClosed as e:
            raise StbError(f"Xstream box connection closed while sending {key!r}") from e

    async def is_on(self) -> bool | None:
        return (await self.connect()).is_on  # None until the box has reported its state

    async def _wait_on(self, want: bool, timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if await self.is_on() == want:
                return True
            await asyncio.sleep(0.5)
        return False

    # Measured on the Xstream box: KEYCODE_WAKEUP (224) and KEYCODE_SLEEP (223) are ignored, but a short
    # KEYCODE_POWER (26) toggles standby in both directions and `is_on` follows within ~2 s. So this is a
    # toggle: same rule as the IR boxes. Read the state, press ONCE only if it is wrong, verify, and never
    # retry blind (a second press after a slow first one would undo it).
    # The box also ignores POWER for a few seconds after a power change (a press ~1 s after a wake did nothing;
    # one 22 s after worked). 15 s is a conservative guess inside that range; narrow it if it ever matters.
    POWER_DEAD_TIME_S = 15.0
    _last_power_change = -1e9

    async def _toggle_power(self, want_on: bool) -> None:
        wait = self._last_power_change + self.POWER_DEAD_TIME_S - time.monotonic()
        if wait > 0:
            await asyncio.sleep(wait)
        if await self.is_on() == want_on:  # state changed while we waited (e.g. someone used the remote)
            return
        await self.press("power")
        self._last_power_change = time.monotonic()
        if not await self._wait_on(want_on, 10):
            raise StbError(f"Xstream box still {'in standby' if want_on else 'on'} after one POWER press; "
                           "not pressing again blind")

    async def ensure_on(self, notify: Callable[[float], None] | None = None) -> bool:
        if await self.is_on() is not False:  # on, or unknown: never toggle blind
            return False
        if notify:
            notify(self.c["boot_s"])
        await self._toggle_power(True)
        await asyncio.sleep(self.c["boot_s"])
        return True

    async def ensure_off(self) -> None:
        if await self.is_on():
            await self._toggle_power(False)

    async def foreground(self) -> str:
        return (await self.connect()).current_app or ""

    async def goto_live(self) -> None:
        if not self.live_app:
            return
        r = await self.connect()
        # A fresh connection reports the foreground app only after a moment. Deciding on the empty
        # value would needlessly leave a live channel that is already on screen.
        deadline = time.monotonic() + 4
        while not await self.foreground() and time.monotonic() < deadline:
            await asyncio.sleep(0.25)
        if self.live_app in await self.foreground():
            return
        await self._wait_available()
        # A launch request alone is ignored while some apps are up (measured: the Settings /
        # Developer options screen swallowed it for 20 s), but works from the home screen. So go
        # HOME first, launch, and retry once.
        for attempt in (1, 2):
            r.send_key_command(self.KEYCODES["home"])
            await asyncio.sleep(1.0)
            r.send_launch_app_command(self.live_app)
            deadline = time.monotonic() + 8
            while self.live_app not in await self.foreground():
                if time.monotonic() > deadline:
                    break
                await asyncio.sleep(0.25)
            else:
                await asyncio.sleep(self.a["live_settle_s"])
                return
            log.warning("live TV app %s not in the foreground after launch attempt %d (foreground: %r)",
                        self.live_app, attempt, await self.foreground())
        raise StbError(f"live TV app {self.live_app} did not come to the foreground "
                       f"(box is showing {await self.foreground() or 'an unknown app'})")

    def estimate(self, number: str) -> float:
        return super().estimate(number) + (self.a["live_settle_s"] if self.live_app else 0.0)

    async def health(self) -> dict:
        out: dict[str, Any] = {"driver": self.name, "host": self.a["host"], "connected": self._available}
        try:
            out["on"] = await self.is_on()
            out["foreground"] = await self.foreground()
        except Exception as e:
            out["error"] = str(e)
        return out

    async def aclose(self) -> None:
        if self._remote is not None:
            self._remote.disconnect()
            self._remote = None
            self._available = False


def make_stb(cfg: dict, tv: Bravia, http: httpx.AsyncClient) -> StbDriver:
    s = cfg["stb"]
    if s["driver"] == "atv_remote":
        return AtvRemoteStb(s, http)
    if s["driver"] == "adb":
        return AdbStb(s, http)
    if s["driver"] == "tv_cec":
        return TvCecStb(s, http, tv)
    codes = load_ir_codes(cfg)
    if s["driver"] == "broadlink":
        if not s["broadlink"]["host"]:
            raise ConfigError("[stb.broadlink] host is required")
        return BroadlinkStb(s, http, codes)
    if not s["esp32"]["base_url"]:
        raise ConfigError("[stb.esp32] base_url is required")
    return Esp32Stb(s, http, codes)


# ---------------------------------------------------------------------------
# Job state machine
# ---------------------------------------------------------------------------
class Timings:
    """EWMA of measured TV wake times, so the on-screen countdown gets more accurate with use."""
    DEFAULTS = {"tv_cold": 20.0, "tv_warm": 2.0}

    def __init__(self, path: Path):
        self.path = path
        self.v = dict(self.DEFAULTS)
        try:
            self.v.update(json.loads(path.read_text()))
        except (OSError, ValueError):
            pass

    def get(self, k: str) -> float:
        return float(self.v.get(k, 10.0))

    def record(self, k: str, secs: float) -> None:
        old = self.v.get(k)
        self.v[k] = round(secs if old is None else 0.7 * old + 0.3 * secs, 2)
        try:
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.v))
            tmp.replace(self.path)
        except OSError:
            pass


@dataclass
class Job:
    target: str          # channel number, or "off"
    label: str
    source: str
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:10])
    state: str = "running"   # running | done | failed | superseded
    tv: str = "pending"      # pending | waking | input | settle | off | ok
    stb: str = "pending"     # pending | waking | tuning | ok | skip
    message: str = ""
    eta_s: float = 0.0
    started: float = field(default_factory=time.monotonic)
    finished: float | None = None
    task: asyncio.Task | None = field(default=None, repr=False)

    def elapsed(self) -> float:
        return (self.finished or time.monotonic()) - self.started

    def public(self) -> dict:
        return {"id": self.id, "target": self.target, "label": self.label, "source": self.source,
                "state": self.state, "tv": self.tv, "stb": self.stb, "message": self.message,
                "eta_s": round(self.eta_s, 1), "elapsed_s": round(self.elapsed(), 2)}


class Orchestrator:
    """Latest request wins: a new target cancels the running job. That is safe because every
    job starts from scratch: it re-reads TV state, and the STB pre-tune EXIT keys wipe any
    half-entered digits."""

    def __init__(self, cfg: dict):
        self.cfg = cfg
        t = cfg["tv"]
        self.tv = Bravia(t["ip"], t["psk"], mac=t["mac"], http_timeout=t["http_timeout_s"],
                         wol_broadcast=t["wol_broadcast"])
        self.http = httpx.AsyncClient(timeout=5.0)
        self.stb = make_stb(cfg, self.tv, self.http)
        self.timings = Timings(Path(cfg["_dir"]) / "timings.json")
        self.jobs: dict[str, Job] = {}
        self.current: Job | None = None

    async def aclose(self) -> None:
        if self.current and self.current.task and not self.current.task.done():
            self.current.task.cancel()
        await self.stb.aclose()
        await self.tv.aclose()
        await self.http.aclose()

    def label_for(self, target: str) -> str:
        if target == "off":
            return "टीवी बंद"
        return next((c["name"] for c in self.cfg["channels"] if c["number"] == target), target)

    def submit(self, target: str, source: str) -> Job:
        cur, now = self.current, time.monotonic()
        if cur and cur.target == target:
            if cur.state == "running":
                return cur  # double tap / button bounce: join the running job
            if cur.state == "done" and now - (cur.finished or now) < self.cfg["server"]["debounce_s"]:
                return cur
        prev = cur.task if cur and cur.task and not cur.task.done() else None
        if prev:
            prev.cancel()
        job = Job(target, self.label_for(target), source)
        job.eta_s = 4.0 if target == "off" else max(self.timings.get("tv_cold"), self.stb.estimate(target))
        self.jobs[job.id] = job
        while len(self.jobs) > 50:
            self.jobs.pop(next(iter(self.jobs)))
        self.current = job
        job.task = asyncio.create_task(self._run(job, prev))
        log.info("job %s: %s (%s) from %s", job.id, target, job.label, source)
        return job

    async def wait(self, job: Job) -> Job:
        if job.task:
            await asyncio.wait([job.task])
        return job

    async def _run(self, job: Job, prev: asyncio.Task | None) -> None:
        try:
            if prev:
                await asyncio.wait([prev])  # never let two jobs drive the hardware at once
            if job.target == "off":
                await self._off(job)
            else:
                await self._tune(job)
            job.state = "done"
        except asyncio.CancelledError:
            job.state, job.message = "superseded", "replaced by a newer request"
            raise
        except Exception as e:
            job.state, job.message = "failed", f"{type(e).__name__}: {e}"
            log.warning("job %s failed: %s", job.id, job.message)
        finally:
            job.finished = time.monotonic()
            log.info("job %s %s in %.1fs (tv=%s stb=%s)", job.id, job.state, job.elapsed(), job.tv, job.stb)

    async def _tune(self, job: Job) -> None:
        stb_est = self.stb.estimate(job.target)
        tv_task = asyncio.create_task(self._tv_path(job, stb_est))
        if self.stb.tv_independent:
            # The box tunes whether or not the TV is showing it: run both in parallel, so the
            # total time is max(TV boot, STB tune) instead of the sum.
            results = await asyncio.gather(tv_task, self._stb_path(job), return_exceptions=True)
            for r in results:
                if isinstance(r, BaseException):
                    raise r
        else:
            await tv_task
            await self._stb_path(job)

    async def _tv_path(self, job: Job, stb_est: float) -> None:
        t, t0 = self.cfg["tv"], time.monotonic()
        job.tv = "waking"
        try:
            cold = await self.tv.power_status() != "active"
        except TvUnreachable:
            cold = True
        tv_est = self.timings.get("tv_cold" if cold else "tv_warm")
        job.eta_s = max(tv_est, stb_est) if self.stb.tv_independent else tv_est + stb_est
        await self.tv.ensure_on(t["wake_timeout_s"], t["poll_s"])
        job.tv = "input"
        changed = await self.tv.ensure_input(t["hdmi_port"], t["input_timeout_s"], t["poll_s"])
        if cold or changed:
            # A cold wake waits for the slow first HDCP/picture lock; an input switch on a TV that
            # was already on only needs a short one.
            job.tv = "settle"
            await asyncio.sleep(t["hdmi_settle_s"] if cold else t["hdmi_switch_settle_s"])
        job.tv = "ok"
        self.timings.record("tv_cold" if cold else "tv_warm", time.monotonic() - t0)

    async def _stb_path(self, job: Job) -> None:
        def bump(extra: float) -> None:
            job.eta_s = max(job.eta_s, job.elapsed() + extra + self.stb.estimate(job.target))
        job.stb = "waking"
        await self.stb.ensure_on(bump)
        job.stb = "tuning"
        await self.stb.select_channel(job.target)
        job.stb = "ok"

    async def _off(self, job: Job) -> None:
        job.tv, job.stb, job.eta_s = "off", "skip", 4.0
        try:
            if await self.tv.power_status() == "active":
                await self.tv.set_power(False)
        except TvUnreachable:
            pass  # NIC asleep means the TV is already in standby
        if self.cfg["stb"]["off_with_tv"]:
            job.stb = "waking"
            await self.stb.ensure_off()
            job.stb = "ok"
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            try:
                if await self.tv.power_status() == "standby":
                    break
            except TvUnreachable:
                break
            await asyncio.sleep(0.5)
        else:
            raise TimeoutError("TV did not go to standby")
        job.tv = "ok"


# ---------------------------------------------------------------------------
# HTTP API + kiosk PWA
# ---------------------------------------------------------------------------
@asynccontextmanager
async def lifespan(app: FastAPI):
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"),
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    cfg = load_config()
    app.state.cfg = cfg
    app.state.orch = Orchestrator(cfg)
    log.info("TV %s HDMI %s, STB driver %s (%s), %d channels",
             cfg["tv"]["ip"], cfg["tv"]["hdmi_port"], cfg["stb"]["driver"],
             cfg["stb"]["power_strategy"], len(cfg["channels"]))
    yield
    await app.state.orch.aclose()


app = FastAPI(title="TV launcher", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)


def _orch(request: Request) -> Orchestrator:
    return request.app.state.orch


@app.post("/api/tune/{number}", status_code=202)
async def api_tune(number: str, request: Request):
    if not re.fullmatch(r"\d{1,4}", number):
        raise HTTPException(400, "channel must be 1-4 digits")
    return _orch(request).submit(number, "ui").public()


@app.post("/api/off", status_code=202)
async def api_off(request: Request):
    return _orch(request).submit("off", "ui").public()


@app.post("/api/volume/{direction}")
async def api_volume(direction: str, request: Request):
    if direction not in ("up", "down"):
        raise HTTPException(400, "up or down")
    o = _orch(request)
    try:
        await o.tv.volume_step(2 if direction == "up" else -2, o.cfg["tv"]["volume_target"])
    except (TvUnreachable, BraviaError) as e:
        raise HTTPException(503, str(e))
    return {"ok": True}


@app.get("/api/job/{job_id}")
async def api_job(job_id: str, request: Request):
    job = _orch(request).jobs.get(job_id)
    if not job:
        raise HTTPException(404, "unknown job")
    return job.public()


@app.get("/api/current")
async def api_current(request: Request):
    cur = _orch(request).current
    return cur.public() if cur else {}


@app.get("/api/health")
async def api_health(request: Request):
    o = _orch(request)
    tv: dict[str, Any] = {"ip": o.tv.ip}
    try:
        tv["power"] = await o.tv.power_status()
        if tv["power"] == "active":
            tv["uri"] = await o.tv.playing_uri()
    except Exception as e:
        tv["error"] = f"{type(e).__name__}: {e}"
    return {"tv": tv, "stb": await o.stb.health(), "timings": o.timings.v,
            "current": o.current.public() if o.current else None}


@app.api_route("/api/button/{button_id}", methods=["GET", "POST"])
async def api_button(button_id: str, request: Request, press: str = "single", token: str = ""):
    """Smart-button webhook. Shelly Button1 action URL example:
    http://<server>:8000/api/button/b1?press=single&token=<webhook_token>"""
    cfg, o = request.app.state.cfg, _orch(request)
    want = cfg["server"]["webhook_token"]
    if want and not secrets.compare_digest(token.encode(), want.encode()):
        raise HTTPException(403, "bad token")
    if request.method == "POST" and "json" in request.headers.get("content-type", ""):
        try:
            press = str((await request.json()).get("press", press))
        except ValueError:
            pass
    target = cfg["buttons"].get(button_id, {}).get(press)
    if target is None:
        raise HTTPException(404, f"no mapping for button {button_id!r} press {press!r}")
    if target in ("vol_up", "vol_down"):
        try:
            await o.tv.volume_step(2 if target == "vol_up" else -2, cfg["tv"]["volume_target"])
        except (TvUnreachable, BraviaError) as e:
            raise HTTPException(503, str(e))
        return {"ok": True}
    return JSONResponse(o.submit(target, f"button:{button_id}:{press}").public(), status_code=202)


@app.get("/logos/{name}")
async def logo(name: str, request: Request):
    p = Path(request.app.state.cfg["_dir"]) / "logos" / name
    if not re.fullmatch(r"[\w.-]+\.(png|jpe?g|webp|svg)", name) or not p.is_file():
        raise HTTPException(404)
    return FileResponse(p, headers={"Cache-Control": "max-age=86400"})


@app.get("/manifest.webmanifest")
async def manifest(request: Request):
    title = request.app.state.cfg["server"]["title"]
    return JSONResponse({
        "name": title, "short_name": "TV", "start_url": "/", "scope": "/", "display": "fullscreen",
        "orientation": "any", "background_color": "#000000", "theme_color": "#000000",
        "icons": [{"src": "/icon.svg", "sizes": "any", "type": "image/svg+xml", "purpose": "any"}],
    }, media_type="application/manifest+json")


@app.get("/icon.svg")
async def icon():
    return Response(ICON_SVG, media_type="image/svg+xml")


@app.get("/sw.js")
async def sw():
    return Response(SW_JS, media_type="application/javascript", headers={"Cache-Control": "no-cache"})


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    cfg = request.app.state.cfg
    ui = {"title": cfg["server"]["title"], "help": cfg["server"]["help_text"],
          "channels": [{"number": c["number"], "name": c["name"], "color": c["color"],
                        "logo": f"/logos/{c['logo']}" if c["logo"] else ""} for c in cfg["channels"]]}
    data = json.dumps(ui, ensure_ascii=False).replace("</", "<\\/")
    title = cfg["server"]["title"].replace("&", "&amp;").replace("<", "&lt;")
    return HTMLResponse(INDEX_HTML.replace("__CFG__", data).replace("__TITLE__", title),
                        headers={"Cache-Control": "no-cache"})


ICON_SVG = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 512 512">
<rect width="512" height="512" rx="96" fill="#000"/>
<rect x="56" y="120" width="400" height="260" rx="28" fill="none" stroke="#fff" stroke-width="32"/>
<path d="M176 450h160M256 380v70" stroke="#fff" stroke-width="32" stroke-linecap="round"/>
<path d="M220 190v120l100-60z" fill="#FFD600"/></svg>"""

SW_JS = """const C = 'tv-shell-v1';
self.addEventListener('install', e => { e.waitUntil(caches.open(C).then(c => c.addAll(['/', '/manifest.webmanifest', '/icon.svg']))); self.skipWaiting(); });
self.addEventListener('activate', e => e.waitUntil(self.clients.claim()));
self.addEventListener('fetch', e => {
  const u = new URL(e.request.url);
  if (e.request.method !== 'GET' || u.pathname.startsWith('/api/')) return;
  e.respondWith(fetch(e.request).then(r => { const cp = r.clone(); caches.open(C).then(c => c.put(e.request, cp)); return r; })
    .catch(() => caches.match(e.request)));
});"""

INDEX_HTML = """<!doctype html>
<html lang="hi">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=1, user-scalable=no, viewport-fit=cover">
<meta name="theme-color" content="#0b0c0e">
<meta name="mobile-web-app-capable" content="yes">
<link rel="manifest" href="/manifest.webmanifest">
<link rel="icon" href="/icon.svg">
<title>__TITLE__</title>
<style>
  :root { --gap: clamp(12px, 2.2vw, 22px); --ink: #fff; --ink-soft: rgba(255,255,255,.82);
    --surface: #1b1d21; --edge: rgba(255,255,255,.22); --focus: #ffd600;
    --ease: cubic-bezier(.16, 1, .3, 1); }
  * { box-sizing: border-box; -webkit-tap-highlight-color: transparent; }
  html, body { margin: 0; height: 100%; background: #0b0c0e; color: var(--ink);
    font-family: system-ui, "Noto Sans Devanagari", "Noto Sans", Roboto, sans-serif;
    -webkit-user-select: none; user-select: none; overscroll-behavior: none; touch-action: manipulation; }
  body { display: flex; flex-direction: column; gap: var(--gap); padding: var(--gap);
    padding-bottom: max(var(--gap), env(safe-area-inset-bottom)); }
  ::selection { background: var(--focus); color: #000; }
  #grid { flex: 1; min-height: 0; overflow-y: auto; display: grid; gap: var(--gap); padding: 4px;
    grid-template-columns: repeat(auto-fit, minmax(min(100%, 240px), 1fr)); grid-auto-rows: minmax(160px, 1fr);
    scrollbar-width: thin; scrollbar-color: var(--edge) transparent; }
  button { font-family: inherit; color: var(--ink); cursor: pointer; border: 0;
    transition: transform .18s var(--ease), box-shadow .18s var(--ease), filter .18s var(--ease); }
  button:focus-visible { outline: 4px solid var(--focus); outline-offset: 4px; }
  .card { border-radius: 28px; padding: 14px; display: flex; flex-direction: column;
    align-items: center; justify-content: center; gap: 10px; text-align: center;
    box-shadow: inset 0 0 0 3px var(--edge), 0 10px 24px rgba(0,0,0,.55), 0 2px 4px rgba(0,0,0,.6); }
  .card:active, #bar button:active { transform: scale(.97); filter: brightness(1.25);
    box-shadow: inset 0 0 0 3px var(--edge), 0 3px 8px rgba(0,0,0,.6); }
  @media (hover: hover) { .card:hover, #bar button:hover { filter: brightness(1.12); } }
  .card img { max-height: 45%; max-width: 80%; object-fit: contain; background: #fff; border-radius: 16px; padding: 8px; }
  .name { font-size: clamp(30px, 6vw, 64px); font-weight: 800; line-height: 1.1; text-wrap: balance; }
  .num { font-size: clamp(22px, 4vw, 40px); font-weight: 700; letter-spacing: .08em; color: var(--ink-soft);
    font-variant-numeric: tabular-nums; }
  #bar { display: grid; grid-template-columns: 2fr 1fr 1fr; gap: var(--gap); }
  #bar button { min-height: 110px; border-radius: 24px; background: var(--surface);
    font-size: clamp(26px, 5vw, 48px); font-weight: 800; display: flex; align-items: center;
    justify-content: center; gap: .35em; box-shadow: inset 0 0 0 3px var(--edge), 0 8px 18px rgba(0,0,0,.5); }
  #bar button svg { width: 1.1em; height: 1.1em; flex: none; fill: none; stroke: currentColor;
    stroke-width: 2.4; stroke-linecap: round; stroke-linejoin: round; }
  #bar #off { background: #7a1414; }
  #ov { position: fixed; inset: 0; z-index: 10; display: none; flex-direction: column; align-items: center;
    justify-content: center; gap: 2vh; padding: 4vw; text-align: center; }
  #ov.show { display: flex; animation: rise .35s var(--ease); }
  @keyframes rise { from { opacity: 0; transform: scale(1.03); } }
  #ov-name { font-size: clamp(40px, 9vw, 110px); font-weight: 900; line-height: 1.05; text-wrap: balance; }
  #ov-count { font-size: clamp(120px, 34vh, 360px); font-weight: 900; line-height: 1; font-variant-numeric: tabular-nums; }
  .row { font-size: clamp(26px, 5vw, 52px); font-weight: 700; min-height: 1.2em; }
  #ov-tech { font-size: 16px; color: var(--ink-soft); max-width: 90vw; word-break: break-word; }
  #down { position: fixed; inset: 0; z-index: 20; background: #7a1414; display: none; flex-direction: column;
    align-items: center; justify-content: center; text-align: center; padding: 6vw; gap: 3vh; }
  #down.show { display: flex; }
  #down div { font-size: clamp(30px, 7vw, 72px); font-weight: 900; line-height: 1.15; text-wrap: balance; }
  @media (prefers-reduced-motion: reduce) {
    *, *::before, *::after { animation: none !important; transition: none !important; }
  }
</style>
</head>
<body>
<main id="grid" aria-label="चैनल"></main>
<nav id="bar" aria-label="टीवी नियंत्रण">
  <button id="off"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 3v9M6.3 6.9a8 8 0 1 0 11.4 0"/></svg>टीवी बंद</button>
  <button id="vdown" aria-label="आवाज़ कम"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M4 9.5v5h3.5L12 18.5v-13L7.5 9.5zM16 9.5a3.5 3.5 0 0 1 0 5"/></svg>−</button>
  <button id="vup" aria-label="आवाज़ ज़्यादा"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M3 9.5v5h3.5L11 18.5v-13L6.5 9.5zM15 9.5a3.5 3.5 0 0 1 0 5M17.5 6.5a7.5 7.5 0 0 1 0 11"/></svg>+</button>
</nav>
<section id="ov" role="status" aria-live="polite">
  <div id="ov-name"></div>
  <div id="ov-count"></div>
  <div class="row" id="ov-tv"></div>
  <div class="row" id="ov-stb"></div>
  <div id="ov-tech"></div>
</section>
<section id="down" role="alert"><div>सर्वर से संपर्क नहीं</div><div id="help"></div></section>
<script>
const CFG = __CFG__;
const T = {
  tv:  { pending: 'टीवी…', waking: 'टीवी चालू हो रहा है…', input: 'टीवी तैयार हो रहा है…',
         settle: 'टीवी तैयार हो रहा है…', off: 'टीवी बंद हो रहा है…', ok: 'टीवी ✓' },
  stb: { pending: 'चैनल…', waking: 'बॉक्स चालू हो रहा है…', tuning: 'चैनल लग रहा है…', ok: 'चैनल ✓', skip: '' },
};
const $ = id => document.getElementById(id);
let following = null, base = 0, eta = 0, tick = null, poll = null, fails = 0, hideT = null;

function buzz(p) { try { navigator.vibrate && navigator.vibrate(p || 60); } catch (e) {} }
// Every request needs a deadline. When the server's machine is off or unplugged the network drops
// the packets silently instead of refusing, and a bare fetch() then waits for minutes: taps would
// hang on the dots and the "server down" screen (which relies on failed heartbeats) would never show.
async function req(path, method, ms) {
  const ac = new AbortController(), t = setTimeout(() => ac.abort(), ms || 6000);
  try {
    const r = await fetch(path, { method: method || 'GET', cache: 'no-store', signal: ac.signal });
    if (!r.ok) throw new Error(r.status + ' ' + (await r.text()));
    return await r.json();
  } catch (e) {
    throw e.name === 'AbortError' ? new Error('no answer from the server') : e;
  } finally { clearTimeout(t); }
}
function colorFor(job) {
  if (job.target === 'off') return '#263238';
  const ch = CFG.channels.find(c => c.number === job.target);
  return ch ? ch.color : '#0D47A1';
}

function render() {
  document.title = CFG.title;
  $('help').textContent = CFG.help || '';
  for (const ch of CFG.channels) {
    const b = document.createElement('button');
    b.className = 'card'; b.style.background = ch.color; b.setAttribute('aria-label', ch.name + ', ' + ch.number);
    if (ch.logo) { const i = document.createElement('img'); i.src = ch.logo; i.alt = ''; b.appendChild(i); }
    const n = document.createElement('div'); n.className = 'name'; n.textContent = ch.name; b.appendChild(n);
    const d = document.createElement('div'); d.className = 'num'; d.textContent = ch.number; b.appendChild(d);
    b.addEventListener('click', () => start('/api/tune/' + ch.number, ch.name, ch.color));
    $('grid').appendChild(b);
  }
  $('off').addEventListener('click', () => start('/api/off', 'टीवी बंद', '#263238'));
  $('vup').addEventListener('click', () => vol('up'));
  $('vdown').addEventListener('click', () => vol('down'));
  $('ov').addEventListener('click', () => { if (following === 'failed') hide(); });
}

function show(title, color) {
  clearTimeout(hideT);
  $('ov').style.background = color; $('ov-name').textContent = title; $('ov-count').textContent = '…';
  $('ov-tv').textContent = ''; $('ov-stb').textContent = ''; $('ov-tech').textContent = '';
  $('ov').className = 'show';
}
function hide() { $('ov').className = ''; following = null; clearInterval(tick); clearTimeout(poll); }

async function start(path, title, color) {
  if (following) return;          // one thing at a time; the overlay covers the cards anyway
  buzz(); following = 'pending'; show(title, color);
  try { follow(await req(path, 'POST')); }
  catch (e) {
    // After a server restart the browser's first request can go down a dead keep-alive socket and
    // fail (measured on a phone: the first tap failed, the second worked). Tune and off are safe to
    // repeat (the same target just joins the running job), so retry once before showing a failure.
    await new Promise(r => setTimeout(r, 400));
    try { follow(await req(path, 'POST')); } catch (e2) { fail(e2.message); }
  }
}
function follow(job) {
  following = job.id; fails = 0;
  clearInterval(tick); tick = setInterval(draw, 200);
  apply(job);
  if (job.state === 'running') schedule();
}
function apply(job) {
  base = performance.now() - job.elapsed_s * 1000; eta = job.eta_s;
  $('ov-tv').textContent = T.tv[job.tv] || '';
  $('ov-stb').textContent = T.stb[job.stb] || '';
  draw();
  if (job.state === 'done') ok();
  else if (job.state === 'failed') fail(job.message);
  else if (job.state === 'superseded') adopt();
}
function draw() {
  if (!following || following === 'failed') return;
  $('ov-count').textContent = Math.max(1, Math.ceil(eta - (performance.now() - base) / 1000));
}
function schedule() {
  clearTimeout(poll);
  poll = setTimeout(async () => {
    const id = following;
    if (!id || id === 'failed' || id === 'pending') return;
    try {
      const j = await req('/api/job/' + id); fails = 0;
      if (following !== id) return;
      apply(j); if (j.state === 'running') schedule();
    } catch (e) { if (++fails > 8) fail('सर्वर से जवाब नहीं'); else schedule(); }
  }, 450);
}
function ok() {
  clearInterval(tick); clearTimeout(poll);
  $('ov-count').textContent = '✓'; buzz([80, 60, 80]);
  hideT = setTimeout(hide, 2500);
}
function fail(msg) {
  clearInterval(tick); clearTimeout(poll); following = 'failed';
  $('ov').style.background = '#7f0000'; $('ov-count').textContent = '✗';
  $('ov-tv').textContent = 'नहीं हुआ — फिर से दबाएँ'; $('ov-stb').textContent = '';
  $('ov-tech').textContent = msg || '';
  buzz([300, 100, 300]); hideT = setTimeout(hide, 8000);
}
async function adopt() {          // a smart button replaced our job: follow the new one
  try { const j = await req('/api/current'); if (j.id) { show(j.label, colorFor(j)); follow(j); } else hide(); }
  catch (e) { hide(); }
}
async function vol(dir) { buzz(30); try { await req('/api/volume/' + dir, 'POST'); } catch (e) {} }

// Background: show progress for button-initiated jobs, and a clear "server down" screen.
let downCount = 0;
async function heartbeat() {
  try {
    const j = await req('/api/current', 'GET', 3000); downCount = 0; $('down').className = '';
    if (!following && j.id && j.state === 'running') { show(j.label, colorFor(j)); follow(j); }
  } catch (e) { if (++downCount >= 3) $('down').className = 'show'; }
  setTimeout(heartbeat, 3000);
}
async function keepAwake() { try { if ('wakeLock' in navigator) await navigator.wakeLock.request('screen'); } catch (e) {} }
document.addEventListener('visibilitychange', () => { if (document.visibilityState === 'visible') keepAwake(); });
document.addEventListener('contextmenu', e => e.preventDefault());
if ('serviceWorker' in navigator && window.isSecureContext) navigator.serviceWorker.register('/sw.js').catch(() => {});
render(); keepAwake(); heartbeat();
</script>
</body>
</html>
"""


if __name__ == "__main__":
    import uvicorn

    _cfg = load_config()
    uvicorn.run(app, host=_cfg["server"]["host"], port=_cfg["server"]["port"])
