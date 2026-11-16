# 移动云盘挂载验证 Demo

同一份 Python 代码通过 fusepy 接入 Windows WinFsp 和 Linux libfuse 2。平台选项在启动入口与适配层，核心层可以单独测试。实测记录见 [docs/RESULT.md](docs/RESULT.md)。

## Windows

安装 WinFsp 和 Python 3.10 或更新版本。使用空闲盘符，例如 Y:。

```powershell
python -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
.venv\Scripts\python -m compileall -q core adapter cmd mock
.venv\Scripts\python -m unittest discover -s test -v
.venv\Scripts\python test/e2e/verify_mock.py Y:
```

## Linux

安装 Python 3.10 或更新版本、venv、libfuse 2 运行库和提供 fusermount 的 FUSE 2 工具。挂载用户需要有权限访问 /dev/fuse。挂载点须为空目录。

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m compileall -q core adapter cmd mock
.venv/bin/python -m unittest discover -s test -v
mkdir -p "$HOME/ydfs-mount"
.venv/bin/python test/e2e/verify_mock.py "$HOME/ydfs-mount"
```

## 手动挂载与验证

以下命令中的 python 指虚拟环境里的解释器。先在启动模拟服务和挂载进程的终端设置相同的 YDFS_AUTH，值用临时随机串。模拟器只监听本机，文件和测试状态保存在内存中。

```text
python -m mock.server --ttl 3
python -m cmd.ydfs --base-url http://127.0.0.1:8765 --mount <挂载点>
python test/e2e/run.py <挂载路径> --suite ydfs-e2e-manual --wait-seconds 5
```

Windows 挂载点参数传 Y:，验证脚本的挂载路径传 Y:\。Ctrl+C 停止前台挂载。Linux 两处都传挂载目录，使用 fusermount -u <挂载点> 卸载。

执行 T4 前重新挂载，保持模拟服务运行，然后用相同的 --suite 执行 run.py 并指定 --case T4。verify_mock.py 会自动完成重新挂载并核对模拟服务端的证据。用例完成后给 run.py 传相同的 --suite 和 --cleanup，可清理专用测试目录。

T13 在 Windows 用资源管理器复制文件到专用目录，再用记事本编辑保存。在 Linux 用 cp 和 vim 重复。确认读回内容、服务端提交摘要和目录列表符合预期，且没有多余的重命名文件。

## 联调参数

在项目根目录准备 `ydfs.local.json` 后，Windows 上可运行一行启动挂载。

```powershell
.\.venv\Scripts\python.exe -m cmd.ydfs
```

JSON 使用 `mount`、`base_url`、`root_file_id`、`client_info`、`app_channel` 和 `authorization` 字段。也可用 `--config <路径>` 指定其他 JSON。默认文件已加入 Git 忽略规则。若省略 `authorization`，程序读取 `YDFS_AUTH`。

Windows 网络盘可设置 `"network_drive": true`，并用 `"network_name": "FDFS"` 固定共享名称，资源管理器显示为 `FDFS (\\ydfs)`。对应命令行参数为 `--network-drive --network-name FDFS`；省略名称时每次挂载生成随机共享名。

设置 `user_domain_id` 后，挂载盘通过 `getPersonalDiskInfo` 获取云盘总容量和可用容量，按 MiB 转换后向操作系统报告。容量缓存 60 秒。

使用命令行参数启动时，真实环境的挂载必须使用 HTTPS，传入 --root-file-id 指定已建好的专用测试目录 ID，并用 --client-info、--app-channel 设置实际客户端标识。可以先用 --read-only 挂载。首次真实写入或删除前须确认操作；测试脚本默认在挂载根下创建独立的 ydfs-e2e-* 目录。

--hash-algo 对云空间使用 sha256，对 PDS 使用 sha1。默认缓存参数是 5 秒元数据 TTL、4 MiB 读块和 10 MiB 分片，可通过 --meta-ttl、--block-size、--part-size 调整。--cache-dir 覆盖暂存目录。
