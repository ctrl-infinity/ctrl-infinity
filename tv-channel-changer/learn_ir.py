#!/usr/bin/env python3
"""
learn_ir.py: capture YOUR Airtel remote's IR codes into ir_codes.json.

Airtel doesn't publish its IR codes, and they differ between remote/box generations, so
don't copy codes off the internet. Learn them from the remote that came with the box.

  # Broadlink RM4 Mini (put it on Wi-Fi with no Broadlink cloud, once, while it's in AP mode):
  python learn_ir.py broadlink --setup-wifi "MySSID" "MyPassword"
  python learn_ir.py broadlink --host 192.168.1.40
  python learn_ir.py broadlink --host 192.168.1.40 --test 1      # fire one learned key

  # ESP32 running esp32_ir_blaster.ino (with the TSOP receiver fitted):
  python learn_ir.py esp32 --url http://192.168.1.41
  python learn_ir.py esp32 --url http://192.168.1.41 --test exit

The default key set is 0-9, exit, ok, back, power. Use --keys to learn a subset again.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

DEFAULT_KEYS = [str(d) for d in range(10)] + ["exit", "ok", "back", "power"]
HINT = {"exit": "EXIT", "ok": "OK / SELECT", "back": "BACK", "power": "POWER (red)"}


def load(out: Path) -> dict:
    return json.loads(out.read_text()) if out.is_file() else {}


def save(out: Path, codes: dict) -> None:
    out.write_text(json.dumps(codes, indent=2, sort_keys=True))
    print(f"saved {len(codes)} keys to {out}")


# -- Broadlink -------------------------------------------------------------
def broadlink_dev(host: str):
    import broadlink
    dev = broadlink.hello(host)
    dev.auth()
    return dev


def broadlink_learn(dev, key: str, timeout: float = 20.0) -> str:
    from broadlink.exceptions import ReadError, StorageError
    dev.enter_learning()
    print(f"  press [{HINT.get(key, key)}] on the Airtel remote, 5-10 cm from the RM4 ...", flush=True)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        time.sleep(0.5)
        try:
            return dev.check_data().hex()
        except (ReadError, StorageError):
            continue
    raise TimeoutError(f"nothing received for {key}")


# -- ESP32 -----------------------------------------------------------------
def http_json(url: str, data: bytes | None = None) -> dict:
    with urllib.request.urlopen(urllib.request.Request(url, data=data), timeout=5) as r:
        return json.loads(r.read() or b"{}")


def esp32_capture(base: str, key: str, timeout: float = 20.0) -> dict:
    http_json(f"{base}/learn?clear=1")
    print(f"  press [{HINT.get(key, key)}] on the Airtel remote, aimed at the ESP32's receiver ...", flush=True)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        time.sleep(0.3)
        got = http_json(f"{base}/learn")
        if got.get("proto"):
            return got
    raise TimeoutError(f"nothing received for {key}")


def esp32_learn(base: str, key: str) -> dict:
    # Capture twice and require a match: catches half-received frames and remotes that alternate toggle bits.
    a = esp32_capture(base, key)
    if a["proto"] == "UNKNOWN":
        print(f"    protocol not decoded; storing raw timings ({len(a['raw'])} pulses)")
        return {"raw": a["raw"], "hz": 38000}
    print(f"    {a['proto']} {a['code']} ({a['bits']} bits). Press it once more to confirm.")
    b = esp32_capture(base, key)
    if (a["proto"], a["code"], a["bits"]) != (b["proto"], b["code"], b["bits"]):
        if a["proto"] in ("RC5", "RC6", "RC5X"):
            print("    RC5/RC6 toggle bit differs between presses; that's expected, keeping the first")
        else:
            raise ValueError(f"captures differ: {a} vs {b}; aim better and retry")
    return {"proto": a["proto"], "code": a["code"], "bits": a["bits"]}


def esp32_send(base: str, c: dict) -> None:
    if "raw" in c:
        url = f"{base}/raw?hz={c.get('hz', 38000)}"
        http_json(url, ",".join(map(str, c["raw"])).encode())
    else:
        http_json(f"{base}/ir?" + urllib.parse.urlencode(
            {"proto": c["proto"], "code": c["code"], "bits": c["bits"], "repeat": c.get("repeat", 0)}))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("backend", choices=["broadlink", "esp32"])
    ap.add_argument("--host", help="Broadlink IP")
    ap.add_argument("--url", help="ESP32 base URL")
    ap.add_argument("--out", default=str(Path(__file__).with_name("ir_codes.json")))
    ap.add_argument("--keys", nargs="+", default=DEFAULT_KEYS)
    ap.add_argument("--test", metavar="KEY", help="send one already-learned key and exit")
    ap.add_argument("--setup-wifi", nargs=2, metavar=("SSID", "PASSWORD"),
                    help="Broadlink only: provision a factory-reset RM4 (in AP mode) onto your Wi-Fi, no cloud")
    a = ap.parse_args()
    out = Path(a.out)
    codes = load(out)

    if a.backend == "broadlink":
        if a.setup_wifi:
            import broadlink
            print("Join your laptop to the RM4's own 'BroadlinkProv' Wi-Fi first (long-press reset until fast blink).")
            broadlink.setup(a.setup_wifi[0], a.setup_wifi[1], 4)  # 4 = WPA/WPA2
            print("Sent. Give the RM4 a fixed DHCP lease on your router and note its IP. Never add it to the "
                  "Broadlink app: newer firmware then locks local control.")
            return 0
        if not a.host:
            ap.error("--host is required")
        dev = broadlink_dev(a.host)
        if a.test:
            dev.send_data(bytes.fromhex(codes[a.test]))
            print(f"sent {a.test}")
            return 0
        for key in a.keys:
            codes[key] = broadlink_learn(dev, key)
            print(f"    ok ({len(codes[key]) // 2} bytes)")
            save(out, codes)
    else:
        if not a.url:
            ap.error("--url is required")
        base = a.url.rstrip("/")
        if a.test:
            esp32_send(base, codes[a.test])
            print(f"sent {a.test}")
            return 0
        for key in a.keys:
            codes[key] = esp32_learn(base, key)
            save(out, codes)
    print("Done. Next: python learn_ir.py ... --test 1   (the box should show '1 _ _')")
    return 0


if __name__ == "__main__":
    sys.exit(main())
