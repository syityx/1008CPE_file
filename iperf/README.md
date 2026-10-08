# iperf 标准文件传输与单路对照

默认从实际文件读取数据，由真正的 **iperf3 TCP `-F`** 发送；接收端保存分段、合并并逐份校验SHA256。表7在本实验中**只约束文件大小**，不强制发送间隔、业务次数或一小时时长。

| 模式目录 | 链路 | 分配方式 |
| --- | --- | --- |
| `adaptive/` | LAN + CPE | 从50%/50%开始，根据真实接收反馈调整下一批 |
| `fixed/` | LAN + CPE | 每份文件的字节及目标速率固定各50% |
| `lan_only/` | 仅LAN | 100%走Wi-Fi/手机USB局域网；不检查CPE、不连接云、不下载工具 |
| `cpe_only/` | 仅CPE | 100%走CPE接口；不检查LAN/Wi-Fi |

CPE链路可以接专网，也可以接公网，前提是这一路能访问发送端的TCP文件控制和iperf服务。仅CPE模式不依赖局域网支路。现有阿里云UDP中继不适用于本TCP文件模式；不能直接将旧中继IP填成发送端地址。

## 目录

```text
send/standard_files/            三个实际标准源文件，与原文件工程共用
iperf/
  adaptive/                    双路反馈
  fixed/                       双路五五
  lan_only/                    仅局域网
  cpe_only/                    仅CPE
    config.json                固定参数，所有模式各有一份
    config.local.json          首次保存本机地址，不提交
    send/main.py + start.cmd   发送端入口
    receive/main.py + start.cmd 接收端入口
    results/                   本模式发送日志、接收文件、统计，不提交
  common/                      标准文件、准备控制、iperf、网络、反馈共用代码
  tools/runtime/               真正iperf3及DLL，不提交
  tests/                       真实工具验收和逻辑测试
  launch.ps1                   Windows启动/本机防火墙设置
```

根目录原`send/receive`文件实验入口和原视频工程`1008CPE`保留。

## 怎么启动

两台电脑都下载完整仓库，安装Python3.10或更新版本。发送端与接收端选**同一种模式**：

1. 发送端双击相应目录的 `send/start.cmd`。
2. 接收端双击该目录的 `receive/start.cmd`。
3. 当前专网地址已预填：发送端A有线`168.168.168.100`，接收端B有线`192.168.2.180`，CPE网关`192.168.2.230`。Wi-Fi沿用本机已保存的配置；首次缺少LAN配置时填写对应地址并选择网卡。保存到该模式的 `config.local.json` 后，后续直接双击即可。更换地址时用命令行覆盖或调整本机配置。
4. 默认总目标速率2Mbps，按小→中→大循环，各访问一次传一份，约60秒后在完整文件批次边界停止。发送端继续等待，Ctrl+C退出。

双路需要两张接收网卡，以及发送端两个不同的可达目标IPv4；单路只需要所选的一张网卡和一个目标地址。目标IP应是发送端地址，或明确转发到发送端所列TCP端口的入口；不要填接收端或CPE自身的地址。专网须有相应业务/回程路由，能ping通CPE不足以确认TCP能到发送端。

两端不是靠云服务器发现地址。本机IPv4用`ipconfig`查看。当前`adaptive/fixed/cpe_only`均已预填用户确认的专网地址；`config.local.json`中的已保存值优先于默认配置，若仍保存旧专网地址，可只删除其中的`cpe`节点后重启，不影响LAN配置。

当前实验拓扑：A的网线接核心网，B的网线接CPE；CPE蜂窝地址由核心网分配为`182.182.x.x`，LAN地址为`192.168.2.230`，NAT可以保持开启。B先经CPE主动连接A的`168.168.168.100`，A沿已建立的TCP连接把文件返回B。程序不要求手动填写CPE的蜂窝地址，也不要求A直接路由到B的`192.168.2.180`。B经CPE到A的文件控制和iperf TCP端口仍需连通。

### 离线局域网使用

