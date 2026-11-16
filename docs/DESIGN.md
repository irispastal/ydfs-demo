# 移动云盘挂载盘 Demo 方案(Windows/Linux)

版本:2026-11-13 · 依据:《个人云SaaS接口文档 2.8.10》

实施更新（2026-11-16）：当前实现为 Python；Linux 使用 mfusepy 3.1.1 + libfuse 3，Windows 使用 `adapter/winfsp3.py` 的 ctypes 绑定调用 WinFsp FUSE 3.2 FFI。两端共用业务适配层与核心层，均不再使用 FUSE 2 接口。下文保留最初的 FUSE 2 / Go 选型方案作为背景，实际安装和运行方式以 [README](../README.md) 为准。跨端原子替换、版本条件提交、锁和打开后删除的服务端契约见 [SERVER_REQUIREMENTS.md](SERVER_REQUIREMENTS.md)。

## 1. 目标与选型

本 demo 要验证一件事:按接口文档,用一套代码把移动云盘挂载成 Windows 盘符和 Linux 目录,并能正常读写。

**通过标准**

- 同一份源码在 Linux(libfuse)和 Windows(WinFsp)上都能挂载。
- 第 7 节的验证脚本在两个系统上全部通过。
- 核心层代码中不出现任何平台判断;平台差异只出现在适配层和启动参数里。

**选型**

| 项 | 选择 | 理由 |
| --- | --- | --- |
| 文件系统接口 | FUSE 2.x 接口 | Linux 原生支持,Windows 由 WinFsp 的 FUSE 兼容层承接 |
| 语言与库(推荐) | Go + cgofuse(github.com/winfsp/cgofuse) | 在 Linux/Windows/macOS 提供同一套接口,rclone mount 用的就是它;Windows 版可不依赖 C 编译器构建 |
| 语言与库(备选) | Python + fusepy | 免编译,验证最快;fusepy 3.0.1 源码中已内置按注册表查找 WinFsp DLL 的逻辑 |
| 无联调账号时 | 自建模拟服务端 | 按文档协议实现 4.1 接口清单,先跑通流程,再切联调环境 |

两层设计与语言无关,下文接口和流程对两种选型都适用。

**不在 demo 范围内**:保险箱、同步盘、秒传(preHash/proofCode)、分片并行上传、缩略图、回收站恢复、离线缓存与断网续传、性能调优。

## 2. 接口文档对设计的约束

云盘接口是"按 fileId 操作的对象存储"模型,和文件系统"按路径、可随机写"的模型有多处不一致,核心层的设计都是为了补这些差。

| 文档事实 | 对设计的影响 |
| --- | --- |
| 所有接口按 fileId 和 parentFileId 寻址,根目录为 "/" | 核心层要维护"路径 → fileId"的目录缓存,逐级列举解析 |
| 没有随机写或追加写接口;改内容只能带 fileId 重新走三阶段上传,生成新 revisionId | 写入先落本地暂存文件,关闭时整文件上传;改已有文件要先整文件下载 |
| /file/create、/file/complete 带整文件 contentHash;PDS 底座只支持 sha1,云空间底座只支持 sha256 | 顺序写入暂存时累积完整摘要,提交前补算失效部分;算法做成配置项 |
| 分片不小于 5MB(最后一片除外);一次最多返回 100 个分片地址,其余用 /file/getUploadUrl 取;上传地址有效期 15 分钟 | 分片大小取 10MB;超过 100 片分批取地址;地址过期后重取 |
| 下载走预签名地址,支持 Range;客户端鉴权下有效期固定 900 秒 | 可按需分块读,不必整文件下载;地址要缓存并在过期前刷新 |
| 文件名禁用 `\ / : * ? " < > |` 共 9 个字符;重名检查不区分大小写 | 与 Windows 命名规则一致;Linux 端遇到这些字符要返回错误 |
| 同步彻底删除接口不支持目录;放入回收站支持目录,可能返回 taskId | 删除统一走回收站(可恢复),并支持轮询 /task/get |
| /file/list 分页返回(pageSize 取值 1–1000),可能出现游标非空但 items 为空 | 翻页以 nextPageCursor 是否为空为准 |
| 没有权限位、属主、设置修改时间的接口 | chmod、chown、utimens 直接返回成功,不落云端 |
| /drive/get 只支持三方鉴权 | 客户端鉴权下取不到容量,statfs 返回固定值 |

## 3. 总体架构

