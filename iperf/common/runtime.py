"""检查已准备的真实iperf3；缺组件直接报错，不下载或安装。"""
import argparse
import os
from pathlib import Path
import shutil
import subprocess

ROOT = Path(__file__).resolve().parents[1]


def resolve_iperf(explicit=None):
    bundled = ROOT / "tools" / "runtime" / "iperf3.exe"
    if explicit:
        path = Path(explicit).expanduser().resolve()
    elif os.name == "nt":
        path = bundled
    else:
        found = shutil.which("iperf3")
        if not found:
            raise ValueError("缺少组件：iperf3。请自行安装，或用 --iperf 指定已有程序。")
        path = Path(found)
    required = [path]
    if os.name == "nt":
        # 默认包使用Cygwin；其他构建只在引用该DLL时检查，不强制静态构建携带DLL。
        uses_cygwin = path == bundled.resolve()
        if path.is_file() and not uses_cygwin:
            uses_cygwin = b"cygwin1.dll" in path.read_bytes().lower()
        if uses_cygwin:
            required.append(path.with_name("cygwin1.dll"))
    missing = [str(item) for item in required if not item.is_file()]
    if missing:
        raise ValueError("缺少组件：\n" + "\n".join("  " + item for item in missing) +
                         "\n请自行准备对应iperf发行包；默认放入iperf/tools/runtime，也可用--iperf指定已有程序。")
    result = subprocess.run([str(path), "--version"], capture_output=True,
                            text=True, encoding="utf-8", errors="replace", timeout=10)
    version = (result.stdout + result.stderr).strip()
    if result.returncode or not version.startswith("iperf 3."):
        raise ValueError("iperf3无法运行：" + str(path) + f"，退出码={result.returncode}。" +
                         (version or "请检查该发行包的运行DLL及系统架构是否匹配。"))
    return str(path), version


def main():
    parser = argparse.ArgumentParser(description="只检查本机iperf组件，不访问下载地址")
    parser.add_argument("--iperf", help="已有iperf3完整路径")
    parser.add_argument("--path-only", action="store_true", help="检查成功后只输出程序路径，供启动脚本使用")
    args = parser.parse_args()
    try:
        path, version = resolve_iperf(args.iperf)
        print(path)
        if not args.path_only:
            print(version)
        return 0
    except (ValueError, OSError, subprocess.SubprocessError) as error:
        print("组件检查失败：" + str(error))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
