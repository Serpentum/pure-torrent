"""Движок: торренты, трекеры, DHT, пиры. Работает в своём потоке с asyncio-циклом;
GUI общается через snapshot()/events() и методы-команд."""
from __future__ import annotations

import asyncio
import json
import os
import random
import struct
import threading
import time
from dataclasses import dataclass
from pathlib import Path

import bcode
import dht as dht_mod
import mse as mse_mod
import picker as picker_mod
import torrent as torrent_mod
from peer import Peer, PeerDied, MSG_UNCHOKE, MSG_CHOKE, MSG_EXTENDED
from storage import Storage, StateStore, recheck
from tracker import Tracker

TARGET_PEERS = 25
MAX_PEERS = 60
CONNECT_BATCH = 6
UNCHOKE_SLOTS = 4

STATE_METADATA = "metadata"
STATE_CHECKING = "checking"
STATE_DOWNLOADING = "downloading"
STATE_SEEDING = "seeding"
STATE_PAUSED = "paused"
STATE_STOPPED = "stopped"
STATE_ERROR = "error"


def default_state_dir() -> Path:
    base = os.environ.get("APPDATA") or str(Path.home())
    new = Path(base) / "PureTorrent"
    old = Path(base) / "CleanTorrent"
    if not new.exists() and old.exists():
        # миграция состояния со старого имени проекта
        try:
            import shutil
            shutil.copytree(old, new)
        except Exception:
            pass
    return new


@dataclass
class Settings:
    download_dir: str = ""
    listen_port: int = 6881
    max_peers: int = TARGET_PEERS
    download_limit_kbs: int = 0     # 0 = без лимита
    upload_limit_kbs: int = 0
    enable_dht: bool = True
    encrypt: bool = True            # MSE-обфускация хендшейка (от DPI)

    def __post_init__(self):
        if not self.download_dir:
            self.download_dir = str(Path.home() / "Downloads")

    @classmethod
    def load(cls, path: Path) -> "Settings":
        s = cls()
        try:
            data = json.loads(path.read_text("utf-8"))
            if isinstance(data, dict):
                for k, v in data.items():
                    if hasattr(s, k) and not k.startswith("_"):
                        setattr(s, k, v)
        except Exception:
            pass
        return s

    def save(self, path: Path):
        path.write_text(json.dumps(self.__dict__, ensure_ascii=False, indent=1), "utf-8")


@dataclass(frozen=True)
class Snapshot:
    hash: str
    name: str
    state: str
    progress: float
    size: int
    done: int
    dl: int
    ul: int
    peers: int
    seeds: int
    eta: int
    paused: bool
    sequential: bool
    save_path: str
    error: str = ""


class RateLimiter:
    """Простой token bucket; rate == 0 — без лимита."""

    def __init__(self):
        self.rate = 0
        self._tokens = 0.0
        self._ts = time.monotonic()
        self._lock = asyncio.Lock()

    def set_rate(self, bytes_per_sec: int):
        self.rate = max(0, int(bytes_per_sec))
        self._tokens = float(self.rate)
        self._ts = time.monotonic()

    async def acquire(self, n: int):
        if self.rate <= 0:
            return
        while True:
            async with self._lock:
                now = time.monotonic()
                self._tokens = min(self.rate, self._tokens + (now - self._ts) * self.rate)
                self._ts = now
                if self._tokens >= n:
                    self._tokens -= n
                    return
                need = (n - self._tokens) / self.rate
            await asyncio.sleep(min(need, 1.0))


