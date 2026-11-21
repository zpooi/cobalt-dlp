"""抖音 a_bogus 签名（纯 Python，无第三方依赖）。

抖音 Web 接口除了自定义的 ``x-secsdk-web-signature``，还要在 query 里带一个
``a_bogus``。它由 query 串、User-Agent、时间戳和浏览器指纹经过 SM3 / RC4 和
变体 Base64 得到。这里按公开的逆向结果用标准库实现，供图文（/note/）的
slidesinfo、iteminfo 和 detail 兜底请求使用。

SM3 优先用 OpenSSL（``hashlib.new("sm3")``）；系统不带 SM3 时退回纯 Python
实现，所以不需要 gmssl 之类的额外依赖。

算法来自社区逆向（a_bogus），非官方；抖音改算法时这里可能需要同步更新。
"""

import hashlib

_MASK = 0xFFFFFFFF
_ALPHABETS = {
    "s0": "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/=",
    "s1": "Dkdpgh4ZKsQB80/Mfvw36XI1R25+WUAlEi7NLboqYTOPuzmFjJnryx9HVGcaStCe=",
    "s2": "Dkdpgh4ZKsQB80/Mfvw36XI1R25-WUAlEi7NLboqYTOPuzmFjJnryx9HVGcaStCe=",
    "s3": "ckdp1h4ZKsUB80/Mfvw36XIgR25+WQAlEi7NLboqYTOPuzmFjJnryx9HVGDaStCe",
    "s4": "Dkdpgh2ZmsQB80/MfvV36XI1R45-WUAlEixNLwoqYTOPuzKFjJnry79HbGcaStCe",
}
_END_STRING = "cus"


# ---------------------------------------------------------------- SM3
_SM3_IV = (0x7380166F, 0x4914B2B9, 0x172442D7, 0xDA8A0600,
           0xA96F30BC, 0x163138AA, 0xE38DEE4D, 0xB0FB0E4E)


def _rotl(x, n):
    n &= 31
    return ((x << n) | (x >> (32 - n))) & _MASK


def _p0(x):
    return x ^ _rotl(x, 9) ^ _rotl(x, 17)


def _p1(x):
    return x ^ _rotl(x, 15) ^ _rotl(x, 23)


def _ff(x, y, z, j):
    if j < 16:
        return x ^ y ^ z
    return (x & y) | (x & z) | (y & z)


def _gg(x, y, z, j):
    if j < 16:
        return x ^ y ^ z
    return (x & y) | ((~x & _MASK) & z)


def _sm3_pure(data):
    msg = bytearray(data) + b"\x80"
    bit_len = len(data) * 8
    while len(msg) % 64 != 56:
        msg.append(0)
    msg += bit_len.to_bytes(8, "big")
    v = list(_SM3_IV)
    for off in range(0, len(msg), 64):
        w = [int.from_bytes(msg[off + i * 4:off + i * 4 + 4], "big")
             for i in range(16)]
        for j in range(16, 68):
            w.append((_p1(w[j - 16] ^ w[j - 9] ^ _rotl(w[j - 3], 15))
                      ^ _rotl(w[j - 13], 7) ^ w[j - 6]) & _MASK)
        w1 = [w[j] ^ w[j + 4] for j in range(64)]
        a, b, c, d, e, f, g, h = v
        for j in range(64):
            t = 0x79CC4519 if j < 16 else 0x7A879D8A
            ss1 = _rotl((_rotl(a, 12) + e + _rotl(t, j)) & _MASK, 7)
            ss2 = ss1 ^ _rotl(a, 12)
            tt1 = (_ff(a, b, c, j) + d + ss2 + w1[j]) & _MASK
            tt2 = (_gg(e, f, g, j) + h + ss1 + w[j]) & _MASK
            d, c, b, a = c, _rotl(b, 9), a, tt1
            h, g, f, e = g, _rotl(f, 19), e, _p0(tt2)
        v = [x ^ y for x, y in zip(v, [a, b, c, d, e, f, g, h])]
    return b"".join(x.to_bytes(4, "big") for x in v)


def _sm3(data):
    try:
        return hashlib.new("sm3", data).digest()
    except (ValueError, TypeError):  # OpenSSL 没编译 SM3
        return _sm3_pure(data)


