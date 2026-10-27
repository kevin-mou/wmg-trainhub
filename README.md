# TrainHub — Kaggle 风格单节点模型训练管理台

TrainHub 是 WMG 仓库下的独立子工程：每个客户凭**访问令牌**进入自己的工作台，上传数据集、提交 Notebook，在 GPU 节点的内置 Jupyter 内核上执行训练；训练日志实时落盘、随时下载，模型产物可单文件或打包下载。

控制面与执行面都在这一个实例内完成（FastAPI + SQLite + 进程内执行器），面向「一个或少量客户共享一台 GPU 节点」的部署形态，不依赖 Redis、消息队列或外部调度器。

## 需求对照

| # | 需求 | 实现 |
|---|------|------|
| 1 | 每个访问请求携带 token，按 token 识别用户 | 所有 `/api/**` 业务接口依赖 `Authorization: Bearer <token>`（或 `X-API-Token`），中间件依令牌解析客户；只有 `/healthz` 与静态资源无需令牌 |
| 2 | 每个客户一个 token，保存在数据表中 | `customers.token`（唯一索引），CLI / 管理 API 创建与重置 |
| 3 | 客户提交数据集 | 多文件 multipart 上传、不可变；运行时以符号链接挂载到 `dataset/`，支持原文件或 ZIP 下载 |
| 4 | 提交 Notebook，用 Jupyter 训练 | 校验 `.ipynb`（nbformat），训练任务通过 `nbclient` 在真实 Jupyter 内核中逐单元格执行 |
| 5 | 输出模型训练结果 | 产物目录 `OUTPUT_DIR`：列表、单文件下载、ZIP 打包；同时保留执行后的 `executed_notebook.ipynb` |
| 6 | 训练日志落盘，随时下载 | `logs/run.log` 增量写入（带时间戳、大小上限），支持分段读取、tail 与下载 |
| 7 | 为每个客户记录训练日志 | 训练记录按客户隔离；另有 `activity_logs` 操作审计（上传 / 提交 / 下载 / 删除） |
| 8 | 不区分控制面与操作面，集中在一个实例 | 同一个 FastAPI 进程提供 API、静态控制台与训练执行器；SQLite 单文件存储 |

## 架构

```
浏览器（零构建静态 SPA，中文 / English）
        │  Authorization: Bearer <customer token>
        ▼
FastAPI（单实例，uvicorn 1 worker）
  ├── 鉴权：customers.token → 当前客户
  ├── SQLite（WAL）：customers / datasets / notebooks / runs / activity_logs
  ├── 文件存储：data/customers/<customer>/...
  └── 训练执行器（asyncio 队列，并发数可配，默认 1）
         └── nbclient ── Jupyter kernel（独立进程）
```

- 训练任务状态以数据库为事实源：服务重启时，执行中的任务标记为失败，排队中的任务自动重新入队。
- 队列 FIFO；`TRAINHUB_MAX_CONCURRENT_RUNS` 控制同时执行数，单 GPU 节点建议保持 `1`。
- 控制台是纯静态 ES Module SPA（无 node 构建链），由 FastAPI 直接托管，GPU 节点离线也能使用。

## 快速开始（本地）

```bash
cd wmg-trainhub
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

cp .env.example .env    # 本地把 TRAINHUB_DATA_DIR 改成可写目录，并设置 TRAINHUB_ADMIN_TOKEN
PYTHONPATH=src .venv/bin/python -m trainhub.cli init
PYTHONPATH=src .venv/bin/python -m trainhub.cli create-customer --name "Acme Labs"

./start.sh          # 默认监听 0.0.0.0:7470
```

`trainhub.cli` 与 `start.sh` 都会读取子工程根目录的同一份 `.env`（进程环境优先）。**改了 `TRAINHUB_DATA_DIR` 后一定要让 CLI 与服务使用同一份 `.env`**：两者指向不同的库时，会出现"客户创建成功、令牌却登录无效"（一边写默认 `data/`、另一边读 `.env` 指定的目录）。

打开 `http://127.0.0.1:7470`，粘贴创建客户时输出的访问令牌即可进入控制台。

仓库自带示例 Notebook `examples/hello_trainhub.ipynb`：上传它（以及任意数据集），提交一次训练，就能看到实时日志、参数传递与产物下载的完整链路。

也可以直接用模块启动：

```bash
PYTHONPATH=src .venv/bin/python -m trainhub.cli serve --host 127.0.0.1 --port 7470
```

## 部署到 GPU 节点

### 1. 运行环境与训练内核

