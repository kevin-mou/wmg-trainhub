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
- **训练可交付**：上传数据集和 Notebook，提交任务，看到实时日志、训练指标、执行环境与产物。
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

### 3.3 调度与资源

- 单实例、FIFO、固定并发；默认 `TRAINHUB_MAX_CONCURRENT_RUNS=1`。
- 没有优先级、抢占、公平调度、GPU 显存感知或跨节点调度。
- 平台推送任务与客户 Web UI 任务共用执行槽；实例已有排队/运行任务时，新的平台推送返回 `409`，不排队、不抢占。
- 节点硬件（CPU/GPU/磁盘/内核）可通过管理 API `GET /api/admin/executor/status` 只读查询，但 TrainHub 不做资源预留。
- 平台控制面通过主动探测该端点判断节点忙闲；TrainHub 不发送心跳、不保存控制面地址。

### 3.4 数据、日志与保留

- 数据集、Notebook、训练日志、训练指标、环境快照和产物都保存在 `TRAINHUB_DATA_DIR` 下。
- 数据不会自动过期；客户或管理员显式删除时才释放。请结合节点磁盘容量配置上传上限与备份策略。
- 训练日志写入 `logs/run.log`，超过 `TRAINHUB_MAX_LOG_MB` 后截断并注明；支持增量读取、tail 与下载。
- 训练指标写入可选的 `logs/metrics.jsonl`，只用于过程曲线；服务只读取尾部，但文件本身仍占用磁盘。
- 运行目录默认只保留日志、环境快照与 `output/`；`work/`、`home/`、`tmp/` 在任务结束后清理（`TRAINHUB_KEEP_RUN_WORKDIR=true` 可保留现场）。
- 备份建议：`sqlite3 trainhub.db ".backup backup.db"` 加整个 `customers/` 目录，在低峰或停机时执行；恢复时保持目录与数据库一致。

### 3.5 结果与版本语义

- TrainHub **只负责执行**：不保存/解释客户 Notebook 的版本语义，也不保存平台推送代码包的版本线；版本由控制面作业记录。
- 客户上传的 Notebook 和数据集是独立对象，没有版本线；每次上传是一个新对象。
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

也可以直接用模块启动：

```bash
PYTHONPATH=src .venv/bin/python -m trainhub.cli serve --host 127.0.0.1 --port 7470
```

`trainhub.cli` 与 `start.sh` 都读取子工程根目录的同一份 `.env`（进程环境优先）。**改了 `TRAINHUB_DATA_DIR` 后一定要让 CLI 与服务使用同一份 `.env`**：两者指向不同的库时，会出现“客户创建成功、令牌却登录无效”。

仓库自带示例 Notebook `examples/hello_trainhub.ipynb`：上传它（以及任意数据集），提交一次训练，就能看到实时日志、参数传递、训练指标曲线与产物下载的完整链路。

### 4.2 客户工作台

登录后左侧导航包括：

- **概览**：客户信息、数据集/Notebook/运行统计、存储水位、进行中任务与最近任务。
- **数据集**：上传多文件数据集；默认私有；所有者可以共享/取消共享、下载、删除。
- **Notebooks**：上传 `.ipynb`、下载原文件、删除（有活动任务时返回 `409`）。
- **训练任务**：提交、查看、取消、删除运行。
- **活动记录**：上传、提交、下载、删除等操作审计。
- **账户**：客户资料、令牌脱敏展示、语言/主题切换。

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

- `POST /api/admin/customers`、`GET /api/admin/customers`
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
- `POST /api/admin/executions`（平台控制面推送的一次闭环执行）

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
| `WMG_BASE_MODEL` | 平台任务声明的基座标识；由训练脚本自行加载/下载 |
| `WMG_ASSETS_DIR` / `WMG_MODEL_DIR` / `WMG_BASELINE_MODEL_DIR` | 平台资产包目录；存在时注入 |

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
   - 表单：`name`、`entry`（zip 内入口 notebook，默认第一个 `.ipynb`）、`kernel`、`parameters`（JSON 对象）、`timeoutSeconds`、`baseModel`、`controlJobId`；
   - 当前实例已有排队/运行任务时返回 `409`，不排队、不抢占。
2. 调度方携带 `runId` 调用 `GET /api/admin/runs/{runId}/logs/stream`，以 **SSE** 接收 `status` / `log` / `end` 事件；任务不存在返回 `404`，已结束返回 `409`。
3. 收到 `end` 后，调度方调用 `GET /api/admin/runs/{runId}` 查询结果、`GET /api/admin/runs/{runId}/outputs/download` 下载产物。
4. TrainHub 自己管理任务生命周期、日志与产物，数据落在 `TRAINHUB_DATA_DIR/customers/sys_platform/runs/<runId>/`。
5. 平台任务挂在保留客户 `sys_platform` 下，不出现在客户列表；`GET /api/admin/runs` 的 `origin` 字段区分 `customer` / `platform`。

节点健康由控制面主动探测（默认每 60 秒）`GET /api/admin/executor/status`；TrainHub 不发送心跳、也不保存控制面地址。控制面任务与客户 Web UI 任务共用同一执行槽（`TRAINHUB_MAX_CONCURRENT_RUNS`，默认 1）。

### 6.3 节点部署下发（可选）

如果使用 WMG 平台，训练节点可以由控制面「部署下发」统一推送代码与配置：

1. 节点前置：安装 docker + nvidia-container-toolkit，并准备训练镜像；训练依赖与 Jupyter kernel 由节点管理员预置。
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

### 7.2 环境变量与启动

复制 `.env.example` 为 `.env`，按需修改。最少需要设置：