```
 Linux:内核 FUSE + libfuse          Windows:WinFsp 驱动 + FUSE 兼容层
 (挂载到目录,如 /mnt/yun)           (挂载到盘符,如 Y:)
            │                                   │
            ▼                                   ▼
 ┌──────────────────────────────────────────────────────────────┐
 │ 适配层:FUSE 回调,两端同一份代码                              │
 │ 回调转核心层调用 · 错误转 errno · 挂载选项和名称匹配按平台配置 │
 └──────────────────────────────┬───────────────────────────────┘
                                ▼
 ┌──────────────────────────────────────────────────────────────┐
 │ 核心层:平台无关,按路径提供读写和目录操作                     │
 │  ┌──────────────┐  ┌──────────────────┐  ┌────────────────┐  │
 │  │ 元数据缓存   │  │ 读写引擎         │  │ 接口客户端     │  │
 │  │ 路径→fileId  │  │ 按块 Range 读    │  │ 鉴权/统一响应  │  │
 │  │ 本地覆盖表   │  │ 暂存,关闭时上传 │  │ 三阶段上传     │  │
 │  └──────────────┘  └──────────────────┘  └────────────────┘  │
 └───────────┬──────────────────────────────────┬───────────────┘
             ▼                                  ▼
 云盘业务接口:HTTP POST + JSON        对象存储:预签名地址
 (/file/list、/file/create 等)        (PUT 分片上传 · GET Range 下载)
```

工作量集中在核心层,它在两个系统上是同一份代码;适配层很薄,日后如需换成 WinFsp 原生接口,只替换这一层。

**工程结构**

```
ydfs/
  cmd/ydfs/main.go     启动参数,按平台设置挂载选项
  core/                核心层(平台无关)
    api/               接口客户端
    meta/              元数据缓存与本地覆盖表
    io/                读块缓存、写暂存与上传
    fs.go              对外的按路径接口
  adapter/fusefs.go    适配层:FUSE 回调
  mock/                模拟服务端
  test/e2e/            验证脚本
```

**核心层对外接口**(适配层只依赖这一组):Lookup、List、Mkdir、Rmdir、Remove、Rename、Create、Open、Read、Write、Truncate、Flush、Release、Statfs。错误用核心层自己的错误类型返回,由适配层转成 errno。

## 4. 核心层设计(平台无关)

核心层对外只暴露按路径操作的接口,内部分三块:接口客户端、元数据缓存、读写引擎。它不引用任何 FUSE 类型,可以脱离挂载单独测试。

### 4.1 接口客户端

所有业务接口都是 HTTP POST + JSON,响应为 `{code, message, success, data}`,`code` 为 `0000` 表示成功。请求头带 `Authorization`、`x-yun-api-version`(默认 v1)、`x-yun-client-info`、`x-yun-app-channel`、`x-yun-tid`;上传类请求加 `x-yun-op-type: 1`。基地址用用户域路由返回的地址(modName=personal),做成配置项。

| 用途 | 接口 | 要点 |
| --- | --- | --- |
| 列目录 | POST /file/list | parentFileId;按 nextPageCursor 翻页直到为空 |
| 建目录 | POST /file/create | type=folder,fileRenameMode=refuse |
| 上传一阶段 | POST /file/create | type=file、size、partInfos、contentHash、contentHashAlgorithm;覆盖写时带 fileId |
| 取分片地址 | POST /file/getUploadUrl | 超过 100 片或地址过期时调用 |
| 上传二阶段 | PUT 预签名 uploadUrl | Content-Type: application/octet-stream;不带 Authorization |
| 上传三阶段 | POST /file/complete | 带 contentHash;返回 04010322 时按 1s、3s、5s 重试 |
| 取下载地址 | POST /file/getDownloadUrl | 返回 url、expiration、size |
| 下载 | GET 预签名 url | Range: bytes=起-止,左闭右闭 |
| 改名 | POST /file/update | fileId、name,fileRenameMode=refuse |
| 移动 | POST /file/batchMoveSync | fileIds、toParentFileId;逐项检查 batchFileResults.errCode |
| 删除 | POST /recyclebin/batchTrash | 返回 taskId 时轮询 |
| 查任务 | POST /task/get(v2) | status 非 Running 为止 |

请求体和响应体的字段以接口文档对应章节为准,实现前先读原文。

### 4.2 元数据缓存

- 按目录缓存子项列表:parentFileId → {名称 → 节点},节点含 fileId、类型、size、updatedAt、contentHash。
- 路径解析从根 "/" 逐级查缓存,未命中或过期时调 /file/list。有效期默认 5 秒,做成配置项。
- 本端的创建、改名、移动、删除、上传完成后,直接让相关目录的缓存失效。
- 名称匹配方式是配置项:Linux 精确匹配,Windows 先精确、再忽略大小写匹配。
- 未上传完成的新文件和改动中的文件放在"本地覆盖表"里,列目录和取属性时叠加在云端结果之上,大小取暂存文件的大小。

