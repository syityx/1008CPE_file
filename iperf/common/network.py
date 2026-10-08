"""专网直连配置。两个不同目标地址配合 /32 路由，确保使用两张接收网卡。"""
import ipaddress
import json
from pathlib import Path
import sys

from network_setup import windows_interfaces, ensure_host_route


def ipv4(value):
    address = ipaddress.IPv4Address(value)
    if address.is_unspecified or address.is_multicast:
        raise ValueError("请使用实际单播 IPv4 地址：" + value)
    return str(address)


def active_branches(mode):
    """单路模式从配置、路由到连接全过程都不访问另一支路。"""
    if mode == "lan_only":
        return ("lan",)
    if mode == "cpe_only":
        return ("cpe",)
    if mode in ("adaptive", "fixed"):
        return ("lan", "cpe")
    raise ValueError("未知链路模式：" + mode)


def prepare(config, config_path, overrides, loopback=False):
    names = active_branches(config["mode"])
    local_path = Path(config_path).with_name("config.local.json")
    local = json.loads(local_path.read_text(encoding="utf-8-sig")) if local_path.exists() else {}
    for name in names:
        config[name].update({key: value for key, value in local.get(name, {}).items()
                             if key in ("sender_ip", "bind_ip", "gateway")})
        config[name].update({k: v for k, v in overrides[name].items() if v is not None})
    if loopback:
        for n in names:
            ip = "127.0.0.1" if n == "lan" else "127.0.0.2"
            config[n].update(sender_ip=ip, bind_ip="127.0.0.1")
        return config
    interactive = sys.stdin.isatty()
    for name, label in (("lan", "Wi-Fi/手机USB"), ("cpe", "CPE专网")):
        if name not in names:
            continue
        if not config[name].get("sender_ip"):
            if not interactive:
                raise ValueError("缺少发送端 " + label + " 地址；双击启动填写，或使用 --" + name + "-host。")
            config[name]["sender_ip"] = input("输入发送端在" + label + "方向可达的IPv4：").strip()
        config[name]["sender_ip"] = ipv4(config[name]["sender_ip"])
    if len(names) == 2 and config["lan"]["sender_ip"] == config["cpe"]["sender_ip"]:
        raise ValueError("两路发送端目标地址相同，会导致 /32 出口冲突。请提供两个不同的可达地址。")
    rows = windows_interfaces() if sys.platform == "win32" else []
    selected = []
    for name, label in (("lan", "Wi-Fi/手机USB"), ("cpe", "CPE专网")):
        if name not in names:
            continue
        branch = config[name]
        if rows:
            # 包括没有默认网关的直连业务接口；由用户确认角色，不猜测未知专网网关。
            physical = [r for r in rows if not any(w in (r.get("description", "") + r.get("name", "")).lower()
                                                  for w in ("vmware", "vethernet", "clash", "virtual", "loopback"))]
            candidates = [r for r in physical if r["index"] not in selected]
            wanted = branch.get("bind_ip", "auto")
            if wanted in ("", "auto"):
                if not candidates:
                    raise ValueError("未找到" + label + "网卡，双路测试需要两个实际接口。")
                if len(candidates) > 1:
                    print("为" + label + "选择网卡：")
                    for i, row in enumerate(candidates, 1):
                        print(f"  {i}. {row['name']} {row['ip']} 网关={row.get('gateway')}")
                    if not interactive:
                        raise ValueError("多个网卡，请指定 --" + name + "-ip。")
                    index = int(input("网卡编号：")) - 1
                    if not 0 <= index < len(candidates):
                        raise ValueError("网卡编号无效。")
                    row = candidates[index]
                else:
                    row = candidates[0]
            else:
                row = next((r for r in candidates if r["ip"] == wanted), None)
                if row is None:
                    raise ValueError(label + "配置地址不是本机可用的独立网卡；删除 config.local.json 后重新选择。")
            selected.append(row["index"])
            branch["bind_ip"] = row["ip"]
            if config.get("auto_routes", True):
                same_subnet = ipaddress.IPv4Address(branch["sender_ip"]) in ipaddress.IPv4Network(
                    f"{row['ip']}/{row['prefix']}", strict=False)
                gateway = "0.0.0.0" if same_subnet else branch.get("gateway") or row.get("gateway")
                if not gateway:
                    raise ValueError(label + "没有跨网段网关，请在 config.local.json 的该分支填写 gateway。")
                ensure_host_route(branch["sender_ip"], gateway, row["index"])
        else:
            branch["bind_ip"] = ipv4(branch.get("bind_ip", ""))
    if len(names) == 2 and config["lan"]["bind_ip"] == config["cpe"]["bind_ip"]:
        raise ValueError("两路不能绑定同一个接收地址。")
    local_path.write_text(json.dumps({n: {k: config[n][k] for k in ("sender_ip", "bind_ip", "gateway")
                                         if k in config[n]} for n in names},
                                     ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print("本机地址配置已保存：", local_path)
    return config
