package main

import (
	"encoding/binary"
	"log"
	"os"
	"path/filepath"

	"github.com/bwmarrin/discordgo"
	"github.com/google/uuid"
	"gopkg.in/hraban/opus.v2"
)

//converts opus slop into OCM into file

/*
sudo dnf install opus opus-devel
sudo dnf install opus opus-devel opusfile opusfile-devel pkgconf-pkg-config
*/

const (
	sampleRate    = 48000
	channels      = 1
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

func newUserAudioPath(outDir string, discordID string) string {
	filename := discordID + "-" + uuid.NewString() + ".wav"
	return filepath.Join(outDir, filename)
}

func closeUserWriters(writers map[string]*WAVWriter) error {
	var firstErr error

	for discordID, wav := range writers {
		if wav == nil {
			continue
		}
		if err := wav.Close(); err != nil && firstErr == nil {
			firstErr = err
			log.Printf("erro ao fechar WAV de user=%s: %v", discordID, err)
		}
	}

	return firstErr
}

func ListenAndWriteOpusToWAV(vc *discordgo.VoiceConnection, outDir string, ssrcUsers *SSRCUserMap) error {
	if outDir == "" {
		outDir = "."
	}
	if err := os.MkdirAll(outDir, 0o755); err != nil {
		return err
	}

	log.Printf("a gravar áudio para a pasta %s", outDir)

	maxSamplesPerChannel := maxFrameMs * sampleRate / 1000
	pcm := make([]int16, maxSamplesPerChannel*channels)
	decoders := make(map[uint32]*opus.Decoder)
	userWriters := make(map[string]*WAVWriter)
	identifiedUsers := make(map[uint32]string)
	unknownSSRCs := make(map[uint32]bool)
	defer closeUserWriters(userWriters)

	for {
		packet, ok := <-vc.OpusRecv
		if !ok {
			log.Println("OpusRecv fechado")
			return nil
		}
		if packet == nil || len(packet.Opus) == 0 {
			continue
		}

		var discordID string
		if ssrcUsers != nil {
			if user, ok := ssrcUsers.GetBySSRC(packet.SSRC); ok {
				discordID = user.DiscordID
				if identifiedUsers[packet.SSRC] != user.DiscordID {
					identifiedUsers[packet.SSRC] = user.DiscordID
					log.Printf("a receber áudio de user=%s ssrc=%d", user.DiscordID, user.SSRC)
				}
			} else if !unknownSSRCs[packet.SSRC] {
				unknownSSRCs[packet.SSRC] = true
				log.Printf("a receber áudio de ssrc=%d sem user associado", packet.SSRC)
			}
		}

		dec := decoders[packet.SSRC]
		if dec == nil {
			newDecoder, err := opus.NewDecoder(sampleRate, channels)
			if err != nil {
				return err
			}
			dec = newDecoder
			decoders[packet.SSRC] = dec
		}

		if discordID == "" {
			continue
		}

		n, err := dec.Decode(packet.Opus, pcm)
		if err != nil {
			log.Printf("erro a descodificar opus (ssrc=%d): %v", packet.SSRC, err)
			continue
		}

		wav := userWriters[discordID]
		if wav == nil {
			outPath := newUserAudioPath(outDir, discordID)
			newWriter, err := NewWAVWriter(outPath, sampleRate, channels, bitsPerSample)
			if err != nil {
				return err
			}
			wav = newWriter
			userWriters[discordID] = wav
			log.Printf("a gravar user=%s para %s", discordID, outPath)
		}

		if err := wav.WritePCM(pcm[:n*channels]); err != nil {
			return err
		}
	}
}
