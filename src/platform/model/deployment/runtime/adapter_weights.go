package runtime

import (
	"bytes"
	"context"
	"encoding/binary"
	"encoding/json"
	"fmt"
	"io"
	"math"
	"os"
	"path/filepath"
	"reflect"
	"sort"
	"strings"

	"model-platform/internal/artifacts"
)

// Read bounded safetensors headers and stream tensor slices. No pickle/model
// code is executed, and a complete embedding tensor never enters Go memory.
type tensorInfo struct {
	Dtype   string  `json:"dtype"`
	Shape   []int64 `json:"shape"`
	Offsets []int64 `json:"data_offsets"`
	file    string
	start   int64
}

func dtypeWidth(dtype string) int64 {
	switch dtype {
	case "F32":
		return 4
	case "BF16", "F16":
		return 2
	}
	return 0
}

func tensorEntries(data []byte) (map[string]json.RawMessage, error) {
	decoder := json.NewDecoder(bytes.NewReader(data))
	token, err := decoder.Token()
	if err != nil || token != json.Delim('{') {
		return nil, fmt.Errorf("safetensors header must be an object")
	}
	entries := make(map[string]json.RawMessage)
	for decoder.More() {
		token, err := decoder.Token()
		if err != nil {
			return nil, err
		}
		name, ok := token.(string)
		if !ok {
			return nil, fmt.Errorf("invalid tensor name")
		}
		if _, exists := entries[name]; exists {
			return nil, fmt.Errorf("duplicate tensor name: %s", name)
		}
		var value json.RawMessage
		if err := decoder.Decode(&value); err != nil {
			return nil, err
		}
		entries[name] = value
	}
	if _, err := decoder.Token(); err != nil {
		return nil, err
	}
	var extra any
	if err := decoder.Decode(&extra); err != io.EOF {
		return nil, fmt.Errorf("trailing safetensors header content")
	}
	return entries, nil
}

func tensorHeader(name string) (map[string]tensorInfo, error) {
	info, err := os.Lstat(name)
	if err != nil {
		return nil, err
	}
	if !info.Mode().IsRegular() || info.Size() < 8 {
		return nil, fmt.Errorf("invalid safetensors file")
	}
	f, err := os.Open(name)
	if err != nil {
		return nil, err
	}
	defer f.Close()
	var size uint64
	if err := binary.Read(f, binary.LittleEndian, &size); err != nil {
		return nil, err
	}
	if size == 0 || size > 16<<20 || size > uint64(info.Size()-8) {
		return nil, fmt.Errorf("invalid safetensors header length")
	}
	data := make([]byte, int(size))
	if _, err := io.ReadFull(f, data); err != nil {
		return nil, err
	}
	raw, err := tensorEntries(data)
	if err != nil {
		return nil, err
	}
	result := make(map[string]tensorInfo)
	available := info.Size() - 8 - int64(size)
	for key, value := range raw {
		if key == "__metadata__" {
			continue
		}
		var t tensorInfo
		if json.Unmarshal(value, &t) != nil || len(t.Offsets) != 2 || len(t.Shape) == 0 {
			return nil, fmt.Errorf("invalid tensor header: %s", key)
		}
		width := dtypeWidth(t.Dtype)
		if width == 0 || t.Offsets[0] < 0 || t.Offsets[1] < t.Offsets[0] || t.Offsets[1] > available {
			return nil, fmt.Errorf("unsupported dtype or tensor range: %s", key)
		}
		n := width
		for _, dim := range t.Shape {
			if dim <= 0 || dim > available/n {
				return nil, fmt.Errorf("invalid tensor shape: %s", key)
			}
			n *= dim
		}
		if t.Offsets[1]-t.Offsets[0] != n {
			return nil, fmt.Errorf("tensor size/shape mismatch: %s", key)
		}
		t.file, t.start = name, 8+int64(size)+t.Offsets[0]
		result[key] = t
	}
	if len(result) == 0 {
		return nil, fmt.Errorf("empty safetensors weights")
	}
	ordered := make([]tensorInfo, 0, len(result))
	for _, t := range result {
		ordered = append(ordered, t)
	}
	sort.Slice(ordered, func(i, j int) bool { return ordered[i].Offsets[0] < ordered[j].Offsets[0] })
	end := int64(0)
	for _, t := range ordered {
		if t.Offsets[0] != end {
			return nil, fmt.Errorf("safetensors has overlapping or missing data")
		}
		end = t.Offsets[1]
	}
	if end != available {
		return nil, fmt.Errorf("safetensors has unindexed data")
	}
	return result, nil
}

func baseTensors(dir string) (map[string]tensorInfo, error) {
	files, err := filepath.Glob(filepath.Join(dir, "model*.safetensors"))
	if err != nil {
		return nil, err
	}
	index := map[string]tensorInfo{}
	for _, file := range files {
		entries, err := tensorHeader(file)
		if err != nil {
			return nil, err
		}
		for name, t := range entries {
			if _, ok := index[name]; ok {
				return nil, fmt.Errorf("duplicate base tensor: %s", name)
			}
			index[name] = t
		}
	}
	if len(index) == 0 {
		return nil, fmt.Errorf("base snapshot needs model*.safetensors weights")
	}
	return index, nil
}

