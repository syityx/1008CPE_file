"""真实TCP文件反馈验收：人为限制CPE回环通路，观察下一份文件的分流变化。"""
from datetime import datetime
import json
import select
import socket
import subprocess
import sys
import threading
import time

from iperf.common.file_service import request
from iperf.common.runner import HIDDEN
from iperf.common.runtime import ROOT, resolve_iperf
from iperf.tests.validate_real import available_port


class TcpBridge:
    def __init__(self, port, bitrate=None):
        self.port, self.bitrate = port, bitrate
        self.stop = threading.Event()
        self.sockets = []
        self.threads = []

    def start(self):
        listener = socket.socket()
        listener.bind(("127.0.0.2", self.port))
        listener.listen()
        listener.settimeout(0.2)
        self.sockets.append(listener)
        self.launch(self.accept, listener)
        return self

    def launch(self, function, *args):
        thread = threading.Thread(target=function, args=args, daemon=True)
        self.threads.append(thread)
        thread.start()

    def accept(self, listener):
        while not self.stop.is_set():
            try:
                client, _ = listener.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            try:
                server = socket.create_connection(("127.0.0.1", self.port), timeout=3)
            except OSError:
                client.close()
                continue
            self.sockets.extend((client, server))
            self.launch(self.copy, client, server)

    def copy(self, client, server):
        deadline = time.monotonic()
        try:
            while not self.stop.is_set():
                readable, _, _ = select.select([client, server], [], [], 0.2)
                for source in readable:
                    payload = source.recv(65536)
                    if not payload:
                        return
                    if self.bitrate and source is server and len(payload) >= 1024:
                        deadline = max(deadline, time.monotonic()) + len(payload) * 8 / self.bitrate
                        if self.stop.wait(max(0, deadline - time.monotonic())):
                            return
                    (server if source is client else client).sendall(payload)
        except OSError:
            pass
        finally:
            client.close()
            server.close()

    def close(self):
        self.stop.set()
        for sock in list(self.sockets):
            sock.close()
        for thread in list(self.threads):
            thread.join(timeout=3)


def main():
    exe, _ = resolve_iperf()
    folder = ROOT / "adaptive" / "results" / datetime.now().strftime("file_feedback_%Y%m%d_%H%M%S_%f")
    folder.mkdir(parents=True)
    config = json.loads((ROOT / "adaptive" / "config.json").read_text())
    used = set()
    for name in ("lan", "cpe"):
        for key in ("port", "control_port"):
            port = available_port()
            while port in used:
                port = available_port()
            used.add(port)
            config[name][key] = port
    config.update(profile="large", count_limit=2, warmup_epochs=0)
    path = folder / "config.json"
    path.write_text(json.dumps(config), encoding="utf-8")
    stop = folder / "sender.stop"
    log = (folder / "sender.txt").open("w", encoding="utf-8")
    sender = subprocess.Popen([sys.executable, str(ROOT / "adaptive" / "send" / "main.py"),
                               "--config", str(path), "--iperf", exe, "--listen-ip", "127.0.0.1",
                               "--files-dir", str(folder / "sources"), "--stop-file", str(stop)],
                              stdout=log, stderr=log, creationflags=HIDDEN)
    bridges = []
    try:
        deadline = time.monotonic() + 15
        branch = dict(config["lan"], sender_ip="127.0.0.1", bind_ip="127.0.0.1")
        while True:
            try:
                request(branch, dict(op="manifest"), timeout=1)
                break
            except (OSError, ValueError):
                if time.monotonic() > deadline or sender.poll() is not None:
                    raise AssertionError("文件发送服务未就绪。")
                time.sleep(0.2)
        bridges.append(TcpBridge(config["cpe"]["control_port"]).start())
        bridges.append(TcpBridge(config["cpe"]["port"], bitrate=500000).start())
        output = folder / "receive"
        client = subprocess.run([sys.executable, str(ROOT / "adaptive" / "receive" / "main.py"),
                                 "--config", str(path), "--iperf", exe, "--loopback-test", "--output", str(output)],
                                capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=90)
        (folder / "receive.txt").write_text(client.stdout + client.stderr, encoding="utf-8")
        print(client.stdout)
        assert client.returncode == 0, client.stdout + client.stderr
        data = json.loads((output / "summary.json").read_text(encoding="utf-8"))
        assert data["completed"] and data["verified_files"] == {"large": 2}
        assert data["epochs"][0]["next_lan_ratio"] > 0.5
        assert data["epochs"][1]["lan_received_bytes"] > data["epochs"][0]["lan_received_bytes"]
        assert sum(data["received_bytes"].values()) == 3000000
        print("真实文件反馈通过：CPE限速后下一份文件的LAN分段增加，合并SHA256仍一致。", folder)
    finally:
        for bridge in bridges:
            bridge.close()
        stop.touch()
        sender.wait(timeout=20)
        log.close()
        assert sender.returncode == 0


if __name__ == "__main__":
    main()
