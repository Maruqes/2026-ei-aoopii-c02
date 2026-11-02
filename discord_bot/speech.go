package main

import (
	"bufio"
	"bytes"
	"context"
	_ "embed"
	"errors"
	"fmt"
	"io"
	"log"
	"os"
	"regexp"
	"strings"
	"time"

	"gopkg.in/hraban/opus.v2"
)

//go:embed speech_synthesis.py
var speechSynthesis string

const defaultSpeechVoice = "pt-PT-RaquelNeural"

var (
	speechLink       = regexp.MustCompile(`\[([^\]]+)\]\([^)]+\)`)
	speechURL        = regexp.MustCompile(`https?://\S+`)
	speechHTML       = regexp.MustCompile(`<[^>]+>`)
	speechLinePrefix = regexp.MustCompile(`(?m)^\s*(?:#{1,6}\s+|[-*+]\s+|>\s*)`)
	speechEmphasis   = strings.NewReplacer("**", "", "__", "", "~~", "", "`", "")
)

// speechProvider selects the neural Edge voice by default, with local Piper as fallback.
func speechProvider() string {
	if strings.EqualFold(strings.TrimSpace(os.Getenv("ASSISTANT_TTS_PROVIDER")), "piper") {
		return "piper"
	}
	return "edge"
}

func speechVoice() string {
	if voice := strings.TrimSpace(os.Getenv("ASSISTANT_VOICE")); voice != "" {
		return voice
	}
	return defaultSpeechVoice
}

func speechVoiceModel() string {
	if model := strings.TrimSpace(os.Getenv("ASSISTANT_VOICE_MODEL")); model != "" {
		return model
	}
	return "/opt/piper/pt_PT-tugao-medium.onnx"
}

func speechText(text string) string {
	text = speechLink.ReplaceAllString(text, "$1")
	text = speechHTML.ReplaceAllString(text, " ")
	text = speechURL.ReplaceAllString(text, "ligação")
	text = speechLinePrefix.ReplaceAllString(text, "")
	text = speechEmphasis.Replace(text)
	text = strings.Join(strings.Fields(text), " ")
	if runes := []rune(text); len(runes) > 2000 {
		text = string(runes[:2000]) + ". A resposta completa está no chat."
	}
	return text
}

func speechDiagnostics(stderr string) string {
	stderr = strings.TrimSpace(stderr)
	if stderr == "" {
		return "no diagnostics"
	}
	if runes := []rune(stderr); len(runes) > 500 {
		stderr = string(runes[len(runes)-500:])
	}
	return stderr
}

// Speak holds the shared Discord output while music retains its decoder and position.
func (p *MusicPlayer) Speak(ctx context.Context, text string) error {
	return p.speak(ctx, text, nil)
}

func (p *MusicPlayer) speak(ctx context.Context, text string, ready func() error) error {
	if p == nil || p.vc == nil || p.vc.OpusSend == nil {
		return errors.New("Discord voice connection is unavailable")
	}
	p.mu.Lock()
	if p.closed {
		p.mu.Unlock()
		return ErrMusicClosed
	}
	p.voicing++
	p.mu.Unlock()
	defer func() { p.mu.Lock(); p.voicing--; p.mu.Unlock() }()
	ctx, cancel := context.WithCancel(ctx)
	defer cancel()
	stopPlayer := context.AfterFunc(p.ctx, cancel)
	defer stopPlayer()
	text = speechText(text)
	if text == "" {
		return nil
	}
	// Python streams 22.05 kHz mono PCM; ffmpeg converts it to Discord's 48 kHz stereo.
	speakStarted := time.Now()
	streamCtx, stop := context.WithCancelCause(ctx)
	defer stop(nil)
	provider, voice, model := speechProvider(), speechVoice(), speechVoiceModel()
	var synthErr bytes.Buffer
	synth := musicCommand(streamCtx, "python3", "-c", speechSynthesis, provider, voice, model)
	synth.Stdin, synth.Stderr = strings.NewReader(text), &synthErr
	audio, err := synth.StdoutPipe()
	if err != nil {
		return err
	}
	if err = synth.Start(); err != nil {
		return fmt.Errorf("could not start Portuguese speech with %s: %w", provider, err)
	}
	synthStarted := time.Now()
	defer func() {
		stop(nil)
		_ = audio.Close()
		_ = synth.Wait()
		if diagnostics := strings.TrimSpace(synthErr.String()); diagnostics != "" {
			log.Printf("speech synthesis provider=%s: %s", provider, diagnostics)
		}
	}()
	cmd := musicCommand(streamCtx, "ffmpeg", "-hide_banner", "-loglevel", "error",
		"-f", "s16le", "-ar", "22050", "-ac", "1", "-i", "pipe:0",
		"-f", "s16le", "-ar", "48000", "-ac", "2", "pipe:1")
	cmd.Stdin, cmd.Stderr = audio, io.Discard
	pcm, err := cmd.StdoutPipe()
	if err != nil {
		return err
	}
	if err = cmd.Start(); err != nil {
		return err
	}
	defer func() { stop(nil); _ = pcm.Close(); _ = cmd.Wait() }()
	buffered := bufio.NewReader(pcm)
	if _, err = buffered.Peek(defaultOpusFrameSamples * channels * 2); err != nil && err != io.EOF {
		return err
	}
	if buffered.Buffered() == 0 {
		if err = synth.Wait(); err != nil {
			return fmt.Errorf("could not synthesize Portuguese speech with %s: %w (%s)", provider, err, speechDiagnostics(synthErr.String()))
		}
		return cmd.Wait()
	}
	// First decoded frame: the audible start, after Edge, the retry hedge and ffmpeg.
	// First decoded frame: the audible start, after Edge, the retry hedge and ffmpeg.
	log.Printf("speech first audio provider=%s latency_ms=%d", provider, time.Since(synthStarted).Milliseconds())
	encoder, err := opus.NewEncoder(sampleRate, channels, opus.AppVoIP)
	if err != nil {
		return err
	}
	if err = p.acquireOutput(streamCtx); err != nil {
		return err
	}
	defer p.releaseOutput()
	if ready != nil {
		if err := ready(); err != nil {
			return err
		}
	}
	log.Printf("speech ready provider=%s total_ms=%d output_and_publish_ms=%d", provider, time.Since(speakStarted).Milliseconds(), time.Since(synthStarted).Milliseconds())
	if err = p.voiceStatus(true); err != nil {
		return err
	}
	defer func() { _ = p.voiceStatus(false); p.resumeMusic = true }()
	if err = p.sendAudioPCM(streamCtx, stop, buffered, encoder, false); err != nil {
		return err
	}
	if err = cmd.Wait(); err != nil {
		return err
	}
	if err = synth.Wait(); err != nil {
		return fmt.Errorf("could not synthesize Portuguese speech with %s: %w (%s)", provider, err, speechDiagnostics(synthErr.String()))
	}
	return nil
}