### 4.3 读

1. 打开时只记录 fileId,不下载。
2. 读请求按 4MB 对齐拆成块;块不在内存缓存时,用 Range 请求下载该块。
3. 块缓存按 (fileId, revisionId, 块号) 索引,近期最少使用淘汰,上限 16 块。
4. 下载地址按 fileId 缓存到 expiration 前 60 秒;对象存储返回 403 时重取地址并重试一次。

### 4.4 写

接口不支持随机写,所以采用"本地暂存、关闭时整文件上传"。

1. 新建文件:在缓存目录建空暂存文件,登记到本地覆盖表,此时不调云端。
2. 改已有文件:第一次写入或截断时,先把整个文件下载到暂存文件;截断到 0 时跳过下载。
3. 写入和截断作用于暂存文件,并置脏标记。同一路径被多次打开时共用一个暂存文件和摘要状态,按引用计数释放。顺序写入时直接对内存中的成功写入字节累积摘要,下载已有文件到暂存时也累积摘要。随机覆盖已摘要的前缀或缩短到前缀以内时使摘要失效;预分配与稀疏扩展保留未变前缀,未覆盖的尾部在提交前补算。
4. flush 或 release 时如有脏标记则提交:复用完整摘要或补算未摘要部分 → /file/create → 逐片 PUT → /file/complete。普通文件流仍走完整扫描。上传失败在 flush 里返回错误,让应用感知到;暂存内容未变时重试复用摘要。基准与限制见 [UPLOAD_PERFORMANCE.md](UPLOAD_PERFORMANCE.md)。
   上传状态在全局锁内建立与发布,摘要和网络请求在锁外执行;同文件变更/重复提交以条件变量等待,等待不持有全局锁。改名、删除与覆盖目标在等待后重新解析路径。属性使用暂存大小,本地读取保留上传游标。新建本地节点在云端提交前也提供有效创建时间。`[upload-perf]` 日志记录各阶段边界、耗时、补算字节数、分片进度和重试。
5. 提交成功后:用返回的 File 更新节点,清掉该 fileId 的块缓存,从本地覆盖表移除。
6. 新建时若 /file/create 返回同名已存在(exist=true 或 04010330),改为带该 fileId 覆盖写。

### 4.5 改名、移动、删除

- 同目录改名调 /file/update;跨目录先 /file/batchMoveSync,名称有变化再 /file/update。
- 目标已存在时,先把目标放入回收站再改名。这一步不是原子操作,编辑器"写临时文件再改名覆盖"的保存方式依赖它。
- 删除目录前先列举,非空返回"目录非空"。
- 还没上传的新文件被改名或删除时,只改本地覆盖表,不调云端。

### 4.6 错误码映射

| 云端错误码 | 含义 | 返回给文件系统 |
| --- | --- | --- |
| 04010300、04000010 | 文件或资源不存在 | ENOENT |
| 04010330、04000011 | 同名已存在 | EEXIST |
| 04000016 | 资源非空 | ENOTEMPTY |
| 04000012 | 配额不足 | ENOSPC |
| 04000005、04000006 | 认证失败、权限不足 | EACCES |
| 04010105、04010106 | 特殊目录不允许更新或删除 | EPERM |
| 其他 | — | EIO,并记日志(含 x-yun-tid) |

## 5. 适配层设计(FUSE 接口)

适配层只做三件事:把 FUSE 回调转成核心层调用、把核心层错误转成 errno、处理平台差异。目标是控制在几百行以内。

### 5.1 回调映射

| FUSE 回调 | 核心层调用 | 说明 |
| --- | --- | --- |
| getattr | Lookup | 目录返回 0755,文件返回 0644;mtime 取 localUpdatedAt,没有则取 updatedAt |
| readdir | List | 叠加本地覆盖表 |
| mkdir / rmdir / unlink | Mkdir / Rmdir / Remove | 先校验文件名:9 个非法字符返回 EINVAL,UTF-8 超过 255 字节返回 ENAMETOOLONG |
| rename | Rename | — |
| create / open | Create / Open | 返回句柄号;按打开标志低两位判断只读或可写 |
| read / write | Read / Write | 按句柄号找到句柄 |
| truncate / ftruncate | Truncate | FUSE 2.x 下 O_TRUNC 会转成单独的 truncate 调用 |
| flush / fsync / release | Flush / Release | 提交上传在 flush,release 兜底并释放句柄 |
| statfs | Statfs | 取不到容量时返回固定值 |
| chmod / chown / utimens | 无 | 直接返回成功 |

