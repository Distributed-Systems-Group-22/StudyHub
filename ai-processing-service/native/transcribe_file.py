"""One-shot MLX subprocess; invoked by transcription_server.py only."""
import json
from pathlib import Path
import sys


def main():
    import mlx_whisper
    audio, model, output = sys.argv[1:]
    result = mlx_whisper.transcribe(audio, path_or_hf_repo=model)
    Path(output).write_text(json.dumps({'text': result['text']}, ensure_ascii=False))


if __name__ == '__main__':
    main()
