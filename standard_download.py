"""表7标准测试调度：提前请求，发送端按绝对时刻发包，接收端合并校验。

多个文件允许在途；慢链路不会把20ms任务变成“下载完成后再等20ms”。
保留原PID数值核心与20块统计窗口，额外记录真实发送时刻与迟到情况。
"""
import csv
import hashlib
import json
import logging
import math
import time
import uuid
from collections import deque
from concurrent.futures import ThreadPoolExecutor

from file_transfer import FileDownloader, Cancelled
from standard_model import make_plan, planned_jobs, PreciseTimer

LOG = logging.getLogger("standard")


class StandardDownloader(FileDownloader):
    def __init__(self, config, transport, output):
        super().__init__(config, transport, output)
        self.plan = make_plan(config.get("standard_profile", "all"), config.get("standard_count_limit", 0))
        self.lead = config.get("standard_prefetch_seconds", 0.25)
        if not 0.05 <= self.lead <= 0.75:
            raise ValueError("标准预请求时间需要0.05至0.75秒。")
        # 计时容差是测量口径，不通过改变规定的20ms/50ms/6s来达标。
        self.tolerance_ms = config.get("standard_timing_tolerance_ms", 5.0)
        if not math.isfinite(self.tolerance_ms) or self.tolerance_ms < 0:
            raise ValueError("发送时刻容差必须是非负有限数。")
        self.finished_counts = {p["profile"]: 0 for p in self.plan}
        self.timing_violations = 0
        self.max_lateness_ms = 0.0
        self.last_sent = {}

    def fetch_standard_block(self, lane, job, block, size):
        before = time.monotonic()
        data, retries, sent_ns = self.transport.request(
            lane, "file_block", with_timing=True, file_index=job["index"],
            offset=block * self.block_size, size=size, session_id=self.session_id,
            profile=job["profile"], number=job["number"])
        return data, retries, time.monotonic() - before, sent_ns

    def finish_job(self, job):
        item = job["item"]
        if job["stream"]:
            job["stream"].close()
            job["stream"] = None
        digest = job["digest"].hexdigest()
        if job["committed"] != item["size"] or digest != item["sha256"]:
            raise ValueError("标准业务文件完整性校验失败：" + item["name"])
        job["part"].replace(self.output / "downloads" / item["name"])
        sent_ns = job["sent_ns"]
        lateness = (sent_ns - job["absolute_due_ns"]) / 1e6
        previous = self.last_sent.get(job["profile"])
        gap_ms = (sent_ns - previous) / 1e6 if previous is not None else ""
        self.last_sent[job["profile"]] = sent_ns
        violation = (lateness > self.tolerance_ms or lateness < 0 or
                     (gap_ms != "" and abs(gap_ms - job["spec"]["interval_ns"] / 1e6) > self.tolerance_ms))
        self.timing_violations += int(violation)
        self.max_lateness_ms = max(self.max_lateness_ms, lateness)
        self.completed += 1
        self.finished_counts[job["profile"]] += 1
        self.records.writerow([job["sequence"], job["profile"], job["number"] + 1, item["name"],
                               item["size"], job["committed"], job["due_ns"] / 1e9,
                               sent_ns, gap_ms, lateness, violation, time.monotonic() - job["released"],
                               digest, "complete"])

    def run(self):
        self.output.mkdir(parents=True, exist_ok=True)
        (self.output / "downloads").mkdir(exist_ok=True)
        self.transport.wait_ready(self.config.get("file_registration_timeout", 60))
        manifests = [self.transport.request(lane, "file_manifest")[0]["files"] for lane in (0, 1)]
        if manifests[0] != manifests[1]:
            raise ValueError("两路文件清单不同。")
        manifest = manifests[0]
        if not isinstance(manifest, list) or not 1 <= len(manifest) <= 8:
            raise ValueError("标准文件清单无效。")
        entries = {item.get("name"): (index, item) for index, item in enumerate(manifest) if isinstance(item, dict)}
        for spec in self.plan:
            item = entries.get(spec["name"], (None, {}))[1]
            if item.get("size") != spec["size"] or not isinstance(item.get("sha256"), str) or len(item["sha256"]) != 64:
                raise ValueError(f"标准文件{spec['name']}必须为{spec['size']}字节，请更新并重启发送端。")
        self.session_id = uuid.uuid4().hex
        before = time.monotonic_ns()
        session = self.transport.request(1, "file_session", session_id=self.session_id,
                                         profile=self.config.get("standard_profile", "all"),
                                         count_limit=self.config.get("standard_count_limit", 0))[0]
        after = time.monotonic_ns()
        if (session.get("plan") != self.plan or session.get("session_id") != self.session_id
                or type(session.get("start_ns")) is not int or type(session.get("server_now_ns")) is not int):
            raise ValueError("发送端返回的标准计划或时钟无效。")
        self.server_start = session["start_ns"]
        # 此估算只用于提前发请求；实际发送期限和时间误差都在发送端同一时钟上计算。
        self.local_start = (before + after) / 2e9 + (self.server_start - session["server_now_ns"]) / 1e9
        self.started = self.last_sample = self.last_log = time.monotonic()
        self.last_bytes = self.transport.received_bytes.copy()
        self.reason, self.error, self.round = "standard_complete", "", 1
        self.streams = []
        def writer(name, fields):
            stream = (self.output / name).open("w", newline="", encoding="utf-8-sig")
            self.streams.append(stream)
            result = csv.writer(stream)
            result.writerow(fields)
            return result
        self.records = writer("files.csv", ["sequence", "profile", "number", "file", "size", "committed_bytes",
                                             "planned_seconds", "sender_start_ns", "sender_gap_ms", "sender_lateness_ms",
                                             "timing_violation", "request_to_complete_seconds", "sha256", "status"])
        self.blocks = writer("blocks.csv", ["sequence", "file", "block", "lane", "size", "retries", "seconds"])
        self.controls = writer("control.csv", ["elapsed", "units", "cycle", "queue_lan", "queue_cpe", "g", "updates"])
        self.samples = writer("throughput.csv", ["elapsed", "lan_bytes", "cpe_bytes", "lan_mbps", "cpe_mbps", "queue_lan", "queue_cpe", "g"])
        saved = {k: v for k, v in self.config.items() if not k.startswith("_")}
        (self.output / "config.json").write_text(json.dumps(dict(config=saved, files=manifest, plan=self.plan,
                                                               session_id=self.session_id, sender_start_ns=self.server_start),
                                                        ensure_ascii=False, indent=2), encoding="utf-8")
        plan_seconds = sum(p["count"] * p["interval_ns"] for p in self.plan) / 1e9
        LOG.info("表7标准任务=%s，周期窗口=%.3fs；发送端定时，单位按1000换算", {p["profile"]: p["count"] for p in self.plan}, plan_seconds)
        tasks = iter(planned_jobs(self.plan))
        upcoming = next(tasks, None)
        jobs, active, pending = deque(), {}, {}
        issued = expected = 0
        last_feedback = 0.0
        pool = ThreadPoolExecutor(max_workers=32)
        try:
            with PreciseTimer():
                while upcoming is not None or jobs or time.monotonic() < self.local_start + plan_seconds:
                    self.tick()
                    now = time.monotonic()
                    while upcoming and now >= self.local_start + upcoming["due_ns"] / 1e9 - self.lead:
                        if len(jobs) >= self.window:
                            raise ValueError("标准业务积压超过窗口，无法维持表7负载；测试中止，不降低发包频率。")
                        job = dict(upcoming)
                        job["index"], job["item"] = entries[job["spec"]["name"]]
                        job.update(blocks=math.ceil(job["item"]["size"] / self.block_size), next_block=0,
                                   committed=0, stream=None, digest=hashlib.sha256(), sent_ns=None,
                                   absolute_due_ns=self.server_start + job["due_ns"], released=now,
                                   part=self.output / "downloads" / (job["item"]["name"] + ".part"))
                        jobs.append(job)
                        upcoming = next(tasks, None)
                    for job in jobs:
                        while job["next_block"] < job["blocks"] and issued - expected < self.window:
                            lane = self.choose()
                            block = job["next_block"]
                            size = min(self.block_size, job["item"]["size"] - block * self.block_size)
                            future = pool.submit(self.fetch_standard_block, lane, job, block, size)
                            active[issued] = (lane, job, block, future)
                            issued += 1
                            job["next_block"] += 1
                    changed = False
                    for ordinal, (lane, job, block, future) in list(active.items()):
                        if future.done():
                            data, retries, seconds, sent_ns = future.result()
                            pending[ordinal] = (lane, data, job)
                            job["sent_ns"] = sent_ns if job["sent_ns"] is None else min(job["sent_ns"], sent_ns)
                            self.blocks.writerow([job["sequence"], job["item"]["name"], block, lane, len(data), retries, seconds])
                            del active[ordinal]
                            changed = True
                    now = time.monotonic()
                    if changed or now - last_feedback >= 0.1:
                        self.feedback({ordinal: (row[0], row[1]) for ordinal, row in pending.items()})
                        last_feedback = now
                    while expected in pending:
                        _, data, job = pending.pop(expected)
                        if job["stream"] is None:
                            job["stream"] = job["part"].open("wb")
                        job["stream"].write(data)
                        job["digest"].update(data)
                        job["committed"] += len(data)
                        expected += 1
                        if job["committed"] == job["item"]["size"]:
                            self.finish_job(job)
                            assert jobs[0] is job
                            jobs.popleft()
                    self.pending_counts = [sum(row[0] == lane for row in pending.values()) for lane in (0, 1)]
                    self.stop.wait(0.001)
        except (Cancelled, KeyboardInterrupt):
            if self.reason != "duration":
                self.reason = "user_stop"
        except Exception as exc:
            self.reason, self.error = "error", str(exc)
            raise
        finally:
            self.stop.set()
            with self.transport.condition:
                self.transport.condition.notify_all()
            pool.shutdown(wait=True, cancel_futures=True)
            for job in jobs:
                if job["stream"]:
                    job["stream"].close()
                self.records.writerow([job["sequence"], job["profile"], job["number"] + 1, job["item"]["name"],
                                       job["item"]["size"], job["committed"], job["due_ns"] / 1e9,
                                       job["sent_ns"] or "", "", "", "", time.monotonic() - job["released"],
                                       job["digest"].hexdigest(), "failed" if self.error else "stopped"])
            now = time.monotonic()
            current = self.transport.received_bytes.copy()
            seconds = max(now - self.last_sample, 0.000001)
            self.samples.writerow([now - self.started, *current,
                                   *[(current[i] - self.last_bytes[i]) * 8 / seconds / 1e6 for i in (0, 1)],
                                   *self.pending_counts, self.g])
            complete = self.reason == "standard_complete" and all(self.finished_counts[p["profile"]] == p["count"] for p in self.plan)
            result = dict(reason=self.reason, error=self.error, model="table7_standard", unit="decimal",
                          profile=self.config.get("standard_profile", "all"), count_limit=self.config.get("standard_count_limit", 0),
                          full_standard_counts=not bool(self.config.get("standard_count_limit", 0)),
                          counts_complete=complete, timing_ok=complete and self.timing_violations == 0,
                          timing_tolerance_ms=self.tolerance_ms, timing_violations=self.timing_violations,
                          max_sender_lateness_ms=self.max_lateness_ms, planned_seconds=plan_seconds,
                          finished_counts=self.finished_counts, completed_files=self.completed,
                          assigned_blocks=self.assigned, received_bytes=current, retries=self.transport.retries,
                          g=self.g, pid_updates=self.updates, elapsed=now - self.started, results_dir=str(self.output))
            (self.output / "summary.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
            for stream in self.streams:
                stream.close()
        return result
