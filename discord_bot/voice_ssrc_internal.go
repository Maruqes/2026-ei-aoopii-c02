package main

import (
	"reflect"
	"unsafe"

	"github.com/bwmarrin/discordgo"
)

func syncSSRCUserMapFromVoiceConnection(vc *discordgo.VoiceConnection, target *SSRCUserMap) bool {
	if vc == nil || target == nil {
		return false
	}

	vc.RLock()
	defer vc.RUnlock()

	value := reflect.ValueOf(vc)
	if value.Kind() != reflect.Pointer || value.IsNil() {
		return false
	}

	field := value.Elem().FieldByName("ssrcToUserID")
	if !field.IsValid() || field.Kind() != reflect.Map || field.IsNil() || !field.CanAddr() {
		return false
	}
	if field.Type().Key().Kind() != reflect.Uint32 || field.Type().Elem().Kind() != reflect.String {
		return false
	}

	readable := reflect.NewAt(field.Type(), unsafe.Pointer(field.UnsafeAddr())).Elem()
	synced := false
	for _, key := range readable.MapKeys() {
		discordID := readable.MapIndex(key).String()
		if discordID == "" {
			continue
		}
		target.Set(uint32(key.Uint()), discordID)
		synced = true
	}

	return synced
}
