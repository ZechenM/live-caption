"""实时日语→中文字幕 主入口。

用法:
    python main.py           启动字幕
    python main.py --check   只做环境自检(BlackHole / 系统输出 / Ollama)
"""

import argparse
import queue
import shutil
import subprocess
import sys
import threading
from pathlib import Path

import yaml

from capture import AudioCapture
from translate import OllamaTranslator

CONFIG_PATH = Path(__file__).parent / "config.yaml"
RESET_SENTINEL = "\x00RESET"   # 通知翻译线程清空上下文的哨兵


def load_config():
    with open(CONFIG_PATH, encoding="utf-8") as f:
        return yaml.safe_load(f)


# ---------------- 环境自检 ----------------

def current_output_device():
    """用 SwitchAudioSource 查询当前系统输出设备名;不可用返回 None。"""
    exe = shutil.which("SwitchAudioSource")
    if not exe:
        return None
    try:
        out = subprocess.run([exe, "-c", "-t", "output"],
                             capture_output=True, text=True, timeout=5)
        return out.stdout.strip() or None
    except Exception:
        return None


def check_environment(cfg, verbose=True):
    """返回 (ok, warnings)。ok=False 表示有致命问题。"""
    ok = True
    msgs = []

    # 1. BlackHole 输入设备
    import sounddevice as sd
    keyword = cfg["audio"]["device_keyword"].lower()
    has_blackhole = any(keyword in d["name"].lower() and d["max_input_channels"] > 0
                        for d in sd.query_devices())
    if has_blackhole:
        msgs.append("[OK] 检测到 BlackHole 输入设备")
    else:
        ok = False
        msgs.append("[!!] 未检测到 BlackHole。请先执行: brew install blackhole-2ch")

    # 2. 当前系统输出是否走 BlackHole / Multi-Output
    sw = cfg["audio_switch"]
    multi_name = sw["multi_output_name"]
    # 合法输出 = 默认 Multi-Output + device_map 里所有映射目标
    valid_names = {multi_name} | set((sw.get("device_map") or {}).values())
    cur = current_output_device()
    cur_l = (cur or "").lower()
    if cur is None:
        msgs.append("[??] 未安装 switchaudio-osx,无法自动检测系统输出设备。"
                    "建议: brew install switchaudio-osx;"
                    "并手动确认系统输出已切到 Multi-Output Device,否则录不到声音。")
    elif cur in valid_names or "blackhole" in cur_l \
            or "multi-output" in cur_l or "-bh" in cur_l or "多输出" in cur:
        msgs.append(f"[OK] 当前系统输出: {cur}")
    else:
        ok = False
        msgs.append(f"[!!] 当前系统输出是「{cur}」,不包含 BlackHole,将录不到声音!\n"
                    f"     请运行: ./setup_audio.sh on   (或手动切到「{multi_name}」)")

    # 3. Ollama
    t = cfg["translate"]
    translator = OllamaTranslator(t["ollama_url"], t["model"])
    t_ok, t_msg = translator.check()
    msgs.append(("[OK] " if t_ok else "[!!] ") + t_msg)
    ok = ok and t_ok

    if verbose:
        print("\n=== 环境自检 ===")
        for m in msgs:
            print(m)
        print()
    return ok, msgs


# ---------------- 流水线线程 ----------------

def asr_worker(cfg, capture, ja_queue, preview_queue, status, stop):
    import time
    from transcribe import ChunkAssembler, Transcriber, is_hallucination
    a = cfg["asr"]
    transcriber = Transcriber(a["model"], a["language"], a["compute_type"],
                              a["beam_size"], a.get("carry_context", True),
                              a.get("cpu_threads", 0),
                              a.get("backend", "auto"), on_status=status)
    assembler = ChunkAssembler(cfg["audio"]["samplerate"],
                               a["min_chunk_sec"], a["max_chunk_sec"],
                               a["silence_sec"], a["energy_threshold"],
                               use_vad=a.get("vad_segmentation", True))
    status("分段方式: " + ("Silero VAD (实验性)" if assembler.vad_enabled
                          else "自适应能量阈值 (随背景底噪自动上浮)"))
    status("开始监听音频…")
    # 流式草稿: 说话过程中每 draft_interval 秒对未完成的缓冲识别一次
    # (仅 MLX 后端开启, CPU 后端跑不动双份识别)
    draft_iv = a.get("draft_interval_sec", 1.0)
    do_draft = draft_iv > 0 and transcriber.backend == "mlx"
    last_draft = 0.0
    # 长时间没有语音片段 (暂停/拖进度条) 则重置上下文, 避免旧语境误导
    reset_gap = a.get("context_reset_gap_sec", 20)
    last_chunk_time = time.monotonic()
    while not stop.is_set():
        frame = capture.read(timeout=0.5)
        if frame is None:
            continue
        chunk = assembler.feed(frame)
        if chunk is None:
            if do_draft:
                now = time.monotonic()
                buf = (assembler.peek(min_sec=1.0)
                       if now - last_draft >= draft_iv else None)
                if buf is not None:
                    last_draft = now
                    try:
                        draft = transcriber.transcribe(buf, is_final=False)
                    except Exception:
                        draft = ""
                    from transcribe import is_hallucination as _ih
                    if draft and not _ih(draft):
                        preview_queue.put(draft)
            continue
        now = time.monotonic()
        if reset_gap > 0 and now - last_chunk_time > reset_gap:
            transcriber.reset_context()
            ja_queue.put(RESET_SENTINEL)
            print(f"[上下文] 空窗 {now - last_chunk_time:.0f}s, 已重置识别/翻译上下文")
        last_chunk_time = now
        t0 = time.monotonic()
        try:
            text = transcriber.transcribe(chunk)
        except Exception as e:
            status(f"识别出错: {e}")
            continue
        asr_ms = (time.monotonic() - t0) * 1000
        if text and not is_hallucination(text):
            print(f"[计时] 音频{len(chunk)/16000:.1f}s 识别{asr_ms:.0f}ms | {text}")
            preview_queue.put(text)       # 立即预览日语原文, 降低体感延迟
            ja_queue.put(text)


