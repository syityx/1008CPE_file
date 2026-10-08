# iperf 双路专网灌包

本目录新增两个真正调用 **iperf3** 的实验，与根目录 `send/receive` 的文件下载测试独立。原视频工程 `1008CPE` 未修改。

| 模式 | 入口目录 | 分流方式 | 默认灌包 |
| --- | --- | --- | --- |
| 反馈调整 | `adaptive/send`、`adaptive/receive` | 从50%/50%开始，每轮真实接收统计调整下一轮 | 总2 Mbps，5秒一轮，合计60秒 |
| 固定五五 | `fixed/send`、`fixed/receive` | 各1 Mbps，整个测试保持50%/50% | 两条流连续60秒 |

这里的分流指**两路发送速率预算之和等于设定总速率**。两条流是独立 iperf 数据，不是同一文件的分片，不做文件合流、SHA256校验或业务补包。UDP丢包允许发生并如实记录；分配各50%并不意味着接收字节数或包数一定各50%。

## 目录管理

```text
iperf/
  adaptive/
    config.json                 固定参数
    config.local.json           首次运行保存本机IP（不提交）
    send/main.py + start.cmd     发送端服务
    receive/main.py + start.cmd  接收端反馈控制
    results/                    本模式日志、原始iperf JSON和统计（不提交）
  fixed/                        同样结构，固定五五
  common/                       工具定位、专网路由、进程、统计、反馈适配器
  tools/                        下载说明；runtime/放真正iperf3（不提交）
  tests/                        逻辑测试和真实iperf集成验收
  launch.ps1                    两种模式共用Windows启动流程
```

两端下载完整仓库。Windows需要Python3.10或更新版本，第一次启动自动下载固定版本iperf3 3.22，下载地址和SHA256见[工具说明](tools/README.md)。离线环境可预先安装工具。启动脚本自动寻找Python，允许管理员窗口后添加本模式iperf防火墙规则；接收端设置对应主机路由。

## 怎么接、怎么启动

```text
发送电脑：Wi-Fi/LAN方向可达地址 A1；核心网业务方向可达地址 A2
接收电脑：Wi-Fi或手机USB共享Wi-Fi；另一路网线连接CPE

LAN数据：发送电脑 → Wi-Fi路由器 → Wi-Fi/手机USB → 接收电脑
CPE数据：发送电脑 → 核心网 → 基站 → CPE → 接收电脑
```

发送端需要能访问两个网络，可以用两张网卡，也可由路由设备接通核心网业务接口；本方案要求接收端能通过各自链路访问发送端的**两个不同IPv4目标地址**。目标地址应属于发送电脑，或是明确映射到发送电脑iperf端口的入口；不能把CPE地址填成发送端地址。核心网的管理IP也不一定是业务可达地址。仅能ping通CPE还不足以确认这条路径。

1. 选一种模式，发送电脑双击该模式的 `send/start.cmd`。
2. 接收电脑双击**同一种模式**的 `receive/start.cmd`。
3. 接收端首次提示时填写发送端 A1/A2，再选择本机对应的两张网卡。输入的地址和选择会保存到该模式的 `config.local.json`，以后双击启动即可。地址变化时删除该文件，或用命令行覆盖。
4. 默认约60秒灌包后接收端自动结束。反馈模式的总耗时另加各轮连接/统计开销；发送端继续监听，Ctrl+C停止。

当前专网A1/A2未知，因此不填虚构地址。发送端会显示本机IPv4供核对。双路同一目标IP会产生主机路由冲突，程序明确报错；不能仅凭绑定源IP就宣称分别走两张网卡。

| 模式 | LAN端口 | CPE端口 | 所需协议 |
| --- | ---: | ---: | --- |
| fixed | 5216 | 5217 | 两个端口均允许TCP及UDP |
| adaptive | 5226 | 5227 | 两个端口均允许TCP及UDP |

接收端是iperf **client**，主动建立TCP控制连接和UDP映射；发送端是iperf **server**，`-u -R`使UDP数据从发送端返回接收端。接收端在CPE/手机NAT后时可利用主动发起的连接，不要求发送端主动连接到接收电脑。实际设备的UDP回程策略仍需验证。发送端可达入口的防火墙/转发规则需要同时放行上述TCP和UDP。Windows脚本只配置本机规则。

固定模式不使用接收反馈调整带宽；iperf自身的TCP控制、UDP建链和结束统计仍存在。“无反馈调整”不表示完全没有反向报文。

本方案直接通过专网，不使用原阿里云UDP中继。原中继不能承载iperf所需的TCP控制；若以后改公网，需要另设能承载TCP+UDP的转发或VPN。

Windows接收端为A1/A2分别建立绑定所选网卡的 `/32` ActiveStore主机路由。同网段直接发送，跨网段用该网卡网关；已有冲突路由则停止，不删除其他规则。路线在重启后清除；切换拓扑前检查已有主机路由。没有默认网关的专网接口可在 `config.local.json` 中给相应分支加 `"gateway": "实际下一跳IPv4"`。两端和CPE/核心网也要有正确回程路由。

