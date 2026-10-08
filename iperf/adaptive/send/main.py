"""反馈分流 send 入口。"""
import sys
from pathlib import Path

# 支持在任意工作目录直接运行入口。
sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from iperf.common.runner import main

if __name__ == "__main__":
    raise SystemExit(main("adaptive", "send"))
