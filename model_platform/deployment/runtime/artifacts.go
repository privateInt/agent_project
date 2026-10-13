package runtime

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"io"
	"os"
	"path"
	"path/filepath"
	"regexp"
	"strings"

	"model-platform/internal/storage"
)

type Fetcher interface {
	Prepare(context.Context, Plan) error
}
type FetchFunc func(context.Context, Plan) error

func (f FetchFunc) Prepare(ctx context.Context, p Plan) error { return f(ctx, p) }

type ArtifactStore interface {
	Bucket() string
	DownloadObject(context.Context, string, string) (int64, error)
}
type SnapshotFetcher struct {
	Store ArtifactStore
	Root  string
}
type snapshotFile struct {
	Path   string `json:"path"`
	SHA256 string `json:"sha256"`
	Size   int64  `json:"size_bytes"`
}
type snapshotManifest struct {
	Files []snapshotFile `json:"files"`
}

func parseSnapshot(data []byte, source Source) (snapshotManifest, error) {
	var m snapshotManifest
	hash := sha256.Sum256(data)
	if hex.EncodeToString(hash[:]) != source.SHA256 {
		return m, fmt.Errorf("snapshot manifest hash mismatch")
	}
	if json.Unmarshal(data, &m) != nil || len(m.Files) == 0 {
		return m, fmt.Errorf("invalid snapshot manifest")
	}
	seen := map[string]bool{}
	total := int64(0)
	digest := regexp.MustCompile(`^[a-f0-9]{64}$`)
	config, weights := false, false
	for _, f := range m.Files {
		if f.Path == "" || f.Path == "." || path.Clean(f.Path) != f.Path || strings.HasPrefix(f.Path, "/") || strings.HasPrefix(f.Path, "../") || strings.ContainsAny(f.Path, "\\\x00") || f.Path == "_manifest.json" || seen[f.Path] {
			return m, fmt.Errorf("unsafe or duplicate snapshot path")
		}
		if !digest.MatchString(f.SHA256) || f.Size < 0 || f.Size > source.SizeBytes-total {
			return m, fmt.Errorf("invalid snapshot file metadata")
		}
		seen[f.Path] = true
		total += f.Size
		config = config || f.Path == "config.json"
		weights = weights || strings.HasSuffix(f.Path, ".safetensors")
	}
	if total != source.SizeBytes || !config || !weights {
		return m, fmt.Errorf("snapshot needs matching size, config.json and safetensors weights")
	}
	// A file cannot also be a parent directory of another manifest entry.
	for name := range seen {
		for parent := path.Dir(name); parent != "."; parent = path.Dir(parent) {
			if seen[parent] {
				return m, fmt.Errorf("snapshot file/directory collision")
			}
		}
	}
	return m, nil
}

func checkFile(name string, f snapshotFile) error {
	info, err := os.Lstat(name)
	if err != nil || !info.Mode().IsRegular() || info.Size() != f.Size {
		return fmt.Errorf("snapshot file size/type mismatch: %s", f.Path)
	}
	r, err := os.Open(name)
	if err != nil {
		return err
	}
	defer r.Close()
	hash := sha256.New()
	if _, err := io.Copy(hash, r); err != nil {
		return err
	}
	if hex.EncodeToString(hash.Sum(nil)) != f.SHA256 {
		return fmt.Errorf("snapshot file hash mismatch: %s", f.Path)
	}
	return nil
}

