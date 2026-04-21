package main

import (
	"encoding/binary"
	"log"
	"os"

	"github.com/bwmarrin/discordgo"
	"gopkg.in/hraban/opus.v2"
)

//converts opus slop into OCM into file

/*
sudo dnf install opus opus-devel
sudo dnf install opus opus-devel opusfile opusfile-devel pkgconf-pkg-config
*/

const (
	sampleRate    = 48000
	channels      = 2
	bitsPerSample = 16
	maxFrameMs    = 120
)

type WAVWriter struct {
	f          *os.File
	dataSize   uint32
	sampleRate uint32
	channels   uint16
	bitDepth   uint16
}

func NewWAVWriter(path string, sampleRate uint32, channels uint16, bitDepth uint16) (*WAVWriter, error) {
	f, err := os.Create(path)
	if err != nil {
		return nil, err
	}

	w := &WAVWriter{
		f:          f,
		sampleRate: sampleRate,
		channels:   channels,
		bitDepth:   bitDepth,
	}

	// Reservar header WAV inicial
	if err := w.writeHeader(0); err != nil {
		f.Close()
		return nil, err
	}

	return w, nil
}

func (w *WAVWriter) writeHeader(dataSize uint32) error {
	byteRate := w.sampleRate * uint32(w.channels) * uint32(w.bitDepth) / 8
	blockAlign := w.channels * w.bitDepth / 8
	chunkSize := 36 + dataSize

	if _, err := w.f.Seek(0, 0); err != nil {
		return err
	}

	// RIFF header
	if _, err := w.f.Write([]byte("RIFF")); err != nil {
		return err
	}
	if err := binary.Write(w.f, binary.LittleEndian, chunkSize); err != nil {
		return err
	}
	if _, err := w.f.Write([]byte("WAVE")); err != nil {
		return err
	}

	// fmt chunk
	if _, err := w.f.Write([]byte("fmt ")); err != nil {
		return err
	}
	if err := binary.Write(w.f, binary.LittleEndian, uint32(16)); err != nil { // PCM chunk size
		return err
	}
	if err := binary.Write(w.f, binary.LittleEndian, uint16(1)); err != nil { // PCM format
		return err
	}
	if err := binary.Write(w.f, binary.LittleEndian, w.channels); err != nil {
		return err
	}
	if err := binary.Write(w.f, binary.LittleEndian, w.sampleRate); err != nil {
		return err
	}
	if err := binary.Write(w.f, binary.LittleEndian, byteRate); err != nil {
		return err
	}
	if err := binary.Write(w.f, binary.LittleEndian, blockAlign); err != nil {
		return err
	}
	if err := binary.Write(w.f, binary.LittleEndian, w.bitDepth); err != nil {
		return err
	}

	// data chunk
	if _, err := w.f.Write([]byte("data")); err != nil {
		return err
	}
	if err := binary.Write(w.f, binary.LittleEndian, dataSize); err != nil {
		return err
	}

	return nil
}

func (w *WAVWriter) WritePCM(pcm []int16) error {
	for _, s := range pcm {
		if err := binary.Write(w.f, binary.LittleEndian, s); err != nil {
			return err
		}
		w.dataSize += 2
	}
	return nil
}

func (w *WAVWriter) Close() error {
	if err := w.writeHeader(w.dataSize); err != nil {
		w.f.Close()
		return err
	}
	return w.f.Close()
}

func ListenAndWriteOpusToWAV(vc *discordgo.VoiceConnection, outPath string) error {
	wav, err := NewWAVWriter(outPath, sampleRate, channels, bitsPerSample)
	if err != nil {
		return err
	}
	defer wav.Close()

	log.Printf("a gravar áudio para %s", outPath)

	maxSamplesPerChannel := maxFrameMs * sampleRate / 1000
	pcm := make([]int16, maxSamplesPerChannel*channels)
	decoders := make(map[uint32]*opus.Decoder)

	for {
		packet, ok := <-vc.OpusRecv
		if !ok {
			log.Println("OpusRecv fechado")
			return nil
		}
		if packet == nil || len(packet.Opus) == 0 {
			continue
		}

		dec := decoders[packet.SSRC]
		if dec == nil {
			dec, err = opus.NewDecoder(sampleRate, channels)
			if err != nil {
				return err
			}
			decoders[packet.SSRC] = dec
		}

		n, err := dec.Decode(packet.Opus, pcm)
		if err != nil {
			log.Printf("erro a descodificar opus (ssrc=%d): %v", packet.SSRC, err)
			continue
		}

		if err := wav.WritePCM(pcm[:n*channels]); err != nil {
			return err
		}
	}
}
