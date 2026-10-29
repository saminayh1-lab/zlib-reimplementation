#!/usr/bin/env python3
"""Build the hidden corpus for the zlib-reimplementation grader.

Runs once at image build time, as root, into a directory under /root that the agent cannot read.
Everything is derived from a fixed seed plus files that are part of the image, and every inflate
case is labelled by zlib itself: the expected output hash for streams zlib accepts, "error" for
streams it rejects. Two rules come from the CLI contract and not from zlib: bytes left over after
the end of a stream are an error, and a zlib header with FDICT set is an error.

    python3 gen_corpus.py /root/grader/corpus
"""
import glob
import hashlib
import json
import os
import random
import struct
import sys
import zlib

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import deflate_asm as asm  # noqa: E402

SEED = 0x5EED_2026
WINDOW = 32768

WORDS = ("the of and to in is it you that he was for on are with as his they be at one have this from or had "
         "by hot word but what some we can out other were all there when up use your how said an each she "
         "which do their time if will way about many then them write would like so these her long make thing "
         "see him two has look more day could go come did number sound no most people my over know water than "
         "call first who may down side been now find any new work part take get place made live where after "
         "back little only round man year came show every good me give our under name very through just form "
         "sentence great think say help low line differ turn cause much mean before move right boy old too same "
         "tell does set three want air well also play small end put home read hand port large spell add even "
         "land here must big high such follow act why ask men change went light kind off need house picture try "
         "us again animal point mother world near build self earth father head stand own page should country "
         "found answer school grow study still learn plant cover food sun four between state keep eye never last "
         "let thought city tree cross farm hard start might story saw far sea draw left late run while press "
         "close night real life few north open seem together next white children begin got walk example ease "
         "paper group always music those both mark often letter until mile river car feet care second book carry "
         "took science eat room friend began idea fish mountain stop once base hear horse cut sure watch color "
         "face wood main enough plain girl usual young ready above ever red list though feel talk bird soon body "
         "dog family direct pose leave song measure door product black short numeral class wind question happen "
         "complete ship area half rock order fire south problem piece told knew pass since top whole king space "
         "heard best hour better true during hundred five remember step early hold west ground interest reach "
         "fast verb sing listen six table travel less morning ten simple several vowel toward war lay against "
         "pattern slow center love person money serve appear road map rain rule govern pull cold notice voice "
         "unit power town fine certain fly fall lead cry dark machine note wait plan figure star box noun field "
         "rest correct able pound done beauty drive stood contain front teach week final gave green oh quick "
         "develop ocean warm free minute strong special mind behind clear tail produce fact street inch multiply "
         "nothing course stay wheel full force blue object decide surface deep moon island foot system busy test "
         "record boat common gold possible plane stead dry wonder laugh thousand ago ran check game shape "
         "equate miss brought heat snow tire bring yes distant fill east paint language among").split()


def sha(b):
    return hashlib.sha256(b).hexdigest()


# ---------------------------------------------------------------- plain samples

def gen_text(rng, n):
    out = []
    size = 0
    while size < n:
        k = rng.randint(4, 18)
        # Zipf-ish word choice: squared uniform favours the head of the list
        ws = [WORDS[int(rng.random() ** 2 * len(WORDS))] for _ in range(k)]
        ws[0] = ws[0].capitalize()
        s = " ".join(ws) + rng.choice([". ", ". ", ", ", "? ", ".\n", ".\n\n"])
        out.append(s)
        size += len(s)
    return "".join(out).encode()[:n]


def gen_json(rng, n):
    levels = ["INFO", "INFO", "INFO", "WARN", "DEBUG", "ERROR"]
    hosts = [f"node-{i:02d}.cluster.local" for i in range(12)]
    paths = ["/api/v1/items", "/api/v1/users", "/healthz", "/api/v2/orders", "/static/app.js"]
    out, size, t = [], 0, 1_700_000_000
    while size < n:
        t += rng.randint(0, 3)
        rec = {"ts": t, "level": rng.choice(levels), "host": rng.choice(hosts), "path": rng.choice(paths),
               "status": rng.choice([200, 200, 200, 204, 301, 404, 500]), "ms": round(rng.expovariate(1 / 40), 2),
               "req": "%016x" % rng.getrandbits(64), "bytes": rng.randint(0, 200000)}
        s = json.dumps(rec, separators=(",", ":")) + "\n"
        out.append(s)
        size += len(s)
    return "".join(out).encode()[:n]


