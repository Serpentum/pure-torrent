"""Peer-wire протокол (BEP 3) + расширения BEP 10/9 + MSE-обфускация хендшейка."""
from __future__ import annotations

import asyncio
import struct
import time

import bcode
import mse as mse_mod

PROTO_NAME = b"BitTorrent protocol"
RESERVED = b"\x00\x00\x00\x00\x00\x10\x00\x01"   # LTEP + DHT
PIPELINE = 6                 # незакрытых блоков на пира
KEEPALIVE_IDLE = 60          # шлём keep-alive, если тихо
READ_TIMEOUT = 120           # рвём связь, если вообще ничего не приходит
MAX_MSG = 4 * 1024 * 1024    # максимальный размер сообщения

MSG_CHOKE = 0
MSG_UNCHOKE = 1
MSG_INTERESTED = 2
MSG_NOT_INTERESTED = 3
MSG_HAVE = 4
MSG_BITFIELD = 5
MSG_REQUEST = 6
MSG_PIECE = 7
MSG_CANCEL = 8
MSG_PORT = 9
MSG_EXTENDED = 20


class PeerDied(Exception):
    pass


class _CryptReader:
    """StreamReader поверх RC4-потока (режим crypto_select=2)."""

    def __init__(self, reader, dec, pending: bytes = b""):
        self._reader = reader
        self._dec = dec
        self._buf = self._dec.crypt(pending) if pending else b""

    async def readexactly(self, n):
        while len(self._buf) < n:
            chunk = await self._reader.read(1 << 16)
            if not chunk:
                raise asyncio.IncompleteReadError(self._buf, n)
            self._buf += self._dec.crypt(chunk)
        out, self._buf = self._buf[:n], self._buf[n:]
        return out


class _CryptWriter:
    def __init__(self, writer, enc):
        self._writer = writer
        self._enc = enc

    @property
    def transport(self):
        return self._writer.transport

    def write(self, data: bytes):
        self._writer.write(self._enc.crypt(data))

    def close(self):
        self._writer.close()


