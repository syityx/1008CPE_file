# 真正的 iperf3 工具

所有模式启动时只检查本机组件，**不会自动下载或安装**。缺少组件会直接显示缺失项及路径并停止启动。

Windows 推荐手动准备本工程已验证的 **3.22 win64**（ar51an/iperf3-win-builds 第三方 Cygwin 构建）。可自行从[固定版本ZIP](https://github.com/ar51an/iperf3-win-builds/releases/download/3.22/iperf-3.22-win64.zip)取得组件，ZIP 的 SHA256：

`c9feabb3d721039508ccb81f74c2ada3ae11b9e75d33d24510573c2aa21da420`

上游程序：[esnet/iperf](https://github.com/esnet/iperf)，[许可证](https://github.com/esnet/iperf/blob/master/LICENSE)。Windows 构建：[ar51an/iperf3-win-builds](https://github.com/ar51an/iperf3-win-builds)。Cygwin：[cygwin.com](https://cygwin.com/)。二进制和压缩包不提交本仓库，不重新分发；两端都需提前准备工具，支持离线复制。

将解压后的 `iperf3.exe` 与 `cygwin1.dll` 一起放在本目录下的 `runtime/`；离线电脑可直接复制这两个文件。也可用入口的 `--iperf 完整路径` 指定已有 iperf3，并准备其发行包所需的 DLL。

在工程根目录运行 `python -m iperf.common.runtime` 可检查组件及版本；这个命令只检查，不下载、不安装。双击各模式入口时也会先执行组件检查，成功后才继续设置防火墙和启动实验。

Linux 使用发行版 `iperf3`，例如 `sudo apt install iperf3`。建议两端同版本；本工程验证的是 Windows 3.22。
