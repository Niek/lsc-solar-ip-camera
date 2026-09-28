#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = ["tuya-mobile==1.2.0"]
# ///
"""Download a Smart Life camera OTA. Never tells the camera to install it."""
import argparse
import asyncio
import base64
import getpass
import hashlib
import json
from pathlib import Path
import re
import time
from urllib.parse import urlencode, urljoin

import aiohttp
from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from tuya_mobile import TuyaMobileApp, TuyaPasswordClient


class Error(Exception):
    pass


def unwrap(value):
    while isinstance(value, dict):
        if value.get("success") is False or value.get("errorCode"):
            raise Error("Tuya rejected the request.")
        if "result" not in value:
            break
        value = value["result"]
    return value


async def device_call(client, device_id, key, action, version, body):
    """Device HTTP protocol; mobile session tokens cannot sign these calls."""
    params = dict(a=action, et=1, t=int(time.time()), devId=device_id, v=version)
    query = urlencode(sorted(params.items()))
    params["sign"] = hashlib.md5((query.replace("&", "||") + "||" + key).encode()).hexdigest()
    pad = padding.PKCS7(128).padder()
    plain = json.dumps(dict(body, t=params["t"]), separators=(",", ":")).encode()
    encrypt = Cipher(algorithms.AES(key.encode()), modes.ECB()).encryptor()
    data = encrypt.update(pad.update(plain) + pad.finalize()) + encrypt.finalize()
    async with client.session.post(
        urljoin(client.mobile_url, "d.json"), params=params,
        data={"data": data.hex().upper()}, headers={"User-Agent": "TUYA_IOT_SDK"},
        allow_redirects=False, timeout=aiohttp.ClientTimeout(total=20),
    ) as response:
        response.raise_for_status()
        envelope = await response.json(content_type=None)
    if isinstance(envelope.get("result"), str):
        decrypt = Cipher(algorithms.AES(key.encode()), modes.ECB()).decryptor()
        plain = decrypt.update(base64.b64decode(envelope["result"])) + decrypt.finalize()
        unpad = padding.PKCS7(128).unpadder()
        envelope = json.loads(unpad.update(plain) + unpad.finalize())
    if envelope.get("success") is not True:
        raise Error("Tuya rejected the device request.")
    return unwrap(envelope)


async def fallback(client, device_id, current, journal):
    credentials = await client.get_device_credentials(device_id)

    async def report(version):
        await device_call(client, device_id, credentials.sec_key,
                          "tuya.device.versions.update", "4.1",
                          {"versions": json.dumps([{"otaChannel": 0, "softVer": version}])})

    if journal.exists():
        state = json.loads(journal.read_text())
        await report(state["original_version"])
        journal.unlink()
        raise Error("Restored the interrupted version report. Run again to query OTA.")
    if not current or not re.fullmatch(r"\d+(?:\.\d+)+", current):
        raise Error("Cannot determine the original version; refusing to change it.")
    branch, patch = current.rsplit(".", 1)
    if int(patch) == 0:
        raise Error(f"Cannot report an older patch version within {branch} for {current}.")
    reported = f"{branch}.{int(patch) - 1}"
    journal.parent.mkdir(parents=True, exist_ok=True)
    with journal.open("x") as output:
        journal.chmod(0o600)
        json.dump({"device_id": device_id, "original_version": current}, output)
    try:
        print(f"Temporarily reporting {reported} (original: {current}).")
        await report(reported)
        return await device_call(client, device_id, credentials.sec_key,
                                 "tuya.device.upgrade.get", "4.4", {"type": 0})
    finally:
        try:
            await report(current)
        except BaseException:
            print(f"RESTORE PENDING: rerun this command with the same output directory ({journal.parent}).")
            raise
        journal.unlink()
        print(f"Restored reported version to {current}.")


async def run(args):
    password = getpass.getpass("Smart Life password: ")
    journal = args.output / "restore.json"
    async with aiohttp.ClientSession() as session:
        client = TuyaPasswordClient.for_application(
            TuyaMobileApp.SMART_LIFE, session, username=args.email, max_login_attempts=1)
        await client.login_with_password(password, country_code=args.country)
        del password
        devices = await client.get_local_keys([])
        wanted = json.loads(journal.read_text())["device_id"] if journal.exists() else args.device
        if wanted:
            device = next((d for d in devices if d["device_id"] == wanted), None)
            if device is None:
                raise Error("Device not found in this account.")
        else:
            if not devices:
                raise Error("No devices found in this account.")
            for index, device in enumerate(devices, 1):
                print(f"{index}. {device.get('name') or 'Unnamed device'}")
            choice = input("Camera number: ")
            if not choice.isdigit() or not 1 <= int(choice) <= len(devices):
                raise Error("Invalid camera number.")
            device = devices[int(choice) - 1]
        device_id = device["device_id"]
        if journal.exists():
            await fallback(client, device_id, None, journal)
        metadata = unwrap(await client._call(
            "thing.m.device.upgrade.info", {"devId": device_id}, version="1.2"))
        channels = metadata if isinstance(metadata, list) else [metadata]
        offer = next((c for c in channels if isinstance(c, dict) and c.get("type") == 0), {})
        current = offer.get("currentVersion")
        print(f"Current firmware: {current or 'unknown'}")
        if not (offer.get("httpsUrl") or offer.get("url")):
            offer = await fallback(client, device_id, current, journal) or {}
        url = offer.get("httpsUrl") or offer.get("url")
        if not url:
            raise Error("Tuya offered no OTA, even with an older version report.")
        if not url.startswith("https://"):
            raise Error("Tuya did not return an HTTPS download URL.")
        version = str(offer.get("version", "unknown"))
        target = args.output / ("firmware-" + re.sub(r"[^\w.-]", "_", version) + ".bin")
        if target.exists():
            raise Error(f"Already exists: {target}")
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=180)) as response:
            response.raise_for_status()
            data = await response.read()
        expected_size = offer.get("fileSize") or offer.get("size")
        if expected_size and len(data) != int(expected_size):
            raise Error("Downloaded size does not match Tuya's metadata.")
        if offer.get("md5") and hashlib.md5(data).hexdigest() != offer["md5"].lower():
            raise Error("Downloaded MD5 does not match Tuya's metadata.")
        args.output.mkdir(parents=True, exist_ok=True)
        with target.open("xb") as output:
            output.write(data)
        print(f"Firmware: {version}\nFile: {target}\nSHA-256: {hashlib.sha256(data).hexdigest()}\nURL: {url}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--email", required=True, help="Smart Life account email")
    parser.add_argument("--country", required=True, help="account country calling code, e.g. 31")
    parser.add_argument("--device", help="device ID (otherwise choose from the account's device list)")
    parser.add_argument("--output", type=Path, default=Path("build/ota"))
    args = parser.parse_args()
    try:
        asyncio.run(run(args))
    except KeyboardInterrupt:
        parser.exit(1, "Interrupted. If restore.json remains, rerun with the same output directory.\n")
    except Exception as error:
        parser.exit(1, f"Error: {error if isinstance(error, Error) else type(error).__name__}\n")


if __name__ == "__main__":
    main()
