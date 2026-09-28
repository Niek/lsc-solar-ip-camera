#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = ["tuya-mobile==1.2.0", "aiomqtt==2.5.0", "aiortc==1.15.0"]
# ///
"""Keep a Smart Life camera awake with a discarded live preview until Ctrl+C."""
import argparse
import asyncio
import contextlib
import getpass
import hashlib
import json
import os
import secrets
import ssl
import time
from types import MethodType
import zlib

import aiohttp
import aiomqtt
from aiortc import (RTCBundlePolicy, RTCConfiguration, RTCIceServer,
                    RTCPeerConnection, RTCRtpSender, RTCSessionDescription)
from aiortc.rtcdtlstransport import State
from aiortc.sdp import candidate_from_sdp
from OpenSSL import crypto
from tuya_mobile import TuyaMobileApp, TuyaPasswordClient, mqtt_client_id, mqtt_credentials


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


class WakeClient(TuyaPasswordClient):
    async def _submit_login(self, *args, **kwargs):
        # tuya-mobile 1.2.0's session omits the broker and partner identity.
        result = await super()._submit_login(*args, **kwargs)
        domain = result.get("domain", {})
        self.mqtt_host = domain.get("mobileMqttsUrl")
        self.mqtt_port = int(domain.get("mqttsPort") or 8883)
        self.partner_id = result.get("partnerIdentity")
        return result


def verify_camera_certificate(transport, parameters):
    # The stock certificate fails cryptography's strict X.509 parser. Verify
    # the same SDP fingerprints over its original DER, without re-encoding it.
    # Private aiortc 1.15.0 hook; TLS broker verification remains unchanged.
    certificate = transport._ssl.get_peer_certificate()
    fingerprints = [f for f in parameters.fingerprints
                    if f.algorithm.lower() in ("sha-256", "sha-384", "sha-512")]
    if certificate is not None and fingerprints:
        der = crypto.dump_certificate(crypto.FILETYPE_ASN1, certificate)
        if all(hashlib.new(f.algorithm.lower().replace("-", ""), der).hexdigest()
               == f.value.replace(":", "").lower() for f in fingerprints):
            return
    transport._set_state(State.FAILED)


async def awake_state(client, device_id):
    metadata = unwrap(await client._call(
        "thing.m.device.get", {"devId": device_id}, version="4.1"))
    return metadata.get("dataPointInfo", {}).get("dps", {}).get("149")


