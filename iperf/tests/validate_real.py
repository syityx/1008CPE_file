"""真实 iperf 本机验收。UDP中间桥仅用于施加丢包，不是工程的灌包器。

python -m iperf.tests.validate_real
两路反向UDP、固定模式不调节、反馈实际改变下一轮-b、单路断连失败均验证。
不能用本机结果代替物理Wi-Fi/CPE专网验收。
"""
import json
from pathlib import Path
import select
import socket
import subprocess
import sys
import threading
import time

from iperf.common.runner import HIDDEN
from iperf.common.runtime import ROOT, resolve_iperf


def available_port(host="127.0.0.1"):
    with socket.socket() as sock:
        sock.bind((host, 0))
        return sock.getsockname()[1]


class LossBridge:
    """转发真实TCP控制和UDP数据，每4个服务端数据报丢1个；控制报文不丢。"""
    def __init__(self, frontend, backend):
        self.frontend, self.backend = frontend, backend
        self.stop = threading.Event()
        self.sockets = []
        self.threads = []
        self.dropped = 0

    def launch(self, target, *args):
        thread = threading.Thread(target=target, args=args, daemon=True)
        self.threads.append(thread)
        thread.start()

    def start(self):
        self.tcp = socket.socket()
        self.tcp.bind(("127.0.0.2", self.frontend))
        self.tcp.listen()
        self.tcp.settimeout(0.2)
        self.udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.udp.bind(("127.0.0.2", self.frontend))
        self.back = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.back.bind(("127.0.0.1", 0))
        self.sockets.extend((self.tcp, self.udp, self.back))
        self.launch(self.accept)
        self.launch(self.udp_loop)
        return self

    def accept(self):
        while not self.stop.is_set():
            try:
                client, _ = self.tcp.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            try:
                server = socket.create_connection(("127.0.0.1", self.backend), timeout=3)
            except OSError:
                client.close()
                continue
            self.sockets.extend((client, server))
            self.launch(self.tcp_loop, client, server)

    def tcp_loop(self, client, server):
        try:
            while not self.stop.is_set():
                readable, _, _ = select.select([client, server], [], [], 0.2)
                for source in readable:
                    data = source.recv(65536)
                    if not data:
                        return
                    (server if source is client else client).sendall(data)
        except OSError:
            pass
        finally:
            client.close()
            server.close()

    def udp_loop(self):
        destination = None
        count = 0
        try:
            while not self.stop.is_set():
                readable, _, _ = select.select([self.udp, self.back], [], [], 0.2)
                for source in readable:
                    payload, peer = source.recvfrom(65535)
                    if source is self.udp:
                        destination = peer
                        self.back.sendto(payload, ("127.0.0.1", self.backend))
                    elif destination:
                        if len(payload) >= 64:
                            count += 1
                            if count % 4 == 0:
                                self.dropped += 1
                                continue
                        self.udp.sendto(payload, destination)
        except (OSError, ValueError):
            pass

    def close(self):
        self.stop.set()
        for sock in list(self.sockets):
            sock.close()
        for thread in list(self.threads):
            thread.join(timeout=2)


def main():
    exe, version = resolve_iperf()
    from datetime import datetime
    folder = ROOT / "adaptive" / "results" / datetime.now().strftime("validation_%Y%m%d_%H%M%S_%f")
    folder.mkdir(parents=True)
    lan, backend, frontend = available_port(), available_port(), available_port("127.0.0.2")
    while len({lan, backend, frontend}) != 3:
        backend, frontend = available_port(), available_port("127.0.0.2")
    processes = []
    logs = []
    bridge = None
    record = {"iperf": version, "physical_network_test": False, "checks": []}
    try:
        for label, port in (("lan", lan), ("backend", backend)):
            log = (folder / (label + "_server.log")).open("w", encoding="utf-8")
            logs.append(log)
            processes.append(subprocess.Popen([exe, "-s", "-4", "-B", "127.0.0.1", "-p", str(port)],
                                              stdout=log, stderr=log, creationflags=HIDDEN))
        time.sleep(0.4)
        assert all(p.poll() is None for p in processes), "iperf服务端启动失败"
        bridge = LossBridge(frontend, backend).start()
        for mode in ("fixed", "adaptive"):
            config = json.loads((ROOT / mode / "config.json").read_text(encoding="utf-8"))
            config.update(duration_seconds=6 if mode == "adaptive" else 3, epoch_seconds=2, warmup_epochs=0)
            config["lan"]["port"], config["cpe"]["port"] = lan, frontend
            path = folder / (mode + "_config.json")
            path.write_text(json.dumps(config), encoding="utf-8")
            output = folder / mode
            result = subprocess.run([sys.executable, str(ROOT / mode / "receive" / "main.py"),
                                     "--config", str(path), "--iperf", exe, "--loopback-test", "--output", str(output)],
                                    capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60)
            (folder / (mode + "_console.txt")).write_text(result.stdout + result.stderr, encoding="utf-8")
            print(result.stdout, flush=True)
            assert result.returncode == 0, result.stdout + result.stderr
            summary = json.loads((output / "summary.json").read_text(encoding="utf-8"))
            assert summary["completed"] and min(summary["received_bytes"].values()) > 0
            assert any(e["cpe_loss_percent"] > 15 for e in summary["epochs"]), "没有观测到人为丢包"
            assert all(e["lan_loss_percent"] < 5 for e in summary["epochs"]), "本机LAN支路意外严重丢包"
            if mode == "fixed":
                assert not summary["feedback_adjustment"]
                assert all(e["lan_ratio"] == e["next_lan_ratio"] == 0.5 for e in summary["epochs"])
                assert summary["epochs"][0]["lan_bps"] == summary["epochs"][0]["cpe_bps"] == 1000000
            else:
                assert summary["epochs"][0]["next_lan_ratio"] > 0.5
                assert summary["epochs"][1]["lan_bps"] > summary["epochs"][0]["lan_bps"]
                assert all(e["lan_bps"] + e["cpe_bps"] == 2000000 for e in summary["epochs"])
            record["checks"].append({"mode": mode, "passed": True, "summary": str(output / "summary.json")})
        # 故障场景：一条服务端口不存在，必须非零退出，不能冒充两路成功。
        config["mode"] = "fixed"
        config["cpe"]["port"] = available_port("127.0.0.2")
        config["duration_seconds"] = 2
        path = folder / "failure_config.json"
        path.write_text(json.dumps(config), encoding="utf-8")
        output = folder / "failure"
        result = subprocess.run([sys.executable, str(ROOT / "fixed" / "receive" / "main.py"), "--config", str(path),
                                 "--iperf", exe, "--loopback-test", "--output", str(output)],
                                capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30)
        (folder / "failure_console.txt").write_text(result.stdout + result.stderr, encoding="utf-8")
        summary = json.loads((output / "summary.json").read_text(encoding="utf-8"))
        assert result.returncode != 0 and not summary["completed"] and summary["error"]
        record["checks"].append({"mode": "single_path_failure", "passed": True})
        record["injected_dropped_datagrams"] = bridge.dropped
        print("真实iperf验证通过：固定五五、丢包反馈改变下一轮速率、单路失败。结果：", folder)
    finally:
        if bridge:
            bridge.close()
        for process in processes:
            if process.poll() is None:
                process.terminate()
            process.wait(timeout=5)
        for log in logs:
            log.close()
        (folder / "validation.json").write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
