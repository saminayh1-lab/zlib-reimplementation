#!/usr/bin/env python3
"""Public check for rz: builds the crate like the grader and runs a public version of each stage.

    python3 /app/tests/check.py            build + all stages
    python3 /app/tests/check.py --no-speed skip the speed measurement
    python3 /app/tests/check.py --binary PATH  skip the build, test this binary

The data here is generated from a different seed than the hidden corpus and the malformed inputs
are a small sample of the kinds the grader uses. Ground truth is Python's zlib module.
"""
import argparse
import hashlib
import os
import random
import shutil
import struct
import subprocess
import sys
import tempfile
import time
import zlib

CRATE = "/app/zlib-rs"
WBITS = {"raw": -15, "zlib": 15, "gzip": 31}
RATIO_FULL, RATIO_ZERO = 1.015, 1.25
SPEED_FULL, SPEED_ZERO = 0.60, 0.10


def scan_sources():
    """The same refusals the grader applies before building."""
    import re
    allowed = {"std", "core", "alloc", "test", "proc_macro"}
    total = 0
    for root, _, files in os.walk(os.path.join(CRATE, "src")):
        for f in files:
            if not f.endswith(".rs"):
                continue
            path = os.path.join(root, f)
            data = open(path, "rb").read()
            total += len(data)
            try:
                text = data.decode("utf-8")
            except UnicodeDecodeError:
                sys.exit(f"{path}: not UTF-8 text, the grader refuses it")
            if re.search(r"#\s*!?\s*\[\s*link\b", text) or re.search(r"\.incbin\b", text):
                sys.exit(f"{path}: #[link] or .incbin, the grader refuses it")
            for m in re.finditer(r"\bextern\s+crate\s+(\w+)", text):
                if m.group(1) not in allowed:
                    sys.exit(f"{path}: extern crate {m.group(1)}, the grader refuses it")
    if total > (2 << 20):
        sys.exit("more than 2 MB of source, the grader refuses it")


def build():
    scan_sources()
    env = dict(os.environ, RUSTFLAGS="-C target-feature=+crt-static")
    print("building (cargo build --release --offline, static) ...")
    r = subprocess.run(["cargo", "build", "--release", "--offline"], cwd=CRATE, env=env)
    if r.returncode != 0:
        sys.exit("build failed")
    return os.path.join(CRATE, "target", "release", "rz")


