# 移动云盘挂载验证 Demo

同一份 Python 核心代码接入 Linux FUSE 3 和 Windows WinFsp FUSE 3。Linux 使用 mfusepy 3.1.1；Windows 使用本项目的 ctypes 绑定，直接调用 WinFsp 的 `fuse3_main_real`，不依赖 fusepy。实测记录见 [docs/RESULT.md](docs/RESULT.md)，Windows 应用兼容性需要的服务端特性见 [docs/SERVER_REQUIREMENTS.md](docs/SERVER_REQUIREMENTS.md)。

## Windows

安装提供 FUSE 3.2 FFI 导出的 WinFsp 和 64 位 Python 3.10 或更新版本。本次实测 WinFsp 2.1.25156 + x64 Python 3.14.3；ARM64 DLL 路径已支持但尚未实测，32 位 Python 会明确报错。使用空闲盘符，例如 Y:。

```powershell
python -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
.venv\Scripts\python -m cmd.ydfs --version
.venv\Scripts\python -m compileall -q core adapter cmd mock
.venv\Scripts\python -m unittest discover -s test -v
.venv\Scripts\python test/e2e/verify_mock.py Y:
```

`--version` 应显示 `ctypes / WinFsp FUSE 3.2 (fuse3_main_real)`。WinFsp 产品版本（如 2.1）与 FUSE 接口版本（3.2）是不同的版本号。Windows 没有第三方 Python 包依赖；绑定按注册表定位匹配 Python 架构的 WinFsp DLL，也支持 `FUSE_LIBRARY_PATH`。缺少 FUSE 3 导出或接口版本不匹配时启动失败，不回退到 FUSE 2。

Windows 验证脚本同时检查 Unicode/二进制读写、共享读写/删除冲突、字节范围锁、普通覆盖改名规则、ReplaceFileW、句柄截断/刷新和打开后删除。网络盘可用 `python test/e2e/verify_mock.py <空闲盘符> --network-drive` 验证。这些是 Win32 自动测试，Word/Excel 的实际操作仍需单独验收。

## Linux

安装 Python 3.10 或更新版本、venv、libfuse 3 运行库和提供 fusermount3 的 FUSE 3 工具。Ubuntu 24.04 可安装 `python3-venv libfuse3-3 fuse3`。挂载用户需要有权限访问 /dev/fuse。挂载点须为空目录。

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m cmd.ydfs --version
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

Windows 挂载点参数传 Y:，验证脚本的挂载路径传 Y:\。Ctrl+C 停止前台挂载。Linux 两处都传挂载目录，使用 `fusermount3 -u <挂载点>` 卸载。

`python -m cmd.ydfs --version` 显示当前实际加载的后端。Linux 默认查找 `fuse3`；可用 `FUSE_LIBRARY_PATH` 指定库路径，但若实际加载的是 FUSE 2，启动会报错，不会自动回退。只升级系统运行库、继续使用原来的 fusepy 不能完成迁移。

Linux 验证脚本还会检查 FUSE 3 的打开句柄截断、fstat/fsync、覆盖已打开的目标文件、打开后删除，以及 renameat2 标志保护。当前普通 rename 保持原有实现；`RENAME_NOREPLACE`、`RENAME_EXCHANGE`、`RENAME_WHITEOUT` 尚未实现，返回不支持（内核也可能提前拒绝），不会忽略标志执行覆盖。跨客户端的原子替换仍需要服务端支持。

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

顺序写入暂存文件时同步累积完整摘要，上传前无需再扫描一遍大文件；覆盖和截断会使相关摘要失效，提交前自动补算。优化与本地基准见 [docs/UPLOAD_PERFORMANCE.md](docs/UPLOAD_PERFORMANCE.md)。

上传已移出全局状态锁，同文件修改等待当前上传，其他文件和目录可继续访问。`[upload-perf]` 日志记录摘要补算/复用字节数、创建上传、分片读取/PUT、取址、云端完成和重试等待的起止与耗时，以及累计上传进度。文件关闭仍同步等待上传完成。

目录分页默认每页 1000 项，可用 `--page-size 1..1000`（JSON 字段 `page_size`）调整。目录缓存同时保存名称索引，属性查询无需反复扫描全部同级文件。

目录耗时日志默认输出到终端。使用 `--log-file .local/ydfs.log` 同时保存到文件（单文件 10 MiB，保留 3 个备份）；`--log-level DEBUG` 可记录每一次属性查询。JSON 配置支持 `log_file`、`log_level`。例如在现有配置中增加这两个字段后，按原命令启动挂载即可。已运行的挂载需要重启才能加载新代码。

日志以 `[directory-perf]` 标识，包含进程/调用编号、锁等待、路径查找、缓存、HTTP 等待、JSON 解码、本地覆盖、属性转换和 WinFsp 目录填充耗时。名称以摘要标识，不记录鉴权、URL 或请求/响应正文。测试方法、瓶颈分析和前后对比见 [目录打开性能报告](docs/DIRECTORY_PERFORMANCE.md)。

真实云盘的只读对比工具为 `test/perf/cloud_directory_open.py`，支持冷/热目录、缓存过期和独立网络盘测试。真实结果和仍存在的等待见 [真实云盘目录复测](docs/REAL_CLOUD_DIRECTORY_PERFORMANCE.md)。日志还会记录慢文件读取的取址、Range 下载、下载字节数和容量刷新，用于分析目录查询被其他网络操作阻塞的情况。

远端文件读取现已移到全局状态锁外执行，并保护读取期间的句柄生命周期和暂存状态；孤立小读取使用最多 64 KiB 窗口，连续读取达到 128 KiB 后恢复配置的大块读取。缓存合并并发请求，并防止失效前的旧请求回填。实现与真实云盘对比见 [读锁与文件头优化](docs/READ_PROBE_PERFORMANCE.md)。

`test/perf/native_cloud_probe.py` 可在不操作任何界面的情况下，通过独立只读 WinFsp 网络盘复测文件头读取、目录枚举和强制属性刷新；同一报告中保留了原生调用前后对比及残余等待说明。
