#!/usr/bin/env python3
"""簡單轉錄：WhatsApp 錄音 → transcript text file"""

import os
import sys
import io
import tempfile

os.environ["HF_HUB_OFFLINE"] = "1"
os.environ["TRANSFORMERS_OFFLINE"] = "1"

if hasattr(sys.stdout, "buffer") and not isinstance(sys.stdout, io.TextIOWrapper):
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "buffer") and not isinstance(sys.stderr, io.TextIOWrapper):
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

import av
from faster_whisper import WhisperModel


def convert_to_wav(input_path):
    print(f"[1/2] 轉換音檔: {input_path}")
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
    return wav_path


def transcribe_audio(wav_path):
    print("[2/2] 語音辨識中 (faster-whisper large-v3-turbo)...")
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
        start = seg.start
        m, s = int(start // 60), int(start % 60)
        results.append(f"[{m:02d}:{s:02d}] {seg.text.strip()}")
    return results


if __name__ == "__main__":
    input_path = sys.argv[1]
    output_path = sys.argv[2] if len(sys.argv) > 2 else "output/transcript.txt"

    os.makedirs(os.path.dirname(output_path), exist_ok=True)

    wav_path = convert_to_wav(input_path)
    try:
        lines = transcribe_audio(wav_path)
        transcript = "\n".join(lines)
        with open(output_path, "w", encoding="utf-8") as f:
            f.write(transcript)
        print(f"\n完成！Transcript 已儲存: {output_path}")
        print(f"共 {len(lines)} 段")
    finally:
        if os.path.exists(wav_path):
            os.remove(wav_path)