```bash
cd /opt/trainhub/wmg-trainhub
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

# 用真正带 CUDA / PyTorch 的解释器注册训练内核（推荐独立的 conda 环境）
/opt/conda/envs/gpu/bin/python -m ipykernel install --name gpu --display-name "Python (GPU)"
```

把 `TRAINHUB_DEFAULT_KERNEL` 设为 `gpu`。机器上没有任何 kernelspec 时，服务会为自身解释器自动注册一个 `python3` 内核，保证开箱可用；但生产训练环境建议显式注册并指定。

> kernelspec 指向的解释器必须能 `import ipykernel`。客户在提交任务时可以从内核下拉框中选择；服务端会校验内核是否存在。

### 2. 环境变量

复制 `.env.example` 为 `.env`，按需修改。最少需要设置：

```bash
TRAINHUB_DATA_DIR=/data/trainhub
TRAINHUB_ADMIN_TOKEN=<openssl rand -hex 32>
TRAINHUB_DEFAULT_KERNEL=gpu
```

完整配置项见下表。

### 3. systemd

`deploy/trainhub.service` 提供了一个可直接修改的单元示例：独立 `trainhub` 用户、`EnvironmentFile`、单进程启动。**不要添加 `--workers`**：执行队列与任务去重都在进程内，多 worker 会各自拉取同一批排队任务。

### 4. 反向代理与 TLS

`deploy/nginx.conf.example` 是 nginx 示例（HTTPS 终止 + 反代到 7470）。访问令牌是长期凭据，请只通过 HTTPS 暴露；如直连端口，请用防火墙限制来源。

## Notebook 运行契约

每个训练任务都在独立工作目录中执行：

```
data/customers/<customer>/runs/<run>/
├── work/
│   ├── notebook.ipynb     # 提交时的 Notebook 副本（内核 cwd）
│   └── dataset/           # 数据集的符号链接（未挂载时为空目录）
├── output/                # 产物目录；执行后的 executed_notebook.ipynb 也在这里
├── logs/run.log
├── home/                  # 内核 HOME / 缓存（任务结束后默认清理）
└── tmp/                   # 内核临时目录（任务结束后默认清理）
```

注入到内核的环境变量：

| 变量 | 含义 |
|------|------|
| `WMG_DATASET_DIR` / `DATASET_DIR` | 数据集目录（`work/dataset`） |
| `WMG_OUTPUT_DIR` / `OUTPUT_DIR` | 产物目录；写入这里的文件会出现在「产物」面板 |
| `WMG_PARAMS` | 提交任务时填写的参数，JSON 字符串 |
| `WMG_RUN_ID` / `WMG_RUN_NAME` | 当前任务 ID / 名称 |
| `WMG_CUSTOMER_ID` | 当前客户 ID |
| `WMG_DATASET_NAME` / `WMG_NOTEBOOK_NAME` | 数据集 / Notebook 显示名 |
| `WMG_WORK_DIR` | 内核工作目录 |

执行语义：

- 按 Notebook 顺序执行代码单元格；**遇到第一个异常即停止**，任务标记为失败，退出码 `1`，日志保留完整 traceback。
- 日志逐行带 UTC 时间戳，包含单元格边界与耗时、标准输出、`execute_result` 的文本表示、异常 traceback；超过 `TRAINHUB_MAX_LOG_MB` 后截断并注明。
- `TRAINHUB_RUN_TIMEOUT_SECONDS` / 任务级 `timeoutSeconds` 是整体时限，超时会终止内核；`TRAINHUB_CELL_TIMEOUT_SECONDS` 是单元格时限。两者默认 `0`（不限制）。
- 任务结束后默认清理 `work/`、`home/`、`tmp/`，只保留日志与 `output/`；设 `TRAINHUB_KEEP_RUN_WORKDIR=true` 可保留现场（会占用更多磁盘）。

## 客户与令牌

```bash
# 创建客户（输出访问令牌）
PYTHONPATH=src .venv/bin/python -m trainhub.cli create-customer --name "Acme Labs" --email ops@acme.test

# 列出客户
PYTHONPATH=src .venv/bin/python -m trainhub.cli list-customers

# 重置令牌 / 停用 / 启用
PYTHONPATH=src .venv/bin/python -m trainhub.cli rotate-token --id c_ab12cd34
PYTHONPATH=src .venv/bin/python -m trainhub.cli set-status --id c_ab12cd34 --status disabled
```

也可以使用管理 API（需要 `X-Admin-Token: $TRAINHUB_ADMIN_TOKEN`）：

