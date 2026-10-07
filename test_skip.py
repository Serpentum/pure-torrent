"""Тест исключения файлов (skip_files): маска кусков, докачка без лишнего,
состояние после перезапуска. Без интернета: свой трекер + сид.

Запуск: python test_skip.py
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import struct
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import bcode
from torrent import TorrentMeta

SEED_PORT = 52001
LEECH_PORT = 52002
TRACKER_PORT = 52081
PORTS = [SEED_PORT, LEECH_PORT]

PIECE = 131072
# размеры подобраны: file2 занимает куски 2..5 целиком, куски 1 и 6 — смешанные
F1, F2, F3 = 200000, 5 * PIECE, 300000
SKIP_IDX = 1        # file2.bin


def sha256_file(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


class TrackerHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if not self.path.startswith("/announce"):
            self.send_error(404)
            return
        peers = b"".join(bytes([127, 0, 0, 1]) + struct.pack(">H", p) for p in PORTS)
        body = bcode.encode({b"interval": 2, b"peers": peers})
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


def make_data(root: Path) -> Path:
    pack = root / "skippack"
    pack.mkdir(parents=True, exist_ok=True)
    (pack / "file1.bin").write_bytes(os.urandom(F1))
    (pack / "file2.bin").write_bytes(os.urandom(F2))
    (pack / "file3.bin").write_bytes(os.urandom(F3))
    return pack


def make_torrent(pack: Path, out: Path, announce: str) -> None:
    files, data = [], b""
    for name in ("file1.bin", "file2.bin", "file3.bin"):
        blob = (pack / name).read_bytes()
        files.append({b"path": [name.encode()], b"length": len(blob)})
        data += blob
    pieces = b"".join(hashlib.sha1(data[i:i + PIECE]).digest()
                      for i in range(0, len(data), PIECE))
    torrent = {
        b"announce": announce.encode(),
        b"info": {
            b"name": pack.name.encode(),
            b"piece length": PIECE,
            b"pieces": pieces,
            b"files": files,
        },
    }
    out.write_bytes(bcode.encode(torrent))


def wait_for(fn, timeout: float, what: str):
    t0 = time.time()
    while time.time() - t0 < timeout:
        v = fn()
        if v:
            return v
        time.sleep(0.5)
    raise AssertionError(f"таймаут: {what}")


def snap_map(engine):
    return {s.hash: s for s in engine.snapshot()}


def check_mask(meta):
    from engine import Torrent
    from picker import bit_get
    t = Torrent(None, meta.info_hash, ".", meta=meta, skip_files={SKIP_IDX})
    t._compute_wanted()
    assert t.wanted_size == F1 + F3, f"wanted_size={t.wanted_size}"
    unwanted = {2, 3, 4, 5}
    for p in range(meta.num_pieces):
        assert bit_get(t._wanted_mask, p) == (p not in unwanted), \
            f"кусок {p}: wanted={bit_get(t._wanted_mask, p)}, ожидалось {p not in unwanted}"
    print(f"[+] маска кусков: исключены {sorted(unwanted)}, размер {t.wanted_size}")


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="cleantorrent_skip_"))
    tracker = None
    engines = []
    try:
        pack = make_data(tmp)
        torrent_path = tmp / "test.torrent"
        announce = f"http://127.0.0.1:{TRACKER_PORT}/announce"
        make_torrent(pack, torrent_path, announce)
        meta = TorrentMeta.from_bytes(torrent_path.read_bytes())

        # 1. маска нужных кусков
        check_mask(meta)

        # 2. трекер и сид
        tracker = ThreadingHTTPServer(("127.0.0.1", TRACKER_PORT), TrackerHandler)
        threading.Thread(target=tracker.serve_forever, daemon=True).start()

        from engine import Engine
        seed = Engine(state_dir=tmp / "s_seed")
        seed.update_settings({"listen_port": SEED_PORT, "enable_dht": False})
        engines.append(seed)
        seed_h = seed.add_torrent_file(str(torrent_path), save_path=str(tmp))
        seed.start()
        wait_for(lambda: (lambda s: s if s and s.state == "seeding" else None)(
            snap_map(seed).get(seed_h)), 30, "сид не дошёл до раздачи")
        print("[+] сид: раздаёт")

        # 3. качалка с исключением file2.bin — движок уже запущен, как в GUI
        # (проверяет путь через call_soon_threadsafe, а не очередь отложенных вызовов)
        dl = tmp / "download"
        leech = Engine(state_dir=tmp / "s_leech")
        leech.update_settings({"listen_port": LEECH_PORT, "enable_dht": False})
        engines.append(leech)
        leech.start()
        wait_for(lambda: leech._ready, 10, "движок качалки не стартовал")
        leech_h = leech.add_torrent_file(str(torrent_path), save_path=str(dl),
                                         skip_files={SKIP_IDX})

        def done():
            s = snap_map(leech).get(leech_h)
            return s if s and s.progress >= 1.0 else None
        s = wait_for(done, 60, "качалка не докачала выбранное")
        assert s.size == F1 + F3, f"размер в снапшоте {s.size} != {F1 + F3}"
        assert s.state == "seeding", f"статус {s.state}"
        print(f"[+] качалка: готово, в снапшоте {s.size} байт (без file2.bin)")

        # нужные файлы — байт в байт
        for name in ("file1.bin", "file3.bin"):
            got = dl / "skippack" / name
            assert got.exists(), f"нет {got}"
            assert sha256_file(got) == sha256_file(pack / name), f"разошлись байты: {name}"
        # исключённый: середина (куски 2..5) не качалась — там нули
        got2 = dl / "skippack" / "file2.bin"
        assert got2.exists(), "file2.bin должен существовать частично (граничные куски)"
        blob = got2.read_bytes()
        head = 2 * PIECE - F1              # доля file2 внутри куска 1
        tail_start = 6 * PIECE - F1        # начало куска 6 внутри file2
        assert blob[:head] == (pack / "file2.bin").read_bytes()[:head], \
            "граничный хвост file2 должен совпасть (кусок 1 качался целиком)"
        mid = blob[head:tail_start]
        assert mid == b"\x00" * len(mid), "середина file2 не должна качаться"
        print("[+] file1/file3 совпали по sha256; file2 — только граничные куски, середина не качалась")

        # 4. сохранение списка исключений
        state = json.loads((tmp / "s_leech" / "state.json").read_text("utf-8"))
        entry = next(e for e in state["torrents"] if e["hash"] == leech_h)
        assert entry.get("skip_files") == [SKIP_IDX], f"skip_files не сохранился: {entry}"
        print("[+] skip_files сохранён в state.json")

        # 5. перезапуск: исключение живёт, докачки file2 не случилось
        leech.stop()
        engines.remove(leech)
        leech2 = Engine(state_dir=tmp / "s_leech")
        leech2.update_settings({"listen_port": LEECH_PORT, "enable_dht": False})
        engines.append(leech2)
        leech2.start()

        def restored():
            for sn in leech2.snapshot():
                if sn.hash == leech_h and sn.state in ("seeding", "downloading"):
                    return sn
            return None
        s2 = wait_for(restored, 30, "торрент не восстановился")
        assert s2.size == F1 + F3, f"после рестарта размер {s2.size}"
        assert s2.progress == 1.0, f"после рестарта прогресс {s2.progress}"
        assert sha256_file(dl / "skippack" / "file1.bin") == sha256_file(pack / "file1.bin")
        print("[+] перезапуск: skip_files восстановлен, готово без докачки file2")

        print("\nВСЕ ТЕСТЫ ПРОЙДЕНЫ ✔")
        return 0
    finally:
        for e in engines:
            try:
                e.stop()
            except Exception:
                pass
        if tracker:
            tracker.shutdown()
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