def run(binary, args, data, timeout=120):
    try:
        p = subprocess.run([binary, *args], input=data, capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return "timeout", b""
    return p.returncode, p.stdout


def label(fmt, data):
    if fmt == "zlib" and len(data) >= 2 and data[1] & 0x20:
        return "error", None
    try:
        if fmt == "gzip":
            out, rest = b"", data
            if not rest:
                return "error", None
            while rest:
                d = zlib.decompressobj(31)
                out += d.decompress(rest) + d.flush()
                if not d.eof:
                    return "error", None
                rest = d.unused_data
            return "ok", out
        d = zlib.decompressobj(WBITS[fmt])
        out = d.decompress(data) + d.flush()
        if not d.eof or d.unused_data:
            return "error", None
        return "ok", out
    except zlib.error:
        return "error", None


def samples(rng):
    words = ("stream block code length distance literal table bit byte window match lazy chain hash tree "
             "header trailer checksum input output level ratio speed the a of and to in for with on").split()

    def text(n):
        out = []
        size = 0
        while size < n:
            s = " ".join(rng.choice(words) for _ in range(rng.randint(5, 14))) + rng.choice([". ", ",\n", "; "])
            out.append(s)
            size += len(s)
        return "".join(out).encode()[:n]
    src = b""
    for p in sorted(os.listdir("/usr/lib/python3.12"))[:40]:
        fp = os.path.join("/usr/lib/python3.12", p)
        if fp.endswith(".py") and os.path.isfile(fp):
            src += open(fp, "rb").read()
    return {
        "empty": b"", "tiny": b"ab", "text_5k": text(5000), "text_300k": text(300_000),
        "pysrc": src[:600_000], "zeros_200k": bytes(200_000), "random_200k": rng.randbytes(200_000),
        "runs": b"".join(bytes([rng.randrange(256)]) * rng.randint(1, 3000) for _ in range(300)),
        "period_40k": rng.randbytes(40_000) * 6, "period_8k": rng.randbytes(8_000) * 30,
    }


def gz(data, extra=None, name=None, comment=None, hcrc=False):
    body = zlib.compress(data, 6)[2:-4]
    flg = (4 if extra is not None else 0) | (8 if name is not None else 0) | (16 if comment is not None else 0) | (2 if hcrc else 0)
    hdr = bytearray([0x1F, 0x8B, 8, flg, 0, 0, 0, 0, 0, 3])
    if extra is not None:
        hdr += struct.pack("<H", len(extra)) + extra
    if name is not None:
        hdr += name + b"\0"
    if comment is not None:
        hdr += comment + b"\0"
    if hcrc:
        hdr += struct.pack("<H", zlib.crc32(bytes(hdr)) & 0xFFFF)
    return bytes(hdr) + body + struct.pack("<II", zlib.crc32(data), len(data) & 0xFFFFFFFF)


def stage_inflate(binary, plain, rng):
    cases = []
    for name, data in plain.items():
        big = len(data) > 100_000
        for level in ((1, 6, 9) if big else (0, 1, 4, 6, 9)):
            for strat in ((zlib.Z_DEFAULT_STRATEGY,) if big else (zlib.Z_DEFAULT_STRATEGY, zlib.Z_HUFFMAN_ONLY, zlib.Z_RLE, zlib.Z_FIXED)):
                for fmt, wb in (("zlib", 15), ("zlib", 10), ("raw", -15), ("gzip", 31)):
                    c = zlib.compressobj(level, zlib.DEFLATED, wb, 8, strat)
                    cases.append((f"{name} L{level} s{strat} {fmt}{wb}", fmt, c.compress(data) + c.flush()))
    t = plain["text_300k"]
    cases.append(("gzip two members", "gzip", gz(t[:1000], name=b"a") + gz(t[1000:3000], extra=b"xy", hcrc=True)))
    cases.append(("gzip empty member", "gzip", gz(b"", comment=b"c")))
    cases.append(("gzip all header fields", "gzip", gz(t[:5000], extra=b"\x01\x02\x03", name=b"n", comment=b"c", hcrc=True)))
    # a few hand-written bitstreams (hex): stored, fixed, and malformed ones
    hand = [
        ("stored block", "raw", "010500faff68656c6c6f"),
        ("fixed block, match", "raw", bytes(zlib.compress(b"abcabcabcabc", 6)[2:-4]).hex()),
        ("block type 3", "raw", "07"),
        ("stored bad nlen", "raw", "010500fbff68656c6c6f"),
        ("distance before start", "raw", "6300" + "00"),
    ]
    for name, fmt, hx in hand:
        cases.append((name, fmt, bytes.fromhex(hx)))
    z = zlib.compress(t, 6)
    cases.append(("truncated zlib", "zlib", z[:-7]))
    cases.append(("trailing byte after zlib", "zlib", z + b"\0"))
    cases.append(("bad adler", "zlib", z[:-1] + bytes([z[-1] ^ 1])))
    cases.append(("fdict set", "zlib", bytes([0x78, 0xBB]) + b"\0\0\0\1" + z[2:]))
    cases.append(("cinfo 8", "zlib", bytes([0x88, 0x9F]) + z[2:]))
    g = bytearray(gz(t))
    g[3] |= 0x20
    cases.append(("gzip reserved flag", "gzip", bytes(g)))
    g = bytearray(gz(t))
    g[-2] ^= 1
    cases.append(("gzip bad isize", "gzip", bytes(g)))
    cases.append(("empty input raw", "raw", b""))
    for _ in range(6):
        b = bytearray(z)
        b[rng.randrange(2, len(b) - 4)] ^= 1 << rng.randrange(8)
        cases.append(("bit flip", "zlib", bytes(b)))

    passed = total = 0
    for name, fmt, data in cases:
        verdict, expected = label(fmt, data)
        rc, out = run(binary, ["-d", "-f", fmt], data)
        ok = (rc == 0 and out == expected) if verdict == "ok" else rc == 1
        passed += ok
        total += 1
        if not ok:
            print(f"  FAIL inflate {name}: expected {verdict}, rc={rc}, {len(out)} bytes out")
    print(f"inflate: {passed}/{total}")
    return passed == total


def stage_deflate(binary, plain):
    passed = total = 0
    rels = {"6": [], "9": []}
    for name, data in plain.items():
        for level in (0, 1, 3, 6, 9):
            for fmt in ("raw", "zlib", "gzip"):
                rc, out = run(binary, ["-c", "-f", fmt, "-l", str(level)], data, timeout=180)
                verdict, back = label(fmt, out) if rc == 0 else ("error", None)
                ok = rc == 0 and verdict == "ok" and back == data
                passed += ok
                total += 1
                if not ok:
                    print(f"  FAIL deflate {name} L{level} {fmt}: rc={rc}, zlib says {verdict}")
                if ok and fmt == "raw" and level in (6, 9) and len(data) >= 5000:
                    rels[str(level)].append(len(out) / len(zlib.compress(data, level)[2:-4]))
    print(f"deflate validity: {passed}/{total}")
    for level, r in rels.items():
        if r:
            m = sum(r) / len(r)
            s = max(0.0, min(1.0, (RATIO_ZERO - m) / (RATIO_ZERO - RATIO_FULL)))
            print(f"ratio level {level}: mean size relative to zlib {m:.4f} (full marks at {RATIO_FULL}) -> {s:.3f}")
    return passed == total


def stage_speed(binary, plain, rng):
    mix = b"".join([plain["text_300k"], plain["pysrc"], plain["runs"], plain["random_200k"], plain["period_8k"]])
    big = (mix * 12)[: 16 << 20]
    z = zlib.compress(big, 6)
    tmp = tempfile.mkdtemp()
    try:
        zin, pin = os.path.join(tmp, "in.zz"), os.path.join(tmp, "in.bin")
        open(zin, "wb").write(z)
        open(pin, "wb").write(big)
        out = os.path.join(tmp, "out")

        def timed(argv, inp):
            best = None
            for _ in range(3):
                with open(inp, "rb") as fi, open(out, "wb") as fo:
                    t0 = time.perf_counter()
                    subprocess.run(argv, stdin=fi, stdout=fo, stderr=subprocess.DEVNULL, timeout=600)
                    dt = time.perf_counter() - t0
                best = dt if best is None else min(best, dt)
            return best
        ci = timed([binary, "-d", "-f", "zlib"], zin)
        zi = timed(["python3", "-c", "import sys,zlib;sys.stdout.buffer.write(zlib.decompress(sys.stdin.buffer.read()))"], zin)
        cd = timed([binary, "-c", "-f", "raw", "-l", "6"], pin)
        zd = timed(["python3", "-c", "import sys,zlib;sys.stdout.buffer.write(zlib.compress(sys.stdin.buffer.read(),6)[2:-4])"], pin)
        ri, rd = zi / ci, zd / cd
        gm = (ri * rd) ** 0.5
        s = max(0.0, min(1.0, (gm - SPEED_ZERO) / (SPEED_FULL - SPEED_ZERO)))
        print(f"speed: inflate {ri:.2f}x zlib, deflate {rd:.2f}x zlib, geometric mean {gm:.2f} (full marks at {SPEED_FULL}) -> {s:.3f}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--binary")
    ap.add_argument("--no-speed", action="store_true")
    args = ap.parse_args()
    binary = args.binary or build()
    rng = random.Random(20261029)
    plain = samples(rng)
    ok_i = stage_inflate(binary, plain, rng)
    ok_d = stage_deflate(binary, plain)
    if not args.no_speed:
        if ok_i and ok_d:
            stage_speed(binary, plain, rng)
        else:
            print("speed: skipped until both stages pass (the grader does the same)")


if __name__ == "__main__":
    main()
