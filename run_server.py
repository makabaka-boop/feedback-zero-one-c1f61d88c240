#!/usr/bin/env python3
"""启动 HTTP 服务。

用法:
  python3 run_server.py [--db merger.db] [--host 127.0.0.1] [--port 8080] [--init]

  --init  若目标数据尚未初始化, 用内置示例数据初始化(等价于 POST /init)。
"""

import argparse

from merger.server import make_server
from merger.service import MergeService


def main():
    parser = argparse.ArgumentParser(description="研究数据修订合并与审定交付服务")
    parser.add_argument("--db", default="merger.db", help="SQLite 数据库文件路径")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--init", action="store_true", help="目标未初始化时用内置示例数据初始化")
    args = parser.parse_args()

    service = MergeService(args.db)
    if args.init and not service.is_initialized():
        service.initialize()
        print(f"已用内置示例数据初始化目标数据(版本 1)")
    server = make_server(service, args.host, args.port)
    print(f"服务已启动: http://{args.host}:{args.port}  数据库: {args.db}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        service.close()


if __name__ == "__main__":
    main()
