"""DHT-нода (BEP 5): KRPC поверх UDP, поиск пиров и анонс для торрентов."""
from __future__ import annotations

import asyncio
import os
import socket
import struct
import time

import bcode

BOOTSTRAP_NODES = [
    ("router.bittorrent.com", 6881),
    ("dht.transmissionbt.com", 6881),
    ("router.utorrent.com", 6881),
    ("dht.libtorrent.org", 25401),
    ("dht.aelitis.com", 6881),
]
K = 8          # размер "k-корзины"
ALPHA = 3      # параллельные запросы
MAX_NODES = 512
PEER_TTL = 30 * 60
TOKEN_TTL = 10 * 60


def _to_int(b: bytes) -> int:
    return int.from_bytes(b, "big")


def _distance(a: bytes, b: bytes) -> int:
    return _to_int(a) ^ _to_int(b)


class _Node:
    __slots__ = ("nid", "addr", "last_seen", "failed")

    def __init__(self, nid: bytes, addr):
        self.nid = nid
        self.addr = addr
        self.last_seen = time.monotonic()
        self.failed = 0


class DHT:
    """Одна нода на движок; торренты дергают lookup()/announce()."""

    def __init__(self, port: int):
        self.node_id = os.urandom(20)
        self.port = port
        self._transport = None
        self._tx = {}                    # txid -> (future, addr, expires)
        self._nodes: dict[bytes, _Node] = {}
        self._secrets = (os.urandom(8), os.urandom(8))
        self._secrets_at = time.monotonic()
        self.peer_store: dict[bytes, dict[tuple, float]] = {}
        self._tasks = []
        self._closed = False
        self.ready = asyncio.Event()

    # ---------- жизненный цикл ----------

    async def start(self):
        loop = asyncio.get_running_loop()
        # SO_EXCLUSIVEADDRUSE: без него Windows молча делит порт с другим
        # процессом (типично — чужой торрент-клиент на 6881), и ответы DHT
        # уходят ему. Занят желаемый порт — берём любой свободный.
        sock = None
        for candidate in (self.port, 0):
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            try:
                s.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
                s.bind(("0.0.0.0", candidate))
                sock = s
                break
            except OSError:
                s.close()
        if sock is None:
            raise OSError("не удалось занять UDP-порт для DHT")
        self.port = sock.getsockname()[1]
        self._transport, _ = await loop.create_datagram_endpoint(
            lambda: _DatagramProtocol(self), sock=sock)
        self._tasks.append(asyncio.create_task(self._maintenance()))
        try:
            await asyncio.wait_for(self._bootstrap(), 25)
        except asyncio.TimeoutError:
            pass
        self.ready.set()

    async def stop(self):
        self._closed = True
        for t in self._tasks:
            t.cancel()
        for fut, _, _ in self._tx.values():
            if not fut.done():
                fut.cancel()
        self._tx.clear()
        if self._transport:
            self._transport.close()

    def node_count(self) -> int:
        return len(self._nodes)

    # ---------- приём пакетов ----------

    def _datagram_received(self, data: bytes, addr):
        if self._closed:
            return
        try:
            msg = bcode.decode(data)
        except bcode.BcodeError:
            return
        if not isinstance(msg, dict):
            return
        y = msg.get(b"y")
        if y == b"r" or y == b"e":
            entry = self._tx.pop(msg.get(b"t"), None)
            if entry is None:
                return
            fut, want_addr, _ = entry
            if fut.done():
                return
            # anycast-роутеры отвечают с другого адреса — сверяем только txid
            if y == b"r":
                node = msg.get(b"r")
                if isinstance(node, dict):
                    nid = node.get(b"id")
                    if isinstance(nid, bytes) and len(nid) == 20:
                        self._touch(nid, addr)
                    fut.set_result(node)
                else:
                    fut.cancel()
            else:
                fut.cancel()
        elif y == b"q":
            asyncio.get_running_loop().create_task(self._on_query(msg, addr))

    # ---------- исходящие запросы ----------

    async def _query(self, addr, name: str, a: dict, timeout: float = 8):
        if self._closed:
            return None
        tx = os.urandom(2)
        fut = asyncio.get_running_loop().create_future()
        self._tx[tx] = (fut, addr, time.monotonic() + timeout)
        payload = bcode.encode({
            b"t": tx, b"y": b"q", b"q": name.encode(),
            b"a": {b"id": self.node_id, **a},
        })
        try:
            self._transport.sendto(payload, addr)
            return await asyncio.wait_for(fut, timeout)
        except (asyncio.TimeoutError, asyncio.CancelledError, OSError):
            return None
        finally:
            self._tx.pop(tx, None)

    # ---------- таблица ----------

    def _touch(self, nid: bytes, addr):
        n = self._nodes.get(nid)
        if n is not None:
            n.last_seen = time.monotonic()
            n.failed = 0
            return
        if len(self._nodes) >= MAX_NODES:
            oldest = min(self._nodes.values(), key=lambda x: x.last_seen)
            self._nodes.pop(oldest.nid, None)
        self._nodes[nid] = _Node(nid, addr)

    def _closest(self, target: bytes, count: int):
        return sorted(self._nodes.values(), key=lambda n: _distance(n.nid, target))[:count]

    @staticmethod
    def _pack_nodes(nodes) -> bytes:
        out = []
        for n in nodes:
            try:
                out.append(n.nid + tracker_mod.pack_compact(n.addr))
            except OSError:
                continue
        return b"".join(out)

    @staticmethod
    def _parse_nodes(blob: bytes):
        out = []
        for i in range(0, len(blob) - 25, 26):
            nid = blob[i:i + 20]
            ip = ".".join(str(b) for b in blob[i + 20:i + 24])
            port = struct.unpack_from(">H", blob, i + 24)[0]
            if port and ip != "0.0.0.0":
                out.append((nid, (ip, port)))
        return out

    # ---------- обработка входящих запросов ----------

    async def _on_query(self, msg: dict, addr):
        t = msg.get(b"t")
        if not isinstance(t, bytes):
            return
        q = msg.get(b"q")
        a = msg.get(b"a")
        if not isinstance(a, dict):
            a = {}
        nid = a.get(b"id")
        if isinstance(nid, bytes) and len(nid) == 20:
            self._touch(nid, addr)
        resp, err = {}, None
        if q == b"ping":
            resp = {}
        elif q == b"find_node":
            target = a.get(b"target")
            resp = {b"nodes": self._pack_nodes(self._closest(target or self.node_id, K))}
        elif q == b"get_peers":
            ih = a.get(b"info_hash")
            resp = {b"token": self._make_token(addr)}
            peers = self.peer_store.get(ih) if isinstance(ih, bytes) else None
            if peers:
                now = time.monotonic()
                vals = []
                for paddr, ts in peers.items():
                    if now - ts <= PEER_TTL:
                        try:
                            vals.append(tracker_mod.pack_compact(paddr))
                        except OSError:
                            pass
                if vals:
                    resp[b"values"] = vals
            if b"values" not in resp:
                resp[b"nodes"] = self._pack_nodes(self._closest(ih or self.node_id, K))
        elif q == b"announce_peer":
            ih = a.get(b"info_hash")
            token = a.get(b"token")
            if not self._check_token(addr, token) or not isinstance(ih, bytes):
                err = (203, "Protocol Error")
            else:
                port = a.get(b"port")
                if a.get(b"implied_port"):
                    port = addr[1]
                try:
                    port = int(port)
                except (TypeError, ValueError):
                    port = 0
                if port > 0:
                    self.peer_store.setdefault(ih, {})[(addr[0], port)] = time.monotonic()
                    if len(self.peer_store[ih]) > 256:
                        store = self.peer_store[ih]
                        for k in sorted(store, key=store.get)[:64]:
                            store.pop(k, None)
                resp = {}
        else:
            err = (204, "Method Unknown")
        reply = {b"t": t, b"y": b"e" if err else b"r"}
        if err:
            reply[b"e"] = list(err)
        else:
            resp[b"id"] = self.node_id
            reply[b"r"] = resp
        try:
            self._transport.sendto(bcode.encode(reply), addr)
        except OSError:
            pass

    # ---------- токены ----------

    def _rotate_secrets(self):
        now = time.monotonic()
        if now - self._secrets_at > TOKEN_TTL:
            self._secrets = (self._secrets[1], os.urandom(8))
            self._secrets_at = now

    def _make_token(self, addr) -> bytes:
        import hashlib
        self._rotate_secrets()
        return hashlib.sha1(addr[0].encode() + self._secrets[1]).digest()[:8]

    def _check_token(self, addr, token) -> bool:
        import hashlib
        if not isinstance(token, bytes):
            return False
        self._rotate_secrets()
        for s in self._secrets:
            if token == hashlib.sha1(addr[0].encode() + s).digest()[:8]:
                return True
        return False

    # ---------- бутстрап и обслуживание ----------

    async def _bootstrap(self):
        loop = asyncio.get_running_loop()

        async def ping(host, port):
            try:
                infos = await loop.getaddrinfo(host, port, family=0x2)  # AF_INET
            except OSError:
                return None
            addr = infos[0][4]
            r = await self._query(addr, "ping", {})
            return addr if r is not None else None

        results = await asyncio.gather(*[ping(h, p) for h, p in BOOTSTRAP_NODES])
        for addr in results:
            if addr is not None:
                await self._find_self(addr)

    async def _find_self(self, addr):
        r = await self._query(addr, "find_node", {b"target": self.node_id})
        if r and isinstance(r.get(b"nodes"), bytes):
            for nid, naddr in self._parse_nodes(r[b"nodes"]):
                self._touch(nid, naddr)

    async def _maintenance(self):
        bootstrapped_once = False
        while not self._closed:
            await asyncio.sleep(30 if len(self._nodes) < 16 else 120)
            if not bootstrapped_once or len(self._nodes) < 16:
                try:
                    await asyncio.wait_for(self._bootstrap(), 20)
                except asyncio.TimeoutError:
                    pass
                bootstrapped_once = True
            now = time.monotonic()
            stale = [n for n in self._nodes.values() if now - n.last_seen > 300]
            for n in stale[:16]:
                r = await self._query(n.addr, "ping", {})
                if r is None:
                    n.failed += 1
                    if n.failed >= 2:
                        self._nodes.pop(n.nid, None)
                else:
                    n.last_seen = now
            for ih in list(self.peer_store):
                store = self.peer_store[ih]
                for paddr in [k for k, ts in store.items() if now - ts > PEER_TTL]:
                    store.pop(paddr, None)
                if not store:
                    self.peer_store.pop(ih, None)
            for tx in [tx for tx, (_, _, exp) in self._tx.items() if exp < now]:
                fut = self._tx.pop(tx, None)
                if fut and not fut[0].done():
                    fut[0].cancel()

    # ---------- публичное: поиск пиров и анонс ----------

    async def lookup(self, info_hash: bytes, announce: bool = False):
        """Итеративный get_peers; возвращает [(ip, port)].
        announce=True — после поиска отправить announce_peer ближайшим."""
        queried = set()
        peers = set()
        tokens = {}
        for _round in range(12):
            candidates = [n for n in self._closest(info_hash, 32) if n.addr not in queried]
            if not candidates:
                break
            candidates.sort(key=lambda n: _distance(n.nid, info_hash))
            batch = candidates[:8]      # параллельно
            results = await asyncio.gather(*[
                self._query(n.addr, "get_peers", {b"info_hash": info_hash}, 6)
                for n in batch])
            progressed = False
            for n, r in zip(batch, results):
                queried.add(n.addr)
                if r is None:
                    n.failed += 1
                    continue
                if isinstance(r.get(b"token"), bytes):
                    tokens[n.addr] = r[b"token"]
                values = r.get(b"values")
                if isinstance(values, list):
                    for v in values:
                        if isinstance(v, bytes) and len(v) == 6:
                            ip = ".".join(str(b) for b in v[:4])
                            port = struct.unpack_from(">H", v, 4)[0]
                            if port:
                                peers.add((ip, port))
                if isinstance(r.get(b"nodes"), bytes):
                    for nid, naddr in self._parse_nodes(r[b"nodes"]):
                        self._touch(nid, naddr)
                        progressed = True
            if not peers and not progressed and len(queried) >= 24:
                break
        if announce:
            targets = [n for n in self._closest(info_hash, K) if n.addr in tokens]
            await asyncio.gather(*[
                self._query(n.addr, "announce_peer", {
                    b"info_hash": info_hash,
                    b"port": self.port,
                    b"token": tokens[n.addr],
                    b"implied_port": 1,
                }) for n in targets
            ], return_exceptions=True)
        return list(peers)

    def stored_peers(self, info_hash: bytes):
        """Пиры, анонсировавшие нам этот торрент (для новых подключений)."""
        store = self.peer_store.get(info_hash)
        if not store:
            return []
        now = time.monotonic()
        return [p for p, ts in store.items() if now - ts <= PEER_TTL]


class _DatagramProtocol(asyncio.DatagramProtocol):
    def __init__(self, node: DHT):
        self._node = node

    def datagram_received(self, data, addr):
        self._node._datagram_received(data, addr)

    def error_received(self, exc):
        pass


# импорт внизу, чтобы избежать цикла при одноимённом модуле
import tracker as tracker_mod  # noqa: E402
