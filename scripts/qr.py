#!/usr/bin/env python3
"""VPS Manager Suite - self-contained QR code generator (original code)

Encodes text (byte mode, error-correction level M) into a QR matrix and
renders it as a compact inline SVG. No third-party code and no external
tools: the panel uses it to show scannable QR codes for Xray links on a
stock system where pip packages cannot be assumed.

Implementation notes (QR Code 2005 / ISO 18004):
  - Galois field GF(256) with primitive polynomial 0x11D, Reed-Solomon
    error correction, block interleaving (short/long block groups are
    derived arithmetically from the per-version totals below)
  - automatic version selection, versions 1..20 (up to ~660 payload
    bytes at level M - enough for the suite's longest share links)
  - all 8 masks are tried and scored with the standard penalty rules;
    the lowest-penalty result wins
The per-version constants (total codewords, EC codewords per block and
block counts at level M) are the specification's factual tables; the
encoder was verified module-for-module against two independent QR
libraries during development.

Public API:
    matrix(text) -> list of rows, each a list of bool (True = dark)
    svg(text, module=4, border=2) -> SVG string
"""
import sys

# version -> (total codewords, ec codewords per block, number of blocks) @ M
_SPECS = {
    1: (26, 10, 1), 2: (44, 16, 1), 3: (70, 26, 1), 4: (100, 18, 2),
    5: (134, 24, 2), 6: (172, 16, 4), 7: (196, 18, 4), 8: (242, 22, 4),
    9: (292, 22, 5), 10: (346, 26, 5), 11: (404, 30, 5), 12: (466, 22, 8),
    13: (532, 22, 9), 14: (581, 24, 9), 15: (655, 24, 10), 16: (733, 28, 10),
    17: (815, 28, 11), 18: (901, 26, 13), 19: (991, 26, 14), 20: (1085, 26, 16),
}
_ALIGN = {
    1: [], 2: [6, 18], 3: [6, 22], 4: [6, 26], 5: [6, 30], 6: [6, 34],
    7: [6, 22, 38], 8: [6, 24, 42], 9: [6, 26, 46], 10: [6, 28, 50],
    11: [6, 30, 54], 12: [6, 32, 58], 13: [6, 34, 62], 14: [6, 26, 46, 66],
    15: [6, 26, 48, 70], 16: [6, 26, 50, 74], 17: [6, 30, 54, 78],
    18: [6, 30, 56, 82], 19: [6, 30, 58, 86], 20: [6, 34, 62, 90],
}

# ---------------------------------------------------------------------------
# GF(256) arithmetic + Reed-Solomon
# ---------------------------------------------------------------------------
_EXP = [0] * 512
_LOG = [0] * 256
_x = 1
for _i in range(255):
    _EXP[_i] = _x
    _LOG[_x] = _i
    _x <<= 1
    if _x & 0x100:
        _x ^= 0x11D
for _i in range(255, 512):
    _EXP[_i] = _EXP[_i - 255]


def _mul(a, b):
    return 0 if (a == 0 or b == 0) else _EXP[_LOG[a] + _LOG[b]]


def _poly_mul(p, q):
    out = [0] * (len(p) + len(q) - 1)
    for i, a in enumerate(p):
        for j, b in enumerate(q):
            out[i + j] ^= _mul(a, b)
    return out


def _rs_generator(deg):
    poly = [1]
    for i in range(deg):
        poly = _poly_mul(poly, [1, _EXP[i]])
    return poly


def _rs_ec(data, gen):
    deg = len(gen) - 1
    res = [0] * deg
    for b in data:
        factor = b ^ res.pop(0)
        res.append(0)
        for i in range(deg):
            res[i] ^= _mul(gen[i + 1], factor)
    return res


# ---------------------------------------------------------------------------
# bit stream helpers
# ---------------------------------------------------------------------------
class _Bits:
    def __init__(self):
        self.bits = []

    def put(self, val, n):
        for i in range(n - 1, -1, -1):
            self.bits.append((val >> i) & 1)

    def __len__(self):
        return len(self.bits)


def _data_codewords(payload, version):
    total, ec_per, nblocks = _SPECS[version]
    data_total = total - ec_per * nblocks
    bs = _Bits()
    bs.put(0b0100, 4)                                   # byte mode
    bs.put(len(payload), 8 if version <= 9 else 16)     # length field
    for b in payload:
        bs.put(b, 8)
    cap = data_total * 8
    if len(bs) > cap:
        raise ValueError("payload too long for version %d" % version)
    bs.put(0, min(4, cap - len(bs)))                    # terminator
    bs.put(0, (-len(bs)) % 8)                           # byte alignment
    out = []
    for i in range(0, len(bs.bits), 8):
        byte = 0
        for bit in bs.bits[i:i + 8]:
            byte = (byte << 1) | bit
        out.append(byte)
    pads = (0xEC, 0x11)
    k = 0
    while len(out) < data_total:
        out.append(pads[k % 2])
        k += 1
    return out, data_total


