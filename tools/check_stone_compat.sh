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

env PYTHONDONTWRITEBYTECODE=1 python3 - "$ROOT" "$STONE" "$PAYLOAD" "$HOST_PATCHER" "$LOW_COPY" "$HIGH_COPY" <<'PY'
import importlib.util
import re
import struct
import subprocess
import sys
from pathlib import Path

root = Path(sys.argv[1])
stone = Path(sys.argv[2])
payload_dir = Path(sys.argv[3])
patcher = sys.argv[4]
low_copy = Path(sys.argv[5])
high_copy = Path(sys.argv[6])
builder_path = root / "tools" / "build_tuya_dat_overflow.py"
spec = importlib.util.spec_from_file_location("lsc_payload_builder", builder_path)
builder = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(builder)

data = stone.read_bytes()
check = subprocess.run([patcher, "--check", str(stone)], check=True, capture_output=True, text=True)
offset = int(re.search(r"low-power branch at 0x([0-9a-f]+)", check.stdout)[1], 16)
stock_word = struct.unpack_from("<I", data, offset)[0]
low_word = struct.unpack_from("<I", low_copy.read_bytes(), offset)[0]
high_word = struct.unpack_from("<I", high_copy.read_bytes(), offset)[0]
assert low_word == stock_word, "low-power mode changed the stock branch"
if stock_word >> 16 == 0x1440:  # bne enters shutdown: remove the branch
    assert high_word == 0
elif stock_word >> 16 == 0x1040:  # beq skips shutdown: always take the same target
    assert high_word == 0x10000000 | (stock_word & 0xffff)
else:
    raise SystemExit(f"unexpected stock low-power branch: 0x{stock_word:08x}")

for copy, options in ((low_copy, ["--keep-low-power"]), (high_copy, [])):
    before = copy.read_bytes()
    subprocess.run([patcher, *options, str(copy)], check=True, capture_output=True)
    assert copy.read_bytes() == before, "patching twice changed the binary"

def reject_patch(label, mutated):
    path = payload_dir / "negative-test-main"
    path.write_bytes(mutated)
    for options in (["--check"], ["--keep-low-power"], []):
        result = subprocess.run([patcher, *options, str(path)], capture_output=True)
        assert result.returncode != 0, f"{label} unexpectedly accepted"
        assert path.read_bytes() == mutated, f"{label} modified before rejection"
    path.unlink()

mutated = bytearray(data)
struct.pack_into("<I", mutated, offset, 0x03e00008)  # jr ra is never a supported decision
reject_patch("unknown low-power instruction", mutated)
if stock_word >> 16 == 0x1040:
    mutated = bytearray(data)
    struct.pack_into("<I", mutated, offset, stock_word + 1)  # wrong skip destination
    reject_patch("changed low-power skip target", mutated)
print("power modes, idempotence, and rejection without writes: passed")

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
