"""Throwaway probe: pair with the Xstream box via Android TV Remote v2, then send digit keys.

  python atv_probe.py pair            -> starts pairing; waits for code in code.txt next to this file
  python atv_probe.py send 7 2 3 0.3  -> sends digits with gap seconds between them (last arg = gap)
"""
import asyncio
import sys
from pathlib import Path

from androidtvremote2 import AndroidTVRemote, CannotConnect, InvalidAuth

HERE = Path(__file__).parent
HOST = "192.168.1.5"
CERT, KEY = str(HERE / "cert.pem"), str(HERE / "key.pem")
CODE_FILE = HERE / "code.txt"


async def pair(remote: AndroidTVRemote) -> None:
    await remote.async_start_pairing()
    print("PAIRING STARTED: read the code on the TV, then I will write it to code.txt", flush=True)
    for _ in range(300):
        if CODE_FILE.exists() and CODE_FILE.read_text().strip():
            break
        await asyncio.sleep(1)
    else:
        print("timed out waiting for code")
        return
    await remote.async_finish_pairing(CODE_FILE.read_text().strip())
    print("PAIRED OK", flush=True)


async def main() -> None:
    mode = sys.argv[1]
    remote = AndroidTVRemote("tvlauncher-probe", CERT, KEY, HOST)
    if await remote.async_generate_cert_if_missing():
        print("generated new client cert")
    name, mac = await remote.async_get_name_and_mac()
    print(f"box: {name} {mac}", flush=True)

    if mode == "pair":
        await pair(remote)
        return

    try:
        await remote.async_connect()
    except InvalidAuth:
        print("not paired yet: run `pair` first")
        return
    except CannotConnect as e:
        print("cannot connect:", e)
        return
    print("connected; is_on =", remote.is_on, " current_app =", remote.current_app, flush=True)
    gap = float(sys.argv[-1])
    for d in sys.argv[2:-1]:
        remote.send_key_command(7 + int(d))  # KEYCODE_0..9 = 7..16
        print("sent", d, flush=True)
        await asyncio.sleep(gap)
    await asyncio.sleep(1)
    remote.disconnect()


asyncio.run(main())
