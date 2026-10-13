// Package datasets validates the exact bytes used by a registered training dataset.
package datasets

import (
	"bufio"
	"bytes"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"strings"
	"unicode/utf8"
)

const (
	MaxBytes = 64 << 20
	MaxRecordBytes = 1 << 20
	ChunkBytes = 256 << 10
	MinRecords = 2 // Axolotl's current debug preprocessing requires at least two.
)

type Info struct {
	SHA256 string `json:"sha256"`
	SizeBytes int64 `json:"size_bytes"`
	RecordCount int64 `json:"record_count"`
}

type byteCounter struct { n int64 }
func (c *byteCounter) Write(p []byte) (int, error) { c.n += int64(len(p)); return len(p), nil }

// Inspect checks every nonblank record; hashes include whitespace and line endings.
// This is structural validation. Tokenizer/template compatibility is checked by Axolotl.
func Inspect(r io.Reader) (Info, error) {
	h := sha256.New()
	counter := &byteCounter{}
	scanner := bufio.NewScanner(io.TeeReader(io.LimitReader(r, MaxBytes+1), io.MultiWriter(h, counter)))
	scanner.Buffer(make([]byte, 64<<10), MaxRecordBytes+2)
	var count int64
	line := 0
	for scanner.Scan() {
		line++
		if counter.n > MaxBytes { return Info{}, fmt.Errorf("dataset exceeds %d bytes", MaxBytes) }
		raw := scanner.Bytes()
		if len(raw) > MaxRecordBytes { return Info{}, fmt.Errorf("line %d exceeds record size limit", line) }
		if !utf8.Valid(raw) { return Info{}, fmt.Errorf("line %d is not valid UTF-8", line) }
		if len(bytes.TrimSpace(raw)) == 0 { continue }
		// Maps enforce case-sensitive keys like Python/Axolotl. Go struct decoding
		// would also accept "Messages" or "Content", which Axolotl cannot consume.
		var record map[string]json.RawMessage
		if err := json.Unmarshal(raw, &record); err != nil || len(record) != 1 { return Info{}, fmt.Errorf("line %d: expected only a messages field", line) }
		var messages []map[string]json.RawMessage
		if err := json.Unmarshal(record["messages"], &messages); err != nil || len(messages) == 0 { return Info{}, fmt.Errorf("line %d: messages must be a nonempty array", line) }
		user, assistant := false, false
		for _, message := range messages {
			var role, content *string
			if len(message) != 2 || json.Unmarshal(message["role"], &role) != nil || role == nil || json.Unmarshal(message["content"], &content) != nil || content == nil || strings.TrimSpace(*content) == "" {
				return Info{}, fmt.Errorf("line %d: each message requires only string role and nonempty string content", line)
			}
			switch *role {
			case "system":
			case "user": user = true
			case "assistant": assistant = true
			default: return Info{}, fmt.Errorf("line %d: role must be system, user or assistant", line)
			}
		}
		if !user || !assistant { return Info{}, fmt.Errorf("line %d: user and assistant messages are required", line) }
		count++
	}
	if err := scanner.Err(); err != nil { return Info{}, fmt.Errorf("read JSONL (record limit %d bytes): %w", MaxRecordBytes, err) }
	if counter.n > MaxBytes { return Info{}, fmt.Errorf("dataset exceeds %d bytes", MaxBytes) }
	if count < MinRecords { return Info{}, fmt.Errorf("dataset requires at least %d records", MinRecords) }
	return Info{SHA256: hex.EncodeToString(h.Sum(nil)), SizeBytes: counter.n, RecordCount: count}, nil
}

func (info Info) Validate() error {
	digest, err := hex.DecodeString(info.SHA256)
	if err != nil || len(digest) != sha256.Size || info.SHA256 != strings.ToLower(info.SHA256) || info.SizeBytes <= 0 || info.SizeBytes > MaxBytes || info.RecordCount < MinRecords {
		return fmt.Errorf("dataset version has incomplete or invalid pinned metadata")
	}
	return nil
}

func Verify(r io.Reader, expected Info) error {
	if err := expected.Validate(); err != nil { return err }
	actual, err := Inspect(io.LimitReader(r, expected.SizeBytes+1))
	if err != nil { return err }
	if actual != expected { return fmt.Errorf("dataset content does not match pinned SHA-256, size or record count") }
	return nil
}

// Materialize publishes a local file only after all pinned properties match.
func Materialize(r io.Reader, dest string, expected Info) error {
	if err := expected.Validate(); err != nil { return err }
	if err := os.MkdirAll(filepath.Dir(dest), 0o700); err != nil { return err }
	tmp, err := os.CreateTemp(filepath.Dir(dest), ".dataset-*")
	if err != nil { return err }
	defer os.Remove(tmp.Name())
	defer tmp.Close()
	if err := Verify(io.TeeReader(io.LimitReader(r, expected.SizeBytes+1), tmp), expected); err != nil { return err }
	if err := tmp.Close(); err != nil { return err }
	return os.Rename(tmp.Name(), dest)
}
