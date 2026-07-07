"""原生 macOS 悬浮字幕窗 (NSPanel + pyobjc), 可覆盖全屏 App。

与 overlay.py (tkinter 版) 接口相同, main.py 优先用本模块, 失败回退。

交互:
- 窗口内按住左键拖动 = 移动 (原生拖拽)
- 窗口边缘/角落拖动 = 调大小 (原生, 含对角双向箭头光标)
- 滚轮/触控板 = 翻看历史字幕
- 右键 = 退出

必须在主线程运行。
"""

import signal

import objc
from AppKit import (NSApp, NSApplication, NSBackingStoreBuffered,
                    NSClickGestureRecognizer, NSColor, NSFont,
                    NSFontAttributeName, NSForegroundColorAttributeName,
                    NSPanel, NSParagraphStyleAttributeName, NSScreen,
                    NSScrollView, NSTextField, NSTextView)
from Foundation import (NSMakeRange, NSMakeRect, NSMakeSize,
                        NSMutableAttributedString, NSMutableParagraphStyle,
                        NSObject, NSTimer)

# NSWindow 常量
STYLE_BORDERLESS = 0
STYLE_RESIZABLE = 1 << 3
STYLE_NONACTIVATING = 1 << 7
BEHAVIOR_ALL_SPACES = (1 << 0) | (1 << 4) | (1 << 8)
# CanJoinAllSpaces | Stationary | FullScreenAuxiliary
LEVEL_OVERLAY = 101          # NSPopUpMenuWindowLevel, 高于全屏内容
ALIGN_CENTER = 2             # NSTextAlignmentCenter (macOS)
MIN_W, MIN_H = 320, 80


class _Ticker(NSObject):
    """NSTimer 的回调载体。"""

    def initWithCallback_(self, cb):
        self = objc.super(_Ticker, self).init()
        if self is None:
            return None
        self._cb = cb
        return self

    def tick_(self, _timer):
        try:
            self._cb()
        except Exception as e:
            print(f"[overlay] poll error: {e}")


