"""以真实标准文件为数据源的四种模式；数据由iperf3 TCP -F反向发送。"""
import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import csv
from datetime import datetime
import json
import math
from pathlib import Path
import subprocess
import threading
import time

from standard_model import SPECS
from .feedback import LossFeedback
from .file_service import FileService, request, start_service
from .file_source import PROJECT, portions, verify_batch
from .network import active_branches, ipv4, prepare
from .runner import Processes
from .runtime import ROOT, resolve_iperf


def validate(config):
    names = active_branches(config.get("mode", ""))
    for key in ("epoch_seconds", "file_batch_seconds", "connect_timeout_ms", "process_grace_seconds", "total_bps", "duration_seconds", "files_per_batch"):
        if type(config.get(key)) is not int or config[key] <= 0:
            raise ValueError(key + "必须是正整数。")
    for key in ("count_limit", "warmup_epochs", "rounds"):
        if type(config.get(key, 0)) is not int or config.get(key, 0) < 0:
            raise ValueError(key + "必须是非负整数。")
    if config["total_bps"] < 10000 or config["total_bps"] % 2:
        raise ValueError("总速率须为至少10000bps的偶数，便于五五分配整数速率。")
    ports = []
    for name in names:
        if not isinstance(config.get(name), dict):
            raise ValueError("缺少所选链路配置：" + name)
        for key in ("port", "control_port"):
            value = config[name].get(key)
            if type(value) is not int or not 1024 <= value <= 65535:
                raise ValueError(name + "." + key + "须为1024..65535整数。")
            ports.append(value)
    if len(set(ports)) != len(ports):
        raise ValueError("iperf与文件控制服务端口不可重复。")


def file_block_length(length):
    # iperf3 3.22在限速-F反向传输中可能卡在最后一个短块。
    # 选取分段长度的约数，让每块完整；不填充、不丢弃真实文件尾部。
    if length <= 65536:
        return length
    largest = 1
    for divisor in range(1, math.isqrt(length) + 1):
        if length % divisor == 0:
            for candidate in (divisor, length // divisor):
                if candidate <= 65536:
                    largest = max(largest, candidate)
    return largest


def file_command(exe, branch, desc, path, rate, timeout_ms):
    # -F放在接收端用于保存真实内容；发送端的-F指向标准文件分段组成的批次。
    # iperf3明确禁止UDP文件传输，此处必须用TCP，不加入-u。
    return [exe, "-4", "-c", branch["sender_ip"], "-p", str(branch["port"]),
            "-B", branch["bind_ip"], "-R", "-P", "1", "-b", str(rate),
            "-n", str(desc["bytes"]), "-l", str(file_block_length(desc["length"])),
            "-F", str(path.resolve()), "-N", "-J", "--get-server-output",
            "--connect-timeout", str(timeout_ms), "--rcv-timeout", "10000"]


def parse_file_result(data):
    if data.get("error"):
        raise ValueError(data["error"])
    start = data.get("start", {}).get("test_start", {})
    stat = data.get("end", {}).get("sum_received", {})
    if start.get("protocol") != "TCP" or not start.get("reverse") or stat.get("sender") is not False:
        raise ValueError("需要真实iperf TCP反向文件接收统计。")
    result = {key: stat.get(key) for key in ("bytes", "seconds", "bits_per_second")}
    if any(not isinstance(v, (int, float)) or not math.isfinite(v) or v <= 0 for v in result.values()):
        raise ValueError("未收到有效文件数据。")
    server_end = data.get("server_output_json", {}).get("end", {})
    result["tcp_retransmits"] = server_end.get("sum_sent", {}).get("retransmits")
    result["connected"] = data["start"].get("connected", [])
    return result


def transfer(exe, config, name, desc, rate, nominal_seconds, output, epoch, processes, barrier):
    path = output / f"{epoch:04d}_{name}.received.bin"
    command = file_command(exe, config[name], desc, path, rate, config["connect_timeout_ms"])
    barrier.wait(timeout=10)
    started = time.monotonic()
    process = processes.start(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              text=True, encoding="utf-8", errors="replace")
    stdout, stderr = "", ""
    try:
        try:
            stdout, stderr = process.communicate(timeout=nominal_seconds + config["process_grace_seconds"])
        except subprocess.TimeoutExpired:
            process.kill()
            stdout, stderr = process.communicate(timeout=3)
            raise ValueError(name + " 文件传输超时。")
        finally:
            (output / f"{epoch:04d}_{name}.iperf.json").write_text(stdout, encoding="utf-8")
            (output / f"{epoch:04d}_{name}.stderr.txt").write_text(stderr, encoding="utf-8")
            (output / f"{epoch:04d}_{name}.command.json").write_text(json.dumps(command, indent=2), encoding="utf-8")
        if process.returncode:
            raise ValueError(name + " iperf失败：" + (stderr or stdout)[-1000:])
        result = parse_file_result(json.loads(stdout))
        if result["bytes"] != desc["bytes"] or not path.is_file() or path.stat().st_size != desc["bytes"]:
            raise ValueError(name + " 未接收到完整标准文件分段。")
        result.update(requested_bps=rate, nominal_seconds=nominal_seconds,
                      process_start=started, process_end=time.monotonic(), path=str(path))
        return result
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=3)
        processes.finished(process)