async def keep_awake(client, device):
    if not client.mqtt_host or not client.partner_id:
        raise Error("Smart Life login returned no MQTT broker or partner identity.")
    device_id = device["device_id"]
    unwrap(await client._call("smartlife.m.p2p.main.pre.link.get", {"devId": device_id}))
    config = unwrap(await client._call("smartlife.m.rtc.config.get", {"devId": device_id}))
    if not config.get("supportsWebrtc"):
        raise Error("This camera does not support WebRTC preview.")
    unwrap(await client._call("smartlife.m.rtc.session.init", {"devId": device_id}))
    ice = config["p2pConfig"]["ices"]
    # aiortc 1.15.0's ICE URL parser cannot parse bracketed IPv6 addresses.
    servers = []
    for server in ice:
        urls = server["urls"]
        urls = [urls] if isinstance(urls, str) else urls
        urls = [url for url in urls if "[" not in url]
        if urls:
            servers.append(RTCIceServer(urls, server.get("username"), server.get("credential")))
    pc = RTCPeerConnection(RTCConfiguration(servers, RTCBundlePolicy.MAX_BUNDLE))
    tasks = []
    connected = asyncio.Event()

    async def discard(track):
        while True:
            await track.recv()

    @pc.on("track")
    def on_track(track):
        tasks.append(asyncio.create_task(discard(track)))

    @pc.on("connectionstatechange")
    def on_state_change():
        if pc.connectionState == "connected":
            connected.set()

    async def watch():
        try:
            await asyncio.wait_for(connected.wait(), 45)
        except TimeoutError:
            raise Error("Camera preview did not start within 45 seconds.") from None
        print("Preview active; keeping camera awake. Ctrl+C to stop.", flush=True)
        while True:
            await asyncio.sleep(5)
            if pc.connectionState != "connected":
                raise Error("Camera preview stopped. Run again to reconnect.")

    credentials = mqtt_credentials(
        client.signer, uid=client.uid, sid=client.sid, ecode=client.ecode,
        partner_id=client.partner_id,
    )
    installation = secrets.token_hex(24) + "_" + hashlib.md5((client.uid + "sdkfasodifca").encode()).hexdigest()
    session_id = secrets.token_hex(16)
    try:
        # Audio must precede video in Tuya offers; no microphone track is added.
        for kind, direction, codecs in (("audio", "sendrecv", ("audio/pcma", "audio/pcmu")),
                                        ("video", "recvonly", ("video/h264",))):
            transceiver = pc.addTransceiver(kind, direction=direction)
            transceiver.setCodecPreferences([c for c in RTCRtpSender.getCapabilities(kind).codecs
                                            if c.mimeType.lower() in codecs])
            transport = transceiver.receiver.transport
            transport._validate_peer_identity = MethodType(verify_camera_certificate, transport)
        await asyncio.wait_for(pc.setLocalDescription(await pc.createOffer()), 30)
        async with aiomqtt.Client(
            client.mqtt_host, port=client.mqtt_port,
            username=credentials["username"], password=credentials["password"],
            identifier=mqtt_client_id(client.profile.package, installation_id=installation),
            tls_context=ssl.create_default_context(), timeout=15,
        ) as mqtt:
            # WebRTC signaling follows go2rtc/pkg/tuya/mqtt.go and aventproxy's
            # mobile bridge: /av topics carry JSON, not the native P2P envelope.
            async def send(kind, body):
                message = {"protocol": 302, "pv": "2.2", "t": int(time.time() * 1000), "data": {
                    "header": {"type": kind, "from": client.uid, "to": device_id,
                               "sub_dev_id": "", "sessionid": session_id, "moto_id": config["motoId"],
                               "tid": "", "seq": 0, "rtx": 0},
                    "msg": {"mode": "webrtc", **body}}}
                await mqtt.publish(f"/av/moto/{config['motoId']}/u/{device_id}",
                                   json.dumps(message), qos=1, retain=False)

            async def receive():
                async for message in mqtt.messages:
                    data = json.loads(message.payload).get("data", {})
                    header, body = data.get("header", {}), data.get("msg", {})
                    if header.get("sessionid") != session_id:
                        continue
                    if header.get("type") == "answer":
                        await pc.setRemoteDescription(RTCSessionDescription(body["sdp"], "answer"))
                    elif header.get("type") == "candidate" and body.get("candidate"):
                        candidate = candidate_from_sdp(body["candidate"].strip()
                                                       .removeprefix("a=").removeprefix("candidate:"))
                        candidate.sdpMLineIndex = 0  # audio/video share one BUNDLE transport
                        await pc.addIceCandidate(candidate)
                    elif header.get("type") == "disconnect":
                        code = body.get("close_reason")
                        suffix = f" (code {code})" if isinstance(code, int) else ""
                        raise Error(f"Camera ended the preview{suffix}.")
                raise Error("Preview signaling connection closed.")

            offered = False
            try:
                await mqtt.subscribe(f"/av/u/{client.uid}", qos=1)
                # A sleeping camera must finish waking before it can answer.
                payload = zlib.crc32(device["local_key"].encode()).to_bytes(4, "big")
                print("Waking camera and starting preview…", flush=True)
                for attempt in range(15):
                    if attempt % 3 == 0:
                        await mqtt.publish(f"m/w/{device_id}", payload, qos=1, retain=False)
                    await asyncio.sleep(2)
                    if await awake_state(client, device_id) is True:
                        break
                else:
                    raise Error("Camera did not report awake within 30 seconds.")
                lines = pc.localDescription.sdp.splitlines()
                candidates = dict.fromkeys(line for line in lines if line.startswith("a=candidate:"))
                # Keep Tuya's offer small and trickle candidates after it.
                sdp = "\r\n".join(line for line in lines if not line.startswith((
                    "a=extmap", "a=candidate:", "a=end-of-candidates",
                    "a=fingerprint:sha-384", "a=fingerprint:sha-512"))) + "\r\n"
                reader = asyncio.create_task(receive())
                tasks.append(reader)
                offered = True
                await send("offer", {"sdp": sdp, "stream_type": 1, "auth": config["auth"],
                                     "datachannel_enable": False, "token": ice, "replay": {"is_replay": 0}})
                for candidate in candidates:
                    await send("candidate", {"candidate": candidate})
                watcher = asyncio.create_task(watch())
                tasks.append(watcher)
                done, _ = await asyncio.wait([reader, watcher], return_when=asyncio.FIRST_COMPLETED)
                for task in done:
                    await task
            finally:
                if offered:
                    with contextlib.suppress(Exception):
                        await send("disconnect", {})
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
    finally:
        await pc.close()


async def run(args):
    password = os.environ.get("TUYA_PASSWORD") or getpass.getpass("Smart Life password: ")
    async with aiohttp.ClientSession() as session:
        client = WakeClient.for_application(
            TuyaMobileApp.SMART_LIFE, session, username=args.email, max_login_attempts=1)
        await client.login_with_password(password, country_code=args.country)
        del password
        devices = await client.get_local_keys([args.device] if args.device else [])
        if args.device:
            device = next((d for d in devices if d["device_id"] == args.device), None)
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
        print(f"Selected: {device.get('name') or 'Unnamed device'}", flush=True)
        awake = await awake_state(client, device["device_id"])
        if not isinstance(awake, bool):
            raise Error("Selected device has no low-power camera awake state (DP 149).")
        await keep_awake(client, device)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--email", default=os.environ.get("TUYA_EMAIL"),
                        required=not os.environ.get("TUYA_EMAIL"), help="Smart Life account email")
    parser.add_argument("--country", default=os.environ.get("TUYA_COUNTRY"),
                        required=not os.environ.get("TUYA_COUNTRY"),
                        help="account country calling code, e.g. 31")
    parser.add_argument("--device", help="device ID (otherwise choose from the account's device list)")
    args = parser.parse_args()
    try:
        asyncio.run(run(args))
    except KeyboardInterrupt:
        print("\nPreview closed; camera can sleep again.")
    except Exception as error:
        parser.exit(1, f"Error: {error if isinstance(error, Error) else type(error).__name__}\n")


if __name__ == "__main__":
    main()
