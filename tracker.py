"""Анонс трекерам: HTTP(S) через urllib и UDP (BEP 15)."""
from __future__ import annotations

import asyncio
import socket
import struct
import urllib.request
import urllib.parse

import bcode


class TrackerError(Exception):
    pass


def _esc(raw: bytes) -> str:
    return "".join("%%%02x" % b for b in raw)


def unpack_compact(blob: bytes):
    """6-байтовые записи ip:port → [(ip, port)]."""
    out = []
    for i in range(0, len(blob) - 5, 6):
        port = struct.unpack_from(">H", blob, i + 4)[0]
        if port:
            out.append((".".join(str(b) for b in blob[i:i + 4]), port))
    return out


def pack_compact(addr) -> bytes:
    return socket.inet_aton(addr[0]) + struct.pack(">H", addr[1])


def _clamp(v, lo, hi):
    try:
        v = int(v)
    except (TypeError, ValueError):
        v = hi
    return max(lo, min(hi, v))


class Tracker:
    """Один announce-URL. Значения uploaded/downloaded/left передаёт движок."""

    def __init__(self, url: str, info_hash: bytes, peer_id: bytes, port: int,
                 stats_fn, key: int):
        self.url = url
        self.info_hash = info_hash
        self.peer_id = peer_id
        self.port = port
        self._stats_fn = stats_fn        # () -> (uploaded, downloaded, left)
        self.key = key
        self.interval = 1800
        self.seeds = 0
        self.leeches = 0

    async def announce(self, event: str = ""):
        """→ список (ip, port). Бросает TrackerError."""
        if self.url.startswith(("udp://", "UDP://")):
            return await self._udp(event)
        if self.url.startswith(("http://", "https://", "HTTP://", "HTTPS://")):
            return await self._http(event)
        raise TrackerError(f"неизвестная схема url: {self.url}")

    # ---------- HTTP(S) ----------

    async def _http(self, event: str):
        uploaded, downloaded, left = self._stats_fn()
        qs = "&".join([
            "info_hash=" + _esc(self.info_hash),
            "peer_id=" + _esc(self.peer_id),
            "port=%d" % self.port,
            "uploaded=%d" % uploaded,
            "downloaded=%d" % downloaded,
            "left=%d" % left,
            "compact=1", "no_peer_id=1", "numwant=50",
            "key=%08x" % self.key,
        ] + (["event=" + event] if event else []))
        url = self.url + ("&" if "?" in self.url else "?") + qs

        def get():
            req = urllib.request.Request(url, headers={"User-Agent": "PureTorrent/1.0"})
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.read(1 << 20)

        try:
            data = await asyncio.to_thread(get)
        except Exception as e:
            raise TrackerError(f"http: {e}")
        try:
            resp = bcode.decode(data)
        except bcode.BcodeError as e:
            raise TrackerError(f"ответ не bencode: {e}")
        if not isinstance(resp, dict):
            raise TrackerError("ответ трекера не словарь")
        if isinstance(resp.get(b"failure reason"), bytes):
            raise TrackerError("трекер: " + resp[b"failure reason"].decode("utf-8", "replace"))
        peers = []
        raw_peers = resp.get(b"peers")
        if isinstance(raw_peers, bytes):
            peers = unpack_compact(raw_peers)
        elif isinstance(raw_peers, list):
            for p in raw_peers:
                if isinstance(p, dict) and isinstance(p.get(b"ip"), bytes):
                    try:
                        peers.append((p[b"ip"].decode(), int(p.get(b"port", 0))))
                    except (ValueError, TypeError):
                        continue
        peers6 = resp.get(b"peers6")
        if isinstance(peers6, bytes):
            for i in range(0, len(peers6) - 17, 18):
                port = struct.unpack_from(">H", peers6, i + 16)[0]
                if port:
                    peers.append((socket.inet_ntop(socket.AF_INET6, peers6[i:i + 16]), port))
        self.interval = _clamp(resp.get(b"interval", 1800), 60, 3600)
        self.seeds = _as_int(resp.get(b"complete"))
        self.leeches = _as_int(resp.get(b"incomplete"))
        return peers

    # ---------- UDP (BEP 15) ----------

    async def _udp(self, event: str):
        loop = asyncio.get_running_loop()
        parsed = urllib.parse.urlsplit(self.url)
        host = parsed.hostname
        if not host or not parsed.port:
            raise TrackerError("битый udp-url")
        try:
            infos = await loop.getaddrinfo(host, parsed.port, family=socket.AF_INET)
        except OSError as e:
            raise TrackerError(f"dns: {e}")
        addr = infos[0][4]
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setblocking(False)
        try:
            conn_id = await self._udp_connect(loop, sock, addr)
            return await self._udp_announce(loop, sock, addr, conn_id, event)
        finally:
            sock.close()

    async def _udp_connect(self, loop, sock, addr):
        import os as _os
        for attempt in range(2):
            tx = _os.urandom(4)
            pkt = struct.pack(">QII", 0x41727101980, 0, int.from_bytes(tx, "big"))
            sock.sendto(pkt, addr)
            try:
                data = await asyncio.wait_for(loop.sock_recv(sock, 2048), 15)
            except (asyncio.TimeoutError, OSError) as e:
                if attempt == 1:
                    raise TrackerError(f"udp connect: {e}")
                continue
            if len(data) >= 16:
                action, recv_tx = struct.unpack_from(">II", data)
                if action == 0 and recv_tx == int.from_bytes(tx, "big"):
                    return struct.unpack_from(">Q", data, 8)[0]
        raise TrackerError("udp connect: нет ответа")

    async def _udp_announce(self, loop, sock, addr, conn_id, event):
        import os as _os
        uploaded, downloaded, left = self._stats_fn()
        code = {"started": 2, "completed": 1, "stopped": 3}.get(event, 0)
        for attempt in range(2):
            tx = _os.urandom(4)
            pkt = struct.pack(
                ">QII20s20sQQQIIIiH",
                conn_id, 1, int.from_bytes(tx, "big"),
                self.info_hash, self.peer_id,
                downloaded, left, uploaded,
                code, 0, self.key & 0x7FFFFFFF, -1, self.port,
            )
            sock.sendto(pkt, addr)
            try:
                data = await asyncio.wait_for(loop.sock_recv(sock, 4096), 15)
            except (asyncio.TimeoutError, OSError) as e:
                if attempt == 1:
                    raise TrackerError(f"udp announce: {e}")
                continue
            if len(data) >= 20:
                action, recv_tx = struct.unpack_from(">II", data)
                if action == 1 and recv_tx == int.from_bytes(tx, "big"):
                    interval, leechers, seeders = struct.unpack_from(">III", data, 8)
                    self.interval = _clamp(interval or 1800, 60, 3600)
                    self.seeds, self.leeches = seeders, leechers
                    return unpack_compact(data[20:])
                if action == 3 and len(data) >= 20:
                    msg = data[20:].decode("utf-8", "replace")
                    raise TrackerError(f"трекер: {msg}")
        raise TrackerError("udp announce: нет ответа")


def _as_int(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return 0
