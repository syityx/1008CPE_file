"""两条真正 iperf3 反向 UDP 流；结果和故障均保留，不用自制灌包替代。"""
import argparse
from concurrent.futures import ThreadPoolExecutor
import csv
from datetime import datetime
import ipaddress
import json
import math
from pathlib import Path
import subprocess
import threading
import time

from .network import prepare
from .runtime import ROOT, resolve_iperf

PROFILES = {"small": (80000, 2664), "medium": (1280000, 540), "large": (2000000, 396)}
HIDDEN = 0x08000000 if __import__("os").name == "nt" else 0


def parse_result(data):
    if data.get("error"):
        raise ValueError(data["error"])
    start = data.get("start", {}).get("test_start", {})
    if start.get("protocol") != "UDP" or not start.get("reverse"):
        raise ValueError("iperf 结果不是反向 UDP。")
    stat = data.get("end", {}).get("sum_received")
    if not stat or stat.get("sender") is not False:
        raise ValueError("缺少接收端 UDP 统计，无法作为反馈。")
    result = {key: stat.get(key) for key in ("bytes", "bits_per_second", "seconds", "packets",
                                            "lost_packets", "lost_percent", "jitter_ms")}
    if any(not isinstance(value, (float, int)) or not math.isfinite(value) or value < 0
           for value in result.values()) or result["lost_percent"] > 100:
        raise ValueError("iperf 接收统计不完整或数值无效。")
    if result["seconds"] <= 0 or result["packets"] <= 0 or result["bytes"] <= 0:
        raise ValueError("没有收到有效 UDP 数据，不能算双路成功。")
    result["connected"] = data["start"].get("connected", [])
    return result


class Processes:
    """Ctrl+C、单路失败、超时都停止本次启动的子进程。"""
    def __init__(self):
        self.lock = threading.Lock()
        self.active = set()
        self.cancelled = threading.Event()

    def start(self, command, **kwargs):
        with self.lock:
            if self.cancelled.is_set():
                raise ValueError("实验已取消。")
            process = subprocess.Popen(command, creationflags=HIDDEN, **kwargs)
            self.active.add(process)
            return process

    def stop(self):
        with self.lock:
            self.cancelled.set()
            for process in list(self.active):
                if process.poll() is None:
                    process.terminate()
            for process in list(self.active):
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=3)
            self.active.clear()

    def finished(self, process):
        with self.lock:
            self.active.discard(process)


def client_command(exe, branch, rate, duration, length, connect_timeout):
    return [exe, "-4", "-c", branch["sender_ip"], "-p", str(branch["port"]),
            "-B", branch["bind_ip"], "-u", "-R", "-P", "1", "-b", str(rate),
            "-t", str(duration), "-l", str(length), "-i", "1", "-J", "--get-server-output",
            "--connect-timeout", str(connect_timeout), "--rcv-timeout", "10000"]


def run_client(exe, branch, rate, duration, config, output, name, epoch, processes, barrier):
    command = client_command(exe, branch, rate, duration, config["datagram_bytes"], config["connect_timeout_ms"])
    barrier.wait(timeout=10)
    started = time.monotonic()
    process = processes.start(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              encoding="utf-8", errors="replace")
    stdout, stderr = "", ""
    try:
        try:
            stdout, stderr = process.communicate(timeout=duration + config["process_grace_seconds"])
        except subprocess.TimeoutExpired:
            process.kill()
            stdout, stderr = process.communicate(timeout=3)
            raise ValueError(name + " 超时；请检查双向 UDP 和 CPE 回程。")
        finally:
            # 原始输出和实际命令即使失败也保留。
            (output / f"{epoch:04d}_{name}.iperf.json").write_text(stdout, encoding="utf-8")
            (output / f"{epoch:04d}_{name}.stderr.txt").write_text(stderr, encoding="utf-8")
            (output / f"{epoch:04d}_{name}.command.json").write_text(json.dumps(command, indent=2), encoding="utf-8")
        if process.returncode:
            raise ValueError(name + f" iperf退出码{process.returncode}：" + (stderr or stdout)[-1000:])
        result = parse_result(json.loads(stdout))
        result.update(requested_bps=rate, requested_seconds=duration,
                      process_start=started, process_end=time.monotonic())
        return result
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=3)
        processes.finished(process)


