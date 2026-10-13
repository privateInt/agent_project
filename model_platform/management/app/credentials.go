package app

import (
	"crypto/aes"
	"crypto/cipher"
	"crypto/rand"
	"encoding/base64"
	"fmt"
	"io"
	"strings"
	"time"
)

const importCredentialTTL = time.Hour

// ParseCredentialEncryptionKey decodes the platform-owned key used only to
// encrypt short-lived user credentials at rest. The value must be a base64
// encoded 32-byte key (AES-256), for example: openssl rand -base64 32.
func ParseCredentialEncryptionKey(value string) ([]byte, error) {
	value = strings.TrimSpace(value)
	if value == "" {
		return nil, fmt.Errorf("credential encryption key is required")
	}
	key, err := base64.StdEncoding.DecodeString(value)
	if err != nil {
		return nil, fmt.Errorf("decode credential encryption key: %w", err)
	}
	if len(key) != 32 {
		return nil, fmt.Errorf("credential encryption key must decode to exactly 32 bytes, got %d", len(key))
	}
	return key, nil
}

func encryptCredential(key []byte, plaintext string) (nonce, ciphertext []byte, err error) {
	block, err := aes.NewCipher(key)
	if err != nil {
		return nil, nil, err
	}
	gcm, err := cipher.NewGCM(block)
	if err != nil {
		return nil, nil, err
	}
	nonce = make([]byte, gcm.NonceSize())
	if _, err := io.ReadFull(rand.Reader, nonce); err != nil {
		return nil, nil, err
	}
	ciphertext = gcm.Seal(nil, nonce, []byte(plaintext), nil)
	return nonce, ciphertext, nil
}

func decryptCredential(key, nonce, ciphertext []byte) (string, error) {
	block, err := aes.NewCipher(key)
	if err != nil {
		return "", err
	}
	gcm, err := cipher.NewGCM(block)
	if err != nil {
		return "", err
	}
	plaintext, err := gcm.Open(nil, nonce, ciphertext, nil)
	if err != nil {
		return "", err
	}
	return string(plaintext), nil
}