func scalar(data []byte, dtype string) float32 {
	switch dtype {
	case "F32":
		return math.Float32frombits(binary.LittleEndian.Uint32(data))
	case "BF16":
		return math.Float32frombits(uint32(binary.LittleEndian.Uint16(data)) << 16)
	case "F16":
		v := binary.LittleEndian.Uint16(data)
		sign := float64(1)
		if v&0x8000 != 0 {
			sign = -1
		}
		exponent, mantissa := (v>>10)&31, v&1023
		if exponent == 31 {
			if mantissa != 0 {
				return float32(math.NaN())
			}
			return float32(math.Inf(int(sign)))
		}
		if exponent == 0 {
			return float32(sign * math.Ldexp(float64(mantissa), -24))
		}
		return float32(sign * math.Ldexp(float64(1024+mantissa), int(exponent)-25))
	}
	return float32(math.NaN())
}

func equalTensor(ctx context.Context, a, b tensorInfo) (bool, error) {
	if !reflect.DeepEqual(a.Shape, b.Shape) {
		return false, nil
	}
	af, err := os.Open(a.file)
	if err != nil {
		return false, err
	}
	defer af.Close()
	bf, err := os.Open(b.file)
	if err != nil {
		return false, err
	}
	defer bf.Close()
	aw, bw := dtypeWidth(a.Dtype), dtypeWidth(b.Dtype)
	count := (a.Offsets[1] - a.Offsets[0]) / aw
	ab, bb := make([]byte, 4096*aw), make([]byte, 4096*bw)
	for done := int64(0); done < count; {
		if err := ctx.Err(); err != nil {
			return false, err
		}
		n := min(int64(4096), count-done)
		if _, err := af.ReadAt(ab[:n*aw], a.start+done*aw); err != nil {
			return false, err
		}
		if _, err := bf.ReadAt(bb[:n*bw], b.start+done*bw); err != nil {
			return false, err
		}
		for i := int64(0); i < n; i++ {
			if scalar(ab[i*aw:], a.Dtype) != scalar(bb[i*bw:], b.Dtype) {
				return false, nil
			}
		}
		done += n
	}
	return true, nil
}

type checkedReader struct {
	ctx context.Context
	io.Reader
}

func (r checkedReader) Read(b []byte) (int, error) {
	if err := r.ctx.Err(); err != nil {
		return 0, err
	}
	return r.Reader.Read(b)
}

// Validate every non-LoRA tensor before excluding redundant saved embeddings.
// A changed/resized embedding is never silently lost to vLLM's LoRA loader.
func selectedLoRA(ctx context.Context, baseDir string, all map[string]tensorInfo, cfg adapterConfig) ([]string, []string, error) {
	base, err := baseTensors(baseDir)
	if err != nil {
		return nil, nil, err
	}
	configBytes, err := readSmall(filepath.Join(baseDir, "config.json"), 1<<20)
	if err != nil {
		return nil, nil, err
	}
	var config struct {
		Tied bool `json:"tie_word_embeddings"`
	}
	if err := json.Unmarshal(configBytes, &config); err != nil {
		return nil, nil, err
	}
	var selected, duplicates []string
	pairs := map[string]map[string]tensorInfo{}
	for name, t := range all {
		normal := strings.TrimPrefix(name, "base_model.model.")
		plain := strings.ReplaceAll(normal, ".base_layer.weight", ".weight")
		if plain == "model.embed_tokens.weight" || plain == "lm_head.weight" {
			original, ok := base[plain]
			if !ok && plain == "lm_head.weight" && config.Tied {
				original, ok = base["model.embed_tokens.weight"]
			}
			if !ok {
				return nil, nil, fmt.Errorf("saved embedding has no matching base tensor: %s", name)
			}
			equal, err := equalTensor(ctx, t, original)
			if err != nil {
				return nil, nil, err
			}
			if !equal {
				return nil, nil, fmt.Errorf("saved embedding differs from base: %s; changed embeddings need a separate full-model export", name)
			}
			duplicates = append(duplicates, name)
			continue
		}
		part, module := "", ""
		for _, side := range []string{"A", "B"} {
			suffix := ".lora_" + side + ".weight"
			if strings.HasSuffix(normal, suffix) {
				part, module = side, strings.TrimSuffix(normal, suffix)
			}
		}
		if part == "" || len(t.Shape) != 2 {
			return nil, nil, fmt.Errorf("unsupported adapter tensor: %s", name)
		}
		if pairs[module] == nil {
			pairs[module] = map[string]tensorInfo{}
		}
		if _, exists := pairs[module][part]; exists {
			return nil, nil, fmt.Errorf("duplicate LoRA tensor identity")
		}
		pairs[module][part] = t
		selected = append(selected, name)
	}
	if len(pairs) == 0 {
		return nil, nil, fmt.Errorf("adapter has no LoRA A/B tensors")
	}
	for module, pair := range pairs {
		a, aok := pair["A"]
		b, bok := pair["B"]
		original, ok := base[module+".weight"]
		if !aok || !bok || !ok || len(original.Shape) != 2 ||
			a.Shape[0] != int64(cfg.Rank) || b.Shape[1] != int64(cfg.Rank) ||
			a.Shape[1] != original.Shape[1] || b.Shape[0] != original.Shape[0] {
			return nil, nil, fmt.Errorf("LoRA rank/pair/base tensor shape mismatch: %s", module)
		}
	}
	sort.Strings(selected)
	sort.Strings(duplicates)
	return selected, duplicates, nil
}

