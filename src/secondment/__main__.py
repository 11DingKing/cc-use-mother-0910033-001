"""启动借调后端服务：python -m secondment [--host 127.0.0.1] [--port 8000]"""
from __future__ import annotations

import argparse

from .api import create_server
from .service import SecondmentService


def main() -> None:
    parser = argparse.ArgumentParser(description="跨场馆志愿者借调后端")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()
    server = create_server(SecondmentService(), args.host, args.port)
    host, port = server.server_address[:2]
    print(f"借调后端已启动：http://{host}:{port}（Ctrl+C 停止）")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
