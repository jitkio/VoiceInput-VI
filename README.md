# Voice Input · 本地离线语音输入

> **按住 CapsLock 说话，松开就上屏。** 识别 + 文字整理全部在本机 GPU 上完成，声音不出本机、不联网、不要 API Key。

<p align="left">
  <img alt="platform" src="https://img.shields.io/badge/platform-Windows%2010%2F11-0078D6">
  <img alt="python" src="https://img.shields.io/badge/python-3.11%2B-3776AB">
  <img alt="inference" src="https://img.shields.io/badge/inference-llama.cpp%20%2B%20CUDA-76B900">
  <img alt="license" src="https://img.shields.io/badge/license-MIT-green">
</p>

```
麦克风 ──► 掐掉首尾静音 ──► Qwen3-ASR-1.7B (llama.cpp :8081, GPU)
                                  │  语音 → 带标点原文
                                  ▼
                            Qwen3.5-4B 润色 (llama.cpp :8082, GPU)
                                  │  去口水词 / 修同音错字 / 断句
                                  ▼
                            剪贴板 + Ctrl+V 上屏
```

**English TL;DR** — A Windows-first, fully offline hold-to-talk dictation tool. Hold `CapsLock`, speak, release: the audio is trimmed, transcribed by **Qwen3-ASR-1.7B** and cleaned up by **Qwen3.5-4B**, both served locally by **llama.cpp on the GPU**, then pasted at the cursor. **One Python file** (~2,900 lines) + one config + models. No PyTorch, no cloud, no API key.

---

## 一分钟上手

### 方式 A：直接跑（推荐先试这个）

```bash
git clone https://github.com/jitkio/VoiceInput-VI.git
cd VoiceInput-VI
VoiceInput.bat
```

`VoiceInput.bat` 会自己完成所有事：

1. 缺 Python 包 → `pip install -r requirements.txt`
2. 缺 llama.cpp → 自动下载 Windows CUDA 版（约 650 MB）解压到 `llama\`
3. 缺模型 → 自动下载 3 个 GGUF（约 5.5 GB）到 `models\`
4. 起两个推理服务，出现「就绪。按住 [caps_lock] 说话」

**如果想自己放模型**（已有 GGUF，或想离线拷贝）：

```
VoiceInput-VI\
├── llama\    ← 解压 llama.cpp 到这里（llama-server.exe 在这一层）
└── models\   ← 放这 3 个文件
    ├── Qwen3-ASR-1.7B-Q8_0.gguf
    ├── mmproj-Qwen3-ASR-1.7B-Q8_0.gguf
    └── Qwen3.5-4B-Q4_K_M.gguf
