#!/bin/sh
set -eu

usage() {
    echo "usage: $0 <extracted-stone-main>" >&2
    exit 2
}

[ "$#" -eq 1 ] || usage

ROOT=$(CDPATH='' cd -- "$(dirname -- "$0")/.." && pwd)
STONE=$1
case "$STONE" in
    /*) ;;
    *) STONE=$(pwd)/$STONE ;;
esac

[ -f "$STONE" ] || {
    echo "not a file: $STONE" >&2
    exit 1
}
[ -x "$ROOT/build/mipsel/bin/patch_stone_main" ] || {
    echo "missing camera binaries; run ./tools/compile.sh first" >&2
    exit 1
}

WORK=$(mktemp -d "${TMPDIR:-/tmp}/lsc-stone-compat.XXXXXX")
cleanup() {
    rm -rf "$WORK"
}
trap cleanup EXIT HUP INT TERM

HOST_PATCHER="$WORK/patch_stone_main"
LOW_COPY="$WORK/stone-low"
HIGH_COPY="$WORK/stone-high"
PAYLOAD="$WORK/payload"

cc -std=c99 -O2 -Wall -Wextra -Werror \
    -o "$HOST_PATCHER" "$ROOT/tools/src/patch_stone_main.c"

cp "$STONE" "$LOW_COPY"
"$HOST_PATCHER" --keep-low-power "$LOW_COPY"
"$HOST_PATCHER" --keep-low-power "$LOW_COPY"

cp "$STONE" "$HIGH_COPY"
"$HOST_PATCHER" "$HIGH_COPY"
"$HOST_PATCHER" "$HIGH_COPY"
if "$HOST_PATCHER" --check --keep-low-power "$HIGH_COPY" >/dev/null 2>&1; then
    echo "check unexpectedly accepted an unrecoverable low-power branch" >&2
    exit 1
fi
if "$HOST_PATCHER" --keep-low-power "$HIGH_COPY" >/dev/null 2>&1; then
    echo "unsafe low-power branch reconstruction unexpectedly succeeded" >&2
    exit 1
fi

mkdir "$PAYLOAD"
"$ROOT/tools/build_tuya_dat_overflow.py" --stone-main "$STONE" "$PAYLOAD"

env PYTHONDONTWRITEBYTECODE=1 python3 - "$ROOT" "$STONE" "$PAYLOAD" <<'PY'
import importlib.util
import struct
import sys
from pathlib import Path

root = Path(sys.argv[1])
stone = Path(sys.argv[2])
payload_dir = Path(sys.argv[3])
builder_path = root / "tools" / "build_tuya_dat_overflow.py"
spec = importlib.util.spec_from_file_location("lsc_payload_builder", builder_path)
builder = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(builder)

data = stone.read_bytes()
builder.validate_tuya_overflow_layout(data, stone)
gadget = builder.discover_system_sp20_gadget(stone)

generated = (payload_dir / "tuya.dat").read_bytes()
saved_ra = struct.unpack_from("<I", generated, builder.SAVED_RA_OFFSET_FROM_FIELD)[0]
if saved_ra != gadget:
    raise SystemExit(
        f"generated saved RA 0x{saved_ra:08x} does not match discovered 0x{gadget:08x}"
    )

mutated = bytearray(data)
matches = builder.find_word_contexts(mutated, builder.TUYA_PARSER_PROLOGUE)
if len(matches) != 1:
    raise SystemExit(f"negative-test setup found {len(matches)} parser signatures")
mutated[matches[0]] ^= 1
try:
    builder.validate_tuya_overflow_layout(bytes(mutated), Path("mutated-stone-main"))
except ValueError:
    pass
else:
    raise SystemExit("mutated parser signature was unexpectedly accepted")

print(f"bootstrap gadget: 0x{gadget:08x}")
print("negative parser-signature test: rejected as expected")
PY

find "$PAYLOAD" -type f -name '*.sh' -exec sh -n {} \;
if command -v shellcheck >/dev/null 2>&1; then
    find "$PAYLOAD" -type f -name '*.sh' -exec shellcheck -S warning {} +
fi

cmp "$ROOT/build/mipsel/bin/patch_stone_main" \
    "$PAYLOAD/custom/bin/patch_stone_main"

echo "compatible: $STONE"
