package main

import (
	"bytes"
	"context"
	"errors"
	"fmt"
	"log"
	"os"
	"os/exec"
	"strings"
	"testing"
)

func TestStepTimingPreservesErrorAndDoesNotLogErrorContents(t *testing.T) {
	var output bytes.Buffer
	previous := log.Writer()
	log.SetOutput(&output)
	defer log.SetOutput(previous)
	want := errors.New("private source or credential")
	if got := timedStep("operation-id", "parser_process", func() error { return want }); got != want {
		t.Fatal(got)
	}
	text := output.String()
	if !strings.Contains(text, "stage=parser_process elapsed_ms=") || strings.Contains(text, "private") {
		t.Fatal(text)
	}
}

func TestParserCommandUsesIsolatedAbsoluteInterpreter(t *testing.T) {
	t.Setenv("MINIO_SECRET_KEY", "fixture-secret")
	t.Setenv("PYTHONPATH", "/untrusted")
	c := parserCommand(context.Background(), parserPython, parserScript, "/tmp/input", "/tmp/spec", "/tmp/output")
	if c.Path != parserPython || strings.Join(c.Args[1:4], " ") != "-I -B -u" {
		t.Fatal("interpreter path/isolation not fixed", c.Args)
	}
	for _, v := range c.Env {
		if strings.Contains(v, "MINIO") || strings.Contains(v, "PYTHONPATH") || strings.Contains(v, "fixture-secret") {
			t.Fatal("inherited credentials/startup settings")
		}
	}
}
func TestParserDiagnosticHelper(t *testing.T) {
	if os.Getenv("DATA_PARSER_TEST_HELPER") != "1" {
		return
	}
	fmt.Fprintln(os.Stderr, "DATA_PARSER_DIAGNOSTIC_V1 {\"reason\":\"permission_denied\",\"stage\":\"read_source\",\"exception\":\"PermissionError\",\"errno\":13,\"line\":99}")
	os.Exit(1)
}
func TestParserDiagnosticsPreserveReasonWithoutRawStderr(t *testing.T) {
	exe, e := os.Executable()
	if e != nil {
		t.Fatal(e)
	}
	cmd := exec.Command(exe, "-test.run=^TestParserDiagnosticHelper$")
	cmd.Env = []string{"DATA_PARSER_TEST_HELPER=1"}
	raw, err := cmd.CombinedOutput()
	if err == nil {
		t.Fatal("helper should fail")
	}
	d := diagnoseParser(context.Background(), err, raw)
	if d.Reason != "permission_denied" || d.Stage != "read_source" || d.Errno != 13 || d.ExitCode != 1 {
		t.Fatal(d)
	}
	hostile := []byte(diagnosticPrefix + `{"reason":"private document content","stage":"parse","exception":"PermissionError"}`)
	d = diagnoseParser(context.Background(), err, hostile)
	if d.Reason != "process_failed" || strings.Contains(d.Error(), "private document") {
		t.Fatal("unvalidated stderr was exposed", d)
	}
	d = diagnoseParser(context.Background(), err, []byte("python: can't open file '/app/parser.py': [Errno 13] Permission denied"))
	if d.Reason != "script_permission" {
		t.Fatal(d)
	}
}
func TestParserStartTimeoutAndCaptureLimit(t *testing.T) {
	d := diagnoseParser(context.Background(), os.ErrNotExist, nil)
	if d.Reason != "interpreter_missing" {
		t.Fatal(d)
	}
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	d = diagnoseParser(ctx, errors.New("irrelevant"), nil)
	if d.Reason != "cancelled" {
		t.Fatal(d)
	}
	b := &boundedCapture{}
	data := []byte(strings.Repeat("s", 20<<10))
	n, e := b.Write(data)
	if e != nil || n != len(data) || len(b.data) != 8<<10 {
		t.Fatal("stderr capture is not bounded")
	}
}
