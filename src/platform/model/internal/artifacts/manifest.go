// Package artifacts defines the immutable on-disk and S3 artifact contract.
// It deliberately has no MLflow, database or S3 dependencies.
package artifacts

import (
	"bytes"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"os"
	"path"
	"path/filepath"
	"regexp"
	"sort"
	"strings"
)

const ManifestName = "artifact-manifest.json"
const maxFiles = 100000

var digestPattern = regexp.MustCompile(`^[0-9a-f]{64}$`)
var uuidPattern = regexp.MustCompile(`^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$`)

type File struct {
	Path   string `json:"path"`
	Size   int64  `json:"size"`
	SHA256 string `json:"sha256"`
}
type Manifest struct {
	SchemaVersion int    `json:"schema_version"`
	ArtifactKind  string `json:"artifact_kind"`
	FileCount     int    `json:"file_count"`
	TotalBytes    int64  `json:"total_bytes"`
	Files         []File `json:"files"`
}

func ValidDigest(s string) bool { return digestPattern.MatchString(s) }
func SHA256(b []byte) string    { sum := sha256.Sum256(b); return hex.EncodeToString(sum[:]) }
func ValidPath(s string) bool {
	if s == "" || s == ManifestName || strings.Contains(s, "\\") || strings.ContainsAny(s, "\x00\r\n") || strings.HasPrefix(s, "/") || path.Clean(s) != s {
		return false
	}
	for _, part := range strings.Split(s, "/") {
		if part == "." || part == ".." || part == "" {
			return false
		}
	}
	return true
}
func Validate(m Manifest) error {
	if m.SchemaVersion != 1 || m.ArtifactKind != "adapter" || m.FileCount != len(m.Files) || len(m.Files) == 0 || len(m.Files) > maxFiles {
		return errors.New("invalid adapter manifest header")
	}
	var total int64
	previous := ""
	required := map[string]bool{"adapter_config.json": false, "adapter_model.safetensors": false}
	for _, f := range m.Files {
		if !ValidPath(f.Path) || f.Path <= previous || f.Size < 0 || !ValidDigest(f.SHA256) {
			return fmt.Errorf("invalid manifest file %q", f.Path)
		}
		if f.Size > int64(^uint64(0)>>1)-total {
			return errors.New("manifest size overflow")
		}
		total += f.Size
		previous = f.Path
		if _, ok := required[f.Path]; ok {
			if f.Size == 0 {
				return fmt.Errorf("empty required adapter file %s", f.Path)
			}
			required[f.Path] = true
		}
	}
	for name, ok := range required {
		if !ok {
			return fmt.Errorf("missing required adapter file %s", name)
		}
	}
	if total <= 0 || total != m.TotalBytes {
		return errors.New("manifest total size mismatch")
	}
	return nil
}
func Encode(m Manifest) ([]byte, string, error) {
	if err := Validate(m); err != nil {
		return nil, "", err
	}
	b, err := json.Marshal(m)
	if err != nil {
		return nil, "", err
	}
	return b, SHA256(b), nil
}
func Parse(b []byte) (Manifest, string, error) {
	var m Manifest
	dec := json.NewDecoder(bytes.NewReader(b))
	dec.DisallowUnknownFields()
	if err := dec.Decode(&m); err != nil {
		return m, "", err
	}
	var extra any
	if err := dec.Decode(&extra); err != io.EOF {
		return m, "", errors.New("trailing manifest content")
	}
	canonical, digest, err := Encode(m)
	if err != nil {
		return m, "", err
	}
	if !bytes.Equal(canonical, b) {
		return m, "", errors.New("manifest is not canonical JSON")
	}
	return m, digest, nil
}
func HashReader(r io.Reader) (string, int64, error) {
	h := sha256.New()
	n, err := io.Copy(h, r)
	return hex.EncodeToString(h.Sum(nil)), n, err
}
func HashFile(name string) (string, int64, error) {
	info, err := os.Lstat(name)
	if err != nil {
		return "", 0, err
	}
	if !info.Mode().IsRegular() {
		return "", 0, fmt.Errorf("not a regular file: %s", name)
	}
	f, err := os.Open(name)
	if err != nil {
		return "", 0, err
	}
	defer f.Close()
	return HashReader(f)
}
func Build(root string) (Manifest, []byte, string, error) {
	m := Manifest{SchemaVersion: 1, ArtifactKind: "adapter", Files: []File{}}
	info, err := os.Lstat(root)
	if err != nil {
		return m, nil, "", err
	}
	if !info.IsDir() || info.Mode()&os.ModeSymlink != 0 {
		return m, nil, "", errors.New("artifact root must be a real directory")
	}
	err = filepath.WalkDir(root, func(name string, d os.DirEntry, walkErr error) error {
		if walkErr != nil {
			return walkErr
		}
		if name == root {
			return nil
		}
		if d.Type()&os.ModeSymlink != 0 {
			return fmt.Errorf("symlink is not allowed: %s", name)
		}
		if d.IsDir() {
			return nil
		}
		if !d.Type().IsRegular() {
			return fmt.Errorf("nonregular artifact entry: %s", name)
		}
		rel, e := filepath.Rel(root, name)
		if e != nil {
			return e
		}
		rel = filepath.ToSlash(rel)
		if !ValidPath(rel) {
			return fmt.Errorf("unsafe artifact path: %s", rel)
		}
		digest, size, e := HashFile(name)
		if e != nil {
			return e
		}
		m.Files = append(m.Files, File{Path: rel, Size: size, SHA256: digest})
		if len(m.Files) > maxFiles {
			return errors.New("too many artifact files")
		}
		return nil
	})
	if err != nil {
		return m, nil, "", err
	}
	sort.Slice(m.Files, func(i, j int) bool { return m.Files[i].Path < m.Files[j].Path })
	for _, f := range m.Files {
		m.TotalBytes += f.Size
	}
	m.FileCount = len(m.Files)
	b, digest, err := Encode(m)
	return m, b, digest, err
}

// Prefix is content-addressed, and never points to a mutable "latest" location.
func Prefix(modelID, versionID, digest string) (string, error) {
	if !uuidPattern.MatchString(modelID) || !uuidPattern.MatchString(versionID) || !ValidDigest(digest) {
		return "", errors.New("invalid output identity")
	}
	return fmt.Sprintf("models/%s/versions/%s/outputs/%s/", modelID, versionID, digest), nil
}
