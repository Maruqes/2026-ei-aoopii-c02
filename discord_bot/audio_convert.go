package main

import (
	"context"
	"encoding/binary"
	"log"
	"os"
	"path/filepath"
	"time"

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

type userAudioRecording struct {
	wav              *WAVWriter
	path             string
	startedAt        time.Time
	user             voiceUserInfo
	channel          string
	sessionID        int64
	ssrc             uint32
	nextRTPTimestamp uint32
	hasRTPTimestamp  bool
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
	if len(pcm) == 0 {
		return nil
	}
	if err := binary.Write(w.f, binary.LittleEndian, pcm); err != nil {
		return err
	}

	w.dataSize += uint32(len(pcm) * 2)
	return nil
}

func (w *WAVWriter) WriteSilence(frames int) error {
	if frames <= 0 {
		return nil
	}

	const maxSilenceChunkFrames = sampleRate
	zeros := make([]int16, maxSilenceChunkFrames*int(w.channels))
	for frames > 0 {
		chunkFrames := min(frames, maxSilenceChunkFrames)
		if err := w.WritePCM(zeros[:chunkFrames*int(w.channels)]); err != nil {
			return err
		}
		frames -= chunkFrames
	}
	return nil
}

func (w *WAVWriter) FramesWritten() int64 {
	bytesPerFrame := int64(w.channels) * int64(w.bitDepth) / 8
	if bytesPerFrame == 0 {
		return 0
	}
	return int64(w.dataSize) / bytesPerFrame
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

func closeUserRecordings(recordings map[string]*userAudioRecording, transcriptions *TranscriptionClient) error {
	var firstErr error

	for discordID, recording := range recordings {
		if recording == nil {
			continue
		}

		delete(recordings, discordID)
		if err := closeAndTranscribeRecording(recording, voiceUserInfo{}, transcriptions); err != nil && firstErr == nil {
			firstErr = err
		}
	}

	return firstErr
}

func finishUserRecording(recordings map[string]*userAudioRecording, info voiceUserInfo, transcriptions *TranscriptionClient) error {
	info = info.withFallbacks()
	if info.DiscordID == "" {
		return nil
	}

	recording := recordings[info.DiscordID]
	if recording == nil {
		return nil
	}

	delete(recordings, info.DiscordID)
	return closeAndTranscribeRecording(recording, info, transcriptions)
}

func closeAndTranscribeRecording(recording *userAudioRecording, info voiceUserInfo, transcriptions *TranscriptionClient) error {
	if recording == nil || recording.wav == nil {
		return nil
	}

	user := mergeVoiceUserInfo(info, recording.user)
	if user.DiscordID == "" {
		user = recording.user.withFallbacks()
	}

	if err := recording.padToElapsed(time.Now().UTC()); err != nil {
		log.Printf("erro ao preencher silêncio final do WAV de user=%s: %v", user.DiscordID, err)
		return err
	}

	log.Printf(
		"a finalizar WAV user=%s username=%s channel=%s file=%s data_bytes=%d started_at=%s",
		user.DiscordID,
		user.Username,
		recording.channel,
		recording.path,
		recording.wav.dataSize,
		recording.startedAt.UTC().Format(time.RFC3339Nano),
	)

	if err := recording.wav.Close(); err != nil {
		log.Printf("erro ao fechar WAV de user=%s: %v", user.DiscordID, err)
		return err
	}

	if recording.wav.dataSize == 0 {
		log.Printf("WAV sem áudio para user=%s file=%s; transcrição ignorada", user.DiscordID, recording.path)
		return nil
	}

	log.Printf(
		"WAV finalizado; a chamar API transcrição user=%s username=%s channel=%s file=%s data_bytes=%d elapsed_recording=%s",
		user.DiscordID,
		user.Username,
		recording.channel,
		recording.path,
		recording.wav.dataSize,
		time.Since(recording.startedAt).Round(time.Second),
	)
	if err := transcriptions.SubmitTranscription(context.Background(), TranscriptionRequest{
		SessionID:          recording.sessionID,
		AudioPath:          recording.path,
		DiscordID:          user.DiscordID,
		Username:           user.Username,
		DisplayName:        user.DisplayName,
		ChannelName:        recording.channel,
		RecordingStartedAt: recording.startedAt,
	}); err != nil {
		log.Printf("erro ao submeter transcriÃ§Ã£o user=%s file=%s: %v", user.DiscordID, recording.path, err)
		return err
	}

	return nil
}

func ListenAndWriteOpusToWAV(
	vc *discordgo.VoiceConnection,
	outDir string,
	sessionID int64,
	ssrcUsers *SSRCUserMap,
	recordingEvents <-chan recordingControlEvent,
	transcriptions *TranscriptionClient,
	lookupUserInfo func(string) voiceUserInfo,
	currentChannelName func() string,
) error {
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
	userRecordings := make(map[string]*userAudioRecording)
	identifiedUsers := make(map[uint32]string)
	unknownSSRCs := make(map[uint32]bool)
	defer closeUserRecordings(userRecordings, transcriptions)

	for {
		select {
		case event, ok := <-recordingEvents:
			if !ok {
				recordingEvents = nil
				continue
			}
			if event.finishAll {
				log.Printf("evento recebido: finalizar todas as gravações ativas count=%d", len(userRecordings))
				if err := closeUserRecordings(userRecordings, transcriptions); err != nil {
					return err
				}
				return nil
			}
			log.Printf("evento recebido: finalizar gravação user=%s", event.user.DiscordID)
			if err := finishUserRecording(userRecordings, event.user, transcriptions); err != nil {
				return err
			}
			continue

		case packet, ok := <-vc.OpusRecv:
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
				} else {
					if syncSSRCUserMapFromVoiceConnection(vc, ssrcUsers) {
						if user, ok := ssrcUsers.GetBySSRC(packet.SSRC); ok {
							discordID = user.DiscordID
							if identifiedUsers[packet.SSRC] != user.DiscordID {
								identifiedUsers[packet.SSRC] = user.DiscordID
								log.Printf("SSRC associado pelo voice websocket user=%s ssrc=%d", user.DiscordID, user.SSRC)
							}
						}
					}
					if discordID == "" && !unknownSSRCs[packet.SSRC] {
						unknownSSRCs[packet.SSRC] = true
						log.Printf("a receber áudio de ssrc=%d sem user associado", packet.SSRC)
					}
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

			packetAt := time.Now().UTC()
			recording := userRecordings[discordID]
			if recording != nil && recording.hasRTPTimestamp && recording.ssrc != packet.SSRC {
				if err := recording.padToElapsed(packetAt); err != nil {
					return err
				}
			}
			if recording != nil {
				if _, ok := recording.rtpGapFrames(packet.SSRC, packet.Timestamp); !ok {
					continue
				}
			}

			n, err := dec.Decode(packet.Opus, pcm)
			if err != nil {
				log.Printf("erro a descodificar opus (ssrc=%d): %v", packet.SSRC, err)
				continue
			}

			if recording == nil {
				outPath := newUserAudioPath(outDir, discordID)
				newWriter, err := NewWAVWriter(outPath, sampleRate, channels, bitsPerSample)
				if err != nil {
					return err
				}
				recording = &userAudioRecording{
					wav:       newWriter,
					path:      outPath,
					startedAt: packetAt,
					user:      getRecordingUserInfo(discordID, lookupUserInfo),
					channel:   getCurrentChannelName(currentChannelName, vc.ChannelID),
					sessionID: sessionID,
				}
				userRecordings[discordID] = recording
				log.Printf("a gravar user=%s para %s", discordID, outPath)
			}

			if err := recording.writeTimedPCM(packet.SSRC, packet.Timestamp, pcm[:n*channels], n); err != nil {
				return err
			}
		}
	}
}

func (recording *userAudioRecording) rtpGapFrames(ssrc uint32, timestamp uint32) (int, bool) {
	if recording == nil || !recording.hasRTPTimestamp || recording.ssrc != ssrc {
		return 0, true
	}

	gap := int32(timestamp - recording.nextRTPTimestamp)
	if gap < 0 {
		return 0, false
	}
	return int(gap), true
}

func (recording *userAudioRecording) writeTimedPCM(ssrc uint32, timestamp uint32, pcm []int16, frames int) error {
	if recording == nil || recording.wav == nil || frames <= 0 {
		return nil
	}

	gapFrames, ok := recording.rtpGapFrames(ssrc, timestamp)
	if !ok {
		return nil
	}
	if err := recording.wav.WriteSilence(gapFrames); err != nil {
		return err
	}
	if err := recording.wav.WritePCM(pcm); err != nil {
		return err
	}

	recording.ssrc = ssrc
	recording.nextRTPTimestamp = timestamp + uint32(frames)
	recording.hasRTPTimestamp = true
	return nil
}

func (recording *userAudioRecording) padToElapsed(endAt time.Time) error {
	if recording == nil || recording.wav == nil || recording.startedAt.IsZero() || !endAt.After(recording.startedAt) {
		return nil
	}

	elapsedFrames := endAt.Sub(recording.startedAt).Nanoseconds() * sampleRate / int64(time.Second)
	missingFrames := elapsedFrames - recording.wav.FramesWritten()
	if missingFrames <= 0 {
		return nil
	}
	return recording.wav.WriteSilence(int(missingFrames))
}

func getRecordingUserInfo(discordID string, lookupUserInfo func(string) voiceUserInfo) voiceUserInfo {
	if lookupUserInfo == nil {
		return voiceUserInfo{DiscordID: discordID}.withFallbacks()
	}

	info := lookupUserInfo(discordID)
	if info.DiscordID == "" {
		info.DiscordID = discordID
	}
	return info.withFallbacks()
}

func getCurrentChannelName(currentChannelName func() string, fallback string) string {
	if currentChannelName != nil {
		if channelName := currentChannelName(); channelName != "" {
			return channelName
		}
	}
	if fallback != "" {
		return fallback
	}
	return "voice"
}
