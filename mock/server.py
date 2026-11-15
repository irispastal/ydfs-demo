import hashlib
import json
import os
import re
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from uuid import uuid4

MIB = 1024 * 1024


class Rejected(Exception):
    pass


def require(condition, code="04000001"):
    if not condition:
        raise Rejected(code)


def timestamp(epoch=None):
    return datetime.fromtimestamp(
        time.time() if epoch is None else epoch, timezone.utc
    ).isoformat()


class Storage:
    def __init__(self, ttl, algorithm):
        self.ttl = ttl
        self.algorithm = algorithm
        self.files = {}
        self.trash = {}
        self.uploads = {}
        self.objects = {}
        self.tickets = {}
        self.tasks = {}
        self.events = []

    def file(self, file_id):
        require(file_id in self.files, "04010300")
        return self.files[file_id]

    def directory(self, file_id):
        if file_id != "/":
            require(self.file(file_id)["type"] == "folder")

    def children(self, parent):
        return [node for node in self.files.values() if node["parentFileId"] == parent]

    def name(self, parent, name, limit, exclude=None):
        require(name and len(name.encode()) <= limit and not re.search(r'[\\/:*?"<>|\x00]', name))
        require(not any(
            node["name"].casefold() == name.casefold() and node["fileId"] != exclude
            for node in self.children(parent)
        ), "04010330")

    def ticket(self, origin, mode, resource):
        token = uuid4().hex
        expiration = time.time() + self.ttl
        self.tickets[token] = (mode, expiration, resource)
        return origin + "/object/" + token, timestamp(expiration)

    def parts(self, specs, upload_id, origin):
        upload = self.uploads[upload_id]
        require(1 <= len(specs) <= 100)
        numbers = [spec["partNumber"] for spec in specs]
        require(len(set(numbers)) == len(numbers))
        result = []
        for spec in specs:
            num, size = spec["partNumber"], spec["partSize"]
            require(type(num) is int and 1 <= num <= 10000 and type(size) is int)
            require(0 <= size <= min(upload["size"], 5 * 1024 * MIB))
            require(upload["sizes"].get(num, size) == size)
            upload["sizes"][num] = size
            url, _ = self.ticket(origin, "PUT", (upload_id, num))
            result.append({"partNumber": num, "uploadUrl": url})
        return result

    def create(self, body, origin):
        parent, name, kind = (body[k] for k in ("parentFileId", "name", "type"))
        file_id = body.get("fileId") or uuid4().hex
        self.directory(parent)
        self.name(parent, name, 255, body.get("fileId"))
        require(kind in {"file", "folder"})
        node = dict(fileId=file_id, parentFileId=parent, name=name, type=kind,
                    size=body.get("size", 0), updatedAt=timestamp())
        result = {**node, "fileName": name, "exist": False, "rapidUpload": False}
        if kind == "folder":
            require("fileId" not in body)
            self.files[file_id] = node
            return result
        if "fileId" in body:
            old = self.file(file_id)
            require(old["type"] == "file" and old["parentFileId"] == parent and old["name"] == name)
        size = node["size"]
        require(type(size) is int and size >= 0)
        algorithm, digest = body["contentHashAlgorithm"].lower(), body["contentHash"]
        require(algorithm == self.algorithm and
                bool(re.fullmatch("[0-9a-fA-F]{%s}" % (hashlib.new(algorithm).digest_size * 2), digest)))
        upload_id = uuid4().hex
        self.uploads[upload_id] = dict(node=node, size=size, digest=digest.lower(),
                                       sizes={}, payloads={})
        try:
            parts = self.parts(body["partInfos"], upload_id, origin)
        except Exception:
            del self.uploads[upload_id]
            raise
        return dict(result, uploadId=upload_id, partInfos=parts)

    def upload(self, body):
        upload_id = body["uploadId"]
        require(upload_id in self.uploads, "04010300")
        upload = self.uploads[upload_id]
        require(upload["node"]["fileId"] == body["fileId"])
        return upload

    def complete(self, body):
        upload = self.upload(body)
        sizes, payloads = upload["sizes"], upload["payloads"]
        order = sorted(sizes)
        require(order == list(range(1, len(order) + 1)))
        require(all(sizes[num] >= 5 * MIB for num in order[:-1]))
        require(set(payloads) == set(sizes), "04010322")
        data = b"".join(payloads[num] for num in order)
        digest = hashlib.new(self.algorithm, data).hexdigest()
        require(len(data) == upload["size"] and digest == upload["digest"] and
                digest == body["contentHash"].lower() and
                self.algorithm == body["contentHashAlgorithm"].lower())
        node = upload["node"]
        file_id = node["fileId"]
        self.directory(node["parentFileId"])
        self.name(node["parentFileId"], node["name"], 255, file_id)
        node.update(contentHash=digest, revisionId=uuid4().hex, updatedAt=timestamp())
        self.files[file_id] = node
        self.objects[file_id] = data
        self.events.append(dict(operation="complete", fileId=file_id, revisionId=node["revisionId"], parts=[sizes[num] for num in order]))
        del self.uploads[body["uploadId"]]
        return node

    def list(self, body):
        parent = body["parentFileId"]
        self.directory(parent)
        page = body.get("pageInfo", {})
        limit = page.get("pageSize", 100)
        require(type(limit) is int and 1 <= limit <= 1000)
        cursor = page.get("pageCursor") or "0"
        require(cursor.isdecimal())
        offset = int(cursor)
        children = sorted(self.children(parent), key=lambda node: node["fileId"])
        require(offset <= len(children))
        next_cursor = str(offset + limit) if offset + limit < len(children) else ""
        self.events.append(dict(operation="list", parentFileId=parent, cursor=cursor, next=next_cursor))
        return dict(items=children[offset:offset + limit], nextPageCursor=next_cursor)

    def batch(self, body, action, limit):
        ids = body["fileIds"]
        require(isinstance(ids, list) and 1 <= len(ids) <= limit)
        results = []
        for file_id in ids:
            result = dict(fileId=file_id)
            try:
                action(self.file(file_id))
            except Rejected as error:
                result["errCode"] = str(error)
            results.append(result)
        return results

    def move(self, node, parent):
        self.directory(parent)
        ancestor = parent
        while ancestor != "/":
            require(ancestor != node["fileId"])
            ancestor = self.file(ancestor)["parentFileId"]
        self.name(parent, node["name"], 255, node["fileId"])
        node.update(parentFileId=parent, updatedAt=timestamp())

    def recycle(self, node):
        for child in self.children(node["fileId"]):
            self.recycle(child)
        file_id = node["fileId"]
        self.trash[file_id] = self.files.pop(file_id)

    def business(self, path, body, origin):
        if path == "/file/create":
            return self.create(body, origin)
        if path == "/file/list":
            return self.list(body)
        if path == "/file/getUploadUrl":
            self.upload(body)
            return dict(fileId=body["fileId"], uploadId=body["uploadId"],
                        partInfos=self.parts(body["partInfos"], body["uploadId"], origin))
        if path == "/file/complete":
            return self.complete(body)
        if path == "/file/getDownloadUrl":
            node = self.file(body["fileId"])
            require(node["type"] == "file")
            url, expiration = self.ticket(origin, "GET", (node["fileId"], self.objects[node["fileId"]]))
            self.events.append(dict(operation='download-url', fileId=node['fileId']))
            return dict(url=url, expiration=expiration, size=node["size"])
        if path == "/file/update":
            node = self.file(body["fileId"])
            self.name(node["parentFileId"], body["name"], 200, node["fileId"])
            node.update(name=body["name"], updatedAt=timestamp())
            return node
        if path == "/file/batchMoveSync":
            return dict(batchFileResults=self.batch(
                body, lambda node: self.move(node, body["toParentFileId"]), 100))
        if path == "/recyclebin/batchTrash":
            task_id = uuid4().hex
            self.tasks[task_id] = self.batch(body, self.recycle, 10000)
            return dict(taskId=task_id)
        if path == "/task/get":
            require(body["taskId"] in self.tasks, "04000010")
            return dict(taskInfo=dict(status="Succeed"), batchFileResults=self.tasks[body["taskId"]])
        raise Rejected("04000010")


