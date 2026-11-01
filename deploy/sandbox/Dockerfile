# TrainHub Notebook 沙箱镜像（Linux / CPU）。
#
# 构建（在 wmg-trainhub 根目录）：
#   deploy/build-sandbox-image.sh base
#   deploy/build-sandbox-image.sh trainer
#
# 镜像只包含 Python/Jupyter 运行时与训练依赖；TrainHub 的代码、数据、.env、数据库都不会进入镜像。
# 容器由 TrainHub 以宿主服务用户的 uid:gid 启动，并只挂载会话目录、选中的数据集和共享模型缓存。

FROM python:3.11-slim AS base

ARG SANDBOX_UID=1000
ARG SANDBOX_GID=1000
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN groupadd -g "${SANDBOX_GID}" sandbox \
    && useradd --create-home --uid "${SANDBOX_UID}" --gid "${SANDBOX_GID}" sandbox

RUN python -m pip install --no-cache-dir \
    "ipykernel>=6.25" \
    "huggingface_hub>=0.23" \
    "filelock>=3.12"

USER sandbox
WORKDIR /tmp
CMD ["python3", "-m", "ipykernel_launcher", "--help"]


# 训练镜像：在 base 上安装 deploy/trainer-requirements.txt（torch/transformers 等）。
FROM base AS trainer

USER root
RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential git \
    && rm -rf /var/lib/apt/lists/*
COPY deploy/trainer-requirements.txt /tmp/trainer-requirements.txt
RUN python -m pip install --no-cache-dir -r /tmp/trainer-requirements.txt
USER sandbox
