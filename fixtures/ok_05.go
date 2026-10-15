package fixtures

import (
	"errors"
	"log"
)

type profile05 struct {
	Name string
}

// fetchProfile05 returns nil and an error when no profile can be fetched.
func fetchProfile05(id string) (*profile05, error) {
	if id == "" {
		return nil, errors.New("missing profile identifier")
	}
	return &profile05{Name: id}, nil
}

func displayProfile05(id string) string {
	profile, err := fetchProfile05(id)
	message := "profile unavailable"
	if err != nil {
		log.Printf("fetch failed: %v", err)
	} else {
		message = profile.Name
	}
	return message
}
