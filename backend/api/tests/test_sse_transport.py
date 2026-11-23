import threading
import zlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import requests
from django.test import SimpleTestCase

from api.llm import iter_sse_lines


class SseTransportTests(SimpleTestCase):
    def test_first_line_is_available_before_response_finishes(self):
        first_line = 'data: {"choices":[{"delta":{"content":"即时中文正文"}}]}'
        for framing in ("length", "close", "chunked"):
            for compressed in (False, True):
                with self.subTest(framing=framing, compressed=compressed):
                    release_tail = threading.Event()
                    first_read = threading.Event()
                    lines, errors = [], []
                    first = (first_line + "\n\n").encode("utf-8")
                    tail = b"data: [DONE]\n\n"
                    if compressed:
                        compressor = zlib.compressobj(wbits=31)
                        first = compressor.compress(first) + compressor.flush(zlib.Z_SYNC_FLUSH)
                        tail = compressor.compress(tail) + compressor.flush()

                    class Handler(BaseHTTPRequestHandler):
                        protocol_version = "HTTP/1.1" if framing == "chunked" else "HTTP/1.0"

                        def do_GET(self):
                            self.send_response(200)
                            self.send_header("Content-Type", "text/event-stream")
                            if compressed:
                                self.send_header("Content-Encoding", "gzip")
                            if framing == "length":
                                self.send_header("Content-Length", str(len(first) + len(tail)))
                            elif framing == "chunked":
                                self.send_header("Transfer-Encoding", "chunked")
                            self.end_headers()
                            try:
                                self.send_part(first)
                                release_tail.wait(5)
                                self.send_part(tail)
                                if framing == "chunked":
                                    self.wfile.write(b"0\r\n\r\n")
                                    self.wfile.flush()
                            except OSError:
                                pass

                        def send_part(self, data):
                            if framing == "chunked":
                                data = f"{len(data):x}\r\n".encode() + data + b"\r\n"
                            self.wfile.write(data)
                            self.wfile.flush()

                        def log_message(self, *args):
                            pass

                    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
                    server.daemon_threads = True
                    server_thread = threading.Thread(target=lambda: server.serve_forever(poll_interval=.01), daemon=True)
                    server_thread.start()
                    session = requests.Session()
                    session.trust_env = False
                    response = None
                    reader = None
                    try:
                        response = session.get(f"http://127.0.0.1:{server.server_port}/", stream=True, timeout=4)

                        def consume():
                            try:
                                for line in iter_sse_lines(response):
                                    lines.append(line)
                                    first_read.set()
                            except Exception as exc:
                                errors.append(type(exc).__name__)

                        reader = threading.Thread(target=consume, daemon=True)
                        reader.start()
                        arrived_before_finish = first_read.wait(2)
                    finally:
                        release_tail.set()
                        if reader is not None:
                            reader.join(4)
                        if response is not None:
                            response.close()
                        session.close()
                        server.shutdown()
                        server.server_close()
                        server_thread.join(2)
                    self.assertTrue(arrived_before_finish, "正文已发送，却等到响应结束才被读取")
                    self.assertFalse(errors)
                    self.assertEqual(lines, [first_line, "data: [DONE]"])
