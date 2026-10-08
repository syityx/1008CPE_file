"""真实UDP标准模型验证：发送端定时、三类尺寸/次数、并发下载和首包测量。"""
import csv
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from standard_model import make_plan, planned_jobs, create_standard_files
from standard_download import StandardDownloader
from file_transfer import FileSender, file_hash
import test_file_download as legacy_tests
import protocol as wire


class StandardTests(unittest.TestCase):
    def setup_standard(self, profile="small", count=16):
        # 复用已验证的回环云中继/注册链路，源文件改为独立标准文件。
        fixture = legacy_tests.FileTests()
        fixture._cleanups = self._cleanups
        _, relay, sender, transport, downloader = fixture.setup_chain()
        sender.close()
        source = downloader.output.parent / "standard_source"
        settings = dict(sender.config, file_test_model="standard")
        sender = FileSender(settings, source)
        self.addCleanup(sender.close)
        sender.start()
        receiver = dict(transport.config, file_test_model="standard", standard_profile=profile,
                        standard_count_limit=count, standard_prefetch_seconds=0.25,
                        standard_timing_tolerance_ms=20.0, file_request_timeout=1.0, file_max_retries=3)
        # 不在表7测试中沿用旧测试为4KiB设置的块，验证1.5MB文件正常跨路分块。
        receiver["file_block_kib"] = 32
        transport.config.update(receiver)
        result = StandardDownloader(receiver, transport, downloader.output)
        return source, relay, sender, transport, result

    def test_table_sizes_counts_and_one_hour_plan(self):
        plan = make_plan()
        self.assertEqual([(p["size"], p["interval_ns"], p["count"]) for p in plan],
                         [(200, 20_000_000, 133200), (8000, 50_000_000, 10800), (1500000, 6_000_000_000, 66)])
        self.assertEqual(sum(p["count"] * p["interval_ns"] for p in plan), 3600_000_000_000)
        jobs = planned_jobs(make_plan("large"))
        self.assertEqual(next(jobs)["due_ns"], 0)
        self.assertEqual(list(jobs)[-1]["due_ns"], 390_000_000_000)
        with tempfile.TemporaryDirectory() as folder:
            create_standard_files(folder)
            self.assertEqual(sorted(p.stat().st_size for p in Path(folder).iterdir()), [200, 8000, 1500000])
            path = Path(folder) / "small.txt"
            path.write_bytes(b"personal")
            with self.assertRaisesRegex(ValueError, "200字节"):
                create_standard_files(folder)
            self.assertEqual(path.read_bytes(), b"personal")

    def test_small_sender_schedule_independent_of_completion(self):
        source, _, _, transport, downloader = self.setup_standard()
        original = transport.request
        def delayed(*args, **kwargs):
            response = original(*args, **kwargs)
            if len(args) > 1 and args[1] == "file_block":
                time.sleep(0.08)  # 每次下载完成比20ms周期更慢，仍须按计划发包。
            return response
        with patch.object(transport, "request", side_effect=delayed):
            result = downloader.run()
        self.assertTrue(result["counts_complete"])
        self.assertFalse(result["full_standard_counts"])
        self.assertEqual(result["finished_counts"], {"small": 16})
        self.assertTrue(all(v > 0 for v in result["received_bytes"]))
        with (downloader.output / "files.csv").open(encoding="utf-8-sig", newline="") as stream:
            rows = list(csv.DictReader(stream))
        self.assertEqual(len(rows), 16)
        stamps = [int(row["sender_start_ns"]) for row in rows]
        self.assertLess((stamps[-1] - stamps[0]) / 1e9, 0.45)
        self.assertTrue(all(float(row["sender_lateness_ms"]) >= 0 for row in rows))
        self.assertEqual(file_hash(source / "small.txt"), file_hash(downloader.output / "downloads" / "small.txt"))

    def test_all_profiles_exact_sizes_counts_and_timing_records(self):
        source, _, _, _, downloader = self.setup_standard("all", 2)
        result = downloader.run()
        self.assertTrue(result["counts_complete"])
        self.assertEqual(result["completed_files"], 6)
        self.assertEqual(result["finished_counts"], dict(small=2, medium=2, large=2))
        self.assertAlmostEqual(result["planned_seconds"], 12.14)
        self.assertGreaterEqual(result["elapsed"], result["planned_seconds"])
        self.assertTrue(all(count > 0 for count in result["assigned_blocks"]))
        for name in ("small.txt", "medium.txt", "large.txt"):
            self.assertEqual(file_hash(source / name), file_hash(downloader.output / "downloads" / name))
        with (downloader.output / "files.csv").open(encoding="utf-8-sig", newline="") as stream:
            rows = list(csv.DictReader(stream))
        self.assertEqual([int(row["size"]) for row in rows], [200, 200, 8000, 8000, 1500000, 1500000])
        self.assertTrue(all(row["sender_start_ns"] and row["status"] == "complete" for row in rows))

    def test_lost_start_metadata_retried_with_original_sender_timestamp(self):
        _, relay, _, _, downloader = self.setup_standard(count=10)
        original = relay.register
        dropped = False
        def lossy(packet, address):
            nonlocal dropped
            message = wire.parse_control(packet, relay.config["token"], relay.config["experiment_id"])
            if message and message["kind"] == "file_block_started" and not dropped:
                dropped = True
                return
            return original(packet, address)
        with patch.object(relay, "register", side_effect=lossy):
            result = downloader.run()
        self.assertTrue(dropped)
        self.assertTrue(result["counts_complete"])
        self.assertGreaterEqual(result["retries"][1], 1)
        self.assertLess(result["max_sender_lateness_ms"], 100)

    def test_timing_miss_reported_separately_from_successful_download(self):
        _, _, _, _, downloader = self.setup_standard(count=4)
        downloader.tolerance_ms = 0  # 严格零误差下，桌面系统的实际调度延迟应如实暴露。
        result = downloader.run()
        self.assertTrue(result["counts_complete"])
        self.assertFalse(result["timing_ok"])
        self.assertGreater(result["timing_violations"], 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
