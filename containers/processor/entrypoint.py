"""
Processor container entrypoint.
Runs on AWS Batch g4dn.xlarge (T4 16GB GPU + 4 vCPU + 16GB RAM).

Flow:
1. Download MP4 from S3
2. Extract audio (ffmpeg) → WAV
3. Parallel:
   - Thread A (GPU): Whisper STT
   - Thread B (CPU): pyannote diarization
4. Sequential (GPU): GOT-OCR2.0 for participant names + slides
5. Upload results to S3
"""

import json
import os
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

# Models: 優先用 host AMI 預裝嘅，fallback 到 default cache（fat container 模式會下載）
if os.path.isdir("/opt/processor/models"):
    os.environ.setdefault("HF_HOME", "/opt/processor/models")
else:
    os.environ.setdefault("HF_HOME", "/tmp/models")

import boto3

s3_client = boto3.client("s3")

JOB_ID = os.environ["JOB_ID"]
S3_BUCKET = os.environ["S3_BUCKET"]
S3_PREFIX = f"jobs/{JOB_ID}"
WORK_DIR = Path(tempfile.mkdtemp())


def main():
    print(f"Starting processor for job {JOB_ID}")

    # Step 1: Download MP4
    mp4_path = WORK_DIR / "input.mp4"
    print("Downloading MP4 from S3...")
    s3_client.download_file(S3_BUCKET, f"{S3_PREFIX}/input.mp4", str(mp4_path))
    print(f"Downloaded {mp4_path.stat().st_size / 1e6:.1f} MB")

    # Step 2: Extract audio
    wav_path = WORK_DIR / "audio.wav"
    print("Extracting audio...")
    subprocess.run(
        [
            "ffmpeg", "-i", str(mp4_path),
            "-vn", "-acodec", "pcm_s16le", "-ar", "16000", "-ac", "1",
            str(wav_path), "-y",
        ],
        check=True,
        capture_output=True,
    )
    print(f"Audio extracted: {wav_path.stat().st_size / 1e6:.1f} MB")

    # Upload WAV for reference
    s3_client.upload_file(str(wav_path), S3_BUCKET, f"{S3_PREFIX}/audio.wav")

    # Step 3: Parallel — Whisper (GPU) + Diarization (CPU)
    results = {}
    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = {
            executor.submit(run_whisper, wav_path): "whisper",
            executor.submit(run_diarize, wav_path): "diarize",
        }
        for future in as_completed(futures):
            name = futures[future]
            try:
                results[name] = future.result()
                print(f"{name} completed")
            except Exception as e:
                print(f"{name} failed: {e}", file=sys.stderr)
                raise

    # Step 4: OCR (GPU) — after Whisper releases GPU memory
    print("Running OCR...")
    results["ocr"] = run_ocr(mp4_path)
    print("OCR completed")

    # Step 5: Upload results to S3
    for name, data in results.items():
        key = f"{S3_PREFIX}/{name}_result.json"
        s3_client.put_object(
            Bucket=S3_BUCKET,
            Key=key,
            Body=json.dumps(data, ensure_ascii=False).encode("utf-8"),
            ContentType="application/json",
        )
        print(f"Uploaded {key}")

    print(f"Processor completed for job {JOB_ID}")


def run_whisper(wav_path):
    """Run faster-whisper STT on GPU."""
    from faster_whisper import WhisperModel

    model = WhisperModel("large-v3-turbo", device="cuda", compute_type="float16")
    segments, info = model.transcribe(
        str(wav_path),
        language="yue",
        beam_size=5,
        vad_filter=True,
    )

    result_segments = []
    for seg in segments:
        result_segments.append({
            "start": round(seg.start, 2),
            "end": round(seg.end, 2),
            "text": seg.text,
        })

    # Free GPU memory
    del model
    _clear_gpu()

    return {
        "language": info.language,
        "duration": round(info.duration, 2),
        "segments": result_segments,
    }


def run_diarize(wav_path):
    """Run pyannote speaker diarization on CPU."""
    import soundfile as sf
    import torch
    from pyannote.audio import Pipeline

    hf_token = os.environ.get("HF_TOKEN")
    pipeline = Pipeline.from_pretrained(
        "pyannote/speaker-diarization-3.1",
        use_auth_token=hf_token,
    )
    # Force CPU
    pipeline.to(torch.device("cpu"))

    waveform, sample_rate = sf.read(str(wav_path), dtype="float32")
    waveform_tensor = torch.tensor(waveform).unsqueeze(0)

    diarization = pipeline(
        {"waveform": waveform_tensor, "sample_rate": sample_rate}
    )

    segments = []
    for turn, _, speaker in diarization.itertracks(yield_label=True):
        segments.append({
            "start": round(turn.start, 2),
            "end": round(turn.end, 2),
            "speaker": speaker,
        })

    return {"segments": segments}


def run_ocr(mp4_path):
    """Extract frames and run GOT-OCR2.0 for participant names and slides."""
    frames_dir = WORK_DIR / "frames"
    frames_dir.mkdir(exist_ok=True)

    # Extract frames: 1 frame every 30 seconds for slides, first frame for names
    subprocess.run(
        [
            "ffmpeg", "-i", str(mp4_path),
            "-vf", "fps=1/30", "-q:v", "2",
            str(frames_dir / "slide_%04d.jpg"), "-y",
        ],
        check=True,
        capture_output=True,
    )

    # Extract first frame for participant names (Teams UI)
    subprocess.run(
        [
            "ffmpeg", "-i", str(mp4_path),
            "-vframes", "1", "-q:v", "2",
            str(frames_dir / "names.jpg"), "-y",
        ],
        check=True,
        capture_output=True,
    )

    result = {"participant_names": [], "slide_contents": []}

    try:
        from transformers import AutoModel, AutoTokenizer

        tokenizer = AutoTokenizer.from_pretrained(
            "stepfun-ai/GOT-OCR-2.0-hf", trust_remote_code=True
        )
        model = AutoModel.from_pretrained(
            "stepfun-ai/GOT-OCR-2.0-hf", trust_remote_code=True,
            device_map="cuda",
        )

        # OCR participant names from first frame
        names_img = frames_dir / "names.jpg"
        if names_img.exists():
            names_text = model.chat(tokenizer, str(names_img), ocr_type="ocr")
            # Parse names from OCR output
            names = [n.strip() for n in names_text.split("\n") if n.strip()]
            result["participant_names"] = names

        # OCR slides
        slide_files = sorted(frames_dir.glob("slide_*.jpg"))
        for i, slide_path in enumerate(slide_files):
            slide_text = model.chat(tokenizer, str(slide_path), ocr_type="ocr")
            if slide_text.strip():
                result["slide_contents"].append({
                    "timestamp": i * 30,
                    "text": slide_text.strip(),
                })

        del model
        _clear_gpu()

    except Exception as e:
        print(f"OCR warning: {e}", file=sys.stderr)
        # Return empty OCR results — pipeline continues without OCR

    return result


def _clear_gpu():
    """Free GPU memory."""
    try:
        import torch
        if torch.cuda.is_available():
            torch.cuda.empty_cache()
    except ImportError:
        pass


if __name__ == "__main__":
    main()
