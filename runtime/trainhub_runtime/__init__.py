"""TrainHub 运行时助手：由平台注入到 Notebook 内核的稳定 API。

Notebook 不需要自己判断缓存目录、拼 huggingface_hub 参数或处理并发下载：

    from trainhub_runtime import ensure_model

    base_model_dir = ensure_model(
        "startlux-models/StartLux-Decision-v1.1-0.8B-Instant-MLX-8bit",
        required_files=["config.json"],
    )

缓存由 TrainHub 服务统一管理（``WMG_MODEL_CACHE``），所有 Notebook 会话与训练任务
共享同一份，重复运行不会重复下载。
"""

from .models import cached_model, ensure_model, model_cache_root

__all__ = ["cached_model", "ensure_model", "model_cache_root"]
