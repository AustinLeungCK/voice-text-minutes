#!/usr/bin/env python3
"""
廣東話會議錄音 → Meeting Minutes

Usage:
    python transcribe.py <錄音檔路徑> [--hf-token <HuggingFace Token>]

流程:
    1. faster-whisper (large-v3, language="yue") 語音辨識
    2. pyannote-audio speaker diarization
    3. 合併 transcript + 講者標記
    4. LM Studio (localhost:1234) 整理成 meeting minutes
    5. 輸出 meeting_minutes.md
"""

import argparse
import io
import os
import sys
import tempfile

# Fix Windows console encoding for Chinese characters
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

import av
from faster_whisper import WhisperModel
from pyannote.audio import Pipeline
from openai import OpenAI


def convert_to_wav(input_path: str) -> str:
    """將 mp4/m4a 轉成 wav（pyannote 需要 wav）"""
    print(f"[1/5] 轉換音檔: {input_path}")
    wav_path = tempfile.mktemp(suffix=".wav")
    input_container = av.open(input_path)
    output_container = av.open(wav_path, mode="w")
    in_stream = input_container.streams.audio[0]
    out_stream = output_container.add_stream("pcm_s16le", rate=16000, layout="mono")
    for frame in input_container.decode(in_stream):
        frame.pts = None
        for packet in out_stream.encode(frame):
            output_container.mux(packet)
    for packet in out_stream.encode():
        output_container.mux(packet)
    output_container.close()
    input_container.close()
    print(f"      → WAV: {wav_path}")
    return wav_path


def transcribe_audio(wav_path: str) -> list[dict]:
    """用 faster-whisper 做廣東話語音辨識，回傳 segments"""
    print("[2/5] 語音辨識中 (faster-whisper large-v3, language=yue)...")
    model = WhisperModel("large-v3", device="auto", compute_type="auto")
    segments, info = model.transcribe(wav_path, language="yue", beam_size=5)
    results = []
    for seg in segments:
        results.append({
            "start": seg.start,
            "end": seg.end,
            "text": seg.text.strip(),
        })
    print(f"      → 辨識到 {len(results)} 段文字")
    return results


def diarize_audio(wav_path: str, hf_token: str) -> list[dict]:
    """用 pyannote-audio 做 speaker diarization"""
    print("[3/5] Speaker diarization 中 (pyannote-audio)...")
    pipeline = Pipeline.from_pretrained(
        "pyannote/speaker-diarization-3.1",
        token=hf_token,
    )
    diarization = pipeline(wav_path)
    speakers = []
    for turn, _, speaker in diarization.itertracks(yield_label=True):
        speakers.append({
            "start": turn.start,
            "end": turn.end,
            "speaker": speaker,
        })
    print(f"      → 分辨到 {len(set(s['speaker'] for s in speakers))} 位講者")
    return speakers


def merge_transcript(whisper_segments: list[dict], speaker_segments: list[dict]) -> str:
    """將 whisper 文字同 speaker diarization 合併"""
    print("[4/5] 合併 transcript + 講者標記...")

    def find_speaker(start: float, end: float) -> str:
        """搵出邊個講者同呢段文字重疊最多"""
        best_speaker = "Unknown"
        best_overlap = 0.0
        for sp in speaker_segments:
            overlap_start = max(start, sp["start"])
            overlap_end = min(end, sp["end"])
            overlap = max(0.0, overlap_end - overlap_start)
            if overlap > best_overlap:
                best_overlap = overlap
                best_speaker = sp["speaker"]
        return best_speaker

    lines = []
    for seg in whisper_segments:
        speaker = find_speaker(seg["start"], seg["end"])
        timestamp = format_time(seg["start"])
        lines.append(f"[{timestamp}] {speaker}: {seg['text']}")

    transcript = "\n".join(lines)
    print(f"      → 合併完成，共 {len(lines)} 行")
    return transcript


def format_time(seconds: float) -> str:
    """將秒數格式化為 HH:MM:SS"""
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


def generate_minutes(transcript: str) -> str:
    """透過 LM Studio API 將 transcript 整理成 meeting minutes"""
    print("[5/5] 用 LLM 整理 meeting minutes...")

    client = OpenAI(
        base_url="http://localhost:1234/v1",
        api_key="lm-studio",
    )

    prompt = f"""你係一個專業嘅會議記錄整理助手。以下係一段廣東話會議嘅逐字稿（帶時間戳同講者標記）。
請將佢整理成結構化嘅 meeting minutes，用繁體中文書寫。

要求：
1. 會議摘要（簡短總結）
2. 出席者（根據講者標記列出）
3. 討論議題（每個議題包括重點內容）
4. 決議事項
5. 行動項目（Action Items）— 包括負責人同期限（如有提及）
6. 下次會議安排（如有提及）

逐字稿：
---
{transcript}
---

請用 Markdown 格式輸出 meeting minutes。"""

    response = client.chat.completions.create(
        model="local-model",
        messages=[
            {"role": "system", "content": "你係一個專業嘅會議記錄整理助手，擅長將廣東話逐字稿整理成結構化嘅會議紀錄。"},
            {"role": "user", "content": prompt},
        ],
        temperature=0.3,
    )

    return response.choices[0].message.content


def main():
    parser = argparse.ArgumentParser(description="廣東話會議錄音轉 Meeting Minutes")
    parser.add_argument("audio_file", help="錄音檔路徑 (.mp4/.m4a/.wav)")
    parser.add_argument(
        "--hf-token",
        default=os.environ.get("HF_TOKEN"),
        help="HuggingFace token（用於 pyannote-audio）。亦可設定 HF_TOKEN 環境變數。",
    )
    parser.add_argument(
        "--output",
        default="meeting_minutes.md",
        help="輸出檔案路徑（預設: meeting_minutes.md）",
    )
    args = parser.parse_args()

    if not os.path.isfile(args.audio_file):
        print(f"錯誤: 搵唔到檔案 '{args.audio_file}'")
        sys.exit(1)

    if not args.hf_token:
        print("錯誤: 需要 HuggingFace token。用 --hf-token 或設定 HF_TOKEN 環境變數。")
        print("申請: https://huggingface.co/settings/tokens")
        print("同時需要接受 pyannote 模型條款: https://huggingface.co/pyannote/speaker-diarization-3.1")
        sys.exit(1)

    # Step 1: 轉換音檔
    wav_path = convert_to_wav(args.audio_file)

    try:
        # Step 2: 語音辨識
        whisper_segments = transcribe_audio(wav_path)

        # Step 3: Speaker diarization
        speaker_segments = diarize_audio(wav_path, args.hf_token)

        # Step 4: 合併
        transcript = merge_transcript(whisper_segments, speaker_segments)

        # 儲存 raw transcript
        transcript_path = args.output.replace(".md", "_transcript.txt")
        with open(transcript_path, "w", encoding="utf-8") as f:
            f.write(transcript)
        print(f"      → Raw transcript 已儲存: {transcript_path}")

        # Step 5: 生成 meeting minutes
        minutes = generate_minutes(transcript)

        with open(args.output, "w", encoding="utf-8") as f:
            f.write(minutes)
        print(f"\n完成！Meeting minutes 已儲存: {args.output}")

    finally:
        # 清理臨時 wav 檔
        if os.path.exists(wav_path):
            os.remove(wav_path)


if __name__ == "__main__":
    main()
