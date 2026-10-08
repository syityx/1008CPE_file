# 真正的 iperf3 工具

Windows 首次启动由 `common/runtime.py` 下载 ar51an/iperf3-win-builds 的固定版本 **3.22 win64**（第三方 Cygwin 构建）。下载 ZIP 校验 SHA256：

`c9feabb3d721039508ccb81f74c2ada3ae11b9e75d33d24510573c2aa21da420`

上游程序：[esnet/iperf](https://github.com/esnet/iperf)，[许可证](https://github.com/esnet/iperf/blob/master/LICENSE)。Windows 构建：[ar51an/iperf3-win-builds](https://github.com/ar51an/iperf3-win-builds)。Cygwin：[cygwin.com](https://cygwin.com/)。二进制和压缩包不提交本仓库，不重新分发；两端首次使用都需要能访问下载地址。

手动安装：在工程根目录运行 `python -m iperf.common.runtime`。离线电脑可从上述固定版本下载页取得 ZIP，将 `iperf3.exe` 与 `cygwin1.dll` 放在本目录下的 `runtime/`。也可用入口的 `--iperf 完整路径` 指定已有 iperf3。

Linux 使用发行版 `iperf3`，例如 `sudo apt install iperf3`。建议两端同版本；本工程验证的是 Windows 3.22。
