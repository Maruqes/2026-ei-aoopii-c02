FROM node:22-bookworm-slim AS youtube-js

FROM golang:1.22-bookworm AS build

WORKDIR /src

RUN apt-get update \
    && apt-get install -y --no-install-recommends libopus-dev libopusfile-dev pkg-config \
    && rm -rf /var/lib/apt/lists/*

COPY discord_bot/go.mod discord_bot/go.sum ./
COPY discord_bot/third_party/discordgo ./third_party/discordgo
RUN go mod download

COPY discord_bot ./
RUN CGO_ENABLED=1 go build -o /out/discord-bot .

FROM debian:bookworm-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
       ca-certificates libopus0 libopusfile0 ffmpeg python3 python3-venv libstdc++6 \
    && rm -rf /var/lib/apt/lists/*

COPY --from=youtube-js /usr/local/bin/node /usr/local/bin/node
COPY discord_bot/music-requirements.txt /tmp/music-requirements.txt
RUN python3 -m venv /opt/youtube \
    && /opt/youtube/bin/pip install --no-cache-dir -r /tmp/music-requirements.txt \
    && /opt/youtube/bin/pip install --no-cache-dir piper-tts==1.8.0 \
    && rm /tmp/music-requirements.txt

ADD https://huggingface.co/rhasspy/piper-voices/resolve/v1.0.0/pt/pt_PT/tug%C3%A3o/medium/pt_PT-tug%C3%A3o-medium.onnx /opt/piper/pt_PT-tugao-medium.onnx
ADD https://huggingface.co/rhasspy/piper-voices/resolve/v1.0.0/pt/pt_PT/tug%C3%A3o/medium/pt_PT-tug%C3%A3o-medium.onnx.json /opt/piper/pt_PT-tugao-medium.onnx.json

ENV PATH="/opt/youtube/bin:${PATH}"

WORKDIR /app

COPY --from=build /out/discord-bot /usr/local/bin/discord-bot

RUN mkdir -p /app/recordings

CMD ["discord-bot"]
