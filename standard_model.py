"""表7单业务流模型：十进制文件大小，业务文件数，发送端包头到包头定时。

默认分段发送小/中/大业务，周期总和恰好一小时。UDP分片和重传不计为
新的业务文件。缩短次数只用于调试，结果中明确标记为非完整标准测试。
"""
import ctypes
import sys
import time
from pathlib import Path

SPECS = {
    "small": dict(name="small.txt", size=200, interval_ns=20_000_000, count=133200),
    "medium": dict(name="medium.txt", size=8000, interval_ns=50_000_000, count=10800),
    "large": dict(name="large.txt", size=1500000, interval_ns=6_000_000_000, count=66),
}


def make_plan(profile="all", count_limit=0):
    if profile not in ("all", *SPECS):
        raise ValueError("标准类型需要是all/small/medium/large。")
    if type(count_limit) is not int or count_limit < 0:
        raise ValueError("标准调试次数必须是非负整数，0表示使用表中完整次数。")
    plan, offset = [], 0
    for key, spec in SPECS.items():
        if profile not in ("all", key):
            continue
        count = min(count_limit, spec["count"]) if count_limit else spec["count"]
        plan.append({**spec, "profile": key, "count": count, "offset_ns": offset})
        offset += count * spec["interval_ns"]
    return plan


def planned_jobs(plan):
    """逐个产生任务，避免把133200个文件或全部下载数据常驻内存。"""
    sequence = 0
    for spec in plan:
        for number in range(spec["count"]):
            yield dict(sequence=sequence, profile=spec["profile"], number=number,
                       due_ns=spec["offset_ns"] + number * spec["interval_ns"], spec=spec)
            sequence += 1


def create_standard_files(folder):
    """独立目录生成标准文件；已有同名文件大小不符时报错，不覆盖个人文件。"""
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    for key, spec in SPECS.items():
        path = folder / spec["name"]
        if path.exists():
            if not path.is_file() or path.stat().st_size != spec["size"]:
                raise ValueError(f"标准文件{path}必须为{spec['size']}字节；请换目录或移走旧文件。")
            continue
        pattern = (f"1008CPE_file standard {key}\n" * 128).encode()
        with path.open("xb") as stream:
            remaining = spec["size"]
            while remaining:
                data = pattern[:min(remaining, len(pattern))]
                stream.write(data)
                remaining -= len(data)


class PreciseTimer:
    """Windows进程运行期间申请1ms计时精度，退出时配对恢复。"""
    def __enter__(self):
        self.active = sys.platform == "win32" and ctypes.windll.winmm.timeBeginPeriod(1) == 0
        return self

    def __exit__(self, *_):
        if self.active:
            ctypes.windll.winmm.timeEndPeriod(1)


def wait_until(due_ns, stop):
    """按perf_counter时钟等待，旧Windows Python也使用高精度内核定时器。

Event.wait在旧Python/Windows上可能按约15.6ms取整。使用Windows10的
高精度waitable timer，每次最多等待100ms，兼顾定时精度和退出响应。
旧系统不提供该功能时回退；真实计时误差仍由结果如实记录。
    """
    handle = None
    if sys.platform == "win32":
        from ctypes import wintypes
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        create = kernel.CreateWaitableTimerExW
        create.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD]
        create.restype = wintypes.HANDLE
        set_timer = kernel.SetWaitableTimer
        set_timer.argtypes = [wintypes.HANDLE, ctypes.POINTER(ctypes.c_longlong), wintypes.LONG,
                             ctypes.c_void_p, ctypes.c_void_p, wintypes.BOOL]
        set_timer.restype = wintypes.BOOL
        wait = kernel.WaitForSingleObject
        wait.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        wait.restype = wintypes.DWORD
        close = kernel.CloseHandle
        close.argtypes = [wintypes.HANDLE]
        close.restype = wintypes.BOOL
        # CREATE_WAITABLE_TIMER_HIGH_RESOLUTION，权限仅修改定时器和等待。
        handle = create(None, None, 0x2, 0x00100002)
    try:
        while not stop.is_set():
            remaining = (due_ns - time.perf_counter_ns()) / 1e9
            if remaining <= 0:
                return True
            seconds = min(remaining, 0.1)
            if handle:
                due = ctypes.c_longlong(-max(1, int(seconds * 10_000_000)))
                if not set_timer(handle, ctypes.byref(due), 0, None, None, False):
                    raise OSError(ctypes.get_last_error(), "无法设置高精度发送定时器")
                if wait(handle, 1000) != 0:
                    raise OSError(ctypes.get_last_error(), "高精度发送定时器等待失败")
            elif stop.wait(seconds):
                return False
        return False
    finally:
        if handle:
            close(handle)
