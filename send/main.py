"""发送端：默认按表7定时提供标准文件，响应Wi-Fi/CPE两路下载请求。"""
import argparse
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import network_setup
import protocol as wire
from file_transfer import FileSender


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default=str(Path(__file__).with_name("config.json")))
    parser.add_argument("--files-dir", help="自定义文件目录；默认标准文件使用standard_files，旧循环使用files")
    parser.add_argument("--model", choices=("standard", "loop"), help="standard按表7定时，loop为旧循环下载")
    parser.add_argument("--lan-ip", help="手动指定Wi-Fi地址，默认自动")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    config = wire.load_config(args.config)
    app = None
    try:
        if args.lan_ip:
            config["lan_bind_ip"] = args.lan_ip
        if args.model:
            config["file_test_model"] = args.model
        folder = args.files_dir or str(Path(__file__).with_name("standard_files" if config.get("file_test_model") == "standard" else "files"))
        network_setup.prepare_sender(config)
        app = FileSender(config, folder)
        app.start()
        while not app.stop.wait(0.5):
            pass
        return 1
    except KeyboardInterrupt:
        return 0
    except (OSError, ValueError) as exc:
        logging.error("文件发送端启动失败：%s", exc)
        return 1
    finally:
        if app:
            app.close()


if __name__ == "__main__":
    raise SystemExit(main())