- `POST /api/admin/customers`、`GET /api/admin/customers`
- `POST /api/admin/customers/{id}/rotate-token`
- `POST /api/admin/customers/{id}/status`
- `DELETE /api/admin/customers/{id}`（同时删除该客户全部文件，不可恢复）
- `GET /api/admin/overview`

令牌按需求**明文保存在数据表中**（`customers.token`）。这是长期凭据：管理 API 的客户列表也会返回令牌明文，只应通过受控网络访问；一旦泄露立即 `rotate-token`。控制台不会在页面 URL 中携带令牌，所有请求走请求头。

## 平台侧集成（P1，可选）

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

安全语义：

- 节点的管理令牌只存在于 wmg-server 服务端，浏览器拿不到；
- 列表接口只返回 `tokenMasked`，只有创建 / 重置令牌时才返回一次明文，由管理员转交客户；
- 上游 401/403 被映射为 502（"节点管理令牌无效"），不会把平台管理员会话误判为过期踢回登录页；
- **平台到训练节点之间仍会传输令牌明文**（TrainHub 管理 API 的响应），请使用 HTTPS，或把节点限制在受控内网；`TRAINHUB_VERIFY_TLS=false` 只用于自签证书的测试环境。

部署顺序：训练节点先按本文档部署并拿到 `TRAINHUB_ADMIN_TOKEN` → 在 wmg-server 配置上述变量并重启 → 重建 wmg-web 后即可在管理端「训练服务」菜单使用。

## API 参考

所有响应统一为 `{ code, message, data }`；分页数据为 `{ records, total, size, current, pages }`。除 `/healthz` 与静态资源外，所有接口都需要 `Authorization: Bearer <token>`。

### 当前客户

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/auth/me` | 当前客户信息（令牌脱敏）与训练计数 |
| GET | `/api/overview` | 概览：统计、存储、进行中任务、最近任务 |
| GET | `/api/kernels` | 可见内核列表与默认内核 |
| GET | `/api/activity` | 本客户操作审计（分页） |
| GET | `/api/health` | 服务与 Jupyter 状态（需令牌；`/healthz` 为无令牌探活） |

### 数据集

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/datasets?page=&size=&q=` | 列表：自己的 + 其他客户共享的（含所有者信息） |
| POST | `/api/datasets` | multipart：`files[]`、`name`、`description`、`shared`（可选） |
| GET | `/api/datasets/{id}` | 详情与文件列表（自己的或共享的） |
| GET | `/api/datasets/{id}/download` | 单文件直出，多文件打包 ZIP（共享数据集同样可下载） |
| PATCH | `/api/datasets/{id}/sharing` | 切换共享：`{"shared": true\|false}`（仅所有者） |
| DELETE | `/api/datasets/{id}` | 删除（仅所有者；任何人排队/执行中任务引用时返回 409） |

数据集默认私有；所有者可以在上传时勾选「共享给其他用户」，也可以之后在列表里切换。共享后其他客户可以在列表中看到、下载，并在提交训练时只读挂载到 `dataset/`。共享是**可信企业场景下的协作可见性**，不是安全边界（见「安全边界」）。

升级后重启 TrainHub 服务即可：SQLite 启动时会自动补 `datasets.visibility` 与 `runs.dataset_owner_*` 列，存量数据集默认私有，无需手工迁移。

### Notebooks

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/notebooks?page=&size=&q=` | 列表 |
| POST | `/api/notebooks` | multipart：`file`（`.ipynb`）、`name` |
| GET | `/api/notebooks/{id}` / `/download` | 详情 / 下载原文件 |
| DELETE | `/api/notebooks/{id}` | 删除（有活动任务时 409） |

### 训练任务

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/runs?page=&size=&status=&q=` | 列表 |
| POST | `/api/runs` | `{notebookId, datasetId?, name?, kernel?, parameters?, timeoutSeconds?}` |
| GET | `/api/runs/{id}` | 详情（含产物清单与日志字节数） |
| POST | `/api/runs/{id}/cancel` | 取消排队中 / 执行中的任务 |
| GET | `/api/runs/{id}/logs?offset=&limit=&tail=` | 增量日志（`tail=true` 取末尾片段） |
| GET | `/api/runs/{id}/logs/download` | 下载完整日志 |
| GET | `/api/runs/{id}/outputs` | 产物清单 |
| GET | `/api/runs/{id}/outputs/download` | 打包下载产物 |
| GET | `/api/runs/{id}/outputs/file?path=` | 下载单个产物（拒绝路径穿越） |
| DELETE | `/api/runs/{id}` | 删除已结束的任务及其日志/产物 |

