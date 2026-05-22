package main

import (
	"encoding/binary"
	"os"
	"path/filepath"
	"testing"
	"time"
)

func TestConfiguredDecoderAcceptsDiscordSilenceFrame(t *testing.T) {
	decoder, err := newDiscordOpusDecoder()
	if err != nil {
		t.Fatal(err)
	}

	pcm, frames, err := decoder.Decode([]byte{0xf8, 0xff, 0xfe})
	if err != nil {
		t.Fatal(err)
	}
	if len(pcm) != frames*channels {
		t.Fatalf("decoded PCM has %d samples, want %d", len(pcm), frames*channels)
	}
}

func TestWAVHeaderDescribesDiscordPCM(t *testing.T) {
	path := filepath.Join(t.TempDir(), "recording.wav")
	wav, err := NewWAVWriter(path, sampleRate, channels, bitsPerSample)
	if err != nil {
		t.Fatal(err)
	}
	if err := wav.WritePCM([]int16{1, 2}); err != nil {
		t.Fatal(err)
	}
	if err := wav.Close(); err != nil {
		t.Fatal(err)
	}

	data, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	if len(data) < 44 {
		t.Fatalf("WAV is %d bytes, want header", len(data))
	}
	if got := string(data[:4]); got != "RIFF" {
		t.Fatalf("WAV chunk ID = %q, want RIFF", got)
	}
	if got := string(data[8:12]); got != "WAVE" {
		t.Fatalf("WAV format = %q, want WAVE", got)
	}
	if got := binary.LittleEndian.Uint16(data[20:22]); got != 1 {
		t.Fatalf("WAV audio format = %d, want PCM", got)
	}
	if got := binary.LittleEndian.Uint16(data[22:24]); got != channels {
		t.Fatalf("WAV channels = %d, want %d", got, channels)
	}
	if got := binary.LittleEndian.Uint32(data[24:28]); got != sampleRate {
		t.Fatalf("WAV sample rate = %d, want %d", got, sampleRate)
	}
	if got := binary.LittleEndian.Uint16(data[34:36]); got != bitsPerSample {
		t.Fatalf("WAV bit depth = %d, want %d", got, bitsPerSample)
	}
}

func TestTimedRecordingCollapsesTinyRTPGapsInWAV(t *testing.T) {
	path := filepath.Join(t.TempDir(), "recording.wav")
	wav, err := NewWAVWriter(path, sampleRate, channels, bitsPerSample)
	if err != nil {
		t.Fatal(err)
	}

	recording := &userAudioRecording{wav: wav, startedAt: time.Now()}
	if err := recording.writeTimedPCM(7, 100, []int16{1, 2, 3, 4}, 2); err != nil {
		t.Fatal(err)
	}
	if err := recording.writeTimedPCM(7, 100+sampleRate/1000*20, []int16{5, 6}, 1); err != nil {
		t.Fatal(err)
	}
	if err := wav.Close(); err != nil {
		t.Fatal(err)
	}

	samples := readWAVSamples(t, path)
	want := []int16{1, 2, 3, 4, 5, 6}
	if len(samples) != len(want) {
		t.Fatalf("got %d samples, want %d: %v", len(samples), len(want), samples)
	}
	for i := range want {
		if samples[i] != want[i] {
			t.Fatalf("sample %d = %d, want %d", i, samples[i], want[i])
		}
	}
}

func TestTimedRecordingPreservesLargeRTPGapsInWAV(t *testing.T) {
	path := filepath.Join(t.TempDir(), "recording.wav")
	wav, err := NewWAVWriter(path, sampleRate, channels, bitsPerSample)
	if err != nil {
		t.Fatal(err)
	}

	recording := &userAudioRecording{wav: wav, startedAt: time.Now()}
	if err := recording.writeTimedPCM(7, 100, []int16{1, 2}, 1); err != nil {
		t.Fatal(err)
	}
	if err := recording.writeTimedPCM(7, 100+sampleRate/1000*300, []int16{3, 4}, 1); err != nil {
		t.Fatal(err)
	}
	if err := wav.Close(); err != nil {
		t.Fatal(err)
	}

	samples := readWAVSamples(t, path)
	wantPrefix := []int16{1, 2}
	wantSuffix := []int16{3, 4}
	gapFrames := sampleRate / 1000 * 300
	wantLen := len(wantPrefix) + gapFrames*channels + len(wantSuffix)
	if len(samples) != wantLen {
		t.Fatalf("got %d samples, want %d", len(samples), wantLen)
	}
	for i := range wantPrefix {
		if samples[i] != wantPrefix[i] {
			t.Fatalf("prefix sample %d = %d, want %d", i, samples[i], wantPrefix[i])
		}
	}
	for i := len(wantPrefix); i < len(samples)-len(wantSuffix); i++ {
		if samples[i] != 0 {
			t.Fatalf("gap sample %d = %d, want 0", i, samples[i])
		}
	}
	for i := range wantSuffix {
		idx := len(samples) - len(wantSuffix) + i
		if samples[idx] != wantSuffix[i] {
			t.Fatalf("suffix sample %d = %d, want %d", idx, samples[idx], wantSuffix[i])
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

func TestTimedRecordingRejectsIncompleteStereoPCM(t *testing.T) {
	path := filepath.Join(t.TempDir(), "recording.wav")
	wav, err := NewWAVWriter(path, sampleRate, channels, bitsPerSample)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = wav.Close() })

	recording := &userAudioRecording{wav: wav, startedAt: time.Now()}
	if err := recording.writeTimedPCM(7, 100, []int16{1}, 1); err == nil {
		t.Fatal("incomplete PCM was accepted")
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
