"""语音识别:滑动分段 + faster-whisper 日语转录。"""

import numpy as np

# Whisper 在静音/背景音乐上常见的幻觉输出, 直接丢弃
HALLUCINATIONS = (
    "ご視聴ありがとうございました",
    "チャンネル登録",
    "ご覧いただきありがとうございます",
    "おやすみなさい",
)


def is_hallucination(text):
    return any(h in text for h in HALLUCINATIONS)


import re as _re

_REPEAT_RE = _re.compile(r"(.{1,12}?)\1{3,}")   # 短片段连续重复4次以上


def looks_garbage(text):
    """检测 Whisper 的重复循环/乱码输出 (如 '10'10'10… )。"""
    if not text:
        return False
    m = _REPEAT_RE.search(text)
    if m and len(m.group(0)) >= 12:   # 重复总长须够长, 避免误杀正常拖长音
        return True
    # 长文本但字符种类极少, 也是循环的特征
    if len(text) > 40 and len(set(text)) / len(text) < 0.1:
        return True
    return False


class ChunkAssembler:
    """把连续音频帧组装成"一句话"级别的片段,做准实时分段。

    首选 Silero VAD 检测人声(对背景音乐免疫, faster-whisper 自带);
    不可用时回退到能量阈值检测。切分条件(任一满足):
      - 语音 >= min_chunk_sec 且人声停顿 >= silence_sec(说完一句)
      - 有短语音且停顿超过 0.8s(短句也及时出)
      - 缓冲达到 max_chunk_sec(强制切分,保证延迟上限)
    """

    def __init__(self, samplerate=16000, min_chunk_sec=2.0, max_chunk_sec=5.0,
                 silence_sec=0.5, energy_threshold=0.004, use_vad=True):
        self.sr = samplerate
        self.min_len = int(min_chunk_sec * samplerate)
        self.max_len = int(max_chunk_sec * samplerate)
        self.silence_len = int(silence_sec * samplerate)
        self.energy_threshold = energy_threshold
        self._buf = []
        self._buf_len = 0
        self._has_voice = False
        self._trailing_silence = 0
        self._since_check = 0
        from collections import deque
        self._rms_hist = deque(maxlen=80)   # 最近8s的帧RMS, 用于自适应底噪
        self._vad_get = None
        if use_vad:
            try:
                from faster_whisper.vad import (VadOptions,
                                                get_speech_timestamps)
                self._vad_get = get_speech_timestamps
                self._vad_opts = VadOptions(
                    min_silence_duration_ms=160,
                    min_speech_duration_ms=100,
                    speech_pad_ms=120)
            except Exception:
                pass   # 回退到能量检测

    @property
    def vad_enabled(self):
        return self._vad_get is not None

    def feed(self, frame):
        """喂入一帧;若切分出片段则返回 np.ndarray,否则返回 None。"""
        if self._vad_get is not None:
            return self._feed_vad(frame)
        return self._feed_energy(frame)

    # ---------- VAD 分段 (对 BGM 免疫) ----------

    def _feed_vad(self, frame):
        self._buf.append(frame)
        self._buf_len += len(frame)
        self._since_check += 1
        # 每 ~0.3s 或缓冲到上限时检查一次 (Silero 很快, 但没必要每帧跑)
        if self._since_check < 3 and self._buf_len < self.max_len:
            return None
        self._since_check = 0
        if self._buf_len < int(0.6 * self.sr):
            return None

        audio = np.concatenate(self._buf)
        ts = self._vad_get(audio, self._vad_opts)
        if not ts:
            # 全是 BGM/静音: 只保留最近 0.5s, 防止缓冲无限增长
            if self._buf_len > self.sr:
                keep = audio[-int(0.5 * self.sr):]
                self._buf = [keep]
                self._buf_len = len(keep)
            return None

        first_start = ts[0]["start"]
        last_end = ts[-1]["end"]
        speech_dur = last_end - first_start
        trailing = len(audio) - last_end

        end_utt = (speech_dur >= self.min_len
                   and trailing >= self.silence_len)
        short_utt = (speech_dur >= int(0.3 * self.sr)
                     and trailing >= int(0.8 * self.sr))
        forced = len(audio) >= self.max_len
        if not (end_utt or short_utt or forced):
            return None

        pad = int(0.15 * self.sr)
        s = max(0, first_start - pad)
        e = len(audio) if forced else min(len(audio), last_end + pad)
        chunk = audio[s:e]
        self._buf = []
        self._buf_len = 0
        return chunk

    # ---------- 能量分段 (自适应噪声地板) ----------

    def _noise_floor(self):
        """最近约8s音量的20分位数 ≈ 背景(BGM/底噪)的稳定水平。"""
        if len(self._rms_hist) < 20:
            return 0.0
        return float(np.percentile(self._rms_hist, 20))

    def _feed_energy(self, frame):
        rms = float(np.sqrt(np.mean(frame ** 2))) if len(frame) else 0.0
        self._rms_hist.append(rms)
        # 阈值 = max(固定下限, 背景底噪的1.8倍): BGM 稳定时人声一停就能测出
        thr = max(self.energy_threshold, self._noise_floor() * 1.8)
        is_voice = rms >= thr

        if not self._has_voice:
            if not is_voice:
                return None          # 前导静音直接丢弃
            self._has_voice = True

        self._buf.append(frame)
        self._buf_len += len(frame)
        self._trailing_silence = 0 if is_voice else \
            self._trailing_silence + len(frame)

        end_of_utterance = (self._buf_len >= self.min_len
                            and self._trailing_silence >= self.silence_len)
        if end_of_utterance or self._buf_len >= self.max_len:
            return self._flush()
        return None

    def _flush(self):
        chunk = np.concatenate(self._buf)
        self._buf = []
        self._buf_len = 0
        self._has_voice = False
        self._trailing_silence = 0
        return chunk

    def peek(self, min_sec=1.0):
        """返回当前未切分的语音缓冲(用于流式草稿识别); 不足则返回 None。"""
        if self._buf_len < int(min_sec * self.sr):
            return None
        if self._vad_get is None and not self._has_voice:
            return None
        return np.concatenate(self._buf)


