package main

import (
	"context"
	"fmt"
	"sync"
	"time"
)

const maxResearchBrowsers = 3
const researchBrowserTimeout = 120 * time.Second

// Shared by all service instances and endpoints, before browser startup.
var browserSlots = make(chan struct{}, maxResearchBrowsers)

func acquireBrowserSlot(ctx context.Context) (func(), error) {
	waitCtx, cancel := context.WithTimeout(ctx, researchBrowserTimeout)
	defer cancel()
	if err := waitCtx.Err(); err != nil {
		return nil, fmt.Errorf("browser queue: %w", err)
	}
	select {
	case browserSlots <- struct{}{}:
		// A cancellation racing with an available slot must not start Chromium.
		if err := waitCtx.Err(); err != nil {
			<-browserSlots
			return nil, fmt.Errorf("browser queue: %w", err)
		}
		var once sync.Once
		return func() { once.Do(func() { <-browserSlots }) }, nil
	case <-waitCtx.Done():
		return nil, fmt.Errorf("browser queue: %w", waitCtx.Err())
	}
}
