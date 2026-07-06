"""翻译:调用本地 Ollama 做日→中翻译,带近句上下文,流式输出。

历史采用"追加为主、偶尔裁剪"的策略而不是滑动窗口:
Ollama/llama.cpp 会对与上一次请求相同的前缀复用 KV cache,
前缀稳定 => 每句只需预填充新增的几十个 token, 首字延迟大幅下降。
"""

import json
import re

import requests

SYSTEM_PROMPT = (
    "你是专业的日译中字幕引擎。输入是语音识别得到的日语,可能有少量识别错误、"
    "缺标点或断句不完整。要求:"
    "1) 只输出简体中文译文,不要任何解释、注音、原文或标注;"
    "2) 口语化、自然,符合字幕习惯;"
    "3) 结合前文语境理解,遇到明显的识别错误按最合理的原意翻译;"
    "4) 人名、专有名词与前文译法保持一致;"
    "5) 语气词(ね、よ、さ等)不必逐字译出;"
    "6) 即使原文含糊、缺上下文或像是半句话, 也直接给出最可能的译文本身,"
    "绝对不要解释、分析、提问或讨论翻译选项。你的输出会被直接当作字幕显示。"
)

# 模型"思考外泄"的特征词; 命中且文本异常长时触发提取
_META_MARKERS = ("可以翻译为", "翻译为“", "最终答案", "请提供更多", "如果需要",
                 "根据你的", "更符合", "语境", "直译", "意译", "这句话的意思")


class OllamaTranslator:
    def __init__(self, base_url="http://localhost:11434", model="qwen2.5:7b",
                 context_pairs=4, timeout=30, temperature=0.3, on_status=None):
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout = timeout
        self.temperature = temperature
        self.on_status = on_status or (lambda msg: print(f"[translate] {msg}"))
        self.context_pairs = context_pairs
        self.history = []            # [(ja, zh), ...] 追加为主, 超限才裁剪

    # ---------- 可用性检查 ----------

    def check(self):
        """返回 (ok: bool, message: str)。"""
        try:
            r = requests.get(f"{self.base_url}/api/tags", timeout=3)
            r.raise_for_status()
        except Exception:
            return False, (
                f"无法连接 Ollama({self.base_url})。"
                "请先运行 `ollama serve`(或打开 Ollama App)。")
        names = [m.get("name", "") for m in r.json().get("models", [])]
        if not any(n == self.model or n.split(":")[0] == self.model.split(":")[0]
                   and n.startswith(self.model) for n in names) \
                and self.model not in names:
            return False, (
                f"Ollama 已运行,但未找到模型 {self.model}。"
                f"请先执行 `ollama pull {self.model}`。已有模型: {names or '无'}")
        return True, f"Ollama 正常,模型 {self.model} 可用。"

    def warmup(self):
        """预加载模型 + 预填充 system prompt 的 KV cache。"""
        try:
            requests.post(f"{self.base_url}/api/chat",
                          json={"model": self.model,
                                "messages": [
                                    {"role": "system", "content": SYSTEM_PROMPT},
                                    {"role": "user", "content": "はい"}],
                                "stream": False,
                                "keep_alive": "60m",
                                "options": {"num_predict": 4,
                                            "num_ctx": 4096}},
                          timeout=120)
            self.on_status("翻译模型已预加载。")
        except Exception as e:
            self.on_status(f"翻译模型预加载失败(不影响使用): {e}")

    # ---------- 翻译 ----------

    def translate(self, ja_text, on_partial=None):
        """返回中文译文;失败返回 None(并通过 on_status 报告)。

        on_partial(zh_so_far): 流式回调, 每收到新 token 调用一次,
        用于边翻译边上屏, 大幅降低体感延迟。
        """
        messages = [{"role": "system", "content": SYSTEM_PROMPT}]
        for prev_ja, prev_zh in self.history:
            messages.append({"role": "user", "content": prev_ja})
            messages.append({"role": "assistant", "content": prev_zh})
        messages.append({"role": "user", "content": ja_text})

        try:
            zh = ""
            with requests.post(
                f"{self.base_url}/api/chat",
                json={
                    "model": self.model,
                    "messages": messages,
                    "stream": True,
                    "keep_alive": "60m",
                    "options": {"temperature": self.temperature,
                                "num_predict": 256,
                                "num_ctx": 4096},
                },
                stream=True,
                timeout=self.timeout,
            ) as r:
                r.raise_for_status()
                for line in r.iter_lines():
                    if not line:
                        continue
                    data = json.loads(line)
                    piece = data.get("message", {}).get("content", "")
                    if piece:
                        zh += piece
                        if on_partial:
                            on_partial(zh)
                    if data.get("done"):
                        break
            zh = zh.strip()
        except Exception as e:
            self.on_status(f"翻译失败: {e}")
            return None

        zh = self._strip_meta(ja_text, zh)
        if zh:
            self.history.append((ja_text, zh))
            # 超过 3 倍上限才裁剪: 平时前缀不变, KV cache 全命中
            if len(self.history) > self.context_pairs * 3:
                self.history = self.history[-self.context_pairs:]
        return zh or None

    @staticmethod
    def _strip_meta(ja, zh):
        """模型偶尔会输出翻译分析而非译文; 检测并提取真正的译文。"""
        if not zh:
            return zh
        suspicious = (any(m in zh for m in _META_MARKERS)
                      and len(zh) > max(24, len(ja) * 2))
        if not suspicious:
            return zh
        # 优先取最后一段引号内的内容(模型通常把最终译文放引号里)
        quoted = re.findall(r'[“"「]([^”"」]{2,})[”"」]', zh)
        if quoted:
            return quoted[-1].strip()
        return zh.splitlines()[0].strip()
