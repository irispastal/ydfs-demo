import hashlib
import json
import time
from datetime import datetime
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener
from uuid import uuid4
from core.io.staging import StagedFile
from core.perf import emit, increment, stage, timed_stage, traced


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
                 part_size=10 * MIB, page_size=1000):
        if not 5 * MIB <= part_size <= 5 * 1024 * MIB:
            raise ValueError("part size must be between 5 MiB and 5 GiB")
        if type(page_size) is not int or not 1 <= page_size <= 1000:
            raise ValueError("page size must be between 1 and 1000")
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
        with stage("http_wait"):
            _, _, data = self.transfer(request, path, tid)
        try:
            with stage("json_decode"):
                reply = json.loads(data)
            if str(reply["code"]) != "0000" or not reply["success"]:
                raise CloudError(reply["code"], path, tid)
            return reply.get("data")
        except (ValueError, KeyError, TypeError):
            raise CloudError("invalid-response", path, tid) from None

    @traced("api_list")
    def list(self, parent):
        items, seen, seen_items = [], set(), set()
        cursor = ""
        while True:
            start = time.perf_counter()
            data = self.post("/file/list", parentFileId=parent,
                             pageInfo=dict(pageSize=self.page_size, pageCursor=cursor))
            increment("http_pages")
            increment("http_items", len(data["items"]))
            emit("list_page", page_size=self.page_size, items=len(data["items"]),
                 has_more=bool(data.get("nextPageCursor")),
                 elapsed_ms=round((time.perf_counter() - start) * 1000, 3))
            # Concurrent commits can shift page boundaries. Keep each stable
            # file ID once, without treating an overlapping page as the end.
            for item in data["items"]:
                file_id = item.get("fileId")
                if file_id is not None and file_id in seen_items:
                    increment("duplicate_items")
                    continue
                items.append(item)
                if file_id is not None:
                    seen_items.add(file_id)
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

    @traced("upload", prefix="upload-perf")
    def upload(self, parent, name, stream, file_id=None):
        size = stream.seek(0, 2)
        count = max(1, (size + self.part_size - 1) // self.part_size)
        if count > 10000:
            raise CloudError("too-many-parts", "/file/create")
        emit("upload_start", size_bytes=size, total_parts=count, algorithm=self.algorithm)
        tracked = isinstance(stream, StagedFile)
        reused = stream.hashed_bytes if tracked and stream.hash_algorithm == self.algorithm else 0
        with timed_stage("upload_hash", size_bytes=size, reused_bytes=reused,
                         pending_bytes=size - reused, algorithm=self.algorithm):
            increment("hash_reused_bytes", reused)
            if tracked:
                content_hash = stream.content_hash(self.algorithm)
            else:
                digest = hashlib.new(self.algorithm)
                stream.seek(0)
                for chunk in iter(lambda: stream.read(MIB), b""):
                    digest.update(chunk)
                    increment("hash_read_bytes", len(chunk))
                content_hash = digest.hexdigest()
        stream.seek(0)
        parts = [dict(partNumber=i + 1, partSize=min(self.part_size, size - i * self.part_size))
                 for i in range(count)]
        hashes = dict(contentHash=content_hash, contentHashAlgorithm=self.algorithm)
        body = dict(parentFileId=parent, name=name, type="file", size=size,
                    fileRenameMode="refuse", partInfos=parts[:100], **hashes)
        if file_id:
            body["fileId"] = file_id
        with timed_stage("upload_create"):
            created = self.post("/file/create", **body)
        if created.get("exist"):
            raise CloudError("04010330", "/file/create")
        if created.get("rapidUpload"):
            emit("upload_rapid", size_bytes=size)
            return {**created, "name": created["fileName"], "size": size,
                    "parentFileId": parent, "type": "file"}
        key = dict(fileId=created["fileId"], uploadId=created["uploadId"])

        def urls(specs):
            with timed_stage("upload_urls", part_count=len(specs)):
                data = self.post("/file/getUploadUrl", **key, partInfos=specs)
            return {part["partNumber"]: part["uploadUrl"] for part in data["partInfos"]}

        addresses = {part["partNumber"]: part["uploadUrl"]
                     for part in (created.get("partInfos") or [])}
        uploaded = 0
        for part in parts:
            number = part["partNumber"]
            if number not in addresses:
                addresses = urls(parts[number - 1:number + 99])
            with timed_stage("upload_read_part", part_number=number, size_bytes=part["partSize"]):
                data = stream.read(part["partSize"])
            for attempt in range(2):
                request = Request(addresses[number], data, method="PUT",
                                  headers={"Content-Type": "application/octet-stream"})
                try:
                    increment("upload_put_attempts")
                    with timed_stage("upload_put_part", part_number=number, attempt=attempt + 1,
                                     size_bytes=len(data)):
                        self.transfer(request, "upload-part")
                    break
                except CloudError as error:
                    if attempt or error.code != "403":
                        raise
                    addresses = urls([part])
            uploaded += len(data)
            increment("uploaded_bytes", len(data))
            emit("upload_progress", completed_parts=number, total_parts=count,
                 uploaded_bytes=uploaded, size_bytes=size)
        for attempt, delay in enumerate((0, 1, 3, 5), 1):
            if delay:
                with timed_stage("upload_complete_wait", attempt=attempt, delay_seconds=delay):
                    time.sleep(delay)
            try:
                increment("upload_complete_attempts")
                with timed_stage("upload_complete", attempt=attempt):
                    return self.post("/file/complete", **key, **hashes)
            except CloudError as error:
                if error.code != "04010322" or delay == 5:
                    raise
