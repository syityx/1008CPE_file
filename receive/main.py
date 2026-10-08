"""接收端：自动发现发送端，默认按表7两路下载并校验，完成规定次数后退出。"""
import argparse
import logging
import sys
import uuid
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import network_setup
import protocol as wire
from file_transfer import DownloadTransport, FileDownloader, Cancelled
from standard_download import StandardDownloader


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default=str(Path(__file__).with_name("config.json")))
    parser.add_argument("--lan-ip")
    parser.add_argument("--cpe-ip")
    parser.add_argument("--sender-ip")
    parser.add_argument("--mode", choices=("fixed", "fuzzy"), help="fixed为固定五五，fuzzy沿用原PID")
    parser.add_argument("--model", choices=("standard", "loop"), help="standard按表7有限测试，loop为旧循环下载")
    parser.add_argument("--profile", choices=("all", "small", "medium", "large"), help="标准模式业务类型，all依次运行三类共1小时")
    parser.add_argument("--count-limit", type=int, help="调试时每类最多传输次数，0使用标准完整次数")
    parser.add_argument("--rounds", type=int, help="下载轮数，0为不限")
    parser.add_argument("--duration", type=float, help="下载总秒数，0为不限")
    parser.add_argument("--block-kib", type=int)
    parser.add_argument("--output", help="结果目录，默认receive/results下新建实验目录")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    config = wire.load_config(args.config)
    app = None
    try:
        overrides = dict(lan_bind_ip=args.lan_ip, cpe_bind_ip=args.cpe_ip, sender_host=args.sender_ip,
                         file_mode=args.mode, file_rounds=args.rounds, file_duration=args.duration, file_block_kib=args.block_kib,
                         file_test_model=args.model, standard_profile=args.profile, standard_count_limit=args.count_limit)
        config.update({key: value for key, value in overrides.items() if value is not None})
        if config.get("file_rounds", 0) < 0 or config.get("file_duration", 0) < 0:
            raise ValueError("轮数与时长不能为负数。")
        network_setup.prepare_receiver(config)
        if config["sender_host"] not in ("auto", "", "0.0.0.0"):
            network_setup.ensure_sender_route(config, config["sender_host"])
        name = datetime.now().strftime("%Y%m%d_%H%M%S_") + uuid.uuid4().hex[:6]
        output = Path(args.output) if args.output else Path(__file__).with_name("results") / name
        app = DownloadTransport(config)
        if config.get("file_test_model", "standard") == "standard" and args.rounds is not None:
            raise ValueError("标准模式按表中次数自动结束；--rounds仅适用于--model loop。")
        downloader = StandardDownloader(config, app, output) if config.get("file_test_model", "standard") == "standard" else FileDownloader(config, app, output)
        app.start()
        logging.info("结果目录：%s；按Ctrl+C停止并保存", output)
        result = downloader.run()
        logging.info("下载结束：%s", result)
        return 0 if result.get("counts_complete", True) and result.get("timing_ok", True) else 2
    except (Cancelled, KeyboardInterrupt):
        return 0
    except (OSError, ValueError, KeyError) as exc:
        logging.error("文件下载失败：%s", exc)
        return 1
    finally:
        if app:
            app.close()


if __name__ == "__main__":
    raise SystemExit(main())