## 磁盘布局与备份

```
$TRAINHUB_DATA_DIR/
├── trainhub.db                  # SQLite（WAL：另有 -wal / -shm）
└── customers/
    └── <customer>/
        ├── datasets/<dataset>/files/...
        ├── notebooks/<notebook>/<name>.ipynb
        └── runs/<run>/{logs,output,work,home,tmp}
```

备份建议：`sqlite3 trainhub.db ".backup backup.db"` 加整个 `customers/` 目录（在低峰或停机时执行）。恢复时保持目录与数据库一致。

数据不会自动过期清理：数据集、Notebook、训练日志与产物都由客户/管理员显式删除。请结合节点磁盘容量配置上传上限与保留策略。

## 运维

- 进程日志：stdout（uvicorn 与 TrainHub 业务日志）；训练日志在各自任务的 `logs/run.log`。
- 探活：`GET /healthz`（无需令牌，仅返回 `{"status":"ok"}`）；如要确认 Jupyter 与内核可用，用令牌请求 `/api/health` 或 `/api/kernels`。
- 重启：执行中的任务会中断并标记为失败（消息「服务重启，训练中断」），排队任务自动恢复执行。
- 磁盘水位：控制台概览显示客户存储与磁盘剩余；节点侧建议额外配置磁盘告警。

## 安全边界（部署前必读）

- **Notebook 是任意代码**：训练内核与服务进程使用同一个操作系统用户。不同客户之间**没有操作系统级隔离**——知道路径的 Notebook 可以读取本实例内其他客户的文件。本工程面向「可信客户 + 专用 GPU 节点」；若要对不可信客户开放，请为每个客户部署独立实例/独立 Unix 用户，或把执行器替换为容器化方案（见 Roadmap）。
- 数据集文件上传后设为只读（`0444`）并符号链接进工作目录，防止训练误写；这不是安全边界（同用户仍可 `chmod`）。数据集「共享/私有」同样只是应用层可见性，不能阻止 Notebook 直接按路径读取未共享的数据。
- 令牌是长期凭据：只走 HTTPS；数据库与备份文件权限收紧；泄露后立即重置。
- 上传上限：`TRAINHUB_MAX_DATASET_MB` / `TRAINHUB_MAX_NOTEBOOK_MB`；日志上限 `TRAINHUB_MAX_LOG_MB`。接口层未做请求频率限制，公网部署请在反向代理上限制速率与并发。
- 下载路径做了穿越校验；ZIP 打包不跟随符号链接，避免产物把节点上的外部文件打包出去。
- 管理接口在未配置 `TRAINHUB_ADMIN_TOKEN` 时整体禁用（503）。

## 配置项

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
| `TRAINHUB_MAX_LOG_MB` | `100` | 单任务日志上限 |
| `TRAINHUB_KEEP_RUN_WORKDIR` | `false` | 结束后是否保留工作目录 |
| `TRAINHUB_DEFAULT_KERNEL` | `python3` | 默认内核 |
| `TRAINHUB_LOG_LEVEL` | `info` | 进程日志级别 |

## 测试与自检

```bash
cd wmg-trainhub
.venv/bin/python -m pytest -q          # 38 项：鉴权、隔离、上传、执行、日志、取消、超时、路径穿越、静态资源、.env 加载
node scripts/check-frontend.mjs        # 前端模块引用 + i18n 键 + 本地资源自检
```

`tests/test_runs.py` 会启动真实 Jupyter 内核执行 Notebook（依赖 venv 中的 `ipykernel`）。

## 已知限制与后续方向

- 执行隔离：当前为同用户子进程；规划支持容器/namespace 隔离与每客户独立 UID。
- 进程被强制杀死（如 SIGKILL / 断电）时，运行中的 Jupyter 内核可能作为孤儿进程残留；正常停止或重启会清理。节点运维可用 `ps` 检查残留的 `ipykernel_launcher` 进程。
- 删除客户前必须先结束该客户的活动训练（接口会返回 409）。
- 调度：FIFO + 固定并发；没有优先级、抢占与 GPU 显存感知调度。
- 配额与计量：暂无每客户存储/算力配额与计费。
- Notebook 与数据集版本：当前每次上传是独立对象，没有版本线。
- 迁移：v1 通过 `CREATE TABLE IF NOT EXISTS` 建表，未引入迁移框架；升级前请备份，若涉及新增列需要手工 `ALTER TABLE` 或引入迁移脚本。
- 通知：任务完成/失败暂无邮件或 Webhook 通知。
