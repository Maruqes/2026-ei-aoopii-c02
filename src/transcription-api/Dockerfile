FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

COPY src/transcription-api/requirements.txt /app/src/transcription-api/requirements.txt
ARG PYTORCH_INDEX_URL=https://download.pytorch.org/whl/cpu
RUN pip install --no-cache-dir torch torchvision torchaudio --index-url ${PYTORCH_INDEX_URL}
RUN pip install --no-cache-dir -r /app/src/transcription-api/requirements.txt

COPY src /app/src

EXPOSE 8000

CMD ["python", "-m", "uvicorn", "app.main:app", "--app-dir", "src/transcription-api", "--host", "0.0.0.0", "--port", "8000"]
