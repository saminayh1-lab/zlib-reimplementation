"""A small DEFLATE bitstream assembler.

It writes stored, fixed and dynamic blocks from explicit code lengths and symbol lists, so the
corpus can hold streams that zlib itself never emits: incomplete distance trees, repeat codes that
spill from the literal lengths into the distance lengths, length 258 spelled with code 284, and a
range of malformed trees. Whether each stream is valid is never decided here; zlib decides when the
corpus is built.
"""


class BitWriter:
    def __init__(self):
        self.buf = bytearray()
        self.acc = 0
        self.n = 0

    def bits(self, value, count):
        """Write `count` bits of `value`, least significant bit first (deflate order)."""
        self.acc |= (value & ((1 << count) - 1)) << self.n
        self.n += count
        while self.n >= 8:
            self.buf.append(self.acc & 0xFF)
            self.acc >>= 8
            self.n -= 8

    def code(self, code, length):
        """Write a Huffman code, most significant bit first (deflate order for codes)."""
        rev = 0
        for _ in range(length):
            rev = (rev << 1) | (code & 1)
            code >>= 1
        self.bits(rev, length)

    def align(self):
        if self.n:
            self.buf.append(self.acc & 0xFF)
            self.acc = 0
            self.n = 0

    def bytes(self):
        out = bytes(self.buf)
        if self.n:
            out += bytes([self.acc & 0xFF])
        return out


def canonical_codes(lengths):
    """RFC 1951 section 3.2.2: codes from a list of code lengths (0 = unused)."""
    max_len = max(lengths) if lengths else 0
    bl_count = [0] * (max_len + 2)
    for l in lengths:
        if l:
            bl_count[l] += 1
    code = 0
    next_code = [0] * (max_len + 2)
    for b in range(1, max_len + 1):
        code = (code + bl_count[b - 1]) << 1
        next_code[b] = code
    codes = [None] * len(lengths)
    for sym, l in enumerate(lengths):
        if l:
            codes[sym] = (next_code[l], l)
            next_code[l] += 1
    return codes


LENGTH_BASE = [3, 4, 5, 6, 7, 8, 9, 10, 11, 13, 15, 17, 19, 23, 27, 31, 35, 43, 51, 59, 67, 83, 99, 115,
               131, 163, 195, 227, 258]
LENGTH_EXTRA = [0, 0, 0, 0, 0, 0, 0, 0, 1, 1, 1, 1, 2, 2, 2, 2, 3, 3, 3, 3, 4, 4, 4, 4, 5, 5, 5, 5, 0]
DIST_BASE = [1, 2, 3, 4, 5, 7, 9, 13, 17, 25, 33, 49, 65, 97, 129, 193, 257, 385, 513, 769, 1025, 1537,
             2049, 3073, 4097, 6145, 8193, 12289, 16385, 24577]
DIST_EXTRA = [0, 0, 0, 0, 1, 1, 2, 2, 3, 3, 4, 4, 5, 5, 6, 6, 7, 7, 8, 8, 9, 9, 10, 10, 11, 11, 12, 12, 13, 13]
CL_ORDER = [16, 17, 18, 0, 8, 7, 9, 6, 10, 5, 11, 4, 12, 3, 13, 2, 14, 1, 15]

FIXED_LIT_LENGTHS = [8] * 144 + [9] * 112 + [7] * 24 + [8] * 8
FIXED_DIST_LENGTHS = [5] * 32


def length_symbol(length, prefer_284=False):
    """(symbol, extra_bits, extra_value) for a match length 3..258."""
    if length == 258 and not prefer_284:
        return 285, 0, 0
    for i in range(28):
        base, extra = LENGTH_BASE[i], LENGTH_EXTRA[i]
        if base <= length < base + (1 << extra) or (extra == 0 and length == base):
            return 257 + i, extra, length - base
    raise ValueError(length)


def dist_symbol(dist):
    for i in range(30):
        base, extra = DIST_BASE[i], DIST_EXTRA[i]
        if base <= dist < base + (1 << extra) or (extra == 0 and dist == base):
            return i, extra, dist - base
    raise ValueError(dist)


def emit_symbols(w, lit_codes, dist_codes, symbols, prefer_284=False):
    """symbols: ints 0..255 are literals, ('m', length, dist) a match, ('raw', sym, extra_bits, extra) a
    hand-picked length symbol with explicit extra bits, ('rawdist', sym, extra_bits, extra) likewise for a
    distance symbol, 256 is end of block."""
    for s in symbols:
        if isinstance(s, int):
            c = lit_codes[s]
            w.code(*c)
        elif s[0] == "m":
            _, length, dist = s
            sym, eb, ev = length_symbol(length, prefer_284)
            w.code(*lit_codes[sym])
            if eb:
                w.bits(ev, eb)
            dsym, deb, dev = dist_symbol(dist)
            w.code(*dist_codes[dsym])
            if deb:
                w.bits(dev, deb)
        elif s[0] == "raw":
            _, sym, eb, ev = s
            w.code(*lit_codes[sym])
            if eb:
                w.bits(ev, eb)
        elif s[0] == "rawdist":
            _, sym, eb, ev = s
            w.code(*dist_codes[sym])
            if eb:
                w.bits(ev, eb)
        else:
            raise ValueError(s)


