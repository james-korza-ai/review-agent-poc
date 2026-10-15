package fixtures

import (
	"fmt"
	"log"
	"strconv"
)

// parseDollarAmount02 returns zero and an error for an invalid invoice amount.
func parseDollarAmount02(raw string) (int64, error) {
	amount, err := strconv.ParseInt(raw, 10, 64)
	if err != nil {
		return 0, fmt.Errorf("invalid invoice amount: %w", err)
	}
	return amount, nil
}

func invoiceCents02(raw string) int64 {
	dollars, err := parseDollarAmount02(raw)
	if err != nil {
		log.Printf("invoice amount failed: %v", err)
	}
	return dollars * 100
}
