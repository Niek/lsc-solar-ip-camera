#!/usr/bin/env python3
"""Extract stone/main from the known T23 Tuya OTA format without installing it."""
import argparse
import hashlib
import stat
import struct
from pathlib import Path


START_POS_MODEL_INDEX = 4
END_POS_MODEL_INDEX = 14
NUM_ALIGN_BITS = 4
OTA_HEADER_SIZE = 236
MAX_ROOTFS_SIZE = 64 * 1024 * 1024


def reverse_bits(value: int, bits: int) -> int:
    out = 0
    for bit in range(bits):
        out <<= 1
        if value & (1 << bit):
            out |= 1
    return out


class BitReader:
    def __init__(self, data: bytes):
        self.data = data
        self.byte_pos = 0
        self.bit_pos = 0

    def bit(self) -> int:
        if self.byte_pos >= len(self.data):
            raise EOFError
        value = 1 if self.data[self.byte_pos] & (1 << self.bit_pos) else 0
        self.bit_pos += 1
        if self.bit_pos == 8:
            self.bit_pos = 0
            self.byte_pos += 1
        return value

    def num(self, bits: int) -> int:
        value = 0
        for _ in range(bits):
            value = (value << 1) | self.bit()
        return value


def decode_length(reader: BitReader) -> int:
    if reader.bit() == 0:
        return reader.num(3) + 2
    if reader.bit() == 0:
        return reader.num(3) + 10
    return reader.num(8) + 18


def decode_distance(reader: BitReader) -> int:
    pos_slot = reader.num(6)
    if pos_slot < START_POS_MODEL_INDEX:
        return pos_slot

    direct_bits = (pos_slot >> 1) - 1
    distance = (2 | (pos_slot & 1)) << direct_bits
    if pos_slot < END_POS_MODEL_INDEX:
        distance += reverse_bits(reader.num(direct_bits), direct_bits)
    else:
        distance += reader.num(direct_bits - NUM_ALIGN_BITS) << NUM_ALIGN_BITS
        distance += reverse_bits(reader.num(NUM_ALIGN_BITS), NUM_ALIGN_BITS)
    return distance


def jzlzma_decompress(payload: bytes, limit: int) -> bytes:
    reader = BitReader(payload)
    reps = [0, 0, 0, 0]
    out = bytearray()

    try:
        while len(out) < limit:
            if reader.bit() == 0:
                out.append(reader.num(8))
                continue

            size = 0
            if reader.bit() == 0:
                size = decode_length(reader)
                reps.insert(0, decode_distance(reader))
                reps.pop()
            elif reader.bit() == 0:
                if reader.bit() == 0:
                    size = 1
            elif reader.bit() == 0:
                reps.insert(0, reps.pop(1))
            elif reader.bit() == 0:
                reps.insert(0, reps.pop(2))
            else:
                reps.insert(0, reps.pop(3))

            if size == 0:
                size = decode_length(reader)

            start = len(out) - reps[0] - 1
            if start < 0:
                raise ValueError(f"invalid JZLZMA back-reference distance {reps[0]}")
            while size > 0:
                chunk = out[start:start + size]
                if not chunk:
                    raise ValueError("empty JZLZMA back-reference chunk")
                out.extend(chunk)
                size -= len(chunk)
                if len(out) >= limit:
                    return bytes(out[:limit])
    except EOFError:
        pass

    if len(out) != limit:
        raise ValueError(f"JZLZMA output ended at {len(out)} bytes, expected {limit}")
    return bytes(out)


def extract_stone_main(data: bytes) -> bytes:
    if len(data) < OTA_HEADER_SIZE:
        raise ValueError("truncated OTA header")
    # Six 36-byte big-endian descriptors follow the 16-byte version label.
    # Type 8 is the rootfs; some builds leave other descriptors empty.
    roots = []
    for pos in range(16, 232, 36):
        kind, offset, size, _flags, checksum = struct.unpack_from(">5I", data, pos)
        if kind == 8:
            roots.append((offset, size, checksum))
    if len(roots) != 1:
        raise ValueError(f"expected one type-8 rootfs, found {len(roots)}")
    offset, size, checksum = roots[0]
    if offset < OTA_HEADER_SIZE or size < 20 or offset + size > len(data):
        raise ValueError("invalid rootfs bounds")
    rootfs = data[offset:offset + size]
    if sum(rootfs) & 0xffffffff != checksum:
        raise ValueError("rootfs checksum mismatch")
    expanded, packed, magic, dictionary, expanded_again = struct.unpack_from("<5I", rootfs)
    if (magic != 0x27051956 or dictionary != 0x10000 or
            expanded != expanded_again or packed != size - 12 or
            not 0 < expanded <= MAX_ROOTFS_SIZE):
        raise ValueError("unrecognized JZLZMA rootfs header")
    archive = jzlzma_decompress(rootfs[20:], expanded)
    return stone_from_cpio(archive)


def stone_from_cpio(archive: bytes) -> bytes:
    # Read just the regular file we need. Never extract archive paths, symlinks,
    # or device nodes onto the host filesystem.
    pos = 0
    while pos + 110 <= len(archive):
        header = archive[pos:pos + 110]
        if header[:6] != b"070701":
            raise ValueError("invalid newc CPIO header")
        fields = [int(header[i:i + 8], 16) for i in range(6, 110, 8)]
        mode, size, name_size = fields[1], fields[6], fields[11]
        name_start = pos + 110
        name_end = name_start + name_size
        if name_size < 1 or name_end > len(archive) or archive[name_end - 1] != 0:
            raise ValueError("invalid CPIO filename")
        name = archive[name_start:name_end - 1]
        start = (name_end + 3) & ~3
        if start + size > len(archive):
            raise ValueError("truncated CPIO member")
        if name in (b"stone/main", b"./stone/main"):
            result = archive[start:start + size]
            if not stat.S_ISREG(mode) or len(result) < 52:
                raise ValueError("stone/main is not a regular ELF file")
            if result[:6] != b"\x7fELF\x01\x01" or struct.unpack_from("<H", result, 18)[0] != 8:
                raise ValueError("stone/main is not a 32-bit little-endian MIPS ELF")
            return result
        if name == b"TRAILER!!!":
            break
        pos = (start + size + 3) & ~3
    raise ValueError("stone/main not found in rootfs")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("ota", type=Path, help="downloaded OTA package (not a raw flash dump)")
    parser.add_argument("output", type=Path, help="new output file for the stock executable")
    args = parser.parse_args()
    try:
        result = extract_stone_main(args.ota.read_bytes())
        args.output.parent.mkdir(parents=True, exist_ok=True)
        with args.output.open("xb") as stream:
            stream.write(result)
    except (OSError, ValueError) as exc:
        parser.error(str(exc))
    print(f"Extracted: {args.output} ({len(result)} bytes)")
    print(f"SHA-256: {hashlib.sha256(result).hexdigest()}")
    print("Check compatibility with tools/check_stone_compat.sh before preparing an SD card.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
