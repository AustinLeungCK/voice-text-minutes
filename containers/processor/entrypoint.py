"""
Processor container entrypoint.
Runs on AWS Batch g4dn.xlarge (T4 16GB GPU + 4 vCPU + 16GB RAM).

Flow:
1. Download MP4 from S3
2. Extract audio (ffmpeg) → WAV
3. Sequential GPU pipeline (each stage clears VRAM before the next):
   a. Whisper STT (faster-whisper, large-v3-turbo)
   b. GOT-OCR2.0 for participant names + slides
   c. pyannote speaker diarization (num_speakers from OCR participant count)
4. Upload results to S3

OCR runs before diarization so the Teams UI participant count can constrain
pyannote's num_speakers, ensuring speaker clusters match the actual attendee list.
All three GPU stages run sequentially; T4 16GB VRAM is sufficient (~3GB per stage).

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

    # Step 3–5: Sequential GPU pipeline (Whisper → OCR → Diarization)
    # OCR runs before diarize so we can pass num_speakers from the Teams UI
    # participant count to pyannote, constraining it to the actual attendee list.
    # Each stage checkpointed to S3 for Spot resume.
    results = {}

    # --- Whisper ---
    results["whisper"] = _run_stage("whisper", lambda: run_whisper(wav_path))

    # --- OCR (names + slides) ---
    results["ocr"] = _run_stage("ocr", lambda: run_ocr(mp4_path))

    # --- Diarization (constrained by OCR participant count) ---
    num_speakers = len(results["ocr"].get("participant_names", []))  if results["ocr"] else None
    if num_speakers and num_speakers >= 2:
        print(f"OCR detected {num_speakers} participants → constraining diarization")
    else:
        num_speakers = None  # let pyannote estimate
    results["diarize"] = _run_stage(
        "diarize", lambda: run_diarize(wav_path, num_speakers=num_speakers)
    )

    print(f"Processor completed for job {JOB_ID}")


def _run_stage(name, fn):
    """Run a pipeline stage with checkpoint check and Spot interruption guard."""
    if _shutting_down.is_set():
        print(f"Interrupted before {name}, exiting.")
        sys.exit(1)

    if _checkpoint_exists(name):
        print(f"Resuming: {name} found in S3, skipping")
        return _download_result(name)

    print(f"Running {name}...")
    result = fn()
    _upload_result(name, result)
    print(f"{name} completed + checkpointed")
    return result


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


def run_diarize(wav_path, num_speakers=None):
    """Run pyannote speaker diarization on GPU.

    Args:
        wav_path: Path to 16kHz mono WAV.
        num_speakers: If set (from OCR participant count), constrains pyannote
            to exactly this many speakers instead of letting it estimate.
    """
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

    diarize_params = {"waveform": waveform_tensor, "sample_rate": sample_rate}
    if num_speakers and num_speakers >= 2:
        print(f"Constraining diarization to {num_speakers} speakers")
        diarization = pipeline(diarize_params, num_speakers=num_speakers)
    else:
        diarization = pipeline(diarize_params)

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


MAX_OCR_FRAMES = 30  # Cap slide OCR to avoid 15+ min processing


def run_ocr(mp4_path):
    """Extract frames and run GOT-OCR2.0 for participant names and slides.

    Optimisations vs naive approach:
    - Higher scene threshold (0.4) to reduce false positives
    - Perceptual dedup via average hash — skip visually identical frames
    - Hard cap at MAX_OCR_FRAMES to bound worst-case runtime
    """
    frames_dir = WORK_DIR / "frames"
    frames_dir.mkdir(exist_ok=True)

    # Extract frames using scene detection (threshold 0.4 — stricter than default)
    subprocess.run(
        [
            "ffmpeg", "-i", str(mp4_path),
            "-vf", "select=gt(scene\\,0.4),showinfo", "-vsync", "vfr", "-q:v", "2",
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

        # Dedup slides by average hash — skip visually identical frames
        slide_files = sorted(frames_dir.glob("slide_*.jpg"))
        unique_slides = _dedup_frames(slide_files)
        if len(unique_slides) < len(slide_files):
            print(f"Dedup: {len(slide_files)} → {len(unique_slides)} unique frames")

        # Cap to avoid unbounded OCR time
        if len(unique_slides) > MAX_OCR_FRAMES:
            print(f"Capping OCR from {len(unique_slides)} to {MAX_OCR_FRAMES} frames")
            unique_slides = unique_slides[:MAX_OCR_FRAMES]

        for i, slide_path in enumerate(unique_slides):
            if _shutting_down.is_set():
                print(f"Interrupted during OCR at slide {i}/{len(unique_slides)}, saving partial results")
                break
            print(f"OCR slide {i+1}/{len(unique_slides)}...")
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

    return result


def _dedup_frames(paths, hash_size=16, threshold=12):
    """Remove near-duplicate frames using average hash (PIL only, no extra deps).

    Uses 16x16 hash (256 bits) with hamming threshold 12 (~5%).
    At this resolution, Excel scroll/cell-select changes blur away while
    genuine slide transitions (different layout/colours) remain distinct.
    """
    from PIL import Image

    def _avg_hash(img_path):
        img = Image.open(str(img_path)).convert("L").resize((hash_size, hash_size))
        pixels = list(img.getdata())
        avg = sum(pixels) / len(pixels)
        return sum(1 << i for i, p in enumerate(pixels) if p > avg)

    def _hamming(a, b):
        return bin(a ^ b).count("1")

    seen_hashes = []
    unique = []
    for p in paths:
        h = _avg_hash(p)
        if all(_hamming(h, s) > threshold for s in seen_hashes):
            seen_hashes.append(h)
            unique.append(p)
    return unique


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
