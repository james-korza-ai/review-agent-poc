package fixtures

import (
	"errors"
	"log"
)

type profile03 struct {
	Name string
}

// fetchProfile03 returns nil and an error when no profile can be fetched.
func fetchProfile03(id string) (*profile03, error) {
	if id == "" {
		return nil, errors.New("missing profile identifier")
	}
	return &profile03{Name: id}, nil
}

func displayProfile03(id string) string {
	profile, err := fetchProfile03(id)
	if err != nil {
		log.Printf("fetch failed: %v", err)
		profile = &profile03{Name: "Guest"}
	}
	return profile.Name
}