### 5.2 平台差异

平台差异集中在启动参数和两个配置项上,回调代码本身两端共用。

| 项 | Linux | Windows |
| --- | --- | --- |
| 底层 | 内核 FUSE + libfuse | WinFsp 内核驱动 + 其 FUSE 兼容层 |
| 挂载点 | 已存在的空目录 | 未占用的盘符,如 Y: |
| 挂载选项 | 默认即可 | uid=-1,gid=-1(文件属主映射为当前用户)、volname(卷标) |
| 名称匹配 | 精确匹配 | 忽略大小写兜底 |
| 非法字符 | 适配层校验并拒绝 | 系统本身已禁止,校验保留 |
| 暂存目录 | ~/.cache/ydfs | %LOCALAPPDATA%\ydfs |
| 资源管理器探测 | 无 | 会频繁查询 desktop.ini、Thumbs.db 等不存在的文件,依赖目录缓存直接返回 ENOENT,不能每次都调云端 |
| 卸载 | fusermount -u | Ctrl+C 或结束进程 |

## 6. 环境、构建与运行

**依赖**

| 平台 | 需要安装 |
| --- | --- |
| Linux | FUSE 2.x 运行库与开发包(Ubuntu:libfuse2、libfuse-dev)、gcc、Go |
| Windows | WinFsp 安装包(用 cgo 构建时勾选 Developer 组件)、Go |
| 备选方案 | Python 3.8+ 与 fusepy;Linux 只需 libfuse2,Windows 只需 WinFsp |

**构建(Go + cgofuse)**

```
# Linux
go build -o ydfs ./cmd/ydfs

# Windows(不依赖 C 编译器)
set CGO_ENABLED=0
go build -o ydfs.exe ./cmd/ydfs
```

**配置项**

| 配置 | 说明 | 默认 |
| --- | --- | --- |
| base-url | 业务接口基地址,如测试环境云空间底座 https://test.yun.139.com/personal-hw-test/hcy | 无,必填 |
| authorization | Authorization 请求头的值,从环境变量 YDFS_AUTH 读取,不写进命令行和日志 | 无,必填 |
| hash-algo | sha256(云空间底座)或 sha1(PDS 底座) | sha256 |
| client-info、app-channel | 对应 x-yun-client-info、x-yun-app-channel,文档注明客户端请求必填 | 无 |
| meta-ttl | 目录缓存有效期 | 5 秒 |
| block-size / part-size | 读块大小 / 上传分片大小 | 4MB / 10MB |
| cache-dir | 暂存目录 | 见 5.2 |

**运行**

```
# Linux
export YDFS_AUTH='<Authorization 值>'
./ydfs --base-url <基地址> --mount /mnt/yun

# Windows
set YDFS_AUTH=<Authorization 值>
ydfs.exe --base-url <基地址> --mount Y:
```

## 7. 验证用例

验证脚本只用普通文件操作访问挂载点,参数是挂载路径,所以同一个脚本在 Linux(/mnt/yun)和 Windows(Y:\)上都能跑。每个用例在独立的测试目录下执行,结束后清理。

| 编号 | 用例 | 通过条件 |
| --- | --- | --- |
| T1 | 建多级目录并列举 | 列表与创建一致;云盘客户端能看到同样的目录 |
| T2 | 写 1KB 小文件再读回 | 内容摘要一致 |
| T3 | 写 23MB 文件再读回 | 按 10MB、10MB、3MB 三个分片上传;读回摘要一致 |
| T4 | 重新挂载后只读 23MB 文件中间的 1MB | 内容正确;日志显示只发了对应块的 Range 请求,下载量远小于文件大小 |
| T5 | 对已有文件追加写 | 内容正确;fileId 不变,revisionId 变化 |
| T6 | 创建空文件 | 文件存在且大小为 0 |
| T7 | 同目录改名、跨目录移动 | 旧路径不存在,新路径内容一致 |
| T8 | 改名覆盖已有文件 | 目标内容为源文件内容;被覆盖的文件在回收站 |
| T9 | 删除文件;删除非空目录;删除空目录 | 非空目录返回错误,其余成功;被删项在回收站 |
| T10 | 列举含 250 个文件的目录 | 返回 250 项,验证翻页 |
| T11 | 打开文件读一块,等待超过 15 分钟后读另一块 | 第二次读成功,验证下载地址刷新 |
| T12 | 非法文件名(仅 Linux)、大小写不同的同名访问(仅 Windows) | Linux 返回错误;Windows 能打开同一文件 |
| T13 | 应用实测:Windows 资源管理器复制粘贴、记事本编辑保存;Linux 下 cp、vim 保存 | 操作成功,云端内容正确,无多余的重命名文件 |

