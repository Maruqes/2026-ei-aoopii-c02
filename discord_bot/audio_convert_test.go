package main

import (
	"encoding/binary"
	"os"
	"path/filepath"
	"testing"
	"time"

	"gopkg.in/hraban/opus.v2"
)

func TestConfiguredDecoderAcceptsDiscordSilenceFrame(t *testing.T) {
	decoder, err := opus.NewDecoder(sampleRate, channels)
	if err != nil {
		t.Fatal(err)
	}

	pcm := make([]int16, maxFrameMs*sampleRate/1000*channels)
	if _, err := decoder.Decode([]byte{0xf8, 0xff, 0xfe}, pcm); err != nil {
		t.Fatal(err)
	}
}

func TestTimedRecordingPreservesRTPGapsInWAV(t *testing.T) {
	path := filepath.Join(t.TempDir(), "recording.wav")
	wav, err := NewWAVWriter(path, sampleRate, channels, bitsPerSample)
	if err != nil {
		t.Fatal(err)
	}

	recording := &userAudioRecording{wav: wav, startedAt: time.Now()}
	if err := recording.writeTimedPCM(7, 100, []int16{1, 2}, 2); err != nil {
		t.Fatal(err)
	}
	if err := recording.writeTimedPCM(7, 107, []int16{5}, 1); err != nil {
		t.Fatal(err)
	}
	if err := wav.Close(); err != nil {
		t.Fatal(err)
	}

	samples := readWAVSamples(t, path)
	want := []int16{1, 2, 0, 0, 0, 0, 0, 5}
	if len(samples) != len(want) {
		t.Fatalf("got %d samples, want %d: %v", len(samples), len(want), samples)
	}
	for i := range want {
		if samples[i] != want[i] {
			t.Fatalf("sample %d = %d, want %d", i, samples[i], want[i])
		}
	}
}

func TestRecordingDropsOlderRTPTimestamps(t *testing.T) {
	path := filepath.Join(t.TempDir(), "recording.wav")
	wav, err := NewWAVWriter(path, sampleRate, channels, bitsPerSample)
	if err != nil {
		t.Fatal(err)
	}

	recording := &userAudioRecording{wav: wav, startedAt: time.Now()}
	if err := recording.writeTimedPCM(7, 100, []int16{1, 2}, 1); err != nil {
		t.Fatal(err)
	}
	if _, ok := recording.rtpGapFrames(7, 100); ok {
		t.Fatal("older RTP timestamp was accepted")
	}
	if err := recording.writeTimedPCM(7, 100, []int16{9, 9}, 1); err != nil {
		t.Fatal(err)
	}
	if err := wav.Close(); err != nil {
		t.Fatal(err)
	}

	samples := readWAVSamples(t, path)
	if len(samples) != 2 || samples[0] != 1 || samples[1] != 2 {
		t.Fatalf("stale packet changed WAV samples: %v", samples)
	}
}

func TestRecordingPadsTrailingSilenceToElapsedTime(t *testing.T) {
	path := filepath.Join(t.TempDir(), "recording.wav")
	wav, err := NewWAVWriter(path, sampleRate, channels, bitsPerSample)
	if err != nil {
		t.Fatal(err)
	}

	startedAt := time.Date(2026, time.May, 22, 12, 0, 0, 0, time.UTC)
	recording := &userAudioRecording{wav: wav, startedAt: startedAt}
	if err := recording.writeTimedPCM(7, 100, []int16{1, 2}, 1); err != nil {
		t.Fatal(err)
	}
	if err := recording.padToElapsed(startedAt.Add(time.Millisecond)); err != nil {
		t.Fatal(err)
	}

	if got, want := wav.FramesWritten(), int64(sampleRate/1000); got != want {
		t.Fatalf("got %d frames after padding, want %d", got, want)
	}
}

func readWAVSamples(t *testing.T, path string) []int16 {
	t.Helper()

	data, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	if len(data) < 44 {
		t.Fatalf("WAV is %d bytes, want header", len(data))
	}

	pcm := data[44:]
	if len(pcm)%2 != 0 {
		t.Fatalf("WAV PCM data has odd byte length: %d", len(pcm))
	}
	samples := make([]int16, len(pcm)/2)
	for i := range samples {
		samples[i] = int16(binary.LittleEndian.Uint16(pcm[i*2 : i*2+2]))
	}
	return samples
}
