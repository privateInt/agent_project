package chunking

import (
	"encoding/json"
	"fmt"
	"strings"
	"testing"
)

// Validate observable output against source bytes, including Unicode offsets,
// full coverage, metadata, size limits, persistence and repeatability.
func checkedTextBuild(t *testing.T, spec Spec, raws map[string][]byte) *Result {
	t.Helper()
	r, err := Build(spec, raws)
	if err != nil {
		t.Fatal(err)
	}
	encoded, _ := json.Marshal(r)
	if _, err = Verify(encoded, spec); err != nil {
		t.Fatal(err)
	}
	again, err := Build(spec, raws)
	if err != nil || HashJSON(again) != HashJSON(r) {
		t.Fatal("result changed on retry", err)
	}
	for _, in := range spec.Documents {
		d, err := readDocument(raws[in.CanonicalID], in)
		if err != nil {
			t.Fatal(err)
		}
		byID, seen := map[string]block{}, map[string][]bool{}
		for _, b := range d.Blocks {
			byID[b.ID] = b
			seen[b.ID] = make([]bool, len(b.Text))
		}
		for _, c := range r.Chunks {
			if c.CanonicalID != in.CanonicalID {
				continue
			}
			text := []rune(c.Text)
			if len(text) > spec.Chunker.Options.MaxCharacters {
				t.Fatal("maximum exceeded")
			}
			newSource := 0
			for _, sp := range c.Spans {
				b := byID[sp.BlockID]
				if string(b.Text[sp.Start:sp.End]) != string(text[sp.ChunkStart:sp.ChunkEnd]) || string(b.Metadata) != string(sp.Metadata) {
					t.Fatal("source text or metadata changed", sp.BlockID)
				}
				for at := sp.Start; at < sp.End; at++ {
					if !seen[sp.BlockID][at] {
						newSource++
					}
					seen[sp.BlockID][at] = true
				}
			}
			if newSource == 0 {
				t.Fatal("overlap-only chunk emitted")
			}
		}
		excluded := map[string]bool{}
		for _, ex := range r.Exclusions {
			if ex.CanonicalID == in.CanonicalID {
				excluded[ex.BlockID] = true
			}
		}
		for id, coverage := range seen {
			if excluded[id] {
				continue
			}
			for _, covered := range coverage {
				if !covered {
					t.Fatal("source lost", id)
				}
			}
		}
	}
	return r
}

func TestContextOverlapAcrossShortBlocks(t *testing.T) {
	for _, overlap := range []int{0, 1, 10, 49} {
		t.Run(fmt.Sprint(overlap), func(t *testing.T) {
			blocks := []map[string]any{}
			for i := 0; i < 35; i++ {
				blocks = append(blocks, map[string]any{"type": "text", "text": fmt.Sprintf("%02d %s", i, strings.Repeat("가😀ab", 15))})
			}
			spec, raw := fixture(t, blocks, "structure_v1")
			spec.Chunker, _ = Normalize("structure_v1", []byte(fmt.Sprintf(`{"max_characters":100,"overlap_characters":%d}`, overlap)))
			r := checkedTextBuild(t, spec, raw)
			if len(r.Chunks) < 2 {
				t.Fatal("no size split")
			}
			shared := false
			for i := 1; i < len(r.Chunks); i++ {
				left, right := r.Chunks[i-1], r.Chunks[i]
				previous, next := []rune(left.Text), []rune(right.Text)
				if string(previous[len(previous)-overlap:]) != string(next[:overlap]) {
					t.Fatal("configured overlap missing")
				}
				for _, a := range left.Spans {
					for _, b := range right.Spans {
						if a.BlockID == b.BlockID && max(a.Start, b.Start) < min(a.End, b.End) {
							shared = true
						}
					}
				}
			}
			if shared != (overlap > 0) {
				t.Fatal("source overlap disagrees with option")
			}
		})
	}
}

func TestContextConsecutiveHeadingsJoinBody(t *testing.T) {
	for _, body := range []string{"입장료는 무료입니다.", strings.Repeat("가나다라😀", 55)} {
		spec, raw := fixture(t, []map[string]any{
			{"type": "heading", "text": "2. Materials and methods"},
			{"type": "heading", "text": "2.1. Company description"},
			{"type": "paragraph", "text": body},
		}, "structure_v1")
		r := checkedTextBuild(t, spec, raw)
		first := r.Chunks[0]
		found := map[string]bool{}
		for _, sp := range first.Spans {
			found[sp.BlockID] = true
		}
		if !found["b1"] || !found["b2"] || !found["b3"] {
			t.Fatal("headings orphaned despite available space")
		}
	}
}

