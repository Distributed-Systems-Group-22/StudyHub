FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
# Cache dependency installation separately from application source changes.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
# The transcription bridge runs separately and is not bundled into this worker image.
COPY main.py .
COPY src/ ./src/
COPY migrations/ ./migrations/
CMD ["python", "main.py"]
