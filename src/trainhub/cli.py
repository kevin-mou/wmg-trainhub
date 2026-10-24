from __future__ import annotations

import argparse
import sys

from .config import PROJECT_ROOT, Settings
from .db import Database
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
    db = _open_database(settings)
    db.close()
    print(f"数据目录：{storage.root}")
    print(f"数据库：{settings.db_path}")
    print("初始化完成")
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
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args) or 0)


if __name__ == "__main__":
    raise SystemExit(main())
