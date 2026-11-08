"""按 FIPS-197 写的纯 Python AES，只在没有 PyCryptodome 时兜底。

它很慢（每秒约几百 KB），只适合刚解压、还没装依赖时跑离线 demo。真实数据库请装
requirements.txt 里的 PyCryptodome；doctor 会提醒。
"""
from __future__ import annotations


def _build_tables() -> tuple[bytes, bytes]:
    sbox = [0] * 256
    p = q = 1
    while True:
        # p 乘 3，q 除以 3，在 GF(2^8) 上遍历所有非零元素
        p = p ^ ((p << 1) & 0xFF) ^ (0x1B if p & 0x80 else 0)
        q ^= (q << 1) & 0xFF
        q ^= (q << 2) & 0xFF
        q ^= (q << 4) & 0xFF
        if q & 0x80:
            q ^= 0x09
        x = q ^ ((q << 1 | q >> 7) & 0xFF) ^ ((q << 2 | q >> 6) & 0xFF)
        x ^= ((q << 3 | q >> 5) & 0xFF) ^ ((q << 4 | q >> 4) & 0xFF)
        sbox[p] = x ^ 0x63
        if p == 1:
            break
    sbox[0] = 0x63
    inv = [0] * 256
    for index, value in enumerate(sbox):
        inv[value] = index
    return bytes(sbox), bytes(inv)


_SBOX, _INV = _build_tables()


def _xtime(value: int) -> int:
    return ((value << 1) & 0xFF) ^ (0x1B if value & 0x80 else 0)


def _mul(a: int, b: int) -> int:
    result = 0
    while b:
        if b & 1:
            result ^= a
        a = _xtime(a)
        b >>= 1
    return result


_M9 = bytes(_mul(i, 9) for i in range(256))
_M11 = bytes(_mul(i, 11) for i in range(256))
_M13 = bytes(_mul(i, 13) for i in range(256))
_M14 = bytes(_mul(i, 14) for i in range(256))
_M2 = bytes(_mul(i, 2) for i in range(256))
_M3 = bytes(_mul(i, 3) for i in range(256))


def _expand_key(key: bytes) -> list[bytes]:
    if len(key) not in (16, 24, 32):
        raise ValueError("AES key must be 16, 24 or 32 bytes")
    nk = len(key) // 4
    rounds = nk + 6
    words = [list(key[i:i + 4]) for i in range(0, len(key), 4)]
    rcon = 1
    for index in range(nk, 4 * (rounds + 1)):
        word = list(words[index - 1])
        if index % nk == 0:
            word = word[1:] + word[:1]
            word = [_SBOX[b] for b in word]
            word[0] ^= rcon
            rcon = _xtime(rcon)
        elif nk > 6 and index % nk == 4:
            word = [_SBOX[b] for b in word]
        words.append([a ^ b for a, b in zip(words[index - nk], word)])
    flat = [b for word in words for b in word]
    return [bytes(flat[i:i + 16]) for i in range(0, len(flat), 16)]


def _encrypt_block(block: bytes, keys: list[bytes]) -> bytes:
    state = [a ^ b for a, b in zip(block, keys[0])]
    last = len(keys) - 1
    for round_index in range(1, last + 1):
        state = [_SBOX[b] for b in state]
        state = [state[(i + 4 * (i % 4)) % 16] for i in range(16)]
        if round_index != last:
            mixed = []
            for col in range(0, 16, 4):
                a, b, c, d = state[col:col + 4]
                mixed += [_M2[a] ^ _M3[b] ^ c ^ d, a ^ _M2[b] ^ _M3[c] ^ d,
                          a ^ b ^ _M2[c] ^ _M3[d], _M3[a] ^ b ^ c ^ _M2[d]]
            state = mixed
        state = [a ^ b for a, b in zip(state, keys[round_index])]
    return bytes(state)


def _decrypt_block(block: bytes, keys: list[bytes]) -> bytes:
    last = len(keys) - 1
    state = [a ^ b for a, b in zip(block, keys[last])]
    for round_index in range(last - 1, -1, -1):
        state = [state[(i - 4 * (i % 4)) % 16] for i in range(16)]
        state = [_INV[b] for b in state]
        state = [a ^ b for a, b in zip(state, keys[round_index])]
        if round_index:
            mixed = []
            for col in range(0, 16, 4):
                a, b, c, d = state[col:col + 4]
                mixed += [_M14[a] ^ _M11[b] ^ _M13[c] ^ _M9[d], _M9[a] ^ _M14[b] ^ _M11[c] ^ _M13[d],
                          _M13[a] ^ _M9[b] ^ _M14[c] ^ _M11[d], _M11[a] ^ _M13[b] ^ _M9[c] ^ _M14[d]]
            state = mixed
    return bytes(state)


def cbc_decrypt(key: bytes, iv: bytes, data: bytes) -> bytes:
    if len(data) % 16 or len(iv) != 16:
        raise ValueError("AES-CBC needs whole blocks and a 16-byte IV")
    keys = _expand_key(key)
    previous = iv
    out = bytearray()
    for offset in range(0, len(data), 16):
        block = data[offset:offset + 16]
        out += bytes(a ^ b for a, b in zip(_decrypt_block(block, keys), previous))
        previous = block
    return bytes(out)


def cbc_encrypt(key: bytes, iv: bytes, data: bytes) -> bytes:
    if len(data) % 16 or len(iv) != 16:
        raise ValueError("AES-CBC needs whole blocks and a 16-byte IV")
    keys = _expand_key(key)
    previous = iv
    out = bytearray()
    for offset in range(0, len(data), 16):
        previous = _encrypt_block(bytes(a ^ b for a, b in zip(data[offset:offset + 16], previous)), keys)
        out += previous
    return bytes(out)


def ecb_decrypt(key: bytes, data: bytes) -> bytes:
    if len(data) % 16:
        raise ValueError("AES-ECB needs whole blocks")
    keys = _expand_key(key)
    return b"".join(_decrypt_block(data[i:i + 16], keys) for i in range(0, len(data), 16))


def ecb_encrypt(key: bytes, data: bytes) -> bytes:
    if len(data) % 16:
        raise ValueError("AES-ECB needs whole blocks")
    keys = _expand_key(key)
    return b"".join(_encrypt_block(data[i:i + 16], keys) for i in range(0, len(data), 16))


def cfb128_decrypt(key: bytes, iv: bytes, data: bytes) -> bytes:
    if len(iv) != 16:
        raise ValueError("AES-CFB needs a 16-byte IV")
    keys = _expand_key(key)
    out = bytearray()
    previous = iv
    for offset in range(0, len(data), 16):
        block = data[offset:offset + 16]
        stream = _encrypt_block(previous, keys)
        out += bytes(a ^ b for a, b in zip(block, stream))
        previous = block if len(block) == 16 else previous
    return bytes(out)
