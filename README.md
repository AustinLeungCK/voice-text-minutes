# voice-text-minutes

將廣東話/中文會議錄音自動轉成結構化 Meeting Minutes。

支援 Teams 會議錄影（mp4 含畫面）同純錄音（mp4/m4a/wav）。

## 功能

- **語音辨識** — faster-whisper (large-v3-turbo)，支援廣東話 (yue)
- **講者分辨** — pyannote-audio speaker diarization，自動分辨唔同人嘅聲音
- **Slide OCR** — 從 Teams 錄影中擷取 share screen 嘅文字內容（easyocr）
- **參與者名字** — 從影片畫面 OCR 擷取 Teams 顯示嘅參與者名字
- **Meeting Minutes** — 透過 LM Studio (OpenAI-compatible API) 整理成結構化會議紀錄
- **Parent-Child Chunking** — 長 transcript 自動分段摘要，保留全局 context
- **AMD GPU 加速** — 透過 onnxruntime-directml 支援 AMD GPU (Windows)
- **完全離線** — 模型預先下載，運行時唔需要 internet

## 流程

```
錄音檔 (.mp4/.m4a/.wav)
  ├─ faster-whisper → 文字 + 時間戳        ┐
  ├─ pyannote-audio → 講者分段              ├─ 並行處理
  ├─ easyocr → slide 文字（如有影片）       │
  └─ easyocr → 參與者名字（如有影片）       ┘
        ↓
  合併 transcript（文字 + 講者 + 時間戳）
        ↓
  LM Studio (Parent-Child Chunking)
    Step 1: 全局大綱 (Parent)
    Step 2: 逐段摘要 + 大綱 context (Child)
    Step 3: 合併成完整 meeting minutes
        ↓
  output/meeting_minutes.md
```

## 安裝

### 前置條件

- Python 3.10+
- [LM Studio](https://lmstudio.ai/) — 本地 LLM 推理（需要開住，預設 localhost:1234）
- [HuggingFace 帳號](https://huggingface.co/) — 首次下載 pyannote 模型需要（之後離線運行）

### 步驟

```bash
# 1. Clone
git clone https://github.com/AustinLeungCK/voice-text-minutes.git
cd voice-text-minutes

# 2. 建立虛擬環境
python -m venv venv
source venv/Scripts/activate  # Windows (Git Bash)
# 或 venv\Scripts\activate    # Windows (CMD)
# 或 source venv/bin/activate # macOS/Linux

# 3. 安裝依賴
pip install -r requirements.txt

# 4. 首次運行：下載模型（需要 internet + HuggingFace token）
#    a) 下載 pyannote 模型（需要先去 HuggingFace 接受條款）
#       - https://huggingface.co/pyannote/speaker-diarization-3.1
#       - https://huggingface.co/pyannote/segmentation-3.0
#       - https://huggingface.co/pyannote/speaker-diarization-community-1
python -c "
from pyannote.audio import Pipeline
p = Pipeline.from_pretrained('pyannote/speaker-diarization-3.1', token='YOUR_HF_TOKEN')
print('pyannote models downloaded')
"

#    b) 下載 whisper 模型
python -c "
from faster_whisper import WhisperModel
m = WhisperModel('large-v3-turbo', device='cpu', compute_type='auto')
print('whisper model downloaded')
"

#    c) 下載 easyocr 模型（繁中 + 英文）
python -c "
import easyocr
r = easyocr.Reader(['ch_tra', 'en'], gpu=False)
print('easyocr models downloaded')
"
```

模型下載完成後，之後運行完全離線，唔需要 internet。

### AMD GPU 支援（可選）

如果有 AMD GPU (Windows)，安裝 DirectML 加速 Whisper：

```bash
pip install onnxruntime-directml
```

## 使用

```bash
# 基本用法
python transcribe.py recordings/meeting.mp4

# 指定輸出路徑
python transcribe.py recordings/meeting.mp4 --output output/my_meeting.md

# 純錄音（冇畫面）— 自動跳過 slide/名字擷取
python transcribe.py recordings/audio_only.m4a
```

### 輸出

每次運行會生成兩個檔案：

- `output/meeting_minutes.md` — 結構化會議紀錄
- `output/meeting_minutes_transcript.txt` — 原始 transcript（帶時間戳 + 講者標記 + slide 內容）

### LM Studio 設定

1. 開啟 LM Studio，載入一個中文能力好嘅模型（推薦 Qwen 系列）
2. 啟動 Local Server（預設 localhost:1234）
3. 建議設定：
   - Context Length: 盡量大（如 131072）
   - GPU Offload: 盡量多層放 GPU

## 檔案結構

```
voice-text-minutes/
├── transcribe.py       # 主程式
├── requirements.txt    # Python 依賴
├── README.md
├── CLAUDE.md           # Claude Code 開發指引
├── .gitignore
├── recordings/         # 錄音檔（唔入 git）
└── output/             # 輸出結果（唔入 git）
```

## 技術細節

### Parent-Child Chunking

長會議 transcript 太大，超出 LLM context window 時自動啟用：

1. **Parent（大綱）**：取每段頭尾各 5 行，生成全局會議大綱
2. **Child（逐段）**：每段 400 行 + 大綱 context，分別摘要
3. **合併**：將所有摘要 + slide 內容合併成最終 meeting minutes

短 transcript（≤400 行）會直接一次過處理，唔使分段。

### 講者對應

1. pyannote 用聲紋分辨唔同講者（SPEAKER_00, SPEAKER_01...）
2. 如果有影片，OCR 從 Teams UI 擷取參與者名字
3. LLM 根據對話內容（例如「謝謝 Thomas」）將 SPEAKER_XX 對應返真名

### 並行處理

以下四個步驟同時運行，大幅縮短處理時間：

| 步驟 | 用途 | 運算資源 |
|:---|:---|:---|
| Whisper | 語音辨識 | GPU (DirectML) 或 CPU |
| pyannote | 講者分辨 | CPU (PyTorch) |
| Slide OCR | 擷取畫面文字 | CPU |
| Name OCR | 擷取參與者名字 | CPU |