def run_pair(exe, config, rate, ratio, duration, output, epoch, processes):
    lan_rate = round(rate * ratio)
    rates = {"lan": lan_rate, "cpe": rate - lan_rate}
    barrier = threading.Barrier(2)
    executor = ThreadPoolExecutor(max_workers=2)
    futures = {name: executor.submit(run_client, exe, config[name], rates[name], duration, config,
                                      output, name, epoch, processes, barrier) for name in rates}
    try:
        # 轮询只是检测故障；反馈只能来自两路都结束后的真实结果。
        while not all(f.done() for f in futures.values()):
            for future in futures.values():
                if future.done() and future.exception():
                    raise future.exception()
            time.sleep(0.05)
        return {name: future.result() for name, future in futures.items()}
    except BaseException:
        processes.stop()
        raise
    finally:
        executor.shutdown(wait=True)


def validate(config):
    required = ("mode", "total_bps", "duration_seconds", "epoch_seconds", "datagram_bytes",
                "connect_timeout_ms", "process_grace_seconds", "lan", "cpe")
    if not isinstance(config, dict) or any(key not in config for key in required):
        raise ValueError("配置缺少必要字段，请参考该模式的config.json。")
    if config["mode"] not in ("adaptive", "fixed"):
        raise ValueError("mode必须是adaptive或fixed。")
    for key in ("total_bps", "duration_seconds", "epoch_seconds", "datagram_bytes", "connect_timeout_ms", "process_grace_seconds"):
        value = config[key]
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise ValueError(key + " 必须是正整数。")
    if not 64 <= config["datagram_bytes"] <= 1400:
        raise ValueError("UDP正文长度应为64..1400，默认1200避免常见MTU下的IP分片。")
    if config["total_bps"] < 10000:
        raise ValueError("总速率至少10000bps，避免自适应低比例分支无法产生有效样本。")
    if config["total_bps"] % 2:
        raise ValueError("总速率必须是偶数，固定模式才能精确各分配50%的整数bps。")
    warmup = config.get("warmup_epochs", 2)
    if isinstance(warmup, bool) or not isinstance(warmup, int) or warmup < 0:
        raise ValueError("warmup_epochs必须是非负整数。")
    ports = []
    for name in ("lan", "cpe"):
        if not isinstance(config[name], dict) or "port" not in config[name]:
            raise ValueError(name + " 分支必须包含port配置。")
        port = config[name]["port"]
        if isinstance(port, bool) or not isinstance(port, int) or not 1024 <= port <= 65535:
            raise ValueError("实验端口必须为1024..65535整数。")
        ports.append(port)
    if ports[0] == ports[1]:
        raise ValueError("两路必须使用不同iperf服务端口。")


def load_config(mode, path):
    config = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    validate(config)
    if config["mode"] != mode:
        raise ValueError("入口与配置模式不一致。")
    return config


def sender(mode, args, config):
    exe, version = resolve_iperf(args.iperf)
    folder = ROOT / mode / "results" / (datetime.now().strftime("send_%Y%m%d_%H%M%S_%f"))
    folder.mkdir(parents=True)
    print(version.splitlines()[0])
    print("发送端启动；接收端应分别填写本机Wi-Fi/LAN地址和核心网业务方向地址。")
    try:
        from network_setup import windows_interfaces
        import sys
        if sys.platform == "win32":
            for row in windows_interfaces():
                print(f"  {row['name']}：{row['ip']} 网关={row.get('gateway')}")
    except ValueError as error:
        print("网卡枚举失败，可用ipconfig查看：", error)
    processes = Processes()
    logs = []
    try:
        for name in ("lan", "cpe"):
            stream = (folder / (name + ".log")).open("w", encoding="utf-8")
            logs.append(stream)
            cmd = [exe, "-4", "-s", "-B", args.listen_ip, "-p", str(config[name]["port"])]
            processes.start(cmd, stdout=stream, stderr=stream)
            print(f"  {name}：监听 {args.listen_ip}:{config[name]['port']} TCP+UDP")
        print("等待接收端启动，Ctrl+C停止。日志：", folder, flush=True)
        while True:
            for process in list(processes.active):
                if process.poll() is not None:
                    raise ValueError("iperf服务异常退出，请检查端口占用。日志：" + str(folder))
            time.sleep(0.2)
    except KeyboardInterrupt:
        print("发送端停止。")
    finally:
        processes.stop()
        for stream in logs:
            stream.close()
    return 0


