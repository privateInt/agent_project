package main

import (
	"data-platform/internal/browser"
	"fmt"
	"os"
)

func browserSelfTest() error {
	if err := browser.SelfTest(browser.New()); err != nil {
		return err
	}
	fmt.Printf("PASS: browser runtime self-test uid=%d; Go -> socket-restricted Python/Chromium -> dynamic DOM/link and preserved HTTP bytes\n", os.Getuid())
	return nil
}
