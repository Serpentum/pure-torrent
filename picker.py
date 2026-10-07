"""Выбор кусков/блоков для скачивания: редкие вперёд или последовательно.
Готовность и кандидаты хранятся битовыми картами, поиск — через int-операции."""
from __future__ import annotations

import random

BLOCK_SIZE = 16384
MAX_ACTIVE_PIECES = 24
SAMPLE_PIECES = 64


def bit_get(bf, i: int) -> bool:
    return bool(bf[i >> 3] & (0x80 >> (i & 7)))


def bit_set(bf, i: int):
    bf[i >> 3] |= 0x80 >> (i & 7)


def bit_clear(bf, i: int):
    bf[i >> 3] &= ~(0x80 >> (i & 7))


class Picker:
    def __init__(self, num_pieces: int, piece_length: int, total_size: int):
        self.num_pieces = num_pieces
        self.piece_length = piece_length
        self.total_size = total_size
        nbits = num_pieces
        self.have = bytearray((nbits + 7) // 8)
        # _want_bits: 1 = кусок ещё не скачан (кандидаты)
        self._want_bits = bytearray((nbits + 7) // 8)
        for i in range(nbits):
            bit_set(self._want_bits, i)
        self._total_bits = len(self.have) * 8
        self._have_count = 0
        self._have_bytes = 0
        self.avail = [0] * num_pieces
        self.blocks_per = self._blocks_table()
        self._done: dict[int, set[int]] = {}        # piece -> полученные блоки
        self._req: dict[int, dict[int, set]] = {}   # piece -> блок -> {ключи пиров}
        self._sequential = False
        # маска нужных кусков (None = все): куски, целиком лежащие
        # в исключённых файлах, не качаются и не входят в прогресс
        self._wanted: bytes | None = None
        self._wanted_count = num_pieces

    def _blocks_table(self):
        out = []
        for p in range(self.num_pieces):
            size = self.piece_size(p)
            out.append(max(1, (size + BLOCK_SIZE - 1) // BLOCK_SIZE))
        return out

    # ---------- состояние ----------

    @property
    def sequential(self):
        return self._sequential

    @sequential.setter
    def sequential(self, on: bool):
        self._sequential = bool(on)

    def set_wanted(self, mask):
        """Маска нужных кусков (bitfield, 1 = качаем). Звать до set_bitfield."""
        self._wanted = bytes(mask) if mask is not None else None
        self._wanted_count = 0
        for i in range(self.num_pieces):
            if self.wanted_piece(i):
                self._wanted_count += 1
        if self._wanted is not None:
            w = int.from_bytes(self._want_bits, "big") & int.from_bytes(self._wanted, "big")
            self._want_bits = bytearray(w.to_bytes(len(self.have), "big"))

    def wanted_piece(self, p: int) -> bool:
        return self._wanted is None or (0 <= p < self.num_pieces
                                        and bit_get(self._wanted, p))

    def num_have(self) -> int:
        return self._have_count

    def have_piece(self, i: int) -> bool:
        return 0 <= i < self.num_pieces and bit_get(self.have, i)

    def is_complete(self) -> bool:
        return self._have_count >= self._wanted_count

    def bytes_done(self) -> int:
        total = self._have_bytes
        for p, blocks in self._done.items():
            for b in blocks:
                total += self.block_size(p, b)
        return total

    def piece_size(self, p: int) -> int:
        if not (0 <= p < self.num_pieces):
            return 0
        return max(0, min(self.piece_length, self.total_size - p * self.piece_length))

    def block_size(self, p: int, b: int) -> int:
        size = self.piece_size(p)
        if size <= 0:
            return 0
        return min(BLOCK_SIZE, size - b * BLOCK_SIZE)

    def set_have(self, p: int):
        if self.have_piece(p):
            return
        bit_set(self.have, p)
        bit_clear(self._want_bits, p)
        if self.wanted_piece(p):
            self._have_count += 1
            self._have_bytes += self.piece_size(p)
        self._done.pop(p, None)
        self._req.pop(p, None)

    def reset_piece(self, p: int):
        if self.have_piece(p):
            bit_clear(self.have, p)
            if self.wanted_piece(p):
                self._have_count -= 1
                self._have_bytes -= self.piece_size(p)
        bit_set(self._want_bits, p)
        self._done.pop(p, None)
        self._req.pop(p, None)

    def set_bitfield(self, bf):
        """Задать готовность целиком (после проверки диска)."""
        self.have = bytearray(bf)
        self._want_bits = bytearray(len(self.have))
        self._have_count = 0
        self._have_bytes = 0
        for i in range(self.num_pieces):
            if bit_get(self.have, i):
                if self.wanted_piece(i):
                    self._have_count += 1
                    self._have_bytes += self.piece_size(i)
            else:
                bit_set(self._want_bits, i)
        if self._wanted is not None:
            w = int.from_bytes(self._want_bits, "big") & int.from_bytes(self._wanted, "big")
            self._want_bits = bytearray(w.to_bytes(len(self.have), "big"))
        self._done.clear()
        self._req.clear()

    # ---------- доступность ----------

    def peer_has(self, bf):
        if bf is None or len(bf) != len(self.have):
            return
        for i in range(self.num_pieces):
            if bit_get(bf, i):
                self.avail[i] += 1

    def peer_gone(self, bf):
        if bf is None or len(bf) != len(self.have):
            return
        for i in range(self.num_pieces):
            if bit_get(bf, i):
                self.avail[i] = max(0, self.avail[i] - 1)

    def peer_set_bit(self, i: int):
        if 0 <= i < self.num_pieces:
            self.avail[i] += 1

    def wants(self, bf) -> bool:
        """Нужен ли нам хоть один кусок, который есть у пира."""
        if bf is None or len(bf) != len(self.have) or self.is_complete():
            return False
        return (int.from_bytes(self._want_bits, "big") &
                int.from_bytes(bytes(bf), "big")) != 0

    # ---------- выбор блоков ----------

    def _sample_indices(self, mask: int, count: int, from_msb: bool):
        """Индексы кусков по битовой маске int (не активные, не скачанные)."""
        out = []
        while mask and len(out) < count:
            if from_msb:
                mask_bit = 1 << (mask.bit_length() - 1)
            else:
                mask_bit = mask & -mask
            mask ^= mask_bit
            out.append(self._total_bits - mask_bit.bit_length())
        return out

    def _free_block(self, p: int):
        done = self._done.get(p)
        if done is None:
            return 0 if self.blocks_per[p] > 0 else None
        req = self._req.get(p, {})
        for b in range(self.blocks_per[p]):
            if b not in done and b not in req:
                return b
        return None

    def _pick_active(self):
        """Активный кусок со свободным блоком, самый редкий."""
        best = None
        best_key = None
        for p in self._done:
            if self._free_block(p) is None:
                continue
            key = (self.avail[p], random.random())
            if best_key is None or key < best_key:
                best_key = key
                best = p
        return best

    def _pick_new(self, cand: int):
        """Новый кусок из кандидатов (ещё не активный), редкий вперёд."""
        if not cand:
            return None
        if self._sequential:
            for idx in self._sample_indices(cand, 8, from_msb=True):
                if idx not in self._done:
                    return idx
            return None
        best, best_key = None, None
        for idx in self._sample_indices(cand, SAMPLE_PIECES, from_msb=False):
            if idx in self._done:
                continue
            key = (self.avail[idx], random.random())
            if best_key is None or key < best_key:
                best_key, best = key, idx
            if len(self._done) >= MAX_ACTIVE_PIECES:
                break
        return best

    def _endgame_block(self, p: int, peer_key):
        done = self._done.get(p, set())
        req = self._req.get(p, {})
        for b in range(self.blocks_per[p]):
            if b not in done and peer_key not in req.get(b, set()):
                return b
        return None

    def pick(self, peer_key, bf, count: int = 1):
        """→ [(piece, begin, length)]; блоки помечаются запрошенными этим пиром."""
        if bf is None or len(bf) != len(self.have) or self.is_complete():
            return []
        cand = int.from_bytes(self._want_bits, "big") & int.from_bytes(bytes(bf), "big")
        out = []
        while len(out) < count:
            p = self._pick_active()
            if p is None:
                if len(self._done) >= MAX_ACTIVE_PIECES:
                    break
                p = self._pick_new(cand)
                if p is None:
                    break
                self._done.setdefault(p, set())
            b = self._free_block(p)
            if b is None:
                b = self._endgame_block(p, peer_key)
                if b is None:
                    break
            self._req.setdefault(p, {}).setdefault(b, set()).add(peer_key)
            out.append((p, b * BLOCK_SIZE, self.block_size(p, b)))
        return out

    # ---------- получение блоков ----------

    def got_block(self, p: int, begin: int) -> bool:
        """Отметить блок полученным; True — кусок собран."""
        b = begin // BLOCK_SIZE
        if not (0 <= b < self.blocks_per[p]):
            return False
        self._done.setdefault(p, set()).add(b)
        self._req.get(p, {}).pop(b, None)
        return len(self._done[p]) >= self.blocks_per[p]

    def has_block(self, p: int, begin: int) -> bool:
        return begin // BLOCK_SIZE in self._done.get(p, set())

    def release(self, peer_key):
        """Снять все запросы пира (при отключении)."""
        for p in list(self._req.keys()):
            for b in list(self._req[p].keys()):
                s = self._req[p][b]
                s.discard(peer_key)
                if not s:
                    self._req[p].pop(b, None)
            if not self._req[p]:
                self._req.pop(p, None)

    def requesters(self, p: int, begin: int):
        return set(self._req.get(p, {}).get(begin // BLOCK_SIZE, set()))