def receiver(mode, args, config, config_path):
    overrides = {name: {"sender_ip": getattr(args, name + "_host"), "bind_ip": getattr(args, name + "_ip")}
                 for name in ("lan", "cpe")}
    config = prepare(config, config_path, overrides, args.loopback_test)
    exe, version = resolve_iperf(args.iperf)
    output = Path(args.output) if args.output else ROOT / mode / "results" / datetime.now().strftime("receive_%Y%m%d_%H%M%S_%f")
    output.mkdir(parents=True, exist_ok=False)
    (output / "effective_config.json").write_text(json.dumps(config, indent=2, ensure_ascii=False), encoding="utf-8")
    phases = [(args.profile, *PROFILES[args.profile])] if args.profile in PROFILES else (
        [(name, *PROFILES[name]) for name in PROFILES] if args.profile == "standard" else
        [("custom", config["total_bps"], config["duration_seconds"])])
    # 单类 --profile 默认仍用用户选择的测试时长；standard才执行三段完整等效流量。
    if args.profile in PROFILES:
        phases[0] = (args.profile, PROFILES[args.profile][0], config["duration_seconds"])
    controller = None
    if mode == "adaptive":
        from .feedback import LossFeedback
        controller = LossFeedback(config["warmup_epochs"])
    processes = Processes()
    started = time.monotonic()
    summary = {"mode": mode, "iperf_version": version, "loopback_test": args.loopback_test,
               "profile": args.profile, "completed": False, "epochs": [], "error": None,
               "feedback_adjustment": mode == "adaptive", "data_direction": "sender_to_receiver",
               "standard_file_compliance": False}
    print(f"模式={mode}，iperf反向UDP；结果：{output}", flush=True)
    for name in ("lan", "cpe"):
        print(f"  {name}: {config[name]['bind_ip']} -> {config[name]['sender_ip']}:{config[name]['port']}")
    if args.loopback_test:
        print("仅本机回环验证，不是物理Wi-Fi/CPE测试。")
    csvfile = (output / "epochs.csv").open("w", newline="", encoding="utf-8-sig")
    fields = ["epoch", "profile", "duration", "total_bps", "lan_ratio", "next_lan_ratio", "launch_gap_seconds",
              "start_skew_seconds", "lan_bps", "cpe_bps", "lan_received_bps", "cpe_received_bps",
              "lan_loss_percent", "cpe_loss_percent", "lan_jitter_ms", "cpe_jitter_ms"]
    writer = csv.DictWriter(csvfile, fieldnames=fields)
    writer.writeheader()
    previous_end = None
    try:
        for profile, total, duration in phases:
            remaining = duration
            while remaining:
                seconds = min(remaining, config["epoch_seconds"]) if mode == "adaptive" else remaining
                ratio = controller.ratio if controller else 0.5
                epoch = len(summary["epochs"]) + 1
                result = run_pair(exe, config, total, ratio, seconds, output, epoch, processes)
                if controller:
                    feedback = controller.update(result["lan"], result["cpe"])
                else:
                    feedback = {"next_lan_ratio": 0.5, "delta": 0.0}
                first_start = min(r["process_start"] for r in result.values())
                last_end = max(r["process_end"] for r in result.values())
                row = dict(epoch=epoch, profile=profile, duration=seconds, total_bps=total,
                           lan_ratio=ratio, next_lan_ratio=feedback["next_lan_ratio"],
                           launch_gap_seconds=first_start - previous_end if previous_end else 0,
                           start_skew_seconds=abs(result["lan"]["process_start"] - result["cpe"]["process_start"]),
                           lan_bps=result["lan"]["requested_bps"], cpe_bps=result["cpe"]["requested_bps"],
                           lan_received_bps=result["lan"]["bits_per_second"], cpe_received_bps=result["cpe"]["bits_per_second"],
                           lan_loss_percent=result["lan"]["lost_percent"], cpe_loss_percent=result["cpe"]["lost_percent"],
                           lan_jitter_ms=result["lan"]["jitter_ms"], cpe_jitter_ms=result["cpe"]["jitter_ms"])
                writer.writerow(row)
                csvfile.flush()
                summary["epochs"].append(dict(row, feedback=feedback, streams=result))
                previous_end = last_end
                remaining -= seconds
                print(f"轮{epoch} {profile}：LAN/CPE={ratio:.0%}/{1-ratio:.0%} "
                      f"接收={row['lan_received_bps']/1e6:.3f}/{row['cpe_received_bps']/1e6:.3f}Mbps "
                      f"丢包={row['lan_loss_percent']:.2f}%/{row['cpe_loss_percent']:.2f}% "
                      f"下一轮LAN={feedback['next_lan_ratio']:.0%}", flush=True)
                (output / "summary.json").write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
        summary["completed"] = True
    except KeyboardInterrupt:
        summary["error"] = "用户提前停止"
    except Exception as error:
        summary["error"] = str(error)
    finally:
        processes.stop()
        csvfile.close()
        summary["elapsed_seconds"] = time.monotonic() - started
        summary["active_test_seconds"] = sum(row["duration"] for row in summary["epochs"])
        summary["received_bytes"] = {n: sum(e["streams"][n]["bytes"] for e in summary["epochs"]) for n in ("lan", "cpe")}
        (output / "summary.json").write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
    print("完成。" if summary["completed"] else "未完成：" + summary["error"], "结果：", output)
    return 0 if summary["completed"] else 1