`lan_only`从启动到传输都不访问公网、云端或CPE。工具需要事先准备：将已安装的 `iperf/tools/runtime/iperf3.exe` 和 `cygwin1.dll` 一起复制到两台电脑相同目录，再双击启动。未找到工具会直接提示，不尝试联网下载。

其他模式首次可自动下载固定Windows版本3.22；也能按上述方式离线准备。工具来源、SHA256和许可证链接见[tools/README.md](tools/README.md)。可用 `--iperf 完整路径` 指定已有程序。

## 实际文件与分流

| 文件 | 字节数 | 单位口径 |
| --- | ---: | --- |
| `send/standard_files/small.txt` | 200 | B |
| `send/standard_files/medium.txt` | 8000 | 8 kB，十进制 |
| `send/standard_files/large.txt` | 1500000 | 1.5 MB，十进制 |

发送端首次缺少文件时生成；已有同名文件只检查大小，不覆盖。可替换成同样大小的其他实际内容，程序会读取当前内容并重新计算SHA256。大小不符时报错。发送端可用 `--files-dir 目录` 指向包含这三个文件名的目录。

双路对每份标准文件按当前比例分成连续字节：例如200B固定五五时，LAN发送前100B，CPE发送后100B。每一批把同一路的分段按文件次序组成有限源文件，发送端iperf `-F`读取它，接收端iperf `-F`保存收到的数据。接收程序按偏移重组，每份完整文件都验证源文件SHA256，不把两个独立随机流称作文件合流。

单路发送整份文件，完全不准备、连接或等待另一支路。固定模式精确按文件字节五五；实际两路吞吐会受网络差异影响。

## TCP与旧UDP模式

iperf3 3.22明确禁止同时使用`-F`和UDP，因此真实文件模式使用TCP。TCP负责丢包重传，反馈输入改为**实际接收速率/本路目标速率**，不伪造UDP丢包率。

原合成UDP灌包仍可在`adaptive/fixed`两端显式选择：

```powershell
python iperf/fixed/send/main.py --payload synthetic
python iperf/fixed/receive/main.py --payload synthetic --rate 2000000 --duration 60
```

UDP模式仍使用iperf生成数据和原丢包反馈。需要自动放行防火墙时：

```powershell
powershell -ExecutionPolicy Bypass -File iperf/launch.ps1 -Mode fixed -Role send -Payload synthetic
powershell -ExecutionPolicy Bypass -File iperf/launch.ps1 -Mode fixed -Role receive -Payload synthetic
```

普通双击默认真实标准文件模式。两端都需更新、选择同一数据模式。

## 网络、端口和控制方向

| 模式 | iperf数据TCP | 文件准备控制TCP |
| --- | --- | --- |
| fixed | LAN 5216 / CPE 5217 | LAN 5316 / CPE 5317 |
| adaptive | LAN 5226 / CPE 5227 | LAN 5326 / CPE 5327 |
| lan_only | LAN 5236 | LAN 5336 |
| cpe_only | CPE 5237 | CPE 5337 |

接收端先主动连接发送端文件准备服务，获取标准文件大小/哈希，要求发送端按比例准备本批分段，再启动iperf client `-R`。发送端iperf server读取真实数据返回接收端。数据方向始终是发送→接收，TCP控制/确认会反向通信；固定和单路模式不运行分流反馈控制器。

CPE/手机NAT后的接收端主动发起连接，可使用建立的连接收数据。发送端入口须能被该支路访问，若本身位于NAT后则需相应TCP转发。这里没有公网自动中继。

Windows双击入口设置iperf程序的数据端口规则和Python程序的文件控制端口规则。网络设备/核心网/外部防火墙规则仍需对应放行TCP。旧UDP服务不受这些入口修改。

Windows接收端按所选网卡给发送端目标建立 `/32` ActiveStore主机路由。同网段直接发送，跨网段用该网卡网关；已有冲突路由停止，不删除其他路由。路由重启后清除。没有默认网关时，可在该模式 `config.local.json` 的对应分支加 `"gateway": "实际下一跳IPv4"`。单路只配置一条路线，不要求另一个接口或不同目标地址。

