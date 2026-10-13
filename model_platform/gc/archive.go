package main

import (
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"io"
	"os"
	"path/filepath"
)

// Get reports missing objects as os.ErrNotExist. Put is called only for missing
// content-addressed objects; every object is read back before collection succeeds.
type ObjectStore interface {
	Get(context.Context, string) (io.ReadCloser, error)
	Put(context.Context, string, io.Reader, int64) error
	URI(string) string
}

type FileEntry struct {
	Path   string `json:"path"`
	Size   int64  `json:"size"`
	SHA256 string `json:"sha256"`
}
type Manifest struct {
	SchemaVersion int         `json:"schema_version"`
	Kind          string      `json:"kind"`
	Files         []FileEntry `json:"files"`
}
type Receipt struct {
	Prefix   string
	Manifest []byte
	Files    []FileEntry
}

func digest(b []byte) string {
	h := sha256.Sum256(b)
	return hex.EncodeToString(h[:])
}

func readDigest(r io.Reader, max int64) (string, int64, error) {
	h := sha256.New()
	n, err := io.Copy(h, io.LimitReader(r, max+1))
	if err == nil && n > max {
		err = fmt.Errorf("object exceeds expected size")
	}
	return hex.EncodeToString(h.Sum(nil)), n, err
}

func verifyFile(ctx context.Context, store ObjectStore, key string, entry FileEntry) error {
	r, err := store.Get(ctx, key)
	if err != nil {
		return err
	}
	hash, size, readErr := readDigest(r, entry.Size)
	closeErr := r.Close()
	if readErr != nil {
		return readErr
	}
	if closeErr != nil {
		return closeErr
	}
	if hash != entry.SHA256 || size != entry.Size {
		return fmt.Errorf("archive checksum mismatch: %s", entry.Path)
	}
	return nil
}

func ensureFile(ctx context.Context, store ObjectStore, key string, entry FileEntry, open func() (io.ReadCloser, error)) error {
	err := verifyFile(ctx, store, key, entry)
	if err == nil {
		return nil
	}
	if !os.IsNotExist(err) {
		return err
	}
	r, err := open()
	if err != nil {
		return err
	}
	putErr := store.Put(ctx, key, r, entry.Size)
	closeErr := r.Close()
	if putErr != nil {
		return putErr
	}
	if closeErr != nil {
		return closeErr
	}
	return verifyFile(ctx, store, key, entry)
}

func (r Receipt) Verify(ctx context.Context, store ObjectStore) error {
	for _, f := range r.Files {
		if err := verifyFile(ctx, store, r.Prefix+f.Path, f); err != nil {
			return err
		}
	}
	return verifyFile(ctx, store, r.Prefix+"manifest.json", FileEntry{
		Path: "manifest.json", Size: int64(len(r.Manifest)), SHA256: digest(r.Manifest),
	})
}

func archive(ctx context.Context, docker Docker, store ObjectStore, s Snapshot, maxBytes int64) (Receipt, error) {
	var receipt Receipt
	root, err := os.MkdirTemp("", "model-job-logs-")
	if err != nil {
		return receipt, err
	}
	defer os.RemoveAll(root)
	stdout, err := os.Create(filepath.Join(root, "stdout.log"))
	if err != nil {
		return receipt, err
	}
	defer stdout.Close()
	stderr, err := os.Create(filepath.Join(root, "stderr.log"))
	if err != nil {
		return receipt, err
	}
	defer stderr.Close()
	budget := &byteBudget{remaining: maxBytes}
	if err := docker.Logs(ctx, s.ContainerID, limitedWriter{stdout, budget}, limitedWriter{stderr, budget}); err != nil {
		return receipt, err
	}
	if err := stdout.Close(); err != nil {
		return receipt, err
	}
	if err := stderr.Close(); err != nil {
		return receipt, err
	}
	metadata, err := json.Marshal(s)
	if err != nil {
		return receipt, err
	}
	if err := os.WriteFile(filepath.Join(root, "metadata.json"), metadata, 0600); err != nil {
		return receipt, err
	}
	manifest := Manifest{SchemaVersion: 1, Kind: "job-logs"}
	for _, name := range []string{"metadata.json", "stderr.log", "stdout.log"} {
		f, err := os.Open(filepath.Join(root, name))
		if err != nil {
			return receipt, err
		}
		hash, size, hashErr := readDigest(f, maxBytes+(1<<20))
		closeErr := f.Close()
		if hashErr != nil {
			return receipt, hashErr
		}
		if closeErr != nil {
			return receipt, closeErr
		}
		manifest.Files = append(manifest.Files, FileEntry{Path: name, Size: size, SHA256: hash})
	}
	encoded, err := json.Marshal(manifest)
	if err != nil {
		return receipt, err
	}
	receipt = Receipt{
		Prefix: "operations/"+s.OperationID+"/"+s.DispatchToken+"/"+s.ContainerID+"/"+digest(encoded)+"/",
		Manifest: encoded, Files: manifest.Files,
	}
	for _, entry := range manifest.Files {
		name := filepath.Join(root, entry.Path)
		if err := ensureFile(ctx, store, receipt.Prefix+entry.Path, entry, func() (io.ReadCloser, error) { return os.Open(name) }); err != nil {
			return Receipt{}, err
		}
	}
	// The manifest is the commit record, written after every log/metadata object
	// has passed a full read-back checksum. Retries reuse the same immutable keys.
	err = ensureFile(ctx, store, receipt.Prefix+"manifest.json", FileEntry{
		Path: "manifest.json", Size: int64(len(encoded)), SHA256: digest(encoded),
	}, func() (io.ReadCloser, error) { return io.NopCloser(bytes.NewReader(encoded)), nil })
	return receipt, err
}