# ---------------------------------------------------------------- a_bogus
class ABogus:
    """按抖音 Web 端的请求参数生成 ``a_bogus``。

    用法::

        params = {"aweme_id": "..."}          # 不含 a_bogus，顺序与最终请求一致
        params["a_bogus"] = ABogus(user_agent).generate_a_bogus(params)
    """

    def __init__(self, user_agent="", platform=None):
        self.user_agent = user_agent
        self.ua_code = self._ua_code(user_agent)
        self.browser = self._browser_info(platform)
        self.browser_len = len(self.browser)
        self.browser_code = [ord(ch) for ch in self.browser]

    # -- 小工具 --------------------------------------------------------
    @staticmethod
    def _from_codes(codes):
        return "".join(chr(c) for c in codes)

    @staticmethod
    def _rc4(plaintext, key):
        s = list(range(256))
        j = 0
        for i in range(256):
            j = (j + s[i] + ord(key[i % len(key)])) % 256
            s[i], s[j] = s[j], s[i]
        i = j = 0
        out = []
        for ch in plaintext:
            i = (i + 1) % 256
            j = (j + s[i]) % 256
            s[i], s[j] = s[j], s[i]
            out.append(chr(s[(s[i] + s[j]) % 256] ^ ord(ch)))
        return "".join(out)

    @staticmethod
    def _sm3_to_array(data):
        if isinstance(data, str):
            data = data.encode("utf-8")
        digest = _sm3(bytes(data))
        return list(digest)

    @staticmethod
    def _encode(data, alphabet="s4"):
        """变体 Base64（抖音自己的字母表）。"""
        table = _ALPHABETS[alphabet]
        out = []
        for i in range(0, len(data), 3):
            chunk = data[i:i + 3]
            n = 0
            for k, ch in enumerate(chunk):
                n |= ord(ch) << (16 - 8 * k)
            for shift, mask in zip((18, 12, 6, 0),
                                   (0xFC0000, 0x03F000, 0x0FC0, 0x3F)):
                if shift == 6 and len(chunk) < 2:
                    break
                if shift == 0 and len(chunk) < 3:
                    break
                out.append(table[(n & mask) >> shift])
        out.append("=" * ((4 - len(out) % 4) % 4))
        return "".join(out)

    # -- 随机字符串 ----------------------------------------------------
    @staticmethod
    def _random_list(b=170, c=85, d=0, e=0, f=0, g=0):
        import random
        r = random.random() * 10000
        v = [int(r) & 255, int(r) >> 8]
        return [v[0] & b | d, v[0] & c | e,
                v[1] & b | f, v[1] & c | g]

    def _string_1(self):
        return (self._from_codes(self._random_list(170, 85, 1, 2, 5, 45 & 170))
                + self._from_codes(self._random_list(170, 85, 1, 0, 0, 0))
                + self._from_codes(self._random_list(170, 85, 1, 0, 5, 0)))

    # -- 时间和参数 ----------------------------------------------------
    @staticmethod
    def _method_code(method="GET"):
        return ABogus._sm3_to_array(ABogus._sm3_to_array(method + _END_STRING))

    @staticmethod
    def _params_code(params):
        return ABogus._sm3_to_array(
            ABogus._sm3_to_array(params + _END_STRING))

    def _list_4(self, a, b, c, d, e, f, g, h, i, j, k, m, n, o, p, q, r):
        return [
            44, a, 0, 0, 0, 0, 24, b, n, 0, c, d, 0, 0, 0, 1, 0, 239,
            e, o, f, g, 0, 0, 0, 0, h, 0, 0, 14, i, j, 0, k, m, 3, p, 1,
            q, 1, r, 0, 0, 0,
        ]

    def _string_2_list(self, url_params, method="GET"):
        import random
        import time
        start = int(time.time() * 1000)
        end = start + random.randint(4, 8)
        params = self._params_code(url_params)
        mcode = self._method_code(method)
        return self._list_4(
            (end >> 24) & 255, params[21], self.ua_code[23],
            (end >> 16) & 255, params[22], self.ua_code[24],
            (end >> 8) & 255, end & 255,
            (start >> 24) & 255, (start >> 16) & 255,
            (start >> 8) & 255, start & 255,
            mcode[21], mcode[22],
            end >> 32, start >> 32,
            self.browser_len,
        )

    def _string_2(self, url_params, method="GET"):
        codes = self._string_2_list(url_params, method)
        checksum = 0
        for value in codes:
            checksum ^= value
        codes.extend(self.browser_code)
        codes.append(checksum)
        return self._rc4(self._from_codes(codes), "y")

    def _ua_code(self, user_agent):
        key = "".join(chr(int(n)) for n in (0.00390625, 1, 14))
        return self._sm3_to_array(self._encode(self._rc4(user_agent, key), "s3"))

    @staticmethod
    def _browser_info(platform=None):
        import random
        inner_w = random.randint(1280, 1920)
        inner_h = random.randint(720, 1080)
        outer_w = random.randint(inner_w, 1920)
        outer_h = random.randint(inner_h, 1080)
        values = [inner_w, inner_h, outer_w, outer_h, 0,
                  random.choice((0, 30)), 0, 0, outer_w, outer_h,
                  outer_w, outer_h, inner_w, inner_h, 24, 24, platform]
        return "|".join(str(v) for v in values)

    # -- 对外接口 ------------------------------------------------------
    def generate_a_bogus(self, url_params):
        """``url_params`` 是 dict 或已编码的 query 串（不含 a_bogus）。"""
        import random
        from urllib.parse import urlencode
        query = url_params if isinstance(url_params, str) else urlencode(url_params)
        raw = self._string_1() + self._string_2(query)
        # 每 3 个字节编码，字母表 s4
        return self._encode(raw, "s4")


def generate_a_bogus(url_params, user_agent=""):
    return ABogus(user_agent).generate_a_bogus(url_params)