func TestContextNeverCrossesHardBoundaries(t *testing.T) {
	for _, barrier := range []map[string]any{
		{"type": "heading", "text": "新しい節"},
		{"type": "table", "text": "| header |\n| --- |\n| value |\n"},
		{"type": "code", "text": "x = 1\n"},
		{"type": "image", "text": "unread", "recognition_status": "unread"},
		{"type": "text", "text": " \n"},
	} {
		t.Run(fmt.Sprint(barrier["type"], barrier["text"]), func(t *testing.T) {
			spec, raw := fixture(t, []map[string]any{
				{"type": "paragraph", "text": strings.Repeat("앞😀", 80)}, barrier,
				{"type": "paragraph", "text": strings.Repeat("뒤🦊", 80)},
			}, "structure_v1")
			r := checkedTextBuild(t, spec, raw)
			for _, c := range r.Chunks {
				ids := map[string]bool{}
				for _, sp := range c.Spans {
					ids[sp.BlockID] = true
				}
				if ids["b1"] && (ids["b2"] || ids["b3"]) {
					t.Fatal("prior section carried across boundary")
				}
				if barrier["type"] == "table" || barrier["type"] == "code" {
					if ids["b2"] && (len(ids) != 1 || c.Kind != barrier["type"]) {
						t.Fatal("structure mixed with body")
					}
				}
			}
		})
	}
}

func TestContextHeadingClassificationRemainsCanonical(t *testing.T) {
	spec, raw := fixture(t, []map[string]any{
		{"type": "text", "text": "앞 문단"}, {"type": "text", "text": "4. Conclusion"}, {"type": "text", "text": "뒤 문단"},
	}, "structure_v1")
	r := checkedTextBuild(t, spec, raw)
	if len(r.Chunks) != 1 || len(r.Chunks[0].Spans) != 3 {
		t.Fatal("inferred heading not present in canonical")
	}
}

func TestContextOversizedHeadingsAndExactLimit(t *testing.T) {
	for _, blocks := range [][]map[string]any{
		{{"type": "heading", "text": strings.Repeat("제😀", 70)}, {"type": "heading", "text": "소제목"}, {"type": "text", "text": "짧은 사실"}},
		{{"type": "heading", "text": strings.Repeat("제", 48)}, {"type": "heading", "text": strings.Repeat("목", 48)}, {"type": "text", "text": "본문"}},
		{{"type": "text", "text": strings.Repeat("가", 100)}, {"type": "text", "text": "나"}},
		{{"type": "text", "text": strings.Repeat("가", 49)}, {"type": "text", "text": strings.Repeat("나", 49)}},
		{{"type": "heading", "text": "문서 끝의 제목"}},
	} {
		spec, raw := fixture(t, blocks, "structure_v1")
		checkedTextBuild(t, spec, raw)
	}
}

func TestContextDocumentBoundaryAndPolicyIdentity(t *testing.T) {
	spec, raw := fixture(t, []map[string]any{{"type": "text", "text": strings.Repeat("source 😀 ", 24)}}, "structure_v1")
	second := spec.Documents[0]
	second.CanonicalID = "canonical-b"
	raw[second.CanonicalID] = raw[spec.Documents[0].CanonicalID]
	spec, _ = NewSpec([]Input{spec.Documents[0], second}, spec.Chunker)
	r := checkedTextBuild(t, spec, raw)
	groups := map[string][]Chunk{}
	for _, c := range r.Chunks {
		groups[c.CanonicalID] = append(groups[c.CanonicalID], c)
	}
	a, b := groups[spec.Documents[0].CanonicalID], groups[second.CanonicalID]
	if len(a) != len(b) {
		t.Fatal("context carried between documents")
	}
	for i := range a {
		if a[i].Text != b[i].Text || a[i].ID == b[i].ID {
			t.Fatal("document identity lost")
		}
	}
	old, err := normalizeVersion("structure_v1", mustOptions(spec.Chunker.Options), PreviousImplementation)
	if err != nil || old.PolicyHash == spec.Chunker.PolicyHash {
		t.Fatal("changed algorithm reused policy identity")
	}
}

