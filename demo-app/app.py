"""被流水线构建和部署的示例应用。改 MESSAGE 再 push,就能看到新版本上线。"""

from http.server import BaseHTTPRequestHandler, HTTPServer

MESSAGE = "hello from mini-ci v6"


def greeting() -> str:
    return MESSAGE


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        body = (greeting() + "\n").encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


if __name__ == "__main__":
    HTTPServer(("0.0.0.0", 8000), Handler).serve_forever()
