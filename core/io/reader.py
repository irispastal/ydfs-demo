import time
from collections import OrderedDict

from core.api.client import CloudError


class Reader:
    def __init__(self, client, block_size):
        self.client = client
        self.block_size = block_size
        self.blocks = OrderedDict()
        self.urls = {}

    def invalidate(self, file_id):
        for key in list(self.blocks):
            if key[0] == file_id:
                del self.blocks[key]
        for key in list(self.urls):
            if key[0] == file_id:
                del self.urls[key]

    def block(self, node, index):
        file_id = node["fileId"]
        revision = node.get("revisionId") or node.get("contentHash") or node.get("updatedAt")
        identity = (file_id, revision)
        key = (*identity, index)
        if key in self.blocks:
            self.blocks.move_to_end(key)
            return self.blocks[key]
        start = index * self.block_size
        size = node["size"]
        end = min(start + self.block_size, size) - 1
        for attempt in range(2):
            ticket = self.urls.get(identity)
            if ticket is None or time.time() >= ticket[1]:
                url, expires = self.client.download_url(file_id)
                lifetime = max(0, expires - time.time())
                ticket = (url, expires - min(60, lifetime / 4))
                self.urls[identity] = ticket
            try:
                data = self.client.read_range(ticket[0], start, end, size)
                break
            except CloudError as error:
                self.urls.pop(identity, None)
                if error.code != "403" or attempt:
                    raise
        self.blocks[key] = data
        if len(self.blocks) > 16:
            self.blocks.popitem(last=False)
        return data

    def read(self, node, offset, size):
        result = bytearray()
        remaining = min(size, node["size"] - offset)
        while remaining > 0:
            index, skip = divmod(offset, self.block_size)
            block = self.block(node, index)
            take = min(remaining, len(block) - skip)
            result.extend(block[skip:skip + take])
            offset += take
            remaining -= take
        return bytes(result)