def gen_csv(rng, n):
    out, size, i = ["id,sensor,reading,flag,note\n"], 0, 0
    while size < n:
        i += 1
        s = f"{i},{rng.randint(1, 40)},{rng.gauss(20, 5):.3f},{rng.choice('ABCN')},{rng.choice(['ok', 'ok', 'ok', 'check', ''])}\n"
        out.append(s)
        size += len(s)
    return "".join(out).encode()[:n]


def gen_rle_mix(rng, n):
    out = bytearray()
    while len(out) < n:
        if rng.random() < 0.7:
            out += bytes([rng.randrange(256)]) * rng.randint(1, 5000)
        else:
            out += rng.randbytes(rng.randint(1, 300))
    return bytes(out[:n])


def gen_period(rng, period, n):
    block = rng.randbytes(period)
    reps = n // period + 1
    return (block * reps)[:n]


def image_text(limit):
    """Source files that ship with the image, concatenated in a fixed order."""
    parts, size = [], 0
    for p in sorted(glob.glob("/usr/lib/python3*/*.py")):
        try:
            b = open(p, "rb").read()
        except OSError:
            continue
        parts.append(b)
        size += len(b)
        if size >= limit:
            break
    return b"".join(parts)[:limit]


def image_binary(limit):
    for cand in ("/usr/bin/python3.12", "/usr/bin/python3", "/usr/bin/perl", "/bin/bash"):
        if os.path.exists(cand):
            return open(cand, "rb").read()[:limit]
    return b""


def build_plain(rng):
    k = 1 << 20
    plain = {
        "empty": b"",
        "tiny1": b"\x00",
        "tiny7": b"abcabca",
        "tiny64": rng.randbytes(64),
        "text_2k": gen_text(rng, 2000),
        "text_60k": gen_text(rng, 60_000),
        "text_1m": gen_text(rng, k),
        "pysrc_3m": image_text(3 * k),
        "elf_2m": image_binary(2 * k),
        "json_1m": gen_json(rng, k),
        "csv_1m": gen_csv(rng, k),
        "zeros_1m": bytes(k),
        "rle_mix_1m": gen_rle_mix(rng, k),
        "random_1m": rng.randbytes(k),
        "period_33k_1m": gen_period(rng, 33_000, k),
        "period_20k_1m": gen_period(rng, 20_000, k),
    }
    return plain


# ---------------------------------------------------------------- labelling

WBITS = {"raw": -15, "zlib": 15, "gzip": 31}


def label(fmt, data):
    """What a correct decoder must do with `data` under the CLI contract: ("ok", output) or ("error", None)."""
    if fmt == "zlib" and len(data) >= 2 and (data[1] & 0x20):
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


# ---------------------------------------------------------------- stream makers

def zcompress(data, level, wbits, strategy=zlib.Z_DEFAULT_STRATEGY, memlevel=8):
    c = zlib.compressobj(level, zlib.DEFLATED, wbits, memlevel, strategy)
    return c.compress(data) + c.flush()


def flush_stream(data, wbits, mode, chunk):
    c = zlib.compressobj(6, zlib.DEFLATED, wbits)
    out = b""
    for i in range(0, len(data), chunk):
        out += c.compress(data[i:i + chunk]) + c.flush(mode)
    return out + c.flush()