class Torrent:
    """Один торрент: пиры, трекеры, состояние."""

    def __init__(self, engine: "Engine", info_hash: bytes, save_path: str,
                 meta: torrent_mod.TorrentMeta | None = None,
                 display_name: str | None = None,
                 magnet_trackers: list | None = None,
                 magnet_uri: str | None = None, sequential: bool = False,
                 start_paused: bool = False, skip_files=None):
        self.engine = engine
        self.info_hash = info_hash
        self.hash_hex = info_hash.hex()
        self.meta = meta
        self.display_name = display_name
        self.magnet_uri = magnet_uri
        self.magnet_trackers = magnet_trackers or []
        self.save_path = save_path
        self.sequential = sequential
        self.skip_files = frozenset(int(i) for i in (skip_files or ()))
        self._wanted_mask = None    # куски, пересекающие неудалённые файлы
        self.wanted_size = 0        # объём того, что реально качаем
        self.paused = start_paused
        self.state = STATE_METADATA if meta is None else STATE_CHECKING
        self.error = ""
        self.stopping = False
        self.storage: Storage | None = None
        self.picker: picker_mod.Picker | None = None
        self.peers: dict[str, Peer] = {}
        self._peer_tasks: set = set()
        self._addrs: dict = {}          # (ip, port) -> время
        self._addr_retry: list = []     # [(время возврата, addr)] — не отвечают
        self._addr_fails: dict = {}     # addr -> сколько раз не отвечал
        self._tasks = []
        self._announce_state: dict = {}
        self._interval_by_url: dict = {}
        self._check_progress = (0, 1)
        self._meta_fetching = False
        self._meta_buf: dict = {}
        self._meta_total = 0
        self._meta_waiters = []
        self._downloaded = 0
        self._uploaded = 0
        self._dl_rate = 0
        self._ul_rate = 0
        self._last_dl = 0
        self._last_ul = 0
        self._last_rate_at = time.monotonic()
        self._state_dirty = False
        self._last_state_save = 0.0
        self._started_once = False
        self._tick_task = None
        self.tracker_err = ""

    # ---------- свойства ----------

    @property
    def num_pieces_expected(self) -> int:
        return self.meta.num_pieces if self.meta else (1 << 30)

    @property
    def trackers(self) -> list:
        if self.meta:
            return self.meta.trackers or self.magnet_trackers
        return self.magnet_trackers

    def name(self) -> str:
        return self.meta.name if self.meta else (self.display_name or self.hash_hex)

    def total_size(self) -> int:
        return self.meta.total_size if self.meta else 0

    def _compute_wanted(self):
        """Маска нужных кусков и объём скачивания по списку исключённых файлов."""
        meta = self.meta
        if meta is None:
            return
        if not self.skip_files:
            self._wanted_mask = None
            self.wanted_size = meta.total_size
            return
        mask = bytearray((meta.num_pieces + 7) // 8)
        pl = meta.piece_length
        for i, f in enumerate(meta.files):
            if i in self.skip_files or f.length == 0:
                continue
            for p in range(f.offset // pl, (f.offset + f.length - 1) // pl + 1):
                if 0 <= p < meta.num_pieces:
                    picker_mod.bit_set(mask, p)
        self._wanted_mask = mask
        self.wanted_size = sum(f.length for i, f in enumerate(meta.files)
                               if i not in self.skip_files)

    # ---------- запуск/остановка ----------

    async def start(self):
        if self.meta is not None:
            await self._init_from_meta()
        elif not self.paused:
            self._start_activity()
            self.state = STATE_METADATA
        self._tick_task = asyncio.get_running_loop().create_task(self._tick())
        self._tasks.append(self._tick_task)
        if self.paused:
            self.state = STATE_PAUSED

    def _restart_tick(self):
        if self._tick_task is not None:
            self._tick_task.cancel()
        self._tick_task = asyncio.get_running_loop().create_task(self._tick())
        self._tasks = [self._tick_task]

    async def _on_wake(self):
        """Компьютер проснулся: соединения мертвы — всё перезапускаем."""
        if self.stopping or self.paused:
            return
        for t in self._tasks:
            if t is not self._tick_task:
                t.cancel()
        self._tasks = [t for t in self._tasks if t is self._tick_task]
        self._close_peers()
        self._started_once = False
        if self.meta is not None:
            self.state = (STATE_SEEDING if self.picker and self.picker.is_complete()
                          else STATE_DOWNLOADING)
            self._announce_now("")
        self._start_activity()

    def _start_activity(self):
        if self._started_once:
            return
        self._started_once = True
        for url in self.trackers:
            self._tasks.append(asyncio.create_task(self._tracker_loop(url)))
        if self.engine.dht is not None and not (self.meta and self.meta.private):
            self._tasks.append(asyncio.create_task(self._dht_loop()))

    async def stop(self, delete_files: bool = False):
        self.stopping = True
        for t in self._tasks:
            t.cancel()
        self._tasks.clear()
        self._close_peers()
        await self._await_peers()
        # event=stopped на трекеры, суммарно не дольше 5 секунд
        async def _stopped(url):
            try:
                tr = Tracker(url, self.info_hash, self.engine.peer_id,
                             self.engine.listen_port, self._stats,
                             self.engine.tracker_key)
                await asyncio.wait_for(tr.announce("stopped"), 5)
            except Exception:
                pass
        if self.trackers:
            try:
                await asyncio.wait_for(
                    asyncio.gather(*[_stopped(u) for u in self.trackers],
                                   return_exceptions=True), 6)
            except asyncio.TimeoutError:
                pass
        if self.picker is not None and self.meta is not None:
            try:
                await asyncio.to_thread(self._save_piece_state)
            except Exception:
                pass
        if self.storage is not None:
            try:
                self.storage.flush()
                if delete_files:
                    await asyncio.to_thread(self.storage.delete_files)
            except Exception:
                pass
            self.storage.close()
        self.state = STATE_STOPPED

    def _close_peers(self):
        for p in list(self.peers.values()):
            p.dead = True
            if p._writer is not None:
                try:
                    p._writer.close()
                except Exception:
                    pass
        self.peers.clear()
        for t in list(self._peer_tasks):
            t.cancel()

    async def _await_peers(self):
        if self._peer_tasks:
            try:
                await asyncio.wait_for(
                    asyncio.gather(*list(self._peer_tasks), return_exceptions=True), 3)
            except asyncio.TimeoutError:
                pass

    def _spawn_peer(self, p: Peer):
        task = asyncio.get_running_loop().create_task(p.run())
        self._peer_tasks.add(task)
        task.add_done_callback(self._peer_tasks.discard)

    def _save_piece_state(self):
        if self.picker is not None and self.meta is not None:
            self.engine.state_store.save(self.hash_hex, self.picker.have, self.meta)

    # ---------- инициализация по метаданным ----------

    async def _init_from_meta(self):
        self.state = STATE_CHECKING
        self._check_progress = (0, 1)
        meta = self.meta
        try:
            self.storage = Storage(Path(self.save_path), meta, skip_files=self.skip_files)
        except Exception as e:
            self.state = STATE_ERROR
            self.error = f"диск: {e}"
            return
        self.picker = picker_mod.Picker(meta.num_pieces, meta.piece_length, meta.total_size)
        self.picker.sequential = self.sequential
        self._compute_wanted()
        if self._wanted_mask is not None:
            self.picker.set_wanted(self._wanted_mask)

        loop = asyncio.get_running_loop()
        saved = await asyncio.to_thread(self.engine.state_store.load, self.hash_hex, meta)

        def prog(cur, total):
            loop.call_soon_threadsafe(self._set_check_progress, cur, total)

        try:
            sizes_ok = await asyncio.to_thread(self.storage.files_exist_with_size)
            if saved is not None and sizes_ok:
                bf = saved
            elif not await asyncio.to_thread(self.storage.any_file_exists):
                bf = bytearray((meta.num_pieces + 7) // 8)
            else:
                bf = await asyncio.to_thread(recheck, meta, self.storage, saved, prog,
                                             self._wanted_mask)
        except Exception as e:
            self.state = STATE_ERROR
            self.error = f"проверка: {e}"
            return
        self.picker.set_bitfield(bytes(bf))
        # bitfield'ы пиров, подключённых до появления метаданных (магниты)
        for p in self.peers.values():
            if p.bitfield is not None and self.picker is not None \
                    and len(p.bitfield) == len(self.picker.have):
                self.picker.peer_has(p.bitfield)
        await asyncio.to_thread(self._save_piece_state)
        complete = self.picker.is_complete()
        if self.paused:
            self.state = STATE_PAUSED
            return
        self.state = STATE_SEEDING if complete else STATE_DOWNLOADING
        self._start_activity()
        self._announce_now("started")

    def _set_check_progress(self, cur, total):
        self._check_progress = (cur, total)

    # ---------- метаданные по magnet ----------

    def peer_extensions_ready(self, peer: Peer):
        if (self.meta is None and not self._meta_fetching
                and peer.ext.get(b"ut_metadata") and not peer.dead):
            self._meta_fetching = True
            self._tasks.append(asyncio.create_task(self._fetch_metadata(peer)))

    def metadata_piece_received(self, piece, total_size, raw):
        self._meta_buf[piece] = raw
        if total_size:
            self._meta_total = total_size
        for w in self._meta_waiters:
            if not w.done():
                w.set_result(True)
        self._meta_waiters = [w for w in self._meta_waiters if not w.done()]

    def _send_meta_request(self, peer: Peer, ut_id: int, piece: int):
        peer._send_msg(MSG_EXTENDED, bytes([ut_id]) +
                       bcode.encode({b"msg_type": 0, b"piece": piece}))

    async def _fetch_metadata(self, peer: Peer):
        ut_id = peer.ext.get(b"ut_metadata")
        loop = asyncio.get_running_loop()
        try:
            total = peer.metadata_size
            if not total:
                fut = loop.create_future()
                self._meta_waiters.append(fut)
                self._send_meta_request(peer, ut_id, 0)
                await asyncio.wait_for(fut, 20)
                total = self._meta_total
                if not total:
                    return
            pieces = (total + 16383) // 16384
            for piece in range(pieces):
                if piece in self._meta_buf:
                    continue
                ok = False
                for _attempt in range(3):
                    if peer.dead:
                        return
                    fut = loop.create_future()
                    self._meta_waiters.append(fut)
                    self._send_meta_request(peer, ut_id, piece)
                    try:
                        await asyncio.wait_for(fut, 20)
                    except asyncio.TimeoutError:
                        continue
                    if piece in self._meta_buf:
                        ok = True
                        break
                if not ok:
                    return
            data = b"".join(self._meta_buf[i] for i in range(pieces))
            if len(data) != total:
                return
            meta = torrent_mod.TorrentMeta.from_info_bytes(data)
            if meta.info_hash != self.info_hash:
                return
            try:
                (self.engine.torrents_dir / f"{self.hash_hex}.torrent").write_bytes(
                    meta.to_torrent_bytes())
            except OSError:
                pass
            meta.trackers = self.magnet_trackers or meta.trackers
            self.meta = meta
            self.display_name = meta.name
            await self._init_from_meta()
        except (asyncio.TimeoutError, PeerDied, ConnectionError, OSError):
            pass
        except Exception as e:
            self.engine.push_event(f"{self.name()}: метаданные: {e}")
        finally:
            self._meta_fetching = False
            self._meta_buf = {}

    # ---------- трекеры и DHT ----------

    def _stats(self):
        left = 0
        if self.meta and self.picker:
            left = (self.wanted_size or self.meta.total_size) - self.picker.bytes_done()
        return (self._uploaded, self._downloaded, max(0, left))

    def _announce_now(self, event: str):
        for url in self.trackers:
            self._tasks.append(asyncio.create_task(self._tracker_once(url, event)))

    async def _tracker_once(self, url: str, event: str):
        try:
            peers = await self._announce(url, event)
            self.add_addrs(peers)
        except Exception:
            pass

    async def _tracker_loop(self, url: str):
        event = "started"
        fails = 0
        while not self.stopping and not self.paused:
            try:
                peers = await self._announce(url, event)
                event = ""
                fails = 0
                self.tracker_err = ""
                self.add_addrs(peers)
                interval = max(60, min(self._interval_by_url.get(url, 1800), 900))
                # голодаем — спрашиваем трекер чаще
                if len(self.peers) < 3 and not self._addrs and not self._addr_retry:
                    interval = min(interval, 120)
            except asyncio.CancelledError:
                return
            except Exception as e:
                fails += 1
                self.tracker_err = f"{url.split('//')[-1][:32]}: {str(e)[:70]}"
                interval = min(1800, 60 * fails)
            try:
                await asyncio.sleep(interval)
            except asyncio.CancelledError:
                return

    async def _announce(self, url: str, event: str):
        tr = self._announce_state.get(url)
        if tr is None:
            tr = Tracker(url, self.info_hash, self.engine.peer_id,
                         self.engine.listen_port, self._stats, self.engine.tracker_key)
            self._announce_state[url] = tr
        peers = await tr.announce(event)
        self._interval_by_url[url] = tr.interval
        return peers

    async def _dht_loop(self):
        dht = self.engine.dht
        while not self.stopping and not self.paused:
            try:
                await dht.ready.wait()
                peers = await asyncio.wait_for(dht.lookup(self.info_hash), 60)
                if peers:
                    self.add_addrs(peers)
                stored = dht.stored_peers(self.info_hash)
                if stored:
                    self.add_addrs(stored)
            except asyncio.CancelledError:
                return
            except Exception:
                pass
            # без пиров ищем чаще, с пирами — раз в три минуты
            interval = 180 if self.peers else 20
            try:
                await asyncio.sleep(interval)
            except asyncio.CancelledError:
                return

    # ---------- пиры ----------

    def add_addrs(self, addrs):
        for a in addrs:
            if a not in self._addrs and a not in self.engine.own_addrs:
                self._addrs[a] = time.monotonic()

    def attach_incoming(self, reader, writer, addr, initial_head: bytes | None = None,
                        mse_result: dict | None = None) -> Peer | None:
        if len(self.peers) >= MAX_PEERS:
            return None
        peer = Peer(self, addr, reader=reader, writer=writer, outgoing=False,
                    initial_head=initial_head, mse_result=mse_result)
        self.peers[peer.key] = peer
        self._spawn_peer(peer)
        return peer

    async def got_block(self, peer: Peer, p: int, begin: int, data: bytes):
        pf, st = self.picker, self.storage
        if pf is None or st is None or self.meta is None:
            return
        size = self.meta.piece_size(p)
        if size <= 0 or begin + len(data) > size or not data:
            return
        if pf.have_piece(p) or pf.has_block(p, begin):
            return
        try:
            await asyncio.to_thread(st.write, p * self.meta.piece_length + begin, data)
        except Exception as e:
            self.state = STATE_ERROR
            self.error = f"запись: {e}"
            return
        self._downloaded += len(data)
        if pf.got_block(p, begin):
            ok = await asyncio.to_thread(st.verify_piece, p)
            if ok:
                pf.set_have(p)
                self._broadcast_have(p)
                self._state_dirty = True
                if pf.is_complete():
                    self._on_complete()
            else:
                pf.reset_piece(p)
                self.engine.push_event(f"{self.name()}: битый кусок {p}, качаем заново")

    def _broadcast_have(self, p: int):
        msg = struct.pack(">IB", 5, 4) + struct.pack(">I", p)
        for peer in list(self.peers.values()):
            if peer.supported and not peer.dead:
                try:
                    peer._send(msg)
                except PeerDied:
                    pass

    def _on_complete(self):
        self._state_dirty = True
        self._announce_now("completed")
        self.engine.push_event(f"Загрузка завершена: {self.name()}")
        asyncio.get_running_loop().create_task(asyncio.to_thread(self._save_piece_state))
        self.state = STATE_SEEDING
        if self.engine.dht is not None and not (self.meta and self.meta.private):

            async def _ann():
                try:
                    await self.engine.dht.lookup(self.info_hash, announce=True)
                except Exception:
                    pass
            self._tasks.append(asyncio.create_task(_ann()))

    async def serve_request(self, peer: Peer, payload: bytes):
        if len(payload) != 12 or self.storage is None or self.picker is None \
                or self.meta is None:
            return
        p, begin, length = struct.unpack(">III", payload)
        if peer.we_choke or length == 0 or length > 128 * 1024:
            return
        size = self.meta.piece_size(p)
        if size <= 0 or begin + length > size or not self.picker.have_piece(p):
            return
        await self.engine.upload_limiter.acquire(length)
        try:
            data = await asyncio.to_thread(
                self.storage.read, p * self.meta.piece_length + begin, length)
        except Exception:
            return
        if peer.dead or peer.we_choke:
            return
        try:
            peer.send_piece(p, begin, data)
            self._uploaded += len(data)
        except PeerDied:
            pass

    def peer_closed(self, peer: Peer):
        self.peers.pop(peer.key, None)
        if self.picker is not None:
            self.picker.peer_gone(peer.bitfield)
            self.picker.release(peer.key)
        if not peer.outgoing or peer.addr is None or peer.addr[0] == "in":
            return
        if peer.supported:
            self._addr_fails.pop(peer.addr, None)     # живой пир
        else:
            fails = self._addr_fails.get(peer.addr, 0) + 1
            if fails < 3:
                self._addr_retry.append((time.monotonic() + 120, peer.addr))
            else:
                self._addr_fails.pop(peer.addr, None)  # хватит пытаться

    # ---------- основной такт ----------

    async def _tick(self):
        last_unchoke = 0.0
        while not self.stopping:
            await asyncio.sleep(1)
            if self.stopping:
                return
            self._update_rates()
            if self.paused:
                continue
            now = time.monotonic()
            # возврат пиров из кулдауна
            if self._addr_retry:
                ready = [a for t, a in self._addr_retry if t <= now]
                if ready:
                    self._addr_retry = [(t, a) for t, a in self._addr_retry
                                        if t > now]
                    self.add_addrs(ready)
            # подключаем новых пиров из накопителя адресов
            batch = 0
            while (self._addrs and batch < CONNECT_BATCH
                   and len(self.peers) < self.engine.settings.max_peers):
                addr = self._addrs.popitem()[0]
                p = Peer(self, addr)
                self.peers[p.key] = p
                self._spawn_peer(p)
                batch += 1
            # анчоки
            if now - last_unchoke > 12:
                last_unchoke = now
                self._unchoke()
            if self.state not in (STATE_ERROR, STATE_PAUSED, STATE_METADATA,
                                  STATE_CHECKING):
                complete = self.picker is not None and self.picker.is_complete()
                self.state = STATE_SEEDING if complete else STATE_DOWNLOADING
            if self._state_dirty and now - self._last_state_save > 10:
                self._last_state_save = now
                self._state_dirty = False
                await asyncio.to_thread(self._save_piece_state)

    def _unchoke(self):
        interested = [p for p in self.peers.values()
                      if p.supported and not p.dead and p.interested_in_us]
        interested.sort(key=lambda p: p.dl_rate, reverse=True)
        chosen = set(interested[:UNCHOKE_SLOTS - 1])
        rest = [p for p in interested if p not in chosen]
        if rest:
            chosen.add(random.choice(rest))    # оптимистичный анчок
        for p in self.peers.values():
            if not p.supported or p.dead:
                continue
            want = p in chosen
            if want and p.we_choke:
                p.we_choke = False
                try:
                    p._send_msg(MSG_UNCHOKE)
                except PeerDied:
                    pass
            elif not want and not p.we_choke:
                p.we_choke = True
                try:
                    p._send_msg(MSG_CHOKE)
                except PeerDied:
                    pass

    def _update_rates(self):
        now = time.monotonic()
        dt = now - self._last_rate_at
        if dt >= 1.0:
            self._dl_rate = int((self._downloaded - self._last_dl) / dt)
            self._ul_rate = int((self._uploaded - self._last_ul) / dt)
            self._last_dl = self._downloaded
            self._last_ul = self._uploaded
            self._last_rate_at = now

    # ---------- команды ----------

    def pause(self):
        if self.paused:
            return
        self.paused = True
        for t in self._tasks:
            t.cancel()
        self._close_peers()
        if self.storage is not None:
            # отпускаем файлы с запасом: отменённая в пуле-потоков запись
            # ещё может дописывать блок
            async def _release():
                await asyncio.sleep(1.0)
                if self.paused and self.storage is not None:
                    try:
                        self.storage.close()
                    except Exception:
                        pass
            asyncio.get_running_loop().create_task(_release())
        self._started_once = False
        self._restart_tick()
        self.state = STATE_PAUSED
        self._dl_rate = self._ul_rate = 0
        asyncio.get_running_loop().create_task(asyncio.to_thread(self._save_piece_state))
        self.engine._save_engine_state()

    def resume(self):
        if not self.paused:
            return
        self.paused = False
        self._started_once = False
        if self.meta is None:
            self.state = STATE_METADATA
            self._start_activity()
        else:
            self.state = (STATE_SEEDING if self.picker and self.picker.is_complete()
                          else STATE_DOWNLOADING)
            self._announce_now("started")
            self._start_activity()
        self.engine._save_engine_state()

    def set_sequential(self, on: bool):
        self.sequential = bool(on)
        if self.picker:
            self.picker.sequential = self.sequential

    async def force_recheck(self):
        if self.meta is None or self.storage is None:
            return
        for t in self._tasks:
            t.cancel()
        self._close_peers()
        self._started_once = False
        self._restart_tick()
        self.state = STATE_CHECKING
        loop = asyncio.get_running_loop()

        def prog(cur, total):
            loop.call_soon_threadsafe(self._set_check_progress, cur, total)

        try:
            bf = await asyncio.to_thread(recheck, self.meta, self.storage, None, prog,
                                         self._wanted_mask)
        except Exception as e:
            self.state = STATE_ERROR
            self.error = f"проверка: {e}"
            return
        if self.picker:
            self.picker.set_bitfield(bytes(bf))
        await asyncio.to_thread(self._save_piece_state)
        if self.paused:
            self.state = STATE_PAUSED
        else:
            self.state = (STATE_SEEDING if self.picker and self.picker.is_complete()
                          else STATE_DOWNLOADING)
            self._announce_now("started")
            self._start_activity()

    # ---------- снимок ----------

    def snapshot(self) -> Snapshot:
        size = self.wanted_size or self.total_size()
        if self.picker is not None:
            done = self.picker.bytes_done()
        else:
            done = 0
        progress = (done / size) if size else 0.0
        if self.state == STATE_CHECKING:
            cur, total = self._check_progress
            progress = (cur / total) if total else 0.0
        elif self.state == STATE_SEEDING:
            progress = 1.0
        peers = [p for p in self.peers.values() if p.supported and not p.dead]
        seeds = 0
        if self.picker is not None:
            n = self.picker.num_pieces
            for p in peers:
                bf = p.bitfield
                if bf is not None and len(bf) == len(self.picker.have):
                    if int.from_bytes(bytes(bf), "big").bit_count() >= n:
                        seeds += 1
        eta = -1
        if self.state == STATE_DOWNLOADING and self._dl_rate > 0 and size > done:
            eta = int((size - done) / self._dl_rate)
        state = self.state
        if self.paused and self.state != STATE_ERROR:
            state = STATE_PAUSED
        return Snapshot(
            hash=self.hash_hex, name=self.name(), state=state,
            progress=min(1.0, progress), size=size, done=done,
            dl=self._dl_rate, ul=self._ul_rate,
            peers=len(peers), seeds=seeds, eta=eta,
            paused=self.paused, sequential=self.sequential,
            save_path=self.save_path, error=self.error)


class Engine:
    def __init__(self, state_dir: Path | None = None):
        self.state_dir = Path(state_dir) if state_dir else default_state_dir()
        self.torrents_dir = self.state_dir / "torrents"
        self.torrents_dir.mkdir(parents=True, exist_ok=True)
        self.state_store = StateStore(self.state_dir)
        self.settings = Settings.load(self.state_dir / "settings.json")
        self.peer_id = b"-PT1000-" + bytes(random.randint(48, 57) for _ in range(12))
        self.tracker_key = random.getrandbits(31)
        self.listen_port = self.settings.listen_port
        self.dht: dht_mod.DHT | None = None
        self.upload_limiter = RateLimiter()
        self.download_limiter = RateLimiter()
        self.own_addrs = {("127.0.0.1", self.listen_port)}
        self._loop = None
        self._thread = None
        self._stop = threading.Event()
        self._aio_stop = None
        self._pending_calls = []
        self._ready = False
        self._torrents = {}          # hex -> Torrent (только в потоке цикла)
        self._snap: list[Snapshot] = []
        self._totals = (0, 0)
        self._events = []
        self._lock = threading.Lock()
        self._started = False

    # ---------- жизненный цикл ----------

    def start(self):
        if self._started:
            return
        self._started = True
        self._thread = threading.Thread(target=self._thread_main, name="engine",
                                        daemon=True)
        self._thread.start()

    def _thread_main(self):
        if os.name == "nt":
            asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._main())
        except Exception:
            import traceback
            traceback.print_exc()
        finally:
            try:
                self._loop.close()
            except Exception:
                pass

    async def _main(self):
        loop = asyncio.get_running_loop()
        self._aio_stop = asyncio.Event()
        server = None
        port = self.settings.listen_port
        for _attempt in range(10):
            try:
                server = await asyncio.start_server(self._client_cb, "0.0.0.0", port)
                break
            except OSError:
                port += 1
        if server is None:
            self.push_event(f"не удалось занять порт {self.settings.listen_port}..+9")
        else:
            self.listen_port = port
            self.own_addrs = {("127.0.0.1", port)}
        if self.settings.enable_dht:
            self.dht = dht_mod.DHT(port if server else self.settings.listen_port)
            try:
                await self.dht.start()
            except OSError as e:
                self.push_event(f"DHT не запущен: {e}")
                self.dht = None
        self.upload_limiter.set_rate(self.settings.upload_limit_kbs * 1024)
        self._load_state()
        # выполнить команды, присланные до старта
        with self._lock:
            calls, self._pending_calls = self._pending_calls, []
            self._ready = True
        for fn, args, kwargs in calls:
            try:
                fn(*args, **kwargs)
            except Exception:
                pass
        try:
            last_wall = time.time()
            while not self._aio_stop.is_set():
                await asyncio.sleep(0.5)
                self._make_snapshot()
                now = time.time()
                if now - last_wall > 30:
                    # системный сон/зависание: соединениям и анонсам нужна перезагрузка
                    self.push_event("выход из сна — перезапускаю загрузки")
                    for t in list(self._torrents.values()):
                        asyncio.get_running_loop().create_task(t._on_wake())
                    if self.dht is not None:
                        asyncio.get_running_loop().create_task(self.dht._bootstrap())
                last_wall = now
        finally:
            try:
                await asyncio.wait_for(
                    asyncio.gather(*[t.stop() for t in self._torrents.values()],
                                   return_exceptions=True), 12)
            except asyncio.TimeoutError:
                pass
            if self.dht:
                await self.dht.stop()
            if server:
                server.close()
            try:
                self.settings.save(self.state_dir / "settings.json")
                self._save_engine_state()
            except Exception:
                pass

    def stop(self):
        if not self._started:
            return
        loop = self._loop
        if loop is not None and self._aio_stop is not None:
            loop.call_soon_threadsafe(self._aio_stop.set)
        if self._thread:
            self._thread.join(timeout=20)

    # ---------- входящие подключения ----------

    async def _client_cb(self, reader, writer):
        """Входящее подключение: первый байт решает — plain-хендшейк или MSE."""
        try:
            first = await asyncio.wait_for(reader.readexactly(1), 20)
        except (asyncio.IncompleteReadError, asyncio.TimeoutError, ConnectionError,
                OSError):
            writer.close()
            return
        peername = writer.get_extra_info("peername") or ("in", 0)
        if first == b"\x13":
            try:
                rest = await asyncio.wait_for(reader.readexactly(67), 20)
            except (asyncio.IncompleteReadError, asyncio.TimeoutError,
                    ConnectionError, OSError):
                writer.close()
                return
            head = first + rest
            if head[1:20] != b"BitTorrent protocol":
                writer.close()
                return
            t = self._torrents.get(head[28:48].hex())
            if t is None or t.stopping or t.paused or len(t.peers) >= MAX_PEERS:
                writer.close()
                return
            t.attach_incoming(reader, writer, peername, initial_head=head)
            return
        # MSE-соединение
        if not self.settings.encrypt:
            writer.close()
            return
        candidates = [t for t in self._torrents.values()
                      if not t.stopping and not t.paused]
        if not candidates:
            writer.close()
            return
        try:
            res = await asyncio.wait_for(
                mse_mod.respond(reader, writer,
                                [t.info_hash for t in candidates], first), 30)
        except Exception:
            writer.close()
            return
        t = self._torrents.get(res["info_hash"].hex())
        if t is None or t.stopping or t.paused or len(t.peers) >= MAX_PEERS:
            writer.close()
            return
        t.attach_incoming(reader, writer, peername, mse_result=res)

    # ---------- команды из GUI-потока ----------

    def _call(self, fn, *args, **kwargs):
        with self._lock:
            if not self._ready:
                self._pending_calls.append((fn, args, kwargs))
                return

        def _run():
            fn(*args, **kwargs)
        # kwargs нельзя передавать в call_soon_threadsafe: там они значат context
        try:
            self._loop.call_soon_threadsafe(_run)
        except RuntimeError:
            pass

    def add_torrent_file(self, path: str, save_path: str | None = None,
                         skip_files=None) -> str:
        meta = torrent_mod.TorrentMeta.from_bytes(Path(path).read_bytes())
        hexh = meta.info_hash_hex
        self._call(self._add_meta, meta, save_path or self.settings.download_dir,
                   False, skip_files=sorted(skip_files or ()))
        return hexh

    def add_magnet(self, uri: str, save_path: str | None = None,
                   skip_files=None) -> str:
        ih, name, trackers = torrent_mod.parse_magnet(uri)
        if ih is None:
            raise torrent_mod.MetaError("в magnet-ссылке нет info-hash")
        self._call(self._add_magnet, ih, name, trackers, uri,
                   save_path or self.settings.download_dir,
                   sorted(skip_files or ()))
        return ih.hex()

    def pause(self, hexh: str):
        self._call(self._torrent_cmd, hexh, "pause")

    def resume(self, hexh: str):
        self._call(self._torrent_cmd, hexh, "resume")

    def remove(self, hexh: str, delete_files: bool = False):
        self._call(self._remove, hexh, delete_files)

    def set_sequential(self, hexh: str, on: bool):
        self._call(self._torrent_cmd, hexh, "sequential", on)

    def force_recheck(self, hexh: str):
        self._call(self._torrent_cmd, hexh, "recheck")

    def add_peers(self, hexh: str, addrs):
        self._call(self._add_peers, hexh, list(addrs))

    def update_settings(self, changes: dict):
        for k, v in changes.items():
            if hasattr(self.settings, k):
                setattr(self.settings, k, v)
        self._call(self._apply_limits)
        try:
            self.settings.save(self.state_dir / "settings.json")
        except OSError:
            pass

    def _apply_limits(self):
        self.upload_limiter.set_rate(self.settings.upload_limit_kbs * 1024)

    # ---------- обработчики команд (в потоке цикла) ----------

    def _add_meta(self, meta, save_path, start_paused, sequential=False,
                  magnet_uri=None, skip_files=None):
        hexh = meta.info_hash_hex
        if hexh in self._torrents:
            return
        t = Torrent(self, meta.info_hash, save_path, meta=meta,
                    sequential=sequential, start_paused=start_paused,
                    magnet_uri=magnet_uri, skip_files=skip_files)
        self._torrents[hexh] = t
        try:
            (self.torrents_dir / f"{hexh}.torrent").write_bytes(meta.to_torrent_bytes())
        except OSError:
            pass
        asyncio.get_running_loop().create_task(t.start())
        self._save_engine_state()

    def _add_magnet(self, ih, name, trackers, uri, save_path, skip_files=None):
        hexh = ih.hex()
        if hexh in self._torrents:
            return
        t = Torrent(self, ih, save_path, meta=None, display_name=name,
                    magnet_trackers=trackers, magnet_uri=uri,
                    skip_files=skip_files)
        self._torrents[hexh] = t
        asyncio.get_running_loop().create_task(t.start())
        self._save_engine_state()

    def _torrent_cmd(self, hexh, cmd, *args):
        t = self._torrents.get(hexh)
        if t is None:
            return
        if cmd == "pause":
            t.pause()
        elif cmd == "resume":
            t.resume()
        elif cmd == "sequential":
            t.set_sequential(args[0])
        elif cmd == "recheck":
            asyncio.get_running_loop().create_task(t.force_recheck())

    def _remove(self, hexh, delete_files):
        t = self._torrents.get(hexh)
        if t is None:
            return

        async def _rm():
            await t.stop(delete_files=delete_files)
            self._torrents.pop(hexh, None)
            try:
                (self.torrents_dir / f"{hexh}.torrent").unlink(missing_ok=True)
            except OSError:
                pass
            self.state_store.remove(hexh)
            self._save_engine_state()

        asyncio.get_running_loop().create_task(_rm())

    def _add_peers(self, hexh, addrs):
        t = self._torrents.get(hexh)
        if t is not None:
            t.add_addrs(addrs)

    # ---------- сохранение списка торрентов ----------

    def _save_engine_state(self):
        entries = []
        for t in self._torrents.values():
            has_file = (self.torrents_dir / f"{t.hash_hex}.torrent").exists()
            e = {
                "hash": t.hash_hex,
                "save_path": t.save_path,
                "sequential": t.sequential,
                "paused": t.paused,
            }
            if t.skip_files:
                e["skip_files"] = sorted(t.skip_files)
            if has_file:
                e["torrent_file"] = f"torrents/{t.hash_hex}.torrent"
            elif t.magnet_uri:
                e["magnet"] = t.magnet_uri
            elif t.meta is not None:
                e["magnet"] = t.meta.magnet_uri()
            else:
                e["magnet"] = f"magnet:?xt=urn:btih:{t.hash_hex}"
            entries.append(e)
        try:
            tmp = self.state_dir / "state.json.tmp"
            tmp.write_text(json.dumps({"torrents": entries}, ensure_ascii=False,
                                      indent=1), "utf-8")
            os.replace(tmp, self.state_dir / "state.json")
        except OSError:
            pass

    def _load_state(self):
        p = self.state_dir / "state.json"
        if not p.exists():
            return
        try:
            data = json.loads(p.read_text("utf-8"))
        except Exception:
            return
        for e in data.get("torrents", []):
            try:
                meta = None
                tf = e.get("torrent_file")
                if tf:
                    mfile = self.state_dir / tf
                    if mfile.exists():
                        meta = torrent_mod.TorrentMeta.from_bytes(mfile.read_bytes())
                save_path = e.get("save_path") or self.settings.download_dir
                skip = e.get("skip_files") or ()
                if meta is not None:
                    if meta.info_hash_hex != e["hash"]:
                        continue
                    t = Torrent(self, meta.info_hash, save_path, meta=meta,
                                sequential=bool(e.get("sequential")),
                                start_paused=bool(e.get("paused")),
                                skip_files=skip)
                else:
                    magnet = e.get("magnet")
                    if not magnet:
                        continue
                    ih, name, trs = torrent_mod.parse_magnet(magnet)
                    if ih is None or ih.hex() != e["hash"]:
                        continue
                    t = Torrent(self, ih, save_path, meta=None, display_name=name,
                                magnet_trackers=trs, magnet_uri=magnet,
                                sequential=bool(e.get("sequential")),
                                start_paused=bool(e.get("paused")),
                                skip_files=skip)
                if t.hash_hex not in self._torrents:
                    self._torrents[t.hash_hex] = t
                    asyncio.get_running_loop().create_task(t.start())
            except Exception as ex:
                self.push_event(f"восстановление: {ex}")

    # ---------- снимки и события ----------

    def _make_snapshot(self):
        snaps = [t.snapshot() for t in self._torrents.values()]
        snaps.sort(key=lambda s: s.name.lower())
        dl = sum(s.dl for s in snaps)
        ul = sum(s.ul for s in snaps)
        dbg = [{"hash": t.hash_hex, "name": t.name()[:28], "state": t.state,
                "addrs": len(t._addrs), "peers": len(t.peers),
                "terr": t.tracker_err} for t in self._torrents.values()]
        with self._lock:
            self._snap = snaps
            self._totals = (dl, ul)
            self._dbg = dbg

    def debug_states(self) -> list:
        with self._lock:
            return list(getattr(self, "_dbg", []))

    def snapshot(self) -> list[Snapshot]:
        with self._lock:
            return list(self._snap)

    def totals(self):
        with self._lock:
            return self._totals

    def dht_nodes(self) -> int:
        d = self.dht
        return d.node_count() if d else 0

    def push_event(self, msg: str):
        with self._lock:
            self._events.append(str(msg))
            del self._events[:-50]

    def events(self) -> list:
        with self._lock:
            out = self._events
            self._events = []
            return out

    def torrent_file_path(self, hexh: str) -> Path | None:
        p = self.torrents_dir / f"{hexh}.torrent"
        return p if p.exists() else None
