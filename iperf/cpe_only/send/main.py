"""标准文件 cpe_only send 入口，保留可选合成UDP模式。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from iperf.common.file_runner import main

if __name__ == "__main__":
    raise SystemExit(main("cpe_only", "send"))
