"""Минимальный bencode-кодек (BEP 3). Строки всегда декодируются в bytes."""
from __future__ import annotations


class BcodeError(ValueError):
    pass


def encode(value) -> bytes:
    if isinstance(value, bool):
        raise BcodeError("bool не кодируется в bencode")
    if isinstance(value, int):
        return b"i%de" % value
    if isinstance(value, bytes):
        return b"%d:" % len(value) + value
    if isinstance(value, str):
        return encode(value.encode("utf-8"))
    if isinstance(value, (list, tuple)):
        return b"l" + b"".join(encode(v) for v in value) + b"e"
    if isinstance(value, dict):
        items = []
        for k, v in value.items():
            kb = k.encode("utf-8") if isinstance(k, str) else k
            if not isinstance(kb, bytes):
                raise BcodeError("ключ словаря должен быть bytes/str")
            items.append((kb, v))
        items.sort(key=lambda kv: kv[0])
        return b"d" + b"".join(encode(k) + encode(v) for k, v in items) + b"e"
    raise BcodeError(f"тип не поддерживается: {type(value)!r}")


def decode(data: bytes):
    """Декодирует ровно одно значение (лишний хвост — ошибка)."""
    value, end = _decode(data, 0)
    if end != len(data):
        raise BcodeError("лишние данные после значения")
    return value


def decode_prefix(data: bytes, pos: int = 0):
    """Декодирует значение с позиции pos; возвращает (значение, позиция конца)."""
    return _decode(data, pos)


def _decode(data: bytes, pos: int):
    limit = len(data)
    if pos >= limit:
        raise BcodeError("неожиданный конец данных")
    c = data[pos:pos + 1]
    if c == b"i":
        end = data.find(b"e", pos)
        if end < 0:
            raise BcodeError("целое без завершения")
        try:
            n = int(data[pos + 1:end])
        except ValueError:
            raise BcodeError("плохое целое")
        return n, end + 1
    if c == b"l":
        pos += 1
        out = []
        while True:
            if pos >= limit:
                raise BcodeError("список без 'e'")
            if data[pos:pos + 1] == b"e":
                return out, pos + 1
            v, pos = _decode(data, pos)
            out.append(v)
    if c == b"d":
        pos += 1
        out = {}
        while True:
            if pos >= limit:
                raise BcodeError("словарь без 'e'")
            if data[pos:pos + 1] == b"e":
                return out, pos + 1
            k, pos = _decode(data, pos)
            if not isinstance(k, bytes):
                raise BcodeError("ключ словаря не строка")
            v, pos = _decode(data, pos)
            out[k] = v
    if c.isdigit():
        colon = data.find(b":", pos, pos + 12)
        if colon < 0:
            raise BcodeError("длина строки без ':'")
        try:
            n = int(data[pos:colon])
        except ValueError:
            raise BcodeError("плохая длина строки")
        start = colon + 1
        if n < 0 or start + n > limit:
            raise BcodeError("строка выходит за границы данных")
        return data[start:start + n], start + n
    raise BcodeError(f"неожиданный символ на позиции {pos}")


def dict_value_span(data: bytes, key: bytes):
    """Для bencode-словаря верхнего уровня — (начало, конец) сырых байт значения."""
    if data[:1] != b"d":
        raise BcodeError("ожидался словарь верхнего уровня")
    pos = 1
    limit = len(data)
    while data[pos:pos + 1] != b"e":
        if pos >= limit:
            raise BcodeError("словарь без 'e'")
        k, pos = _decode(data, pos)
        if not isinstance(k, bytes):
            raise BcodeError("ключ словаря не строка")
        start = pos
        _, pos = _decode(data, pos)
        if k == key:
            return start, pos
    raise KeyError(key)
