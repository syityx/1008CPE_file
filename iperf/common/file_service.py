"""发送端文件准备控制服务；实际文件数据由真正iperf3 -F发送。"""
import json
from pathlib import Path
import socket
import socketserver
import subprocess
import threading
import time
import uuid

from standard_model import SPECS
from .file_source import source_manifest, write_batch
from .runner import HIDDEN


def request(branch, message, timeout=10):
    # 源地址绑定同时约束准备控制和iperf数据流，单路不访问另一条控制服务。
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(timeout)
        sock.bind((branch["bind_ip"], 0))
        sock.connect((branch["sender_ip"], branch["control_port"]))
        sock.sendall((json.dumps(message) + "\n").encode())
        with sock.makefile("rb") as stream:
            line = stream.readline(65537)
        if len(line) > 65536 or not line:
            raise ValueError("文件控制回复为空或过大。")
        response = json.loads(line)
        if not response.get("ok"):
            raise ValueError(response.get("error", "文件控制请求失败。"))
        return response


class FileService:
    def __init__(self, exe, folder, sources, listen_ip, branch, grace):
        self.exe, self.folder = exe, Path(folder)
        self.sources, self.listen_ip, self.branch = Path(sources), listen_ip, branch
        self.manifest = source_manifest(sources)
        self.grace = grace
        self.lock = threading.RLock()
        self.session = None
        self.closed = False

    def close(self):
        with self.lock:
            # 防止已接入但仍排队的控制线程在发送端退出后再启动iperf。
            self.closed = True
            self.cleanup()

    def cleanup(self):
        if self.session:
            process = self.session["process"]
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=3)
            self.session["log"].close()
            self.session["source"].unlink(missing_ok=True)
            self.session = None

    def expire(self):
        with self.lock:
            if self.session and time.monotonic() > self.session["deadline"]:
                self.cleanup()

    def handle(self, message):
        with self.lock:
            if self.closed:
                raise ValueError("发送端正在关闭。")
            self.expire()
            if not isinstance(message, dict):
                raise ValueError("控制报文须为对象。")
            op = message.get("op")
            if op == "manifest":
                return dict(ok=True, files=self.manifest, data_protocol="TCP", iperf_port=self.branch["port"])
            if op == "release":
                if self.session:
                    if message.get("session_id") != self.session["id"]:
                        raise ValueError("session_id不匹配，不能取消其他传输。")
                    self.cleanup()
                return dict(ok=True)
            if op != "prepare":
                raise ValueError("未知文件控制操作。")
            if self.session:
                raise ValueError("本链路已有未释放的测试会话，请等待或重启发送端。")
            profile, count = message.get("profile"), message.get("count")
            if profile not in SPECS or type(count) is not int or not 1 <= count <= 1000000:
                raise ValueError("标准类型或次数不合法。")
            offset, length = message.get("offset"), message.get("length")
            rate = message.get("rate_bps")
            if type(rate) is not int or rate <= 0 or type(length) is not int:
                raise ValueError("文件灌包速率或长度不合法。")
            if count * length > 128 * 1024 * 1024:
                raise ValueError("单批分段最多128MiB，请减小批次大小。")
            nominal_seconds = count * length * 8 / rate
            if nominal_seconds > 86400:
                raise ValueError("单批文件传输的预计时长不能超过24小时。")
            session_id = uuid.uuid4().hex
            path = self.folder / (session_id + ".source.bin")
            info = self.manifest[profile]
            digest = write_batch(self.sources / info["filename"], path, offset, length, count, info["sha256"])
            log = (self.folder / (session_id + ".iperf.json")).open("w", encoding="utf-8")
            command = [self.exe, "-4", "-s", "-1", "-B", self.listen_ip, "-p", str(self.branch["port"]),
                       "-F", str(path.resolve()), "-J"]
            process = subprocess.Popen(command, stdout=log, stderr=log, creationflags=HIDDEN)
            self.session = dict(id=session_id, process=process, log=log, source=path,
                                deadline=time.monotonic() + nominal_seconds + self.grace + 15)
            time.sleep(0.2)
            if process.poll() is not None:
                self.cleanup()
                raise ValueError("iperf文件服务启动失败，请检查端口占用和发送端日志。")
            reply = dict(ok=True, session_id=session_id, profile=profile, count=count, offset=offset,
                         length=length, bytes=count * length, stream_sha256=digest,
                         standard_file_sha256=info["sha256"], iperf_port=self.branch["port"])
            (self.folder / (session_id + ".manifest.json")).write_text(
                json.dumps(dict(reply, command=command), indent=2, ensure_ascii=False), encoding="utf-8")
            return reply


class ControlServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True
    block_on_close = True


class Handler(socketserver.StreamRequestHandler):
    def handle(self):
        self.connection.settimeout(15)
        try:
            line = self.rfile.readline(16385)
            if len(line) > 16384:
                raise ValueError("控制报文过大。")
            reply = self.server.service.handle(json.loads(line))
        except Exception as error:
            reply = dict(ok=False, error=str(error))
        try:
            self.wfile.write((json.dumps(reply, ensure_ascii=False) + "\n").encode("utf-8"))
        except OSError:
            pass


def start_service(service):
    server = ControlServer((service.listen_ip, service.branch["control_port"]), Handler)
    server.service = service
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread
