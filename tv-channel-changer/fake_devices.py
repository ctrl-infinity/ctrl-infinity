#!/usr/bin/env python3
"""
fake_devices.py: a LAN-free simulator of a Bravia TV, an ESP32 IR blaster with an Airtel box
behind it, and a Tasmota plug. Use it to work on the UI and state machine without the real TV.

  python fake_devices.py                       # serves on 127.0.0.1:8081
  LAUNCHER_CONFIG=fake_config.toml python launcher.py
  curl -s localhost:8081/fake/state             # what the "TV" and "box" are doing

POST /fake/set {"tv": "standby", "stb_on": false, "stb_menu": true} to force mismatched states.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response

PSK = "0000"
TV_BOOT_S = 4.0            # standby -> active
TV_INPUT_READY_S = 1.5     # after active, setPlayContent fails with error 40005 for this long
STB_BOOT_S = 3.0
DIGIT_TIMEOUT_S = 1.5      # box commits an entered number after this much silence
MIN_GAP_S = 0.12           # digits closer than this are dropped (IR frames collide)

app = FastAPI()
S = {
    "tv": "standby", "tv_wake_at": 0.0, "uri": "extInput:hdmi?port=1", "volume": 20,
    "stb_on": True, "stb_boot_until": 0.0, "stb_menu": False, "channel": "100",
    "entry": "", "last_digit": 0.0, "relay": True, "log": [],
}
CODES = json.loads((Path(__file__).with_name("fake_ir_codes.json")).read_text())
BY_CODE = {v["code"].lower(): k for k, v in CODES.items()}
IRCC = {f"Num{i}": f"AAAAAQAAAAEAAAA{i}Aw==" for i in range(10)} | {"Home": "AAAAAQAAAAEAAABgAw==",
                                                                     "Exit": "AAAAAQAAAAEAAABjAw=="}


def _tick() -> None:
    now = time.monotonic()
    if S["tv"] == "booting" and now >= S["tv_wake_at"]:
        S["tv"] = "active"
    if S["entry"] and now - S["last_digit"] > DIGIT_TIMEOUT_S:
        S["channel"], S["entry"] = S["entry"], ""
        S["log"].append(f"tuned {S['channel']}")


def _stb_key(key: str) -> None:
    _tick()
    now = time.monotonic()
    if key == "power":
        S["stb_on"] = not S["stb_on"]
        S["stb_boot_until"] = now + STB_BOOT_S if S["stb_on"] else 0
        S["entry"] = ""
        return
    if not S["stb_on"] or now < S["stb_boot_until"]:
        return
    if key in ("exit", "back"):
        S["stb_menu"], S["entry"] = False, ""
    elif key.isdigit() and not S["stb_menu"]:
        if S["entry"] and now - S["last_digit"] < MIN_GAP_S:
            S["log"].append(f"dropped digit {key} (gap too short)")
            return
        S["entry"] = (S["entry"] + key)[-3:]
        S["last_digit"] = now


def _err(code: int, msg: str) -> JSONResponse:
    return JSONResponse({"error": [code, msg], "id": 1})


@app.post("/sony/{service}")
async def sony(service: str, request: Request):
    _tick()
    if request.headers.get("x-auth-psk") != PSK:
        return Response(status_code=403)
    if service == "IRCC":
        body = (await request.body()).decode()
        code = body.split("<IRCCCode>")[1].split("</IRCCCode>")[0]
        name = next(k for k, v in IRCC.items() if v == code)
        if name == "Home":
            S["uri"] = None
        return Response("<ok/>", media_type="text/xml")  # this box ignores CEC keys
    req = await request.json()
    m, p = req["method"], (req["params"] or [{}])[0]
    if m == "getPowerStatus":
        return {"result": [{"status": "active" if S["tv"] == "active" else "standby"}], "id": req["id"]}
    if m == "setPowerStatus":
        if p["status"] and S["tv"] == "standby":
            S["tv"], S["tv_wake_at"] = "booting", time.monotonic() + TV_BOOT_S
        elif not p["status"]:
            S["tv"] = "standby"
        return {"result": [], "id": req["id"]}
    if m == "getInterfaceInformation":
        return {"result": [{"modelName": "FAKE-55X80K", "interfaceVersion": "6.4.0"}]}
    if m == "getSystemInformation":
        return {"result": [{"model": "FAKE-55X80K", "macAddr": "aa:bb:cc:dd:ee:ff"}]}
    if m == "getRemoteControllerInfo":
        return {"result": [{"bundled": True}, [{"name": k, "value": v} for k, v in IRCC.items()]]}
    if S["tv"] != "active":
        return _err(40005, "Display Is Turned off")
    if m == "getPlayingContentInfo":
        return {"result": [{"uri": S["uri"], "source": "extInput:hdmi"}]} if S["uri"] else _err(7, "Illegal State")
    if m == "setPlayContent":
        if time.monotonic() < S["tv_wake_at"] + TV_INPUT_READY_S:
            return _err(40005, "not ready")
        S["uri"] = p["uri"]
        return {"result": [], "id": req["id"]}
    if m == "getCurrentExternalInputsStatus":
        return {"result": [[{"uri": f"extInput:hdmi?port={i}", "title": f"HDMI {i}", "label": "",
                             "connection": i == 2, "icon": "meta:hdmi"} for i in range(1, 5)]]}
    if m == "setAudioVolume":
        S["volume"] += int(p["volume"])
        return {"result": [0]}
    return _err(12, "No Such Method")


@app.get("/ir")
async def ir(proto: str, code: str, bits: int, repeat: int = 0):
    key = BY_CODE.get(code.lower())
    if S["relay"] and key:
        _stb_key(key)
    return {"ok": True}


@app.get("/health")
async def health():
    return {"ok": True, "fake": True}


@app.get("/cm")
async def tasmota(cmnd: str):
    if cmnd == "Status 8":
        w = 0.0 if not S["relay"] else (9.0 if S["stb_on"] else 1.5)
        return {"StatusSNS": {"ENERGY": {"Power": w}}}
    if cmnd.startswith("Power"):
        if cmnd.endswith("On"):
            S["relay"] = True
        elif cmnd.endswith("Off"):
            S["relay"] = False
        return {"POWER": "ON" if S["relay"] else "OFF"}
    return {}


@app.get("/fake/state")
async def state():
    _tick()
    return {k: v for k, v in S.items() if k not in ("tv_wake_at", "last_digit", "stb_boot_until")}


@app.post("/fake/set")
async def set_state(request: Request):
    S.update(await request.json())
    return await state()


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8081, log_level="warning")
