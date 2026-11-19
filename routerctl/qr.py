"""A small QR-code maker (byte mode, error correction M, versions 1-10).

Enough for a web address like http://192.168.1.4:8770/#pin=123456, with no
library to install. Follows the QR standard (ISO/IEC 18004); the structure
mirrors Project Nayuki's public-domain reference design.
"""

import os
import sys

# version -> (EC codewords per block, [(block count, data codewords per block), ...]) for level M
_M = {
    1: (10, [(1, 16)]), 2: (16, [(1, 28)]), 3: (26, [(1, 44)]), 4: (18, [(2, 32)]), 5: (24, [(2, 43)]),
    6: (16, [(4, 27)]), 7: (18, [(4, 31)]), 8: (22, [(2, 38), (2, 39)]), 9: (22, [(3, 36), (2, 37)]),
    10: (26, [(4, 43), (1, 44)]),
}
_ALIGN = {1: [], 2: [6, 18], 3: [6, 22], 4: [6, 26], 5: [6, 30], 6: [6, 34], 7: [6, 22, 38], 8: [6, 24, 42],
          9: [6, 26, 46], 10: [6, 28, 50]}


def _bit(x, i):
    return (x >> i) & 1 != 0


def _gf_mul(x, y):
    z = 0
    for i in reversed(range(8)):
        z = (z << 1) ^ ((z >> 7) * 0x11D)
        z ^= ((y >> i) & 1) * x
    return z


def _rs_divisor(degree):
    result = [0] * (degree - 1) + [1]
    root = 1
    for _ in range(degree):
        for j in range(degree):
            result[j] = _gf_mul(result[j], root)
            if j + 1 < degree:
                result[j] ^= result[j + 1]
        root = _gf_mul(root, 0x02)
    return result


def _rs_remainder(data, divisor):
    result = [0] * len(divisor)
    for b in data:
        factor = b ^ result.pop(0)
        result.append(0)
        for i, coef in enumerate(divisor):
            result[i] ^= _gf_mul(coef, factor)
    return result


