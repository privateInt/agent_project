package browser

import (
	"bufio"
	"context"
	"crypto/sha256"
	collectionv1 "data-platform/gen/collection/v1"
	"data-platform/internal/crawl"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"io"
	"os"
	"os/exec"
	"path/filepath"
	"syscall"
	"time"
	"unicode/utf8"
)

type Renderer struct {
	Python, Script, BrowsersPath, PythonPath string
	budget                                   networkBudget
}

func New() *Renderer {
	return &Renderer{Python: "/usr/local/bin/python", Script: "/app/browser.py", BrowsersPath: "/ms-playwright"}
}

type event struct {
	Stage   string `json:"stage"`
	Type    string `json:"type"`
	URL     string `json:"url"`
	Method  string `json:"method"`
	Body    []byte `json:"body"`
	Blocked int32  `json:"blocked"`
}

func (r *Renderer) Render(ctx context.Context, f *crawl.Fetcher, seed string, o crawl.Options, initial crawl.Result) (result crawl.Result, capture *collectionv1.BrowserCapture, err error) {
	bad := func() (crawl.Result, *collectionv1.BrowserCapture, error) {
		return crawl.Result{}, nil, fmt.Errorf("browser rendering failed or exceeded its limits")
	}
	if !crawl.IsHTML(initial.ContentType) || !crawl.InScope(seed, initial.FinalURL) || initial.Size < 0 || initial.Size > o.MaxBytes {
		return bad()
	}
	r.budget.requests++
	r.budget.bytes += initial.Size
	if r.budget.requests > crawl.BrowserRunRequests || r.budget.bytes > crawl.BrowserRunBytes || initial.Size > crawl.BrowserPageBytes {
		return bad()
	}
	page := networkBudget{requests: 1, bytes: initial.Size}
	raw, e := os.ReadFile(initial.Path)
	if e != nil || int64(len(raw)) != initial.Size {
		return bad()
	}
	ctx, cancel := context.WithTimeout(ctx, time.Duration(o.TimeoutSeconds)*time.Second)
	defer cancel()
	directory, e := os.MkdirTemp("", "data-browser-*")
	if e != nil {
		return bad()
	}
	defer os.RemoveAll(directory)
	cmd := exec.CommandContext(ctx, r.Python, "-I", r.Script)
	// The isolated driver cannot inherit MinIO credentials or execution grants.
	// PYTHONPATH is used only by explicitly constructed local test runtimes.
	cmd.Env = []string{"PATH=/usr/local/bin:/usr/bin:/bin", "HOME=" + directory, "TMPDIR=" + directory, "XDG_CACHE_HOME=" + directory, "PLAYWRIGHT_BROWSERS_PATH=" + r.BrowsersPath, "PYTHONUNBUFFERED=1", "PYTHONDONTWRITEBYTECODE=1"}
	if r.PythonPath != "" {
		cmd.Args = []string{r.Python, r.Script}
		cmd.Env = append(cmd.Env, "PYTHONPATH="+r.PythonPath)
	}
	cmd.Dir = directory
	cmd.SysProcAttr = &syscall.SysProcAttr{Setpgid: true}
	cmd.Cancel = func() error { return syscall.Kill(-cmd.Process.Pid, syscall.SIGKILL) }
	cmd.WaitDelay = 2 * time.Second
	stdin, e := cmd.StdinPipe()
	if e != nil {
		return bad()
	}
	stdout, e := cmd.StdoutPipe()
	if e != nil {
		return bad()
	}
	cmd.Stderr = io.Discard
	if e = cmd.Start(); e != nil {
		return bad()
	}
	defer func() {
		stdin.Close()
		if cmd.ProcessState == nil {
			_ = cmd.Cancel()
			_ = cmd.Wait()
		}
	}()
	config := map[string]any{"url": initial.FinalURL, "status": initial.Status, "headers": headers(initial.Headers), "body": raw, "max_bytes": o.MaxBytes, "wait_ms": o.RenderWaitMS, "timeout_ms": int64(o.TimeoutSeconds) * 1000}
	config["headers"].(map[string]string)["content-type"] = initial.ContentType
	writer := json.NewEncoder(stdin)
	if e = writer.Encode(config); e != nil {
		return bad()
	}
	scanner := bufio.NewScanner(stdout)
	scanner.Buffer(make([]byte, 64<<10), 48<<20)
	var body []byte
	var blocked int32
	frames := 0
	for scanner.Scan() {
		frames++
		if frames > 1026 {
			return bad()
		}
		var msg event
		if json.Unmarshal(scanner.Bytes(), &msg) != nil {
			return bad()
		}
		switch msg.Type {
		case "fetch":
			if body != nil || len(msg.URL) > 4096 {
				return bad()
			}
			response, e := r.fetch(ctx, f, seed, msg.URL, msg.Method, o, &page)
			if e != nil {
				return bad()
			}
			if e = writer.Encode(response); e != nil {
				return bad()
			}
		case "result":
			if body != nil || msg.URL != initial.FinalURL || len(msg.Body) == 0 || int64(len(msg.Body)) > o.MaxBytes || !utf8.Valid(msg.Body) {
				return bad()
			}
			body = msg.Body
			blocked = msg.Blocked
		case "error":
			switch msg.Stage {
			case "configuration", "network_isolation", "browser_start", "navigation", "render_wait", "dom_capture":
				return crawl.Result{}, nil, fmt.Errorf("browser stage %s failed", msg.Stage)
			}
			return bad()
		default:
			return bad()
		}
	}
	if scanner.Err() != nil || cmd.Wait() != nil || ctx.Err() != nil || body == nil {
		return bad()
	}
	capture = &collectionv1.BrowserCapture{ResponseSha256: initial.SHA256, ResponseSizeBytes: initial.Size, ResponseContentType: initial.ContentType, Representation: "rendered_dom", NetworkRequests: int32(page.requests), NetworkBytes: page.bytes, BlockedRequests: blocked}
	if crawl.ValidateCapture(o, "page", "text/html; charset=utf-8", capture) != nil {
		return bad()
	}
	file, e := os.CreateTemp("", "data-dom-*")
	if e != nil {
		return bad()
	}
	name := file.Name()
	if _, e = file.Write(body); e != nil {
		file.Close()
		os.Remove(name)
		return bad()
	}
	if e = file.Close(); e != nil {
		os.Remove(name)
		return bad()
	}
	hash := sha256.Sum256(body)
	return crawl.Result{Path: filepath.Clean(name), FinalURL: initial.FinalURL, SHA256: hex.EncodeToString(hash[:]), Size: int64(len(body)), ContentType: "text/html; charset=utf-8", Status: initial.Status}, capture, nil
}
