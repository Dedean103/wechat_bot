package main

import (
	"context"
	"errors"
	"sync"
	"sync/atomic"
	"testing"
	"time"
)

func TestBrowserSlotsCapAndCancellation(t *testing.T) {
	var releases []func()
	defer func() {
		for _, release := range releases {
			release()
		}
	}()
	for i := 0; i < 3; i++ {
		release, err := acquireBrowserSlot(context.Background())
		if err != nil {
			t.Fatal(err)
		}
		releases = append(releases, release)
	}
	ctx, cancel := context.WithTimeout(context.Background(), 20*time.Millisecond)
	defer cancel()
	if _, err := acquireBrowserSlot(ctx); !errors.Is(err, context.DeadlineExceeded) {
		t.Fatalf("fourth browser must wait and respect cancellation: %v", err)
	}
	// Releasing twice cannot accidentally free a different browser's slot.
	releases[0]()
	releases[0]()
	release, err := acquireBrowserSlot(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	releases = append(releases, release)
	if len(browserSlots) != 3 {
		t.Fatalf("expected three occupied slots, got %d", len(browserSlots))
	}
}

func TestBrowserSlotsConcurrentRequests(t *testing.T) {
	var workers sync.WaitGroup
	var active, peak atomic.Int32
	for i := 0; i < 30; i++ {
		workers.Add(1)
		go func() {
			defer workers.Done()
			release, err := acquireBrowserSlot(context.Background())
			if err != nil {
				t.Error(err)
				return
			}
			n := active.Add(1)
			for old := peak.Load(); n > old; old = peak.Load() {
				if peak.CompareAndSwap(old, n) {
					break
				}
			}
			time.Sleep(time.Millisecond)
			active.Add(-1)
			release()
		}()
	}
	workers.Wait()
	if peak.Load() > 3 || len(browserSlots) != 0 {
		t.Fatalf("peak=%d, leaked slots=%d", peak.Load(), len(browserSlots))
	}
}

func TestCancelledRequestDoesNotAcquireAvailableSlot(t *testing.T) {
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	if _, err := acquireBrowserSlot(ctx); !errors.Is(err, context.Canceled) {
		t.Fatalf("expected cancellation, got %v", err)
	}
	if len(browserSlots) != 0 {
		t.Fatal("cancelled request leaked a slot")
	}
}
