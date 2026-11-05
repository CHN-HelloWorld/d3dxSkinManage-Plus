"""使用本地 HTTP 连接验证中断后换源、重定向与 Range 恢复。"""

from __future__ import annotations

import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from tests.services.app_update_download_helpers import make_session


async def test_socket_disconnect_resumes_through_redirect(tmp_path):
    payload = b"synthetic update contents" * 4000
    requests = []

    class Handler(BaseHTTPRequestHandler):
        """首源提前关闭 TCP，备用源通过临时地址返回实际字节范围。"""

        protocol_version = "HTTP/1.1"

        def do_GET(self):
            """验证真实 HTTP 层关闭连接后仍保留正确的续传偏移。"""
            requests.append((self.path, self.headers.get("Range")))
            if self.path == "/first":
                self.send_response(200)
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload[:8192])
                self.wfile.flush()
                self.close_connection = True
            elif self.path == "/second":
                self.send_response(302)
                self.send_header("Location", "/asset?ticket=current")
                self.send_header("Content-Length", "0")
                self.end_headers()
            else:
                self.send_response(206)
                self.send_header("Content-Range", f"bytes 8192-{len(payload) - 1}/{len(payload)}")
                self.send_header("Content-Length", str(len(payload) - 8192))
                self.end_headers()
                self.wfile.write(payload[8192:])

        def log_message(self, format, *args):
            """测试不输出服务器访问日志，断言保留完整请求记录。"""

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    try:
        result = await make_session(tmp_path, payload, [base + "/first", base + "/second"]).run()
        assert result.read_bytes() == payload
        assert requests == [("/first", None), ("/second", "bytes=8192-"), ("/asset?ticket=current", "bytes=8192-")]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(2)
        assert not thread.is_alive()
