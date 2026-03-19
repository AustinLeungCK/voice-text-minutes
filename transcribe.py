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

# 強制離線模式 — 唔會連 HuggingFace
os.environ["HF_HUB_OFFLINE"] = "1"
os.environ["TRANSFORMERS_OFFLINE"] = "1"

# Fix Windows console encoding for Chinese characters
if hasattr(sys.stdout, "buffer") and not isinstance(sys.stdout, io.TextIOWrapper):
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "buffer") and not isinstance(sys.stderr, io.TextIOWrapper):
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

import re
from concurrent.futures import ThreadPoolExecutor, as_completed

import numpy as np
import torch
import soundfile as sf
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


def extract_participant_names(video_path: str) -> list[str]:
    """從影片中 OCR 擷取 Teams 參與者名字"""
    container = av.open(video_path)
    video_stream = None
    for s in container.streams:
        if s.type == "video":
            video_stream = s
            break
    if video_stream is None:
        return []

    print("[*] 擷取參與者名字中...")
    import easyocr
    reader = easyocr.Reader(["ch_tra", "en"], gpu=torch.cuda.is_available())

    fps = float(video_stream.average_rate)
    # 喺頭 30 秒內每 5 秒抽一個 frame
    sample_times = [int(fps * t) for t in range(0, 31, 5)]
    all_texts = []
    frame_count = 0

    for frame in container.decode(video_stream):
        if frame_count in sample_times:
            img = frame.to_ndarray(format="rgb24")
            h, w = img.shape[:2]
            # Teams 參與者名字通常喺畫面左側或底部
            # 擷取左邊 1/3 同底部 1/4
            regions = [
                img[:, :w // 3],          # 左邊 1/3
                img[h * 3 // 4:, :],       # 底部 1/4
                img[:h // 4, :],           # 頂部 1/4（Teams 有時顯示名字喺頂部）
            ]
            for region in regions:
                results = reader.readtext(region)
                for _, text, conf in results:
                    text = text.strip()
                    # 過濾：名字通常 2-30 字，有一定信心度
                    if conf > 0.3 and 2 <= len(text) <= 30:
                        all_texts.append(text)

        frame_count += 1
        if frame_count > max(sample_times):
            break

    container.close()

    # 過濾同統計
    from collections import Counter

    def is_likely_name(text: str) -> bool:
        """判斷文字是否可能係人名"""
        # 去掉純數字 / 大部分係數字嘅（AWS account numbers 等）
        digits = sum(c.isdigit() for c in text)
        if digits > len(text) * 0.5:
            return False
        # 去掉太短嘅
        if len(text) < 3:
            return False
        # 去掉 UI 元素
        ui_words = {"Edit", "View", "Help", "Search", "Recorded by", "Ctrl",
                     "Insert", "100%", "公式", "插入", "杜視", "亡共用"}
        if text in ui_words or any(text.startswith(w) for w in ["Ctrl+", "Cmd"]):
            return False
        # 去掉帶太多特殊符號嘅
        specials = sum(c in "|-+#~[]{}$&@!_=" for c in text)
        if specials > 2:
            return False
        # 名字通常有字母
        if not any(c.isalpha() for c in text):
            return False
        return True

    filtered = [t for t in all_texts if is_likely_name(t)]
    text_counts = Counter(filtered)
    # 出現 2 次或以上嘅文字，按出現次數排序
    names = [text for text, count in text_counts.most_common() if count >= 2]

    if names:
        print(f"      → 偵測到可能嘅參與者名字: {names}")
    else:
        print("      → 未能從影片中偵測到參與者名字")
    return names


def extract_slides(video_path: str, interval: float = 5.0, threshold: float = 30.0) -> list[dict]:
    """從影片中擷取 slide 變化，OCR 提取文字"""
    container = av.open(video_path)
    video_stream = None
    for s in container.streams:
        if s.type == "video":
            video_stream = s
            break
    if video_stream is None:
        print("      → 冇搵到影片軌道，跳過 slide 擷取")
        return []

    print("[*] 擷取 slides 中...")
    # 懶載入 easyocr（第一次會下載模型）
    import easyocr
    reader = easyocr.Reader(["ch_tra", "en"], gpu=torch.cuda.is_available())

    fps = float(video_stream.average_rate)
    frame_interval = int(fps * interval)
    prev_frame = None
    slides = []
    frame_count = 0

    for frame in container.decode(video_stream):
        if frame_count % frame_interval != 0:
            frame_count += 1
            continue
        frame_count += 1

        img = frame.to_ndarray(format="rgb24")
        gray = np.mean(img, axis=2)

        # 比較同上一個 frame 嘅差異
        if prev_frame is not None:
            diff = np.mean(np.abs(gray - prev_frame))
            if diff < threshold:
                continue  # 畫面冇變，跳過

        prev_frame = gray
        timestamp = float(frame.pts * video_stream.time_base)

        # OCR
        results = reader.readtext(img)
        text = " ".join([r[1] for r in results]).strip()
        if text:
            slides.append({"timestamp": timestamp, "text": text})

    container.close()
    print(f"      → 擷取到 {len(slides)} 頁 slide 文字")
    return slides


def transcribe_audio(wav_path: str) -> list[dict]:
    """用 faster-whisper 做廣東話語音辨識，回傳 segments"""
    print("[2/5] 語音辨識中 (faster-whisper large-v3-turbo + DirectML)...")
    # 嘗試用 DirectML (AMD GPU)，失敗就用 CPU
    try:
        import onnxruntime
        if "DmlExecutionProvider" in onnxruntime.get_available_providers():
            model = WhisperModel("large-v3-turbo", device="auto", compute_type="float32")
            print("      → 使用 DirectML (AMD GPU)")
        else:
            raise RuntimeError("No DML")
    except Exception:
        model = WhisperModel("large-v3-turbo", device="cpu", compute_type="auto")
        print("      → 使用 CPU")
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
    # 用 soundfile 預先載入音檔，繞過 torchcodec
    data, sample_rate = sf.read(wav_path)
    waveform = torch.from_numpy(data).float().unsqueeze(0)  # (1, samples)
    diarization = pipeline({"waveform": waveform, "sample_rate": sample_rate})
    annotation = diarization.speaker_diarization
    speakers = []
    for turn, _, speaker in annotation.itertracks(yield_label=True):
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


def _llm_call(client, system: str, user: str) -> str:
    """呼叫 LM Studio API 並清理 <think> 標籤"""
    response = client.chat.completions.create(
        model="local-model",
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        temperature=0.3,
    )
    content = response.choices[0].message.content
    return re.sub(r"<think>.*?</think>\s*", "", content, flags=re.DOTALL)


def generate_minutes(transcript: str, slide_text: str = "", participant_names: list[str] = None, chunk_lines: int = 400) -> str:
    """透過 LM Studio API 將 transcript 整理成 meeting minutes（Parent-Child Chunking）"""
    print("[5/5] 用 LLM 整理 meeting minutes (Parent-Child Chunking)...")

    client = OpenAI(
        base_url="http://localhost:1234/v1",
        api_key="lm-studio",
    )

    system_msg = "你係一個專業嘅會議記錄整理助手，擅長將廣東話逐字稿整理成結構化嘅會議紀錄。"
    names_hint = ""
    if participant_names:
        names_hint = f"\n已知參與者名字：{', '.join(participant_names)}。請將 SPEAKER_XX 對應返真名。"

    lines = transcript.split("\n")
    total_lines = len(lines)

    # 如果 transcript 夠短，直接一次過處理
    if total_lines <= chunk_lines:
        print("      → Transcript 夠短，直接處理")
        slide_section = ""
        if slide_text:
            slide_section = f"\n\nSlide 內容：\n{slide_text}"
        prompt = f"以下係一段會議逐字稿。請整理成結構化 meeting minutes（Markdown），包括：1.會議摘要 2.出席者 3.討論議題 4.決議事項 5.行動項目 6.下次會議。用繁體中文。{names_hint}\n\n逐字稿：\n{transcript}{slide_section}"
        return _llm_call(client, system_msg, prompt)

    # === Parent-Child Chunking ===

    # Step 1 (Parent): 用每段嘅頭尾幾行做粗略大綱
    print("      → Step 1/3: 生成全局大綱 (Parent)...")
    sample_lines = []
    for i in range(0, total_lines, chunk_lines):
        chunk = lines[i:i + chunk_lines]
        # 取每段嘅頭 5 行同尾 5 行做 sample
        head = chunk[:5]
        tail = chunk[-5:] if len(chunk) > 10 else []
        sample_lines.extend(head)
        if tail:
            sample_lines.append("...")
            sample_lines.extend(tail)
        sample_lines.append("")

    sample_text = "\n".join(sample_lines)
    parent_summary = _llm_call(client, system_msg,
        f"以下係一段會議逐字稿嘅摘錄（每段嘅開頭同結尾）。請寫一個簡短嘅會議大綱，列出主要討論嘅議題同參與者。用繁體中文。{names_hint}\n\n{sample_text}")
    print("      → 大綱完成")

    # Step 2 (Child): 逐段摘要，附帶 parent context
    chunks = [lines[i:i + chunk_lines] for i in range(0, total_lines, chunk_lines)]
    summaries = []

    for i, chunk in enumerate(chunks):
        chunk_text = "\n".join(chunk)
        print(f"      → Step 2/3: 處理第 {i + 1}/{len(chunks)} 段...", flush=True)
        try:
            summary = _llm_call(client, system_msg,
                f"以下係一段會議逐字稿嘅第 {i + 1} 部分（共 {len(chunks)} 部分）。\n\n會議大綱（全局 context）：\n{parent_summary}\n\n請摘要呢段嘅重點，包括：討論咗咩、邊個講咗咩、有咩決定同行動項目。用繁體中文。{names_hint}\n\n逐字稿：\n{chunk_text}")
            summaries.append(summary)
        except Exception as e:
            print(f"        ERROR: {e}")
            summaries.append(f"[第 {i + 1} 段處理失敗]")

    # Step 3: 合併所有 child summaries + slide 內容 → 最終 meeting minutes
    print("      → Step 3/3: 合併生成最終 meeting minutes...")
    combined = "\n\n".join([f"### 第 {i + 1} 部分摘要\n{s}" for i, s in enumerate(summaries)])

    slide_section = ""
    if slide_text:
        slide_section = f"\n\n此外，以下係會議畫面嘅 slide 內容：\n{slide_text}"

    final = _llm_call(client, system_msg,
        f"以下係一個會議嘅分段摘要同全局大綱。請整合成一份完整嘅結構化 meeting minutes（Markdown），用繁體中文。\n\n要求：\n1. 會議摘要\n2. 出席者（用真名）\n3. 討論議題（每個議題包括重點內容）\n4. 決議事項\n5. 行動項目（Action Items）\n6. 下次會議安排\n\n全局大綱：\n{parent_summary}\n\n分段摘要：\n{combined}{slide_section}")

    return final


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
        default="output/meeting_minutes.md",
        help="輸出檔案路徑（預設: output/meeting_minutes.md）",
    )
    args = parser.parse_args()

    if not os.path.isfile(args.audio_file):
        print(f"錯誤: 搵唔到檔案 '{args.audio_file}'")
        sys.exit(1)

    if not args.hf_token:
        args.hf_token = "offline"  # 離線模式唔需要真 token

    # 確保 output 目錄存在
    output_dir = os.path.dirname(args.output)
    if output_dir:
        os.makedirs(output_dir, exist_ok=True)

    # Step 1: 轉換音檔
    wav_path = convert_to_wav(args.audio_file)

    try:
        # Step 2-3: 並行跑 whisper + diarization + slide 擷取 + 名字擷取
        print("[*] 並行處理: 語音辨識 + Speaker diarization + Slide 擷取 + 名字擷取...")
        with ThreadPoolExecutor(max_workers=4) as executor:
            future_whisper = executor.submit(transcribe_audio, wav_path)
            future_diarize = executor.submit(diarize_audio, wav_path, args.hf_token)
            future_slides = executor.submit(extract_slides, args.audio_file)
            future_names = executor.submit(extract_participant_names, args.audio_file)

            whisper_segments = future_whisper.result()
            speaker_segments = future_diarize.result()
            slides = future_slides.result()
            participant_names = future_names.result()

        slide_text = ""
        if slides:
            slide_lines = [f"[{format_time(s['timestamp'])}] {s['text']}" for s in slides]
            slide_text = "\n".join(slide_lines)

        # Step 4: 合併
        transcript = merge_transcript(whisper_segments, speaker_segments)

        # 儲存 raw transcript
        transcript_path = args.output.replace(".md", "_transcript.txt")
        with open(transcript_path, "w", encoding="utf-8") as f:
            f.write(transcript)
            if slide_text:
                f.write("\n\n--- Slide 內容 ---\n")
                f.write(slide_text)
        print(f"      → Raw transcript 已儲存: {transcript_path}")

        # Step 5: 生成 meeting minutes
        minutes = generate_minutes(transcript, slide_text, participant_names)

        with open(args.output, "w", encoding="utf-8") as f:
            f.write(minutes)
        print(f"\n完成！Meeting minutes 已儲存: {args.output}")

    finally:
        # 清理臨時 wav 檔
        if os.path.exists(wav_path):
            os.remove(wav_path)


if __name__ == "__main__":
    main()
