package main

import "strings"

// This exclusion is scoped to the reviewed immutable Surya snapshot. Arbitrary
// repositories/revisions retain every file; do not infer dependencies by suffix.
func selectSnapshotFiles(repo, revision string, files []struct {
	Filename string `json:"rfilename"`
}) ([]struct {
	Filename string `json:"rfilename"`
}, string, []string) {
	if repo != "datalab-to/surya-ocr-2" || revision != "3b3d4cdf88d6928b0acdc75181b13206ea67c4a3" {
		return files, "", nil
	}
	selected := make([]struct {
		Filename string `json:"rfilename"`
	}, 0, len(files))
	var excluded []string
	for _, file := range files {
		name := file.Filename
		if strings.HasPrefix(name, "assets/") || strings.HasPrefix(name, ".eval_results/") || name == ".gitattributes" || name == "datalab-logo.png" {
			excluded = append(excluded, name)
		} else {
			selected = append(selected, file)
		}
	}
	return selected, "surya-runtime-v1", excluded
}
