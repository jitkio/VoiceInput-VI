# Voice Input · 本地离线语音输入

> **按住 CapsLock 说话，松开就上屏。** 语音识别 + 文字整理全部在本机完成，声音不出本地、不联网、不要 API Key。

<p align="left">
  <img alt="platform" src="https://img.shields.io/badge/platform-Windows%2010%2F11-0078D6">
  <img alt="python" src="https://img.shields.io/badge/python-3.11%2B-3776AB">
  <img alt="inference" src="https://img.shields.io/badge/inference-llama.cpp-000000">
  <img alt="license" src="https://img.shields.io/badge/license-MIT-green">
</p>

```
麦克风 ──► 掐掉首尾静音 ──► Qwen3-ASR-1.7B (llama.cpp :8081)
                                  │  语音 → 带标点原文
                                  ▼
                            Qwen3.5-4B 润色 (llama.cpp :8082)
                                  │  去口水词 / 修同音错字 / 断句
                                  ▼
                            剪贴板 + Ctrl+V 上屏
```

**English TL;DR** — A Windows-first, fully offline hold-to-talk dictation tool. Hold `CapsLock`, speak, release: audio is trimmed, transcribed by **Qwen3-ASR-1.7B** and cleaned up by **Qwen3.5-4B**, both served locally by **llama.cpp**, then pasted at the cursor. PyTorch-free, single Python process, ~5.5 GB of GGUF models, ~5.4 GB VRAM. See [Quick start](#快速开始部署).

---

## 特性

| | |
|---|---|
| **完全离线** | 模型跑在本机 llama.cpp，无云端请求、无 API Key、无遥测 |
| **按住说话** | `CapsLock` 按下开始录、松开自动识别并粘贴；CapsLock 原功能被抑制，不会误切大小写 |
| **两级模型** | 1.7B ASR 出原文 → 4B 小模型加标点、去「嗯/那个/就是说」、修同音字 |
| **液态玻璃悬浮条** | 屏幕底部胶囊：真高斯模糊背景、实时波形、计时/转圈/对勾状态，亮暗背景自动配色 |
| **系统托盘** | 暂停监听 / 开机自启 / 打开配置 / 查看日志 / 退出 |
| **进程自管理** | 主程序负责拉起和关闭两个 `llama-server`，关窗口/Ctrl+C/强杀都不会留后台；启动时自动清理上次残留 |
| **健壮性** | 单实例锁、看门狗巡检（睡眠唤醒后自动重启服务）、润色失败回退原文、防跑飞长度校验 |
| **零第三方推理框架** | 不装 PyTorch / CUDA toolkit，只用 llama.cpp 预编译二进制 |

## 使用的模型

| 环节 | 模型 | 量化 | 体积 | 运行方式 |
|---|---|---|---|---|
| 语音识别 | **Qwen3-ASR-1.7B**（`ggml-org/Qwen3-ASR-1.7B-GGUF`） | Q8_0 | 2.2 GB + mmproj 0.34 GB | `llama-server :8081` |
| 文字润色 | **Qwen3.5-4B**（`unsloth/Qwen3.5-4B-GGUF`） | Q4_K_M | 2.9 GB | `llama-server :8082` |

- **Qwen3-ASR-1.7B**：通义千问的端到端语音识别模型，中英混说、自动语种识别，输出自带标点。llama.cpp 通过 `mtmd`（多模态）通道喂音频，所以需要同时加载 `mmproj`（音频编码器权重），少一个都起不来。
- **Qwen3.5-4B**：负责二次整理。它只做「改写这一段已经识别出来的字」，不做翻译和扩写；请求里关掉了思考链（`chat_template_kwargs.enable_thinking = false`），延迟能压到 1 秒内。
- 模型与框架都只是**本地推理**用途，遵守各自仓库的开源协议（Qwen 系列为 Apache-2.0）。本仓库不附带模型权重。

> 换模型：见 [替换 / 升级模型](#替换--升级模型)。只要有对应 GGUF（ASR 需要 `mmproj`），把 `config.toml` 里的路径改掉即可，代码不用动。

## 环境要求

| 项 | 要求 |
|---|---|
| 系统 | Windows 10 / 11 x64（悬浮条、热键抑制、托盘、开机自启都是 Win32 实现；其他系统未适配） |
| Python | 3.11+（用到标准库 `tomllib`），实测 3.12 |
| 显卡 | NVIDIA GPU，显存 ≥ 6 GB 体验最佳（ASR ≈2.4 GB + 润色 ≈3 GB）；`-ngl 0` 可纯 CPU 跑，但慢很多 |
| 磁盘 | llama.cpp ≈1.2 GB + 模型 ≈5.5 GB |
| 其他 | 一个能用的麦克风；llama.cpp 预编译包（CUDA 12 或 Vulkan 版） |

## 快速开始（部署）

### 1. 拿代码

```bash
git clone https://github.com/jitkio/VoiceInput-VI.git
cd VoiceInput-VI
```

### 2. 装 Python 依赖

```bash
python -m pip install -r requirements.txt
```

依赖只有 7 个包：`numpy`、`sounddevice`、`pyperclip`、`pynput`、`Pillow`、`skia-python`、`pystray`。
`skia-python` 装不上也不影响主流程，悬浮条会自动降级到 Pillow 渲染。

### 3. 下载 llama.cpp（推理服务）

到 [llama.cpp Releases](https://github.com/ggml-org/llama.cpp/releases) 下载 Windows 包：

- `llama-bXXXXX-bin-win-cuda-12.4-x64.zip` + 同版本的 `cudart-llama-bin-win-cuda-12.4-x64.zip`（NVIDIA 卡，**推荐**，本项目实测版本 b11304）
- 或 `llama-bXXXXX-bin-win-vulkan-x64.zip`（A 卡 / 集显 / 包更小）
- 或 `llama-bXXXXX-bin-win-cpu-x64.zip`（无 GPU）

解压到项目下的 `llama.cpp\`，确保存在 `llama.cpp\llama-server.exe`。
不想放这里就改 `config.toml` 的 `[llama] dir`，或者设环境变量 `LLAMA_DIR`（`.bat` 手动模式会读它）。

> 需要 b6xxx 之后的版本（llama.cpp 支持 Qwen3-ASR 音频编码器之后）。

### 4. 下载模型

```bash
python download_models.py            # 全部（约 5.5 GB，支持断点续传）
python download_models.py --only asr      # 只下识别模型
python download_models.py --only polish   # 只下润色模型
```

国内网络慢就用镜像：

```bash
set HF_ENDPOINT=https://hf-mirror.com
python download_models.py
```

也可以手动下载，放进 `models\`：

| 文件 | 来源 |
|---|---|
| `Qwen3-ASR-1.7B-Q8_0.gguf` | <https://huggingface.co/ggml-org/Qwen3-ASR-1.7B-GGUF> |
| `mmproj-Qwen3-ASR-1.7B-Q8_0.gguf` | 同上 |
| `Qwen3.5-4B-Q4_K_M.gguf` | <https://huggingface.co/unsloth/Qwen3.5-4B-GGUF> |

### 5. 启动

```bash
start_all.bat
```

等它打印：

```
启动 Qwen3-ASR-1.7B-Q8_0.gguf -> :8081 ...
  :8081 就绪（8s）
启动 Qwen3.5-4B-Q4_K_M.gguf -> :8082 ...
  :8082 就绪（5s）
就绪。按住 [caps_lock] 说话，松开上屏。
```

首次冷启动 10–20 秒（要 mmap 进 5 GB 权重），之后从系统缓存起会快很多。

> **一键脚本**：`setup.bat` 会把「装依赖 + 下模型 + 检查 llama.cpp」串起来跑一遍。

## 使用

| 操作 | 说明 |
|---|---|
| 按住 `CapsLock` 说话 | 底部浮出胶囊，波形随音量跳动 |
| 松开 | 蓝色「识别中」→ 自动粘贴到光标处 → 绿色「已上屏」淡出 |
| 托盘图标右键 | 暂停监听 / 开机自启 / 打开配置 / 查看日志 / 退出 |
| 关闭启动窗口 / `Ctrl+C` | 主程序退出，两个 `llama-server` 一并关闭 |
| `stop_all.bat` | 应急：强杀主程序与所有 `llama-server` |

- 只想要剪贴板、不想自动粘贴：`[output] paste = false`
- 粘贴后想保留原剪贴板内容：默认 `restore_clipboard = true`（粘贴 0.6 秒后还原）
- 想换热键：`[hotkey] key`，可选 `caps_lock` / `scroll_lock` / `pause` / `f9`
- 打断时的 `CapsLock` 原始行为被 `win32_event_filter` 吞掉，不会切大小写

## 配置说明

全部配置在 `config.toml`（TOML 格式，改完重启生效）。也可以用环境变量 `VOICE_INPUT_CONFIG` 指向别的配置文件。

### `[audio]`

| 键 | 默认 | 说明 |
|---|---|---|
| `samplerate` | `16000` | 目标采样率；设备不支持会自动用它自己的采样率录，再重采样 |
| `device` | `""` | 麦克风设备名或索引，留空 = 系统默认。`python -c "import sounddevice;print(sounddevice.query_devices())"` 查索引 |
| `min_rms` | `0.012` | 静音门限。环境吵就调大（0.02 左右），掉字就调小 |
| `pad_ms` | `200` | 掐掉首尾静音后，两端各保留多少毫秒 |
| `max_seconds` | `150` | 单次录音上限。模型硬上限约 4.2 分钟，留了余量 |

### `[llama]` — 推理服务

| 键 | 默认 | 说明 |
|---|---|---|
| `manage` | `true` | 由主程序托管子进程。`false` 则自己用 `start_asr.bat` / `start_polish.bat` 起服务 |
| `dir` | `"llama.cpp"` | llama.cpp 解压目录，相对项目根目录或绝对路径 |
| `asr_model` | `models/Qwen3-ASR-1.7B-Q8_0.gguf` | 识别模型 |
| `asr_mmproj` | `models/mmproj-Qwen3-ASR-1.7B-Q8_0.gguf` | 识别模型的音频编码器 |
| `polish_model` | `models/Qwen3.5-4B-Q4_K_M.gguf` | 润色模型 |
| `ngl` | `99` | 放到 GPU 的层数。显存紧张调小（如 26），纯 CPU 设 0 |
| `threads` | `8` | CPU 线程数 |

> 所有 `.gguf` 路径都支持相对路径（相对项目根目录）和绝对路径。

### `[asr]` / `[polish]`

| 键 | 默认 | 说明 |
|---|---|---|
| `asr.url` | `http://127.0.0.1:8081/v1/chat/completions` | OpenAI 兼容接口；换成局域网里别的机器也能用 |
| `asr.timeout` | `90` | 秒。长音频识别会久一些 |
| `asr.language` | `""` | 留空自动识别；可填 `Chinese` / `English` |
| `polish.enabled` | `true` | 关掉就只用 ASR 原文（也已经带标点），延迟更低 |
| `polish.url` | `http://127.0.0.1:8082/v1/chat/completions` | 润色服务地址 |
| `polish.temperature` | `0.2` | 越低越稳定；再低会过于「照抄」 |
| `polish.max_ratio` | `2.2` | 润色结果超过原文这个倍数就丢弃，回退原文（防模型跑飞） |
| `polish.extra_rules` | `[]` | 附加整理规则，每条追加到 system prompt。例：数字转阿拉伯数字、说话人自我纠错只留最终结论 |

### `[ui]` — 悬浮胶囊

| 键 | 默认 | 说明 |
|---|---|---|
| `width` / `height` | `360` / `80` | 逻辑像素，自动按屏幕缩放；圆角 = 高度/2 |
| `bottom_margin` | `92` | 距屏幕底部像素 |
| `glass` | `true` | 截取身后桌面做高斯模糊（全屏独占程序下会自动退成纯色） |
| `tint` | `90` | 磨砂白度 0–255：越大越不透明、字越清楚；越小越透 |

### `[output]`

| 键 | 默认 | 说明 |
|---|---|---|
| `paste` | `true` | 自动 `Ctrl+V` 到光标处；`false` 只写剪贴板 |
| `restore_clipboard` | `true` | 粘贴后把剪贴板还原成原内容 |

## 性能实测

环境：Windows 11 + NVIDIA 独显（CUDA 12.4 版 llama.cpp b11304）、Q8_0 ASR + Q4_K_M 润色。

| 环节 | 耗时 |
|---|---|
| ASR（10 秒中文语音） | 0.6–1.5 s |
| 润色（4B，关思考链） | 0.4–1.2 s |
| 端到端（7.4 s 语音 → 上屏） | ≈ 1.9 s（识别 1.13 s + 润色 0.74 s） |
| 显存占用 | ASR ≈2.4 GB，润色 ≈3 GB |

润色效果示例：

```
原: 嗯这个 我今天下午三点钟要跟李工开个会 然后然后 把那个电源模块的测试报告发给他 顺便问一下他那个 cuda 那一块儿进展怎么样了
润: 今天下午三点钟要跟李工开个会，然后把电源模块的测试报告发给他，顺便问一下他 CUDA 那一块的进展怎么样了。
```

## 调优

- **识别不准 / 掉字**：先把 `[audio] min_rms` 调到 `0.02` 左右；离麦太远也会掉字（电容麦 15–25 cm 最佳）
- **嫌慢**：`[polish] enabled = false` 直接跳过第二级；或把润色模型换成更小的（1.7B / 2B）
- **显存不够（8 GB 卡）**：`[llama] ngl` 调到 26 左右，或干脆关润色
- **中间停顿被切掉**：不会。只掐首尾静音，中间的停顿原样保留
- **悬浮条太亮/太透**：调 `[ui] tint`（暗背景会自动加白，永远保持可读）
- **热键冲突**：换 `scroll_lock` / `pause` / `f9`

## 排错

| 现象 | 原因 / 处理 |
|---|---|
| 提示 `找不到 llama-server.exe` | 没解压 llama.cpp 或 `[llama] dir` 指错。见[第 3 步](#3-下载-llamacpp推理服务) |
| 提示 `缺少模型文件` | 模型没下全，重跑 `python download_models.py` |
| `:8081 等了 180 秒没就绪` | 看 `asr.log`：通常是显存不足、CUDA 驱动版本不匹配，或量化/版本不兼容 |
| 提示 `8081/8082 连接失败` | 服务没起来。先手动 `start_asr.bat` 看报错；或临时 `[polish] enabled = false` |
| 「已经有一个语音输入在运行了」 | 重复双击了。单实例锁会让第二个窗口 2 秒后自动退出 |
| 后台残留 `llama-server` | 正常退出会自动清理；万一残留，双击 `stop_all.bat` |
| 按热键没反应 | 托盘里被暂停了，或别的软件抢了 CapsLock（某些输入法/远程桌面） |
| 托盘没图标 | 在任务栏「^」隐藏区里，拖出来即可 |
| 悬浮条变朴素方块 | Skia 不可用，降级到 Pillow 版；日志里有 `[ui] 使用 Pillow 渲染` |
| 玻璃是纯色、没有模糊 | 全屏独占游戏 / 安全桌面截图失败，自动退成纯色玻璃 |
| 波形一直是小点 | 麦克风没声音：检查系统输入音量、换 `[audio] device`、调小 `min_rms` |
| 粘贴不进去 | 目标窗口是管理员权限，普通权限模拟的 Ctrl+V 无效 → 以管理员身份运行 `start_all.bat` |
| 日志里有 `[warn] 润色结果异常长，回退原文` | 正常保护：模型跑飞时丢弃润色结果，直接用 ASR 原文 |

日志：`asr.log`（识别服务）、`polish.log`（润色服务）、`client.log`（无控制台时主程序输出）。超过 5 MB 自动轮转成 `.log.1`。

## 目录结构

```
VoiceInput-VI/
├── voice_input.py          # 主程序：热键、录音、ASR/润色调用、上屏、模型子进程管理
├── overlay_skia.py         # 悬浮胶囊主渲染器（Win32 分层窗口 + Skia，亚像素 AA / 真模糊 / 预乘 alpha）
├── overlay_win.py          # 旧版 Pillow 渲染器 + Tk 兜底（Skia 不可用时自动降级）
├── tray_icon.py            # 系统托盘：暂停 / 开机自启 / 配置 / 日志 / 退出
├── test_asr.py             # 命令行链路测试：wav 文件 → ASR → 润色
├── preview_variants.py     # 生成胶囊外观参数对照图（调 UI 时用）
├── download_models.py      # GGUF 模型下载（断点续传 / HF 镜像）
├── config.toml             # 全部配置
├── requirements.txt
├── setup.bat               # 一次性安装：依赖 + 模型
├── start_all.bat           # 一键启动（只这一个窗口）
├── stop_all.bat            # 一键强停
├── start_asr.bat           # 手动模式：单独起 :8081
├── start_polish.bat        # 手动模式：单独起 :8082
├── ROADMAP.md              # 后续规划（设置界面 / 打包 / 流式上屏）
└── models/                 # 模型（.gitignore，不入库）
```

**进程模型**：`voice_input.py` 是唯一的常驻进程。它用 `subprocess` 拉起两个 `llama-server`，并用 Windows **Job 对象**（`KILL_ON_JOB_CLOSE`）把它们绑在自己身上 —— 主进程无论怎么死，系统都会连带回收子进程。启动时还会检测 8081/8082 端口，先杀掉上次残留再起自己的。

**测试整条链路**（不用麦克风）：

```bash
python test_asr.py your_sample.wav          # 默认 wav 不入库，自己准备一段
python test_asr.py your_sample.wav --no-polish
```

## 隐私与数据

- 音频只在**内存**里流转：`numpy int16` → WAV bytes → base64 → 本机 `127.0.0.1:8081`，不落盘、不出网卡
- 两个推理服务都绑 `127.0.0.1`，局域网也访问不到
- 剪贴板：默认粘贴后 0.6 秒还原原内容；不想写剪贴板可以关掉 `paste`
- 本仓库不含任何 API Key、账号、个人路径；`models/`、`*.wav`、`*.log`、截图都在 `.gitignore` 里

## 替换 / 升级模型

1. 把新的 `.gguf` 放进 `models\`（ASR 还要一个匹配的 `mmproj-*.gguf`）
2. 改 `config.toml`：

```toml
[llama]
asr_model = "models/<你的ASR>.gguf"
asr_mmproj = "models/mmproj-<你的ASR>.gguf"
polish_model = "models/<你的润色模型>.gguf"
```

3. 重启 `start_all.bat`

体积/速度权衡参考：

| 档位 | ASR | 润色 | 模型总体积 | 说明 |
|---|---|---|---|---|
| 极速 | Qwen3-ASR-0.6B Q8_0 | Qwen3.5-1.7B Q4 | ≈1.7 GB | 老旧笔记本也能跑 |
| 标准（默认） | Qwen3-ASR-1.7B Q8_0 | Qwen3.5-4B Q4_K_M | ≈5.5 GB | 准确率和延迟平衡 |
| 仅识别 | Qwen3-ASR-1.7B Q8_0 | — | ≈2.5 GB | `[polish] enabled = false` |

> 换用**思考型**模型做润色时，注意请求里已经带了 `chat_template_kwargs.enable_thinking = false`；如果你的模型模板不认这个参数，会按默认行为输出，可能需要调大 `timeout` 或在 `extra_rules` 里要求「只输出最终文字」。

## 路线图

见 [ROADMAP.md](ROADMAP.md)：图形设置界面 → 打包 exe / 首启下载 → VAD 分段流式上屏。

## 致谢 / 许可

- 推理：[llama.cpp](https://github.com/ggml-org/llama.cpp)（MIT）
- 模型：[Qwen3-ASR](https://huggingface.co/ggml-org/Qwen3-ASR-1.7B-GGUF)、[Qwen3.5](https://huggingface.co/unsloth/Qwen3.5-4B-GGUF)（Apache-2.0）
- 本项目代码：[MIT](LICENSE)