def one_batch(exe, config, names, manifests, profile, count, ratio, output, epoch, processes):
    spec = SPECS[profile]
    chunks = portions(spec["size"], ratio, names)
    interval = spec["size"] * 8 / config["total_bps"]
    nominal_seconds = count * interval
    rates = {n: round(config["total_bps"] * chunks[n][1] / spec["size"]) for n in names}
    descriptions = {}
    with ThreadPoolExecutor(max_workers=len(names)) as pool:
        futures = {pool.submit(request, config[name], dict(op="prepare", profile=profile, count=count,
                                                          offset=chunks[name][0], length=chunks[name][1],
                                                          rate_bps=rates[name])): name for name in names}
        errors = []
        for future in as_completed(futures):
            name = futures[future]
            try:
                descriptions[name] = future.result()
            except Exception as error:
                errors.append(name + ": " + str(error))
    try:
        if errors:
            raise ValueError("文件准备失败：" + "; ".join(errors))
        for name in names:
            desc = descriptions[name]
            if any(desc.get(k) != v for k, v in dict(profile=profile, count=count, offset=chunks[name][0],
                                                     length=chunks[name][1], bytes=count * chunks[name][1],
                                                     standard_file_sha256=manifests[name][profile]["sha256"],
                                                     iperf_port=config[name]["port"]).items()):
                raise ValueError(name + " 服务端返回的文件分段不符合请求。")
        (output / f"{epoch:04d}_manifest.json").write_text(json.dumps(descriptions, indent=2), encoding="utf-8")
        barrier = threading.Barrier(len(names))
        pool = ThreadPoolExecutor(max_workers=len(names))
        tasks = {pool.submit(transfer, exe, config, name, descriptions[name],
                             rates[name], nominal_seconds,
                             output, epoch, processes, barrier): name for name in names}
        try:
            result = {tasks[f]: f.result() for f in as_completed(tasks)}
        except BaseException:
            processes.stop()
            raise
        finally:
            pool.shutdown(wait=True)
        verified = verify_batch({n: result[n]["path"] for n in names}, descriptions, count,
                                spec["size"], manifests[names[0]][profile]["sha256"])
        return result, verified, nominal_seconds
    finally:
        for name, desc in descriptions.items():
            try:
                request(config[name], dict(op="release", session_id=desc["session_id"]), timeout=3)
            except (ValueError, OSError) as error:
                print(name + " 释放会话未确认，发送端会到期清理：", error)


