package app

import (
	"embed"
	"net/http"
)

//go:embed web/*
var consoleFiles embed.FS

func console(w http.ResponseWriter, r *http.Request) bool {
	names := map[string]string{"/console": "index.html", "/console/": "index.html", "/console/app.js": "app.js", "/console/agents.js": "agents.js", "/console/workflows.js": "workflows.js", "/console/style.css": "style.css", "/console/example.json": "example.json"}
	file, ok := names[r.URL.Path]
	if !ok {
		return false
	}
	if r.Method != "GET" {
		method(w, "GET")
		return true
	}
	if r.URL.RawQuery != "" {
		writeError(w, 400, "INVALID_ARGUMENT", "query unsupported")
		return true
	}
	body, e := consoleFiles.ReadFile("web/" + file)
	if e != nil {
		writeError(w, 404, "NOT_FOUND", "not found")
		return true
	}
	mime := map[string]string{"index.html": "text/html; charset=utf-8", "app.js": "text/javascript; charset=utf-8", "agents.js": "text/javascript; charset=utf-8", "workflows.js": "text/javascript; charset=utf-8", "style.css": "text/css; charset=utf-8", "example.json": "application/json; charset=utf-8"}
	w.Header().Set("Content-Type", mime[file])
	w.Header().Set("Referrer-Policy", "no-referrer")
	w.Header().Set("Content-Security-Policy", "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'")
	w.WriteHeader(200)
	_, _ = w.Write(body)
	return true
}
