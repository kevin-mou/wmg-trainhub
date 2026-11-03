# 训练内核运行时（按 driver 隔离）

本目录声明 Docker 沙箱镜像里可用的 Jupyter 内核（`runtimes.json`），并给节点运维
提供按 driver 拆分的安装步骤。**内核是节点/镜像里的运行环境，不是新的子工程。**

## 目标内核

| 内核 | driver | 关键依赖 |
|---|---|---|
| `wmg-trainer` | Qwen / 默认 | 现有 `deploy/trainer-requirements.txt`（transformers main、peft、smol 训练栈） |
| `wmg-trainer-laya` | Laya | `laya==0.3.20`、`laya-finetune==0.1.2`、torch |
| `wmg-trainer-startlux` | StartLux | `transformers>=5.8,<5.9`、`startlux-decide`、`mlx-vlm>=0.7.6`（MLX） |

三个 driver 的 transformers 版本互斥，**不能共用同一个 venv**。

## Docker 模式（Linux 节点默认）

1. 在沙箱镜像里为每个 driver 建独立 venv，并分别
   `python -m ipykernel install --name <kernel> --prefix /usr/local`。
2. 镜像启动后 TrainHub 从 `deploy/runtimes/runtimes.json` 读取可用内核；
   也可用 `TRAINHUB_SANDBOX_KERNELS=wmg-trainer,wmg-trainer-laya,wmg-trainer-startlux`
   显式覆盖（改配置后重启服务）。
3. 构建/更新镜像后，跑一次 dryRun 任务验证目标 kernel；run 的环境快照会记录
   实际 kernel 与包版本。

## 宿主模式（`TRAINHUB_SANDBOX=none`，macOS/MLX）

`prepare-trainer-env.sh` 仍注册默认 `wmg-trainer`；新增 driver 时按同样方式建独立
venv + `ipykernel install`，宿主模式直接通过 `jupyter kernelspec list` 发现。

## 控制面约束

WMG 控制面的 driver 元数据只需声明 kernel 名；调度会校验节点 `health.kernels`，
请求的 kernel 不在节点上时不会被派发（错误信息会提示先部署运行环境）。