def sender(mode, args, config):
    names = active_branches(mode)
    exe, version = resolve_iperf(args.iperf)
    sources = Path(args.files_dir).resolve() if args.files_dir else PROJECT / "send" / "standard_files"
    folder = ROOT / mode / "results" / datetime.now().strftime("send_%Y%m%d_%H%M%S_%f")
    folder.mkdir(parents=True)
    services, servers = [], []
    try:
        for name in names:
            branch_folder = folder / name
            branch_folder.mkdir()
            service = FileService(exe, branch_folder, sources, args.listen_ip, config[name], config["process_grace_seconds"])
            services.append(service)
            server, thread = start_service(service)
            servers.append((server, thread))
            print(f"{name}：文件控制TCP {config[name]['control_port']}；iperf文件TCP {config[name]['port']}", flush=True)
        print(version.splitlines()[0], "模式=" + mode, "真实标准文件目录：", sources, flush=True)
        print("等待接收端。Ctrl+C停止。日志：", folder, flush=True)
        print("发送端IPv4请用ipconfig核对；接收端只填写所选链路可达地址。", flush=True)
        deadline = time.monotonic() + args.server_duration if args.server_duration else None
        while (deadline is None or time.monotonic() < deadline) and not (args.stop_file and Path(args.stop_file).exists()):
            for service in services:
                service.expire()
            time.sleep(0.2)
    except KeyboardInterrupt:
        print("发送端停止。")
    finally:
        for server, thread in servers:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)
        for service in services:
            service.close()
    return 0