```bash
TRAINHUB_DATA_DIR=/data/trainhub
TRAINHUB_ADMIN_TOKEN=<openssl rand -hex 32>
```

完整配置项见 [9. 配置项](#9-配置项)。

### 7.3 systemd

`deploy/trainhub.service` 提供了一个可直接修改的单元示例：独立 `trainhub` 用户、`EnvironmentFile`、单进程启动。**不要添加 `--workers`**：执行队列与任务去重都在进程内，多 worker 会各自拉取同一批排队任务。

### 7.4 反向代理与 TLS

`deploy/nginx.conf.example` 是 nginx 示例（HTTPS 终止 + 反代到 7470）。访问令牌是长期凭据，请只通过 HTTPS 暴露；如直连端口，请用防火墙限制来源。

### 7.5 磁盘布局与备份

```text
$TRAINHUB_DATA_DIR/
├── trainhub.db                  # SQLite（WAL：另有 -wal / -shm）
├── executions/<run>/{code,dataset.zip}   # 平台控制面推送的本次输入（执行完回收）
└── customers/
    └── <customer>/
        ├── datasets/<dataset>/files/...
        ├── notebooks/<notebook>/<name>.ipynb
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
| POST | `/api/datasets` | multipart：`files[]`、`name`、`description`、`shared`（可选） |
| GET | `/api/datasets/{id}` | 详情与文件列表（自己的或共享的） |
| GET | `/api/datasets/{id}/download` | 单文件直出，多文件打包 ZIP（共享数据集同样可下载） |
| PATCH | `/api/datasets/{id}/sharing` | 切换共享：`{"shared": true\|false}`（仅所有者） |
| DELETE | `/api/datasets/{id}` | 删除（仅所有者；任何人排队/执行中任务引用时返回 409） |

### 8.3 Notebooks

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/notebooks?page=&size=&q=` | 列表 |
| POST | `/api/notebooks` | multipart：`file`（`.ipynb`）、`name` |
| GET | `/api/notebooks/{id}` / `/download` | 详情 / 下载原文件 |
| GET | `/api/notebooks/{id}/last-run` | 该 Notebook 最近一次运行（提交训练时回填参数；没有则 `null`） |
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

---

## 9. 配置项

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `TRAINHUB_DATA_DIR` | `<项目目录>/data` | 数据根目录 |
| `TRAINHUB_DB_PATH` | `$TRAINHUB_DATA_DIR/trainhub.db` | SQLite 文件 |
| `TRAINHUB_ADMIN_TOKEN` | 空（禁用管理接口） | 管理令牌 |
| `TRAINHUB_HOST` / `TRAINHUB_PORT` | `0.0.0.0` / `7470` | 监听地址 |
| `TRAINHUB_MAX_CONCURRENT_RUNS` | `1` | 同时执行的任务数 |
| `TRAINHUB_RUN_TIMEOUT_SECONDS` | `0` | 默认整体时限（0 不限） |
| `TRAINHUB_CELL_TIMEOUT_SECONDS` | `0` | 单元格时限（0 不限） |
| `TRAINHUB_MAX_DATASET_MB` | `10240` | 单个数据集上传上限 |
| `TRAINHUB_MAX_NOTEBOOK_MB` | `10` | Notebook 上传上限 |
| `TRAINHUB_MAX_CODE_MB` | `200` | 平台推送代码包（.ipynb/.zip）上限 |
| `TRAINHUB_MAX_ASSETS_MB` | `10240` | 平台推送资产包（模型/基线目录）上限 |
| `TRAINHUB_MAX_LOG_MB` | `100` | 单任务日志上限 |
| `TRAINHUB_KEEP_RUN_WORKDIR` | `false` | 结束后是否保留工作目录 |
| `TRAINHUB_LOG_LEVEL` | `info` | 进程日志级别 |

---

## 10. 测试与自检

```bash
cd wmg-trainhub
.venv/bin/python -m pytest -q
node scripts/check-frontend.mjs
```

测试覆盖：鉴权、客户隔离、上传/下载、Notebook 执行、SSE、单任务限制、取消、超时、路径穿越、平台闭环执行、执行器状态、静态资源、`.env` 加载、数据集共享、训练指标、参数回填、环境快照。

`tests/test_runs.py` 会启动真实 Jupyter 内核执行 Notebook（依赖 venv 中的 `ipykernel`）。

---

## 11. 已知限制与后续方向

- **执行隔离**：当前为同用户子进程；规划支持容器/namespace 隔离与每客户独立 UID。
- **孤儿内核**：进程被强制杀死（SIGKILL/断电）时，运行中的 Jupyter 内核可能残留；正常停止或重启会清理。运维可用 `ps` 检查 `ipykernel_launcher`。
- **调度**：FIFO + 固定并发；没有优先级、抢占、GPU 显存感知调度或跨节点调度。
- **配额与计量**：暂无每客户存储/算力配额与计费；只有上传大小与日志大小上限。
- **版本线**：Notebook、数据集和平台代码包都没有版本语义；版本由客户或控制面自行管理。
- **迁移**：启动时按内置清单补列，不是通用迁移框架；复杂升级前请备份。
- **通知**：任务完成/失败暂无邮件或 Webhook 通知。
- **指标**：只读尾部 4 MB、每系列最多 2000 点；没有指标归档、对比或 TensorBoard 集成。
- **环境快照**：只记录服务环境与 kernelspec 标识，不采集内核实际 Python/依赖版本；需要完整复现时应在训练脚本中自行记录。
- **平台集成**：TrainHub 不主动连接控制面，不做心跳、不保存控制面地址；节点健康由控制面主动探测。
