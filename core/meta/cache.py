import time


class DirectoryCache:
    def __init__(self, client, ttl, ignore_case):
        self.client = client
        self.ttl = ttl
        self.ignore_case = ignore_case
        self.entries = {}

    def key(self, name):
        return name.casefold() if self.ignore_case else name

    def list(self, parent):
        cached = self.entries.get(parent)
        if cached is None or time.monotonic() >= cached[0]:
            nodes = self.client.list(parent)
            cached = (time.monotonic() + self.ttl, nodes)
            self.entries[parent] = cached
        return cached[1]

    def invalidate(self, *parents):
        for parent in parents:
            self.entries.pop(parent, None)