class QR:
    def __init__(self, text):
        data = text.encode("utf-8")
        for version in range(1, 11):
            ec, groups = _M[version]
            cap = sum(n * k for n, k in groups)
            count_bits = 8 if version < 10 else 16
            if 4 + count_bits + 8 * len(data) <= cap * 8:
                break
        else:
            raise ValueError("text too long for this QR maker")
        self.version, self.size = version, version * 4 + 17
        self.modules = [[False] * self.size for _ in range(self.size)]
        self.function = [[False] * self.size for _ in range(self.size)]
        self._draw_function_patterns()
        self._draw_codewords(self._codewords(data, cap, count_bits, ec, groups))
        best, best_score = 0, None
        for mask in range(8):
            self._apply_mask(mask)
            self._draw_format(mask)
            score = self._penalty()
            if best_score is None or score < best_score:
                best, best_score = mask, score
            self._apply_mask(mask)  # undo
        self.mask = best
        self._apply_mask(best)
        self._draw_format(best)

    # --- building blocks
    def _set(self, x, y, dark):
        self.modules[y][x] = dark
        self.function[y][x] = True

    def _draw_function_patterns(self):
        n = self.size
        for i in range(n):
            self._set(6, i, i % 2 == 0)
            self._set(i, 6, i % 2 == 0)
        for cx, cy in ((3, 3), (n - 4, 3), (3, n - 4)):
            for dy in range(-4, 5):
                for dx in range(-4, 5):
                    x, y = cx + dx, cy + dy
                    if 0 <= x < n and 0 <= y < n:
                        self._set(x, y, max(abs(dx), abs(dy)) not in (2, 4))
        pos = _ALIGN[self.version]
        last = len(pos) - 1
        for i, a in enumerate(pos):
            for j, b in enumerate(pos):
                if (i, j) in ((0, 0), (0, last), (last, 0)):
                    continue
                for dy in range(-2, 3):
                    for dx in range(-2, 3):
                        self._set(a + dx, b + dy, max(abs(dx), abs(dy)) != 1)
        self._draw_format(0)
        if self.version >= 7:
            rem = self.version
            for _ in range(12):
                rem = (rem << 1) ^ ((rem >> 11) * 0x1F25)
            bits = self.version << 12 | rem
            for i in range(18):
                a, b = n - 11 + i % 3, i // 3
                self._set(a, b, _bit(bits, i))
                self._set(b, a, _bit(bits, i))

    def _draw_format(self, mask):
        data = 0 << 3 | mask          # level M = 0
        rem = data
        for _ in range(10):
            rem = (rem << 1) ^ ((rem >> 9) * 0x537)
        bits = (data << 10 | rem) ^ 0x5412
        n = self.size
        for i in range(6):
            self._set(8, i, _bit(bits, i))
        self._set(8, 7, _bit(bits, 6))
        self._set(8, 8, _bit(bits, 7))
        self._set(7, 8, _bit(bits, 8))
        for i in range(9, 15):
            self._set(14 - i, 8, _bit(bits, i))
        for i in range(8):
            self._set(n - 1 - i, 8, _bit(bits, i))
        for i in range(8, 15):
            self._set(8, n - 15 + i, _bit(bits, i))
        self._set(8, n - 8, True)

    @staticmethod
    def _codewords(data, cap, count_bits, ec, groups):
        bits = []

        def put(value, length):
            bits.extend((value >> i) & 1 for i in reversed(range(length)))
        put(0b0100, 4)
        put(len(data), count_bits)
        for b in data:
            put(b, 8)
        put(0, min(4, cap * 8 - len(bits)))
        put(0, (-len(bits)) % 8)
        out = [int("".join(map(str, bits[i:i + 8])), 2) for i in range(0, len(bits), 8)]
        pad = 0xEC
        while len(out) < cap:
            out.append(pad)
            pad ^= 0xEC ^ 0x11
        blocks, k = [], 0
        div = _rs_divisor(ec)
        for count, size in groups:
            for _ in range(count):
                blk = out[k:k + size]
                k += size
                blocks.append((blk, _rs_remainder(blk, div)))
        result = []
        for i in range(max(len(b) for b, _ in blocks)):
            result.extend(b[i] for b, _ in blocks if i < len(b))
        for i in range(ec):
            result.extend(e[i] for _, e in blocks)
        return result

    def _draw_codewords(self, data):
        n, i, total = self.size, 0, len(data) * 8
        right = n - 1
        while right >= 1:
            if right == 6:
                right = 5
            for vert in range(n):
                for j in range(2):
                    x = right - j
                    upward = ((right + 1) & 2) == 0
                    y = n - 1 - vert if upward else vert
                    if not self.function[y][x] and i < total:
                        self.modules[y][x] = _bit(data[i >> 3], 7 - (i & 7))
                        i += 1
            right -= 2

    def _apply_mask(self, mask):
        f = [lambda x, y: (x + y) % 2 == 0, lambda x, y: y % 2 == 0, lambda x, y: x % 3 == 0,
             lambda x, y: (x + y) % 3 == 0, lambda x, y: (x // 3 + y // 2) % 2 == 0,
             lambda x, y: x * y % 2 + x * y % 3 == 0, lambda x, y: (x * y % 2 + x * y % 3) % 2 == 0,
             lambda x, y: ((x + y) % 2 + x * y % 3) % 2 == 0][mask]
        for y in range(self.size):
            for x in range(self.size):
                if not self.function[y][x] and f(x, y):
                    self.modules[y][x] = not self.modules[y][x]

    def _penalty(self):
        m, n, score = self.modules, self.size, 0
        for lines in (m, [list(c) for c in zip(*m)]):
            for row in lines:
                run, prev = 0, None
                for v in row:
                    run = run + 1 if v == prev else 1
                    prev = v
                    if run == 5:
                        score += 3
                    elif run > 5:
                        score += 1
                s = "".join("1" if v else "0" for v in row)
                score += 40 * (s.count("10111010000") + s.count("00001011101"))
        for y in range(n - 1):
            for x in range(n - 1):
                if m[y][x] == m[y][x + 1] == m[y + 1][x] == m[y + 1][x + 1]:
                    score += 3
        dark = sum(sum(r) for r in m)
        score += (abs(dark * 20 - n * n * 10) + n * n - 1) // (n * n) * 10 - 10
        return score

    # --- output
    def svg(self, border=3):
        n = self.size + border * 2
        path = "".join("M%d,%dh1v1h-1z" % (x + border, y + border)
                       for y in range(self.size) for x in range(self.size) if self.modules[y][x])
        return ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 %d %d" shape-rendering="crispEdges">'
                '<rect width="100%%" height="100%%" fill="#fff"/><path d="%s" fill="#000"/></svg>' % (n, n, path))

    def terminal(self, border=2):
        """Black-on-white QR made of half blocks, for the console window."""
        if os.name == "nt":
            os.system("")   # switch on colour codes in the Windows console
        n = self.size
        get = lambda x, y: 0 <= x < n and 0 <= y < n and self.modules[y][x]
        lines = []
        for y in range(-border, n + border, 2):
            row = ""
            for x in range(-border, n + border):
                top, bot = get(x, y), get(x, y + 1)
                row += "\033[%d;%dm▀" % (30 if top else 97, 40 if bot else 107)
            lines.append(row + "\033[0m")
        return "\n".join(lines)


def print_terminal(text):
    try:
        print(QR(text).terminal())
    except (UnicodeEncodeError, ValueError, OSError):
        pass
    sys.stdout.flush()
