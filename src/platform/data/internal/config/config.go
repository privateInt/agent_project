package config

import (
 "log"
 "os"
 "strconv"
)

func Env(key, fallback string) string { if v:=os.Getenv(key);v!="" {return v};return fallback }
func Required(key string) string {v:=os.Getenv(key);if v=="" {log.Fatalf("%s is required",key)};return v}
func Bool(key string, fallback bool) bool {v:=os.Getenv(key);if v=="" {return fallback};b,e:=strconv.ParseBool(v);if e!=nil {log.Fatalf("%s must be boolean",key)};return b}
