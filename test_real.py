"""Диагностика на живом рое: торрент-файл передаётся аргументом (или ubuntu по умолчанию).
Запуск: python test_real.py [файл.torrent] [секунды]
"""
from __future__ import annotations

import shutil
import sys
import tempfile
import time
from pathlib import Path

import peer as peer_mod
import tracker as tracker_mod
from engine import Engine

_orig_announce = tracker_mod.Tracker.announce
_orig_handshake = peer_mod.Peer._handshake


async def _announce(self, event=""):
    try:
        peers = await _orig_announce(self, event)
        print(f'  [трекер ok] {self.url[:70]} {event or "—"} -> {len(peers)} пиров',
              flush=True)
        return peers
    except Exception as e:
        print(f'  [трекер err] {self.url[:70]} {event or "—"}: {e}', flush=True)
        raise


async def _handshake(self):
    try:
        await _orig_handshake(self)
        print(f'  [peer ok] {self.key} ext={sorted(k.decode() for k in self.ext)}',
              flush=True)
    except Exception as e:
        print(f'  [peer err] {self.key}: {type(e).__name__} {e}', flush=True)
        raise


tracker_mod.Tracker.announce = _announce
peer_mod.Peer._handshake = _handshake


def main() -> int:
    torrent = sys.argv[1] if len(sys.argv) > 1 else "tmp_real/ubuntu.torrent"
    seconds = int(sys.argv[2]) if len(sys.argv) > 2 else 120
    tmp = Path(tempfile.mkdtemp(prefix="ct_real_"))
    dl_dir = tmp / "dl"
    dl_dir.mkdir()
    eng = Engine(state_dir=tmp / "state")
    try:
        h = eng.add_torrent_file(str(torrent), save_path=str(dl_dir))
        eng.start()
        print(f"[*] качаем {torrent} в {dl_dir}, {seconds} c…", flush=True)
        best = None
        for i in range(seconds // 5):
            time.sleep(5)
            for s in eng.snapshot():
                if s.hash == h:
                    best = s
                    print(f"  [{(i + 1) * 5:3}с] {s.state:11} прог={s.progress:6.1%} "
                          f"пиров={s.peers:3} сидов={s.seeds:3} ↓{s.dl // 1024:5}КБ/с "
                          f"DHT={eng.dht_nodes()}", flush=True)
            if best and best.progress >= 1.0:
                break
        ev = eng.events()
        if ev:
            print("[*] события:", *ev[-5:], sep="\n    ", flush=True)
        if best and best.done > 0:
            print(f"\nРЕЗУЛЬТАТ: скачано {best.done} байт, прогресс {best.progress:.1%} ✔",
                  flush=True)
            return 0
        print("\nРЕЗУЛЬТАТ: данные не пошли ✘", flush=True)
        return 1
    finally:
        eng.stop()
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