def main(mode, role):
    parser = argparse.ArgumentParser(description="真正iperf3两路反向UDP：" + mode + "/" + role)
    parser.add_argument("--config", default=str(ROOT / mode / "config.json"))
    parser.add_argument("--iperf", help="已有iperf3的完整路径；Windows默认自动下载3.22")
    parser.add_argument("--listen-ip", default="0.0.0.0")
    parser.add_argument("--lan-host")
    parser.add_argument("--cpe-host")
    parser.add_argument("--lan-ip")
    parser.add_argument("--cpe-ip")
    parser.add_argument("--rate", type=int, help="两路合计目标速率，bps")
    parser.add_argument("--duration", type=int, help="实际iperf灌包时长；adaptive不含轮次启动空隙")
    parser.add_argument("--epoch", type=int, help="反馈轮次秒数，fixed忽略")
    parser.add_argument("--profile", choices=["custom", "small", "medium", "large", "standard"], default="custom")
    parser.add_argument("--output", help="新的结果目录，不能覆盖已有目录")
    parser.add_argument("--loopback-test", action="store_true", help="仅回环测试，显式允许一张网卡")
    args = parser.parse_args()
    try:
        config = load_config(mode, args.config)
        for arg, key in ((args.rate, "total_bps"), (args.duration, "duration_seconds"), (args.epoch, "epoch_seconds")):
            if arg is not None:
                config[key] = arg
        validate(config)
        if args.profile != "custom" and args.rate is not None:
            raise ValueError("--profile与--rate不能同时设置。")
        if args.profile == "standard" and args.duration is not None:
            raise ValueError("standard采用3600秒业务时长；短测试请选单类或custom。")
        if role == "send":
            ipaddress.IPv4Address(args.listen_ip)
            return sender(mode, args, config)
        return receiver(mode, args, config, args.config)
    except (ValueError, OSError, subprocess.SubprocessError) as error:
        print("启动失败：", error)
        return 1