class Peer:
    def __init__(self, torrent, addr, reader=None, writer=None, outgoing=True,
                 initial_head: bytes | None = None, mse_result: dict | None = None):
        self.torrent = torrent
        self.addr = addr or ("?", 0)
        self.key = f"{self.addr[0]}:{self.addr[1]}"
        self.outgoing = outgoing
        self._reader = reader
        self._writer = writer
        self._initial_head = initial_head   # уже прочитанный plain-хендшейк пира
        self._mse_result = mse_result       # MSE уже согласован движком (входящее)
        self._pre = b""                     # байты, прочитанные сверх нужного
        self.bitfield: bytearray | None = None
        self.choking_us = True
        self.interested_in_us = False
        self.we_choke = True
        self.we_interested = False
        self.ext: dict[bytes, int] = {}
        self.metadata_size = 0
        self.outstanding = 0
        self.downloaded = 0
        self.uploaded = 0
        self.dl_rate = 0
        self._last_rx = time.monotonic()
        self._last_tx = time.monotonic()
        self._last_count = 0
        self._count_at = time.monotonic()
        self.dead = False
        self.supported = False   # рукопожатие прошло

    # ---------- низкий уровень ----------

    def _send(self, data: bytes):
        if self.dead or self._writer is None:
            raise PeerDied()
        try:
            self._writer.write(data)
            self._last_tx = time.monotonic()
        except (ConnectionError, OSError, RuntimeError):
            raise PeerDied()

    def _send_msg(self, mid: int, payload: bytes = b""):
        self._send(struct.pack(">IB", 1 + len(payload), mid) + payload)

    def _send_ok(self) -> bool:
        """Не переполнен ли буфер отправки."""
        try:
            tr = self._writer.transport
            return tr.get_write_buffer_size() < (1 << 20)
        except Exception:
            return False

    async def _read_ex(self, n: int, timeout: float) -> bytes:
        data = self._pre[:n]
        self._pre = self._pre[n:]
        while len(data) < n:
            chunk = await asyncio.wait_for(self._reader.readexactly(n - len(data)),
                                           timeout)
            data += chunk
        return data

    async def _read_message(self):
        header = await self._reader.readexactly(4)
        (n,) = struct.unpack(">I", header)
        if n == 0:
            return None                      # keep-alive
        if n > MAX_MSG:
            raise PeerDied("сообщение слишком большое")
        payload = await self._reader.readexactly(n)
        return payload[0], payload[1:]

    # ---------- жизненный цикл ----------

    async def run(self):
        try:
            await self._handshake()
            self._post_handshake()
            await self._main_loop()
        except (PeerDied, asyncio.IncompleteReadError, ConnectionError, OSError,
                asyncio.TimeoutError, struct.error):
            pass
        except Exception:
            if not self.dead:
                import traceback
                traceback.print_exc()
        finally:
            self.dead = True
            if self._writer is not None:
                try:
                    self._writer.close()
                except Exception:
                    pass
            self.torrent.peer_closed(self)

    def _plain_handshake(self) -> bytes:
        t = self.torrent
        return (bytes([19]) + PROTO_NAME + RESERVED + t.info_hash
                + t.engine.peer_id)

    async def _handshake(self):
        if self.outgoing:
            await self._outgoing_handshake()
        else:
            await self._incoming_handshake()

    async def _outgoing_handshake(self):
        """Исходящее: сначала MSE (если включён), при неудаче — plain."""
        t = self.torrent
        modes = ["mse", "plain"] if t.engine.settings.encrypt else ["plain"]
        last_exc = None
        for i, mode in enumerate(modes):
            if self._writer is not None:
                try:
                    self._writer.close()
                except Exception:
                    pass
                self._writer = None
                self._reader = None
            try:
                self._reader, self._writer = await asyncio.wait_for(
                    asyncio.open_connection(self.addr[0], self.addr[1]), 10)
            except (ConnectionError, OSError, asyncio.TimeoutError) as e:
                raise PeerDied(f"нет соединения: {e}")
            ours = self._plain_handshake()
            try:
                if mode == "mse":
                    res = await mse_mod.initiate(self._reader, self._writer,
                                                 t.info_hash, ours)
                    if res["mode"] == "rc4":
                        self._apply_rc4(res["rc4"], res["pending"])
                    else:
                        self._pre = res["pending"]
                    await self._verify_remote_handshake()
                    return
                self._send(ours)
                await self._verify_remote_handshake()
                return
            except Exception as e:
                last_exc = e
                if mode == "mse" and i + 1 < len(modes):
                    continue    # повторяем соединение обычным хендшейком
                raise

    def _apply_rc4(self, rc4_pair, pending: bytes):
        enc, dec = rc4_pair
        self._writer = _CryptWriter(self._writer, enc)
        self._reader = _CryptReader(self._reader, dec, pending)

    async def _verify_remote_handshake(self):
        head = await self._read_ex(68, 15)
        if head[0] != 19 or head[1:20] != PROTO_NAME:
            raise PeerDied("не BitTorrent-протокол")
        if head[28:48] != self.torrent.info_hash:
            raise PeerDied("не тот info_hash")
        self.supported = True

    async def _incoming_handshake(self):
        if self._mse_result is not None:
            res = self._mse_result
            ia = res.get("remote_ia", b"")
            if len(ia) < 68 or ia[0] != 19 or ia[1:20] != PROTO_NAME \
                    or ia[28:48] != self.torrent.info_hash:
                raise PeerDied("битый IA в MSE")
            if res["mode"] == "rc4":
                self._apply_rc4(res["rc4"], res.get("pending", b""))
            else:
                self._pre = res.get("pending", b"")
            self._send(self._plain_handshake())
        elif self._initial_head is not None:
            head = self._initial_head
            if head[0] != 19 or head[1:20] != PROTO_NAME:
                raise PeerDied("не BitTorrent-протокол")
            if head[28:48] != self.torrent.info_hash:
                raise PeerDied("не тот info_hash")
            self._send(self._plain_handshake())
        else:
            raise PeerDied("входящее без хендшейка")
        self.supported = True

    def _post_handshake(self):
        t = self.torrent
        ext_hs = {
            b"m": {b"ut_metadata": 1},
            b"v": b"PureTorrent 1.0",
            b"p": t.engine.listen_port,
            b"reqq": 250,
        }
        if t.meta is not None:
            ext_hs[b"metadata_size"] = len(t.meta.info_raw)
        self._send_msg(MSG_EXTENDED, bytes([0]) + bcode.encode(ext_hs))
        if t.picker is not None and t.picker.num_have() > 0:
            self._send_msg(MSG_BITFIELD, bytes(t.picker.have))

    async def _main_loop(self):
        while not self.dead and not self.torrent.stopping:
            try:
                msg = await asyncio.wait_for(self._read_message(), KEEPALIVE_IDLE)
            except asyncio.TimeoutError:
                now = time.monotonic()
                if now - self._last_rx > READ_TIMEOUT:
                    break
                if now - self._last_tx > KEEPALIVE_IDLE:
                    try:
                        self._send(struct.pack(">I", 0))
                    except PeerDied:
                        break
                continue
            self._last_rx = time.monotonic()
            if msg is None:
                continue
            mid, payload = msg
            await self._dispatch(mid, payload)
            self._update_rates()
            self._drive()

    def _drive(self):
        """Выразить интерес и запросить блоки, если нас разчокали."""
        if self.dead or self.bitfield is None or not self._send_ok():
            return
        pf = self.torrent.picker
        if pf is None:
            return
        try:
            if pf.wants(self.bitfield):
                if not self.we_interested:
                    self.we_interested = True
                    self._send_msg(MSG_INTERESTED)
                if not self.choking_us:
                    while self.outstanding < PIPELINE:
                        reqs = pf.pick(self.key, self.bitfield, 1)
                        if not reqs:
                            break
                        p, begin, length = reqs[0]
                        self._send_msg(MSG_REQUEST, struct.pack(">III", p, begin, length))
                        self.outstanding += 1
            elif self.we_interested:
                self.we_interested = False
                self._send_msg(MSG_NOT_INTERESTED)
        except PeerDied:
            pass

    async def _dispatch(self, mid: int, payload: bytes):
        t = self.torrent
        if mid == MSG_CHOKE:
            self.choking_us = True
        elif mid == MSG_UNCHOKE:
            self.choking_us = False
        elif mid == MSG_INTERESTED:
            self.interested_in_us = True
        elif mid == MSG_NOT_INTERESTED:
            self.interested_in_us = False
        elif mid == MSG_HAVE:
            if len(payload) != 4:
                raise PeerDied("плохой have")
            (i,) = struct.unpack(">I", payload)
            if i >= t.num_pieces_expected:
                raise PeerDied("have за пределами")
            if self.bitfield is not None and not bit_has(self.bitfield, i):
                bit_or(self.bitfield, i)
                if t.picker is not None and len(self.bitfield) == len(t.picker.have):
                    t.picker.peer_set_bit(i)
        elif mid == MSG_BITFIELD:
            if self.bitfield is not None:
                raise PeerDied("двойной bitfield")
            if t.meta is not None:
                n = t.meta.num_pieces
                if len(payload) != (n + 7) // 8:
                    raise PeerDied("битая длина bitfield")
                for i in range(n, len(payload) * 8):
                    if payload[i >> 3] & (0x80 >> (i & 7)):
                        raise PeerDied("мусорные биты в bitfield")
            elif len(payload) > 65536:
                raise PeerDied("bitfield слишком большой")
            self.bitfield = bytearray(payload)
            if t.picker is not None and len(self.bitfield) == len(t.picker.have):
                t.picker.peer_has(self.bitfield)
        elif mid == MSG_REQUEST:
            await t.serve_request(self, payload)
        elif mid == MSG_PIECE:
            if len(payload) < 9:
                raise PeerDied("плохой piece")
            p, begin = struct.unpack_from(">II", payload)
            data = payload[8:]
            self.outstanding = max(0, self.outstanding - 1)
            self.downloaded += len(data)
            await t.got_block(self, p, begin, data)
        elif mid == MSG_CANCEL:
            pass
        elif mid == MSG_PORT:
            pass
        elif mid == MSG_EXTENDED:
            await self._extended(payload)
        # неизвестные id игнорируем

    # ---------- расширения ----------

    async def _extended(self, payload: bytes):
        if not payload:
            return
        eid = payload[0]
        data = payload[1:]
        if eid == 0:
            try:
                d, _ = bcode.decode_prefix(data, 0)
            except bcode.BcodeError:
                return
            if not isinstance(d, dict):
                return
            m = d.get(b"m")
            if isinstance(m, dict):
                self.ext = {k: v for k, v in m.items()
                            if isinstance(k, bytes) and isinstance(v, int)}
            ms = d.get(b"metadata_size")
            if isinstance(ms, int) and 0 < ms <= 32 * 1024 * 1024:
                self.metadata_size = ms
            self.torrent.peer_extensions_ready(self)
        elif eid == self.ext.get(b"ut_metadata"):
            try:
                d, consumed = bcode.decode_prefix(data, 0)
            except bcode.BcodeError:
                return
            if not isinstance(d, dict):
                return
            msg_type = d.get(b"msg_type")
            piece = d.get(b"piece", 0)
            if msg_type == 0:                       # request
                await self._serve_metadata(piece)
            elif msg_type == 1:                     # data
                self.torrent.metadata_piece_received(
                    piece, d.get(b"total_size", 0), data[consumed:])
            # msg_type == 2 (reject) — ждём другого пира

    async def _serve_metadata(self, piece: int):
        t = self.torrent
        ut_id = self.ext.get(b"ut_metadata")
        if ut_id is None:
            return
        if t.meta is None:
            self._send_msg(MSG_EXTENDED, bytes([ut_id]) + bcode.encode(
                {b"msg_type": 2, b"piece": piece}))
            return
        info = t.meta.info_raw
        chunk = info[piece * 16384:(piece + 1) * 16384]
        d = {b"msg_type": 1, b"piece": piece, b"total_size": len(info)}
        self._send_msg(MSG_EXTENDED, bytes([ut_id]) + bcode.encode(d) + chunk)

    # ---------- отдача ----------

    def send_piece(self, p: int, begin: int, data: bytes):
        self._send_msg(MSG_PIECE, struct.pack(">II", p, begin) + data)
        self.uploaded += len(data)

    def _update_rates(self):
        now = time.monotonic()
        dt = now - self._count_at
        if dt >= 2.0:
            self.dl_rate = int((self.downloaded - self._last_count) / dt)
            self._last_count = self.downloaded
            self._count_at = now

    def __repr__(self):
        return f"<Peer {self.key}>"


def bit_has(bf: bytearray, i: int) -> bool:
    return bool(bf[i >> 3] & (0x80 >> (i & 7)))


def bit_or(bf: bytearray, i: int):
    bf[i >> 3] |= 0x80 >> (i & 7)
