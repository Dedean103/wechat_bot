package configs

import (
	"testing"

	"github.com/stretchr/testify/assert"
)

func TestBackendURLs(t *testing.T) {
	t.Run("default uses Xiaohongshu", func(t *testing.T) {
		t.Setenv("XHS_BACKEND", "")
		assert.Equal(t, BackendXiaohongshu, BackendFromEnv())
		assert.Equal(t, "https://www.xiaohongshu.com", WebBaseURL())
		assert.Equal(t, "https://creator.xiaohongshu.com", CreatorBaseURL())
	})

	t.Run("rednote uses RedNote domains", func(t *testing.T) {
		t.Setenv("XHS_BACKEND", "rednote")
		assert.Equal(t, BackendRednote, BackendFromEnv())
		assert.Equal(t, "https://www.rednote.com", WebBaseURL())
		assert.Equal(t, "https://creator.rednote.com", CreatorBaseURL())
	})

	t.Run("backend is case insensitive", func(t *testing.T) {
		t.Setenv("XHS_BACKEND", " REDNOTE ")
		assert.Equal(t, BackendRednote, BackendFromEnv())
	})

	t.Run("invalid backend falls back safely", func(t *testing.T) {
		t.Setenv("XHS_BACKEND", "unknown")
		assert.Equal(t, BackendXiaohongshu, BackendFromEnv())
	})
}
