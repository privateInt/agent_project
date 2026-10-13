package canonical

import (
	"encoding/json"
	"math"
	"reflect"
	"strings"
)

type VisualRegion struct {
	ID           string        `json:"id"`
	Status       string        `json:"status"`
	Reason       string        `json:"reason"`
	SourceLabel  string        `json:"source_label"`
	Source       tableLocation `json:"source_location"`
	Location     tableLocation `json:"location"`
	ResultBlocks int           `json:"result_blocks"`
}

func validateVisualRegions(d *Document) bool {
	regions := map[string]VisualRegion{}
	counts := map[string]int{}
	withOutput := 0
	if d.OCR != nil {
		for _, p := range d.OCR.Pages {
			if len(p.VisualRegions) > 500 {
				return false
			}
			if len(p.VisualRegions) == 0 {
				continue
			}
			if p.Page < 1 || p.Page > len(d.Pages) {
				return false
			}
			var page struct {
				Width       float64 `json:"width"`
				Height      float64 `json:"height"`
				Coordinates string  `json:"coordinate_system"`
			}
			if json.Unmarshal(d.Pages[p.Page-1], &page) != nil || page.Width <= 0 || page.Height <= 0 {
				return false
			}
			boxOK := func(loc tableLocation) bool {
				return loc.Page == p.Page && loc.Coordinates == page.Coordinates &&
					validTableBox(loc.Box) && loc.Box[0] >= 0 && loc.Box[1] >= 0 &&
					loc.Box[2] <= page.Width+1e-6 && loc.Box[3] <= page.Height+1e-6
			}
			for _, r := range p.VisualRegions {
				if r.ID == "" || len(r.ID) > 32 || regions[r.ID].ID != "" || !boxOK(r.Source) ||
					(r.SourceLabel != "Image" && r.SourceLabel != "Figure" && r.SourceLabel != "Diagram") {
					return false
				}
				switch r.Status {
				case "completed", "partial":
					withOutput++
					if withOutput > 8 || r.ResultBlocks < 1 || r.ResultBlocks > 500 ||
						!boxOK(r.Location) || (p.Status != "completed" && p.Status != "partial") {
						return false
					}
					if r.Status == "completed" && r.Reason != "" {
						return false
					}
					unread := 0
					for _, b := range d.Blocks {
						if b.Type == "image" && b.RecognitionStatus == "unread" && b.Label == r.SourceLabel &&
							b.VisualRegionID == "" && reflect.DeepEqual(b.Location, r.Source) {
							unread++
						}
					}
					if r.Status == "completed" && unread != 0 {
						return false
					}
					if r.Status == "partial" {
						if r.Reason != "visual_text_unread" || p.Status != "partial" {
							return false
						}
						// A partial crop preserves both readable children and its
						// original unread extent, not merely an unrelated page image.
						if unread != 1 {
							return false
						}
						for _, previous := range regions {
							if previous.Status == "partial" && reflect.DeepEqual(previous.Source, r.Source) {
								return false
							}
						}
					}
					// The crop covers the visual box, rounded out to raster pixels.
					for i := 0; i < 2; i++ {
						if r.Location.Box[i] > r.Source.Box[i]+1e-6 || r.Location.Box[i+2] < r.Source.Box[i+2]-1e-6 {
							return false
						}
					}
				case "failed", "skipped":
					if r.Reason == "" || r.ResultBlocks != 0 || p.Status == "completed" {
						return false
					}
				default:
					return false
				}
				regions[r.ID] = r
			}
		}
	}
	for _, b := range d.Blocks {
		if b.VisualRegionID == "" {
			continue
		}
		r, found := regions[b.VisualRegionID]
		if !found || (r.Status != "completed" && r.Status != "partial") || b.RecognitionStatus != "" ||
			(strings.TrimSpace(b.Text) == "" && !readableTable(b.Cells)) ||
			b.Location.Page != r.Location.Page || b.Location.Coordinates != r.Location.Coordinates ||
			!validTableBox(b.Location.Box) || !validTableBox(b.ModelBox) ||
			b.ModelCoordinates != "normalized-0-1000-crop-top-left" {
			return false
		}
		for i, v := range b.ModelBox {
			if v < 0 || v > 1000 {
				return false
			}
			axis := i % 2
			expected := r.Location.Box[axis] + v*(r.Location.Box[axis+2]-r.Location.Box[axis])/1000
			if math.Abs(expected-b.Location.Box[i]) > 1e-6 {
				return false
			}
		}
		counts[r.ID]++
	}
	for id, r := range regions {
		if counts[id] != r.ResultBlocks {
			return false
		}
	}
	return true
}
