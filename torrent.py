"""Разбор .torrent-файлов и magnet-ссылок, метаданные торрента."""
from __future__ import annotations

import base64
import hashlib
import urllib.parse
from dataclasses import dataclass

import bcode


class MetaError(Exception):
    pass


@dataclass
class FileEntry:
    path: str      # относительный путь с '/'
    length: int
    offset: int    # смещение внутри общего потока кусков


def _sanitize(path: str) -> str:
    parts = []
    for p in path.replace("\\", "/").split("/"):
        if p in ("", ".", ".."):
            continue
        parts.append(p)
    if not parts:
        raise MetaError("пустой путь файла в торренте")
    return "/".join(parts)


def _decode_name(info: dict, top: dict) -> str:
    raw = info.get(b"name.utf-8") or info.get(b"name") or b""
    if isinstance(raw, str):
        return raw
    enc = top.get(b"encoding")
    codecs = ["utf-8"]
    if isinstance(enc, bytes):
        codecs.append(enc.decode("ascii", "ignore"))
    codecs.append("cp1251")
    seen, ordered = set(), []
    for c in codecs:
        if c and c not in seen:
            seen.add(c)
            ordered.append(c)
    for codec in ordered:
        try:
            return raw.decode(codec)
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode("utf-8", "replace")


class TorrentMeta:
    """Метаданные v1-торрента (v2 без v1-части не поддерживается)."""

    def __init__(self, info_hash: bytes, name: str, piece_length: int,
                 pieces: list[bytes], files: list[FileEntry], trackers: list[str],
                 private: bool, info_raw: bytes):
        self.info_hash = info_hash
        self.name = name
        self.piece_length = piece_length
        self.pieces = pieces
        self.files = files
        self.trackers = trackers
        self.private = private
        self.info_raw = info_raw
        self.total_size = files[-1].offset + files[-1].length if files else 0
        self.num_pieces = len(pieces)

    @property
    def info_hash_hex(self) -> str:
        return self.info_hash.hex()

    def piece_size(self, index: int) -> int:
        if 0 <= index < self.num_pieces:
            return min(self.piece_length, self.total_size - index * self.piece_length)
        return 0

    @classmethod
    def from_bytes(cls, data: bytes) -> "TorrentMeta":
        try:
            top, _ = bcode.decode_prefix(data, 0)
        except bcode.BcodeError as e:
            raise MetaError(f"битый .torrent: {e}")
        if not isinstance(top, dict) or not isinstance(top.get(b"info"), dict):
            raise MetaError("в .torrent нет словаря info")
        try:
            start, end = bcode.dict_value_span(data, b"info")
        except (KeyError, bcode.BcodeError) as e:
            raise MetaError(f"не удалось найти info: {e}")
        info_raw = data[start:end]
        return cls._build(top, top[b"info"], hashlib.sha1(info_raw).digest(), info_raw)

    @classmethod
    def from_info_bytes(cls, info_raw: bytes) -> "TorrentMeta":
        """Метаданные, полученные по magnet (BEP 9)."""
        try:
            info, _ = bcode.decode_prefix(info_raw, 0)
        except bcode.BcodeError as e:
            raise MetaError(f"битые метаданные: {e}")
        if not isinstance(info, dict):
            raise MetaError("метаданные не словарь")
        return cls._build({}, info, hashlib.sha1(info_raw).digest(), info_raw)

    @classmethod
    def _build(cls, top: dict, info: dict, info_hash: bytes, info_raw: bytes) -> "TorrentMeta":
        piece_length = info.get(b"piece length")
        pieces_blob = info.get(b"pieces")
        if not isinstance(piece_length, int) or piece_length <= 0:
            raise MetaError("нет piece length")
        if not isinstance(pieces_blob, bytes) or len(pieces_blob) % 20 != 0 or not pieces_blob:
            raise MetaError("торрент v2 (без v1-части) или битые pieces — не поддерживается")
        if b"file tree" in info and b"files" not in info and b"length" not in info:
            raise MetaError("торрент v2 не поддерживается")
        pieces = [pieces_blob[i:i + 20] for i in range(0, len(pieces_blob), 20)]

        files: list[FileEntry] = []
        try:
            if b"files" in info:
                off = 0
                for f in info[b"files"]:
                    length = f[b"length"]
                    if not isinstance(length, int) or length < 0:
                        raise MetaError("плохая длина файла")
                    parts = [p.decode("utf-8", "replace") for p in f[b"path"] if p not in (b"", b".")]
                    files.append(FileEntry(_sanitize("/".join(parts)), length, off))
                    off += length
            else:
                length = info[b"length"]
                if not isinstance(length, int) or length < 0:
                    raise MetaError("плохая длина файла")
                files.append(FileEntry(_sanitize(_decode_name(info, top)), length, 0))
        except (KeyError, TypeError, MetaError):
            raise MetaError("не удалось разобрать список файлов")
        if not files:
            # нулевой объём — оставляем один пустой файл, чтобы не ломать расчёты
            files = [FileEntry(_sanitize(_decode_name(info, top)), 0, 0)]

        trackers: list[str] = []
        seen = set()
        urls: list[bytes] = []
        if isinstance(top.get(b"announce"), bytes):
            urls.append(top[b"announce"])
        if isinstance(top.get(b"announce-list"), list):
            for tier in top[b"announce-list"]:
                if isinstance(tier, list):
                    urls.extend(u for u in tier if isinstance(u, bytes))
        for u in urls:
            s = u.decode("utf-8", "replace").strip()
            if s and s not in seen:
                seen.add(s)
                trackers.append(s)

        private = bool(info.get(b"private", 0))
        return cls(info_hash, _decode_name(info, top), piece_length, pieces,
                   files, trackers[:32], private, info_raw)

    def to_torrent_bytes(self) -> bytes:
        info, _ = bcode.decode_prefix(self.info_raw, 0)
        top = {b"info": info}
        if self.trackers:
            top[b"announce"] = self.trackers[0]
            if len(self.trackers) > 1:
                top[b"announce-list"] = [[t.encode("utf-8")] for t in self.trackers]
        return bcode.encode(top)

    def magnet_uri(self) -> str:
        parts = [f"magnet:?xt=urn:btih:{self.info_hash_hex}"]
        parts.append("dn=" + urllib.parse.quote(self.name))
        for t in self.trackers:
            parts.append("tr=" + urllib.parse.quote(t))
        return "&".join(parts)


def parse_magnet(uri: str):
    """→ (info_hash bytes | None, имя | None, [трекеры])."""
    uri = uri.strip()
    if not uri.lower().startswith("magnet:?"):
        raise MetaError("это не magnet-ссылка")
    params = urllib.parse.parse_qs(uri[8:], keep_blank_values=True)
    info_hash = None
    for xt in params.get("xt", []):
        xt = xt.strip()
        low = xt.lower()
        if low.startswith("urn:btih:"):
            v = xt[9:]
            if len(v) == 40:
                try:
                    info_hash = bytes.fromhex(v)
                    break
                except ValueError:
                    pass
            elif len(v) == 32:
                try:
                    info_hash = base64.b32decode(v.upper())
                    break
                except Exception:
                    pass
    name = params.get("dn", [None])[0]
    trackers = [t for t in params.get("tr", []) if t]
    return info_hash, name, trackers
