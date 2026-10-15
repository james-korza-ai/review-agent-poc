package fixtures

import (
	"errors"
	"log"
)

// warmCache06 is best-effort. Its count remains usable on error: it is the
// number of keys processed before the first failure. Callers may report it
// and continue even when part of the warming operation fails.
func warmCache06(keys []string) (int, error) {
	processed := 0
	for _, key := range keys {
		if key == "" {
			return processed, errors.New("empty cache key")
		}
		processed++
	}
	return processed, nil
}

func cacheWarmSummary06(keys []string) int {
	hits, err := warmCache06(keys)
	if err != nil {
		log.Printf("cache warming failed: %v", err)
	}
	return hits
}