```

然后 `python voice_input.py --check` 自检，再双击 `VoiceInput.bat`。

### 方式 B：打包成 exe（可分发给别人）

```bash
build_exe.bat
```

产出 `dist\VoiceInput\VoiceInput.exe`（约 164 MB，PyInstaller onedir）。把 `dist\VoiceInput\` 整个文件夹拷走就能用；
里面的 `models\`、`llama\` 是空的，要么把文件放进去，要么在该目录执行 `VoiceInput.exe --setup` 自动下。

构建脚本会自动建一个隔离环境 `.venv_build`，并固定 `pyinstaller==6.11.1` —— 更新的 PyInstaller 打包 skia 会原生崩溃。
exe 版和脚本版**用同一套 Skia 渲染**，动画完全一致。产物约 230 MB（含 numpy / skia 运行库），首次构建 3–5 分钟。

---

## 它是用 GPU 跑的吗？

**推理是，界面/录音不是。** 具体分工：

| 部分 | 在哪跑 | 说明 |
|---|---|---|
| 语音识别（Qwen3-ASR-1.7B） | **GPU** | `-ngl 99` 全部层offload；音频编码器（mmproj）也默认在 GPU |
| 文字润色（Qwen3.5-4B） | **GPU** | 同上 |
| 录音、掐静音、剪贴板、热键 | CPU | numpy 处理几十毫秒音频，可忽略 |
| 悬浮胶囊、托盘 | CPU | Win32 + Skia/Pillow 绘制 |

- 显存占用：ASR ≈ 2.4 GB + 润色 ≈ 3 GB ≈ **5.4 GB**（8 GB 卡够用，但别再开大游戏）
- 只支持 NVIDIA 的场景用了 CUDA 后端；A 卡 / 集显可用 `--setup --backend vulkan`；没显卡就 `--backend cpu` + `config.toml` 里 `ngl = 0`
- 显存不够：把 `ngl` 调小（如 26），或直接 `[polish] enabled = false` 只留 ASR
- 想确认真的在用 GPU：`nvidia-smi` 里应该能看到两个 `llama-server.exe` 占显存

## 项目结构（就 7 个文件）

| 文件 | 作用 |
|---|---|
| `voice_input.py` | **全部代码**：热键/录音/ASR/润色/上屏 + 悬浮胶囊 + 托盘 + 模型进程管理 + 命令行工具 |
| `config.toml` | 全部配置（相对路径，可整目录搬走） |
| `requirements.txt` | 6 个 Python 依赖 |
| `VoiceInput.bat` | 双击启动；`VoiceInput.bat stop` 强制停止 |
| `build_exe.bat` | 打包成 exe |
| `README.md` / `LICENSE` | 说明 / MIT |

运行时目录：`models/`（模型）、`llama/`（llama.cpp）、`dist/`（打包产物）——都已 gitignore，不入库。

## 使用的模型

| 环节 | 模型 | 量化 | 体积 | 服务 |
|---|---|---|---|---|
| 语音识别 | **Qwen3-ASR-1.7B** · [ggml-org/Qwen3-ASR-1.7B-GGUF](https://huggingface.co/ggml-org/Qwen3-ASR-1.7B-GGUF) | Q8_0 | 2.2 GB + mmproj 0.34 GB | `:8081` |
| 文字润色 | **Qwen3.5-4B** · [unsloth/Qwen3.5-4B-GGUF](https://huggingface.co/unsloth/Qwen3.5-4B-GGUF) | Q4_K_M | 2.9 GB | `:8082` |

- **Qwen3-ASR-1.7B**：端到端语音识别，中英混说自动识别语种，输出自带标点。llama.cpp 通过 `mtmd` 多模态通道喂音频，所以 `mmproj` 音频编码器必须一起加载。
- **Qwen3.5-4B**：只做「把这段已经识别出来的字整理通顺」——加标点、去「嗯/那个/就是说」、修同音错字、断句；不翻译、不扩写、不改人称。请求里关掉了思考链（`chat_template_kwargs.enable_thinking = false`），延迟压到 1 秒内。
- 推理框架：[llama.cpp](https://github.com/ggml-org/llama.cpp)（MIT），项目固定用 `b11304` 的 Windows 预编译包。
- 模型协议：Qwen 系列 Apache-2.0。本仓库不附带权重。

## 环境要求

| 项 | 要求 |
|---|---|
| 系统 | Windows 10 / 11 x64（悬浮窗、热键抑制、托盘、开机自启都是 Win32 实现；macOS/Linux 未适配） |
| Python | 3.11+（用到 `tomllib`），实测 3.12 |
| 显卡 | NVIDIA，显存 ≥ 6 GB 体验最佳；Vulkan / 纯 CPU 也能跑 |
| 磁盘 | llama.cpp ≈ 0.7 GB（CUDA 版）+ 模型 ≈ 5.5 GB |
| 其他 | 一个能用的麦克风 |

## 命令行

```bash
python voice_input.py                # 启动（自动拉起/托管两个 llama-server）
python voice_input.py --setup        # 一次性准备：下载 llama.cpp + 模型（断点续传）
python voice_input.py --setup --backend vulkan   # A 卡/集显
python voice_input.py --check        # 自检：llama.cpp 和模型是否就位
python voice_input.py --test a.wav   # 不用麦克风，把 wav 过一遍 ASR+润色
python voice_input.py --make-icon i.ico   # 导出图标（打包用）
python voice_input.py --config my.toml    # 用别的配置文件
```

国内下载慢：`set HF_ENDPOINT=https://hf-mirror.com` 后再跑 `--setup`。

## 配置说明

全部配置在 `config.toml`（改完重启生效）。

### `[audio]`

| 键 | 默认 | 说明 |
|---|---|---|
| `samplerate` | `16000` | 目标采样率；设备不支持会自动改用设备采样率再重采样 |
| `device` | `""` | 麦克风设备名或索引，留空 = 系统默认。`python -c "import sounddevice;print(sounddevice.query_devices())"` |
| `min_rms` | `0.012` | 静音门限，环境吵调大（0.02），掉字调小 |
| `pad_ms` | `200` | 掐掉首尾静音后两端各保留多少毫秒 |
| `max_seconds` | `150` | 单次录音上限（模型硬上限约 4.2 分钟） |

