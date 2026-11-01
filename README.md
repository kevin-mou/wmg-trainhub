# TrainHub — 单节点模型训练管理台

TrainHub 是 WMG 仓库下的独立子工程，也是可以单独部署的训练节点服务。它把**数据集、Notebook、训练执行、日志、训练指标、执行环境与模型产物**放在同一台可控节点上：客户凭访问令牌进入工作台，提交训练后由节点内置的 Jupyter 内核执行，训练过程实时落盘，结果可直接下载，也可以被平台控制面推送和回收。

一句话概括：**不是公有云 Kaggle 的复刻，而是一台 GPU 节点的“客户自助训练工作台 + 平台执行器”。**

---

## 目录

- [1. 设计目的](#1-设计目的)
- [2. 适用场景](#2-适用场景)
- [3. 业务边界](#3-业务边界)
- [4. 使用方法](#4-使用方法)
- [5. Notebook 运行契约](#5-notebook-运行契约)
- [6. 平台侧集成（可选）](#6-平台侧集成可选)
- [7. 部署与运维](#7-部署与运维)
- [8. API 参考](#8-api-参考)
- [9. 配置项](#9-配置项)
- [10. 测试与自检](#10-测试与自检)
- [11. 已知限制与后续方向](#11-已知限制与后续方向)

---

## 1. 设计目的

### 1.1 解决什么问题

企业里常见的训练场景是：数据在内部、机器只有一台或少量几台、使用者是若干个可信的团队或客户。此时引入 Kubernetes、队列系统、对象存储、实验平台，成本和运维复杂度往往超过训练本身。

TrainHub 的目标是把这条链路压缩到一个进程和一个数据目录里：

- **客户自助**：每个客户一个长期访问令牌，登录后只看到自己的工作区。
- **训练可交付**：上传数据集，新建或导入 Notebook，提交任务，看到实时日志、训练指标、执行环境与产物。
- **低依赖**：FastAPI + SQLite + 进程内执行器 + 静态 SPA，不依赖 Redis、消息队列、外部调度器或前端构建链。
- **节点可离线**：控制台由服务端直接托管，GPU 节点无外网也能使用。
- **控制面可接入**：平台模型运营可以把代码包、数据集和资产包推给 TrainHub，按 SSE 收日志、结束后取产物；TrainHub 本身不主动连接控制面。

### 1.2 明确的非目标

TrainHub **不试图**成为：

- 面向不可信租户的强隔离多租户平台；
- 跨节点分布式训练或 GPU 集群调度器；
- 完整的实验管理/模型注册平台；
- 代码版本、数据集版本或模型版本的权威来源；
- 公有云式的按量计费、抢占或弹性伸缩系统。

这些能力如果将来需要，应当作为独立控制面或独立基础设施引入，而不是在单节点执行器里逐项堆叠。

### 1.3 架构概览

```text
浏览器（零构建静态 SPA，中文 / English）
        │  Authorization: Bearer <customer token>
        ▼
FastAPI（单实例，uvicorn 单 worker）
  ├── 鉴权：customers.token → 当前客户
  ├── SQLite（WAL）：customers / datasets / notebooks / runs / activity_logs
  ├── 文件存储：data/customers/<customer>/...
  └── 训练执行器（asyncio 队列，默认并发 1）
         └── nbclient ── Jupyter kernel（独立进程）

平台控制面（可选）
        │  POST /api/admin/executions + Admin Token
        └── 同一个 FastAPI / 执行器 / 数据目录
```

- 训练任务状态以数据库为事实源：服务重启时执行中的任务标记为失败，排队任务自动重新入队。
- 队列 FIFO；`TRAINHUB_MAX_CONCURRENT_RUNS` 控制并发，单 GPU 节点建议保持 `1`。
- 客户任务与平台推送任务共用同一执行槽，因此同一时刻通常只有一个任务运行。
- 控制台是纯静态 ES Module SPA，由 FastAPI 直接托管，不依赖 node 构建链。

---

## 2. 适用场景

### 2.1 适合

- **企业内网 / 专用 GPU 节点**：客户是内部团队或少量可信合作方，节点由企业自己掌控。
- **数据不出内网**：数据、Notebook、日志和产物都落在节点本地磁盘，不需要对象存储或外部实验平台。
- **离线 / 气隙环境**：节点没有稳定外网，控制台和训练执行仍要可用。
- **少量客户共享一台节点**：每个客户有自己的令牌、数据集、Notebook 和运行记录；默认单任务执行。
- **平台模型运营的节点执行器**：控制面把一次训练/评估的代码包、数据集、资产包推给节点，TrainHub 只负责执行并回传日志和产物。
- **以 Notebook 为训练入口**：用户希望保留 Jupyter 的交互式表达，而不是被迫改写成固定训练器插件。

### 2.2 不适合

- **不可信公网多租户**：Notebook 是任意代码，当前没有容器或操作系统级隔离。
- **大规模并发/多节点训练**：没有分布式调度、跨节点数据分发、GPU 显存感知调度或抢占。
- **需要严格按客户隔离文件系统**：共享/私有数据集只是应用层可见性，知道路径的 Notebook 可以读取同用户下的其他文件。
- **需要完整实验管理**：版本线、模型注册、指标对比、审批流等应由模型运营/实验平台承担。
- **需要按量计费或强配额**：当前只有上传大小与日志大小上限，没有存储/算力配额与计费。

---

## 3. 业务边界

这一节是部署和接入前最需要确认的部分。TrainHub 的“隔离”和“权限”是**可信企业场景下的协作边界**，不是对抗不可信代码的安全沙箱。

### 3.1 身份与可见性

- 每个客户一个长期访问令牌，保存在 `customers.token`（唯一索引），所有 `/api/**` 业务接口都需要 `Authorization: Bearer <token>`（或 `X-API-Token`）。
- 令牌决定客户身份，也决定默认工作区：客户只能看到自己的数据集、Notebook、运行记录和活动日志。
- 数据集支持 `private` / `shared`：
  - 默认私有；
  - 所有者可以在上传时共享，或之后切换；
  - 共享后其他客户可以看到、下载，并在提交训练时只读挂载；
  - 只有所有者可以取消共享或删除；
  - 有任何人排队/执行中的任务引用时，删除返回 `409`。
- **共享不是安全边界**：它只控制应用层的列表与挂载可见性，不能阻止 Notebook 按路径读取未共享的数据。这个能力是为“可信团队之间不要互相污染工作区”设计的，不是为隔离恶意代码设计的。

### 3.2 执行隔离

- Notebook 是任意代码，训练内核与 TrainHub 服务使用**同一个操作系统用户**。
- 当前没有容器、namespace、每客户独立 UID 或 CPU/GPU cgroup 隔离。
- 因此只应面向**可信客户 + 专用节点**。若要对不可信客户开放，请为每个客户部署独立实例/独立 Unix 用户，或把执行器替换为容器化方案。
- 服务重启会中断正在执行的任务；排队任务会在重启后恢复。SIGKILL/断电可能留下孤儿 `ipykernel_launcher` 进程，运维可用 `ps` 检查。
- 编辑器的“运行单元格”会为每个 Notebook 懒启动一个独立 Jupyter kernel，**同样与服务使用同一个操作系统用户**；会话工作目录在 `sessions/<客户>/<Notebook>/<会话>`，离开编辑器、空闲超时或服务停止时关闭。交互式 kernel 与训练队列是两条独立进程路径，不能把前者当作安全隔离。

### 3.3 调度与资源

- 单实例、FIFO、固定并发；默认 `TRAINHUB_MAX_CONCURRENT_RUNS=1`。
- 没有优先级、抢占、公平调度、GPU 显存感知或跨节点调度。
- 平台推送任务与客户 Web UI 任务共用执行槽；实例已有排队/运行任务时，新的平台推送返回 `409`，不排队、不抢占。
- 节点硬件（CPU/GPU/磁盘/内核）可通过管理 API `GET /api/admin/executor/status` 只读查询，但 TrainHub 不做资源预留。
- 平台控制面通过主动探测该端点判断节点忙闲；TrainHub 不发送心跳、不保存控制面地址。
- 交互式 Notebook 会话受 `TRAINHUB_MAX_KERNEL_SESSIONS`（默认 4）限制，空闲 `TRAINHUB_KERNEL_SESSION_IDLE_SECONDS`（默认 1800 秒）后回收；单单元格执行上限由 `TRAINHUB_KERNEL_CELL_TIMEOUT_SECONDS`（默认 300 秒）控制。

### 3.4 数据、日志与保留

- 数据集、Notebook、训练日志、训练指标、环境快照和产物都保存在 `TRAINHUB_DATA_DIR` 下。
- 数据不会自动过期；客户或管理员显式删除时才释放。请结合节点磁盘容量配置上传上限与备份策略。
- 训练日志写入 `logs/run.log`，超过 `TRAINHUB_MAX_LOG_MB` 后截断并注明；支持增量读取、tail 与下载。
- 训练指标写入可选的 `logs/metrics.jsonl`，只用于过程曲线；服务只读取尾部，但文件本身仍占用磁盘。
- 运行目录默认只保留日志、环境快照与 `output/`；`work/`、`home/`、`tmp/` 在任务结束后清理（`TRAINHUB_KEEP_RUN_WORKDIR=true` 可保留现场）。
- 备份建议：`sqlite3 trainhub.db ".backup backup.db"` 加整个 `customers/` 目录，在低峰或停机时执行；恢复时保持目录与数据库一致。

### 3.5 结果与版本语义

- TrainHub **只负责执行**：不保存/解释客户 Notebook 的版本语义，也不保存平台推送代码包的版本线；版本由控制面作业记录。
- 客户新建/导入的 Notebook 是独立对象，没有版本线；编辑会更新同一个对象，不保留历史版本。数据集同样每次上传是一个新对象。
- 有排队/执行中的任务引用某个 Notebook 时，禁止编辑该 Notebook（返回 `409`）；避免排队任务读到保存到一半的内容。
- `OUTPUT_DIR/trainhub_result.json` 是平台成果桥的**最终结果清单**：模型名、基座、产物路径、最终指标等，供模型运营导入。
- `logs/metrics.jsonl` 是**过程观测**：不参与模型运营导入决策。两者语义不同，不互为替代。
- `environment.json` 区分 `service`（TrainHub 服务进程的 Python/平台/主机/CPU）与 `kernel`（Notebook 的 kernelspec 标识）：
  - 训练内核实际使用的 Python/依赖由节点管理员准备的 kernelspec 决定；
  - 服务进程快照不会冒充内核 Python；
  - NVIDIA GPU 通过 `nvidia-smi` 记录；Apple GPU 的实时信息由 executor status 提供。

### 3.6 安全边界

- 访问令牌是长期凭据：只应通过 HTTPS 暴露；数据库与备份文件权限要收紧；泄露后立即 `rotate-token`。
- 管理接口需要 `X-Admin-Token: $TRAINHUB_ADMIN_TOKEN`；未配置时整体禁用（`503`）。
- 平台到 TrainHub 之间会传输管理令牌（请求头）与客户令牌明文（创建/重置响应），请使用 HTTPS 或把节点限制在受控内网；平台侧的 `TRAINHUB_VERIFY_TLS=false` 只用于自签证书测试环境。
- 上传上限：`TRAINHUB_MAX_DATASET_MB` / `TRAINHUB_MAX_NOTEBOOK_MB` / `TRAINHUB_MAX_CODE_MB` / `TRAINHUB_MAX_ASSETS_MB`；日志上限 `TRAINHUB_MAX_LOG_MB`。接口层未做请求频率限制，公网部署应在反向代理上限制速率与并发。
- 下载路径做了穿越校验；ZIP 打包不跟随符号链接，避免产物把节点上的外部文件打包出去。
- **执行隔离（2026-11-01）**：Linux 节点默认启用 Docker 沙箱（`TRAINHUB_SANDBOX=docker`）：每个交互式内核/训练任务一个临时容器，非 root、只读根文件系统、`cap-drop ALL`、`no-new-privileges`、CPU/内存/PID 限制、默认 `--network none`；只挂载会话 `work`/`home`/IPC、选中的数据集（只读）、共享模型缓存（默认只读）和 `trainhub_runtime`（只读），不挂 `.env`、数据库、代码仓库或 Docker socket。容器结束即删除，启动时按标签清理遗留容器。
- **macOS/MLX 节点不能使用 Docker 沙箱**：Docker 在 macOS 上是 Linux 容器，MLX 依赖 macOS/Metal，无法运行；macOS 节点必须 `TRAINHUB_SANDBOX=none`，只适合可信客户/专用设备。
- **`none` 模式仍然是同用户子进程**，不是安全边界；本机内核已改为**最小环境白名单**（不再整份继承 `os.environ`，默认不传 `TRAINHUB_ADMIN_TOKEN`、`HF_TOKEN`、数据库/云凭据等），但这只能降低误泄，不能阻止同用户读取 `.env`/SQLite/其他客户目录。
- `TRAINHUB_SANDBOX_RUN_AS` 默认服务用户的 `uid:gid`；服务不能以 root 运行 Docker 沙箱。Docker 组本身等价于 root 权限，生产建议 rootless Docker/Podman，并把 7470 限制在受控网络。
- 网络 `none` 时容器内 `HF_HUB_OFFLINE=1`，模型必须先在节点上用 `trainhub.cli models pull` 预拉；需要 Notebook 联网时显式设置 `TRAINHUB_SANDBOX_NETWORK` 并接受相应出口风险。
- 登录页「记住令牌」Cookie 与 localStorage 同属浏览器可读凭据；共享设备请取消勾选。

### 3.7 设计取舍：刻意不做的事

| 不做 | 原因 |
|------|------|
| 全局参数 schema / ParamSpec | Notebook 参数由用户代码定义，TrainHub 不知道也不应假装知道；强制 schema 会变成假契约或限制任意 Notebook。用「最近一次运行参数回填」解决大部分手写 JSON 易错问题。 |
| 每次都写完整 `run.json` | 运行元数据已在 SQLite，`trainhub_result.json` 已承载最终结果；再写一份运行清单会形成第二事实源。需要可移植时按需导出 bundle，而不是常驻清单。 |
| 设备下拉字段 | Notebook/训练框架自己选择设备；平台加一个字段但没有消费者就是哑字段。硬件事实已在 executor status 提供。 |
| 数据集格式识别（YOLO/labelme 等） | 这是标注/数据集工具或模型运营的语义，不是通用 Notebook 执行器该承担的职责。 |
| 训练器插件注册表 | 执行单元是 Notebook；训练语义由 Notebook 和模型运营定义。在 TrainHub 加注册表会形成第二套“能训什么”的目录。 |

---

## 4. 使用方法

### 4.1 本地快速开始

```bash
cd wmg-trainhub
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

cp .env.example .env    # 本地把 TRAINHUB_DATA_DIR 改成可写目录，并设置 TRAINHUB_ADMIN_TOKEN
PYTHONPATH=src .venv/bin/python -m trainhub.cli init
PYTHONPATH=src .venv/bin/python -m trainhub.cli create-customer --name "Acme Labs"

./start.sh          # 默认监听 0.0.0.0:7470
```

打开 `http://127.0.0.1:7470`，粘贴创建客户时输出的访问令牌即可进入控制台。

> **执行沙箱提示**：Linux 节点默认 `TRAINHUB_SANDBOX=docker`，需要先构建/拉取沙箱镜像（见 7.1）；本地开发若暂时不用 Docker，在 `.env` 设置 `TRAINHUB_SANDBOX=none`（只适合可信本地环境）。macOS/MLX 节点必须 `none`。

也可以直接用模块启动：

```bash
PYTHONPATH=src .venv/bin/python -m trainhub.cli serve --host 127.0.0.1 --port 7470
```

`trainhub.cli` 与 `start.sh` 都读取子工程根目录的同一份 `.env`（进程环境优先）。**改了 `TRAINHUB_DATA_DIR` 后一定要让 CLI 与服务使用同一份 `.env`**：两者指向不同的库时，会出现“客户创建成功、令牌却登录无效”。

仓库自带示例 Notebook `examples/hello_trainhub.ipynb`：导入它（以及任意数据集），提交一次训练，就能看到实时日志、参数传递、训练指标曲线与产物下载的完整链路。

### 4.2 客户工作台

登录后左侧导航包括：

- **概览**：客户信息、数据集/Notebook/运行统计、存储水位、进行中任务与最近任务。
- **数据集**：上传多文件或整个目录（保留相对路径，自动去掉共同顶层目录）；ZIP 可一键解压为目录内容；点击数据集名称查看文件清单（相对路径与大小）；默认私有；所有者可以共享/取消共享、下载、删除。
- **Notebooks**：**新建 Notebook 默认进入手动编辑器**；也可以点击「导入 .ipynb」导入已有文件。支持下载（纯脚本 `.ipynb`，会自动挂载最新且代码匹配的 Session 快照输出）、编辑、删除（有活动任务时返回 `409`）。
- **训练任务**：提交、查看、取消、删除运行。
- **活动记录**：上传、提交、编辑、下载、删除等操作审计。
- **账户**：客户资料、令牌脱敏展示、语言/主题切换。

#### 新建与编辑 Notebook

「Notebooks」页面的主按钮是「新建 Notebook」，点击进入手动编辑器：

- 默认带一个自动识别类型的空单元；可以添加代码、描述（Markdown）、原始（raw）单元。
- 每个单元是独立编辑器：**非编辑态显示预览**（Markdown 渲染、代码纯文本），点击预览进入编辑态；失焦后回到预览。Markdown 预览按普通文档排版，不显示 cell 卡片背景、边框和头部标题。
- 代码单元使用 **CodeMirror 6**（Python/Markdown 语法高亮、行内换行、撤销/重做）；非编辑态的代码预览也使用同一 Python 解析器做只读语法高亮。Markdown 预览由 marked + DOMPurify 渲染，原始 HTML/SVG 会被清理。
- 单元类型默认为「自动」，按内容判断 code / markdown / raw，也可以随时手动切换。
- 编辑器顶部（名称下方）保留精简工具栏：**运行**（按顺序运行全部代码单元格）、重启内核（清空变量与导入状态）、下载（`.py`/`.md`/`.txt`）；工具栏最右侧是 Kaggle 风格的运行状态标签（`编辑中` / `运行中`）。每个 cell 最左侧的 prompt 槽位显示 `[cell序号]`；选中/聚焦代码单元时，序号下方出现运行按钮（运行本格，前置未执行或已修改时自动补跑）。cell 头部左侧是类型切换下拉；进入编辑态时头部右侧显示六个快捷操作：下方插入、上方插入、上移、下移、复制、删除。Markdown 预览不显示头部，需要点击进入编辑后才能切换类型。工具栏在页面滚动时吸顶在顶部栏下方，按钮 tooltip 会跟随视口避让并在窄屏自动换行。工具栏还包含**数据集选择**：选中结果持久化到该 Notebook，运行单元格时挂载到 `WMG_DATASET_DIR`（运行中不可切换，数据集支持目录结构）。
- 快捷键：`A` / `B` 在选中单元上方/下方插入代码单元，`D` `D` 删除，`Ctrl/Cmd + S` 保存，`Ctrl/Cmd + Shift + ↑/↓` 移动单元。
- 页面顶部标题直接显示 Notebook 名称，名称右侧的编辑图标可进入就地改名（失焦或回车提交、Esc 取消；改名算脚本修改，需保存）；保存后写入服务器的 `.ipynb` 文件，并出现在 Notebook 列表中。
- 列表里点击 Notebook 名称或编辑图标，会加载现有内容继续编辑；已导入的 Notebook 也可以编辑，保存时会保留原有单元格 metadata、输出与顶层 metadata。
- 代码单元卡片下方显示**运行输出回显（只读）**：stdout/stderr、异常、`text/plain`、`application/json` 以及 PNG/JPEG/GIF/WebP 图片；HTML/SVG 等不可信内容不渲染，只提示已省略。输出来自独立 Session 快照，不属于 `.ipynb` 脚本内容；输出区右上角的复制按钮可一键复制该单元格的全部文本输出（图片等非文本块不参与复制）。
- 右侧栏分两个区块：**「目录」**从 Markdown 标题生成（忽略代码块内的 `#`），**「单元格」**列出全部单元格（序号 + 类型 + 首个非空行摘要），点击都会选中并滚动到对应单元格，当前单元格在两处都高亮；窄屏时右侧栏移到上方。
- 有排队/执行中的训练任务引用该 Notebook 时，保存返回 `409`，避免排队任务读到保存到一半的内容。
- **运行单元格**使用按 Notebook 懒启动的交互式 Jupyter kernel：连续运行共享变量/导入状态，输出写回当前单元作为运行回显，**执行结束后若有输出会自动滚动到该单元的输出区**（错误 traceback 同样适用）。单元格运行按钮的默认动作是**「运行本格 + 自动补前置」**：先在同一内核里按顺序补跑尚未执行、或源码自上次成功执行后已变化的**前置代码单元格**，再运行目标单元格——任意单元格都可以直接点运行，无需手动逐格执行上面的单元格；已执行且源码未变的前置单元格会跳过，不重复产生副作用；点击运行的那一格则总是重新执行（跳过它会让点击毫无反应）。工具栏的**运行**是**「运行全部」**：按顺序执行全部代码单元格、不跳过已执行格，用于完整重放或首次执行整本脚本；工具栏另提供**「重启内核」**（丢弃内核状态、不运行代码，之后可用「运行」或单元格按钮干净重跑）。**训练任务（含平台推送的执行）排队/运行期间不允许运行单元格**：节点是单任务实例、GPU 独占，执行接口直接返回 409 并点名占用中的任务；任务结束自动恢复（内核启动本身不受限，仅执行时把关）。**执行是后台任务**：点击运行后服务端在后台执行并记录运行态（当前单元格、逐格状态与输出），接口立即返回。输出通过 **SSE 事件流实时回显**（`GET /api/notebooks/{id}/runs/stream?runId=`）：连接后先发 `state` 快照（补齐已产生的输出），之后按变化推送 `count` / `cell` / `stream`（只带新增文本）/ `output`（新增输出项）/ `outputs`（整体替换）/ `run`（终态）；断线自动重连，重连同样以快照重同步（事件带单调 `seq`，快照带 `lastEventSeq`，更小的事件丢弃）。刷新/重新进入页面**不会中断执行**，新页面会自动接管运行态——运行中继续显示「运行中」并接收实时输出，已结束则直接回填输出；运行中再次启动会得到 409（同一 Notebook 同一时刻只允许一个运行）。`GET /runs/current` 保留用于初载/终态取回完整结果。运行**不自动保存** Notebook，也不自动保存 Session；运行结束后点击「保存会话」才会写入 Session 快照。快照只保存运行输出与 `execution_count`，不保存源码、名称或结构；存在未保存的脚本修改时会阻止保存并提示先保存 Notebook。`.ipynb` 永远是纯脚本（`outputs=[]`、`execution_count=null`）；导入已有 Notebook 时原有输出会迁移为 `origin=import` 的 Session 快照，存量文件在首次读取/下载时也会把旧 outputs 迁为 `origin=legacy` 快照后清空脚本。下载 `.ipynb` 时，服务端会把最新快照中**源码哈希匹配**的输出挂载到导出的副本，不修改存储文件。编辑器与“提交整本 Notebook 训练”是两条独立路径：后者仍走执行队列、日志与产物。
- 新建但尚未保存的 Notebook 首次运行单元格前会先保存纯脚本（用于创建 Notebook 与稳定 cell id）；**离开编辑器不再关闭内核会话**（后台运行需要继续、刷新后靠它恢复运行态与输出），会话由服务端按空闲回收（`TRAINHUB_KERNEL_SESSION_IDLE_SECONDS`，默认 30 分钟；同时最多 `TRAINHUB_MAX_KERNEL_SESSIONS` 个）。
- 交互式内核会创建 `work/dataset` 与 `work/output`（注入 `WMG_DATASET_DIR` / `WMG_OUTPUT_DIR`）。工具栏选择数据集后，运行单元格/会话会把数据集文件挂载到 `work/dataset`（符号链接/复制，保留目录结构）；未选择时该目录为空。训练产物仍需通过训练任务或自行访问可读路径。
- 「导入 .ipynb」保留为独立入口：选择文件、可选显示名称，导入后同样可以继续手动编辑。

#### 提交训练

在「训练任务 → 提交训练」中：

1. 选择 Notebook（必填）和数据集（可选）。
2. 选择内核；不选时交给 Jupyter 使用 Notebook metadata 或默认内核。
3. 可选填写任务名称、超时秒数、参数 JSON。
4. 参数表单会自动回填该 Notebook **最近一次运行**的参数；同一次弹窗内切换 Notebook 会保留各自未提交的草稿。
5. 提交后跳转到运行详情，日志与指标随训练进度更新。

#### 运行详情

- **日志**：增量轮询 `run.log`，支持自动跟随、回到底部、下载完整日志。
- **训练指标**：如果 Notebook 写了 `WMG_METRICS_FILE`，显示多系列曲线、末值和数据点数量；没有指标文件时不显示该面板。
- **执行环境**：显示服务 Python/平台、实际内核、NVIDIA GPU、TrainHub 版本。
- **任务信息**：状态、内核、数据集（含共享来源）、时间、耗时、退出码、超时设置。
- **参数**：提交时的 JSON 参数。
- **产物**：`OUTPUT_DIR` 下的文件列表、单文件下载与 ZIP 打包下载。
- **操作**：排队/执行中可取消；已结束可删除（日志与产物一并删除，不可恢复）。

#### 数据集共享

- 上传时勾选「共享给其他用户」，或之后在数据集列表切换。
- 共享后其他客户可以在列表中看到、下载，并在提交训练时只读挂载到 `dataset/`。
- 只有所有者能取消共享或删除；任何人排队/执行中的任务引用时删除返回 `409`。
- 共享是应用层可见性，不是安全边界；详见 [3.1](#31-身份与可见性)。

### 4.3 客户与令牌管理

```bash
# 创建客户（输出访问令牌）
PYTHONPATH=src .venv/bin/python -m trainhub.cli create-customer --name "Acme Labs" --email ops@acme.test

# 列出客户
PYTHONPATH=src .venv/bin/python -m trainhub.cli list-customers

# 重置令牌 / 停用 / 启用
PYTHONPATH=src .venv/bin/python -m trainhub.cli rotate-token --id c_ab12cd34
PYTHONPATH=src .venv/bin/python -m trainhub.cli set-status --id c_ab12cd34 --status disabled
```

管理 API（需要 `X-Admin-Token: $TRAINHUB_ADMIN_TOKEN`）：

- `POST /api/admin/customers`（可选 `token`：导入既有身份时指定访问令牌，须 `trh_` 开头且全局唯一）、`GET /api/admin/customers`
- `POST /api/admin/customers/{id}/rotate-token`
- `POST /api/admin/customers/{id}/status`
- `DELETE /api/admin/customers/{id}`（同时删除该客户全部文件，不可恢复）
- `GET /api/admin/overview`
- `GET /api/admin/runs?page=&size=&status=&customerId=&q=`
- `GET /api/admin/runs/{run_id}`
- `GET /api/admin/runs/{run_id}/metrics?maxPoints=`
- `GET /api/admin/runs/{run_id}/outputs/download`
- `GET /api/admin/runs/{run_id}/logs`
- `GET /api/admin/runs/{run_id}/logs/stream`（SSE：status/log/end）
- `POST /api/admin/runs/{run_id}/cancel`
- `GET /api/admin/executor/status`
- `POST /api/admin/executions`（平台控制面推送的一次闭环执行；`customerId` 指定执行归属客户，缺省保留客户 `sys_platform`）

令牌按需求**明文保存在数据表中**（`customers.token`）。管理 API 的客户列表会返回令牌明文，只应通过受控网络访问；一旦泄露立即 `rotate-token`。控制台不会在页面 URL 中携带令牌，所有请求走请求头。

### 4.4 登录页「记住令牌」

登录页支持「记住令牌」（默认勾选）：成功登录后把令牌写入本浏览器 Cookie `trainhub_remembered_token`（30 天、`Path=/`、`SameSite=Strict`、HTTPS 下带 `Secure`），下次打开登录页自动回填。该 Cookie 只用于回填登录框，不参与鉴权；取消勾选并登录即清除。共享设备请取消勾选。

---

## 5. Notebook 运行契约

每个训练任务都在独立工作目录中执行：

```text
data/customers/<customer>/runs/<run>/
├── work/
│   ├── notebook.ipynb     # 客户任务：提交时的 Notebook 副本（内核 cwd）
│   ├── code/              # 平台任务：解压后的代码包（入口 notebook 所在目录为 cwd）
│   ├── dataset/           # 数据集挂载（客户任务为符号链接；平台任务为解压目录）
│   └── assets/            # 平台任务：可选资产包（模型/基线目录）
├── output/                # 产物目录；executed_notebook.ipynb 也在这里
├── logs/run.log
├── logs/metrics.jsonl     # 可选：Notebook 追加的结构化训练指标
├── environment.json       # 执行器写入的执行环境快照
├── home/                  # 内核 HOME / 缓存（任务结束后默认清理）
└── tmp/                   # 内核临时目录（任务结束后默认清理）
```

### 5.1 注入到内核的环境变量

| 变量 | 含义 |
|------|------|
| `WMG_DATASET_DIR` / `DATASET_DIR` | 数据集目录（`work/dataset`） |
| `WMG_OUTPUT_DIR` / `OUTPUT_DIR` | 产物目录；写入这里的文件会出现在「产物」面板 |
| `WMG_METRICS_FILE` | 可选训练指标文件（`logs/metrics.jsonl`）；追加 JSONL 后运行详情显示曲线 |
| `WMG_PARAMS` | 提交任务时填写的参数，JSON 字符串 |
| `WMG_RUN_ID` / `WMG_RUN_NAME` | 当前任务 ID / 名称 |
| `WMG_CUSTOMER_ID` | 当前客户 ID |
| `WMG_DATASET_NAME` / `WMG_NOTEBOOK_NAME` | 数据集 / Notebook 显示名 |
| `WMG_WORK_DIR` | 内核工作目录 |
| `WMG_EXECUTION_ORIGIN` | 平台任务为 `platform`；客户任务不设置 |
| `WMG_CONTROL_JOB_ID` | 平台控制面作业 ID（平台任务） |
| `WMG_CODE_SHA256` / `WMG_DATASET_SHA256` / `WMG_ASSETS_SHA256` | 平台任务输入包摘要 |
| `WMG_BASE_MODEL` | 平台任务声明的基座标识；训练脚本可用 `trainhub_runtime.ensure_model` 解析 |
| `WMG_ASSETS_DIR` / `WMG_MODEL_DIR` / `WMG_BASELINE_MODEL_DIR` | 平台资产包目录；存在时注入 |
| `WMG_MODEL_CACHE` | 服务级共享模型缓存根目录（所有会话/任务共用） |
| `HF_HUB_CACHE` / `HF_XET_CACHE` | 指向共享缓存的 Hugging Face 缓存目录，避免每个会话重复下载 |
| `PYTHONPATH` | 追加了 `trainhub_runtime` 助手包所在目录，Notebook 可直接 import |

**Notebook 数据集选择**：编辑器工具栏的「数据集」选择会持久化在该 Notebook 上（`notebooks.dataset_id`）。
运行单元格/启动会话时，选中的数据集文件会以符号链接（失败退化为复制）挂载到会话的
`WMG_DATASET_DIR`，**目录结构原样保留**；切换数据集会整体替换该目录。运行中不能切换（接口返回 409），
新选择在下次运行时生效。数据集本身支持整目录上传（自动去掉共同顶层目录），ZIP 可在数据集页一键解压成
目录内容。训练任务提交表单里的数据集选择是独立的，按每次任务快照。

### 5.2 执行语义

- 按 Notebook 顺序执行代码单元格；**遇到第一个异常即停止**，任务标记为失败，退出码 `1`，日志保留完整 traceback。
- 日志逐行带 UTC 时间戳，包含单元格边界与耗时、标准输出、`execute_result` 的文本表示、异常 traceback；超过 `TRAINHUB_MAX_LOG_MB` 后截断并注明。
- `TRAINHUB_RUN_TIMEOUT_SECONDS` / 任务级 `timeoutSeconds` 是整体时限，超时会终止内核；`TRAINHUB_CELL_TIMEOUT_SECONDS` 是单元格时限。两者默认 `0`（不限制）。
- 任务结束后默认清理 `work/`、`home/`、`tmp/`，只保留日志、环境快照与 `output/`；设 `TRAINHUB_KEEP_RUN_WORKDIR=true` 可保留现场（会占用更多磁盘）。
- 平台任务执行完会回收 `executions/<run>/` 下的输入包；代码/数据版本在控制面，不在节点重复保存。

### 5.3 可选：训练指标曲线

训练框架可以把逐 step / epoch 的指标追加到 `WMG_METRICS_FILE`（UTF-8 JSONL，一行一个样本）：

```json
{"step": 1, "metrics": {"loss": 0.9, "accuracy": 0.6}}
{"step": 2, "metrics": {"loss": 0.5, "accuracy": 0.7}}
```

- `step` 缺失时用 `epoch`；两者都缺失时用有效样本序号作为横轴。
- 只有有限数值会进入图表；非法行、非数值字段会被跳过；写入中的最后半行不会计成错误。
- 服务只读取文件尾部最多 4 MB，每个系列最多返回 2000 个点（最多 32 个系列），超出时均匀降采样并把 `truncated` 标为 `true`。
- `GET /api/runs/{id}/metrics` 返回 `{present, series:[{name, points:[[x,y], …]}], sampleCount, pointCount, skippedLines, truncated, updatedAt}`；没有文件时 `present=false`、`series=[]`。
- 指标是**过程观测**：不写这个文件就没有曲线，不影响训练、日志或产物；模型运营导入仍只读取 `OUTPUT_DIR/trainhub_result.json` 的最终结果。
- 建议每个 step / epoch 只写一行数值指标，不要写入日志或大段文本；服务只读取尾部，但文件本身仍会占用运行目录磁盘。
- 示例 Notebook `examples/hello_trainhub.ipynb` 已演示如何写入。

### 5.4 执行环境快照

执行器在训练启动时写 `environment.json`：

```json
{
  "trainhubVersion": "0.1.0",
  "service": {
    "pythonVersion": "3.11.x",
    "pythonExecutable": "/opt/trainhub/.venv/bin/python",
    "platform": "Linux-...",
    "machine": "x86_64",
    "processor": "...",
    "hostname": "...",
    "cpuCount": 32
  },
  "kernel": {
    "requested": "python3",
    "effective": "python3",
    "displayName": "Python 3 (Training)",
    "language": "python"
  },
  "gpu": [{ "name": "NVIDIA ...", "vendor": "nvidia" }],
  "capturedAt": "2026-10-27T00:00:00+00:00"
}
```

- `service` 是 TrainHub 服务进程环境，不等于训练内核的 Python/依赖环境。
- `kernel` 是 Notebook 的 kernelspec 标识；实际解释器与依赖由节点管理员准备的 kernelspec 决定。
- `gpu` 只探测 NVIDIA（`nvidia-smi`）；Apple GPU 的实时信息在 executor status 中查看。

### 5.5 平台成果桥：`trainhub_result.json`

平台管理端「训练服务 → 训练成果」可以跨客户查看训练任务，并把**带结果清单**的成功任务导入模型运营。

Notebook 在 `OUTPUT_DIR` 写 `trainhub_result.json`：

```json
{
  "modelName": "qwen-phish-l2",
  "baseModel": "Qwen3.5-0.8B",
  "adapterPath": "adapter",
  "packagePath": "adapter",
  "family": "wmg-mail",
  "dataset": { "name": "demo", "version": 1 },
  "metrics": {
    "overall": { "joint_accuracy": 0.8, "type_accuracy": 0.9,
                 "motivation_accuracy": 0.85, "binary_accuracy": 0.95 },
    "eval_set": { "sha256": "…", "samples": 0,
                  "label_schema": "v2", "input_schema": "v1" }
  },
  "backend": "hf",
  "notes": "可选"
}
```

- `modelName` / `baseModel` 必填（用于控制面登记模型版本）；`adapterPath` 默认整个输出目录，指向的文件/目录必须存在。
- `metrics` 可选；支持标准的 `overall`/`eval_set` 结构，也接受 `joint`/`typeAcc`/`motivationAcc`/`binaryAccuracy` 摘要键。
- 导入会生成模型运营的 `external.import` 作业、模型版本（stage=candidate）与一条 `mode=metrics` 评估；`run_id = trainhub-<训练任务 ID>`，重复导入幂等。
- 产物会落到模型运营 artifacts，作为部署包与基线归档使用；训练任务的输出目录本身保持不变。

### 5.6 共享模型缓存与 `trainhub_runtime`

**基座模型是 TrainHub 的服务级基础资源，不属于某一次训练任务。** 所有 Notebook 会话与训练任务共用
`TRAINHUB_MODEL_DIR`（默认 `$TRAINHUB_DATA_DIR/model-cache`）下的同一份缓存；Notebook 不要再自己拼缓存目录、
选择下载策略或处理并发下载。

平台向内核注入 `WMG_MODEL_CACHE`、`HF_HUB_CACHE`、`HF_XET_CACHE`，并把 `trainhub_runtime` 加进
`PYTHONPATH`。Notebook 里只需要：

```python
from trainhub_runtime import ensure_model

base_model_dir = ensure_model(
    "startlux-models/StartLux-Decision-v1.1-0.8B-Instant-MLX-4bit",
    required_files=["config.json"],
)
```

- `ref` 也可以是本地目录（例如平台资产包里的 `WMG_MODEL_DIR`），此时直接校验并返回；
- 命中缓存**不联网**；未命中才在仓库级文件锁内调用 `huggingface_hub.snapshot_download`，
  多个内核/任务同时启动不会重复拉同一个基座；
- `revision=` 固定分支/tag/commit（可复现训练建议显式指定）；`refresh=True` 重新校验远端；
- `local_files_only=True` 或环境变量 `TRAINHUB_MODEL_OFFLINE=1` 时只读缓存，缺失立即报错（气隙节点）；
- 调用时若进程里 `HF_HUB_CACHE` / `HF_XET_CACHE` 未设置，助手会把它们指到同一根——手工
  `models pull`、本地调试等不经平台注入的进程也不会把 xet 分块缓存散落到各自的 `~/.cache`；
- 下载进度、断点与文件去重都由 Hugging Face 负责，TrainHub 不再实现一套下载器。

运维侧：

```bash
# 查看缓存占用（服务级只读接口：GET /api/admin/models；应用 venv 即可）
PYTHONPATH=src .venv/bin/python -m trainhub.cli models list

# 预热基座（可重复执行；已有缓存时不会重新下载）
# pull 需要 huggingface_hub，请用训练环境解释器（生产为 trainer-venv，本机为 trainer-mlx-venv）
PYTHONPATH=src /opt/trainhub/trainer-venv/bin/python -m trainhub.cli models pull org/model \
  --revision main --required-file config.json
```

缓存目录可以直接备份或整体删除（删除后下次运行会重新下载）；它不是模型注册表，不表达模型版本线，
模型版本仍由模型运营/实验平台管理。`TRAINHUB_MODEL_DIR` 直接就是 Hugging Face 的 `cache_dir`
（目录内是 `models--org--name/...`）；如果节点上已有过去由 Notebook 自行 `snapshot_download(cache_dir=...)`
下载的缓存，把它指向该目录即可复用，不需要搬文件或重新下载。

> **安全边界**：共享模型缓存按"可信客户"场景设计（与数据集共享一致）。Notebook 内核继承 TrainHub
> 服务进程的环境变量，如果服务 env 配了 `HF_TOKEN`，客户代码可以读取并使用它；仅管理员可见的 HF 凭据
> 不要放进 TrainHub 服务 env。需要气隙/预置模型时，用节点侧 `models pull` 预下载并关闭 Notebook 的联网下载。

---

## 6. 平台侧集成（可选）

TrainHub 可以完全独立使用；以下集成是可选的上层能力，不改变 TrainHub 单实例执行模型。

### 6.1 管理端代理（P1）

平台管理端（wmg-web）有一个独立的「训练服务 → 训练客户」菜单组，用于统一管理训练客户；客户训练工作台仍是本工程自带的控制台，平台不代理其 UI。

**wmg-server 环境变量**（管理端代理到训练节点）：

| 变量 | 说明 |
|------|------|
| `TRAINHUB_BASE_URL` | 训练节点服务地址，如 `https://trainhub.example.com`；空 = 菜单页提示未配置 |
| `TRAINHUB_ADMIN_TOKEN` | 训练节点的 `TRAINHUB_ADMIN_TOKEN`（服务端持有，不下发浏览器） |
| `TRAINHUB_CONSOLE_URL` | 控制台浏览器地址（可选，默认等于 `TRAINHUB_BASE_URL`） |
| `TRAINHUB_VERIFY_TLS` | 是否校验节点 TLS 证书（默认 `true`；自签测试环境可关） |
| `TRAINHUB_TIMEOUT_SECONDS` | 代理请求超时（默认 15 秒） |

**代理端点**（平台系统管理员；写操作另要求平台写权限）：

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/v1/admin/training/config` | 是否已配置 + 控制台地址 |
| GET | `/api/v1/admin/training/customers?page=&size=&q=` | 训练客户列表（令牌脱敏） |
| POST | `/api/v1/admin/training/customers` | 创建客户（响应返回一次明文令牌） |
| POST | `/api/v1/admin/training/customers/{id}/rotate-token` | 重置令牌（返回一次明文） |
| POST | `/api/v1/admin/training/customers/{id}/status` | 启用 / 停用 |
| GET | `/api/v1/admin/training/runs?page=&size=&status=&customerId=&q=` | 跨客户训练任务列表（结果桥） |
| GET | `/api/v1/admin/training/runs/{run_id}` | 训练任务详情（产物 + 结果清单） |
| POST | `/api/v1/admin/training/runs/{run_id}/import` | 把结果导入模型运营（生成模型版本/评估） |

安全语义：

- 节点的管理令牌只存在于 wmg-server 服务端，浏览器拿不到；
- 列表接口只返回 `tokenMasked`，只有创建 / 重置令牌时才返回一次明文，由管理员转交客户；
- 上游 401/403 被映射为 502（“节点管理令牌无效”），不会把平台管理员会话误判为过期踢回登录页；
- **平台到训练节点之间会传输管理令牌（请求头）与客户令牌明文（创建/重置响应）**，请使用 HTTPS，或把节点限制在受控内网；平台侧的 `TRAINHUB_VERIFY_TLS=false` 只用于自签证书的测试环境。

部署顺序：训练节点先按本文档部署并拿到 `TRAINHUB_ADMIN_TOKEN` → 在 wmg-server 配置上述变量并重启 → 重建 wmg-web 后即可在管理端「训练服务」菜单使用。

### 6.2 闭环执行协议（push + SSE）

TrainHub 是**单任务实例**，只接受外部调度方推送任务，不主动连接任何控制面：

1. 调度方调用 `POST /api/admin/executions`（multipart）提交一次闭环执行：
   - 文件：`code`（单个 `.ipynb` 或 zip，zip 内可带辅助模块）、`dataset`（可选 zip）、`assets`（可选 zip，评估场景的模型/基线目录）；
   - 表单：`name`、`entry`（zip 内入口 notebook，默认第一个 `.ipynb`）、`kernel`、`parameters`（JSON 对象）、`timeoutSeconds`、`baseModel`、`controlJobId`、`customerId`（执行归属客户：平台控制面传它配置的平台身份客户，客户用同一令牌登录控制台即可看到这次 API 发起的执行；缺省 `sys_platform`）；
   - 当前实例已有排队/运行任务时返回 `409`，不排队、不抢占。
2. 调度方携带 `runId` 调用 `GET /api/admin/runs/{runId}/logs/stream`，以 **SSE** 接收 `status` / `log` / `end` 事件；任务不存在返回 `404`，已结束返回 `409`。
3. 收到 `end` 后，调度方调用 `GET /api/admin/runs/{runId}` 查询结果、`GET /api/admin/runs/{runId}/outputs/download` 下载产物。
4. TrainHub 自己管理任务生命周期、日志与产物，数据落在 `TRAINHUB_DATA_DIR/customers/<归属客户>/runs/<runId>/`。
5. 平台任务默认挂在保留客户 `sys_platform`（不出现在客户列表）；平台控制面配置了**平台身份客户**时（`customerId`），任务归属该普通客户，管理员用同一客户令牌登录控制台即可看到这些执行。`GET /api/admin/runs` 的 `origin` 字段区分 `customer` / `platform`。

节点健康由控制面主动探测（默认每 60 秒）`GET /api/admin/executor/status`；TrainHub 不发送心跳、也不保存控制面地址。控制面任务与客户 Web UI 任务共用同一执行槽（`TRAINHUB_MAX_CONCURRENT_RUNS`，默认 1）。

### 6.3 节点部署下发（可选）

如果使用 WMG 平台，训练节点可以由控制面「部署下发」统一推送代码与配置：

1. 节点前置：Linux 节点安装 Docker（GPU 另装 nvidia-container-toolkit），用 `deploy/build-sandbox-image.sh base|trainer|cuda` 构建沙箱镜像，并在 `.env` 设置 `TRAINHUB_SANDBOX=docker` 与 `TRAINHUB_SANDBOX_IMAGE`；macOS/MLX 节点保持 `TRAINHUB_SANDBOX=none`（Docker 无法运行 MLX）。
2. 管理端配置：SSH 地址/端口/用户/私钥；「节点 API 地址 / API 令牌」需与节点侧 `TRAINHUB_ADMIN_TOKEN` 一致。
3. 点击「部署下发」：控制面打包本机 `wmg-trainhub` 源码并 SFTP 上传，远端创建 `trainhub` 用户与 venv、安装依赖、更新源码与 `trainhub.service`、写入 `/etc/trainhub/trainhub.env`、重启服务并探测健康。
4. 核对节点 API 地址；未配置 API 地址的节点不会被调度。建议用防火墙把 7470 限制为控制面/内网可达。
5. 验证：控制面探测节点在线且 kernels 含预期内核；提交训练作业，经 SSE 接收日志，结束后查询结果并下载产物。

`deploy/cuda-node-install.sh` 保留为离线/手工安装备用路径；正常部署不需要在节点上手工执行。

节点只需被控制面访问，不需要访问控制面的反向隧道。开发环境可用 SSH 本地转发把节点 7470 映射到控制面本机：

```bash
ssh -N -L 17470:127.0.0.1:7470 -p 20191 ubuntu@<node>
```

---

## 7. 部署与运维

### 7.1 Jupyter 运行环境

TrainHub 不定义、不修改 Jupyter 环境；节点管理员负责准备 Python/MLX/CUDA 环境与 kernelspec：

```bash
cd /opt/trainhub/wmg-trainhub
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

# 训练环境（示例：CUDA/PyTorch 解释器）
/opt/conda/envs/gpu/bin/python -m ipykernel install --user --name python3 \
  --display-name "Python 3 (Training)"
```

任务没有显式指定 `kernel` 时，TrainHub 交给 Jupyter 决定（notebook 的 `metadata.kernelspec.name`，否则 Jupyter 默认的 `python3`）。因此节点管理员只需让 Jupyter 的默认内核指向训练环境，或在 notebook metadata 里写明内核名；不需要在 WMG 或 TrainHub env 里配置默认内核。

> kernelspec 指向的解释器必须能 `import ipykernel`。客户在提交任务时仍可从内核下拉框选择；服务端只校验显式指定的内核是否存在。

**应用 venv 与训练 venv 分离**：`start.sh` 默认用 `.venv` 启动 TrainHub（只装 FastAPI/Jupyter 客户端），
Notebook 内核由 kernelspec 指向训练 venv（MLX/CUDA/transformers）。TrainHub 只向内核注入共享模型缓存
（`WMG_MODEL_CACHE` / `HF_HUB_CACHE` / `HF_XET_CACHE`）和 `trainhub_runtime` 助手，不修改内核环境本身。

#### macOS / MLX 训练 venv

MLX 节点的训练内核是**独立 venv**（不是 TrainHub 的 `.venv`），用 `deploy/macos-trainer-env.sh` 创建/更新：

```bash
# 以 TrainHub 服务用户执行（不要 sudo）；内核注册到该用户的 ~/Library/Jupyter/kernels
deploy/macos-trainer-env.sh --dry-run      # 先看计划
BASE_PYTHON=/opt/homebrew/bin/python3.11 deploy/macos-trainer-env.sh
```

- 干净 venv（不使用 `--system-site-packages`），依赖固定版本见 `deploy/trainer-requirements-macos.txt`
  （mlx / mlx-vlm / transformers / huggingface_hub / ipykernel）；
- `startlux-decide`：`/opt/trainhub/wheelhouse` 有 wheel 就用 wheel，否则钉 commit 从 git 安装；
- 注册用户级内核 `wmg-mlx`（`KERNEL_NAME` 可改），提交训练时在 `kernel` 或 notebook metadata 里选它；
- 安装结束做导入自检（`startlux_decide.prompt`、`mlx_vlm.trainer.peft`）并写 `requirements.lock`；
- 升级版本：改 requirements 后重跑脚本；`--recreate` 可删除旧 venv 重建；
- macOS 节点只能 `TRAINHUB_SANDBOX=none`，不要用 Docker 跑 MLX。

#### Docker 沙箱（Linux）

Linux 节点默认 `TRAINHUB_SANDBOX=docker`；macOS/MLX 节点默认且只能 `none`（显式 `docker` 会在启动时报错）。
沙箱把每个交互式内核和每个训练任务放进**独立的短生命周期容器**，不再以服务用户身份直接跑 Notebook 代码：

- **镜像**：`deploy/sandbox/Dockerfile`（CPU 最小内核；`trainer` 目标安装 `deploy/trainer-requirements.txt`）；
  `deploy/sandbox/Dockerfile.cuda` 是基于 CUDA 的训练镜像示例。构建：

  ```bash
  deploy/build-sandbox-image.sh            # base
  deploy/build-sandbox-image.sh trainer    # 含训练依赖
  deploy/build-sandbox-image.sh cuda       # CUDA 示例
  ```

  构建后把同一镜像名写进 `.env` 的 `TRAINHUB_SANDBOX_IMAGE`。
- **隔离参数**：`--user <服务uid:gid>`、`--read-only`、`--cap-drop ALL`、`--security-opt no-new-privileges`、
  `--pids-limit`、`--memory`、`--cpus`、`--network none`（默认）、`--tmpfs /tmp`；不挂 Docker socket、
  不挂宿主根目录、不传宿主环境变量。
- **挂载**：会话 `work`（读写，含 `output`）、`home`（读写）、IPC 目录（读写）、选中数据集的存储目录（只读）、
  共享模型缓存（默认只读）和仓库内 `runtime/` 助手（只读）。`.env`、SQLite、其他客户目录、代码仓库都不会出现。
- **内核协议**：宿主与容器内核通过 Unix socket（IPC）通信，因此 `--network none` 不影响内核控制通道；
  IPC 目录默认 `/var/tmp/trainhub-ipc`（要短，避免 Unix socket 路径上限），目录 0700。
- **模型**：网络 `none` 时容器内 `HF_HUB_OFFLINE=1`；先在节点执行
  `python -m trainhub.cli models pull ...`（训练 venv 解释器）预拉模型；共享模型缓存默认只读，避免一个
  Notebook 污染其他租户的模型。**Notebook 的运行时依赖（pip 包）必须预装在镜像里**（`trainer` 镜像安装
  `trainer-requirements.txt`；特殊依赖请扩展镜像），`--network none` 下不能临时 pip install/HF 下载。
- **数据集**：挂载的是创建内核时选中的数据集；Docker 模式下切换数据集不会热改容器挂载，**下一次运行会重建内核**
  （变量/导入状态丢失），本地 `none` 模式仍是热切换。
- **内核选择**：沙箱镜像自带 Python 内核，UI/metadata 里的宿主 kernelspec 名称不再决定运行时；镜像里的
  Python 由 `TRAINHUB_SANDBOX_PYTHON`（默认 `python3`）指定。
- **预检**：启动时检查 Docker 守护进程与镜像、服务非 root、IPC 目录可写；不满足时执行接口返回 503，
  **绝不回退到宿主执行**。`GET /api/admin/executor/status` 的 `sandbox` 字段回显模式、镜像与错误信息。
- **清理**：容器带 `com.wmg.trainhub.sandbox=1` 标签；正常关闭即 `rm -f`，服务启动/停止时按标签清理遗留容器。
- **GPU**：`TRAINHUB_SANDBOX_GPUS=all` 或 `device=0`；生产建议 MIG/按设备分配，避免单个 Notebook 占满 GPU。
- **验证**：在 Linux 节点可用真实 Docker 跑内核集成测试：

  ```bash
  TRAINHUB_TEST_DOCKER_SANDBOX=1 \
  TRAINHUB_SANDBOX_TEST_IMAGE=trainhub-sandbox:latest \
  .venv/bin/python -m pytest -q tests/test_sandbox_docker.py
  ```

- **macOS/MLX**：不要把 MLX 训练指向 Docker；macOS 节点保持 `none`，并把它当作可信客户/专用设备部署。

### 7.2 环境变量与启动

复制 `.env.example` 为 `.env`，按需修改。最少需要设置：

```bash
TRAINHUB_DATA_DIR=/data/trainhub
TRAINHUB_ADMIN_TOKEN=<openssl rand -hex 32>
```

完整配置项见 [9. 配置项](#9-配置项)。

### 7.3 systemd

`deploy/trainhub.service` 提供了一个可直接修改的单元示例：独立 `trainhub` 用户、`EnvironmentFile`、单进程启动。**不要添加 `--workers`**：执行队列与任务去重都在进程内，多 worker 会各自拉取同一批排队任务。

启用 Docker 沙箱时，rootful Docker 需要把 `trainhub` 用户加入 `docker` 组（单元文件里有 `SupplementaryGroups=docker` 注释行；docker 组等价 root 权限），或改用 rootless Docker；`docker.service` 已在单元依赖中声明。

### 7.4 反向代理与 TLS

`deploy/nginx.conf.example` 是 nginx 示例（HTTPS 终止 + 反代到 7470）。访问令牌是长期凭据，请只通过 HTTPS 暴露；如直连端口，请用防火墙限制来源。

#### 前端缓存与发布刷新

TrainHub 是零构建 SPA，没有带构建哈希的资源文件名，因此静态资源不能使用长缓存：

- `index.html`、`/assets/**/*.js`、`/assets/**/*.css` 由服务端发送 `Cache-Control: no-store, no-cache, must-revalidate`，浏览器每次加载都会重新获取。
- 字体文件发送 `Cache-Control: no-cache`，保留条件请求（304），但不会长期使用旧字体。
- 更新前端代码后，普通刷新页面即可拿到新版本；不需要重启 TrainHub（静态文件按请求从磁盘读取）。
- 如果浏览器在**本次缓存策略上线前**已经缓存了旧的启发式资源，第一次可能需要 `Ctrl/Cmd + Shift + R` 强制刷新一次；之后由 `no-store` 接管。
- 如果前置了 CDN/代理缓存，请对 `/assets/` 关闭缓存或改成带版本号的 URL；否则 CDN 可能先于浏览器返回旧文件。

### 7.5 磁盘布局与备份

```text
$TRAINHUB_DATA_DIR/
├── trainhub.db                  # SQLite（含 notebook_sessions 运行快照；WAL：另有 -wal / -shm）
├── model-cache/                 # 共享模型缓存（HF cache；所有 Notebook/训练任务共用）
├── executions/<run>/{code,dataset.zip}   # 平台控制面推送的本次输入（执行完回收）
├── sessions/<customer>/<notebook>/<session>/{work,home,tmp}  # 交互式内核临时目录（关闭即删）
└── customers/
    └── <customer>/
        ├── datasets/<dataset>/files/...
        ├── notebooks/<notebook>/<name>.ipynb   # 纯脚本；运行输出只在 Session 快照/导出副本中
        └── runs/<run>/{logs,output,environment.json,work,home,tmp}
```

备份建议：`sqlite3 trainhub.db ".backup backup.db"` 加整个 `customers/` 目录（低峰或停机时执行）；恢复时保持目录与数据库一致。数据不会自动过期清理，请结合节点磁盘容量配置上传上限与保留策略。

### 7.6 健康检查与重启

- 无令牌探活：`GET /healthz`，返回 `{"status":"ok"}`。
- 带令牌检查服务与 Jupyter：`GET /api/health` 或 `GET /api/kernels`。
- 重启：执行中的任务会中断并标记为失败（消息「服务重启，训练中断」），排队任务自动恢复执行。
- 进程日志：stdout（uvicorn 与 TrainHub 业务日志）；训练日志在各自任务的 `logs/run.log`。
- 磁盘水位：控制台概览显示客户存储与磁盘剩余；节点侧建议额外配置磁盘告警。

### 7.7 常见问题

| 现象 | 排查 |
|------|------|
| 客户创建成功但令牌登录无效 | CLI 与服务是否使用同一份 `.env` / `TRAINHUB_DATA_DIR` 指向同一个 SQLite。 |
| 提交训练返回 409 | 实例已有排队/运行任务（单任务实例），等待结束后再提交；平台推送与客户任务共用执行槽。 |
| 训练报“内核不可用” | 节点是否安装 Jupyter、kernelspec 是否存在、解释器是否能 `import ipykernel`。 |
| 运行详情没有指标曲线 | Notebook 是否写入 `WMG_METRICS_FILE`；文件是否为合法 JSONL；是否只有非数值字段。 |
| 其他客户看不到共享数据集 | 所有者是否切换为共享；数据集是否被删除；是否有活动任务引用。 |
| 管理 API 返回 503 | 节点是否配置 `TRAINHUB_ADMIN_TOKEN`；未配置时管理接口整体禁用。 |
| 平台代理返回 502 | 平台侧 `TRAINHUB_ADMIN_TOKEN` 是否与节点一致；节点是否可达；TLS 证书/`TRAINHUB_VERIFY_TLS` 是否正确。 |
| 更新前端后浏览器仍显示旧页面 | 先确认 `/assets/` 响应带 `no-store`；旧缓存首次用 `Ctrl/Cmd + Shift + R` 强制刷新；前置 CDN 是否缓存了 `/assets/`。 |

### 7.8 升级与迁移

- 服务启动时会对已知的 SQLite 列做幂等补列（如数据集共享、平台执行字段），存量库无需手工迁移。
- 这不是通用迁移框架；涉及表结构重写、数据搬迁或复杂升级时，请先备份数据库与 `customers/` 目录，再按版本说明操作。
- 升级前建议停服或确认没有运行中的任务；重启会中断执行中的内核。

---

## 8. API 参考

所有响应统一为 `{ code, message, data }`；分页数据为 `{ records, total, size, current, pages }`。除 `/healthz` 与静态资源外，所有客户接口都需要 `Authorization: Bearer <token>`。

### 8.1 当前客户

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/auth/me` | 当前客户信息（令牌脱敏）与训练计数 |
| GET | `/api/overview` | 概览：统计、存储、进行中任务、最近任务 |
| GET | `/api/kernels` | 可见内核列表与默认内核 |
| GET | `/api/activity` | 本客户操作审计（分页） |
| GET | `/api/health` | 服务与 Jupyter 状态（需令牌；`/healthz` 为无令牌探活） |

### 8.2 数据集

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/datasets?page=&size=&q=` | 列表：自己的 + 其他客户共享的（含所有者信息） |
| POST | `/api/datasets` | multipart：`files[]`（文件名可带相对路径）、`name`、`description`、`shared`（可选） |
| POST | `/api/datasets/{id}/extract` | 把数据集中的 ZIP 解压为目录内容（仅所有者；活动任务引用时 409） |
| GET | `/api/datasets/{id}` | 详情与文件列表（自己的或共享的） |
| GET | `/api/datasets/{id}/download` | 单文件直出，多文件打包 ZIP（共享数据集同样可下载） |
| PATCH | `/api/datasets/{id}/sharing` | 切换共享：`{"shared": true\|false}`（仅所有者） |
| DELETE | `/api/datasets/{id}` | 删除（仅所有者；任何人排队/执行中任务引用时返回 409） |

### 8.3 Notebooks

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/notebooks?page=&size=&q=` | 列表 |
| POST | `/api/notebooks` | JSON 手动新建：`{name?, content}`，`content` 是合法 `.ipynb` JSON 对象 |
| POST | `/api/notebooks/import` | multipart 导入：`file`（`.ipynb`）、`name`（可选） |
| GET | `/api/notebooks/{id}` | 详情 |
| GET | `/api/notebooks/{id}/download` | 下载 `.ipynb`：纯脚本 + 最新且源码匹配的 Session 快照输出 |
| GET | `/api/notebooks/{id}/content` | 可编辑的纯脚本 Notebook JSON（不含 `outputs` / `execution_count`） |
| PUT | `/api/notebooks/{id}` | 保存脚本：`{name?, content}`；忽略并清空 outputs；有活动任务时 409 |
| GET | `/api/notebooks/{id}/session` | 最新 Session 快照（含每 cell 的 `matchesScript`） |
| PUT | `/api/notebooks/{id}/session` | 保存 Session 快照：`{cells:[{cellId, source, executionCount, outputs}]}`；不改脚本 |
| GET | `/api/notebooks/{id}/last-run` | 该 Notebook 最近一次运行（提交训练时回填参数；没有则 `null`） |
| POST | `/api/notebooks/{id}/sessions` | 启动/复用交互式内核：`{kernel?}` |
| POST | `/api/notebooks/{id}/sessions/{session_id}/execute` | 执行单元格：`{code, cellId?, timeoutSeconds?}`，返回 `{status, executionCount, outputs, message}` |
| DELETE | `/api/notebooks/{id}/sessions/{session_id}` | 关闭该 Notebook 的交互式内核 |
| DELETE | `/api/notebooks/{id}` | 删除（有活动任务时 409） |

### 8.4 训练任务

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/runs?page=&size=&status=&q=` | 列表 |
| POST | `/api/runs` | `{notebookId, datasetId?, name?, kernel?, parameters?, timeoutSeconds?}` |
| GET | `/api/runs/{id}` | 详情（含产物清单、日志字节数与执行环境快照） |
| POST | `/api/runs/{id}/cancel` | 取消排队中 / 执行中的任务 |
| GET | `/api/runs/{id}/logs?offset=&limit=&tail=` | 增量日志（`tail=true` 取末尾片段） |
| GET | `/api/runs/{id}/metrics?maxPoints=` | 可选的训练指标系列（`metrics.jsonl`） |
| GET | `/api/runs/{id}/logs/download` | 下载完整日志 |
| GET | `/api/runs/{id}/outputs` | 产物清单 |
| GET | `/api/runs/{id}/outputs/download` | 打包下载产物 |
| GET | `/api/runs/{id}/outputs/file?path=` | 下载单个产物（拒绝路径穿越） |
| DELETE | `/api/runs/{id}` | 删除已结束的任务及其日志/产物 |

### 8.5 管理接口

管理接口统一需要 `X-Admin-Token: $TRAINHUB_ADMIN_TOKEN`（或同值的 Bearer 令牌）。完整清单见 [4.3](#43-客户与令牌管理)。

`GET /api/admin/models` 返回共享模型缓存概览（模型目录、总占用、各仓库大小与 refs），用于节点运维；
它只读，不含下载/删除操作，预热与清理通过 `trainhub.cli models` 或文件系统完成。

---

## 9. 配置项

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `TRAINHUB_DATA_DIR` | `<项目目录>/data` | 数据根目录 |
| `TRAINHUB_MODEL_DIR` | `$TRAINHUB_DATA_DIR/model-cache` | 共享模型缓存根目录（所有 Notebook/训练任务共用） |
| `TRAINHUB_DB_PATH` | `$TRAINHUB_DATA_DIR/trainhub.db` | SQLite 文件 |
| `TRAINHUB_ADMIN_TOKEN` | 空（禁用管理接口） | 管理令牌 |
| `TRAINHUB_HOST` / `TRAINHUB_PORT` | `0.0.0.0` / `7470` | 监听地址 |
| `TRAINHUB_MAX_CONCURRENT_RUNS` | `1` | 同时执行的任务数 |
| `TRAINHUB_RUN_TIMEOUT_SECONDS` | `0` | 默认整体时限（0 不限） |
| `TRAINHUB_CELL_TIMEOUT_SECONDS` | `0` | 训练任务单元格时限（0 不限） |
| `TRAINHUB_MAX_KERNEL_SESSIONS` | `4` | 编辑器交互式内核并发会话上限 |
| `TRAINHUB_KERNEL_SESSION_IDLE_SECONDS` | `1800` | 交互式内核空闲回收时间 |
| `TRAINHUB_KERNEL_CELL_TIMEOUT_SECONDS` | `300` | 编辑器运行单元格的整体时限 |
| `TRAINHUB_MAX_DATASET_MB` | `10240` | 单个数据集上传上限 |
| `TRAINHUB_MAX_NOTEBOOK_MB` | `10` | Notebook 大小上限（导入/保存） |
| `TRAINHUB_MAX_CODE_MB` | `200` | 平台推送代码包（.ipynb/.zip）上限 |
| `TRAINHUB_MAX_ASSETS_MB` | `10240` | 平台推送资产包（模型/基线目录）上限 |
| `TRAINHUB_MAX_LOG_MB` | `100` | 单任务日志上限 |
| `TRAINHUB_KEEP_RUN_WORKDIR` | `false` | 结束后是否保留工作目录 |
| `TRAINHUB_SANDBOX` | Linux `docker` / macOS `none` | 执行沙箱：`docker`（仅 Linux）或 `none`（同用户，可信环境） |
| `TRAINHUB_SANDBOX_IMAGE` | `trainhub-sandbox:latest` | 沙箱镜像名（需先构建/拉取） |
| `TRAINHUB_SANDBOX_DOCKER` | `docker` | Docker CLI 路径 |
| `TRAINHUB_SANDBOX_NETWORK` | `none` | 容器网络；`none` 时 HF 离线，模型需预拉 |
| `TRAINHUB_SANDBOX_MEMORY_MB` | `4096` | 每内核容器内存上限 |
| `TRAINHUB_SANDBOX_CPUS` | `2` | 每内核容器 CPU 上限 |
| `TRAINHUB_SANDBOX_PIDS_LIMIT` | `512` | 每内核容器 PID 上限 |
| `TRAINHUB_SANDBOX_GPUS` | 空 | `all` / `device=0`；CUDA 镜像使用 |
| `TRAINHUB_SANDBOX_RUN_AS` | 服务用户 `uid:gid` | 容器运行用户；uid 必须与服务用户一致；服务不能以 root 运行 |
| `TRAINHUB_SANDBOX_IPC_DIR` | `/var/tmp/trainhub-ipc` | IPC 目录（要短，Unix socket 路径限制） |
| `TRAINHUB_SANDBOX_MODEL_CACHE_RO` | `true` | 共享模型缓存是否只读挂载 |
| `TRAINHUB_SANDBOX_PYTHON` | `python3` | 镜像内启动内核的 Python |
| `TRAINHUB_KERNEL_ENV_ALLOW` | 空 | `none` 模式额外放行的宿主环境变量（逗号分隔） |
| `TRAINHUB_LOG_LEVEL` | `info` | 进程日志级别 |

---

## 10. 测试与自检

```bash
cd wmg-trainhub
.venv/bin/python -m pytest -q
node scripts/check-frontend.mjs
```

测试覆盖：鉴权、客户隔离、上传/下载、Notebook 手动新建与编辑、纯脚本存储与 Session 快照迁移/保存/导出挂载、交互式内核会话与单元格执行、Notebook 训练执行、SSE、单任务限制、取消、超时、路径穿越、平台闭环执行、执行器状态、静态资源、`.env` 加载、数据集共享、训练指标、参数回填、环境快照、Docker 沙箱参数/环境剥离/IPC 内核与清理。

`tests/test_runs.py` 会启动真实 Jupyter 内核执行 Notebook（依赖 venv 中的 `ipykernel`）。

---

## 11. 已知限制与后续方向

- **执行隔离**：Linux 节点已提供 Docker 沙箱（每内核/每训练任务一个临时容器，默认 `--network none`、只读根、丢弃 capability、只挂会话目录/选中数据集/模型缓存）；macOS/MLX 节点只能 `none`，属于可信设备模型。剩余风险：Docker 守护进程权限等价 root（建议 rootless）、共享模型缓存只读但仍可能被读取、绑定目录没有磁盘配额、GPU 尚无显存感知调度。
- **孤儿内核/容器**：正常停止或重启会清理；SIGKILL/断电时本地内核可能残留 `ipykernel_launcher`，Docker 容器会在下次启动时按标签清理。运维可用 `ps`/`docker ps --filter label=com.wmg.trainhub.sandbox=1` 检查。
- **调度**：FIFO + 固定并发；没有优先级、抢占、GPU 显存感知调度或跨节点调度。
- **配额与计量**：暂无每客户存储/算力配额与计费；只有上传大小与日志大小上限。
- **版本线**：Notebook、数据集和平台代码包都没有版本语义；Notebook 编辑是就地覆盖，没有历史版本或撤销，需要保留旧版本时请先下载或另存为新 Notebook。
- **迁移**：启动时按内置清单补列，不是通用迁移框架；复杂升级前请备份。
- **通知**：任务完成/失败暂无邮件或 Webhook 通知。
- **指标**：只读尾部 4 MB、每系列最多 2000 点；没有指标归档、对比或 TensorBoard 集成。
- **环境快照**：只记录服务环境与 kernelspec 标识，不采集内核实际 Python/依赖版本；需要完整复现时应在训练脚本中自行记录。
- **平台集成**：TrainHub 不主动连接控制面，不做心跳、不保存控制面地址；节点健康由控制面主动探测。
- **交互式内核**：每个 Notebook 一个会话，建模为临时工作目录；编辑器工具栏可选择数据集挂载到 `WMG_DATASET_DIR`，训练产物仍需通过训练任务生成。编辑器没有独立的 interrupt/restart 按钮（超时由服务端中断），离开编辑器或空闲超时会关闭会话。运行结果写回单元格后需要保存 Notebook 才持久化。
