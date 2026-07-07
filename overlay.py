"""字幕悬浮窗:置顶、半透明、无边框、可拖动、可拉边缘调大小、可滚动历史。

- 窗口中间按住左键拖动 = 移动
- 靠近边缘/角落 (12px) 按住左键拖动 = 调整大小
- 鼠标滚轮 / 触控板 = 上下滚动查看历史字幕 (有新字幕时, 若已滚到底部会自动跟随)
- 右键 = 退出

必须在主线程运行 (macOS 限制)。其他线程通过 main.py 的队列喂字幕,
main.py 用 root.after 轮询并调用 append_line / show_status。
"""

import tkinter as tk

RESIZE_MARGIN = 12       # 边缘判定宽度(px)
MIN_W, MIN_H = 320, 80   # 最小窗口尺寸

# 每个方向按优先级列出候选光标, 运行时挑第一个当前平台支持的。
# macOS 上 resizeupdown/resizeleftright 是系统原生的双向箭头;
# 对角双向箭头是 macOS 私有光标, tk 拿不到, 用最接近的候选兜底。
_CURSOR_CANDIDATES = {
    "t": ["resizeupdown", "sb_v_double_arrow", "top_side"],
    "b": ["resizeupdown", "sb_v_double_arrow", "bottom_side"],
    "l": ["resizeleftright", "sb_h_double_arrow", "left_side"],
    "r": ["resizeleftright", "sb_h_double_arrow", "right_side"],
    "tl": ["size_nw_se", "top_left_corner", "sizing", "fleur"],
    "br": ["size_nw_se", "bottom_right_corner", "sizing", "fleur"],
    "tr": ["size_ne_sw", "top_right_corner", "sizing", "fleur"],
    "bl": ["size_ne_sw", "bottom_left_corner", "sizing", "fleur"],
    "": ["arrow"],
}


