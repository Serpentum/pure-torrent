"""Локальный end-to-end тест без интернета: свой HTTP-трекер, движок-сид,
движок-качалка (.torrent), движок-магнит (метаданные по BEP 9).

Запуск: python test_local.py
"""
from __future__ import annotations

import hashlib
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

SEED_PORT = 51001
LEECH_PORT = 51002
MAGNET_PORT = 51003
TRACKER_PORT = 51081
ALL_PORTS = [SEED_PORT, LEECH_PORT, MAGNET_PORT]


def sha256_file(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


# ---------- мини-трекер ----------

class TrackerHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if not self.path.startswith("/announce"):
            self.send_error(404)
            return
        peers = b"".join(
            bytes([127, 0, 0, 1]) + struct.pack(">H", p) for p in ALL_PORTS)
        body = bcode.encode({
            b"interval": 2,
            b"complete": 1,
            b"incomplete": 1,
            b"peers": peers,
        })
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


# ---------- создание данных и .torrent ----------

def make_seed_data(root: Path) -> Path:
    pack = root / "testpack"
    (pack / "subdir").mkdir(parents=True, exist_ok=True)
    (pack / "a.bin").write_bytes(os.urandom(3 * 1024 * 1024 + 777))
    (pack / "b.txt").write_bytes(b"hello clean torrent\n" * 3000)
    (pack / "subdir" / "c.bin").write_bytes(os.urandom(1024 * 1024))
    return pack


def make_torrent_file(pack: Path, out: Path, announce: str) -> None:
    files = []
    for p in sorted(pack.rglob("*")):
        if p.is_file():
            files.append({
                b"path": [part.encode() for part in p.relative_to(pack).parts],
                b"length": p.stat().st_size,
            })
    piece_length = 131072
    data = b""
    for p in sorted(pack.rglob("*")):
        if p.is_file():
            data += p.read_bytes()
    pieces = b"".join(
        hashlib.sha1(data[i:i + piece_length]).digest()
        for i in range(0, len(data), piece_length))
    info = {
        b"name": pack.name.encode(),
        b"piece length": piece_length,
        b"pieces": pieces,
        b"files": files,
    }
    torrent = {
        b"announce": announce.encode(),
        b"info": info,
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


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="cleantorrent_test_"))
    tracker = None
    engines = []
    try:
        # 1. данные и торрент
        pack = make_seed_data(tmp)
        torrent_path = tmp / "test.torrent"
        announce = f"http://127.0.0.1:{TRACKER_PORT}/announce"
        make_torrent_file(pack, torrent_path, announce)
        meta = TorrentMeta.from_bytes(torrent_path.read_bytes())
        print(f"[+] торрент создан: {meta.num_pieces} кусков, "
              f"{meta.total_size} байт, hash {meta.info_hash_hex[:12]}…")

        # 2. трекер
        tracker = ThreadingHTTPServer(("127.0.0.1", TRACKER_PORT), TrackerHandler)
        threading.Thread(target=tracker.serve_forever, daemon=True).start()
        print("[+] трекер запущен")

        from engine import Engine
        common = dict(enable_dht=False)

        # 3. сид
        seed = Engine(state_dir=tmp / "s_seed")
        seed.update_settings({"listen_port": SEED_PORT, "enable_dht": False})
        engines.append(seed)
        seed_h = seed.add_torrent_file(str(torrent_path), save_path=str(tmp))
        seed.start()

        def seeded():
            s = snap_map(seed).get(seed_h)
            return s if s and s.state == "seeding" else None
        wait_for(seeded, 30, "сид не дошёл до раздачи")
        print("[+] сид: файлы проверены, раздача")

        # 4. качалка
        dl = tmp / "download"
        leech = Engine(state_dir=tmp / "s_leech")
        leech.update_settings({"listen_port": LEECH_PORT, "enable_dht": False})
        engines.append(leech)
        leech_h = leech.add_torrent_file(str(torrent_path), save_path=str(dl))
        leech.start()

        def downloaded():
            s = snap_map(leech).get(leech_h)
            return s if s and s.progress >= 1.0 else None
        s = wait_for(downloaded, 60, "качалка не докачала")
        print(f"[+] качалка: скачано за {s.size} байт, пиры={s.peers}")
        for p in sorted(pack.rglob("*")):
            if p.is_file():
                got = dl / "testpack" / p.relative_to(pack)
                assert got.exists(), f"нет файла {got}"
                assert sha256_file(got) == sha256_file(p), f"разошлись байты: {got}"
        print("[+] качалка: sha256 всех файлов совпал")

        # 5. пауза/резюм
        leech.pause(leech_h)
        time.sleep(2)
        assert snap_map(leech)[leech_h].paused, "не встал на паузу"
        leech.resume(leech_h)
        time.sleep(2)
        assert not snap_map(leech)[leech_h].paused, "не снялся с паузы"
        assert snap_map(leech)[leech_h].state == "seeding", "после докачки не раздаёт"
        print("[+] пауза/резюм работают, качалка раздаёт")

        # 6. магнит: третий движок, метаданные по BEP 9 от сида/качалки
        magnet_uri = meta.magnet_uri()
        dl2 = tmp / "download_magnet"
        mag = Engine(state_dir=tmp / "s_magnet")
        mag.update_settings({"listen_port": MAGNET_PORT, "enable_dht": False})
        engines.append(mag)
        mag_h = mag.add_magnet(magnet_uri, save_path=str(dl2))
        mag.start()

        def got_meta():
            s = snap_map(mag).get(mag_h)
            return s if s and s.state not in ("metadata",) else None
        wait_for(got_meta, 60, "метаданные по магниту не получены")
        print("[+] магнит: метаданные получены по BEP 9")

        def magnet_done():
            s = snap_map(mag).get(mag_h)
            return s if s and s.progress >= 1.0 else None
        wait_for(magnet_done, 60, "магнит не докачал")
        for p in sorted(pack.rglob("*")):
            if p.is_file():
                got = dl2 / "testpack" / p.relative_to(pack)
                assert got.exists(), f"нет файла {got}"
                assert sha256_file(got) == sha256_file(p), f"разошлись байты: {got}"
        assert (tmp / "s_magnet" / "torrents" / f"{mag_h}.torrent").exists(), \
            ".torrent не сохранён после метаданных"
        ms = snap_map(mag)[mag_h]
        assert ms.state == "seeding", \
            f"после докачки магнит должен раздавать, а стал {ms.state}"
        print("[+] магнит: файлы скачаны, sha256 совпал, .torrent сохранён, раздаёт")

        # 7. удаление
        mag.remove(mag_h)
        time.sleep(2)
        assert mag_h not in snap_map(mag), "торрент не удалился"
        print("[+] удаление работает")

        # 8. перезапуск движка: восстановление списка торрентов
        leech.stop()
        from engine import Engine as _E
        leech2 = _E(state_dir=tmp / "s_leech")
        leech2.update_settings({"listen_port": LEECH_PORT, "enable_dht": False})
        engines.append(leech2)
        leech2.start()

        def restored():
            for s in leech2.snapshot():
                if s.hash == leech_h and s.state in ("seeding", "downloading"):
                    return s
            return None
        wait_for(restored, 30, "торрент не восстановился после перезапуска")
        assert (dl / "testpack" / "a.bin").exists(), "файлы пропали после перезапуска"
        print("[+] перезапуск: список восстановлен, файлы на месте")

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
