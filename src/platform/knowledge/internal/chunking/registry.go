package chunking

// Strategy controls text boundaries. Canonical decoding, structural guards,
// source spans, IDs and coverage validation are shared by every strategy.
type Strategy interface {
	Describe() Descriptor
	FlushBefore(segments []segment, next block, options Options) bool
	Split(text []rune, segments []segment, options Options) [][2]int
}

type recursiveStrategy struct{}

func (recursiveStrategy) Describe() Descriptor {
	return Descriptor{"recursive_character_v1", PreviousImplementation, "문자 기반 재귀 분할", "문단·줄바꿈·공백을 우선하며 큰 표·코드는 구조 경계로 나눕니다.", Options{1000, 100}, 100, 8000}
}
func (recursiveStrategy) FlushBefore([]segment, block, Options) bool {
	return false
}
func (recursiveStrategy) Split(text []rune, _ []segment, options Options) [][2]int {
	return split(text, options.MaxCharacters, options.OverlapCharacters)
}

type structureStrategy struct{ version string }

func (s structureStrategy) Describe() Descriptor {
	return Descriptor{"structure_v1", s.version, "문서 구조 기반 분할", "제목·블록 경계를 우선하고 같은 본문 안에서 겹침을 유지합니다. 큰 표·코드는 구조 경계로 나눕니다.", Options{1000, 100}, 100, 8000}
}
func (s structureStrategy) FlushBefore(segments []segment, next block, options Options) bool {
	if s.version != Implementation {
		currentLength := 0
		if len(segments) > 0 {
			currentLength = segments[len(segments)-1].End
		}
		return next.Kind == "heading" || currentLength+2+len(next.Text) > options.MaxCharacters
	}
	// Keep consecutive headings with their following body. A recognized heading
	// after body text starts a new section; size windows are split together later.
	// Once body has appeared, the next heading flushes before it is appended;
	// therefore a nonempty buffer ending in a heading contains only headings.
	return next.Kind == "heading" && len(segments) > 0 && segments[len(segments)-1].Block.Kind != "heading"
}
func (s structureStrategy) Split(text []rune, segments []segment, options Options) [][2]int {
	if s.version == Implementation {
		return splitStructure(text, segments, options)
	}
	return split(text, options.MaxCharacters, options.OverlapCharacters)
}

// Static registration deliberately excludes arbitrary module/plugin loading.
var registry = []Strategy{recursiveStrategy{}, structureStrategy{Implementation}}

func lookup(id string) (Strategy, bool) {
	for _, strategy := range registry {
		if strategy.Describe().ID == id {
			return strategy, true
		}
	}
	return nil, false
}

// Select the algorithm pinned in a stored spec. Recursive output remains at .2;
// the new version changes only structure_v1. Legacy retries keep their policy.
func lookupVersion(id, version string) (Strategy, bool) {
	strategy, ok := lookup(id)
	if !ok {
		return nil, false
	}
	if version != LegacyImplementation && version != PreviousImplementation && !(id == "structure_v1" && version == Implementation) {
		return nil, false
	}
	if id == "structure_v1" {
		return structureStrategy{version}, true
	}
	return strategy, true
}

func Describe() []Descriptor {
	out := make([]Descriptor, 0, len(registry))
	for _, strategy := range registry {
		out = append(out, strategy.Describe())
	}
	return out
}
