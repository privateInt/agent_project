package canonical

import (
	"encoding/json"
	"fmt"
	"golang.org/x/text/unicode/norm"
	"math"
	"reflect"
	"strings"
	"unicode"
)

type tableLocation struct {
	Page        int       `json:"page"`
	Box         []float64 `json:"bbox"`
	Coordinates string    `json:"coordinate_system"`
}
type tableCandidate struct {
	Type     string          `json:"type"`
	Cells    json.RawMessage `json:"cells"`
	Text     string          `json:"text"`
	Location tableLocation   `json:"location"`
}
type tableMetric struct {
	Valid    bool     `json:"valid"`
	Rows     int      `json:"rows"`
	Columns  int      `json:"columns"`
	Nonempty int      `json:"nonempty_cells"`
	Issues   []string `json:"issues"`
}
type TableComparison struct {
	Version string `json:"version"`
	Policy  string `json:"policy"`
	Tables  []struct {
		ID              string           `json:"id"`
		Page            int              `json:"page"`
		Box             []float64        `json:"bbox"`
		Coordinates     string           `json:"coordinate_system"`
		Status          string           `json:"status"`
		Selected        string           `json:"selected"`
		Reasons         []string         `json:"reasons"`
		Native          []tableCandidate `json:"parser_candidates"`
		OCR             []tableCandidate `json:"ocr_candidates"`
		NativeMetrics   []tableMetric    `json:"parser_metrics"`
		OCRMetrics      []tableMetric    `json:"ocr_metrics"`
		BlockIDs        []string         `json:"result_block_ids"`
		DifferenceCount int              `json:"difference_count"`
		Differences     []struct {
			Row    int     `json:"row"`
			Column int     `json:"column"`
			Kind   string  `json:"kind"`
			Native *string `json:"parser"`
			OCR    *string `json:"ocr"`
		} `json:"differences"`
		Truncated bool `json:"differences_truncated"`
	} `json:"tables"`
}

func validTableBox(box []float64) bool {
	if len(box) != 4 {
		return false
	}
	for _, n := range box {
		if math.IsNaN(n) || math.IsInf(n, 0) {
			return false
		}
	}
	return box[0] < box[2] && box[1] < box[3]
}
func equalJSON(a, b json.RawMessage) bool {
	var x, y any
	return json.Unmarshal(a, &x) == nil && json.Unmarshal(b, &y) == nil && reflect.DeepEqual(x, y)
}

type gridValue struct {
	RowSpan, ColumnSpan int
	Text                string
}

func normalizedTableText(s string) string {
	return strings.Map(func(r rune) rune {
		if unicode.IsSpace(r) {
			return -1
		}
		return r
	}, norm.NFC.String(s))
}
func tableGrid(raw json.RawMessage) (map[[2]int]gridValue, bool) {
	var cells []struct {
		Row        *int    `json:"row"`
		Column     *int    `json:"column"`
		RowSpan    *int    `json:"row_span"`
		ColumnSpan *int    `json:"column_span"`
		Text       *string `json:"text"`
	}
	if json.Unmarshal(raw, &cells) != nil || len(cells) == 0 || len(cells) > 20000 {
		return nil, false
	}
	grid := map[[2]int]gridValue{}
	occupied := map[[2]int]bool{}
	rows, columns, nonempty := 0, 0, 0
	for _, c := range cells {
		if c.Row == nil || c.Column == nil || c.Text == nil {
			return nil, false
		}
		r, col, rs, cs := *c.Row, *c.Column, 1, 1
		if c.RowSpan != nil {
			rs = *c.RowSpan
		}
		if c.ColumnSpan != nil {
			cs = *c.ColumnSpan
		}
		if r < 0 || col < 0 || rs < 1 || cs < 1 || r > 1000 || col > 1000 || rs > 1000 || cs > 1000 || r+rs > 1000 || col+cs > 1000 || rs*cs+len(occupied) > 20000 {
			return nil, false
		}
		for rr := r; rr < r+rs; rr++ {
			for cc := col; cc < col+cs; cc++ {
				key := [2]int{rr, cc}
				if occupied[key] {
					return nil, false
				}
				occupied[key] = true
			}
		}
		text := normalizedTableText(*c.Text)
		if strings.Contains(text, "\ufffd") || strings.Contains(text, "(cid:") {
			return nil, false
		}
		for _, r := range text {
			if unicode.IsControl(r) {
				return nil, false
			}
		}
		if text != "" {
			nonempty++
		}
		if r+rs > rows {
			rows = r + rs
		}
		if col+cs > columns {
			columns = col + cs
		}
		grid[[2]int{r, col}] = gridValue{rs, cs, text}
	}
	return grid, nonempty > 0 && rows*columns == len(occupied) && rows*columns <= 20000
}

func validTableMetrics(candidates []tableCandidate, metrics []tableMetric, page int, coordinates string) bool {
	if len(candidates) != len(metrics) {
		return false
	}
	for i, c := range candidates {
		var cells []json.RawMessage
		m := metrics[i]
		_, actualValid := tableGrid(c.Cells)
		if m.Valid != actualValid {
			return false
		}
		if c.Type != "table" || c.Location.Page != page || c.Location.Coordinates != coordinates || !validTableBox(c.Location.Box) ||
			json.Unmarshal(c.Cells, &cells) != nil || cells == nil || len(cells) > 100000 ||
			m.Rows < 0 || m.Rows > 1000 || m.Columns < 0 || m.Columns > 1000 || m.Nonempty < 0 || m.Nonempty > len(cells) ||
			m.Issues == nil || m.Valid != (len(m.Issues) == 0) {
			return false
		}
	}
	return true
}

