"""PureTorrent — точка входа."""
from __future__ import annotations

import argparse
import sys

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication

from engine import Engine
from ui import MainWindow, apply_dark_theme


def main() -> int:
    ap = argparse.ArgumentParser(description="PureTorrent — чистый торрент-клиент")
    ap.add_argument("files", nargs="*",
                    help=".torrent-файлы или magnet-ссылки (можно перетащить на ярлык)")
    ap.add_argument("--smoke", action="store_true", help=argparse.SUPPRESS)
    args = ap.parse_args()

    app = QApplication(sys.argv)
    app.setApplicationName("PureTorrent")
    app.setOrganizationName("PureTorrent")
    apply_dark_theme(app)

    engine = Engine()
    engine.start()
    win = MainWindow(engine)
    win.show()

    # файлы/магниты из аргументов (drop на ярлык или «открыть с помощью»)
    for f in args.files:
        if f.lower().startswith("magnet:?"):
            win.add_magnet_interactive(f, interactive=not args.smoke)
        elif f.lower().endswith(".torrent"):
            win.add_torrent_path(f, interactive=not args.smoke)

    if args.smoke:
        QTimer.singleShot(6000, app.quit)

    import os
    if os.environ.get("PT_DEBUG"):
        def _dbg():
            for s in engine.snapshot():
                print(f"[dbg] {s.state:11} prog={s.progress:5.1%} peers={s.peers:3} "
                      f"seeds={s.seeds:3} dl={s.dl // 1024:5}КБ/с err={s.error}",
                      flush=True)
            for d in engine.debug_states():
                print(f"[int] {d['name'][:28]:28} {d['state']:11} "
                      f"адресов={d['addrs']:3} пиров={d['peers']:3} "
                      f"трекер={d['terr'] or 'ок'}", flush=True)
            ev = engine.events()
            for e in ev[-3:]:
                print(f"[ev] {e}", flush=True)
        t = QTimer(win)
        t.timeout.connect(_dbg)
        t.start(5000)

    rc = app.exec()
    engine.stop()
    return rc


if __name__ == "__main__":
    sys.exit(main())
