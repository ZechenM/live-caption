# Live Caption:macOS 实时日语 → 中文字幕

链路:BlackHole 捕获系统音频 → whisper large-v3-turbo(MLX GPU 加速)日语识别 → Ollama(qwen2.5:14b)流式翻译 → 屏幕悬浮字幕。全程本地运行,说完一句约 1 秒出字幕。

分段采用自适应底噪检测(对背景音乐免疫),翻译带上下文且流式上屏(灰色=翻译中,白色=定稿)。字幕窗口支持拖动、边缘调大小、滚轮翻看历史。

## 一、首次配置

### 1. 安装依赖(Homebrew)

```bash
brew install blackhole-2ch switchaudio-osx
```

装完 BlackHole 建议重启一次 CoreAudio(或直接重启电脑):`sudo killall coreaudiod`

### 2. 安装并准备 Ollama

```bash
brew install ollama          # 或从 ollama.com 下载 App
ollama pull qwen2.5:14b      # 约 9GB; 内存紧张可用 qwen2.5:7b (改 config.yaml)
```

运行时保持 Ollama 在后台(`ollama serve` 或打开 Ollama App)。

### 3. Python 环境

```bash
cd live-caption
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

首次启动时会自动下载 whisper large-v3-turbo 的 MLX 权重(约 1.6GB),需要几分钟。识别引擎优先用 MLX(Apple Silicon GPU,快 5-10 倍),未安装 mlx-whisper 时自动回退到 faster-whisper(CPU);启动日志的 `>>> 识别引擎:` 一行会显示实际用的是哪个。

### 4. 创建 Multi-Output Device(关键步骤)

BlackHole 是一个虚拟声卡:把系统输出指到它,程序就能"录"到系统声音——但你自己就听不到了。解决办法是创建 **Multi-Output Device(多输出设备)**,同时输出到你的耳机/扬声器 **和** BlackHole。

打开 **音频MIDI设置**(Audio MIDI Setup,聚焦搜索即可找到),点左下角 **+** → **创建多输出设备**,然后按你的收听方式勾选:

**方案 A:扬声器外放**

1. 勾选 ✅ MacBook 扬声器(或你的外接音箱)
2. 勾选 ✅ BlackHole 2ch
3. 右侧"主设备"选扬声器,并给 BlackHole 勾上"漂移校正"(Drift Correction)

**方案 B:耳机(AirPods 或有线)**

1. 先连上耳机(AirPods 需要已连接才会出现在列表里)
2. 勾选 ✅ AirPods / 外置耳机
3. 勾选 ✅ BlackHole 2ch
4. 主设备选耳机,BlackHole 勾"漂移校正"

> AirPods 提示:蓝牙设备采样率可能与 BlackHole 不一致,主设备务必选 AirPods。如果换着用耳机和扬声器,可以建两个多输出设备(如 "Multi-Output 扬声器" / "Multi-Output 耳机"),用 `./setup_audio.sh on "设备名"` 切换。

建好后可把设备重命名为 `Multi-Output Device`(与 `config.yaml` 中 `audio_switch.multi_output_name` 一致),或修改 config 里的名字。

### 5. 音量键失效的限制

系统输出切到 Multi-Output Device 后,**键盘音量键会变灰失效**——这是 macOS 对聚合设备的限制,不是 bug。调音量的替代办法:

- **音频MIDI设置**里选中你的扬声器/耳机,拖动右侧音量滑块(推荐,提前调好);
- 或在播放软件里调(浏览器/播放器自带音量);
- AirPods 可以用 iPhone 或"降噪/通透"长按等方式间接控制,也可以直接捏柄调音量(AirPods Pro 2);
- 看完视频运行 `./setup_audio.sh off` 恢复原输出,音量键立即恢复正常。

## 二、日常使用

```bash
cd live-caption
source venv/bin/activate

# 方式 1(推荐):一键切音频 + 启动,退出时自动恢复原输出设备
./setup_audio.sh run

# 方式 2:手动分步
./setup_audio.sh on      # 切到 Multi-Output Device
python main.py           # 启动字幕
./setup_audio.sh off     # 用完恢复
```

其他命令:`./setup_audio.sh list` 列出输出设备名;`python main.py --check` 只做环境自检(BlackHole、系统输出、Ollama)。

字幕窗口:**左键拖动**移动;**边缘/角落拖动**调大小;**滚轮/触控板**翻看历史字幕(滚到底自动跟随新字幕);**右键退出**。灰色文字是流式翻译中的内容,白色是定稿。字号、透明度、是否显示日语原文、历史条数、分段参数都在 `config.yaml` 里调。

项目用 git 管理,`stable-v1` 标签是一个已验证的稳定版本,出问题可 `git checkout stable-v1 -- .` 回滚。

## 三、故障排除

**字幕一直显示"等待音频"/ 识别不到内容**:系统输出没走 BlackHole。运行 `python main.py --check` 查看;`main.py` 启动时也会检测并提示,不会静默录空。

**AirPods 中途断连**:音频流会自动断开重连(每 2 秒重试),程序不会崩溃。注意:AirPods 断开后 macOS 可能把输出切回扬声器,重新连上耳机后请再跑一次 `./setup_audio.sh on`(旧的 Multi-Output 设备若含已消失的 AirPods 会静音)。

**Ollama 连不上**:提示 `无法连接 Ollama` 时,先 `ollama serve` 或打开 Ollama App;提示缺模型则 `ollama pull qwen2.5:14b`(或改 config 用已有模型)。

**延迟太大 / 机器卡**:确认识别引擎是 MLX(见启动日志);翻译慢就换 `qwen2.5:7b`。终端每句都打 `[计时]`,识别和翻译谁慢一目了然。

**断句太碎 / 半句半句出**:调大 `asr.min_chunk_sec`(如 4.0)和 `silence_sec`(如 0.7)。反之句子总撞 5s 上限则调小 `silence_sec`。

**麦克风权限**:首次运行时 macOS 会向你的终端(Terminal/iTerm)申请"麦克风"权限(BlackHole 算输入设备),必须允许,否则读到的全是静音。

**翻译输出了解释/分析而不是译文**:程序会自动检测并提取真正的译文;若仍偶发,说明该句触发了模型的"讨论欲",通常下一句就恢复。
