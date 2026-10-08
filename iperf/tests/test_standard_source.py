import copy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from iperf.common.file_runner import file_block_length, file_command, validate
from iperf.common.file_source import portions, source_manifest, verify_batch, write_batch
from iperf.common.network import prepare
from iperf.common.runtime import ROOT, resolve_iperf
from standard_model import SPECS


class StandardSourceTests(unittest.TestCase):
    def test_actual_three_files_split_and_reassemble(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            manifests = source_manifest(folder)
            for profile, spec in SPECS.items():
                source = folder / spec["name"]
                self.assertEqual(source.stat().st_size, spec["size"])
                # 使用不同于默认生成器的真实内容，防止测试仅验证等效流量。
                source.write_bytes(bytes((i * 37 + 11) % 256 for i in range(spec["size"])))
                digest = hashlib.sha256(source.read_bytes()).hexdigest()
                for names, ratio in ((("lan", "cpe"), 0.5), (("lan", "cpe"), 0.6), (("lan",), 1), (("cpe",), 0)):
                    chunks = portions(spec["size"], ratio, names)
                    paths, desc = {}, {}
                    for name, (offset, length) in chunks.items():
                        path = folder / (profile + name + ".bin")
                        path.unlink(missing_ok=True)
                        stream_hash = write_batch(source, path, offset, length, 2, digest)
                        paths[name] = path
                        desc[name] = dict(offset=offset, length=length, count=2, bytes=length * 2, stream_sha256=stream_hash)
                    self.assertEqual(verify_batch(paths, desc, 2, spec["size"], digest), 2)
                    self.assertEqual(sum(p.stat().st_size for p in paths.values()), 2 * spec["size"])

    def test_modified_source_and_received_corruption_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            source = folder / "src"
            source.write_bytes(b"abcd")
            digest = hashlib.sha256(b"abcd").hexdigest()
            dest = folder / "recv"
            stream_hash = write_batch(source, dest, 0, 4, 2, digest)
            desc = dict(offset=0, length=4, count=2, bytes=8, stream_sha256=stream_hash)
            dest.write_bytes(b"xbcdabcd")
            with self.assertRaises(ValueError):
                verify_batch({"lan": dest}, {"lan": desc}, 2, 4, digest)
            source.write_bytes(b"xbcd")
            with self.assertRaises(ValueError):
                write_batch(source, folder / "other", 0, 4, 1, digest)

    def test_single_branch_requires_only_selected_interface_and_route(self):
        for mode, name in (("lan_only", "lan"), ("cpe_only", "cpe")):
            with tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / "config.json"
                config = dict(mode=mode, auto_routes=True, **{name: dict(sender_ip="192.0.2.10", bind_ip="auto", port=5236)})
                row = dict(ip="192.0.2.20", prefix=24, index=8, name="Ethernet", description="Real adapter", gateway=None)
                with patch("iperf.common.network.sys.platform", "win32"), \
                     patch("iperf.common.network.windows_interfaces", return_value=[row]), \
                     patch("iperf.common.network.ensure_host_route") as route:
                    result = prepare(config, path, {name: {}}, loopback=False)
                    route.assert_called_once_with("192.0.2.10", "0.0.0.0", 8)
                self.assertEqual(result[name]["bind_ip"], "192.0.2.20")
                local = json.loads(path.with_name("config.local.json").read_text())
                self.assertEqual(set(local), {name})

    def test_unused_branch_is_not_validated(self):
        for mode in ("lan_only", "cpe_only"):
            config = json.loads((ROOT / mode / "config.json").read_text())
            other = "cpe" if mode == "lan_only" else "lan"
            config[other] = dict(port="unavailable", sender_ip="invalid")
            validate(config)

    def test_file_command_is_tcp_and_carries_real_output_file(self):
        command = file_command("iperf3", dict(sender_ip="192.0.2.1", bind_ip="192.0.2.2", port=5216),
                               dict(bytes=400, length=100), Path("result.bin"), 40000, 5000)
        self.assertIn("-F", command)
        self.assertIn("-n", command)
        self.assertIn("-R", command)
        self.assertNotIn("-u", command)

    def test_large_file_tail_not_truncated_or_padded(self):
        for length in (750000, 1500000, 900000, 600000, 825000):
            block = file_block_length(length)
            self.assertLessEqual(block, 65536)
            self.assertEqual(length % block, 0)

    def test_missing_components_do_not_access_network_or_run_process(self):
        with patch("iperf.common.runtime.os.name", "nt"), \
             patch("pathlib.Path.is_file", return_value=False), \
             patch("urllib.request.urlopen") as network, \
             patch("iperf.common.runtime.subprocess.run") as run:
            with self.assertRaisesRegex(ValueError, "缺少组件") as error:
                resolve_iperf()
            self.assertIn("iperf3.exe", str(error.exception))
            self.assertIn("cygwin1.dll", str(error.exception))
            network.assert_not_called()
            run.assert_not_called()

    def test_missing_cygwin_dll_stops_before_executable_starts(self):
        with tempfile.TemporaryDirectory() as tmp:
            exe = Path(tmp) / "iperf3.exe"
            exe.write_bytes(b"test executable importing cygwin1.dll")
            with patch("iperf.common.runtime.os.name", "nt"), \
                 patch("iperf.common.runtime.subprocess.run") as run:
                with self.assertRaisesRegex(ValueError, "缺少组件") as error:
                    resolve_iperf(exe)
                self.assertIn(str(exe.with_name("cygwin1.dll")), str(error.exception))
                run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