class Handler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass

    def send(self, status, data=b"", headers=None):
        self.send_response(status)
        self.send_header("Content-Length", str(len(data)))
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(data)

    def json(self, data):
        self.send(200, json.dumps(data).encode(), {"Content-Type": "application/json"})

    def do_POST(self):
        try:
            require(self.server.authorization and
                    self.headers.get("Authorization") == self.server.authorization, "04000005")
            for key in ("x-yun-tid", "x-yun-client-info", "x-yun-app-channel"):
                require(self.headers.get(key))
            require(self.headers.get("x-yun-api-version") == ("v2" if self.path == "/task/get" else "v1"))
            if self.path in {"/file/create", "/file/getUploadUrl", "/file/complete"}:
                require(self.headers.get("x-yun-op-type") == "1")
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            data = self.server.storage.business(self.path, body, self.server.origin)
            self.json(dict(code="0000", message="", success=True, data=data))
        except (Rejected, KeyError, TypeError, ValueError) as error:
            code = str(error) if isinstance(error, Rejected) else "04000001"
            self.json(dict(code=code, message="request rejected", success=False, data=None))

    def resource(self, method):
        ticket = self.server.storage.tickets.get(self.path.removeprefix("/object/"))
        if (not self.path.startswith("/object/") or not ticket or ticket[0] != method
                or ticket[1] <= time.time() or "Authorization" in self.headers):
            self.send(403)
            return None
        return ticket[2]

    def do_PUT(self):
        resource = self.resource("PUT")
        if resource is None:
            return
        upload_id, num = resource
        upload = self.server.storage.uploads.get(upload_id)
        size = int(self.headers.get("Content-Length", "-1"))
        if (not upload or size != upload["sizes"][num]
                or self.headers.get_content_type() != "application/octet-stream"):
            self.send(400)
            return
        upload["payloads"][num] = self.rfile.read(size)
        self.send(200)

    def do_GET(self):
        storage = self.server.storage
        if self.path == "/_state":
            self.json(dict(files=list(storage.files.values()), trash=list(storage.trash.values()),
                           events=storage.events))
            return
        resource = self.resource("GET")
        if resource is None:
            return
        file_id, data = resource
        match = re.fullmatch(r"bytes=(\d+)-(\d+)", self.headers.get("Range", ""))
        if not match:
            self.send(400)
            return
        start, end = map(int, match.groups())
        if not 0 <= start <= end < len(data):
            self.send(416)
            return
        storage.events.append(dict(operation="range", fileId=file_id, start=start, end=end))
        self.send(206, data[start:end + 1], {"Content-Range": f"bytes {start}-{end}/{len(data)}"})


def server(port, ttl, algorithm, authorization):
    http = HTTPServer(("127.0.0.1", port), Handler)
    http.storage = Storage(ttl, algorithm)
    http.authorization = authorization
    http.origin = f"http://127.0.0.1:{http.server_port}"
    return http


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--ttl", type=float, default=900)
    parser.add_argument("--hash-algo", choices=("sha1", "sha256"), default="sha256")
    args = parser.parse_args()
    auth = os.environ.get("YDFS_AUTH")
    if not auth:
        parser.error("YDFS_AUTH is required")
    with server(args.port, args.ttl, args.hash_algo, auth) as http:
        print(http.origin, flush=True)
        http.serve_forever()