模拟服务端下 T11 可把地址有效期调成几秒;T1、T8、T9 中"云盘客户端可见""在回收站"两项改为查模拟服务端状态。

**输出物**:两个平台各一份用例结果表(通过/失败及失败原因)、T3 的上传耗时和 T4 的首次读取耗时、第 8 节各项的实测结论。

## 8. 待联调确认项与风险

接口文档没有写明、需要在联调环境实测的事项:

| 编号 | 待确认 | 影响 |
| --- | --- | --- |
| Q1 | 0 字节文件怎么创建:partInfos 传一个 partSize=0 的分片是否被接受 | 决定 T6 的实现方式 |
| Q2 | 带 fileId 覆盖写时,name 和 parentFileId 是否仍需填写、是否触发重名检查 | 决定 T5 的请求体 |
| Q3 | 新建时同名冲突的返回形态:是 exist=true 加已有文件 fileId,还是直接报 04010330 | 决定 4.4 第 6 步的分支 |
| Q4 | 同一目录下是否可能存在仅大小写不同的两个名称 | 决定 Windows 端名称匹配是否会有歧义 |
| Q5 | 客户端鉴权下有没有可用的容量查询接口 | 决定 statfs 能否返回真实容量 |
| Q6 | 频繁调用 /file/list 和 /file/getDownloadUrl 是否会触发限流(04000009) | 决定目录缓存有效期和地址缓存策略 |
| Q7 | WinFsp 的 FUSE 兼容层在不声明大小写不敏感时,资源管理器和 Office 的行为是否正常 | 决定是否要在初始化时声明该能力 |

已知风险(demo 不解决,但要在结论里写明):

- **改大文件代价高**。改一个字节也要整文件下载再整文件上传,这是接口模型决定的,不适合数据库、虚拟机镜像这类原地改写的负载。
- **关闭文件会阻塞**。上传在关闭时同步完成,大文件关闭会卡住应用;产品化需要改成后台上传队列加失败重试。
- **改名覆盖非原子**。先删目标再改名,中途失败会丢目标文件(可从回收站恢复)。
- **多端一致性弱**。其他终端的改动要等目录缓存过期才可见;两端同时改同一文件,后提交的覆盖先提交的。
- **暂存占用本地磁盘**。写入中的文件按整文件大小占用本地空间。
- **WinFsp 许可**。WinFsp 为 GPLv3,闭源商用需购买商业授权;demo 阶段不受影响,立项前要确认。
- **内容审核**。高敏文件的下载地址会被替换成警示文件地址,读到的内容和大小会与元数据不符。

## 9. 实施步骤

按顺序执行,每一步的完成条件满足后再进下一步。

1. **搭工程骨架**。按第 3 节目录结构建工程,引入 cgofuse。完成条件:一个只含固定文件的只读文件系统能在本机挂载并列出。
2. **实现模拟服务端**。按 4.1 接口清单实现,并校验文档规则:分片最小 5MB、单次最多 100 个分片、complete 校验摘要、同名检查不区分大小写、分页游标、地址过期。有联调账号时这一步可跳过。
3. **实现接口客户端(4.1)**。完成条件:不经挂载,用单元测试跑通列举、建目录、三阶段上传、Range 下载、改名、移动、删除。
4. **实现元数据缓存和只读路径(4.2、4.3)**,接上适配层的 getattr、readdir、open、read、release。完成条件:T4、T10、T11 通过。
5. **实现写路径(4.4)** 和 create、write、truncate、flush。完成条件:T2、T3、T5、T6 通过。
6. **实现目录与改名删除(4.5)**。完成条件:T1、T7、T8、T9 通过。
7. **Windows 适配**。加上 5.2 的挂载选项和名称匹配配置,在 Windows 上构建并跑全部用例。完成条件:T1–T13 在两个平台都通过,且这一步没有改动核心层代码。
8. **切到联调或测试环境**。用测试账号和专用测试目录重跑用例,逐项回答 Q1–Q7。不要在有真实数据的账号上跑删除和覆盖类用例。
9. **出结论**。按第 7 节的输出物整理可行性结论:哪些通过、哪些受接口限制、产品化还缺什么。

**约束**

- 核心层不得引用 FUSE 类型,不得出现操作系统判断。
- 鉴权信息只从环境变量读取,不写入代码和日志;预签名地址的请求不带 Authorization。
- 删除一律走回收站,不调彻底删除接口。