# whisper 模型名 -> MLX 社区仓库 (Apple Silicon GPU 权重)
MLX_REPOS = {
    "large-v3-turbo": "mlx-community/whisper-large-v3-turbo",
    "large-v3": "mlx-community/whisper-large-v3-mlx",
    "medium": "mlx-community/whisper-medium-mlx",
    "small": "mlx-community/whisper-small-mlx",
}


class Transcriber:
    """whisper 封装,日语、可携带前文提示。

    后端优先用 MLX (Apple Silicon GPU, 同模型精度不变、快5-10倍),
    没装 mlx-whisper 时回退到 faster-whisper (CPU)。
    """

    def __init__(self, model_size="large-v3-turbo", language="ja",
                 compute_type="int8", beam_size=5, carry_context=True,
                 cpu_threads=0, backend="auto", on_status=None):
        import os
        self.on_status = on_status or (lambda msg: print(f"[asr] {msg}"))
        self.language = language
        self.beam_size = beam_size
        self.carry_context = carry_context
        self._prev_text = ""
        self.backend = None

        if backend in ("auto", "mlx"):
            try:
                import mlx_whisper  # noqa: F401
                self._mlx = mlx_whisper
                self.mlx_repo = MLX_REPOS.get(
                    model_size, f"mlx-community/whisper-{model_size}")
                self.backend = "mlx"
                self.on_status(f"正在加载 MLX GPU 模型 {self.mlx_repo}"
                               f"(首次运行会自动下载)…")
                import numpy as np
                self._mlx.transcribe(          # 预热: 触发下载和编译
                    np.zeros(16000, dtype=np.float32),
                    path_or_hf_repo=self.mlx_repo,
                    language=self.language, fp16=True, verbose=None)
            except ImportError:
                if backend == "mlx":
                    raise RuntimeError(
                        "指定了 mlx 后端但未安装: pip install mlx-whisper")
                self.on_status("未安装 mlx-whisper, 回退到 CPU (较慢)。"
                               "建议: pip install mlx-whisper")

        if self.backend is None:
            self.backend = "fw"
            if cpu_threads <= 0:
                cpu_threads = max(4, (os.cpu_count() or 8) - 2)
            self.on_status(f"正在加载 faster-whisper 模型 {model_size}"
                           f"({cpu_threads} 线程; 首次运行会自动下载)…")
            from faster_whisper import WhisperModel
            self.model = WhisperModel(model_size, device="cpu",
                                      compute_type=compute_type,
                                      cpu_threads=cpu_threads)
        backend_desc = ("MLX — Apple Silicon GPU 加速" if self.backend == "mlx"
                        else "faster-whisper — CPU")
        self.on_status(f">>> 识别引擎: {backend_desc} | 模型: {model_size}")

    def transcribe(self, audio, is_final=True):
        """audio: 16kHz float32 单声道。返回识别文本(可能为空串)。

        is_final=False 表示对未说完的缓冲做草稿识别, 不更新前文上下文。
        """
        # 把上一句的尾部作为提示, 让模型知道前文语境(人名/术语更稳)
        prompt = self._prev_text[-100:] if (self.carry_context
                                            and self._prev_text) else None
        if self.backend == "mlx":
            result = self._mlx.transcribe(
                audio,
                path_or_hf_repo=self.mlx_repo,
                language=self.language,
                initial_prompt=prompt,
                condition_on_previous_text=False,
                fp16=True,
                verbose=None,
            )
            # 过滤"无语音"概率过高、压缩比异常(重复循环)的段
            text = "".join(
                seg["text"] for seg in result.get("segments", [])
                if seg.get("no_speech_prob", 0.0) < 0.66
                and seg.get("compression_ratio", 1.0) < 2.6).strip()
        else:
            segments, _info = self.model.transcribe(
                audio,
                language=self.language,
                beam_size=self.beam_size,
                initial_prompt=prompt,
                vad_filter=True,
                vad_parameters={"min_silence_duration_ms": 300},
                condition_on_previous_text=False,
                without_timestamps=True,
            )
            text = "".join(seg.text for seg in segments).strip()
        if looks_garbage(text):
            return ""
        if is_final and text and not is_hallucination(text):
            self._prev_text = text
        return text
