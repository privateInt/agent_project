package main

import (
	"encoding/json"
	"os"
	"reflect"
	"testing"
)

func TestReviewedSuryaSnapshotKeepsEveryRuntimeFileAndLicense(t *testing.T) {
	raw, err := os.ReadFile("testdata/surya-files.json")
	if err != nil {
		t.Fatal(err)
	}
	var info hfModelInfo
	if err := json.Unmarshal(raw, &info); err != nil {
		t.Fatal(err)
	}
	selected, policy, excluded := selectSnapshotFiles("datalab-to/surya-ocr-2", info.SHA, info.Siblings)
	if len(info.Siblings) != 42 || len(selected) != 11 || len(excluded) != 31 || policy != "surya-runtime-v1" {
		t.Fatal(len(selected), len(excluded), policy)
	}
	var names []string
	for _, file := range selected {
		names = append(names, file.Filename)
	}
	want := []string{"LICENSE", "README.md", "chat_template.jinja", "config.json", "generation_config.json", "model.safetensors", "preprocessor_config.json", "processor_config.json", "tokenizer.json", "tokenizer_config.json", "video_preprocessor_config.json"}
	if !reflect.DeepEqual(names, want) {
		t.Fatal("runtime file set changed", names)
	}
	for _, identity := range [][2]string{{"other/model", info.SHA}, {"datalab-to/surya-ocr-2", "another-commit"}} {
		files, policy, excluded := selectSnapshotFiles(identity[0], identity[1], info.Siblings)
		if !reflect.DeepEqual(files, info.Siblings) || policy != "" || excluded != nil {
			t.Fatal("unreviewed snapshot was filtered")
		}
	}
}
