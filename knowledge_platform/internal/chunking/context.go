package chunking

import "sort"

// splitStructure plans all size windows in one recognized section. Overlap is
// measured in output runes (including the existing two-newline block joiner).
// It never carries text across a section, table/code, exclusion or document
// boundary: Build flushes at those boundaries before calling this function.
func splitStructure(text []rune, segments []segment, options Options) [][2]int {
	maximum, overlap := options.MaxCharacters, options.OverlapCharacters
	blockEnds := make([]int, 0, len(segments))
	bodyStart := len(text)
	for _, s := range segments {
		if s.Block.Kind != "heading" {
			blockEnds = append(blockEnds, s.End)
			if bodyStart == len(text) {
				bodyStart = s.Start
			}
		}
	}
	cuts := boundaries(text, 0, len(text), maximum, []string{"\n\n", "\n", "。", ". ", " ", "\t"})
	var windows [][2]int
	for start := 0; start < len(text); {
		if len(text)-start <= maximum {
			windows = append(windows, [2]int{start, len(text)})
			break
		}
		limit, minimum := start+maximum, start+max(overlap, maximum/2)
		// A boundary immediately after the last window can consist only of
		// the synthetic joiner. Require at least one previously unseen source
		// rune, even when overlap is close to half of the maximum.
		if len(windows) > 0 {
			previousEnd := windows[len(windows)-1][1]
			i := sort.Search(len(segments), func(i int) bool { return segments[i].End > previousEnd })
			if i < len(segments) {
				minimum = max(minimum, previousEnd, segments[i].Start)
			}
		}
		// If the leading headings and some body fit together, do not choose a
		// separator that leaves the headings alone. An oversized heading chain
		// still obeys the maximum and keeps every source character.
		if start < bodyStart && bodyStart < limit {
			minimum = max(minimum, bodyStart)
		}
		end := limit
		if i := sort.SearchInts(blockEnds, limit+1) - 1; i >= 0 && blockEnds[i] > minimum {
			end = blockEnds[i]
		} else if i := sort.SearchInts(cuts, limit+1) - 1; i >= 0 && cuts[i] > minimum {
			end = cuts[i]
		}
		windows = append(windows, [2]int{start, end})
		start = end - overlap
	}
	return windows
}
