package config

import (
	"fmt"
	"os"
	"strings"
)

func Env(name, fallback string) string {
	if value := os.Getenv(name); value != "" {
		return value
	}
	return fallback
}

// Secrets are read from files, never included in an error or command argument.
func Secret(name string) (string, error) {
	path := os.Getenv(name)
	if path == "" {
		return "", fmt.Errorf("%s is required", name)
	}
	data, err := os.ReadFile(path)
	if err != nil {
		return "", fmt.Errorf("cannot read %s", name)
	}
	value := strings.TrimSpace(string(data))
	if len(value) < 32 || len(value) > 256 || strings.ContainsAny(value, " \r\n\t\x00") {
		return "", fmt.Errorf("%s must contain a 32-256 character token", name)
	}
	return value, nil
}