class _DragTextView(NSTextView):
    """左键拖动窗口、右键退出的只读文本视图。"""

    def mouseDown_(self, event):
        w = self.window()
        if w is not None:
            w.performWindowDragWithEvent_(event)

    def rightMouseDown_(self, _event):
        owner = getattr(self, "_owner", None)
        if owner is not None:
            owner._quit()


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

        self._finals = []            # 定稿行
        self._partial = None         # 流式更新中的行
        self._status = "等待音频…"

        self.app = NSApplication.sharedApplication()
        self.app.setActivationPolicy_(1)   # accessory: 无 Dock 图标

        # 屏幕底部居中 (macOS 坐标原点在左下)
        sf = NSScreen.mainScreen().frame()
        x = cfg.get("x", (sf.size.width - width) / 2)
        y = cfg.get("y", 80)

        style = STYLE_BORDERLESS | STYLE_RESIZABLE | STYLE_NONACTIVATING
        self.panel = NSPanel.alloc().initWithContentRect_styleMask_backing_defer_(
            NSMakeRect(x, y, width, height), style, NSBackingStoreBuffered, False)
        self.panel.setLevel_(LEVEL_OVERLAY)
        self.panel.setCollectionBehavior_(BEHAVIOR_ALL_SPACES)
        self.panel.setBackgroundColor_(NSColor.blackColor())
        self.panel.setAlphaValue_(cfg.get("opacity", 0.82))
        self.panel.setOpaque_(False)
        self.panel.setHidesOnDeactivate_(False)
        self.panel.setFloatingPanel_(True)
        self.panel.setBecomesKeyOnlyIfNeeded_(True)
        self.panel.setMovableByWindowBackground_(True)
        self.panel.setMinSize_(NSMakeSize(MIN_W, MIN_H))

        content = self.panel.contentView()
        cb = content.bounds()

        # 日语预览行 (顶部)
        ja_h = ja_size + 12 if self.show_ja else 0
        if self.show_ja:
            self.ja_label = NSTextField.labelWithString_("")
            self.ja_label.setFrame_(NSMakeRect(
                12, cb.size.height - ja_h - 2, cb.size.width - 24, ja_h))
            self.ja_label.setAutoresizingMask_(2 | 8)   # 宽可变 | 贴顶
            self.ja_label.setAlignment_(ALIGN_CENTER)
            self.ja_label.setTextColor_(NSColor.colorWithWhite_alpha_(0.62, 1.0))
            self.ja_label.setFont_(self._font(font_family, ja_size))
            content.addSubview_(self.ja_label)
        else:
            self.ja_label = None

        # 可滚动中文字幕区
        self.scroll = NSScrollView.alloc().initWithFrame_(NSMakeRect(
            10, 6, cb.size.width - 20, cb.size.height - ja_h - 10))
        self.scroll.setAutoresizingMask_(2 | 16)        # 宽高都可变
        self.scroll.setHasVerticalScroller_(True)
        self.scroll.setScrollerStyle_(1)                # overlay 式滚动条
        self.scroll.setDrawsBackground_(False)

        inner = self.scroll.contentView().bounds()
        self.tv = _DragTextView.alloc().initWithFrame_(inner)
        self.tv._owner = self
        self.tv.setEditable_(False)
        self.tv.setSelectable_(False)
        self.tv.setDrawsBackground_(False)
        self.tv.setVerticallyResizable_(True)
        self.tv.setHorizontallyResizable_(False)
        self.tv.setAutoresizingMask_(2)
        self.tv.textContainer().setWidthTracksTextView_(True)
        self.tv.setMinSize_(NSMakeSize(0.0, 0.0))
        self.tv.setMaxSize_(NSMakeSize(1e7, 1e7))
        self.scroll.setDocumentView_(self.tv)
        content.addSubview_(self.scroll)

        # 右键(文本区之外的部分)也能退出
        gr = NSClickGestureRecognizer.alloc().initWithTarget_action_(
            _Ticker.alloc().initWithCallback_(self._quit), "tick:")
        gr.setButtonMask_(0x2)
        content.addGestureRecognizer_(gr)

        # 文本样式
        self._para = NSMutableParagraphStyle.alloc().init()
        self._para.setAlignment_(ALIGN_CENTER)
        self._para.setParagraphSpacing_(6.0)
        zh_font = self._font(font_family, zh_size)
        self._attr_final = {NSFontAttributeName: zh_font,
                            NSForegroundColorAttributeName: NSColor.whiteColor(),
                            NSParagraphStyleAttributeName: self._para}
        self._attr_partial = {NSFontAttributeName: zh_font,
                              NSForegroundColorAttributeName:
                                  NSColor.colorWithWhite_alpha_(0.74, 1.0),
                              NSParagraphStyleAttributeName: self._para}

        self._render()
        self.panel.orderFrontRegardless()

    @staticmethod
    def _font(family, size):
        f = NSFont.fontWithName_size_(family, size)
        return f if f is not None else NSFont.systemFontOfSize_(size)

    # ---------- 渲染 ----------

    def _at_bottom(self):
        vis = self.scroll.contentView().documentVisibleRect()
        doc_h = self.tv.frame().size.height
        return vis.origin.y + vis.size.height >= doc_h - 30

    def _render(self):
        follow = self._at_bottom()
        out = NSMutableAttributedString.alloc().init()

        def append(txt, attrs):
            out.appendAttributedString_(
                NSMutableAttributedString.alloc()
                .initWithString_attributes_(txt, attrs))

        if not self._finals and self._partial is None:
            append(self._status, self._attr_partial)
        else:
            finals = self._finals[-self.max_history:]
            if finals:
                append("\n".join(finals), self._attr_final)
            if self._partial is not None:
                if finals:
                    append("\n", self._attr_final)
                append(self._partial, self._attr_partial)

        self.tv.textStorage().setAttributedString_(out)
        if follow:
            self.tv.scrollRangeToVisible_(NSMakeRange(out.length(), 0))

    # ---------- 对外接口 (与 tkinter 版一致, 仅主线程调用) ----------

    def show_partial(self, zh, ja=""):
        self._partial = zh
        self._render()
        if ja:
            self.show_ja_preview(ja, mark=False)

    def append_line(self, zh, ja=""):
        self._partial = None
        self._finals.append(zh)
        if len(self._finals) > self.max_history:
            self._finals = self._finals[-self.max_history:]
        self._render()
        if self.ja_label is not None:
            self.ja_label.setStringValue_(ja)

    def show_ja_preview(self, ja, mark=True):
        if self.ja_label is not None:
            self.ja_label.setStringValue_(("⋯ " + ja) if mark else ja)

    def show_status(self, msg):
        if self.has_lines:
            return
        self._status = msg
        self._render()

    @property
    def has_lines(self):
        return bool(self._finals)

    # ---------- 生命周期 ----------

    def _quit(self):
        try:
            self.on_close()
        finally:
            NSApp().terminate_(None)

    def run(self, poll_fn, interval_ms=100):
        self._ticker = _Ticker.alloc().initWithCallback_(
            lambda: poll_fn(self))
        NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
            interval_ms / 1000.0, self._ticker, "tick:", None, True)
        signal.signal(signal.SIGINT, lambda *_: self._quit())
        self.app.run()
