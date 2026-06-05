#!/usr/bin/env python3
import argparse
import hashlib
import os
import shutil
import socket
import struct
import tempfile
import threading
import time
from pathlib import Path


SD_ROOT = "/tmp/mnt/sdcard"


def shell_quote(value: str) -> str:
    return "'" + value.replace("'", "'\\''") + "'"


def local_ip_for(remote_ip: str) -> str:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect((remote_ip, 9))
        return sock.getsockname()[0]
    finally:
        sock.close()


class TftpServer:
    def __init__(self, root: Path, bind_port: int):
        self.root = root
        self.bind_port = bind_port
        self.stop = threading.Event()
        self.served = 0
        self.lock = threading.Lock()

    def start(self) -> threading.Thread:
        thread = threading.Thread(target=self._serve, daemon=True)
        thread.start()
        return thread

    def _serve(self) -> None:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind(("0.0.0.0", self.bind_port))
            sock.settimeout(0.5)
            while not self.stop.is_set():
                try:
                    packet, addr = sock.recvfrom(516)
                except socket.timeout:
                    continue
                if len(packet) < 4 or struct.unpack("!H", packet[:2])[0] != 1:
                    continue
                name = packet[2:].split(b"\0", 1)[0].decode("ascii", "ignore")
                threading.Thread(target=self._serve_file, args=(name, addr), daemon=True).start()

    def _serve_file(self, name: str, client_addr: tuple[str, int]) -> None:
        path = self.root / name
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.settimeout(2.0)
            if not path.is_file():
                sock.sendto(struct.pack("!HH", 5, 1) + b"not found\0", client_addr)
                print(f"tftp-miss {name}", flush=True)
                return

            data = path.read_bytes()
            block = 1
            offset = 0
            while True:
                payload = data[offset : offset + 512]
                packet = struct.pack("!HH", 3, block) + payload
                for _ in range(8):
                    sock.sendto(packet, client_addr)
                    try:
                        ack, ack_addr = sock.recvfrom(516)
                    except socket.timeout:
                        continue
                    if ack_addr == client_addr and ack[:4] == struct.pack("!HH", 4, block):
                        break
                else:
                    print(f"tftp-fail {name} block={block}", flush=True)
                    return

                offset += len(payload)
                if len(payload) < 512:
                    with self.lock:
                        self.served += 1
                        if self.served == 1 or self.served % 25 == 0:
                            print(f"tftp-served-count {self.served}", flush=True)
                    return
                block = (block + 1) & 0xFFFF


def add_chunks(chunks_dir: Path, index: int, src: Path, chunk_size: int) -> tuple[list[str], str, int]:
    data = src.read_bytes()
    md5 = hashlib.md5(data).hexdigest()
    names = []
    for part, offset in enumerate(range(0, max(len(data), 1), chunk_size)):
        name = f"f{index:04d}_{part:04d}"
        (chunks_dir / name).write_bytes(data[offset : offset + chunk_size])
        names.append(name)
    return names, md5, len(data)


def manifest_for(payload: Path, include_trigger: bool) -> list[tuple[Path, str]]:
    files = []

    factory = payload / "factory"
    for src in sorted(factory.rglob("*")):
        if src.is_file() and src.name != "stone-main.bin":
            files.append((src, f"{SD_ROOT}/{src.relative_to(payload).as_posix()}"))

    custom = payload / "custom"
    for src in sorted(custom.rglob("*")):
        if src.is_file():
            files.append((src, f"{SD_ROOT}/{src.relative_to(payload).as_posix()}"))

    tuya_dat = payload / "tuya.dat"
    if include_trigger and tuya_dat.is_file():
        files.append((tuya_dat, f"{SD_ROOT}/tuya.dat"))
    return files


def mode_for(remote_path: str) -> str:
    if "/bin/" in remote_path or remote_path.endswith("/factory/main"):
        return "755"
    if remote_path.endswith("/custom/scripts/entrypoint_t23.sh"):
        return "755"
    return "644"


