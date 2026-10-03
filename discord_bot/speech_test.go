package main

import (
	"bytes"
	"context"
	"encoding/binary"
	"os"
	"os/exec"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/bwmarrin/discordgo"
	"gopkg.in/hraban/opus.v2"
)

func speechPlayer(t *testing.T) *MusicPlayer {
	t.Helper()
	for _, command := range []string{"python3", "piper", "ffmpeg"} {
		if _, err := exec.LookPath(command); err != nil {
			t.Skip(command + " is not installed")
		}
	}
	for _, path := range []string{speechVoiceModel(), speechVoiceModel() + ".json"} {
		if _, err := os.Stat(path); err != nil {
			t.Skip("Piper voice is not installed: " + path)
		}
	}
	p := newMusicPlayer(&discordgo.VoiceConnection{OpusSend: make(chan []byte, 500)})
	p.voiceStatus = func(bool) error { return nil }
	t.Cleanup(p.cancel)
	return p
}

func TestSpeechPortugueseSynthesisProducesDiscordOpusWhileMusicPaused(t *testing.T) {
	p := speechPlayer(t)
	p.paused = true
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	if err := p.Speak(ctx, "Boa! 😂"); err != nil {
		t.Fatal(err)
	}
	if !p.Snapshot().Paused || p.IsBusy() {
		t.Fatal("speech changed the manual music pause or remained busy")
	}
	decoder, err := opus.NewDecoder(sampleRate, channels)
	if err != nil {
		t.Fatal(err)
	}
	pcm := make([]int16, defaultOpusFrameSamples*channels)
	var energy int64
	packets := len(p.vc.OpusSend)
	for i := 0; i < packets; i++ {
		frames, err := decoder.Decode(<-p.vc.OpusSend, pcm)
		if err != nil || frames != defaultOpusFrameSamples {
			t.Fatalf("invalid Discord Opus packet: frames=%d err=%v", frames, err)
		}
		for _, sample := range pcm {
			energy += int64(sample) * int64(sample)
		}
	}
	if packets < 5 || energy == 0 {
		t.Fatalf("no audible Portuguese synthesis: packets=%d energy=%d", packets, energy)
	}
}

func TestSpeechUnknownEmojiOnlyReplyStaysSilent(t *testing.T) {
	p := speechPlayer(t)
	if err := p.Speak(context.Background(), "🚀🧑‍💻"); err != nil {
		t.Fatal(err)
	}
	if len(p.vc.OpusSend) != 0 || p.IsBusy() {
		t.Fatal("silent emoji reply produced speech or retained playback")
	}
}

func TestSpeechOutputIsExclusiveAndMusicResumes(t *testing.T) {
	p := speechPlayer(t)
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	started, musicDone := make(chan struct{}), make(chan error, 1)
	var once sync.Once
	p.voiceStatus = func(speaking bool) error {
		if speaking {
			once.Do(func() { close(started) })
		} else {
			select {
			case <-musicDone:
				t.Error("music sent packets during speech")
			default:
			}
		}
		return nil
	}
	voiceDone := make(chan error, 1)
	go func() { voiceDone <- p.Speak(ctx, "Diz.") }()
	select {
	case <-started:
	case <-ctx.Done():
		t.Fatal("speech never started")
	}
	pcm := make([]byte, defaultOpusFrameSamples*channels*2*3)
	for i := 0; i < len(pcm)/2; i++ {
		binary.LittleEndian.PutUint16(pcm[i*2:], 1000)
	}
	encoder, err := opus.NewEncoder(sampleRate, channels, opus.AppAudio)
	if err != nil {
		t.Fatal(err)
	}
	streamCtx, stop := context.WithCancelCause(ctx)
	defer stop(nil)
	go func() { musicDone <- p.sendPCM(streamCtx, stop, bytes.NewReader(pcm), encoder) }()
	if err := <-voiceDone; err != nil {
		t.Fatal(err)
	}
	if err := <-musicDone; err != nil {
		t.Fatal(err)
	}
	if p.IsBusy() {
		t.Fatal("speech did not release playback")
	}
}

func TestSpeechSlowsDownAndPausesBetweenSentences(t *testing.T) {
	p := speechPlayer(t)
	ctx, cancel := context.WithTimeout(context.Background(), 20*time.Second)
	defer cancel()
	text := "Olá. Vamos falar com calma para perceberes tudo."
	baseline := exec.CommandContext(ctx, "piper", "--model", speechVoiceModel(), "--output-raw")
	baseline.Stdin = strings.NewReader(text)
	audio, err := baseline.Output()
	if err != nil {
		t.Fatal(err)
	}
	normalSeconds := float64(len(audio)) / (22050 * 2)
	if normalSeconds == 0 {
		t.Fatal("baseline voice produced no audio")
	}
	if err := p.Speak(ctx, text); err != nil {
		t.Fatal(err)
	}
	spokenSeconds := float64(len(p.vc.OpusSend)) * 0.02
	// Allow phoneme-duration randomness while requiring slower speech and a sentence pause.
	if spokenSeconds < normalSeconds*1.1+0.3 {
		t.Fatalf("speech is too rushed: normal=%.2fs spoken=%.2fs", normalSeconds, spokenSeconds)
	}
	t.Logf("normal=%.2fs, slower voice with pauses=%.2fs", normalSeconds, spokenSeconds)
}

func TestSpeechCancellationReleasesOutputAndChildProcesses(t *testing.T) {
	p := speechPlayer(t)
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	done := make(chan error, 1)
	go func() { done <- p.Speak(ctx, strings.Repeat("Uma resposta longa. ", 5)) }()
	select {
	case <-p.vc.OpusSend:
	case <-time.After(10 * time.Second):
		t.Fatal("speech never sent audio")
	}
	cancel()
	select {
	case err := <-done:
		if err == nil {
			t.Fatal("cancelled speech succeeded")
		}
	case <-time.After(time.Second):
		t.Fatal("cancellation did not stop playback")
	}
	if p.IsBusy() {
		t.Fatal("cancelled speech remained busy")
	}
	if err := p.acquireOutput(context.Background()); err != nil {
		t.Fatal(err)
	}
	p.releaseOutput()
}

func TestSpeechTextIsBoundedAndFailureReleasesPlayer(t *testing.T) {
	if got := speechText("**Olá**\n`macaco`"); got != "Olá macaco" {
		t.Fatal(got)
	}
	if got := speechText(strings.Repeat("á", 2100)); !strings.HasSuffix(got, "A resposta completa está no chat.") || len([]rune(got)) > 2040 {
		t.Fatal("unbounded speech input")
	}
	p := speechPlayer(t)
	t.Setenv("PATH", t.TempDir())
	if err := p.Speak(context.Background(), "Diz"); err == nil {
		t.Fatal("missing synthesis dependency succeeded")
	}
	if p.IsBusy() {
		t.Fatal("failed synthesis retained playback")
	}
}
