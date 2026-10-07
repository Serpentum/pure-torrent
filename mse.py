"""MSE — Message Stream Encryption (обфускация/шифрование хендшейка).
Задача — спрятать «0x13 BitTorrent protocol» из первого пакета от DPI.
После согласования выбираем plain-режим (быстро) либо RC4, если пир требует."""
from __future__ import annotations

import asyncio
import hashlib
import os

# 768-битное простое число из спецификации MSE (не RFC-группа!)
P = int(
    "FFFFFFFFFFFFFFFFC90FDAA22168C234C4C6628B80DC1CD1"
    "29024E088A67CC74020BBEA63B139B22514A08798E3404DD"
    "EF9519B3CD3A431B302B0A6DF25F14374FE1356D6D51C245"
    "E485B576625E7EC6F44C42E9A63A36210000000000090563", 16)
G = 2
CRYPTO_PLAIN = 1
CRYPTO_RC4 = 2


class MSEError(Exception):
    pass


def _sha1(*parts: bytes) -> bytes:
    h = hashlib.sha1()
    for p in parts:
        h.update(p)
    return h.digest()


def _xor(a: bytes, b: bytes) -> bytes:
    return bytes(x ^ y for x, y in zip(a, b))


class RC4:
    """Классический RC4 с пропуском первых 1024 байт keystream (по спецификации)."""

    __slots__ = ("s", "i", "j")

    def __init__(self, key: bytes):
        s = list(range(256))
        j = 0
        for i in range(256):
            j = (j + s[i] + key[i % len(key)]) & 0xFF
            s[i], s[j] = s[j], s[i]
        self.s = s
        self.i = self.j = 0
        self._skip(1024)

    def _skip(self, n: int):
        s, i, j = self.s, self.i, self.j
        for _ in range(n):
            i = (i + 1) & 0xFF
            j = (j + s[i]) & 0xFF
            s[i], s[j] = s[j], s[i]
        self.i, self.j = i, j

    def crypt(self, data: bytes) -> bytes:
        s, i, j = self.s, self.i, self.j
        out = bytearray(len(data))
        for k in range(len(data)):
            i = (i + 1) & 0xFF
            j = (j + s[i]) & 0xFF
            s[i], s[j] = s[j], s[i]
            out[k] = data[k] ^ s[(s[i] + s[j]) & 0xFF]
        self.i, self.j = i, j
        return bytes(out)


def _dh_pair():
    x = (int.from_bytes(os.urandom(96), "big") % (P - 2)) + 1
    return x, pow(G, x, P).to_bytes(96, "big")


def _secret(x: int, other_y: bytes) -> bytes:
    return pow(int.from_bytes(other_y, "big"), x, P).to_bytes(96, "big")


class _RawBuf:
    """Чтение из StreamReader с буфером сырых (не расшифрованных) байт."""

    def __init__(self, reader):
        self.reader = reader
        self.buf = b""

    async def need(self, n: int, timeout: float) -> bytes:
        while len(self.buf) < n:
            chunk = await asyncio.wait_for(self.reader.read(4096), timeout)
            if not chunk:
                raise MSEError("соединение закрыто")
            self.buf += chunk
        out, self.buf = self.buf[:n], self.buf[n:]
        return out

    def take_raw(self) -> bytes:
        out, self.buf = self.buf, b""
        return out


def _keys(s: bytes, info_hash: bytes):
    key_a = _sha1(b"keyA", s, info_hash)[:20]
    key_b = _sha1(b"keyB", s, info_hash)[:20]
    vc = b"\x00" * 8       # верификационная константа — 8 нулевых байт
    return key_a, key_b, vc


