package main

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"time"

	"data-platform/internal/canonical"
)

const parserPython = "/usr/local/bin/python"
const parserScript = "/app/parser.py"
const diagnosticPrefix = "DATA_PARSER_DIAGNOSTIC_V1 "

type parserDiagnostic struct {
	Reason    string `json:"reason"`
	Stage     string `json:"stage"`
	Exception string `json:"exception"`
	Errno     int    `json:"errno"`
	Line      int    `json:"line"`
	ExitCode  int    `json:"-"`
}

func (d *parserDiagnostic) Error() string {
	return fmt.Sprintf("reason=%s stage=%s exception=%s errno=%d line=%d exit_code=%d", d.Reason, d.Stage, d.Exception, d.Errno, d.Line, d.ExitCode)
}

type boundedCapture struct{ data []byte }

func (b *boundedCapture) Write(p []byte) (int, error) {
	n := len(p)
	room := (8 << 10) - len(b.data)
	if room > len(p) {
		room = len(p)
	}
	if room > 0 {
		b.data = append(b.data, p[:room]...)
	}
	return n, nil
}

func parserCommand(ctx context.Context, python, script, input, spec, output string) *exec.Cmd {
	// Absolute interpreter + isolated mode make startup independent of inherited
	// PATH, PYTHONPATH, user-site modules and the current working directory.
	cmd := exec.CommandContext(ctx, python, "-I", "-B", "-u", script, "--input", input, "--spec", spec, "--output", output)
	cmd.Env = []string{"PATH=/usr/local/bin:/usr/bin:/bin", "LC_ALL=C.UTF-8"}
	cmd.Stdout = io.Discard
	return cmd
}
func diagnoseParser(ctx context.Context, err error, stderr []byte) *parserDiagnostic {
	d := &parserDiagnostic{Reason: "process_failed", Stage: "startup", Exception: "none", ExitCode: -1}
	if errors.Is(ctx.Err(), context.DeadlineExceeded) {
		d.Reason = "timeout"
		return d
	}
	if errors.Is(ctx.Err(), context.Canceled) {
		d.Reason = "cancelled"
		return d
	}
	var exited *exec.ExitError
	if !errors.As(err, &exited) {
		if errors.Is(err, os.ErrNotExist) {
			d.Reason = "interpreter_missing"
		} else if errors.Is(err, os.ErrPermission) {
			d.Reason = "interpreter_permission"
		} else {
			d.Reason = "process_start_failed"
		}
		return d
	}
	d.ExitCode = exited.ExitCode()
	// Only fixed enums/numbers from the parser are eligible for public logs.
	// Never log raw stderr, exception messages, source text, paths or env values.
	for _, line := range strings.Split(string(stderr), "\n") {
		if !strings.HasPrefix(line, diagnosticPrefix) {
			continue
		}
		var parsed parserDiagnostic
		if json.Unmarshal([]byte(strings.TrimPrefix(line, diagnosticPrefix)), &parsed) != nil {
			continue
		}
		if !oneOf(parsed.Reason, "permission_denied", "file_missing", "output_exists", "invalid_utf8", "invalid_spec", "parse_rejected", "dependency_unavailable", "memory_error", "io_error", "internal_error") {
			continue
		}
		if !oneOf(parsed.Stage, "read_source", "read_spec", "parse", "write_output") {
			continue
		}
		if !oneOf(parsed.Exception, "PermissionError", "FileNotFoundError", "FileExistsError", "UnicodeDecodeError", "JSONDecodeError", "KeyError", "TypeError", "ParseError", "ModuleNotFoundError", "ImportError", "MemoryError", "OSError", "other") {
			continue
		}
		if parsed.Errno < 0 || parsed.Errno > 4096 || parsed.Line < 0 || parsed.Line > 100000 {
			continue
		}
		parsed.ExitCode = d.ExitCode
		return &parsed
	}
	raw := string(stderr)
	if strings.Contains(raw, "can't open file") {
		d.Reason = "script_unavailable"
		if strings.Contains(raw, "Permission denied") {
			d.Reason = "script_permission"
		}
		if strings.Contains(raw, "No such file") {
			d.Reason = "script_missing"
		}
	} else if strings.Contains(raw, "ModuleNotFoundError:") || strings.Contains(raw, "ImportError:") {
		d.Reason = "dependency_unavailable"
	} else if strings.Contains(raw, "SyntaxError:") {
		d.Reason = "script_syntax_error"
	}
	return d
}
func oneOf(value string, allowed ...string) bool {
	for _, v := range allowed {
		if value == v {
			return true
		}
	}
	return false
}
func invokeParser(ctx context.Context, python, script, input, spec, output string) error {
	cmd := parserCommand(ctx, python, script, input, spec, output)
	captured := &boundedCapture{}
	cmd.Stderr = captured
	if err := cmd.Run(); err != nil {
		return diagnoseParser(ctx, err, captured.data)
	}
	return nil
}

// This path uses only built-in synthetic text and temporary files. It never
// connects to infrastructure or reads an Operation/input supplied by a caller.
func runtimeSelfTest(ctx context.Context, python, script string) error {
	dir, e := os.MkdirTemp("", "refinement-runtime-check-")
	if e != nil {
		return fmt.Errorf("self-test temporary directory unavailable")
	}
	defer os.RemoveAll(dir)
	input, output, specPath := filepath.Join(dir, "source"), filepath.Join(dir, "canonical.json"), filepath.Join(dir, "spec.json")
	data := []byte("원본 문서\r\n  공백과 줄바꿈을 그대로 보존합니다.\t123\n")
	sum := sha256.Sum256(data)
	spec := canonical.Spec{DocumentID: "11111111-1111-4111-8111-111111111111", SHA256: hex.EncodeToString(sum[:]), Format: "text", RulesVersion: canonical.Rules, ImageID: "sha256:" + strings.Repeat("a", 64)}
	encoded, _ := json.Marshal(spec)
	if e = os.WriteFile(input, data, 0600); e != nil {
		return fmt.Errorf("self-test input write failed")
	}
	if e = os.WriteFile(specPath, encoded, 0600); e != nil {
		return fmt.Errorf("self-test spec write failed")
	}
	parseCtx, cancel := context.WithTimeout(ctx, 30*time.Second)
	defer cancel()
	if e = invokeParser(parseCtx, python, script, input, specPath, output); e != nil {
		return fmt.Errorf("runtime self-test: %w", e)
	}
	f, e := os.Open(output)
	if e != nil {
		return fmt.Errorf("self-test output unavailable")
	}
	defer f.Close()
	raw, e := io.ReadAll(io.LimitReader(f, canonical.MaxBytes+1))
	if e != nil || int64(len(raw)) > canonical.MaxBytes {
		return fmt.Errorf("self-test output size invalid")
	}
	if _, e = canonical.Validate(raw, spec); e != nil {
		return fmt.Errorf("self-test canonical validation failed")
	}
	var parsed struct {
		Blocks []struct {
			Text string `json:"text"`
		} `json:"blocks"`
	}
	if json.Unmarshal(raw, &parsed) != nil || len(parsed.Blocks) != 1 || parsed.Blocks[0].Text != strings.ReplaceAll(string(data), "\r\n", "\n") {
		return fmt.Errorf("self-test text/indentation changed")
	}
	fmt.Printf("PASS: refinement runtime self-test uid=%d; Go -> isolated Python CLI -> canonical validation\n", os.Geteuid())
	return nil
}
