"""
Processor container entrypoint.
Runs on AWS Batch g4dn.xlarge (T4 16GB GPU + 4 vCPU + 16GB RAM).

Flow:
1. Download MP4 from S3
2. Extract audio (ffmpeg) → WAV
3. Sequential GPU pipeline (each stage clears VRAM before the next):
   a. Whisper STT (faster-whisper, large-v3-turbo)
   b. pyannote speaker diarization
   c. GOT-OCR2.0 for participant names + slides
4. Upload results to S3

All three GPU stages run sequentially rather than in parallel because
diarization on CPU takes 25-40 min for a 1-hour meeting, while the full
sequential GPU pipeline (Whisper → diarize → OCR) completes in ~12 min.
T4 16GB VRAM is sufficient (peak ~3GB per stage).

Spot handling:
- SIGTERM → upload whatever results are done, then exit
- Checkpoint each stage to S3 immediately after completion
- On restart, skip stages that already have results in S3
"""

import json
import os
import signal
import subprocess
import sys
import tempfile
import threading
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

# Spot interruption flag
_shutting_down = threading.Event()


def _sigterm_handler(signum, frame):
    """Handle SIGTERM from Spot interruption (2-min warning)."""
    print("SIGTERM received — Spot interruption. Uploading partial results...", flush=True)
    _shutting_down.set()


signal.signal(signal.SIGTERM, _sigterm_handler)


def _checkpoint_exists(name):
    """Check if a stage result already exists in S3 (from a previous attempt)."""
    try:
        s3_client.head_object(Bucket=S3_BUCKET, Key=f"{S3_PREFIX}/{name}_result.json")
        return True
    except s3_client.exceptions.ClientError as e:
        if e.response["Error"]["Code"] == "404":
            return False
        raise


def _upload_result(name, data):
    """Upload a stage result to S3 as checkpoint."""
    key = f"{S3_PREFIX}/{name}_result.json"
    s3_client.put_object(
        Bucket=S3_BUCKET,
        Key=key,
        Body=json.dumps(data, ensure_ascii=False).encode("utf-8"),
        ContentType="application/json",
    )
    print(f"Uploaded {key}")


def _download_result(name):
    """Download a previously checkpointed result from S3."""
    key = f"{S3_PREFIX}/{name}_result.json"
    resp = s3_client.get_object(Bucket=S3_BUCKET, Key=key)
    return json.loads(resp["Body"].read().decode("utf-8"))


def main():
    print(f"Starting processor for job {JOB_ID}")

    # Step 1: Download MP4
    mp4_path = WORK_DIR / "input.mp4"
    print("Downloading MP4 from S3...")
    s3_client.download_file(S3_BUCKET, f"{S3_PREFIX}/input.mp4", str(mp4_path))
    print(f"Downloaded {mp4_path.stat().st_size / 1e6:.1f} MB")

    if _shutting_down.is_set():
        print("Interrupted after download, exiting.")
        sys.exit(1)

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

    # Step 3–5: Sequential GPU pipeline (Whisper → Diarization → OCR)
    # Each stage checkpointed to S3 for Spot resume
    results = {}

    stages = [
        ("whisper", lambda: run_whisper(wav_path)),
        ("diarize", lambda: run_diarize(wav_path)),
        ("ocr", lambda: run_ocr(mp4_path)),
    ]

    for name, fn in stages:
        if _shutting_down.is_set():
            print(f"Interrupted before {name}, exiting.")
            sys.exit(1)

        if _checkpoint_exists(name):
            print(f"Resuming: {name} found in S3, skipping")
            results[name] = _download_result(name)
            continue

        print(f"Running {name}...")
        results[name] = fn()
        _upload_result(name, results[name])
        print(f"{name} completed + checkpointed")

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
    """Run pyannote speaker diarization on GPU."""
    import soundfile as sf
    import torch
    from pyannote.audio import Pipeline

    hf_token = os.environ.get("HF_TOKEN")
    pipeline = Pipeline.from_pretrained(
        "pyannote/speaker-diarization-3.1",
        use_auth_token=hf_token,
    )
    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    pipeline.to(device)
    print(f"Diarization running on {device}")

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

    del pipeline
    _clear_gpu()

    return {"segments": segments}


def run_ocr(mp4_path):
    """Extract frames and run GOT-OCR2.0 for participant names and slides."""
    frames_dir = WORK_DIR / "frames"
    frames_dir.mkdir(exist_ok=True)

    # Extract frames using scene detection — only when the screen changes significantly
    # (e.g., slide transitions, window switches). Much faster than fixed fps for typical
    # meetings: 10-20 scene changes vs 120 fixed-interval frames for a 1-hour meeting.
    subprocess.run(
        [
            "ffmpeg", "-i", str(mp4_path),
            "-vf", "select=gt(scene\\,0.3),showinfo", "-vsync", "vfr", "-q:v", "2",
            str(frames_dir / "slide_%04d.jpg"), "-y",
        ],
        check=True,
        capture_output=True,
    )
    slide_count = len(list(frames_dir.glob("slide_*.jpg")))
    print(f"Scene detection extracted {slide_count} frames")

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
        import torch
        from PIL import Image
        from transformers import AutoModel, AutoProcessor

        processor = AutoProcessor.from_pretrained(
            "stepfun-ai/GOT-OCR-2.0-hf", trust_remote_code=True
        )
        model = AutoModel.from_pretrained(
            "stepfun-ai/GOT-OCR-2.0-hf", trust_remote_code=True,
            torch_dtype=torch.float16,
        ).to("cuda")

        def _ocr_image(image_path):
            image = Image.open(str(image_path)).convert("RGB")
            inputs = processor(image, return_tensors="pt").to("cuda", torch.float16)
            generated_ids = model.generate(**inputs, max_new_tokens=1024)
            return processor.decode(generated_ids[0], skip_special_tokens=True)

        # OCR participant names from first frame
        names_img = frames_dir / "names.jpg"
        if names_img.exists():
            names_text = _ocr_image(names_img)
            names = [n.strip() for n in names_text.split("\n") if n.strip()]
            result["participant_names"] = names
            print(f"OCR found {len(names)} participant names")

        # OCR slides (scene-detected frames)
        slide_files = sorted(frames_dir.glob("slide_*.jpg"))
        for i, slide_path in enumerate(slide_files):
            if _shutting_down.is_set():
                print(f"Interrupted during OCR at slide {i}/{len(slide_files)}, saving partial results")
                break
            print(f"OCR slide {i+1}/{len(slide_files)}...")
            slide_text = _ocr_image(slide_path)
            if slide_text.strip():
                result["slide_contents"].append({
                    "slide_index": i,
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
