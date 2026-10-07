"""Тест сценария «файл перетащили на ярлык»: app.py <файл.torrent> --smoke.
Всё во временных каталогах, APPDATA подменяется."""
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from test_local import make_seed_data, make_torrent_file  # noqa: E402


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="ct_dnd_"))
    app = Path(__file__).parent / "app.py"
    try:
        pack = make_seed_data(tmp)
        torrent = tmp / "dropped.torrent"
        make_torrent_file(pack, torrent, "http://127.0.0.1:1/announce")
        env = dict(os.environ)
        env["APPDATA"] = str(tmp)
        env["QT_QPA_PLATFORM"] = "offscreen"
        print("[*] запускаем app.py с файлом-аргументом (offscreen, 6 c)…")
        r = subprocess.run(
            [sys.executable, str(app), "--smoke", str(torrent)],
            env=env, capture_output=True, text=True, timeout=60)
        out = (r.stdout + r.stderr).strip()
        for line in out.splitlines():
            if "font" in line.lower() or "propagateSizeHints" in line:
                continue
            print("   |", line)
        print(f"[*] код выхода: {r.returncode}")
        assert r.returncode == 0, "приложение упало"
        state_file = tmp / "PureTorrent" / "state.json"
        assert state_file.exists(), "state.json не создан"
        state = json.loads(state_file.read_text("utf-8"))
        entries = state.get("torrents", [])
        assert len(entries) == 1, f"в состоянии {len(entries)} торрентов, ожидался 1"
        saved = tmp / "PureTorrent" / "torrents" / f"{entries[0]['hash']}.torrent"
        assert saved.exists(), ".torrent не скопирован в состояние"
        print("[+] файл-аргумент подхвачен: торрент добавлен и сохранён в состоянии")
        print("ТЕСТ DnD/АРГУМЕНТОВ ПРОЙДЕН ✔")
        return 0
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