### `[llama]` — 推理服务

| 键 | 默认 | 说明 |
|---|---|---|
| `manage` | `true` | 由主程序启动/关闭 llama-server；`false` 则自己手动起服务 |
| `dir` | `"llama"` | llama.cpp 目录（相对项目根目录或绝对路径；也认环境变量 `LLAMA_DIR`） |
| `asr_model` | `models/Qwen3-ASR-1.7B-Q8_0.gguf` | 识别模型 |
| `asr_mmproj` | `models/mmproj-Qwen3-ASR-1.7B-Q8_0.gguf` | 音频编码器 |
| `polish_model` | `models/Qwen3.5-4B-Q4_K_M.gguf` | 润色模型 |
| `ngl` | `99` | 上 GPU 的层数；显存紧张调小，纯 CPU 设 0 |
| `threads` | `8` | CPU 线程数 |

### `[asr]` / `[polish]`

| 键 | 默认 | 说明 |
|---|---|---|
| `asr.url` | `http://127.0.0.1:8081/v1/chat/completions` | OpenAI 兼容接口，可指向局域网别的机器 |
| `asr.timeout` | `90` | 秒 |
| `asr.language` | `""` | 留空自动识别；可填 `Chinese` / `English` |
| `polish.enabled` | `true` | 关掉只用 ASR 原文（也带标点），更快 |
| `polish.url` | `http://127.0.0.1:8082/v1/chat/completions` | 润色服务 |
| `polish.temperature` | `0.2` | 越低越稳 |
| `polish.max_ratio` | `2.2` | 润色结果超过原文这个倍数就丢弃，回退原文（防跑飞） |
| `polish.extra_rules` | `[]` | 附加整理规则，例：数字转阿拉伯数字、自我纠错只留结论 |

### `[ui]` / `[hotkey]` / `[output]`

| 键 | 默认 | 说明 |
|---|---|---|
| `ui.width` / `ui.height` | `360` / `80` | 胶囊尺寸（逻辑像素，自动按 DPI 缩放） |
| `ui.bottom_margin` | `92` | 离屏幕底部距离 |
| `ui.glass` | `true` | 截取身后桌面做高斯模糊 |
| `ui.tint` | `90` | 磨砂白度 0–255，越大越不透明 |
| `hotkey.key` | `"caps_lock"` | 可选 `caps_lock` / `scroll_lock` / `pause` / `f9` |
| `output.paste` | `true` | 自动 Ctrl+V；`false` 只写剪贴板 |
| `output.restore_clipboard` | `true` | 粘贴 0.6 秒后还原原剪贴板内容 |

## 界面动画

悬浮胶囊有四种状态，每次切换都是连续动画，不是硬切：

| 状态 | 动画 |
|---|---|
| 出现 | 0.55 s：缩放 0.90 → 1.00 带回弹、上浮 13 px、颜色由上一状态过渡；同时玻璃上扫过一道高光 |
| 录音中 | 红点随音量微放大 + 双层扩散光圈；32 根深蓝波形柱（起音快/回落慢，带倒影）；计时数字每秒轻轻弹一下；每 3.6 s 扫一次高光 |
| 识别中 | 波形变成流动的正弦波；右侧黑圆里是锥形渐变彗尾 + 头部光点 + 三颗环绕小点，半径轻微呼吸 |
| 已上屏 | 对勾沿路径逐段画出（0.3 s）、圆钮弹一下、绿色光圈扩散、六颗小火花、整条玻璃闪一下绿、文字滑入；1.1 s 后淡出 |
| 出错 | 整颗胶囊左右衰减抖动、感叹号弹跳进入、文字滑入，1.1 s 后淡出 |

时间/状态都由 `time.time()` 驱动，帧率固定 50 fps；胶囊隐藏时直接不渲染（CPU ≈ 0）。
单帧绘制实测 ~3 ms（含光晕、波形、渐变），只在胶囊出现的两三秒内发生。

## 使用

| 操作 | 效果 |
|---|---|
| 按住 `CapsLock` 说话 | 底部浮出胶囊，波形随音量跳动（CapsLock 不会切大小写） |
| 松开 | 「识别中」→ 自动粘贴 → 「已上屏」淡出 |
| 托盘右键 | 暂停监听 / 开机自启 / 打开配置 / 查看日志 / 退出 |
| 关窗口 / `Ctrl+C` | 主程序退出，两个 llama-server 一起关闭（Windows Job 对象保证） |
| `VoiceInput.bat stop` | 应急：强杀主程序和所有 llama-server |

