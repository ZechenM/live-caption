"""音频捕获:从 BlackHole 设备读取 16kHz 音频流,断连自动重试。"""

import queue
import threading
import time

import numpy as np
import sounddevice as sd


class AudioCapture:
    """从名称包含 device_keyword 的输入设备捕获音频,输出 16kHz 单声道 float32 帧。

    设备断开(如 AirPods 断连导致 CoreAudio 重排设备)时不会崩溃:
    音频流出错后自动关闭,按 retry_interval 秒轮询重连。
    """

    def __init__(self, device_keyword="BlackHole", samplerate=16000,
                 blocksize=1600, retry_interval=2.0, on_status=None,
                 highpass_hz=0):
        self.device_keyword = device_keyword.lower()
        self.samplerate = samplerate
        self.blocksize = blocksize
        self.retry_interval = retry_interval
        self.on_status = on_status or (lambda msg: print(f"[capture] {msg}"))
        self.frames = queue.Queue(maxsize=200)   # 单声道 float32 帧
        self._stop = threading.Event()
        self._stream_error = threading.Event()
        self._thread = None
        # 高通滤波 (FIR, 加窗sinc): 滤掉马达/震动等低频噪音, 保留人声
        self._hp = None
        if highpass_hz and highpass_hz > 0:
            n = 101
            fc = highpass_hz / samplerate
            m = np.arange(n) - (n - 1) / 2
            h_lp = 2 * fc * np.sinc(2 * fc * m) * np.hamming(n)
            h_lp /= h_lp.sum()
            h_hp = -h_lp
            h_hp[(n - 1) // 2] += 1.0
            self._hp = h_hp.astype(np.float32)
            self._hp_tail = np.zeros(n - 1, dtype=np.float32)

    # ---------- 设备 ----------

    def find_device(self):
        """返回匹配的输入设备索引,找不到返回 None。"""
        try:
            for idx, dev in enumerate(sd.query_devices()):
                if (self.device_keyword in dev["name"].lower()
                        and dev["max_input_channels"] > 0):
                    return idx, dev
        except Exception as e:
            self.on_status(f"查询音频设备失败: {e}")
        return None, None

    # ---------- 流 ----------

    def _callback(self, indata, frames, time_info, status):
        if status and (status.input_overflow is False):
            # 除 overflow 之外的状态多为设备异常
            self._stream_error.set()
        mono = indata.mean(axis=1) if indata.ndim > 1 else indata[:, 0]
        mono = mono.astype(np.float32, copy=True)
        if self._hp is not None:
            x = np.concatenate([self._hp_tail, mono])
            mono = np.convolve(x, self._hp, mode="valid").astype(np.float32)
            self._hp_tail = x[-(len(self._hp) - 1):]
        try:
            self.frames.put_nowait(mono)
        except queue.Full:
            pass  # 下游太慢时丢帧,保证实时性

    def _run(self):
        announced_missing = False
        while not self._stop.is_set():
            idx, dev = self.find_device()
            if idx is None:
                if not announced_missing:
                    self.on_status(
                        f"找不到输入设备「{self.device_keyword}」。"
                        f"请确认 BlackHole 已安装且系统输出已切到 Multi-Output Device,"
                        f"每 {self.retry_interval:.0f}s 自动重试…")
                    announced_missing = True
                time.sleep(self.retry_interval)
                continue
            announced_missing = False
            channels = min(2, dev["max_input_channels"])
            self._stream_error.clear()
            try:
                with sd.InputStream(device=idx, samplerate=self.samplerate,
                                    channels=channels, dtype="float32",
                                    blocksize=self.blocksize,
                                    callback=self._callback) as stream:
                    self.on_status(f"已连接音频设备: {dev['name']}")
                    while not self._stop.is_set():
                        if self._stream_error.is_set() or not stream.active:
                            raise RuntimeError("音频流中断")
                        time.sleep(0.25)
            except Exception as e:
                if self._stop.is_set():
                    break
                self.on_status(
                    f"音频流断开({e})。可能是设备断连(如 AirPods)。"
                    f"{self.retry_interval:.0f}s 后自动重连;"
                    f"若切换过输出设备,请重新运行 setup_audio.sh on。")
                time.sleep(self.retry_interval)

    # ---------- 生命周期 ----------

    def start(self):
        self._thread = threading.Thread(target=self._run, daemon=True,
                                        name="audio-capture")
        self._thread.start()

    def stop(self):
        self._stop.set()

    def read(self, timeout=0.5):
        """取一帧单声道音频;超时返回 None。"""
        try:
            return self.frames.get(timeout=timeout)
        except queue.Empty:
            return None
