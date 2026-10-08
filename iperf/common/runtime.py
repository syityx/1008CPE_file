"""定位真正的 iperf3，Windows 首次使用下载固定版本并校验 SHA256。"""
import hashlib
import os
from pathlib import Path
import shutil
import subprocess
import urllib.request
import zipfile

ROOT = Path(__file__).resolve().parents[1]
VERSION = "3.22"
URL = "https://github.com/ar51an/iperf3-win-builds/releases/download/3.22/iperf-3.22-win64.zip"
SHA256 = "c9feabb3d721039508ccb81f74c2ada3ae11b9e75d33d24510573c2aa21da420"


def install_windows():
    if os.name != "nt":
        raise ValueError("Linux 请先安装 iperf3，例如 sudo apt install iperf3。")
    folder = ROOT / "tools" / "runtime"
    folder.mkdir(parents=True, exist_ok=True)
    archive = ROOT / "tools" / "iperf-3.22.zip"
    print("首次下载 iperf3 3.22（第三方 Windows 构建，约 1.2 MB）…", flush=True)
    with urllib.request.urlopen(URL, timeout=60) as response:
        payload = response.read(5 * 1024 * 1024)
    if hashlib.sha256(payload).hexdigest() != SHA256:
        raise ValueError("iperf 压缩包 SHA256 不符，停止安装。")
    archive.write_bytes(payload)
    # 只提取运行必需的两个文件；不执行下载包中的脚本。
    with zipfile.ZipFile(archive) as source:
        for name in ("iperf3.exe", "cygwin1.dll"):
            entries = [entry for entry in source.namelist() if Path(entry).name == name]
            if len(entries) != 1:
                raise ValueError("iperf 压缩包缺少运行文件：" + name)
            (folder / name).write_bytes(source.read(entries[0]))
    return folder / "iperf3.exe"


def resolve_iperf(explicit=None):
    bundled = ROOT / "tools" / "runtime" / "iperf3.exe"
    if explicit:
        path = Path(explicit).expanduser().resolve()
    elif os.name == "nt":
        # 同一 Windows 实验优先使用固定版本，避免 PATH 中旧版的 JSON 差异。
        path = bundled if bundled.is_file() else install_windows()
    else:
        found = shutil.which("iperf3")
        if not found:
            raise ValueError("未找到真实 iperf3，请先安装或指定 --iperf。")
        path = Path(found)
    if not path.is_file():
        raise ValueError("找不到 iperf3：" + str(path))
    result = subprocess.run([str(path), "--version"], capture_output=True,
                            text=True, encoding="utf-8", errors="replace", timeout=10)
    version = (result.stdout + result.stderr).strip()
    if result.returncode or not version.startswith("iperf 3."):
        raise ValueError("指定程序不是可运行的 iperf3：" + version)
    return str(path), version


if __name__ == "__main__":
    path, version = resolve_iperf()
    print(path)
    print(version)