def stored_block(w, data, final, bad_nlen=False, short_by=0):
    w.bits(1 if final else 0, 1)
    w.bits(0, 2)
    w.align()
    n = len(data)
    nlen = (~n) & 0xFFFF
    if bad_nlen:
        nlen ^= 1
    w.buf += n.to_bytes(2, "little") + nlen.to_bytes(2, "little")
    w.buf += data[: len(data) - short_by] if short_by else data


def fixed_block(w, symbols, final, prefer_284=False):
    w.bits(1 if final else 0, 1)
    w.bits(1, 2)
    emit_symbols(w, canonical_codes(FIXED_LIT_LENGTHS), canonical_codes(FIXED_DIST_LENGTHS), symbols, prefer_284)


def run_length_code_lengths(lengths):
    """Encode a code-length sequence with repeat codes 16/17/18 the way zlib does."""
    out = []
    i = 0
    n = len(lengths)
    while i < n:
        l = lengths[i]
        j = i
        while j < n and lengths[j] == l:
            j += 1
        run = j - i
        if l == 0:
            while run >= 11:
                r = min(run, 138)
                out.append((18, r - 11))
                run -= r
            if run >= 3:
                out.append((17, run - 3))
                run = 0
            out.extend([(0, None)] * run)
        else:
            out.append((l, None))
            run -= 1
            while run >= 3:
                r = min(run, 6)
                out.append((16, r - 3))
                run -= r
            out.extend([(l, None)] * run)
        i = j
    return out


def dynamic_block(w, lit_lengths, dist_lengths, symbols, final, cl_items=None, hlit=None, hdist=None,
                  cl_lengths=None, hclen=None, prefer_284=False, body=True):
    """Write a dynamic block. By default every header field is derived from the lengths; each can be
    overridden to build a malformed header. `cl_items` is a list of (code, extra) pairs for the code
    length alphabet, replacing the derived run-length encoding."""
    w.bits(1 if final else 0, 1)
    w.bits(2, 2)
    nlit = len(lit_lengths)
    ndist = len(dist_lengths)
    if hlit is None:
        hlit = nlit - 257
    if hdist is None:
        hdist = ndist - 1
    if cl_items is None:
        cl_items = run_length_code_lengths(list(lit_lengths) + list(dist_lengths))
    if cl_lengths is None:
        used = sorted({c for c, _ in cl_items})
        # a complete code over the used code-length symbols
        cl_lengths = [0] * 19
        if len(used) == 1:
            cl_lengths[used[0]] = 1
        else:
            k = max(1, (len(used) - 1).bit_length())
            for c in used:
                cl_lengths[c] = k
            # make it complete by shortening codes where possible
            cl_lengths = _complete_lengths(used, k)
    if hclen is None:
        last = max(i for i in range(19) if cl_lengths[CL_ORDER[i]]) if any(cl_lengths) else 3
        hclen = max(last + 1, 4) - 4
    w.bits(hlit, 5)
    w.bits(hdist, 5)
    w.bits(hclen, 4)
    for i in range(hclen + 4):
        w.bits(cl_lengths[CL_ORDER[i]], 3)
    cl_codes = canonical_codes(cl_lengths)
    for c, extra in cl_items:
        w.code(*cl_codes[c])
        if c == 16:
            w.bits(extra, 2)
        elif c == 17:
            w.bits(extra, 3)
        elif c == 18:
            w.bits(extra, 7)
    if body:
        emit_symbols(w, canonical_codes(lit_lengths), canonical_codes(dist_lengths), symbols, prefer_284)


def _complete_lengths(used, k):
    """Code lengths over the 19 code-length symbols forming a complete prefix code on `used`."""
    n = len(used)
    lengths = [0] * 19
    # Huffman over equal weights: n leaves, depths k or k-1
    full = 1 << k
    short = full - n          # number of symbols that get length k-1
    for i, c in enumerate(used):
        lengths[c] = (k - 1) if i < short and k > 1 else k
    return lengths


def lengths_for_symbols(symbols, max_bits=15):
    """Package-merge-free helper: equal-ish code lengths for the symbols used, complete code."""
    used = sorted(set(symbols))
    k = max(1, (len(used) - 1).bit_length())
    n = len(used)
    full = 1 << k
    short = full - n
    lengths = [0] * (max(used) + 1)
    for i, s in enumerate(used):
        lengths[s] = (k - 1) if i < short and k > 1 else k
    return lengths


def complete_lengths(nsyms, used):
    """Complete code lengths over `nsyms` symbols where only `used` symbols get codes."""
    used = sorted(used)
    n = len(used)
    if n == 1:
        lengths = [0] * nsyms
        lengths[used[0]] = 1
        return lengths
    k = max(1, (n - 1).bit_length())
    full = 1 << k
    short = full - n
    lengths = [0] * nsyms
    for i, s in enumerate(used):
        lengths[s] = (k - 1) if i < short and k > 1 else k
    return lengths
