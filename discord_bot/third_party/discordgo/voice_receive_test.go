package discordgo

import (
	"bytes"
	"crypto/aes"
	"crypto/cipher"
	"encoding/binary"
	"fmt"
	"net"
	"testing"
	"time"

	"golang.org/x/crypto/chacha20poly1305"
)

func TestOpusReceiverOwnsQueuedPacket(t *testing.T) {
	for _, mode := range []string{"aes-gcm", "xchacha20"} {
		for _, extension := range []bool{false, true} {
			for _, dave := range []bool{false, true} {
				t.Run(fmt.Sprintf("%s/extension=%t/dave=%t", mode, extension, dave), func(t *testing.T) {
					key := make([]byte, 32)
					var aead cipher.AEAD
					var err error
					if mode == "aes-gcm" {
						block, blockErr := aes.NewCipher(key)
						if blockErr != nil {
							t.Fatal(blockErr)
						}
						aead, err = cipher.NewGCM(block)
					} else {
						aead, err = chacha20poly1305.NewX(key)
					}
					if err != nil {
						t.Fatal(err)
					}
					receiver, err := net.ListenUDP("udp", &net.UDPAddr{IP: net.IPv4(127, 0, 0, 1)})
					if err != nil {
						t.Fatal(err)
					}
					stop := make(chan struct{})
					done := make(chan struct{})
					vc := &VoiceConnection{aead: aead}
					if dave {
						vc.dave = NewDAVESession("1")
					}
					packets := make(chan *Packet, 2)
					go func() {
						defer close(done)
						vc.opusReceiver(receiver, stop, packets)
					}()
					t.Cleanup(func() {
						close(stop)
						receiver.Close()
						select {
						case <-done:
						case <-time.After(time.Second):
							t.Error("receiver did not stop")
						}
					})
					sender, err := net.DialUDP("udp", nil, receiver.LocalAddr().(*net.UDPAddr))
					if err != nil {
						t.Fatal(err)
					}
					defer sender.Close()
					send := func(sequence uint16, opus, ext []byte) *Packet {
						t.Helper()
						header := make([]byte, 12)
						header[0], header[1] = 0x80, 0x78
						binary.BigEndian.PutUint16(header[2:4], sequence)
						binary.BigEndian.PutUint32(header[4:8], uint32(sequence)*960)
						binary.BigEndian.PutUint32(header[8:12], 10)
						payload := append([]byte(nil), opus...)
						if extension {
							header[0] |= 0x10
							header = append(header, 0xbe, 0xde, 0, 1)
							payload = append(append([]byte(nil), ext...), opus...)
						}
						nonce := make([]byte, aead.NonceSize())
						binary.LittleEndian.PutUint32(nonce, uint32(sequence))
						wire := append(header, aead.Seal(nil, nonce, payload, header)...)
						wire = append(wire, nonce[:4]...)
						if _, err := sender.Write(wire); err != nil {
							t.Fatal(err)
						}
						select {
						case packet := <-packets:
							return packet
						case <-time.After(time.Second):
							t.Fatal("timed out receiving encrypted RTP")
							return nil
						}
					}
					// Discord's Opus silence packet takes the DAVE passthrough path.
					firstOpus := []byte{0xf8, 0xff, 0xfe}
					firstExt := []byte{0x10, 0x01, 0, 0}
					first := send(1, firstOpus, firstExt)
					// Retain the first packet as a jitter queue does. Receiving the
					// second proves the shared UDP buffer has been reused.
					secondOpus := []byte{0xfc, 0x01, 0x02, 0, 0, 0, 0, 0, 0, 0, 0, 0}
					second := send(2, secondOpus, []byte{0x10, 0x02, 0, 0})
					if !bytes.Equal(first.Opus, firstOpus) || !bytes.Equal(second.Opus, secondOpus) {
						t.Fatalf("queued audio overwritten: first=%x second=%x", first.Opus, second.Opus)
					}
					if extension {
						want := append([]byte{0xbe, 0xde, 0, 1}, firstExt...)
						if !bytes.Equal(first.Extension, want) {
							t.Fatalf("queued extension overwritten: got=%x want=%x", first.Extension, want)
						}
					}
					// Also retain a normal (non-silence) DAVE passthrough frame.
					send(3, firstOpus, firstExt)
					if !bytes.Equal(second.Opus, secondOpus) {
						t.Fatalf("queued passthrough audio overwritten: got=%x want=%x", second.Opus, secondOpus)
					}
				})
			}
		}
	}
}