def translate_worker(cfg, ja_queue, sub_queue, status, stop):
    import time
    t = cfg["translate"]
    merge_n = max(1, t.get("merge_backlog", 3))
    translator = OllamaTranslator(t["ollama_url"], t["model"],
                                  t["context_pairs"], t["timeout"],
                                  t["temperature"], on_status=status)
    translator.warmup()
    while not stop.is_set():
        try:
            ja = ja_queue.get(timeout=0.5)
        except queue.Empty:
            continue
        if ja == RESET_SENTINEL:
            translator.history.clear()
            continue
        # 翻译跟不上时, 把积压的句子合并成一次请求, 防止延迟滚雪球
        merged = [ja]
        while len(merged) < merge_n:
            try:
                nxt = ja_queue.get_nowait()
            except queue.Empty:
                break
            if nxt == RESET_SENTINEL:
                translator.history.clear()
                break
            merged.append(nxt)
        ja = " ".join(merged)
        t0 = time.monotonic()
        last_emit = 0.0

        def on_partial(zh_so_far):
            nonlocal last_emit
            now = time.monotonic()
            if now - last_emit >= 0.25:      # 节流, 每 0.25s 上屏一次
                last_emit = now
                sub_queue.put(("partial", ja, zh_so_far))

        zh = translator.translate(ja, on_partial)
        print(f"[计时] 翻译{(time.monotonic() - t0) * 1000:.0f}ms"
              f"{' (合并' + str(len(merged)) + '句)' if len(merged) > 1 else ''}")
        sub_queue.put(("final", ja, zh or ""))  # 翻译失败也显示原文


# ---------------- 主程序 ----------------

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true", help="只做环境自检")
    args = parser.parse_args()

    cfg = load_config()
    ok, _ = check_environment(cfg)
    if args.check:
        sys.exit(0 if ok else 1)
    if not ok:
        print("环境自检未通过(见上)。修复后重试,或确认无误后继续。")
        if input("仍要继续启动吗?[y/N] ").strip().lower() != "y":
            sys.exit(1)

    stop = threading.Event()
    status_queue = queue.Queue()
    ja_queue = queue.Queue()
    sub_queue = queue.Queue()
    preview_queue = queue.Queue()

    def status(msg):
        print(msg)
        status_queue.put(msg)

    au = cfg["audio"]
    capture = AudioCapture(au["device_keyword"], au["samplerate"],
                           au["blocksize"], au["retry_interval"],
                           on_status=status,
                           highpass_hz=au.get("highpass_hz", 0))
    capture.start()
    threading.Thread(target=asr_worker, daemon=True, name="asr",
                     args=(cfg, capture, ja_queue, preview_queue,
                           status, stop)).start()
    threading.Thread(target=translate_worker, daemon=True, name="translate",
                     args=(cfg, ja_queue, sub_queue, status, stop)).start()

    # 悬浮窗必须在主线程; 优先原生 NSPanel (可覆盖全屏App), 失败回退 tkinter
    def make_overlay(on_close):
        if cfg["overlay"].get("native", True):
            try:
                from overlay_native import SubtitleOverlay as Native
                ov = Native(cfg["overlay"], on_close=on_close)
                print("悬浮窗引擎: 原生 NSPanel (支持覆盖全屏App)")
                return ov
            except Exception as e:
                print(f"原生浮窗不可用({e}), 回退 tkinter。"
                      f"如需全屏覆盖请: pip install pyobjc-framework-Cocoa")
        from overlay import SubtitleOverlay as Tk
        return Tk(cfg["overlay"], on_close=on_close)

    def poll(ov):
        updated = False
        while True:
            try:
                kind, ja, zh = sub_queue.get_nowait()
            except queue.Empty:
                break
            if kind == "partial":
                ov.show_partial(zh, ja)
            else:
                ov.append_line(zh or f"(未译) {ja}", ja)
            updated = True
        if updated:
            return
        # 识别完成但还没翻译好的日语, 先行显示
        preview = None
        while True:
            try:
                preview = preview_queue.get_nowait()
            except queue.Empty:
                break
        if preview:
            ov.show_ja_preview(preview)
            return
        # 没有新字幕时,显示最新状态信息(仅当还没有任何字幕)
        msg = None
        while True:
            try:
                msg = status_queue.get_nowait()
            except queue.Empty:
                break
        if msg and not ov.has_lines:
            ov.show_status(msg)

    def on_close():
        stop.set()
        capture.stop()

    overlay = make_overlay(on_close)
    print("字幕窗口已启动:左键拖动,右键退出。Ctrl+C 也可退出。")
    try:
        overlay.run(poll, interval_ms=100)
    except KeyboardInterrupt:
        pass
    finally:
        on_close()


if __name__ == "__main__":
    main()