def gz_member(data, level=6, extra=None, name=None, comment=None, hcrc=False, text=False, mtime=0, xfl=0, os_=3,
              bad=None):
    body = zcompress(data, level, -15)
    flg = 0
    if text:
        flg |= 1
    if hcrc:
        flg |= 2
    if extra is not None:
        flg |= 4
    if name is not None:
        flg |= 8
    if comment is not None:
        flg |= 16
    hdr = bytearray([0x1F, 0x8B, 8, flg]) + struct.pack("<IBB", mtime, xfl, os_)
    if extra is not None:
        hdr += struct.pack("<H", len(extra)) + extra
    if name is not None:
        hdr += name + b"\0"
    if comment is not None:
        hdr += comment + b"\0"
    if hcrc:
        hdr += struct.pack("<H", zlib.crc32(bytes(hdr)) & 0xFFFF)
    trailer = struct.pack("<II", zlib.crc32(data), len(data) & 0xFFFFFFFF)
    out = bytearray(hdr + body + trailer)
    if bad == "reserved5":
        out[3] |= 0x20
    elif bad == "reserved7":
        out[3] |= 0x80
    elif bad == "magic":
        out[1] = 0x8C
    elif bad == "cm":
        out[2] = 7
    elif bad == "hcrc":
        out[len(hdr) - 1] ^= 0x55
    elif bad == "crc":
        out[-5] ^= 1
    elif bad == "isize":
        out[-1] ^= 1
    elif bad == "xlen":
        out[10] = 0xFF
        out[11] = 0xFF
    elif bad == "name_unterminated":
        out = out[: len(hdr) - 1]
    return bytes(out)


