import json
import os

import boto3

s3_client = boto3.client("s3")


def lambda_handler(event, context):
    job_id = event["job_id"]
    bucket = event["s3_bucket"]
    prefix = f"jobs/{job_id}"

    whisper = _load_json(bucket, f"{prefix}/whisper_result.json")
    diarize = _load_json(bucket, f"{prefix}/diarize_result.json")
    ocr = _load_json(bucket, f"{prefix}/ocr_result.json")

    participant_names = ocr.get("participant_names", [])
    slide_contents = ocr.get("slide_contents", [])

    # Build speaker label map from diarization
    speaker_segments = diarize.get("segments", [])

    # Merge whisper segments with speaker labels
    merged_lines = []

    for seg in whisper.get("segments", []):
        start = seg["start"]
        end = seg["end"]
        text = seg["text"].strip()

        if not text:
            continue

        speaker = _find_speaker(start, end, speaker_segments)
        speaker_name = _resolve_speaker_name(speaker, participant_names)
        timestamp = _format_time(start)

        merged_lines.append(f"[{timestamp}] {speaker_name}: {text}")

    # Add slide context if available
    transcript_parts = []

    if slide_contents:
        transcript_parts.append("=== SLIDE CONTENTS ===")
        for slide in slide_contents:
            transcript_parts.append(
                f"[Slide at {_format_time(slide.get('timestamp', 0))}]"
            )
            transcript_parts.append(slide.get("text", ""))
            transcript_parts.append("")

    if participant_names:
        transcript_parts.append(f"=== PARTICIPANTS: {', '.join(participant_names)} ===")
        transcript_parts.append("")

    transcript_parts.append("=== TRANSCRIPT ===")
    transcript_parts.extend(merged_lines)

    merged_text = "\n".join(transcript_parts)

    s3_client.put_object(
        Bucket=bucket,
        Key=f"{prefix}/merged_transcript.txt",
        Body=merged_text.encode("utf-8"),
        ContentType="text/plain; charset=utf-8",
    )

    return {
        "status": "merged",
        "lines": len(merged_lines),
        "s3_key": f"{prefix}/merged_transcript.txt",
    }


def _load_json(bucket, key):
    try:
        resp = s3_client.get_object(Bucket=bucket, Key=key)
        return json.loads(resp["Body"].read().decode("utf-8"))
    except s3_client.exceptions.NoSuchKey:
        print(f"Warning: {key} not found, returning empty")
        return {}


def _find_speaker(start, end, speaker_segments):
    mid = (start + end) / 2
    for seg in speaker_segments:
        if seg["start"] <= mid <= seg["end"]:
            return seg.get("speaker", "UNKNOWN")
    return "UNKNOWN"


def _resolve_speaker_name(speaker_label, participant_names):
    if not participant_names or speaker_label == "UNKNOWN":
        return speaker_label
    # Map SPEAKER_00 → index 0, etc.
    try:
        idx = int(speaker_label.replace("SPEAKER_", ""))
        if idx < len(participant_names):
            return participant_names[idx]
    except (ValueError, IndexError):
        pass
    return speaker_label


def _format_time(seconds):
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    if h > 0:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"