**进程模型**：`voice_input.py` 是唯一常驻进程，它把两个 `llama-server` 绑进自己的 Windows Job 对象——主进程不管怎么死（关窗、Ctrl+C、任务管理器强杀），系统都会连带回收子进程。启动时还会检查 8081/8082，先清理上次残留再起自己的。

## 性能实测

环境：Windows 11 + NVIDIA 独显（CUDA 12.4 / llama.cpp b11304）、ASR Q8_0 + 润色 Q4_K_M。

| 环节 | 耗时 |
|---|---|
| ASR（10 秒中文语音） | 0.6–1.5 s |
| 润色（4B，关思考链） | 0.4–1.2 s |
| 端到端（9.2 s 语音 → 上屏） | ≈ 1.9 s（识别 0.96 s + 润色 1.04 s） |
| 显存占用 | ≈ 5.4 GB |

润色效果：

```
原: 嗯这个 我今天下午三点钟要跟李工开个会 然后然后 把那个电源模块的测试报告发给他 顺便问一下他那个 cuda 那一块儿进展怎么样了
润: 今天下午三点钟要跟李工开个会，然后把电源模块的测试报告发给他，顺便问一下他 CUDA 那一块的进展怎么样了。
```

## 调优

- **识别不准 / 掉字**：`[audio] min_rms` 调到 0.02 左右；离麦 15–25 cm
- **嫌慢**：`[polish] enabled = false` 跳过第二级
- **显存不够**：`ngl` 降到 26，或关润色；再不行 `--backend cpu` + `ngl = 0`
- **悬浮条太亮/太透**：调 `[ui] tint`
- **热键冲突**：换 `scroll_lock` / `pause` / `f9`

## 排错

| 现象 | 原因 / 处理 |
|---|---|
| 双击 bat 闪一下就没了 | 在 cmd 里跑 `python -u voice_input.py` 看报错；或 `python voice_input.py --check` 查缺什么 |
| `--check` 报缺 llama-server.exe | llama.cpp 没解压或 `[llama] dir` 指错。跑 `python voice_input.py --setup --no-models` |
| `--check` 报缺模型 | 跑 `python voice_input.py --setup --no-llama`，或手动放 `models\` |
| `:8081 等了 180 秒没就绪` | 看 `asr.log`：显存不足 / 驱动版本不匹配 / 模型文件损坏 |
| 提示 `8081/8082 连接失败` | 服务没起来，先看 `asr.log`、`polish.log` |
| 「已经有一个语音输入在运行了」 | 重复双击了，第二个实例 2 秒自动退出（想并行跑可用环境变量 `VOICE_INPUT_LOCK_PORT` 换锁端口） |
| 后台残留 llama-server | `VoiceInput.bat stop` |
| 按热键没反应 | 托盘里被暂停了，或被输入法/远程桌面抢了 CapsLock |
| 托盘没图标 | 在任务栏「^」隐藏区，拖出来 |
| 悬浮条变朴素方块 | exe 版正常（打包排除 skia）；脚本版见日志 `[ui] 使用 Skia 渲染` |
| 玻璃是纯色、没模糊 | 全屏独占程序/安全桌面截不到屏，自动退成纯色 |
| 粘贴不进去 | 目标窗口是管理员权限 → 以管理员身份运行 `VoiceInput.bat` |
| exe 版没有动画 | 不该发生：打包版同样带 Skia；看日志里有没有 `[ui] 使用 Skia 渲染` |

日志：`asr.log`（识别服务）、`polish.log`（润色服务）、`client.log`（无控制台时的主程序输出），超过 5 MB 自动轮转。

## 隐私与数据

- 音频只在内存里流转（numpy → WAV → base64 → `127.0.0.1:8081`），不落盘、不出网卡
- 两个推理服务只绑 `127.0.0.1`，局域网也访问不到
- 剪贴板默认粘贴后还原；不想写剪贴板可关 `paste`
- 仓库不含 API Key、账号、个人路径；`models/`、`llama/`、`*.wav`、`*.log`、`dist/` 都在 `.gitignore` 里

## 路线图

见 [ROADMAP.md](ROADMAP.md)：图形设置界面 → 首启下载向导 → VAD 分段流式上屏。

## 许可

代码 [MIT](LICENSE)。推理 [llama.cpp](https://github.com/ggml-org/llama.cpp)（MIT），模型 Qwen 系列（Apache-2.0）。
