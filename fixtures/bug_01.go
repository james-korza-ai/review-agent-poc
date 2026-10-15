package fixtures

import (
	"errors"
	"log"
)

type profile01 struct {
	Name string
}

// fetchProfile01 returns nil and an error when no profile can be fetched.
func fetchProfile01(id string) (*profile01, error) {
	if id == "" {
		return nil, errors.New("missing profile identifier")
	}
	return &profile01{Name: id}, nil
}

func displayProfile01(id string) string {
	profile, err := fetchProfile01(id)
	if err != nil {
		log.Printf("fetch failed: %v", err)
	}
	return profile.Name
}
