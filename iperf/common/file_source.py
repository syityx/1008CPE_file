"""真实标准文件的分段与批次；分流的是同一文件的连续字节，不复制两份业务。"""
import hashlib
from pathlib import Path

from standard_model import SPECS, create_standard_files

PROJECT = Path(__file__).resolve().parents[2]


def source_manifest(folder):
    create_standard_files(folder)
    return {name: {"filename": spec["name"], "size": spec["size"],
                   "sha256": hashlib.sha256((Path(folder) / spec["name"]).read_bytes()).hexdigest()}
            for name, spec in SPECS.items()}


def portions(size, ratio, names):
    if names == ("lan",):
        return {"lan": (0, size)}
    if names == ("cpe",):
        return {"cpe": (0, size)}
    boundary = round(size * ratio)
    return {"lan": (0, boundary), "cpe": (boundary, size - boundary)}


def write_batch(source, destination, offset, length, count, expected_hash):
    payload = Path(source).read_bytes()
    if hashlib.sha256(payload).hexdigest() != expected_hash:
        raise ValueError("标准源文件在测试期间改变，停止传输。")
    if type(offset) is not int or type(length) is not int or not 0 <= offset < len(payload) or not 0 < length <= len(payload) - offset:
        raise ValueError("标准文件分段超出范围。")
    piece = payload[offset:offset + length]
    digest = hashlib.sha256()
    with Path(destination).open("xb") as stream:
        for _ in range(count):
            stream.write(piece)
            digest.update(piece)
    return digest.hexdigest()


def verify_batch(paths, descriptions, count, expected_size, expected_hash):
    """按源文件偏移重组，每次只保存一份文件到内存；所有业务均逐份校验。"""
    ordered = sorted(descriptions, key=lambda name: descriptions[name]["offset"])
    boundary = 0
    for name in ordered:
        desc = descriptions[name]
        if desc["offset"] != boundary or desc["length"] <= 0 or desc["count"] != count:
            raise ValueError("两路分段存在空洞、重叠或业务次数不一致。")
        boundary += desc["length"]
        path = Path(paths[name])
        if path.stat().st_size != desc["bytes"]:
            raise ValueError(name + " 接收文件长度不符。")
        if hashlib.sha256(path.read_bytes()).hexdigest() != desc["stream_sha256"]:
            raise ValueError(name + " 接收分段SHA256不符。")
    if boundary != expected_size:
        raise ValueError("重组大小不等于标准文件大小。")
    streams = {name: Path(paths[name]).open("rb") for name in ordered}
    try:
        for _ in range(count):
            digest = hashlib.sha256()
            for name in ordered:
                digest.update(streams[name].read(descriptions[name]["length"]))
            if digest.hexdigest() != expected_hash:
                raise ValueError("重组标准文件SHA256不符。")
    finally:
        for stream in streams.values():
            stream.close()
    return count
