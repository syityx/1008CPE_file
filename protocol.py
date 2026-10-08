"""1008CPE_file注册与文件请求共用协议。公开token用于实验匹配，不加密文件。"""
import errno
import hashlib
import hmac
import json
import socket
from pathlib import Path

CONTROL_MAGIC = b"FZC1"


def load_config(path):
    config = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    if len(config.get("token", "")) < 16:
        raise ValueError("token 至少 16 个字符，两端和云端必须一致。")
    return config


def lan_endpoint(ip, port):
    """检查云端公告的IPv4和端口，避免无效公告导致接收线程退出。"""
    try:
        socket.inet_pton(socket.AF_INET, ip)
        if ip == "0.0.0.0" or type(port) is not int or not 1 <= port <= 65535:
            return None
    except (TypeError, OSError):
        return None
    return ip, port


def signed_message(magic, payload, token):
    signature = hmac.new(token.encode(), magic + payload, hashlib.sha256).digest()
    return magic + signature + payload


def verify_message(packet, magic, token):
    if len(packet) < 36 or not packet.startswith(magic):
        return None
    payload = packet[36:]
    signature = hmac.new(token.encode(), magic + payload, hashlib.sha256).digest()
    return payload if hmac.compare_digest(packet[4:36], signature) else None


def make_control(kind, token, experiment_id, **fields):
    content = {"kind": kind, "experiment_id": experiment_id, **fields}
    payload = json.dumps(content, separators=(",", ":"), ensure_ascii=True).encode()
    return signed_message(CONTROL_MAGIC, payload, token)


def parse_control(packet, token, experiment_id):
    if len(packet) > 4096:
        return None
    payload = verify_message(packet, CONTROL_MAGIC, token)
    if payload is None:
        return None
    try:
        content = json.loads(payload)
    except (ValueError, UnicodeDecodeError):
        return None
    if not isinstance(content, dict) or content.get("experiment_id") != experiment_id:
        return None
    return content


def udp_socket(ip, port):
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 1024 * 1024)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 1024 * 1024)
        try:
            sock.bind((ip, port))
        except OSError as exc:
            # 显示冲突端点；不复用端口，避免不同实验进程争抢UDP数据。
            if exc.errno == errno.EADDRINUSE or getattr(exc, "winerror", None) == 10048:
                raise OSError(
                    exc.errno,
                    "本机 UDP {}:{} 已被其他程序占用。请关闭旧的发送/接收程序后重试；"
                    "Windows 可在 PowerShell 执行："
                    "Get-NetUDPEndpoint -LocalPort {} | "
                    "Select-Object LocalAddress,LocalPort,OwningProcess".format(ip, port, port),
                ) from exc
            raise
        sock.settimeout(0.2)
        # Windows 的 UDP 对端未启动时，不让 ICMP 错误变成接收线程异常。
        if hasattr(socket, "SIO_UDP_CONNRESET"):
            sock.ioctl(socket.SIO_UDP_CONNRESET, False)
        return sock
    except Exception:
        sock.close()
        raise
