"""Файлы на диске: блочное чтение/запись поверх списка файлов, проверка кусков,
сохранение состояния докачки."""
from __future__ import annotations

import hashlib
import json
import os
from bisect import bisect_right
from pathlib import Path

from torrent import TorrentMeta


class StorageError(Exception):
    pass


class Storage:
    def __init__(self, save_path: Path, meta: TorrentMeta,
                 skip_files=None):
        self.save_path = Path(save_path)
        self.meta = meta
        # индексы исключённых файлов (не качаем). Куски на границе нужного
        # и исключённого файла пишутся целиком — хэш куска покрывает все байты
        self.skip = frozenset(skip_files or ())
        # мульти-файловый торрент: файлы в save_path/<имя торрента>/...
        # одиночный: сам файл лежит в save_path/<имя>
        if len(meta.files) > 1:
            root = self.save_path / meta.name
        else:
            root = self.save_path
        self.files = []
        for f in meta.files:
            self.files.append((root / f.path, f.length, f.offset))
        self._offsets = [f.offset for f in meta.files]
        self._handles: dict[str, object] = {}
        self.written = 0

    # ---------- служебное ----------

    def _file_at(self, offset: int):
        idx = bisect_right(self._offsets, offset) - 1
        return self.files[idx]

    def _handle(self, path: Path):
        key = str(path)
        h = self._handles.get(key)
        if h is None:
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                h = open(path, "r+b")
            except FileNotFoundError:
                h = open(path, "w+b")
            except OSError as e:
                raise StorageError(f"не открыть {path}: {e}")
            self._handles[key] = h
        return h

    def close(self):
        for h in self._handles.values():
            try:
                h.flush()
                h.close()
            except OSError:
                pass
        self._handles.clear()

    def flush(self):
        for h in self._handles.values():
            try:
                h.flush()
            except OSError:
                pass

    # ---------- чтение/запись ----------

    def write(self, offset: int, data: bytes):
        pos = 0
        while pos < len(data):
            path, length, foff = self._file_at(offset + pos)
            rel = offset + pos - foff
            n = min(len(data) - pos, length - rel)
            if n <= 0:
                raise StorageError("запись за пределами файлов")
            h = self._handle(path)
            h.seek(rel)
            h.write(data[pos:pos + n])
            pos += n
        self.written += len(data)

    def read(self, offset: int, length: int) -> bytes:
        out = bytearray()
        pos = 0
        while pos < length:
            path, flen, foff = self._file_at(offset + pos)
            rel = offset + pos - foff
            n = min(length - pos, flen - rel)
            if n <= 0 or not path.exists():
                out += b"\x00" * max(0, n)
                pos += max(0, n)
                continue
            h = self._handle(path)
            h.seek(rel)
            chunk = h.read(n)
            out += chunk
            if len(chunk) < n:
                out += b"\x00" * (n - len(chunk))
            pos += n
        return bytes(out)

    def any_file_exists(self) -> bool:
        return any(p.exists() and p.stat().st_size > 0 for p, _, _ in self.files)

    def files_exist_with_size(self) -> bool:
        for i, (p, length, _) in enumerate(self.files):
            if length == 0 or i in self.skip:
                continue
            if not p.exists() or p.stat().st_size != length:
                return False
        return True

    # ---------- куски ----------

    def piece_offset(self, p: int) -> int:
        return p * self.meta.piece_length

    def verify_piece(self, p: int) -> bool:
        size = self.meta.piece_size(p)
        if size <= 0:
            return True
        data = self.read(self.piece_offset(p), size)
        return hashlib.sha1(data).digest() == self.meta.pieces[p]

    def delete_files(self):
        self.close()
        for p, _, _ in self.files:
            try:
                if p.is_file():
                    p.unlink()
            except OSError:
                pass
        # убрать опустевшие каталоги внутри save_path
        dirs = sorted({p.parent for p, _, _ in self.files}, key=lambda d: len(d.parts),
                      reverse=True)
        for d in dirs:
            if d == self.save_path:
                continue
            try:
                next(d.iterdir())
            except StopIteration:
                try:
                    d.rmdir()
                except OSError:
                    pass
            except OSError:
                pass


def recheck(meta: TorrentMeta, storage: Storage, bitfield: bytearray | None,
            progress_cb=None, wanted_mask: bytearray | None = None):
    """Проверка кусков на диске (вызывать в рабочем потоке).
    bitfield — ранее сохранённое состояние (может быть None).
    wanted_mask — куски, которые качаем (None = все); остальные не проверяем.
    → bytearray готовности."""
    from picker import bit_get, bit_set

    n = meta.num_pieces
    result = bytearray((n + 7) // 8)
    skip_full = bitfield is not None and storage.files_exist_with_size()
    for p in range(n):
        if wanted_mask is not None and not bit_get(wanted_mask, p):
            continue    # кусок исключённых файлов — не нужен и не проверяется
        if skip_full and bit_get(bitfield, p):
            bit_set(result, p)
            continue
        # не хэшируем отсутствующие файлы
        size = meta.piece_size(p)
        if size > 0:
            path, flen, foff = storage._file_at(p * meta.piece_length)
            if not path.exists() or path.stat().st_size == 0:
                if progress_cb and p % 32 == 0:
                    progress_cb(p + 1, n)
                continue
        if storage.verify_piece(p):
            bit_set(result, p)
        if progress_cb and p % 32 == 0:
            progress_cb(p + 1, n)
    if progress_cb:
        progress_cb(n, n)
    return result


class StateStore:
    """Сохранение bitfield докачки: state_dir/pieces/<hex>.json"""

    def __init__(self, state_dir: Path):
        self.dir = Path(state_dir) / "pieces"
        self.dir.mkdir(parents=True, exist_ok=True)

    def path_for(self, info_hash_hex: str) -> Path:
        return self.dir / f"{info_hash_hex}.json"

    def save(self, info_hash_hex: str, bitfield: bytearray, meta: TorrentMeta):
        files = [[f.path, f.length] for f in meta.files]
        data = {
            "files": files,
            "bitfield": bytes(bitfield).hex(),
        }
        tmp = self.path_for(info_hash_hex).with_suffix(".tmp")
        tmp.write_text(json.dumps(data), encoding="utf-8")
        os.replace(tmp, self.path_for(info_hash_hex))

    def load(self, info_hash_hex: str, meta: TorrentMeta):
        p = self.path_for(info_hash_hex)
        if not p.exists():
            return None
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            bf = bytearray.fromhex(data["bitfield"])
            files = [[f.path, f.length] for f in meta.files]
            if data.get("files") != files:
                return None
            if len(bf) != (meta.num_pieces + 7) // 8:
                return None
            return bf
        except Exception:
            return None

    def remove(self, info_hash_hex: str):
        try:
            self.path_for(info_hash_hex).unlink(missing_ok=True)
        except OSError:
            pass
