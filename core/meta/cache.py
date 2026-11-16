import time
from dataclasses import dataclass
from core.perf import increment, stage


@dataclass
class CachedDirectory:
    expires: float
    nodes: list
    exact: dict
    folded: dict


class DirectoryCache:
    def __init__(self, client, ttl, ignore_case):
        self.client = client
        self.ttl = ttl
        self.ignore_case = ignore_case
        self.entries = {}

    def key(self, name):
        return name.casefold() if self.ignore_case else name

    def directory(self, parent):
        cached = self.entries.get(parent)
        if cached is None or time.monotonic() >= cached.expires:
            increment("cache_miss")
            with stage("cache_fetch"):
                nodes = self.client.list(parent)
            with stage("cache_index"):
                exact, folded = {}, {}
                for node in nodes:
                    # Preserve first-match ordering, including case collisions.
                    exact.setdefault(node["name"], node)
                    if self.ignore_case:
                        folded.setdefault(self.key(node["name"]), node)
                cached = CachedDirectory(time.monotonic() + self.ttl, nodes, exact, folded)
            self.entries[parent] = cached
        else:
            increment("cache_hit")
        return cached

    def list(self, parent):
        return self.directory(parent).nodes

    def lookup(self, parent, name):
        cached = self.directory(parent)
        with stage("name_lookup"):
            node = cached.exact.get(name)
            if node is None and self.ignore_case:
                node = cached.folded.get(self.key(name))
            return node

    def invalidate(self, *parents):
        for parent in parents:
            self.entries.pop(parent, None)