class SubtitleOverlay:
    def __init__(self, cfg, on_close=None):
        self.on_close = on_close or (lambda: None)
        width = cfg.get("width", 900)
        height = cfg.get("height", 160)
        font_family = cfg.get("font_family", "PingFang SC")
        zh_size = cfg.get("font_size", 28)
        ja_size = cfg.get("ja_font_size", 16)
        self.show_ja = cfg.get("show_japanese", True)
        self.max_history = cfg.get("max_history", 100)

        self._count = 0          # 已完成的字幕行数
        self._status_shown = False
        self._partial_active = False   # 是否有正在流式更新的行

        self.root = tk.Tk()
        self.root.overrideredirect(True)          # 无边框
        self.root.attributes("-topmost", True)     # 置顶
        self.root.attributes("-alpha", cfg.get("opacity", 0.82))
        self.root.configure(bg="black")

        # 默认放在屏幕底部居中
        sw = self.root.winfo_screenwidth()
        sh = self.root.winfo_screenheight()
        x = cfg.get("x", (sw - width) // 2)
        y = cfg.get("y", sh - height - 80)
        self.root.geometry(f"{width}x{height}+{x}+{y}")

        if self.show_ja:
            self.ja_label = tk.Label(
                self.root, text="", fg="#9aa0a6", bg="black",
                font=(font_family, ja_size), wraplength=width - 40,
                justify="center")
            self.ja_label.pack(pady=(8, 0))
        else:
            self.ja_label = None

        # 可滚动的中文字幕区
        self.text = tk.Text(
            self.root, bg="black", fg="white",
            font=(font_family, zh_size), wrap="word",
            bd=0, highlightthickness=0, insertwidth=0,
            spacing1=4, spacing3=4, cursor="arrow")
        self.text.tag_configure("zh", justify="center")
        self.text.tag_configure("partial", justify="center",
                                foreground="#b8bcc2")
        self.text.insert("end", "等待音频…", "zh")
        self._status_shown = True
        self.text.configure(state="disabled")
        self.text.pack(expand=True, fill="both", padx=20, pady=(4, 10))

        # 事件绑定 (用 x_root/y_root 统一换算成窗口内坐标)
        for w in filter(None, [self.root, self.text, self.ja_label]):
            w.bind("<ButtonPress-1>", self._press)
            w.bind("<B1-Motion>", self._motion)
            w.bind("<Motion>", self._hover)
            w.bind("<ButtonPress-2>", self._quit)   # 中键
            w.bind("<ButtonPress-3>", self._quit)   # 右键
        self.root.bind("<Configure>", self._on_configure)

        self._mode = ""              # "" = 移动, 否则如 "br" = 拉右下角
        self._press_xy = (0, 0)
        self._press_geo = (x, y, width, height)
        self._cursors = self._probe_cursors()
        self.root.update_idletasks()
        self._enable_fullscreen_overlay()

    def _enable_fullscreen_overlay(self):
        """让浮窗能覆盖全屏 App (如全屏 Chrome)。

        macOS 全屏应用独占 Space, 普通置顶窗口进不去。给底层 NSWindow
        设置 CanJoinAllSpaces + FullScreenAuxiliary 后即可跟随所有 Space。
        需要 pyobjc (pip install pyobjc-framework-Cocoa), 没装则跳过。
        """
        try:
            from AppKit import NSApplication
            behavior = (1 << 0) | (1 << 4) | (1 << 8)
            # CanJoinAllSpaces | Stationary | FullScreenAuxiliary
            app = NSApplication.sharedApplication()
            for w in app.windows():
                w.setCollectionBehavior_(behavior)
                w.setLevel_(101)   # NSPopUpMenuWindowLevel, 高于全屏内容
            print("[overlay] 已启用全屏覆盖 (可显示在全屏App之上)")
        except ImportError:
            print("[overlay] 提示: 想让字幕显示在全屏App上, 请安装:"
                  " pip install pyobjc-framework-Cocoa")
        except Exception as e:
            print(f"[overlay] 全屏覆盖设置失败(不影响其他功能): {e}")

    def _probe_cursors(self):
        """逐方向探测当前平台支持的光标名, 挑候选表里第一个可用的。"""
        chosen = {}
        for edges, names in _CURSOR_CANDIDATES.items():
            chosen[edges] = "arrow"
            for name in names:
                try:
                    self.root.config(cursor=name)
                    chosen[edges] = name
                    break
                except tk.TclError:
                    continue
        self.root.config(cursor="arrow")
        return chosen

    # ---------- 命中检测 ----------

    def _hit_test(self, x_root, y_root):
        x = x_root - self.root.winfo_rootx()
        y = y_root - self.root.winfo_rooty()
        w = self.root.winfo_width()
        h = self.root.winfo_height()
        edges = ""
        if y <= RESIZE_MARGIN:
            edges += "t"
        elif y >= h - RESIZE_MARGIN:
            edges += "b"
        if x <= RESIZE_MARGIN:
            edges += "l"
        elif x >= w - RESIZE_MARGIN:
            edges += "r"
        return edges

    def _hover(self, event):
        try:
            self.root.config(cursor=self._cursors.get(
                self._hit_test(event.x_root, event.y_root), "arrow"))
        except tk.TclError:
            pass

    # ---------- 拖动 / 调整大小 ----------

    def _press(self, event):
        self._mode = self._hit_test(event.x_root, event.y_root)
        self._press_xy = (event.x_root, event.y_root)
        self._press_geo = (self.root.winfo_x(), self.root.winfo_y(),
                           self.root.winfo_width(), self.root.winfo_height())
        return "break"   # 阻止 Text 的文字选择

    def _motion(self, event):
        dx = event.x_root - self._press_xy[0]
        dy = event.y_root - self._press_xy[1]
        x, y, w, h = self._press_geo
        m = self._mode
        if not m:                                   # 移动
            self.root.geometry(f"+{x + dx}+{y + dy}")
            return "break"
        if "r" in m:
            w = max(MIN_W, w + dx)
        if "b" in m:
            h = max(MIN_H, h + dy)
        if "l" in m:
            new_w = max(MIN_W, w - dx)
            x += w - new_w
            w = new_w
        if "t" in m:
            new_h = max(MIN_H, h - dy)
            y += h - new_h
            h = new_h
        self.root.geometry(f"{w}x{h}+{x}+{y}")
        return "break"

    def _on_configure(self, _event):
        if self.ja_label is not None:
            self.ja_label.config(
                wraplength=max(100, self.root.winfo_width() - 40))

    def _quit(self, _event=None):
        self.on_close()
        self.root.destroy()

    # ---------- 更新内容 (仅主线程调用) ----------

    def _at_bottom(self):
        return self.text.yview()[1] > 0.999

    def _open_line(self):
        """定位到"当前行": 若无进行中的行则新起一行, 否则清空重写。

        用 mark 而不是行号定位: mark 会随内容增删自动调整;
        删除只到 end-1c, 避免 Tk 吞掉上一行末尾的换行符。
        """
        if self._status_shown:
            self.text.delete("1.0", "end")
            self._status_shown = False
        if not self._partial_active:
            if self._count:
                self.text.insert("end", "\n")
            self.text.mark_set("pstart", "end-1c")
            self.text.mark_gravity("pstart", "left")
            self._partial_active = True
        else:
            self.text.delete("pstart", "end-1c")

    def show_partial(self, zh, ja=""):
        """流式更新当前行(灰色), 翻译完成前逐步显示。"""
        follow = self._at_bottom()
        self.text.configure(state="normal")
        self._open_line()
        self.text.insert("end", zh, ("zh", "partial"))
        self.text.configure(state="disabled")
        if follow:
            self.text.see("end")
        if ja and self.ja_label is not None:
            self.ja_label.config(text=ja)

    def append_line(self, zh, ja=""):
        """完成一条中文字幕(白色定稿); 若视图在底部则自动跟随滚动。"""
        follow = self._at_bottom()
        self.text.configure(state="normal")
        self._open_line()
        self.text.insert("end", zh, "zh")
        self._partial_active = False
        self._count += 1
        over = self._count - self.max_history
        if over > 0:                          # 裁剪最老的历史
            self.text.delete("1.0", f"{1 + over}.0")
            self._count -= over
        self.text.configure(state="disabled")
        if follow:
            self.text.see("end")
        if self.ja_label is not None:
            self.ja_label.config(text=ja)

    def show_ja_preview(self, ja):
        """识别完成、翻译尚未返回时, 先在日语行显示原文(带标记)。"""
        if self.ja_label is not None:
            self.ja_label.config(text=f"⋯ {ja}")

    def show_status(self, msg):
        """还没有任何字幕时, 在字幕区显示状态/错误提示。"""
        if self._count:
            return
        self.text.configure(state="normal")
        self.text.delete("1.0", "end")
        self.text.insert("end", msg, "zh")
        self.text.configure(state="disabled")
        self._status_shown = True

    @property
    def has_lines(self):
        return self._count > 0

    # ---------- 主循环 ----------

    def run(self, poll_fn, interval_ms=100):
        def tick():
            try:
                poll_fn(self)
            except Exception as e:
                print(f"[overlay] poll error: {e}")
            self.root.after(interval_ms, tick)

        self.root.after(interval_ms, tick)
        self.root.mainloop()