async def initiate(reader, writer, info_hash: bytes, ia: bytes, timeout: float = 20):
    """Инициатор MSE. ia — наш plaintext BT-хендшейк (68 байт).
    → dict(mode='plain'|'rc4', pending=байты после шага D, rc4=(enc_a, dec_b) или None)."""
    x, ya = _dh_pair()
    writer.write(ya + os.urandom(80))
    await writer.drain()

    rb = _RawBuf(reader)
    yb = await rb.need(96, timeout)
    rb.take_raw()                              # лишнее — PadB
    s = _secret(x, yb)
    key_a, key_b, vc = _keys(s, info_hash)

    enc_a = RC4(key_a)
    pad_c = os.urandom(16)
    body = (vc + (CRYPTO_PLAIN | CRYPTO_RC4).to_bytes(4, "big")
            + len(pad_c).to_bytes(2, "big") + pad_c
            + len(ia).to_bytes(2, "big") + ia)
    writer.write(_sha1(b"req1", s) + _xor(_sha1(b"req2", info_hash), _sha1(b"req3", s))
                 + enc_a.crypt(body))
    await writer.drain()

    enc_b = RC4(key_b)
    head = enc_b.crypt(await rb.need(14, timeout))
    if head[:8] != vc:
        raise MSEError("не совпал VC (шаг D)")
    select = int.from_bytes(head[8:12], "big")
    pad_d_len = int.from_bytes(head[12:14], "big")
    if pad_d_len:
        enc_b.crypt(await rb.need(pad_d_len, timeout))    # пропускаем PadD
    if select == CRYPTO_PLAIN:
        return {"mode": "plain", "pending": rb.take_raw(), "rc4": None}
    if select == CRYPTO_RC4:
        return {"mode": "rc4", "pending": rb.take_raw(), "rc4": (enc_a, enc_b)}
    raise MSEError(f"неизвестный crypto_select {select}")


async def respond(reader, writer, info_hashes: list, initial: bytes,
                  timeout: float = 30):
    """Ответчик MSE (входящее подключение). initial — первый прочитанный байт (!= 0x13).
    Перебирает info_hashes для расшифровки скрытого хеша.
    → dict(info_hash, mode, remote_ia, pending, rc4). Наш хендшейк шлёт вызывающий."""
    x, yb = _dh_pair()
    writer.write(yb + os.urandom(80))
    await writer.drain()

    rb = _RawBuf(reader)
    ya = (initial + await rb.need(95, timeout))[:96]
    rb.take_raw()                              # лишнее — PadA
    s = _secret(x, ya)

    marker = await rb.need(40, timeout)
    req1 = _sha1(b"req1", s)
    if marker[:20] != req1:
        raise MSEError("не совпал req1")
    hidden = marker[20:]
    # ищем торент, под который зашифрован info_hash
    req3 = _sha1(b"req3", s)
    info_hash = None
    for ih in info_hashes:
        if _xor(_sha1(b"req2", ih), req3) == hidden:
            info_hash = ih
            break
    if info_hash is None:
        raise MSEError("info_hash не найден среди наших торрентов")

    key_a, key_b, vc = _keys(s, info_hash)
    dec_a = RC4(key_a)
    head = dec_a.crypt(await rb.need(14, timeout))
    if head[:8] != vc:
        raise MSEError("не совпал VC (шаг C)")
    provide = int.from_bytes(head[8:12], "big")
    pad_c_len = int.from_bytes(head[12:14], "big")
    if pad_c_len:
        dec_a.crypt(await rb.need(pad_c_len, timeout))
    ia_len = int.from_bytes(dec_a.crypt(await rb.need(2, timeout)), "big")
    if not (0 < ia_len <= 4096):
        raise MSEError("странная длина IA")
    ia = dec_a.crypt(await rb.need(ia_len, timeout))

    select = CRYPTO_PLAIN if (provide & CRYPTO_PLAIN) else CRYPTO_RC4
    enc_b = RC4(key_b)
    pad_d = os.urandom(8)
    step_d = enc_b.crypt(vc + select.to_bytes(4, "big")
                         + len(pad_d).to_bytes(2, "big") + pad_d)
    writer.write(step_d)
    await writer.drain()
    return {"info_hash": info_hash, "mode": "plain" if select == CRYPTO_PLAIN else "rc4",
            "remote_ia": ia, "pending": rb.take_raw(),
            "rc4": None if select == CRYPTO_PLAIN else (dec_a, enc_b)}
