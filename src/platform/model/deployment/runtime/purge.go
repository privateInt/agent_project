package runtime

import (
	"context"
	"fmt"
	"github.com/google/uuid"
	"os"
	"path/filepath"
)

// Each deployment owns a separate cache directory. RemoveAll does not follow
// child symlinks, and neither the cache root nor the selected directory may be links.
func (f SnapshotFetcher) Purge(ctx context.Context, id string) error {
	u, e := uuid.Parse(id)
	if e != nil || u == uuid.Nil || u.String() != id || !filepath.IsAbs(f.Root) {
		return fmt.Errorf("invalid cache purge identity")
	}
	if e = ctx.Err(); e != nil {
		return e
	}
	root, e := os.Lstat(f.Root)
	if e != nil {
		return e
	}
	if !root.IsDir() || root.Mode()&os.ModeSymlink != 0 {
		return fmt.Errorf("unsafe cache root")
	}
	target := filepath.Join(f.Root, id)
	info, e := os.Lstat(target)
	if os.IsNotExist(e) {
		return nil
	}
	if e != nil {
		return e
	}
	if !info.IsDir() || info.Mode()&os.ModeSymlink != 0 {
		return fmt.Errorf("unsafe cache directory")
	}
	return os.RemoveAll(target)
}