def handcrafted():
    """Streams built with the assembler. Name -> (format, bytes). zlib labels them later."""
    cases = {}
    rng = random.Random(SEED ^ 0x77)

    def raw(name, builder):
        w = asm.BitWriter()
        builder(w)
        cases[name] = ("raw", w.bytes())

    txt = gen_text(rng, 300)
    raw("stored_single", lambda w: asm.stored_block(w, b"hello", True))
    raw("stored_empty_final", lambda w: asm.stored_block(w, b"", True))
    raw("stored_two_blocks", lambda w: (asm.stored_block(w, (bytes(range(256)) * 256)[:65535], False),
                                         asm.stored_block(w, b"tail", True)))
    raw("stored_bad_nlen", lambda w: asm.stored_block(w, b"hello", True, bad_nlen=True))
    raw("stored_truncated", lambda w: asm.stored_block(w, b"hello world", True, short_by=4))
    raw("stored_nonfinal_eof", lambda w: asm.stored_block(w, b"abc", False))
    raw("btype3", lambda w: (w.bits(1, 1), w.bits(3, 2), w.bits(0, 5)))
    raw("fixed_literals", lambda w: asm.fixed_block(w, list(txt) + [256], True))
    raw("fixed_match", lambda w: asm.fixed_block(w, list(b"abcabcabc") + [("m", 9, 3), ("m", 3, 9), 256], True))
    raw("fixed_len258_code285", lambda w: asm.fixed_block(w, [65, ("m", 258, 1), ("m", 258, 1), 256], True))
    raw("fixed_len258_code284", lambda w: asm.fixed_block(w, [65, ("m", 258, 1), 256], True, prefer_284=True))
    raw("fixed_dist_too_far_start", lambda w: asm.fixed_block(w, [("m", 3, 1), 256], True))
    raw("fixed_dist_too_far_by_one", lambda w: asm.fixed_block(w, [66, 67, ("m", 5, 3), 256], True))
    raw("fixed_dist_exact", lambda w: asm.fixed_block(w, [66, 67, ("m", 5, 2), 256], True))
    raw("fixed_litlen_286", lambda w: asm.fixed_block(w, [65, ("raw", 286, 0, 0), 256], True))
    raw("fixed_litlen_287", lambda w: asm.fixed_block(w, [65, ("raw", 287, 0, 0), 256], True))
    raw("fixed_dist_30", lambda w: asm.fixed_block(w, [65, 66, 67, ("raw", 257, 0, 0), ("rawdist", 30, 0, 0), 256], True))
    raw("fixed_dist_31", lambda w: asm.fixed_block(w, [65, 66, 67, ("raw", 257, 0, 0), ("rawdist", 31, 0, 0), 256], True))
    raw("fixed_nonfinal_then_eof", lambda w: asm.fixed_block(w, [65, 256], False))
    raw("fixed_missing_eob", lambda w: asm.fixed_block(w, [65, 66], True))

    # window edge: 32768 literal bytes then a match at distance 32768, then one at 32768 again
    big = rng.randbytes(WINDOW)
    raw("fixed_window_edge", lambda w: asm.fixed_block(w, list(big) + [("m", 258, WINDOW), ("m", 100, WINDOW), 256], True))
    raw("fixed_window_edge_plus_one", lambda w: asm.fixed_block(w, list(big[:-1]) + [("m", 3, WINDOW), 256], True))

    # dynamic blocks
    lits = list(b"the rain in spain stays mainly in the plain ") * 3
    used = set(lits) | {256, 257, 258, 259}

    def dyn_single_dist(w):
        ll = asm.complete_lengths(286, used)
        dl = [0] * 30
        dl[2] = 1  # a single distance code (distance 3), an incomplete tree of one code
        asm.dynamic_block(w, ll, dl, lits + [("m", 3, 3), ("m", 5, 3), 256], True)
    raw("dyn_single_dist_code", dyn_single_dist)

    def dyn_no_dist(w):
        ll = asm.complete_lengths(286, set(lits) | {256})
        asm.dynamic_block(w, ll, [0], lits + [256], True)
    raw("dyn_no_dist_codes", dyn_no_dist)

    def dyn_no_dist_but_match(w):
        ll = asm.complete_lengths(286, used)
        asm.dynamic_block(w, ll, [0], lits + [("raw", 257, 0, 0)], True)
        w.bits(0, 16)  # whatever follows, no distance code can be read from an empty distance tree
    raw("dyn_no_dist_codes_match_used", dyn_no_dist_but_match)

    def dyn_spill_repeat(w):
        # literal lengths 257..285 are zero and so are distance lengths 0..27: one repeat code 18 covers
        # the whole run across the boundary between the two tables
        ll = asm.complete_lengths(286, set(lits) | {256})
        dl = [0] * 30
        dl[28] = 1
        dl[29] = 1
        asm.dynamic_block(w, ll, dl, lits + [256], True)
    raw("dyn_repeat_spills_into_dist", dyn_spill_repeat)

    def dyn_full_alphabet(w):
        ll = asm.complete_lengths(286, set(range(286)))
        dl = asm.complete_lengths(30, set(range(30)))
        syms = list(range(256)) + [("m", 258, 1), ("m", 3, 256), 256]
        asm.dynamic_block(w, ll, dl, syms, True)
    raw("dyn_full_alphabets", dyn_full_alphabet)

    def dyn_hlit_30(w):
        ll = asm.complete_lengths(287, set(lits) | {256})
        asm.dynamic_block(w, ll, [1, 1], lits + [256], True)
    raw("dyn_hlit_too_large", dyn_hlit_30)

    def dyn_hdist_32(w):
        ll = asm.complete_lengths(286, set(lits) | {256})
        asm.dynamic_block(w, ll, asm.complete_lengths(32, set(range(32))), lits + [256], True)
    raw("dyn_hdist_too_large", dyn_hdist_32)

    def dyn_oversubscribed(w):
        ll = asm.complete_lengths(286, set(lits) | {256})
        bad = list(ll)
        s = min(x for x in set(lits))
        bad[s] = max(1, bad[s] - 1)  # shorten one code: over-subscribed
        asm.dynamic_block(w, bad, [1, 1], lits + [256], True)
    raw("dyn_lit_oversubscribed", dyn_oversubscribed)

    def dyn_incomplete(w):
        ll = asm.complete_lengths(286, set(lits) | {256})
        bad = list(ll)
        s = min(x for x in set(lits))
        bad[s] += 1  # lengthen one code: incomplete
        asm.dynamic_block(w, bad, [1, 1], lits + [256], True)
    raw("dyn_lit_incomplete", dyn_incomplete)

    def dyn_dist_oversubscribed(w):
        ll = asm.complete_lengths(286, used)
        dl = [0] * 30
        dl[0] = dl[1] = dl[2] = 1
        asm.dynamic_block(w, ll, dl, lits + [("m", 3, 3), 256], True)
    raw("dyn_dist_oversubscribed", dyn_dist_oversubscribed)

    def dyn_dist_incomplete_two(w):
        ll = asm.complete_lengths(286, used)
        dl = [0] * 30
        dl[0] = 1
        dl[2] = 2  # two codes, incomplete
        asm.dynamic_block(w, ll, dl, lits + [("m", 3, 3), 256], True)
    raw("dyn_dist_incomplete_two_codes", dyn_dist_incomplete_two)

    def dyn_missing_eob(w):
        ll = asm.complete_lengths(286, set(lits) | {257})
        asm.dynamic_block(w, ll, [1, 1], lits, True, body=True)
    raw("dyn_missing_eob_code", dyn_missing_eob)

    def dyn_only_eob(w):
        ll = [0] * 286
        ll[256] = 1
        asm.dynamic_block(w, ll, [1, 1], [256], True)
    raw("dyn_only_eob_symbol", dyn_only_eob)

    def dyn_repeat_first(w):
        ll = asm.complete_lengths(286, set(lits) | {256})
        items = asm.run_length_code_lengths(ll + [1, 1])
        items = [(16, 0)] + items
        asm.dynamic_block(w, ll, [1, 1], lits + [256], True, cl_items=items)
    raw("dyn_repeat_code_first", dyn_repeat_first)

    def dyn_repeat_overrun(w):
        ll = asm.complete_lengths(286, set(lits) | {256})
        items = asm.run_length_code_lengths(ll + [1, 1]) + [(18, 127)]
        asm.dynamic_block(w, ll, [1, 1], lits + [256], True, cl_items=items)
    raw("dyn_repeat_overruns_table", dyn_repeat_overrun)

    def dyn_cl_oversubscribed(w):
        ll = asm.complete_lengths(286, set(lits) | {256})
        items = asm.run_length_code_lengths(ll + [1, 1])
        usedcl = sorted({c for c, _ in items})
        cl = [0] * 19
        for c in usedcl:
            cl[c] = 1  # all length 1: over-subscribed unless two symbols
        asm.dynamic_block(w, ll, [1, 1], lits + [256], True, cl_items=items, cl_lengths=cl)
    raw("dyn_code_length_code_oversubscribed", dyn_cl_oversubscribed)

    def dyn_cl_incomplete(w):
        ll = asm.complete_lengths(286, set(lits) | {256})
        items = asm.run_length_code_lengths(ll + [1, 1])
        usedcl = sorted({c for c, _ in items})
        cl = [0] * 19
        for c in usedcl:
            cl[c] = 7
        asm.dynamic_block(w, ll, [1, 1], lits + [256], True, cl_items=items, cl_lengths=cl)
    raw("dyn_code_length_code_incomplete", dyn_cl_incomplete)

    def dyn_cl_single(w):
        # one code length symbol only: every length is 8 for 256 literals... use a single symbol "8"
        ll = [8] * 256 + [8] + [0] * 29
        # 257 symbols of length 8 is over-subscribed by one; use 255 literals + eob = 256 codes of length 8
        ll = [8] * 255 + [0] + [8] + [0] * 29
        items = [(8, None)] * 255 + [(0, None)] + [(8, None)] + [(0, None)] * 29 + [(1, None), (1, None)]
        cl = [0] * 19
        cl[8] = 1
        cl[0] = 2
        cl[1] = 2
        syms = [s for s in lits if s < 255] + [256]
        asm.dynamic_block(w, ll, [1, 1], syms, True, cl_items=items, cl_lengths=cl)
    raw("dyn_code_lengths_mostly_eight", dyn_cl_single)

    def mixed_blocks(w):
        a = gen_text(rng, 700)
        asm.stored_block(w, a, False)
        asm.fixed_block(w, [("m", 100, 700), ("m", 258, 350), 256], False)
        ll = asm.complete_lengths(286, set(a) | {256, 257, 264, 285})
        dl = asm.complete_lengths(30, set(range(30)))
        asm.dynamic_block(w, ll, dl, list(a[:50]) + [("m", 258, 1000), ("m", 3, 1), ("m", 10, 1300), 256], False)
        asm.stored_block(w, b"", True)
    raw("mixed_block_types_cross_block_matches", mixed_blocks)

    def trailing_bits_only(w):
        asm.fixed_block(w, [65, 66, 256], True)
        # pad with zero bits inside the last byte only: still one stream
    raw("fixed_padding_bits", trailing_bits_only)

    def extra_zero_byte(w):
        asm.fixed_block(w, [65, 66, 256], True)
        w.align()
        w.buf.append(0)
    raw("fixed_trailing_zero_byte", extra_zero_byte)

    # zlib container specifics
    body = zcompress(b"zlib container test " * 20, 6, -15)
    adler = struct.pack(">I", zlib.adler32(b"zlib container test " * 20))

    def zhdr(cmf, flg_hi):
        flg = flg_hi
        rem = (cmf * 256 + flg) % 31
        if rem:
            flg += 31 - rem
        return bytes([cmf, flg])
    cases["zlib_cinfo_0"] = ("zlib", zhdr(0x08, 0x40) + body + adler)
    cases["zlib_cinfo_7_level0_flag"] = ("zlib", zhdr(0x78, 0x00) + body + adler)
    cases["zlib_cinfo_8"] = ("zlib", zhdr(0x88, 0x40) + body + adler)
    cases["zlib_cm_7"] = ("zlib", zhdr(0x77, 0x40) + body + adler)
    cases["zlib_bad_fcheck"] = ("zlib", bytes([0x78, 0x9D]) + body + adler)
    cases["zlib_fdict_set"] = ("zlib", zhdr(0x78, 0x60) + b"\0\0\0\1" + body + adler)
    cases["zlib_bad_adler"] = ("zlib", zhdr(0x78, 0x40) + body + bytes(a ^ 0x80 for a in adler))
    cases["zlib_short_adler"] = ("zlib", zhdr(0x78, 0x40) + body + adler[:3])
    cases["zlib_header_only"] = ("zlib", zhdr(0x78, 0x40))
    cases["zlib_one_byte"] = ("zlib", b"\x78")
    cases["zlib_empty_input"] = ("zlib", b"")
    cases["raw_empty_input"] = ("raw", b"")
    cases["gzip_empty_input"] = ("gzip", b"")
    cases["gzip_header_only"] = ("gzip", b"\x1f\x8b\x08\x00\x00\x00\x00\x00\x00\x03")
    cases["gzip_nine_bytes"] = ("gzip", b"\x1f\x8b\x08\x00\x00\x00\x00\x00\x00")
    return cases


