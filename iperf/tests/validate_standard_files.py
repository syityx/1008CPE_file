"""启动真实发送入口与接收入口，验证三种标准文件和四个模式，不访问云。

每类两份；是回环内容/流程验收，不是完整次数或严格业务节奏验收。
"""
from datetime import datetime
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import time

from iperf.common.file_service import request
from iperf.common.network import active_branches
from iperf.common.runner import HIDDEN
from iperf.common.runtime import ROOT, resolve_iperf
from iperf.tests.validate_real import available_port
from standard_model import SPECS


def main():
    cycle_only = "--cycle-only" in sys.argv
    exe, version = resolve_iperf()
    folder = ROOT / "adaptive" / "results" / datetime.now().strftime("file_validation_%Y%m%d_%H%M%S_%f")
    folder.mkdir(parents=True)
    sources = folder / "sources"
    sources.mkdir()
    # 所有文件使用不同的确定内容，验证实际文件读写，而非iperf随机负载巧合。
    for number, (profile, spec) in enumerate(SPECS.items()):
        (sources / spec["name"]).write_bytes(bytes((i * 37 + number + 17) % 256 for i in range(spec["size"])))
    record = dict(physical_network_test=False, iperf=version, checks=[])
    for mode in ("fixed", "adaptive", "lan_only", "cpe_only"):
        config = json.loads((ROOT / mode / "config.json").read_text())
        names = active_branches(mode)
        used = set()
        for name in names:
            for key in ("port", "control_port"):
                port = available_port("127.0.0.1" if name == "lan" else "127.0.0.2")
                while port in used:
                    port = available_port()
                used.add(port)
                config[name][key] = port
        config.update(count_limit=2, profile="standard", warmup_epochs=0)
        if cycle_only:
            config.update(count_limit=0, profile="small", rounds=2)
        path = folder / (mode + "_config.json")
        path.write_text(json.dumps(config), encoding="utf-8")
        stop = folder / (mode + ".stop")
        log = (folder / (mode + "_send_console.txt")).open("w", encoding="utf-8")
        process = subprocess.Popen([sys.executable, str(ROOT / mode / "send" / "main.py"), "--config", str(path),
                                    "--iperf", exe, "--files-dir", str(sources), "--stop-file", str(stop)],
                                   stdout=log, stderr=log, creationflags=HIDDEN)
        try:
            branches = {n: dict(config[n], sender_ip="127.0.0.1" if n == "lan" else "127.0.0.2", bind_ip="127.0.0.1") for n in names}
            deadline = time.monotonic() + 15
            while True:
                try:
                    for branch in branches.values():
                        request(branch, dict(op="manifest"), timeout=1)
                    break
                except (OSError, ValueError):
                    if process.poll() is not None or time.monotonic() > deadline:
                        raise AssertionError("真实发送入口未就绪：" + str(folder / (mode + "_send_console.txt")))
                    time.sleep(0.2)
            output = folder / mode
            client = subprocess.run([sys.executable, str(ROOT / mode / "receive" / "main.py"), "--config", str(path),
                                     "--iperf", exe, "--loopback-test", "--output", str(output)],
                                    capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=90)
            (folder / (mode + "_receive_console.txt")).write_text(client.stdout + client.stderr, encoding="utf-8")
            print(client.stdout, flush=True)
            assert client.returncode == 0, client.stdout + client.stderr
            summary = json.loads((output / "summary.json").read_text(encoding="utf-8"))
            expected_counts = {"small": 2} if cycle_only else {p: 2 for p in SPECS}
            assert summary["completed"] and summary["verified_files"] == expected_counts
            assert summary["active_branches"] == list(names)
            assert not summary["full_standard_counts"] and not summary["exact_business_timing_verified"]
            assert sum(summary["received_bytes"].values()) == 2 * sum(SPECS[p]["size"] for p in expected_counts)
            if cycle_only:
                assert summary["completed_rounds"] == 2
            for profile, spec in SPECS.items():
                assert summary["sources"][profile]["sha256"] == hashlib.sha256((sources / spec["name"]).read_bytes()).hexdigest()
            if mode == "fixed":
                assert summary["received_bytes"]["lan"] == summary["received_bytes"]["cpe"]
                assert all(e["lan_ratio"] == e["next_lan_ratio"] == 0.5 for e in summary["epochs"])
            if len(names) == 1:
                other = "cpe" if names[0] == "lan" else "lan"
                assert summary["received_bytes"][other] == 0
                assert all(set(e["streams"]) == set(names) for e in summary["epochs"])
                assert not list(output.glob("*" + other + ".command.json"))
            record["checks"].append(dict(mode=mode, passed=True, summary=str(output / "summary.json")))
            # 新默认是按轮数/时长循环，不使用表7的次数；用两轮小文件验证循环控制。
            cycle_output = folder / (mode + "_cycle")
            cycle = subprocess.run([sys.executable, str(ROOT / mode / "receive" / "main.py"), "--config", str(path),
                                    "--iperf", exe, "--loopback-test", "--output", str(cycle_output),
                                    "--count-limit", "0", "--rounds", "2", "--profile", "small"],
                                   capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30)
            assert cycle.returncode == 0, cycle.stdout + cycle.stderr
            cycle_data = json.loads((cycle_output / "summary.json").read_text(encoding="utf-8"))
            assert cycle_data["completed_rounds"] == 2 and cycle_data["verified_files"] == {"small": 2}
            assert cycle_data["enforced_specifications"] == ["file_size"]
            record["checks"].append(dict(mode=mode + "_cycle", passed=True))
        finally:
            stop.touch()
            try:
                process.wait(timeout=20)
            finally:
                log.close()
            assert process.returncode == 0, "发送端未正常清理退出"
            (folder / "validation.json").write_text(json.dumps(record, indent=2, ensure_ascii=False), encoding="utf-8")
    print("循环控制：四种模式的小文件两轮循环全部通过。" if cycle_only else
          "真实标准文件：四种模式、三种大小、逐份SHA256、单路不访问另一路，全部通过。", folder)


if __name__ == "__main__":
    main()
