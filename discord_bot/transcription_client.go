package main

import (
	"context"
	"fmt"
	"io"
	"log"
	"net/http"
	"net/url"
	"os"
	"path/filepath"
	"strings"
	"time"
)

const (
	defaultTranscriptionAPIURL     = "http://localhost:8000"
	defaultTranscriptionAPITimeout = 10 * time.Minute
)

type TranscriptionClient struct {
	endpoint   string
	httpClient *http.Client
}

type TranscriptionRequest struct {
	AudioPath          string
	DiscordID          string
	Username           string
	DisplayName        string
	ChannelName        string
	RecordingStartedAt time.Time
}

func NewTranscriptionClientFromEnv() *TranscriptionClient {
	client := &TranscriptionClient{
		endpoint: transcriptionEndpointFromEnv(os.Getenv("TRANSCRIPTION_API_URL")),
		httpClient: &http.Client{
			Timeout: transcriptionTimeoutFromEnv(os.Getenv("TRANSCRIPTION_API_TIMEOUT")),
		},
	}
	log.Printf("cliente API transcrição configurado endpoint=%s timeout=%s", client.endpoint, client.httpClient.Timeout)
	return client
}

func transcriptionEndpointFromEnv(raw string) string {
	endpoint := strings.TrimSpace(raw)
	if endpoint == "" {
		endpoint = defaultTranscriptionAPIURL
	}

	endpoint = strings.TrimRight(endpoint, "/")
	if strings.HasSuffix(endpoint, "/v1/transcriptions") {
		return endpoint
	}
	return endpoint + "/v1/transcriptions"
}

func transcriptionTimeoutFromEnv(raw string) time.Duration {
	value := strings.TrimSpace(raw)
	if value == "" {
		return defaultTranscriptionAPITimeout
	}

	timeout, err := time.ParseDuration(value)
	if err != nil {
		log.Printf("TRANSCRIPTION_API_TIMEOUT inválido (%q), a usar %s", value, defaultTranscriptionAPITimeout)
		return defaultTranscriptionAPITimeout
	}
	return timeout
}

func (c *TranscriptionClient) QueueTranscription(request TranscriptionRequest) {
	if c == nil {
		log.Printf("cliente API transcrição nil; pedido ignorado user=%s file=%s", request.DiscordID, request.AudioPath)
		return
	}

	request = request.withFallbacks()
	log.Printf(
		"chamada API transcrição enfileirada user=%s username=%s channel=%s file=%s started_at=%s",
		request.DiscordID,
		request.Username,
		request.ChannelName,
		request.AudioPath,
		request.RecordingStartedAt.UTC().Format(time.RFC3339Nano),
	)

	go func() {
		if err := c.CreateTranscription(context.Background(), request); err != nil {
			log.Printf("erro ao chamar API de transcrição para user=%s file=%s: %v", request.DiscordID, request.AudioPath, err)
			return
		}
		log.Printf("pedido de transcrição aceite pela API para user=%s file=%s", request.DiscordID, request.AudioPath)
	}()
}

func (c *TranscriptionClient) CreateTranscription(ctx context.Context, request TranscriptionRequest) error {
	if c == nil || c.httpClient == nil {
		return fmt.Errorf("cliente de transcrição não configurado")
	}

	request = request.withFallbacks()
	started := time.Now()
	audioSize := int64(-1)
	if stat, err := os.Stat(request.AudioPath); err != nil {
		log.Printf("não foi possível obter tamanho do áudio antes da API user=%s file=%s: %v", request.DiscordID, request.AudioPath, err)
	} else {
		audioSize = stat.Size()
	}

	log.Printf(
		"chamada API transcrição início endpoint=%s user=%s username=%s channel=%s file=%s recording_filename=%s bytes=%d",
		c.endpoint,
		request.DiscordID,
		request.Username,
		request.ChannelName,
		request.AudioPath,
		filepath.Base(request.AudioPath),
		audioSize,
	)

	form := url.Values{}
	form.Set("recording_filename", filepath.Base(request.AudioPath))
	form.Set("discord_id", request.DiscordID)
	form.Set("username", request.Username)
	form.Set("channel_name", request.ChannelName)
	form.Set("recording_started_at", request.RecordingStartedAt.UTC().Format(time.RFC3339Nano))
	if request.DisplayName != "" {
		form.Set("display_name", request.DisplayName)
	}

	httpRequest, err := http.NewRequestWithContext(ctx, http.MethodPost, c.endpoint, strings.NewReader(form.Encode()))
	if err != nil {
		return err
	}
	httpRequest.Header.Set("Content-Type", "application/x-www-form-urlencoded")

	response, err := c.httpClient.Do(httpRequest)
	if err != nil {
		log.Printf("chamada API transcrição erro user=%s file=%s elapsed=%s err=%v", request.DiscordID, request.AudioPath, time.Since(started).Round(time.Millisecond), err)
		return err
	}
	defer response.Body.Close()

	if response.StatusCode >= http.StatusOK && response.StatusCode < http.StatusMultipleChoices {
		_, _ = io.Copy(io.Discard, response.Body)
		log.Printf("chamada API transcrição sucesso user=%s file=%s status=%s elapsed=%s", request.DiscordID, request.AudioPath, response.Status, time.Since(started).Round(time.Millisecond))
		return nil
	}

	responseBody, _ := io.ReadAll(io.LimitReader(response.Body, 4096))
	detail := strings.TrimSpace(string(responseBody))
	if detail == "" {
		detail = response.Status
	}
	log.Printf("chamada API transcrição falhou user=%s file=%s status=%s elapsed=%s body=%s", request.DiscordID, request.AudioPath, response.Status, time.Since(started).Round(time.Millisecond), detail)
	return fmt.Errorf("API devolveu %s: %s", response.Status, detail)
}

func (r TranscriptionRequest) withFallbacks() TranscriptionRequest {
	r.AudioPath = strings.TrimSpace(r.AudioPath)
	r.DiscordID = strings.TrimSpace(r.DiscordID)
	r.Username = strings.TrimSpace(r.Username)
	r.DisplayName = strings.TrimSpace(r.DisplayName)
	r.ChannelName = strings.TrimSpace(r.ChannelName)

	if r.Username == "" {
		r.Username = r.DiscordID
	}
	if r.ChannelName == "" {
		r.ChannelName = "voice"
	}
	if r.RecordingStartedAt.IsZero() {
		r.RecordingStartedAt = time.Now().UTC()
	}

	return r
}
