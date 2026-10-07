"""Прямой тест DHT-ноды: бутстрап и поиск пиров в изолированном asyncio-цикле."""
import asyncio
import os
import sys

import dht as dht_mod


async def main() -> int:
    dht = dht_mod.DHT(51950)
    orig_query = dht._query
    orig_dgram = dht._datagram_received

    async def query(addr, name, a, timeout=8):
        r = await orig_query(addr, name, a, timeout)
        if r is None:
            print(f'  [q timeout] {name} -> {addr}', flush=True)
        else:
            keys = sorted(k.decode() for k in r) if isinstance(r, dict) else r
            print(f'  [q ok] {name} -> {addr} r={keys}', flush=True)
        return r

    def dgram(data, addr):
        orig_dgram(data, addr)

    dht._query = query
    dht._datagram_received = dgram
    await dht.start()
    for i in range(6):
        await asyncio.sleep(5)
        print(f'[{(i + 1) * 5}с] нод в таблице: {dht.node_count()}', flush=True)
    # ищем пиры реального торрента
    from torrent import TorrentMeta
    m = TorrentMeta.from_bytes(open('tmp_real/ubuntu.torrent', 'rb').read())
    peers = await dht.lookup(m.info_hash)
    print(f'lookup: {len(peers)} пиров:', peers[:10], flush=True)
    await asyncio.sleep(3)
    print(f'нод после lookup: {dht.node_count()}', flush=True)
    await dht.stop()
    return 0 if dht.node_count() > 4 else 1


if __name__ == '__main__':
    if os.name == 'nt':
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    sys.exit(asyncio.run(main()))
