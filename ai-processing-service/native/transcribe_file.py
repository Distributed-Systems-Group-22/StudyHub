"""One-shot, cross-platform faster-whisper subprocess for the HTTP bridge."""
import json
import os
from pathlib import Path
import sys


def transcribe_audio(audio, model):
    """Use CPU INT8 by default on both macOS and Windows; exhaust lazy segments."""
    from faster_whisper import WhisperModel

    device = os.getenv('AI_TRANSCRIPTION_DEVICE', 'cpu').strip()
    compute_type = os.getenv('AI_TRANSCRIPTION_COMPUTE_TYPE', 'int8').strip()
    threads = int(os.getenv('AI_TRANSCRIPTION_CPU_THREADS', '4'))
    language = os.getenv('AI_TRANSCRIPTION_LANGUAGE', '').strip() or None
    if device not in ('cpu', 'cuda', 'auto') or not 1 <= threads <= 64:
        raise ValueError('Invalid transcription device or CPU thread count')
    if model.startswith('mlx-community/'):
        raise ValueError('MLX checkpoints are incompatible; set AI_TRANSCRIPTION_MODEL=small')

    # Each request owns one decoder. Process exit releases its memory on every OS.
    decoder = WhisperModel(model, device=device, compute_type=compute_type,
                           cpu_threads=threads, num_workers=1)
    segments, _ = decoder.transcribe(audio, beam_size=5, language=language,
                                     task='transcribe', vad_filter=False)
    # Inference happens while iterating, not when transcribe() returns the generator.
    text = ' '.join(segment.text.strip() for segment in segments if segment.text.strip())
    if not text:
        raise ValueError('No speech text was produced')
    return text


def main():
    """Return only transcript text through the private per-request result file."""
    audio, model, output = sys.argv[1:]
    text = transcribe_audio(audio, model)
    Path(output).write_text(json.dumps({'text': text}, ensure_ascii=False), encoding='utf-8')


if __name__ == '__main__':
    main()
