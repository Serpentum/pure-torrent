"""Тест отпускания файлов: во время раздачи файлы залочены, на паузе —
свободны (переименование на Windows падает, пока процесс держит хэндл).

Запуск: python test_release.py
"""
from __future__ import annotations

import shutil
import sys
import tempfile
import threading
import time
from http.server import ThreadingHTTPServer
from pathlib import Path

import test_skip as ts

SEED_PORT, LEECH_PORT, TRACKER_PORT = 52101, 52102, 52181
ts.PORTS = [SEED_PORT, LEECH_PORT]


def rename_ok(p: Path) -> bool:
    tmp = p.with_name(p.name + ".__locktest")
    try:
        p.rename(tmp)
        tmp.rename(p)
        return True
    except OSError:
        return False


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="cleantorrent_release_"))
    tracker = None
    engines = []
    try:
        pack = ts.make_data(tmp)
        torrent_path = tmp / "test.torrent"
        announce = f"http://127.0.0.1:{TRACKER_PORT}/announce"
        ts.make_torrent(pack, torrent_path, announce)

        tracker = ThreadingHTTPServer(("127.0.0.1", TRACKER_PORT), ts.TrackerHandler)
        threading.Thread(target=tracker.serve_forever, daemon=True).start()

        from engine import Engine

        # 1. сид: пока раздаёт — файлы залочены, на паузе — свободны
        seed = Engine(state_dir=tmp / "s_seed")
        seed.update_settings({"listen_port": SEED_PORT, "enable_dht": False})
        engines.append(seed)
        seed_h = seed.add_torrent_file(str(torrent_path), save_path=str(tmp))
        seed.start()
        ts.wait_for(lambda: (lambda s: s if s and s.state == "seeding" else None)(
            ts.snap_map(seed).get(seed_h)), 30, "сид не дошёл до раздачи")
        f1 = pack / "file1.bin"
        assert not rename_ok(f1), "тест ничего не ловит: файл и так переименовался"
        seed.pause(seed_h)
        ts.wait_for(lambda: ts.snap_map(seed).get(seed_h).paused, 10, "сид не встал на паузу")
        time.sleep(1.5)   # пауза отпускает файлы с задержкой в 1 секунду
        assert rename_ok(f1), "после паузы файл сида всё ещё залочен"
        print("[+] сид: во время раздачи файл залочен, на паузе — отпущен")

        seed.resume(seed_h)
        ts.wait_for(lambda: (lambda s: s if s and s.state == "seeding" else None)(
            ts.snap_map(seed).get(seed_h)), 30, "сид не вернулся к раздаче")

        # 2. качалка: докачала и раздаёт — файл залочен, на паузе — отпущен
        dl = tmp / "download"
        leech = Engine(state_dir=tmp / "s_leech")
        leech.update_settings({"listen_port": LEECH_PORT, "enable_dht": False})
        engines.append(leech)
        leech_h = leech.add_torrent_file(str(torrent_path), save_path=str(dl))
        leech.start()

        def seeding():
            s = ts.snap_map(leech).get(leech_h)
            return s if s and s.state == "seeding" and s.progress >= 1.0 else None
        s = ts.wait_for(seeding, 120, "качалка не докачала")
        d1 = dl / "skippack" / "file1.bin"
        assert not rename_ok(d1), "качалка докачала и раздаёт — файл должен быть залочен"
        assert d1.stat().st_size == ts.F1
        leech.pause(leech_h)
        ts.wait_for(lambda: ts.snap_map(leech).get(leech_h).paused, 10,
                    "качалка не встала на паузу")
        time.sleep(1.5)   # пауза отпускает файлы с задержкой в 1 секунду
        assert rename_ok(d1), "после паузы файл качалки всё ещё залочен"
        print("[+] качалка: докачала и раздаёт (файл залочен), на паузе — отпущен")

        # 3. перезапуск: проверка с диска и снова раздача
        leech.stop()
        engines.remove(leech)
        leech2 = Engine(state_dir=tmp / "s_leech")
        leech2.update_settings({"listen_port": LEECH_PORT, "enable_dht": False})
        engines.append(leech2)
        leech2.start()
        # сохранённый на паузе торрент восстанавливается на паузе — снимаем её
        ts.wait_for(lambda: leech2._ready, 10, "движок после рестарта не стартовал")
        ts.wait_for(lambda: ts.snap_map(leech2).get(leech_h), 30,
                    "после рестарта торрент не появился в списке")
        assert ts.snap_map(leech2)[leech_h].paused, \
            "после рестарта торрент должен быть на паузе (как сохранён)"
        leech2.resume(leech_h)

        def seeding2():
            for s in leech2.snapshot():
                if s.hash == leech_h and s.state == "seeding":
                    return s
            return None
        ts.wait_for(seeding2, 30, "после рестарта и resume торрент не раздаёт")
        print("[+] перезапуск: проверка с диска, resume — снова раздача")

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