func validateDirectory(dir string, source Source) error {
	manifestPath := filepath.Join(dir, "_manifest.json")
	info, err := os.Lstat(manifestPath)
	if err != nil || !info.Mode().IsRegular() || info.Size() > 16<<20 {
		return fmt.Errorf("invalid cached manifest")
	}
	data, err := os.ReadFile(manifestPath)
	if err != nil {
		return err
	}
	m, err := parseSnapshot(data, source)
	if err != nil {
		return err
	}
	allowed := map[string]bool{"_manifest.json": true}
	for _, f := range m.Files {
		allowed[f.Path] = true
	}
	if err := filepath.WalkDir(dir, func(name string, entry os.DirEntry, walkErr error) error {
		if walkErr != nil {
			return walkErr
		}
		if entry.IsDir() {
			return nil
		}
		rel, err := filepath.Rel(dir, name)
		if err != nil {
			return err
		}
		if entry.Type()&os.ModeSymlink != 0 || !allowed[filepath.ToSlash(rel)] {
			return fmt.Errorf("unexpected cached snapshot file")
		}
		return nil
	}); err != nil {
		return err
	}
	for _, f := range m.Files {
		name := filepath.Join(dir, filepath.FromSlash(f.Path))
		// Reject symlink parents as well as symlink leaf entries.
		for parent := filepath.Dir(name); parent != filepath.Dir(dir); parent = filepath.Dir(parent) {
			info, err := os.Lstat(parent)
			if err != nil || !info.IsDir() || info.Mode()&os.ModeSymlink != 0 {
				return fmt.Errorf("unsafe snapshot directory")
			}
		}
		if err := checkFile(name, f); err != nil {
			return err
		}
	}
	return nil
}

// Prepare materializes only the authenticated manifest entries. A partial
// directory is never mounted into vLLM, and every download is checked locally.
func (f SnapshotFetcher) Prepare(ctx context.Context, p Plan) error {
	if err := f.prepareBase(ctx, p); err != nil {
		return err
	}
	if p.Spec.Profile == RerankerProfile {
        _, err := ValidateRerankerSnapshot(f.Root, p)
        return err
    }
	if p.Spec.Profile == EmbeddingProfile {
        _, err := ValidateEmbeddingSnapshot(f.Root, p)
        return err
    }
	if p.Spec.Profile == SuryaProfile {
		return validateSuryaSnapshot(filepath.Join(f.Root, p.DeploymentID, "model"))
	}
	if p.Source.Adapter != nil {
		return f.prepareAdapter(ctx, p)
	}
	return nil
}

func (f SnapshotFetcher) prepareBase(ctx context.Context, p Plan) error {
	bucket, prefix, err := storage.ParseS3URI(p.Source.URI)
	if err != nil || bucket != f.Store.Bucket() || !strings.HasSuffix(prefix, "/") {
		return fmt.Errorf("snapshot is outside the configured artifact bucket/prefix")
	}
	target := filepath.Join(f.Root, p.DeploymentID, "model")
	if info, err := os.Lstat(target); err == nil {
		if !info.IsDir() || info.Mode()&os.ModeSymlink != 0 {
			return fmt.Errorf("unsafe serving cache path")
		}
		return validateDirectory(target, p.Source)
	} else if !os.IsNotExist(err) {
		return err
	}
	if err := os.MkdirAll(filepath.Dir(target), 0o755); err != nil {
		return err
	}
	temp, err := os.MkdirTemp(filepath.Dir(target), ".preparing-")
	if err != nil {
		return err
	}
	defer os.RemoveAll(temp)
	manifestPath := filepath.Join(temp, "_manifest.json")
	// DownloadObject does not load model-sized objects into RAM.
	if _, err := f.Store.DownloadObject(ctx, prefix+"_manifest.json", manifestPath); err != nil {
		return err
	}
	info, err := os.Stat(manifestPath)
	if err != nil || info.Size() > 16<<20 {
		return fmt.Errorf("snapshot manifest exceeds 16 MiB")
	}
	data, err := os.ReadFile(manifestPath)
	if err != nil {
		return err
	}
	manifest, err := parseSnapshot(data, p.Source)
	if err != nil {
		return err
	}
	for _, entry := range manifest.Files {
		if err := ctx.Err(); err != nil {
			return err
		}
		name := filepath.Join(temp, filepath.FromSlash(entry.Path))
		if _, err := f.Store.DownloadObject(ctx, prefix+entry.Path, name); err != nil {
			return err
		}
		if err := checkFile(name, entry); err != nil {
			return err
		}
	}
	return os.Rename(temp, target)
}
