"""HTTP dashboard with administrator and read-only access."""
import argparse
import contextlib
import hmac
import json
import mimetypes
import os
import secrets
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from .config import Config
from .runs import FIGURES, Runs
from .scheduler import Scheduler

STATIC = Path(__file__).parent / "static"


@contextlib.contextmanager
def state_lock(directory):
    with (directory / "panel.lock").open("a+b") as stream:
        if os.name == "nt":
            import msvcrt
            stream.seek(0, 2)
            if stream.tell() == 0:
                stream.write(b"0")
                stream.flush()
            stream.seek(0)
            msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def send_bytes(self, content, kind, status=200):
        self.send_response(status)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'self'; frame-ancestors 'none'; object-src 'none'; style-src 'self' 'unsafe-inline'")
        self.end_headers()
        self.wfile.write(content)

    def json(self, content, status=200):
        self.send_bytes(json.dumps(content, ensure_ascii=False, allow_nan=False).encode(), "application/json; charset=utf-8", status)

    def authorized(self, write=False):
        token = self.headers.get("X-Panel-Token", "")
        admin = hmac.compare_digest(token, self.server.admin_token)
        viewer = hmac.compare_digest(token, self.server.view_token)
        return admin if write else admin or viewer

    def file(self, path):
        self.send_bytes(path.read_bytes(), mimetypes.guess_type(path.name)[0] or "application/octet-stream")

    def do_GET(self):
        route = urlsplit(self.path)
        if route.path in ("/", "/app.js", "/styles.css"):
            self.file(STATIC / ("index.html" if route.path == "/" else route.path[1:]))
            return
        if not self.authorized():
            self.json({"error": "访问密钥无效，请用服务启动时的完整链接打开"}, 403)
            return
        args = {k: v[0] for k, v in parse_qs(route.query).items()}
        runs, cfg = self.server.runs, self.server.cfg
        try:
            if route.path == "/api/catalog":
                data = cfg.public()
                data["readonly"] = not self.authorized(write=True)
                self.json(data)
            elif route.path == "/api/state":
                self.json(self.server.queue.state())
            elif route.path == "/api/runs":
                self.json(runs.list())
            elif route.path == "/api/run":
                self.json(runs.describe(runs.path(args.get("id")), detail=True))
            elif route.path == "/api/log":
                self.json({"text": self.server.queue.log_tail(args.get("id"))})
            elif route.path == "/api/source":
                if not self.authorized(write=True):
                    self.json({"error": "只读链接无法读取源码"}, 403)
                    return
                script = cfg.entries[args["id"]]["script"]
                self.json({"path": script, "text": cfg.resolve(script).read_text(encoding="utf-8-sig")[:50000]})
            elif route.path == "/api/guide":
                self.json({"path": "使用指南", "text": (Path(__file__).parents[1] / "docs/使用指南.md").read_text(encoding="utf8")})
            elif route.path == "/api/figure" and args.get("name") in FIGURES:
                run = runs.path(args.get("id"))
                figure = (run / args["name"]).resolve()
                if not figure.is_relative_to(run):
                    raise ValueError("图片路径无效")
                self.file(figure)
            else:
                self.json({"error": "资源不存在"}, 404)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            self.json({"error": str(exc)}, 400)

    def do_POST(self):
        if not self.authorized(write=True):
            self.json({"error": "需要管理员链接"}, 403)
            return
        origin = self.headers.get("Origin")
        if origin and urlsplit(origin).netloc != self.headers.get("Host"):
            self.json({"error": "跨站请求被拒绝"}, 403)
            return
        try:
            size = int(self.headers.get("Content-Length", 0))
            if not 0 < size <= 32768 or self.headers.get_content_type() != "application/json":
                raise ValueError("请求体无效")
            data = json.loads(self.rfile.read(size))
            if self.path != "/api/control" or not isinstance(data, dict):
                raise ValueError("请求路径无效")
            q = self.server.queue
            action, ident = data.get("action"), data.get("id")
            if action == "add":
                q.add(data.get("task"))
            elif action in ("start", "pause"):
                q.switch(action == "start")
            elif action in ("up", "down", "remove"):
                q.queued_action(ident, action)
            elif action == "stop":
                q.stop(ident)
            else:
                raise ValueError("未知操作")
            self.json({"ok": True})
        except (OSError, ValueError, KeyError, TypeError) as exc:
            self.json({"error": str(exc)}, 400)


def main():
    parser = argparse.ArgumentParser(description="TrainBoard")
    parser.add_argument("--config", default="panel.json", type=Path)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", default=8766, type=int)
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()
    cfg = Config(args.config)
    with state_lock(cfg.state), ThreadingHTTPServer((args.host, args.port), Handler) as server:
        server.cfg, server.runs = cfg, Runs(cfg)
        server.admin_token = os.environ.get("TRAINBOARD_ADMIN_TOKEN") or secrets.token_urlsafe(32)
        server.view_token = os.environ.get("TRAINBOARD_VIEW_TOKEN") or secrets.token_urlsafe(32)
        if server.admin_token == server.view_token or min(len(server.admin_token), len(server.view_token)) < 16:
            raise ValueError("Admin/view tokens must differ and be at least 16 characters")
        server.queue = Scheduler(cfg)
        url = f"http://127.0.0.1:{server.server_port}/"
        print(f"TrainBoard 管理员 {url}#token={server.admin_token}", flush=True)
        print(f"TrainBoard 只读观测 {url}#token={server.view_token}", flush=True)
        print("保持此进程运行。退出时等待当前训练结束。", flush=True)
        if not args.no_browser:
            webbrowser.open(url + "#token=" + server.admin_token)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            server.queue.close()


if __name__ == "__main__":
    main()
