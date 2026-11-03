"""TrainHub 运行时助手：由平台注入到 Notebook 内核的稳定 API。

Notebook 不需要自己判断缓存目录、拼 huggingface_hub 参数或处理并发下载：

    from trainhub_runtime import ensure_model

    base_model_dir = ensure_model(
        "startlux-models/StartLux-Decision-v1.1-0.8B-Instant-MLX-8bit",
        required_files=["config.json"],
    )

缓存由 TrainHub 服务统一管理（``WMG_MODEL_CACHE``），所有 Notebook 会话与训练任务
共享同一份，重复运行不会重复下载。

数据集同样按名称读取（包括其他客户共享的数据集）：

    from trainhub_runtime import load_dataset

    dataset = load_dataset("wmg-mail-v3full-v1")
    dataset = load_dataset("c_ab12cd34/wmg-mail-v3full-v1")  # 共享数据集用 customer_id/名称

路径由服务端清单解析（``WMG_DATASET_CATALOG``），Notebook 不要自己拼
``customers/<客户>/datasets/<名称>``：共享数据集的文件目录属于所有者。
"""

from .datasets import (
    Dataset,
    DatasetError,
    dataset_info,
    dataset_path,
    list_datasets,
    load_dataset,
)
from .models import cached_model, ensure_model, model_cache_root

__all__ = [
    "Dataset",
    "DatasetError",
    "cached_model",
    "dataset_info",
    "dataset_path",
    "ensure_model",
    "list_datasets",
    "load_dataset",
    "model_cache_root",
]