// Optional on older canonical documents. New records must retain both sides,
// point to the actual selected blocks and disclose every unresolved comparison.
func validateTables(d *Document) bool {
	c := d.TableComparison
	if c == nil {
		for _, b := range d.Blocks {
			if b.TableComparisonID != "" {
				return false
			}
		}
		return true
	}
	if d.Source.Format != "pdf" || c.Version != "table-comparison-v1" || (c.Policy != "conservative-v1" && c.Policy != "ocr-priority-v1") ||
		len(c.Tables) == 0 || len(c.Tables) > 100000 {
		return false
	}
	blocks := map[string]int{}
	for i, b := range d.Blocks {
		blocks[b.ID] = i
	}
	used := map[string]bool{}
	review := map[string]bool{}
	for _, issue := range d.Quality.Issues {
		if issue.Code == "table_comparison_requires_review" {
			review[fmt.Sprintf("%d/%s", issue.Location.Page, issue.Location.TableID)] = true
		}
	}
	for i, t := range c.Tables {
		if t.ID != fmt.Sprintf("t%d", i+1) || t.Page < 1 || t.Page > len(d.Pages) || !validTableBox(t.Box) ||
			t.Coordinates != "pdf-points-top-left" || t.Native == nil || t.OCR == nil || t.NativeMetrics == nil || t.OCRMetrics == nil ||
			len(t.Native)+len(t.OCR) == 0 || len(t.Native)+len(t.OCR) > 500 || len(t.Reasons) == 0 ||
			!validTableMetrics(t.Native, t.NativeMetrics, t.Page, t.Coordinates) || !validTableMetrics(t.OCR, t.OCRMetrics, t.Page, t.Coordinates) ||
			t.Differences == nil || t.DifferenceCount < 0 || t.DifferenceCount < len(t.Differences) || len(t.Differences) > 100 ||
			t.Truncated != (t.DifferenceCount > len(t.Differences)) {
			return false
		}
		var page struct {
			Number      int    `json:"number"`
			Coordinates string `json:"coordinate_system"`
		}
		if json.Unmarshal(d.Pages[t.Page-1], &page) != nil || page.Number != t.Page || page.Coordinates != t.Coordinates {
			return false
		}
		if len(t.OCR) > 0 {
			found := false
			if d.OCR != nil {
				for _, p := range d.OCR.Pages {
					if p.Page == t.Page && (p.Status == "completed" || p.Status == "partial") {
						found = true
					}
				}
			}
			if !found {
				return false
			}
		}
		for _, x := range t.Differences {
			if x.Row < 0 || x.Column < 0 || (x.Kind != "text" && x.Kind != "span" && x.Kind != "missing_cell") {
				return false
			}
		}
		expectedSelection := "parser"
		if c.Policy == "ocr-priority-v1" {
			usable := len(t.OCR) > 0
			for _, candidate := range t.OCR {
				usable = usable && readableTable(candidate.Cells)
			}
			if usable || len(t.Native) == 0 {
				expectedSelection = "ocr"
			}
			if t.Selected != expectedSelection {
				return false
			}
		}
		switch t.Status {
		case "agreed":
			if len(t.Native) != 1 || len(t.OCR) != 1 {
				return false
			}
			ng, _ := tableGrid(t.Native[0].Cells)
			og, _ := tableGrid(t.OCR[0].Cells)
			if !reflect.DeepEqual(ng, og) {
				return false
			}
			if len(t.Native) != 1 || len(t.OCR) != 1 || !t.NativeMetrics[0].Valid || !t.OCRMetrics[0].Valid || t.DifferenceCount != 0 || t.Selected != expectedSelection {
				return false
			}
		case "selected":
			if len(t.Native) != 1 || len(t.OCR) != 1 || t.NativeMetrics[0].Valid == t.OCRMetrics[0].Valid ||
				(t.Selected == "parser") != t.NativeMetrics[0].Valid {
				return false
			}
		case "needs_review":
			if d.Quality.State == "ready" || !review[fmt.Sprintf("%d/%s", t.Page, t.ID)] {
				return false
			}
		default:
			return false
		}
		selected := t.Native
		if t.Selected == "ocr" {
			selected = t.OCR
		} else if t.Selected != "parser" {
			return false
		}
		if len(selected) == 0 || len(selected) != len(t.BlockIDs) {
			return false
		}
		for j, id := range t.BlockIDs {
			index, ok := blocks[id]
			if !ok || used[id] {
				return false
			}
			b := d.Blocks[index]
			if b.Type != "table" || b.TableComparisonID != t.ID || !reflect.DeepEqual(b.Location, selected[j].Location) ||
				!equalJSON(b.Cells, selected[j].Cells) || b.Text != selected[j].Text {
				return false
			}
			used[id] = true
		}
	}
	for _, b := range d.Blocks {
		if b.TableComparisonID != "" && !used[b.ID] {
			return false
		}
	}
	return true
}