def receiver(mode, args, config):
    # 在填写地址、保存配置或设置路由前检查工具，缺组件时直接退出。
    exe, version = resolve_iperf(args.iperf)
    names = active_branches(mode)
    overrides = {n: dict(sender_ip=getattr(args, n + "_host"), bind_ip=getattr(args, n + "_ip")) for n in names}
    config = prepare(config, args.config, overrides, args.loopback_test)
    output = Path(args.output) if args.output else ROOT / mode / "results" / datetime.now().strftime("receive_%Y%m%d_%H%M%S_%f")
    output.mkdir(parents=True, exist_ok=False)
    (output / "effective_config.json").write_text(json.dumps(config, indent=2, ensure_ascii=False), encoding="utf-8")
    profiles = list(SPECS) if args.profile == "standard" else [args.profile]
    # 显式count-limit是一轮定量验证；默认按小→中→大循环，到时长或轮数边界停止。
    bounded = config["count_limit"] > 0
    counts = {p: config["count_limit"] if bounded else config["files_per_batch"] for p in profiles}
    summary = dict(mode=mode, active_branches=list(names), payload="standard_files", data_protocol="TCP", iperf_version=version,
                   loopback_test=args.loopback_test, completed=False, error=None, epochs=[], verified_files={p: 0 for p in profiles},
                   planned_files=counts if bounded else None, files_per_visit=counts, full_standard_counts=False,
                   enforced_specifications=["file_size"], completed_rounds=0,
                   feedback_adjustment=mode == "adaptive", feedback_metric="received_bps/target_bps" if mode == "adaptive" else None,
                   exact_business_timing_verified=False)
    processes = Processes()
    controller = LossFeedback(config["warmup_epochs"]) if mode == "adaptive" else None
    started = time.monotonic()
    fields = ["epoch", "profile", "file_count", "file_bytes", "nominal_seconds", "lan_ratio", "next_lan_ratio",
              "lan_received_bytes", "cpe_received_bytes", "lan_bps", "cpe_bps", "lan_received_bps", "cpe_received_bps",
              "launch_gap_seconds", "start_skew_seconds", "verified_files"]
    csvfile = (output / "epochs.csv").open("w", newline="", encoding="utf-8-sig")
    writer = csv.DictWriter(csvfile, fieldnames=fields)
    writer.writeheader()
    previous_end = None
    print(f"{mode}：标准文件 → 真正iperf TCP -F → 接收合并校验；链路={','.join(names)}", flush=True)
    try:
        manifests = {}
        for name in names:
            print(f"{name}: {config[name]['bind_ip']} -> {config[name]['sender_ip']}", flush=True)
            reply = request(config[name], dict(op="manifest"), timeout=config["connect_timeout_ms"] / 1000)
            if reply.get("data_protocol") != "TCP" or reply.get("iperf_port") != config[name]["port"]:
                raise ValueError(name + " 文件服务协议或端口不一致。")
            manifests[name] = reply["files"]
            for p in profiles:
                if manifests[name][p]["size"] != SPECS[p]["size"]:
                    raise ValueError("发送端标准文件大小不符。")
        if len(names) == 2 and manifests[names[0]] != manifests[names[1]]:
            raise ValueError("两路标准源文件不一致，不能合并。")
        summary["sources"] = manifests[names[0]]
        stopping = False
        while not stopping:
            for profile in profiles:
                remaining = counts[profile]
                interval = SPECS[profile]["size"] * 8 / config["total_bps"]
                window = config["epoch_seconds"] if controller else config["file_batch_seconds"]
                batch_count = max(1, int(window / interval))
                while remaining:
                    if time.monotonic() - started >= config["duration_seconds"]:
                        if bounded:
                            raise ValueError("达到测试时长，指定文件次数尚未完成。")
                        summary["stop_reason"] = "duration"
                        stopping = True
                        break
                    count = min(remaining, batch_count)
                    ratio = controller.ratio if controller else (0.5 if len(names) == 2 else float(names[0] == "lan"))
                    epoch = len(summary["epochs"]) + 1
                    result, verified, nominal = one_batch(exe, config, names, manifests, profile, count, ratio, output, epoch, processes)
                    if controller:
                        qualities = {n: min(1, result[n]["bits_per_second"] / result[n]["requested_bps"]) for n in names}
                        feedback = controller.update_quality(qualities["lan"], qualities["cpe"])
                    else:
                        feedback = dict(next_lan_ratio=ratio, delta=0)
                    first = min(r["process_start"] for r in result.values())
                    last = max(r["process_end"] for r in result.values())
                    row = dict(epoch=epoch, profile=profile, file_count=count, file_bytes=SPECS[profile]["size"],
                               nominal_seconds=nominal, lan_ratio=ratio, next_lan_ratio=feedback["next_lan_ratio"],
                               launch_gap_seconds=first - previous_end if previous_end else 0,
                               start_skew_seconds=max(r["process_start"] for r in result.values()) - first, verified_files=verified)
                    for name in ("lan", "cpe"):
                        row[name + "_received_bytes"] = result[name]["bytes"] if name in result else 0
                        row[name + "_bps"] = result[name]["requested_bps"] if name in result else 0
                        row[name + "_received_bps"] = result[name]["bits_per_second"] if name in result else 0
                    summary["epochs"].append(dict(row, streams=result, feedback=feedback))
                    summary["verified_files"][profile] += verified
                    remaining -= count
                    previous_end = last
                    writer.writerow(row)
                    csvfile.flush()
                    (output / "summary.json").write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
                    print(f"轮{epoch} {profile} {count}份×{SPECS[profile]['size']}B：SHA256全部一致；"
                          f"LAN比例={ratio:.0%}，下一轮={feedback['next_lan_ratio']:.0%}", flush=True)
                if stopping:
                    break
            if not stopping:
                summary["completed_rounds"] += 1
            if bounded or (config["rounds"] and summary["completed_rounds"] >= config["rounds"]):
                summary["stop_reason"] = "file_count" if bounded else "rounds"
                stopping = True
        if not summary["epochs"]:
            raise ValueError("测试时长已到但没有完成任何文件传输。")
        summary["completed"] = True
    except KeyboardInterrupt:
        summary["error"] = "用户提前停止"
    except Exception as error:
        summary["error"] = str(error)
    finally:
        processes.stop()
        csvfile.close()
        summary["elapsed_seconds"] = time.monotonic() - started
        summary["nominal_seconds"] = sum(e["nominal_seconds"] for e in summary["epochs"])
        summary["received_bytes"] = {n: sum(e["streams"].get(n, {}).get("bytes", 0) for e in summary["epochs"]) for n in ("lan", "cpe")}
        (output / "summary.json").write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
    print("完成。" if summary["completed"] else "未完成：" + summary["error"], "结果：", output, flush=True)
    return 0 if summary["completed"] else 1