func TestContextVersionRegistryAndRejection(t *testing.T) {
	for _, d := range Describe() {
		s, err := Normalize(d.ID, nil)
		if err != nil || s.Version != d.Version {
			t.Fatal("descriptor differs from submission", err)
		}
		if d.ID == "structure_v1" && s.Version != Implementation {
			t.Fatal("new structure not selected")
		}
		if d.ID == "recursive_character_v1" && s.Version != PreviousImplementation {
			t.Fatal("recursive identity changed")
		}
	}
	for _, item := range [][2]string{{"structure_v1", "future"}, {"recursive_character_v1", Implementation}, {"unknown", PreviousImplementation}} {
		if _, err := normalizeVersion(item[0], nil, item[1]); err == nil {
			t.Fatal("unsupported algorithm version accepted")
		}
	}
	spec, raw := fixture(t, []map[string]any{{"type": "text", "text": "short fact"}}, "structure_v1")
	r := checkedTextBuild(t, spec, raw)
	encoded, _ := json.Marshal(r)
	spec.Chunker.Version = "future"
	if _, err := Verify(encoded, spec); err == nil {
		t.Fatal("unknown stored version accepted")
	}
}

// These complete result hashes were generated with the unmodified .2 source,
// before this fix. They pin legacy policy IDs, chunk IDs, spans and JSON bytes.
func TestContextHistoricalResultsRemainByteIdentical(t *testing.T) {
	for _, tc := range []struct{ id, version, hash string }{
		{"structure_v1", LegacyImplementation, "b48ce019e9fffbc9aeb99e74aeabd0069eb42056f6db093ececd2f9c24a412be"},
		{"structure_v1", PreviousImplementation, "7d340c0774fd25295ba5885155d9211f273495423128ea4968153e949100e760"},
		{"recursive_character_v1", LegacyImplementation, "cc373b1747cbf7e10d22de7456acd70ec07ad253b46acde948a1216f69478ba9"},
		{"recursive_character_v1", PreviousImplementation, "bdd401f86cdb06032820889211bf1c157cb6204b2540eb42be328553c7690fad"},
	} {
		t.Run(tc.id+"/"+tc.version, func(t *testing.T) {
			spec, raw := fixture(t, []map[string]any{
				{"type": "heading", "text": "Parent heading"},
				{"type": "heading", "text": "Child heading"},
				{"type": "text", "text": strings.Repeat("가😀ab ", 16)},
				{"type": "text", "text": strings.Repeat("다🦊ef ", 16)},
				{"type": "heading", "text": "Next section"},
				{"type": "text", "text": strings.Repeat("words and lines\n", 18)},
				{"type": "table", "text": "| header |\n| --- |\n| value |\n"},
				{"type": "code", "text": "x = 1\n"},
			}, tc.id)
			var err error
			spec.Chunker, err = normalizeVersion(tc.id, mustOptions(spec.Chunker.Options), tc.version)
			if err != nil {
				t.Fatal(err)
			}
			r := checkedTextBuild(t, spec, raw)
			if HashJSON(r) != tc.hash {
				t.Fatal("historical result bytes changed")
			}
		})
	}
}

func TestContextPreviousStructureFragmentsStillVerify(t *testing.T) {
	for _, tc := range []struct{ id, hash string }{
		{"structure_v1", "1d319c7d218c56bf680e9b0cf685045a4ff0848121acc60b720e2c15f837f7bb"},
		{"recursive_character_v1", "bc496465ab6f241cc5dd7fb2b9990fc35f95f376475a84801915e5d4c924c333"},
	} {
		spec, raw := fixture(t, []map[string]any{{"type": "code", "text": strings.Repeat("print(1)\n", 30)}}, tc.id)
		spec.Chunker, _ = normalizeVersion(tc.id, mustOptions(spec.Chunker.Options), PreviousImplementation)
		r := checkedTextBuild(t, spec, raw)
		if HashJSON(r) != tc.hash || len(r.Chunks) != 3 {
			t.Fatal("stored .2 fragments changed or rejected")
		}
	}
}