def build_remote_script(entries: list[dict], host: str, port: int, reboot: bool) -> str:
    lines = [
        "#!/bin/sh",
        "echo live-push-start",
        "TMP=/tmp/lsc_push",
        'rm -rf "$TMP"',
        'mkdir -p "$TMP" || exit 2',
        'fail(){ echo "live-push-fail:$*"; exit 1; }',
        f"HOST={host}",
        f"PORT={port}",
        'fetch(){ n="$1"; out="$TMP/$n"; rm -f "$out"; i=0; '
        'while [ "$i" -lt 4 ]; do '
        'tftp -g -r "$n" -l "$out" "$HOST" "$PORT" && return 0; '
        'i=$((i+1)); echo retry-$n-$i; sleep 1; done; return 1; }',
        'put(){ dst="$1"; dir="$2"; mode="$3"; expect="$4"; shift 4; '
        'echo file-start-$(basename "$dst"); mkdir -p "$dir" || fail mkdir; '
        'tmp="$dst.new"; rm -f "$tmp"; : > "$tmp" || fail create; '
        'for n in "$@"; do fetch "$n" || fail fetch-$n; '
        'cat "$TMP/$n" >> "$tmp" || fail append-$n; rm -f "$TMP/$n"; done; '
        'set -- $(md5sum "$tmp"); [ "$1" = "$expect" ] || fail md5-$dst-$1; '
        'chmod "$mode" "$tmp" 2>/dev/null || true; '
        'mv -f "$tmp" "$dst" || fail mv-$dst; echo file-done-$(basename "$dst"); }',
        'echo live-push-host-$HOST-port-$PORT',
    ]

    for entry in entries:
        args = [
            shell_quote(entry["remote"]),
            shell_quote(os.path.dirname(entry["remote"])),
            entry["mode"],
            entry["md5"],
            *entry["chunks"],
        ]
        lines.append("put " + " ".join(args))

    lines.append("sync")
    lines.append("echo live-push-done")
    if reboot:
        lines.append("/sbin/reboot")
    return "\n".join(lines) + "\n"


def run_telnet(camera_ip: str, telnet_port: int, bootstrap: str) -> None:
    with socket.create_connection((camera_ip, telnet_port), timeout=8) as sock:
        sock.settimeout(0.5)
        time.sleep(0.2)
        try:
            print(sock.recv(4096).decode("latin1", "replace"), end="", flush=True)
        except socket.timeout:
            pass

        sock.sendall(bootstrap.encode("ascii"))
        last = time.time()
        buf = b""
        while True:
            try:
                data = sock.recv(4096)
            except socket.timeout:
                if time.time() - last > 240:
                    print("\ntelnet-idle-timeout", flush=True)
                    return
                continue
            if not data:
                print("\ntelnet-closed", flush=True)
                return
            last = time.time()
            buf += data
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                text = line.decode("latin1", "replace").rstrip("\r")
                if (
                    text.startswith("live-push")
                    or text.startswith("file-start")
                    or text.startswith("file-done")
                    or text.startswith("retry-")
                    or text.startswith("~ #")
                ):
                    print(text, flush=True)


def main() -> int:
    parser = argparse.ArgumentParser(description="Push a generated SD payload to a live T23 camera.")
    parser.add_argument("payload", type=Path, help="payload directory generated by build_tuya_dat_overflow.py")
    parser.add_argument("--camera-ip", required=True, help="camera IP address")
    parser.add_argument("--telnet-port", type=int, default=2323)
    parser.add_argument("--tftp-port", type=int, default=12069)
    parser.add_argument("--chunk-size", type=int, default=20000)
    parser.add_argument("--include-trigger", action="store_true",
                        help="also push tuya.dat; useful for forced first-boot tests, not normal live updates")
    parser.add_argument("--no-reboot", action="store_true")
    args = parser.parse_args()

    payload = args.payload.resolve()
    files = manifest_for(payload, args.include_trigger)
    if not files:
        raise SystemExit(f"no payload files found in {payload}")

    chunks_dir = Path(tempfile.mkdtemp(prefix="lsc-tftp-push-"))
    server = None
    try:
        entries = []
        total_chunks = 0
        total_bytes = 0
        for index, (src, remote) in enumerate(files):
            chunks, md5, size = add_chunks(chunks_dir, index, src, args.chunk_size)
            entries.append({"remote": remote, "mode": mode_for(remote), "md5": md5, "chunks": chunks})
            total_chunks += len(chunks)
            total_bytes += size

        host = local_ip_for(args.camera_ip)
        remote_script = build_remote_script(entries, host, args.tftp_port, not args.no_reboot)
        (chunks_dir / "push.sh").write_text(remote_script, encoding="ascii")

        server = TftpServer(chunks_dir, args.tftp_port)
        server.start()
        print(
            f"prepared {len(entries)} files, {total_chunks} chunks, "
            f"{total_bytes} bytes, push.sh={len(remote_script)} bytes",
            flush=True,
        )
        print(f"tftp-serving {chunks_dir} on {host}:{args.tftp_port}", flush=True)

        bootstrap = (
            "rm -f /tmp/lsc_live_push.sh\n"
            f"tftp -g -r push.sh -l /tmp/lsc_live_push.sh {host} {args.tftp_port}\n"
            "chmod 755 /tmp/lsc_live_push.sh\n"
            "sh /tmp/lsc_live_push.sh\n"
        )
        run_telnet(args.camera_ip, args.telnet_port, bootstrap)
        return 0
    finally:
        if server:
            server.stop.set()
        time.sleep(0.5)
        shutil.rmtree(chunks_dir, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