def _interleave(payload, version):
    total, ec_per, nblocks = _SPECS[version]
    data, data_total = _data_codewords(payload, version)
    gen = _rs_generator(ec_per)
    # block split: (nblocks - long) short blocks, then long blocks (+1 byte)
    short_len = data_total // nblocks
    long_n = data_total % nblocks
    blocks, ecs = [], []
    pos = 0
    for i in range(nblocks):
        ln = short_len + (1 if i >= nblocks - long_n else 0)
        blk = data[pos:pos + ln]
        pos += ln
        blocks.append(blk)
        ecs.append(_rs_ec(blk, gen))
    out = []
    for i in range(max(len(b) for b in blocks)):
        for b in blocks:
            if i < len(b):
                out.append(b[i])
    for i in range(ec_per):
        for e in ecs:
            out.append(e[i])
    return out


# ---------------------------------------------------------------------------
# matrix construction
# ---------------------------------------------------------------------------
def _blank(version):
    size = version * 4 + 17
    m = [[False] * size for _ in range(size)]
    fn = [[False] * size for _ in range(size)]  # function-module map

    def setf(x, y, dark):
        if 0 <= x < size and 0 <= y < size:
            m[y][x] = bool(dark)
            fn[y][x] = True

    # finder patterns + separators
    for dx, dy in ((0, 0), (size - 7, 0), (0, size - 7)):
        for yy in range(-1, 8):
            for xx in range(-1, 8):
                x, y = dx + xx, dy + yy
                if not (0 <= x < size and 0 <= y < size):
                    continue
                dark = (0 <= xx <= 6 and (yy in (0, 6) or 0 <= yy <= 6 and xx in (0, 6))) or \
                       (2 <= xx <= 4 and 2 <= yy <= 4)
                if xx == -1 or xx == 7 or yy == -1 or yy == 7:
                    dark = False
                setf(x, y, dark)
    # timing patterns
    for i in range(8, size - 8):
        setf(i, 6, i % 2 == 0)
        setf(6, i, i % 2 == 0)
    # alignment patterns - skipped ONLY at the three finder corners
    # (patterns centered on the timing lines are real and must be drawn)
    centers = _ALIGN[version]
    if centers:
        corners = {(centers[0], centers[0]), (centers[-1], centers[0]),
                   (centers[0], centers[-1])}
        for cy in centers:
            for cx in centers:
                if (cx, cy) in corners:
                    continue
                for yy in range(-2, 3):
                    for xx in range(-2, 3):
                        setf(cx + xx, cy + yy, max(abs(xx), abs(yy)) != 1)
    # dark module
    setf(8, size - 8, True)
    # reserve format info areas
    for i in range(9):
        if i != 6:
            fn[i][8] = fn[8][i] = True
    for i in range(8):
        fn[size - 1 - i][8] = True
        fn[8][size - 1 - i] = True
    # version info areas (version >= 7)
    if version >= 7:
        for i in range(18):
            fn[i // 3][size - 11 + i % 3] = True
            fn[size - 11 + i % 3][i // 3] = True
    return m, fn, size


def _place(m, fn, size, codewords):
    bits = []
    for byte in codewords:
        for i in range(7, -1, -1):
            bits.append((byte >> i) & 1)
    idx = 0
    right = size - 1
    upward = True
    while right >= 1:
        if right == 6:
            right = 5
        rows = range(size - 1, -1, -1) if upward else range(size)
        for y in rows:
            for x in (right, right - 1):
                if not fn[y][x]:
                    m[y][x] = bool(bits[idx]) if idx < len(bits) else False
                    idx += 1
        upward = not upward
        right -= 2


def _mask_bit(mask, x, y):
    if mask == 0:
        return (x + y) % 2 == 0
    if mask == 1:
        return y % 2 == 0
    if mask == 2:
        return x % 3 == 0
    if mask == 3:
        return (x + y) % 3 == 0
    if mask == 4:
        return (x // 3 + y // 2) % 2 == 0
    if mask == 5:
        return (x * y) % 2 + (x * y) % 3 == 0
    if mask == 6:
        return ((x * y) % 2 + (x * y) % 3) % 2 == 0
    return ((x + y) % 2 + (x * y) % 3) % 2 == 0


def _format_bits(mask):
    data = mask  # EC level M indicator bits are 00 -> data = 0b00<<<3 | mask
    rem = data
    for _ in range(10):
        rem = (rem << 1) ^ ((rem >> 9) * 0x537)
    return ((data << 10) | rem) ^ 0x5412


def _version_bits(version):
    # 18-bit version information: the 6 version bits followed by the
    # 12-bit BCH remainder of (version * x^12) divided by the generator
    # polynomial 0x1F25 (x^12+x^11+x^10+x^9+x^8+x^5+x^2+1)
    rem = version << 12
    for i in range(17, 11, -1):
        if (rem >> i) & 1:
            rem ^= 0x1F25 << (i - 12)
    return (version << 12) | rem


def _draw_info(m, size, version, mask):
    fbits = _format_bits(mask)
    # first copy: around the top-left finder
    pos1 = [(8, 0), (8, 1), (8, 2), (8, 3), (8, 4), (8, 5), (8, 7), (8, 8),
            (7, 8), (5, 8), (4, 8), (3, 8), (2, 8), (1, 8), (0, 8)]
    for i, (x, y) in enumerate(pos1):
        m[y][x] = bool((fbits >> i) & 1)
    # second copy: split along the other finders
    for i in range(8):
        m[8][size - 1 - i] = bool((fbits >> i) & 1)
    for i in range(8, 15):
        m[size - 15 + i][8] = bool((fbits >> i) & 1)
    if version >= 7:
        vbits = _version_bits(version)
        for i in range(18):
            bit = bool((vbits >> i) & 1)
            m[i // 3][size - 11 + i % 3] = bit
            m[size - 11 + i % 3][i // 3] = bit


def _penalty(m, size):
    score = 0
    lines = list(m) + [[m[y][x] for y in range(size)] for x in range(size)]
    # rule 1: long runs
    for line in lines:
        run = 1
        for i in range(1, size):
            if line[i] == line[i - 1]:
                run += 1
            else:
                if run >= 5:
                    score += run - 2
                run = 1
        if run >= 5:
            score += run - 2
    # rule 2: 2x2 blocks
    for y in range(size - 1):
        for x in range(size - 1):
            if m[y][x] == m[y][x + 1] == m[y + 1][x] == m[y + 1][x + 1]:
                score += 3
    # rule 3: finder-like patterns with a 4-module quiet side
    pat = [True, False, True, True, True, False, True]
    for line in lines:
        for i in range(size - 6):
            if list(line[i:i + 7]) == pat:
                before = i >= 4 and not any(line[i - 4:i])
                after = i + 11 <= size and not any(line[i + 7:i + 11])
                if before or after:
                    score += 40
    # rule 4: dark ratio
    dark = sum(sum(1 for c in row if c) for row in m)
    total = size * size
    k = (abs(dark * 20 - total * 10) + total - 1) // total - 1
    score += max(0, k) * 10
    return score


def matrix(text):
    """Encode text -> QR matrix (list of rows of bool). Byte mode, level M."""
    payload = text.encode("utf-8")
    version = None
    for v in range(1, 21):
        total, ec_per, nblocks = _SPECS[v]
        data_total = total - ec_per * nblocks
        need = 4 + (8 if v <= 9 else 16) + 8 * len(payload)
        if need <= data_total * 8:
            version = v
            break
    if version is None:
        raise ValueError("payload too long (max ~660 bytes)")
    codewords = _interleave(payload, version)
    best, best_score = None, None
    for mask in range(8):
        m, fn, size = _blank(version)
        _place(m, fn, size, codewords)
        for y in range(size):
            for x in range(size):
                if not fn[y][x] and _mask_bit(mask, x, y):
                    m[y][x] = not m[y][x]
        _draw_info(m, size, version, mask)
        s = _penalty(m, size)
        if best_score is None or s < best_score:
            best, best_score = m, s
    return best


def svg(text, module=4, border=2, dark="#111827", light="#ffffff"):
    """Render the QR code for text as a standalone inline SVG string."""
    m = matrix(text)
    size = len(m)
    dim = (size + border * 2) * module
    parts = ["<svg xmlns='http://www.w3.org/2000/svg' width='%d' height='%d' "
             "viewBox='0 0 %d %d'><rect width='100%%' height='100%%' fill='%s'/><path fill='%s' d='" % (
                 dim, dim, dim, dim, light, dark)]
    for y in range(size):
        for x in range(size):
            if m[y][x]:
                parts.append("M%d %dh%dv%dh%dz" % ((x + border) * module, (y + border) * module,
                                                  module, module, -module))
    parts.append("'/></svg>")
    return "".join(parts)


if __name__ == "__main__":
    data = sys.argv[1] if len(sys.argv) > 1 else "https://example.com"
    print(svg(data))
