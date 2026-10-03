package main

import (
	"bytes"
	"context"
	"errors"
	"fmt"
	"io"
	"os"
	"strings"

	"gopkg.in/hraban/opus.v2"
)

func speechVoiceModel() string {
	if model := strings.TrimSpace(os.Getenv("ASSISTANT_VOICE_MODEL")); model != "" {
		return model
	}
	return "/opt/piper/pt_PT-tugao-medium.onnx"
}

func speechText(text string) string {
	text = strings.Join(strings.Fields(strings.NewReplacer("**", "", "`", "").Replace(text)), " ")
	if runes := []rune(text); len(runes) > 2000 {
		text = string(runes[:2000]) + ". A resposta completa está no chat."
	}
	return text
}

// Speak holds the shared Discord output while music retains its decoder and position.
func (p *MusicPlayer) Speak(ctx context.Context, text string) error {
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
	// ponytail: buffer one bounded local reply; stream synthesis if longer replies are needed.
	// Tugão produces 22.05 kHz mono PCM. Longer phonemes slow speech without changing pitch.
	synth := musicCommand(ctx, "piper", "--model", speechVoiceModel(),
		"--length-scale", "1.3", "--sentence-silence", "0.4", "--output-raw")
	synth.Stdin, synth.Stderr = strings.NewReader(text), io.Discard
	audio, err := synth.Output()
	if err != nil {
		return fmt.Errorf("could not synthesize Portuguese speech with Piper: %w", err)
	}
	streamCtx, stop := context.WithCancelCause(ctx)
	defer stop(nil)
	cmd := musicCommand(streamCtx, "ffmpeg", "-hide_banner", "-loglevel", "error",
		"-f", "s16le", "-ar", "22050", "-ac", "1", "-i", "pipe:0",
		"-f", "s16le", "-ar", "48000", "-ac", "2", "pipe:1")
	cmd.Stdin, cmd.Stderr = bytes.NewReader(audio), io.Discard
	pcm, err := cmd.StdoutPipe()
	if err != nil {
		return err
	}
	if err = cmd.Start(); err != nil {
		return err
	}
	defer func() { stop(nil); _ = pcm.Close(); _ = cmd.Wait() }()
	encoder, err := opus.NewEncoder(sampleRate, channels, opus.AppVoIP)
	if err != nil {
		return err
	}
	if err = p.acquireOutput(streamCtx); err != nil {
		return err
	}
	defer p.releaseOutput()
	if err = p.voiceStatus(true); err != nil {
		return err
	}
	defer func() { _ = p.voiceStatus(false); p.resumeMusic = true }()
	if err = p.sendAudioPCM(streamCtx, stop, pcm, encoder, false); err != nil {
		return err
	}
	return cmd.Wait()
}
