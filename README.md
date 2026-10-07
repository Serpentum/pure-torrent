<div align="center">

# 🌊 PureTorrent

**Чистый торрент-клиент: без рекламы, баннеров и «премиум»-мусора**

BitTorrent-движок написан с нуля на чистом Python — без libtorrent и закрытых бинарников.
Единственная зависимость — PySide6 для интерфейса.

![Release](https://img.shields.io/github/v/release/Serpentum/pure-torrent?style=flat-square&label=релиз)
![CI](https://img.shields.io/github/actions/workflow/status/Serpentum/pure-torrent/release.yml?style=flat-square&label=CI)
![Python](https://img.shields.io/badge/python-3.10+-blue?style=flat-square)
![Platform](https://img.shields.io/badge/platform-Windows-blueviolet?style=flat-square)
![License](https://img.shields.io/github/license/Serpentum/pure-torrent?style=flat-square)

**[🇬🇧 English version](#-english)**

</div>

---

## 🇷🇺 Русский

### ✨ Возможности

- 📁 добавление `.torrent`-файлов и 🧲 magnet-ссылок (в т.ч. drag&drop в окно)
- 🗂 диалог добавления: имя, размер, список файлов, выбор диска и папки
- ☑️ выбор файлов для скачивания: лишние не качаются и не создаются на диске
- ⬇️⬆️ полноценная загрузка **и раздача**, докачка после перезапуска
- ⏸ пауза/возобновление, удаление (с файлами или без), перепроверка файлов
- 🎬 последовательная загрузка (удобно для видео)
- 📡 трекеры HTTP(S) и UDP (BEP 15), DHT (BEP 5) — магниты работают без трекеров
- 🧬 обмен метаданными ut_metadata (BEP 9): магниты качаются «с нуля»
- 🔒 MSE-шифрование хендшейка — обход DPI-блокировок провайдера (вкл. по умолчанию)
- 🚦 лимиты скорости скачивания/отдачи, лимит пиров
- 📊 статистика: прогресс, скорости, пиры/сиды, ETA, узлы DHT
- 🌙 тёмный интерфейс, ничего лишнего

### 📥 Скачать

Готовый портативный `PureTorrent.exe` собирается автоматически — берите свежий
с [страницы релизов](https://github.com/Serpentum/pure-torrent/releases/latest):
каждый смёрженный в `main` Pull Request поднимает версию (patch) и публикует
новый релиз, а описание PR становится ченджлогом релиза. Прямые пуши в `main`
запрещены — изменения заходят только через PR с зелёными тестами.

> ⚠️ exe не подписан — Windows SmartScreen может предупредить при первом запуске
> («Подробнее → Выполнить в любом случае») или добавьте исключение.

### 🚀 Запуск из исходников

Нужен Python 3.10+:

```bat
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
.venv\Scripts\python.exe app.py
:: или просто
run.bat
```

### 🧪 Тесты

Все офлайн — поднимают свой трекер, сида и качалку на localhost, интернет не нужен:

```bat
python test_local.py     :: e2e: трекер + сид + качалка + магнит (BEP 9)
python test_skip.py      :: исключение файлов, маска кусков, перезапуск
python test_release.py   :: отпускание файлов на паузе/при раздаче
python test_dnd.py       :: файл-аргумент/drag&drop (offscreen)
```

### 🧰 Отладка

Переменная окружения `PT_DEBUG=1` включает отладочный дамп движка в консоль
каждые 5 секунд (состояния торрентов, пирующие адреса, ошибки трекеров):

```bat
set PT_DEBUG=1 && .venv\Scripts\python.exe app.py
```

### 🏗️ Архитектура

| Файл | За что отвечает |
|---|---|
| `bcode.py` | bencode-кодек (BEP 3) |
| `torrent.py` | разбор .torrent и magnet, метаданные |
| `tracker.py` | анонсы трекерам: HTTP(S) и UDP (BEP 15) |
| `dht.py` | DHT-нода: KRPC, поиск пиров, анонс (BEP 5) |
| `mse.py` | MSE-шифрование хендшейка (DH + RC4, обход DPI) |
| `peer.py` | peer-wire протокол + расширения BEP 10/9 |
| `picker.py` | выбор кусков: редкие вперёд / последовательно, эндгейм |
| `storage.py` | файлы на диске, проверка кусков, состояние докачки |
| `engine.py` | оркестратор: торренты, asyncio-поток, снимки для GUI |
| `ui.py` | интерфейс на PySide6 |
| `app.py` | точка входа |

Движок живёт в отдельном потоке со своим asyncio-циклом, GUI общается с ним
через потокобезопасные снимки и команды — интерфейс не подвисает.

### 🔨 Сборка exe вручную

```bat
pip install pyinstaller
python -m PyInstaller --noconsole --onefile --name PureTorrent app.py
:: готовый файл: dist\PureTorrent.exe
```

### 📂 Где хранится состояние

`%APPDATA%\PureTorrent\` — `settings.json`, `state.json` (список торрентов),
`torrents\` (копии .torrent), `pieces\` (карты готовности кусков для докачки).
При первом запуске состояние автоматически переносится из старой папки
`CleanTorrent`, если она есть.

### ⚠️ Ограничения

- только v1-торренты (v2-only — редкость; гибридные работают по v1-части)
- нет uTP и PEX; шифрование данных (RC4-режим MSE) работает, но медленное на
  чистом Python — по умолчанию согласуется быстрый plain-режим после MSE-обмена

### 📄 Лицензия

MIT — см. [LICENSE](LICENSE).

---

<div align="center">

## 🇬🇧 English

*BitTorrent client written from scratch in pure Python. No ads, no bloat, no binaries.*

</div>

### ✨ Features

- 📁 add `.torrent` files and 🧲 magnet links (drag & drop into the window works too)
- 🗂 add dialog: name, size, file list, drive/folder picker
- ☑️ file selection: skipped files are neither downloaded nor created on disk
- ⬇️⬆️ full download **and seeding**, resume across restarts
- ⏸ pause/resume, remove (with or without files), force recheck
- 🎬 sequential download (great for video)
- 📡 HTTP(S) and UDP trackers (BEP 15), DHT (BEP 5) — magnets work trackerless
- 🧬 ut_metadata exchange (BEP 9): magnets start from zero
- 🔒 MSE handshake encryption — bypasses ISP DPI blocks (on by default)
- 🚦 download/upload speed limits, peer limit
- 📊 stats: progress, speeds, peers/seeds, ETA, DHT nodes
- 🌙 dark UI, nothing extra

### 📥 Download

A portable `PureTorrent.exe` is built automatically — grab the latest one from the
[releases page](https://github.com/Serpentum/pure-torrent/releases/latest):
every Pull Request merged into `main` bumps the patch version and publishes a
release, with the PR description serving as the release changelog. Direct pushes
to `main` are forbidden — changes land only via PRs with passing tests.

> ⚠️ The exe is unsigned — Windows SmartScreen may warn on first launch
> ("More info → Run anyway") or add an exclusion.

### 🚀 Run from source

Python 3.10+ required:

```bat
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
.venv\Scripts\python.exe app.py
```

### 🧪 Tests

All offline — they spin up a local tracker, a seeder and a leecher on localhost:

```bat
python test_local.py     :: e2e: tracker + seed + leech + magnet (BEP 9)
python test_skip.py      :: file skipping, piece mask, restart
python test_release.py   :: file handle release on pause/seed
python test_dnd.py       :: file argument / drag & drop (offscreen)
```

### 🧰 Debugging

The `PT_DEBUG=1` environment variable prints an engine debug dump to the console
every 5 seconds (torrent states, peer addresses, tracker errors):

```bat
set PT_DEBUG=1 && .venv\Scripts\python.exe app.py
```

### 🏗️ Architecture

| File | Responsibility |
|---|---|
| `bcode.py` | bencode codec (BEP 3) |
| `torrent.py` | .torrent and magnet parsing, metadata |
| `tracker.py` | tracker announces: HTTP(S) and UDP (BEP 15) |
| `dht.py` | DHT node: KRPC, peer lookup, announce (BEP 5) |
| `mse.py` | MSE handshake encryption (DH + RC4, DPI bypass) |
| `peer.py` | peer-wire protocol + BEP 10/9 extensions |
| `picker.py` | piece picking: rarest first / sequential, endgame |
| `storage.py` | files on disk, piece verification, resume state |
| `engine.py` | orchestrator: torrents, asyncio thread, GUI snapshots |
| `ui.py` | PySide6 interface |
| `app.py` | entry point |

The engine runs in its own thread with a dedicated asyncio loop; the GUI talks
to it via thread-safe snapshots and commands — the interface never freezes.

### 🔨 Building the exe manually

```bat
pip install pyinstaller
python -m PyInstaller --noconsole --onefile --name PureTorrent app.py
:: result: dist\PureTorrent.exe
```

### 📂 State location

`%APPDATA%\PureTorrent\` — `settings.json`, `state.json` (torrent list),
`torrents\` (.torrent copies), `pieces\` (piece readiness maps for resume).
On first launch, state is migrated automatically from the legacy `CleanTorrent`
folder if one exists.

### ⚠️ Limitations

- v1 torrents only (v2-only is rare; hybrid ones work via their v1 part)
- no uTP and no PEX; full-stream RC4 encryption works but is slow in pure
  Python — the fast plain mode is negotiated by default after the MSE exchange

### 📄 License

MIT — see [LICENSE](LICENSE).