def main(mode, role):
    # 保留之前的真正UDP吞吐模式，两端都明确指定后才运行。
    select_parser = argparse.ArgumentParser(add_help=False)
    select_parser.add_argument("--payload", choices=("standard_files", "synthetic"), default="standard_files")
    selection, argv = select_parser.parse_known_args()
    if selection.payload == "synthetic":
        if mode not in ("adaptive", "fixed"):
            print("单路新增模式使用标准文件；合成UDP模式请使用adaptive/fixed入口。")
            return 1
        from .runner import main as synthetic_main
        return synthetic_main(mode, role, argv)
    parser = argparse.ArgumentParser(description="真正iperf标准文件：" + mode + "/" + role)
    parser.add_argument("--payload", choices=("standard_files", "synthetic"), default="standard_files")
    parser.add_argument("--config", default=str(ROOT / mode / "config.json"))
    parser.add_argument("--iperf")
    parser.add_argument("--listen-ip", default="0.0.0.0")
    parser.add_argument("--lan-host")
    parser.add_argument("--cpe-host")
    parser.add_argument("--lan-ip")
    parser.add_argument("--cpe-ip")
    parser.add_argument("--profile", choices=("standard", *SPECS), default=None)
    parser.add_argument("--count-limit", type=int, help="每类指定次数，正数执行一轮定量测试；0按轮数/时长循环")
    parser.add_argument("--rounds", type=int, help="小中大循环轮数，0按测试时长循环")
    parser.add_argument("--rate", type=int, help="两路合计目标速率bps，按字节分段比例分配")
    parser.add_argument("--epoch", type=int)
    parser.add_argument("--duration", type=int, help="测试墙钟时长，在完整批次边界停止")
    parser.add_argument("--files-dir", help="发送端三个实际标准文件所在目录")
    parser.add_argument("--output")
    parser.add_argument("--loopback-test", action="store_true")
    parser.add_argument("--server-duration", type=int, default=0, help="测试时定时关闭发送端；默认一直等待")
    parser.add_argument("--stop-file", help="测试助手：指定文件出现时正常关闭发送端及子进程")
    args = parser.parse_args(argv)
    try:
        config = json.loads(Path(args.config).read_text(encoding="utf-8-sig"))
        if config.get("mode") != mode:
            raise ValueError("配置与入口模式不一致。")
        for arg, key in ((args.count_limit, "count_limit"), (args.epoch, "epoch_seconds"),
                         (args.duration, "duration_seconds"), (args.rounds, "rounds"), (args.rate, "total_bps")):
            if arg is not None:
                config[key] = arg
        validate(config)
        args.profile = args.profile or config.get("profile", "standard")
        if args.profile not in ("standard", *SPECS):
            raise ValueError("标准文件profile无效。")
        if args.duration is not None and args.duration <= 0 or args.server_duration < 0:
            raise ValueError("时长参数须为正数，server-duration的0表示不限。")
        if role == "send":
            if args.listen_ip != "0.0.0.0":
                ipv4(args.listen_ip)
            return sender(mode, args, config)
        return receiver(mode, args, config)
    except (ValueError, OSError, subprocess.SubprocessError) as error:
        print("启动失败：", error, flush=True)
        return 1