func writeProjection(ctx context.Context, name string, all map[string]tensorInfo, selected []string) error {
	header := map[string]any{"__metadata__": map[string]string{"format": "pt"}}
	offset := int64(0)
	for _, key := range selected {
		t := all[key]
		size := t.Offsets[1] - t.Offsets[0]
		t.Offsets = []int64{offset, offset + size}
		header[key] = t
		offset += size
	}
	encoded, err := json.Marshal(header)
	if err != nil {
		return err
	}
	for len(encoded)%8 != 0 {
		encoded = append(encoded, ' ')
	}
	out, err := os.Create(name)
	if err != nil {
		return err
	}
	defer out.Close()
	if err := binary.Write(out, binary.LittleEndian, uint64(len(encoded))); err != nil {
		return err
	}
	if _, err := out.Write(encoded); err != nil {
		return err
	}
	for _, key := range selected {
		t := all[key]
		in, err := os.Open(t.file)
		if err != nil {
			return err
		}
		_, copyErr := io.Copy(out, checkedReader{ctx, io.NewSectionReader(in, t.start, t.Offsets[1]-t.Offsets[0])})
		in.Close()
		if copyErr != nil {
			return copyErr
		}
	}
	return out.Close()
}

func projectAdapter(ctx context.Context, baseDir, sourceDir, target string, p Plan, cfg adapterConfig, config []byte) error {
	all, err := tensorHeader(filepath.Join(sourceDir, "adapter_model.safetensors"))
	if err != nil {
		return err
	}
	selected, duplicates, err := selectedLoRA(ctx, baseDir, all, cfg)
	if err != nil {
		return err
	}
	chat, chatSource, err := servingChatTemplate(baseDir, sourceDir)
	if err != nil {
		return err
	}
	temp, err := os.MkdirTemp(filepath.Dir(target), ".adapter-view-")
	if err != nil {
		return err
	}
	defer os.RemoveAll(temp)
	for name, data := range map[string][]byte{"adapter_config.json": config, "chat_template.jinja": chat} {
		if err := os.WriteFile(filepath.Join(temp, name), data, 0o644); err != nil {
			return err
		}
	}
	weights := filepath.Join(temp, "adapter_model.safetensors")
	if err := writeProjection(ctx, weights, all, selected); err != nil {
		return err
	}
	digest, size, err := artifacts.HashFile(weights)
	if err != nil {
		return err
	}
	receipt, err := json.Marshal(struct {
		Format         int      `json:"serving_format"`
		BaseSHA        string   `json:"base_manifest_sha256"`
		AdapterSHA     string   `json:"adapter_manifest_sha256"`
		Rank           int      `json:"lora_rank"`
		TensorCount    int      `json:"lora_tensor_count"`
		Duplicates     []string `json:"verified_duplicate_embeddings"`
		ProjectionSHA  string   `json:"projection_sha256"`
		ProjectionSize int64    `json:"projection_size_bytes"`
		ChatSource     string   `json:"chat_template_source"`
		ChatSHA        string   `json:"chat_template_sha256"`
	}{p.Source.Adapter.ServingFormat, p.Source.SHA256, p.Source.Adapter.SHA256, cfg.Rank, len(selected), duplicates, digest, size, chatSource, artifacts.SHA256(chat)})
	if err != nil {
		return err
	}
	if err := os.WriteFile(filepath.Join(temp, "serving-adapter.json"), receipt, 0o644); err != nil {
		return err
	}
	if err := ctx.Err(); err != nil {
		return err
	}
	if info, err := os.Lstat(target); err == nil {
		if !info.IsDir() || info.Mode()&os.ModeSymlink != 0 {
			return fmt.Errorf("unsafe serving adapter directory")
		}
		entries, err := os.ReadDir(target)
		if err != nil {
			return err
		}
		if len(entries) != 4 {
			return fmt.Errorf("unexpected serving adapter files")
		}
		for _, name := range []string{"adapter_config.json", "adapter_model.safetensors", "chat_template.jinja", "serving-adapter.json"} {
			want, n, err := artifacts.HashFile(filepath.Join(temp, name))
			if err != nil {
				return err
			}
			got, m, err := artifacts.HashFile(filepath.Join(target, name))
			if err != nil {
				return err
			}
			if want != got || n != m {
				return fmt.Errorf("serving adapter projection changed: %s", name)
			}
		}
		return nil
	} else if !os.IsNotExist(err) {
		return err
	}
	return os.Rename(temp, target)
}
