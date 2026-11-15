import hashlib
import json
import time
from datetime import datetime
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener
from uuid import uuid4


MIB = 1024 * 1024


class CloudError(Exception):
    def __init__(self, code, operation, tid=None):
        self.code = str(code)
        super().__init__(f"{operation}: code={code}, tid={tid or '-'}")


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


class Client:
    def __init__(self, base_url, auth, client_info, app_channel, algorithm="sha256",
                 part_size=10 * MIB, page_size=100):
        if not 5 * MIB <= part_size <= 5 * 1024 * MIB:
            raise ValueError("part size must be between 5 MiB and 5 GiB")
        self.base_url = base_url.rstrip("/")
        self.headers = {
            "Authorization": auth,
            "x-yun-client-info": client_info,
            "x-yun-app-channel": app_channel,
        }
        self.algorithm = algorithm
        self.part_size = part_size
        self.page_size = page_size
        self.http = build_opener(NoRedirect())

    def transfer(self, request, operation, tid=None):
        try:
            with self.http.open(request, timeout=60) as response:
                return response.status, response.headers, response.read()
        except HTTPError as error:
            error.close()
            raise CloudError(error.code, operation, tid) from None
        except (URLError, OSError, ValueError):
            raise CloudError("transport", operation, tid) from None

    def post(self, path, **body):
        tid = uuid4().hex
        headers = {
            **self.headers,
            "Content-Type": "application/json",
            "x-yun-tid": tid,
            "x-yun-api-version": "v2" if path == "/task/get" else "v1",
        }
        if path in {"/file/create", "/file/getUploadUrl", "/file/complete"}:
            headers["x-yun-op-type"] = "1"
        request = Request(self.base_url + path, json.dumps(body).encode(), headers)
        _, _, data = self.transfer(request, path, tid)
        try:
            reply = json.loads(data)
            if str(reply["code"]) != "0000" or not reply["success"]:
                raise CloudError(reply["code"], path, tid)
            return reply.get("data")
        except (ValueError, KeyError, TypeError):
            raise CloudError("invalid-response", path, tid) from None

    def list(self, parent):
        items, seen = [], set()
        cursor = ""
        while True:
            data = self.post("/file/list", parentFileId=parent,
                             pageInfo=dict(pageSize=self.page_size, pageCursor=cursor))
            items.extend(data["items"])
            cursor = data.get("nextPageCursor")
            if not cursor:
                return items
            if cursor in seen:
                raise CloudError("repeated-cursor", "/file/list")
            seen.add(cursor)

    def mkdir(self, parent, name):
        response = self.post("/file/create", parentFileId=parent, name=name,
                             type="folder", fileRenameMode="refuse")
        if response.get("exist"):
            raise CloudError("04010330", "/file/create")
        return {**response, "name": response["fileName"], "size": 0}

    def rename(self, file_id, name):
        return self.post("/file/update", fileId=file_id, name=name, fileRenameMode="refuse")

    def batch_result(self, data, file_id, operation):
        result = next((row for row in data.get("batchFileResults", [])
                       if row.get("fileId") == file_id), None)
        if result is None:
            raise CloudError("missing-batch-result", operation)
        if result.get("errCode") not in (None, "", "0000"):
            raise CloudError(result["errCode"], operation)

    def move(self, file_id, parent):
        path = "/file/batchMoveSync"
        self.batch_result(self.post(path, fileIds=[file_id], toParentFileId=parent), file_id, path)

    def trash(self, file_id):
        path = "/recyclebin/batchTrash"
        data = self.post(path, fileIds=[file_id])
        task = data.get("taskId")
        if task:
            deadline = time.monotonic() + 30
            path = "/task/get"
            while True:
                data = self.post(path, taskId=task)
                status = data["taskInfo"]["status"]
                if status in {"Succeed", "PartialSucceed"}:
                    break
                if status != "Running" or time.monotonic() >= deadline:
                    raise CloudError(status, path)
                time.sleep(0.25)
        self.batch_result(data, file_id, path)

    def download_url(self, file_id):
        data = self.post("/file/getDownloadUrl", fileId=file_id)
        return data["url"], datetime.fromisoformat(data["expiration"].replace("Z", "+00:00")).timestamp()

    def personal_disk_space(self, user_domain_id):
        data = self.post("/getPersonalDiskInfo", userDomainId=user_domain_id)
        try:
            total = int(data["diskSize"]) * MIB
            free = int(data["freeDiskSize"]) * MIB
            if not 0 <= free <= total:
                raise ValueError
        except (KeyError, TypeError, ValueError):
            raise CloudError("invalid-response", "/getPersonalDiskInfo") from None
        return total, free

    def read_range(self, url, start, end, size):
        request = Request(url, headers={"Range": f"bytes={start}-{end}"})
        status, headers, data = self.transfer(request, "download")
        if (status != 206 or len(data) != end - start + 1
                or headers.get("Content-Range") != f"bytes {start}-{end}/{size}"):
            raise CloudError("invalid-range", "download")
        return data

    def upload(self, parent, name, stream, file_id=None):
        digest = hashlib.new(self.algorithm)
        stream.seek(0)
        for chunk in iter(lambda: stream.read(MIB), b""):
            digest.update(chunk)
        size = stream.tell()
        stream.seek(0)
        count = max(1, (size + self.part_size - 1) // self.part_size)
        if count > 10000:
            raise CloudError("too-many-parts", "/file/create")
        parts = [dict(partNumber=i + 1, partSize=min(self.part_size, size - i * self.part_size))
                 for i in range(count)]
        hashes = dict(contentHash=digest.hexdigest(), contentHashAlgorithm=self.algorithm)
        body = dict(parentFileId=parent, name=name, type="file", size=size,
                    fileRenameMode="refuse", partInfos=parts[:100], **hashes)
        if file_id:
            body["fileId"] = file_id
        created = self.post("/file/create", **body)
        if created.get("exist"):
            raise CloudError("04010330", "/file/create")
        if created.get("rapidUpload"):
            return {**created, "name": created["fileName"], "size": size,
                    "parentFileId": parent, "type": "file"}
        key = dict(fileId=created["fileId"], uploadId=created["uploadId"])

        def urls(specs):
            data = self.post("/file/getUploadUrl", **key, partInfos=specs)
            return {part["partNumber"]: part["uploadUrl"] for part in data["partInfos"]}

        addresses = {part["partNumber"]: part["uploadUrl"]
                     for part in (created.get("partInfos") or [])}
        for part in parts:
            number = part["partNumber"]
            if number not in addresses:
                addresses = urls(parts[number - 1:number + 99])
            data = stream.read(part["partSize"])
            for attempt in range(2):
                request = Request(addresses[number], data, method="PUT",
                                  headers={"Content-Type": "application/octet-stream"})
                try:
                    self.transfer(request, "upload-part")
                    break
                except CloudError as error:
                    if attempt or error.code != "403":
                        raise
                    addresses = urls([part])
        for delay in (0, 1, 3, 5):
            time.sleep(delay)
            try:
                return self.post("/file/complete", **key, **hashes)
            except CloudError as error:
                if error.code != "04010322" or delay == 5:
                    raise
