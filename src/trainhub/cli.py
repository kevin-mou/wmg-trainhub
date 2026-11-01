from __future__ import annotations

import argparse
import sys

from .config import PROJECT_ROOT, Settings
from .db import Database
from .model_cache import model_cache_summary
from .runtime_env import RUNTIME_PACKAGE_ROOT, ensure_model_dirs
from .storage import Storage
from .utils import new_customer_token


def _open_database(settings: Settings) -> Database:
    storage = Storage(settings.data_dir)
    storage.ensure_base_dirs()
    return Database(settings.db_path)


def cmd_serve(args: argparse.Namespace) -> int:
    import uvicorn

    settings = Settings.from_env()
    host = args.host or settings.host
    port = args.port or settings.port
    if args.reload:
        uvicorn.run(
            "trainhub.app:app",
            host=host,
            port=port,
            reload=True,
            reload_dirs=[str(PROJECT_ROOT / "src")],
            log_level=settings.log_level,
        )
    else:
        from .main import create_app

        uvicorn.run(create_app(settings), host=host, port=port, log_level=settings.log_level)
    return 0


def cmd_init(args: argparse.Namespace) -> int:
    settings = Settings.from_env()
    storage = Storage(settings.data_dir)
    storage.ensure_base_dirs()
    model_dir = ensure_model_dirs(settings)
    db = _open_database(settings)
    db.close()
    print(f"数据目录：{storage.root}")
    print(f"数据库：{settings.db_path}")
    print(f"共享模型缓存：{model_dir}")
    print("初始化完成")
    return 0


def _human_size(size: int) -> str:
    value = float(size)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= 1024
    return f"{size} B"


def cmd_models_list(args: argparse.Namespace) -> int:
    settings = Settings.from_env()
    summary = model_cache_summary(settings)
    print(f"模型目录：{summary['modelDir']}（共 {_human_size(summary['totalBytes'])}）")
    if not summary["models"]:
        print("缓存为空；可用 `python -m trainhub.cli models pull <org/model>` 预下载。")
        return 0
    for item in summary["models"]:
        refs = ",".join(item["refs"]) or "-"
        print(
            f"{_human_size(item['sizeBytes']):>10}  {item['repoId']:<64}  "
            f"refs={refs:<10}  更新={item['updatedAt']}"
        )
    return 0


def cmd_models_pull(args: argparse.Namespace) -> int:
    settings = Settings.from_env()
    ensure_model_dirs(settings)
    runtime_path = str(RUNTIME_PACKAGE_ROOT)
    if runtime_path not in sys.path:
        sys.path.insert(0, runtime_path)
    from trainhub_runtime import ensure_model

    try:
        path = ensure_model(
            args.repo,
            revision=args.revision,
            cache_root=settings.model_dir_path,
            required_files=args.required_file,
            local_files_only=args.offline,
            refresh=args.refresh,
        )
    except ModuleNotFoundError as exc:
        if exc.name == "huggingface_hub":
            print(
                "当前解释器没有 huggingface_hub；请用训练环境解释器运行，例如 "
                "/opt/trainhub/trainer-venv/bin/python -m trainhub.cli models pull ...",
                file=sys.stderr,
            )
            return 1
        raise
    print(path)
    return 0


def cmd_create_customer(args: argparse.Namespace) -> int:
    settings = Settings.from_env()
    db = _open_database(settings)
    try:
        customer = db.create_customer(args.name.strip(), args.email, new_customer_token())
    finally:
        db.close()
    print(f"客户已创建：{customer['id']}  {customer['name']}")
    print(f"访问令牌：{customer['token']}")
    print("请把令牌交给客户；服务端只按令牌识别客户，令牌请妥善保管。")
    return 0


def cmd_list_customers(args: argparse.Namespace) -> int:
    settings = Settings.from_env()
    db = _open_database(settings)
    try:
        rows, total = db.list_customers(1, 200)
    finally:
        db.close()
    if not rows:
        print("暂无客户")
        return 0
    print(f"共 {total} 个客户")
    for row in rows:
        print(
            f"{row['id']}  {row['status']:<8}  {row['name']}  "
            f"数据集 {row['dataset_count']}  训练 {row['run_count']}"
        )
    return 0


def cmd_rotate_token(args: argparse.Namespace) -> int:
    settings = Settings.from_env()
    db = _open_database(settings)
    try:
        if db.get_customer(args.customer_id) is None:
            print(f"客户不存在：{args.customer_id}", file=sys.stderr)
            return 1
        customer = db.update_customer_token(args.customer_id, new_customer_token())
    finally:
        db.close()
    print(f"客户 {customer['id']} 的新访问令牌：{customer['token']}")
    return 0


def cmd_set_status(args: argparse.Namespace) -> int:
    settings = Settings.from_env()
    db = _open_database(settings)
    try:
        if db.get_customer(args.customer_id) is None:
            print(f"客户不存在：{args.customer_id}", file=sys.stderr)
            return 1
        db.update_customer_status(args.customer_id, args.status)
    finally:
        db.close()
    print(f"客户 {args.customer_id} 状态已更新为 {args.status}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="trainhub", description="TrainHub 训练管理台")
    sub = parser.add_subparsers(dest="command", required=True)

    serve = sub.add_parser("serve", help="启动 HTTP 服务")
    serve.add_argument("--host", default=None)
    serve.add_argument("--port", type=int, default=None)
    serve.add_argument("--reload", action="store_true", help="开发模式自动重载")
    serve.set_defaults(func=cmd_serve)

    init = sub.add_parser("init", help="初始化数据目录与数据库")
    init.set_defaults(func=cmd_init)

    create = sub.add_parser("create-customer", help="创建客户并输出访问令牌")
    create.add_argument("--name", required=True)
    create.add_argument("--email", default=None)
    create.set_defaults(func=cmd_create_customer)

    listing = sub.add_parser("list-customers", help="列出客户")
    listing.set_defaults(func=cmd_list_customers)

    rotate = sub.add_parser("rotate-token", help="重置客户访问令牌")
    rotate.add_argument("--id", dest="customer_id", required=True)
    rotate.set_defaults(func=cmd_rotate_token)

    status = sub.add_parser("set-status", help="启用 / 停用客户")
    status.add_argument("--id", dest="customer_id", required=True)
    status.add_argument("--status", choices=["active", "disabled"], required=True)
    status.set_defaults(func=cmd_set_status)

    models = sub.add_parser("models", help="共享模型缓存（服务级基础资源）")
    models_sub = models.add_subparsers(dest="models_command", required=True)
    models_list = models_sub.add_parser("list", help="列出已缓存模型与占用")
    models_list.set_defaults(func=cmd_models_list)
    models_pull = models_sub.add_parser("pull", help="把模型预下载到共享缓存")
    models_pull.add_argument("repo", help="Hugging Face 仓库 ID，如 org/model")
    models_pull.add_argument("--revision", default=None, help="分支 / tag / commit")
    models_pull.add_argument(
        "--required-file",
        action="append",
        dest="required_file",
        default=None,
        help="下载后必须存在的文件（可重复，如 --required-file config.json）",
    )
    models_pull.add_argument("--offline", action="store_true", help="只查缓存，不联网")
    models_pull.add_argument(
        "--refresh", action="store_true", help="跳过缓存命中判断，重新校验远端"
    )
    models_pull.set_defaults(func=cmd_models_pull)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args) or 0)


if __name__ == "__main__":
    raise SystemExit(main())