# ---------------------------------------------------------------- main

def main(outdir):
    rng = random.Random(SEED)
    os.makedirs(outdir, exist_ok=True)
    plain_dir = os.path.join(outdir, "plain")
    inf_dir = os.path.join(outdir, "inflate")
    perf_dir = os.path.join(outdir, "perf")
    for d in (plain_dir, inf_dir, perf_dir):
        os.makedirs(d, exist_ok=True)

    plain = build_plain(rng)
    manifest = {"plain": [], "inflate": [], "zlib_sizes": {}, "perf": {}}
    for name, data in plain.items():
        with open(os.path.join(plain_dir, name + ".bin"), "wb") as fh:
            fh.write(data)
        manifest["plain"].append({"name": name, "size": len(data), "sha256": sha(data)})
        if len(data) >= 10_000:
            manifest["zlib_sizes"][name] = {str(l): len(zcompress(data, l, -15)) for l in (1, 6, 9)}

    cases = []  # (id, fmt, group, data)

    def add(cid, fmt, group, data):
        cases.append((cid, fmt, group, data))

    # standard streams produced by zlib
    small = [n for n, d in plain.items() if len(d) <= 65536]
    bigs = [n for n, d in plain.items() if len(d) > 65536]
    strategies = {"default": zlib.Z_DEFAULT_STRATEGY, "huff": zlib.Z_HUFFMAN_ONLY, "rle": zlib.Z_RLE, "fixed": zlib.Z_FIXED,
                  "filtered": zlib.Z_FILTERED}
    containers = {"zlib15": ("zlib", 15), "zlib9": ("zlib", 9), "raw15": ("raw", -15), "raw10": ("raw", -10),
                  "gzip": ("gzip", 31)}
    for n in small:
        for level in (0, 1, 4, 6, 9):
            for sname, strat in strategies.items():
                if sname == "filtered" and level != 6:
                    continue
                for cname, (fmt, wb) in containers.items():
                    add(f"std_{n}_L{level}_{sname}_{cname}", fmt, "std", zcompress(plain[n], level, wb, strat))
    for n in bigs:
        for level in (1, 6, 9):
            for cname in ("zlib15", "raw15", "gzip"):
                fmt, wb = containers[cname]
                add(f"std_{n}_L{level}_default_{cname}", fmt, "std", zcompress(plain[n], level, wb))
        add(f"std_{n}_L6_rle_zlib15", "zlib", "std", zcompress(plain[n], 6, 15, zlib.Z_RLE))
        add(f"std_{n}_L6_huff_raw15", "raw", "std", zcompress(plain[n], 6, -15, zlib.Z_HUFFMAN_ONLY))
        add(f"std_{n}_L9_default_memlevel1_zlib15", "zlib", "std", zcompress(plain[n], 9, 15, memlevel=1))
    # flush modes leave empty blocks and block boundaries inside the stream
    for mode, mname in ((zlib.Z_SYNC_FLUSH, "sync"), (zlib.Z_FULL_FLUSH, "full"), (zlib.Z_PARTIAL_FLUSH, "partial"),
                        (zlib.Z_BLOCK, "block")):
        for cname in ("zlib15", "raw15", "gzip"):
            fmt, wb = containers[cname]
            add(f"std_flush_{mname}_{cname}", fmt, "std", flush_stream(plain["text_60k"], wb, mode, 7001))
            add(f"std_flush_{mname}_json_{cname}", fmt, "std", flush_stream(plain["json_1m"][:200_000], wb, mode, 13_337))

    # gzip header features and multi-member files
    for n in ("empty", "tiny7", "text_60k"):
        d = plain[n]
        for mask in range(16):
            kw = {}
            if mask & 1:
                kw["extra"] = b"AP\x04\x00\x01\x02\x03\x04"
            if mask & 2:
                kw["name"] = b"sample.txt"
            if mask & 4:
                kw["comment"] = b"made for the grader"
            if mask & 8:
                kw["hcrc"] = True
            add(f"gz_{n}_flags{mask:02d}", "gzip", "gzip_hdr", gz_member(d, text=bool(mask & 1), mtime=1_700_000_000 if mask & 2 else 0,
                                                                         xfl=2 if mask & 4 else 0, os_=255 if mask & 8 else 3, **kw))
    members = [gz_member(plain["tiny7"]), gz_member(plain["text_2k"], name=b"a"), gz_member(b""),
               gz_member(plain["text_60k"], level=1, hcrc=True), gz_member(plain["tiny64"], comment=b"c", extra=b"\x01\x02")]
    add("gz_members_2", "gzip", "gzip_hdr", members[0] + members[1])
    add("gz_members_3_with_empty", "gzip", "gzip_hdr", members[1] + members[2] + members[3])
    add("gz_members_5", "gzip", "gzip_hdr", b"".join(members))
    add("gz_members_empty_twice", "gzip", "gzip_hdr", members[2] + members[2])
    add("gz_member_zero_length_extra", "gzip", "gzip_hdr", gz_member(plain["text_2k"], extra=b""))
    add("gz_member_empty_name", "gzip", "gzip_hdr", gz_member(plain["text_2k"], name=b"", comment=b""))
    for bad in ("reserved5", "reserved7", "magic", "cm", "hcrc", "crc", "isize", "xlen", "name_unterminated"):
        kw = {"hcrc": True} if bad == "hcrc" else {}
        if bad == "xlen":
            kw["extra"] = b"xy"
        if bad == "name_unterminated":
            kw["name"] = b"n"
        add(f"gz_bad_{bad}", "gzip", "reject", gz_member(plain["text_2k"], **kw, bad=bad))
    add("gz_members_second_truncated", "gzip", "reject", members[0] + members[1][:-3])
    add("gz_members_then_partial_header", "gzip", "reject", members[0] + b"\x1f\x8b\x08")
    add("gz_trailing_zero", "gzip", "reject", members[0] + b"\x00")

    # hand-built bitstreams
    for name, (fmt, data) in handcrafted().items():
        add(f"hand_{name}", fmt, "hand", data)

    # truncation, corruption, trailing bytes applied to standard streams
    pool = [c for c in cases if c[2] == "std" and 20 <= len(c[3]) <= 300_000]
    rng2 = random.Random(SEED ^ 0x1234)
    for cid, fmt, _, data in rng2.sample(pool, 36):
        for k in range(2):
            cut = rng2.randint(1, len(data) - 1)
            add(f"trunc_{cid}_{k}", fmt, "reject", data[:cut])
    for cid, fmt, _, data in rng2.sample(pool, 40):
        b = bytearray(data)
        pos = rng2.randrange(len(b))
        b[pos] ^= 1 << rng2.randrange(8)
        add(f"flip_{cid}", fmt, "flip", bytes(b))
    for cid, fmt, _, data in rng2.sample(pool, 24):
        add(f"trail_{cid}", fmt, "reject", data + rng2.randbytes(rng2.randint(1, 4)))

    # label and write
    seen = set()
    for cid, fmt, group, data in cases:
        assert cid not in seen, cid
        seen.add(cid)
        verdict, out = label(fmt, data)
        with open(os.path.join(inf_dir, cid + ".in"), "wb") as fh:
            fh.write(data)
        entry = {"id": cid, "fmt": fmt, "group": group, "expect": verdict, "in_len": len(data)}
        if verdict == "ok":
            entry["sha256"] = sha(out)
            entry["out_len"] = len(out)
        # a bit flip that zlib still accepts is scored with the "hand" valid streams; a rejected one with the rejects
        if group == "flip":
            entry["group"] = "hand" if verdict == "ok" else "reject"
        manifest["inflate"].append(entry)

    # performance inputs
    mix = b"".join([plain["text_1m"], plain["pysrc_3m"], plain["elf_2m"], plain["json_1m"], plain["csv_1m"],
                    plain["rle_mix_1m"], plain["random_1m"][:262144], plain["period_20k_1m"]])
    perf_deflate = (mix * 2)[: 16 << 20]
    perf_inflate_plain = (mix * 6)[: 48 << 20]
    with open(os.path.join(perf_dir, "deflate_in.bin"), "wb") as fh:
        fh.write(perf_deflate)
    inflate_in = zcompress(perf_inflate_plain, 6, 15)
    with open(os.path.join(perf_dir, "inflate_in.zz"), "wb") as fh:
        fh.write(inflate_in)
    manifest["perf"] = {"deflate_in": "perf/deflate_in.bin", "deflate_in_len": len(perf_deflate),
                        "inflate_in": "perf/inflate_in.zz", "inflate_in_len": len(inflate_in),
                        "inflate_out_len": len(perf_inflate_plain), "inflate_out_sha256": sha(perf_inflate_plain)}

    with open(os.path.join(outdir, "manifest.json"), "w") as fh:
        json.dump(manifest, fh, indent=1)
    groups = {}
    for e in manifest["inflate"]:
        groups[(e["group"], e["expect"])] = groups.get((e["group"], e["expect"]), 0) + 1
    total_in = sum(e["in_len"] for e in manifest["inflate"])
    print(f"plain samples: {len(plain)}; inflate cases: {len(manifest['inflate'])} ({total_in / 2**20:.1f} MB)")
    for k in sorted(groups):
        print(f"  {k[0]:9s} {k[1]:5s} {groups[k]}")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "/root/grader/corpus")