Linux可以手动使用Python入口，但需要自行安装兼容iperf、配置接口地址和两路路由。本次实际验证使用Windows iperf3 3.22。

## 可调测试参数

`config.json`中的`total_bps`默认2000000，`duration_seconds`默认60，`rounds`默认0（按时长循环），`files_per_batch`默认1，`count_limit`默认0。这些都是实验参数，不是表7强制条款。

在工程根目录运行；手动启动需已放行程序防火墙，自动设置Windows路由需管理员终端：

```powershell
# 仅局域网，两端各运行对应命令；无需CPE/公网地址
python iperf/lan_only/send/main.py
python iperf/lan_only/receive/main.py --lan-host A1 --lan-ip B1

# 仅CPE，不填写LAN地址
python iperf/cpe_only/send/main.py
python iperf/cpe_only/receive/main.py
# 覆盖旧本机配置时可明确指定已确认地址
python iperf/cpe_only/receive/main.py --cpe-host 168.168.168.100 --cpe-ip 192.168.2.180

# 双路反馈，10Mbps，循环120秒
python iperf/adaptive/receive/main.py --rate 10000000 --duration 120

# 小→中→大两轮；最长按默认60秒检查，可需要时加长duration
python iperf/fixed/receive/main.py --rounds 2

# 每类两份，完成一轮就停止；大小仍严格保持
python iperf/fixed/receive/main.py --count-limit 2

# 只循环大文件
python iperf/cpe_only/receive/main.py --profile large --duration 60
```

A1/A2是发送端目标，B1/B2是接收端所选接口的IPv4。已有本机配置时省略地址参数。`--profile standard`表示循环三个文件，不表示按表7的次数/间隔执行。正数`count-limit`指定每类一轮的份数；0按时长或轮数循环。定量测试达到时长但份数未完成会报未完成。时间在完整批次边界检查，TCP慢路可能让实际结束时间超过指定时长；Ctrl+C可提前停止并保存未完成记录。

反馈模式复用原模糊PID规则/整数输出核心：暖机2批，交付质量差低于1%保持比例，每次最多变化10个百分点，LAN比例限制10%..90%。`epoch_seconds`控制大批拆分的参考窗口，最小单位是一份文件；iperf不能在运行中修改`-b`，下一批会重新建连接。TCP文件统计包含测试收尾开销，小文件速率尤其受其影响。固定/单路模式不按反馈改变比例。

## 结果与验证

每个模式的`results/receive_时间/`保存原始iperf JSON、实际命令、接收`.bin`分段、源文件与分段哈希、`epochs.csv`和`summary.json`。记录文件份数、源文件SHA256、两路字节数、目标与接收速率、下一批比例、进程启动偏差/间隙和结束原因。只有大小及逐份内容校验成功才累计完成份数；单路故障不会冒充双路成功。

表7只要求大小，结果明确记录 `enforced_specifications=["file_size"]`。不把iperf内部块长、TCP分段或平均速率当成严格业务包头间隔，也不要求表中的完整份数。

```powershell
python -m unittest iperf.tests.test_logic iperf.tests.test_standard_source -v
python -m iperf.tests.validate_standard_files
python -m iperf.tests.validate_standard_files --cycle-only
python -m iperf.tests.validate_file_feedback
python -m iperf.tests.validate_real
```

已做本机真实工具验收：四种模式各传三种实际文件，逐份SHA256一致；固定五五字节严格相等；单路未连接另一支路；循环轮数正常；人为限制CPE路径后下一份文件的LAN分段增加且重组仍正确。旧UDP的固定/反馈/断路测试保留。测试桥只施加限速或丢包，不替代iperf发送文件。

实际Wi-Fi/CPE专网仍需双机验证。离线LAN模式的工具与单网卡检查也有测试覆盖。原视频工程及原文件下载代码保留。
