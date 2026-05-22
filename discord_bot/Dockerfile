FROM golang:1.22-bookworm AS build

WORKDIR /src

RUN apt-get update \
    && apt-get install -y --no-install-recommends libopus-dev libopusfile-dev pkg-config \
    && rm -rf /var/lib/apt/lists/*

COPY discord_bot/go.mod discord_bot/go.sum ./
RUN go mod download

COPY discord_bot ./
RUN CGO_ENABLED=1 go build -o /out/discord-bot .

FROM debian:bookworm-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates libopus0 libopusfile0 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY --from=build /out/discord-bot /usr/local/bin/discord-bot

RUN mkdir -p /app/recordings

CMD ["discord-bot"]