Linux可手动运行Python入口，但须自行安装兼容的iperf3、指定本机IP和配置两路路由；本次实际验证平台是Windows 3.22。

## 速率与反馈

`config.json` 已填写端口、总速率、时长、分片长度和超时。可选命令，在工程根目录执行：

```powershell
# 发送端二选一
python iperf/fixed/send/main.py
python iperf/adaptive/send/main.py

# 接收端；配置已保存后可以省略四个地址参数
python iperf/fixed/receive/main.py --lan-host A1 --cpe-host A2 --lan-ip B1 --cpe-ip B2 --rate 10000000 --duration 60
python iperf/adaptive/receive/main.py --lan-host A1 --cpe-host A2 --lan-ip B1 --cpe-ip B2 --rate 10000000 --duration 60 --epoch 5
```

将A1/A2/B1/B2替换为实际IPv4。这些手动命令需要事先放行本机iperf防火墙；Windows自动路由需管理员终端。双击入口已处理这些步骤。

反馈模式每轮并行启动两条iperf流，解析接收端 `end.sum_received` 的真实丢包率、接收速率、抖动和包数。两路都成功才采纳这一轮；单路连接失败、无有效UDP数据或进程超时，实验失败并停止另一条流，不冒充两路完成。

反馈控制复用 `send/fuzzy_pid.py` 原规则表和整数增量核心，输入改为两路**交付比例 `1-丢包率`**，不是文件合流缓存数量。将比例乘4000适配原误差乘0.01的论域；较大输入在前计算增量、再恢复两路符号，与原比较方式一致。暖机2轮；丢包差低于1个百分点维持比例；每个整数输出对应5个百分点，单轮最多改变10个百分点；LAN比例限制10%..90%，保留差路探测流量。导数瞬态指向当前差路时保持比例。无原20文件块窗口和暖机10窗口，原文件模式的 `G=5+4.5*u` 也没有直接搬过来。

两路无明显丢包时保持当前比例，不保证寻找最大吞吐；两路都拥塞时保持总灌包预算不变，不自动降总速率。抖动和吞吐用于记录，当前控制输入只有丢包率。可比较不同总速率和轮长下的表现，不把未经物理测试的参数称作最优参数。

普通iperf3不能在运行中修改`-b`，因此反馈模式重新启动下一轮。两条流不是严格同时开始，轮间有连接和统计间隙；程序记录启动偏差、启动间隙和实际总耗时。固定模式每个profile只启动一次，期间不调节、不轮询反馈控制器。

## 表7的等效负载

| profile | 表7业务 | 等效两路总目标速率 |
| --- | --- | ---: |
| small | 200B/20ms | 80000bps |
| medium | 8000B/50ms | 1280000bps |
| large | 1500000B/6s | 2000000bps |

```powershell
# 单类等效速率测60秒
python iperf/fixed/receive/main.py --profile small --duration 60
python iperf/adaptive/receive/main.py --profile large --duration 60

# 三类依次按2664/540/396秒执行，共3600秒实际灌包（反馈模式另有启动空隙）
python iperf/fixed/receive/main.py --profile standard
python iperf/adaptive/receive/main.py --profile standard
```

速率按大小×8÷周期计算，iperf默认用1200B UDP报文正文，按自身节奏灌包；目标`-b`不是含IP/UDP/以太网头的线上速率。**这不是表7逐份文件、严格首包间隔和业务次数的测试**：尤其1.5MB不能放进一个UDP包，2Mbps平滑灌包也不同于每6秒一次的大文件突发。需要文件大小、次数、重组校验和规定业务节奏时，继续用根目录原文件模式。结果始终标记 `standard_file_compliance=false`。

## 结果与验收

各模式的 `results/receive_时间/` 保存：

- `effective_config.json`：生效地址、速率和参数。
- 每轮每路的 `.command.json`、`.iperf.json`、`.stderr.txt`：真实命令、原始输出和错误。
- `epochs.csv`：本轮/下一轮比例、目标及接收速率、丢包、抖动、进程启动偏差和轮间启动间隙。启动指标不是网卡首包时间戳。
- `summary.json`：是否完整完成、错误、两路接收字节、累计灌包时长和实际总耗时。成功仅表示完整执行并两路收到数据，不代表满足某项丢包/吞吐阈值。

```powershell
python -m unittest iperf.tests.test_logic -v
python -m iperf.tests.validate_real
```

集成验证启动真正iperf服务器和客户端，并在一条回环路径上丢弃约25%的UDP数据报。验证固定五五不随丢包变化、反馈把下一轮预算移到好路、实际`-b`随之改变、总预算保持不变，以及单路失联明确失败。测试脚本的UDP桥只用于施加故障；产品数据由iperf发送。

当前仅完成本机真实iperf测试，尚未验证你的实际Wi-Fi/CPE专网，也未运行3600秒等效负载。实际验收时确认两端网络地址，查看两张接收网卡的流量及两路原始JSON；必要时抓包确认物理出口。
