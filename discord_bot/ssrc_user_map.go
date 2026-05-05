package main

import "sync"

type SSRCUser struct {
	SSRC      uint32
	DiscordID string
}

type SSRCUserMap struct {
	mu          sync.RWMutex
	bySSRC      map[uint32]SSRCUser
	byDiscordID map[string]SSRCUser
}

func NewSSRCUserMap() *SSRCUserMap {
	return &SSRCUserMap{
		bySSRC:      make(map[uint32]SSRCUser),
		byDiscordID: make(map[string]SSRCUser),
	}
}

func (m *SSRCUserMap) Set(ssrc uint32, discordID string) {
	if m == nil || ssrc == 0 || discordID == "" {
		return
	}

	user := SSRCUser{
		SSRC:      ssrc,
		DiscordID: discordID,
	}

	m.mu.Lock()
	defer m.mu.Unlock()

	if current, ok := m.bySSRC[ssrc]; ok && current.DiscordID != "" && current.DiscordID != discordID {
		delete(m.byDiscordID, current.DiscordID)
	}
	if current, ok := m.byDiscordID[discordID]; ok && current.SSRC != 0 && current.SSRC != ssrc {
		delete(m.bySSRC, current.SSRC)
	}

	m.bySSRC[ssrc] = user
	m.byDiscordID[discordID] = user
}

func (m *SSRCUserMap) GetBySSRC(ssrc uint32) (SSRCUser, bool) {
	if m == nil {
		return SSRCUser{}, false
	}

	m.mu.RLock()
	defer m.mu.RUnlock()

	user, ok := m.bySSRC[ssrc]
	return user, ok
}

func (m *SSRCUserMap) GetByDiscordID(discordID string) (SSRCUser, bool) {
	if m == nil || discordID == "" {
		return SSRCUser{}, false
	}

	m.mu.RLock()
	defer m.mu.RUnlock()

	user, ok := m.byDiscordID[discordID]
	return user, ok
}

func (m *SSRCUserMap) SSRCByDiscordID(discordID string) (uint32, bool) {
	user, ok := m.GetByDiscordID(discordID)
	if !ok {
		return 0, false
	}
	return user.SSRC, true
}

func (m *SSRCUserMap) DiscordIDBySSRC(ssrc uint32) (string, bool) {
	user, ok := m.GetBySSRC(ssrc)
	if !ok {
		return "", false
	}
	return user.DiscordID, true
}

func (m *SSRCUserMap) DeleteBySSRC(ssrc uint32) {
	if m == nil {
		return
	}

	m.mu.Lock()
	defer m.mu.Unlock()

	user, ok := m.bySSRC[ssrc]
	if !ok {
		return
	}

	delete(m.bySSRC, ssrc)
	delete(m.byDiscordID, user.DiscordID)
}

func (m *SSRCUserMap) DeleteByDiscordID(discordID string) {
	if m == nil || discordID == "" {
		return
	}

	m.mu.Lock()
	defer m.mu.Unlock()

	user, ok := m.byDiscordID[discordID]
	if !ok {
		return
	}

	delete(m.byDiscordID, discordID)
	delete(m.bySSRC, user.SSRC)
}

func (m *SSRCUserMap) Reset() {
	if m == nil {
		return
	}

	m.mu.Lock()
	defer m.mu.Unlock()

	m.bySSRC = make(map[uint32]SSRCUser)
	m.byDiscordID = make(map[string]SSRCUser)
}
