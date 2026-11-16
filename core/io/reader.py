import time
from collections import OrderedDict
from concurrent.futures import Future
from threading import RLock

from core.api.client import CloudError
from core.perf import increment, stage


class Reader:
    """Concurrent, revision-aware ranges. Network I/O never holds cache_lock.

    Probes use at most 64 KiB windows. Contiguous reads promote back to the
    configured block size after 128 KiB, preserving bulk-read throughput.
    """

    def __init__(self, client, block_size):
        self.client = client
        self.block_size = block_size
        self.probe_size = min(64 * 1024, block_size)
        self.blocks = OrderedDict()
        self.urls = {}
        self.patterns = OrderedDict()
        self.generations = {}
        self.pending = {}
        self.pending_urls = {}
        self.cache_lock = RLock()
        self.cache_bytes = 0

    @staticmethod
    def identity(node):
        revision = node.get("revisionId") or node.get("contentHash") or node.get("updatedAt")
        return node["fileId"], revision

    def invalidate(self, file_id):
        with self.cache_lock:
            self.generations[file_id] = self.generations.get(file_id, 0) + 1
            for key in list(self.blocks):
                if key[0] == file_id:
                    self.cache_bytes -= len(self.blocks.pop(key))
            for mapping in (self.urls, self.patterns):
                for key in list(mapping):
                    if key[0] == file_id:
                        mapping.pop(key)
            # In-flight reads keep their snapshot; the generation prevents
            # publishing stale data or sharing it with post-invalidation reads.

    def ticket(self, identity, generation):
        pending_key = (*identity, generation)
        with self.cache_lock:
            cached = self.urls.get(identity)
            if cached is not None and time.time() < cached[1]:
                return cached
            pending = self.pending_urls.get(pending_key)
            owner = pending is None
            if owner:
                pending = self.pending_urls[pending_key] = Future()
        if not owner:
            with stage("url_wait"):
                return pending.result()
        try:
            increment("url_requests")
            with stage("download_url"):
                url, expires = self.client.download_url(identity[0])
            lifetime = max(0, expires - time.time())
            cached = (url, expires - min(60, lifetime / 4))
            with self.cache_lock:
                if self.generations.get(identity[0], 0) == generation:
                    self.urls[identity] = cached
            pending.set_result(cached)
            return cached
        except BaseException as error:
            pending.set_exception(error)
            raise
        finally:
            with self.cache_lock:
                self.pending_urls.pop(pending_key, None)

    def block(self, node, index, unit=None):
        unit = self.block_size if unit is None else unit
        identity = self.identity(node)
        key = (*identity, unit, index)
        start = index * unit
        with self.cache_lock:
            # A header probe can reuse bytes already fetched by a bulk read.
            large_key = (*identity, self.block_size, start // self.block_size)
            covered = min(start + unit, node["size"]) <= (large_key[3] + 1) * self.block_size
            cached_key = key if key in self.blocks else large_key if covered else None
            if cached_key is not None and cached_key in self.blocks:
                increment("block_cache_hit")
                self.blocks.move_to_end(cached_key)
                data = self.blocks[cached_key]
                skip = start - cached_key[3] * cached_key[2]
                return data[skip:skip + unit]
            generation = self.generations.get(identity[0], 0)
            pending_key = (*key, generation)
            pending = self.pending.get(pending_key)
            owner = pending is None
            if owner:
                pending = self.pending[pending_key] = Future()
        if not owner:
            increment("block_shared_wait")
            with stage("block_wait"):
                return pending.result()
        increment("block_cache_miss")
        end = min(start + unit, node["size"]) - 1
        try:
            for attempt in range(2):
                ticket = self.ticket(identity, generation)
                try:
                    increment("range_requests")
                    with stage("range_read"):
                        data = self.client.read_range(ticket[0], start, end, node["size"])
                    increment("downloaded_bytes", len(data))
                    break
                except CloudError as error:
                    with self.cache_lock:
                        if self.urls.get(identity) == ticket:
                            self.urls.pop(identity, None)
                    if error.code != "403" or attempt:
                        raise
            with self.cache_lock:
                if self.generations.get(identity[0], 0) == generation:
                    self.blocks[key] = data
                    self.cache_bytes += len(data)
                    while len(self.blocks) > 256 or self.cache_bytes > 16 * self.block_size:
                        _, evicted = self.blocks.popitem(last=False)
                        self.cache_bytes -= len(evicted)
            pending.set_result(data)
            return data
        except BaseException as error:
            pending.set_exception(error)
            raise
        finally:
            with self.cache_lock:
                self.pending.pop(pending_key, None)

    def read(self, node, offset, size):
        remaining = min(size, node["size"] - offset)
        if remaining <= 0:
            return b""
        identity = self.identity(node)
        with self.cache_lock:
            previous_end, run = self.patterns.get(identity, (None, 0))
            run = run + remaining if previous_end == offset else remaining
            self.patterns[identity] = (offset + remaining, run)
            self.patterns.move_to_end(identity)
            if len(self.patterns) > 256:
                self.patterns.popitem(last=False)
        unit = self.block_size if size > self.probe_size or run >= 2 * self.probe_size else self.probe_size
        increment("large_reads" if unit == self.block_size else "probe_reads")
        result = bytearray()
        while remaining > 0:
            index, skip = divmod(offset, unit)
            block = self.block(node, index, unit)
            take = min(remaining, len(block) - skip)
            if take <= 0:
                raise CloudError("short-range", "read")
            result.extend(block[skip:skip + take])
            offset += take
            remaining -= take
        return bytes(result)
