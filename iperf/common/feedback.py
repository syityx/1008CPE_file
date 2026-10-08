"""iperf 反馈适配器：复用原模糊 PID 数值核心，输入改为接收交付质量。"""
from send.fuzzy_pid import strategy


class LossFeedback:
    def __init__(self, warmup_epochs=2):
        self.core = strategy()
        self.ratio = 0.5
        self.samples = 0
        self.warmup_epochs = warmup_epochs

    def update(self, lan, cpe):
        # 两路丢包率按比例归一化，避免低速路因为字节少而永远被判为差路。
        quality0 = 1 - lan["lost_percent"] / 100
        quality1 = 1 - cpe["lost_percent"] / 100
        return self.update_quality(quality0, quality1)

    def update_quality(self, quality0, quality1):
        """TCP文件模式传入接收速率/目标速率；不能把TCP重传隐瞒为UDP零丢包。"""
        if not 0 <= quality0 <= 1 or not 0 <= quality1 <= 1:
            raise ValueError("交付质量须为0..1。")
        self.samples += 1
        difference = quality0 - quality1
        delta = 0.0
        raw = 0
        if self.samples > self.warmup_epochs and abs(difference) >= 0.01:
            # 原核心内部误差再乘 0.01，4000 对应 10% 丢包差抵达误差论域边界。
            # 保留规则表/整数输出；不复用文件重排缓存的反馈语义和20块窗口。
            quality_counts = (round(4000 * quality0), round(4000 * quality1))
            # 与原data_process一致：较大值在前算增量，再根据两路顺序还原符号。
            raw = self.core.Fuzzy_PID_Increase(max(quality_counts), min(quality_counts))
            if difference < 0:
                raw = -raw
            delta = max(-0.1, min(0.1, raw * 0.05))
            # 导数项瞬态不可把更多预算分给当前明显更差的链路。
            if delta * difference < 0:
                delta = 0.0
            self.ratio = max(0.1, min(0.9, self.ratio + delta))
        return {"next_lan_ratio": self.ratio, "quality_difference": difference,
                "raw_fuzzy_output": raw, "delta": delta,
                "warmup": self.samples <= self.warmup_epochs}
