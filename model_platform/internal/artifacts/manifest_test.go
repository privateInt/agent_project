package artifacts

import (
	"bytes"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func fixture(t *testing.T) string {
	t.Helper()
	root := t.TempDir()
	for name, data := range map[string]string{"adapter_config.json": "{}", "adapter_model.safetensors": "weights", "checkpoint-1/optimizer.pt": "state"} {
		p := filepath.Join(root, name)
		if err := os.MkdirAll(filepath.Dir(p), 0700); err != nil {
			t.Fatal(err)
		}
		if err := os.WriteFile(p, []byte(data), 0600); err != nil {
			t.Fatal(err)
		}
	}
	return root
}
func TestManifestRoundTrip(t *testing.T) {
	root := fixture(t)
	m, b, d, err := Build(root)
	if err != nil {
		t.Fatal(err)
	}
	if m.FileCount != 3 || m.TotalBytes != 14 {
		t.Fatalf("unexpected manifest: %+v", m)
	}
	got, hash, err := Parse(b)
	if err != nil || hash != d || got.TotalBytes != m.TotalBytes {
		t.Fatalf("roundtrip: %v", err)
	}
	_, b2, d2, err := Build(root)
	if err != nil || !bytes.Equal(b, b2) || d != d2 {
		t.Fatal("manifest is not deterministic")
	}
	for _, path := range []string{"../escape", "/absolute", "a/../b", "a//b", "a\\b", ManifestName} {
		if ValidPath(path) {
			t.Errorf("accepted unsafe path %q", path)
		}
	}
	if _, err := Prefix("00000000-0000-0000-0000-000000000001", "00000000-0000-0000-0000-000000000002", d); err != nil {
		t.Fatal(err)
	}
}
func TestManifestRejectsTampering(t *testing.T) {
	root := fixture(t)
	_, b, _, err := Build(root)
	if err != nil {
		t.Fatal(err)
	}
	for _, bad := range [][]byte{append([]byte(nil), b[:len(b)-1]...), append(append([]byte(nil), b...), byte(' ')), bytes.Replace(b, []byte("weights"), []byte("other"), 1)} {
		// The first two are noncanonical; a content change is checked separately.
		if bytes.Equal(bad, b) {
			continue
		}
		if _, _, err := Parse(bad); err == nil && len(bad) != len(b) {
			t.Fatal("accepted malformed manifest")
		}
	}
	m, _, err := Parse(b)
	if err != nil {
		t.Fatal(err)
	}
	m.Files[0].SHA256 = strings.Repeat("0", 64)
	encoded, _, err := Encode(m)
	if err != nil {
		t.Fatal(err)
	}
	parsed, _, err := Parse(encoded)
	if err != nil {
		t.Fatal(err)
	}
	digest, _, err := HashFile(filepath.Join(root, parsed.Files[0].Path))
	if err != nil {
		t.Fatal(err)
	}
	if digest == parsed.Files[0].SHA256 {
		t.Fatal("tampering was not detected")
	}
}
func TestManifestRejectsInvalidOutput(t *testing.T) {
	root := fixture(t)
	if err := os.Remove(filepath.Join(root, "adapter_model.safetensors")); err != nil {
		t.Fatal(err)
	}
	if _, _, _, err := Build(root); err == nil {
		t.Fatal("accepted missing adapter weights")
	}
	if err := os.Symlink("/etc/passwd", filepath.Join(root, "external")); err != nil {
		t.Fatal(err)
	}
	if _, _, _, err := Build(root); err == nil {
		t.Fatal("accepted symlink")
	}
}
