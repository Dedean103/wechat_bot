package xiaohongshu

import (
	"testing"

	"github.com/stretchr/testify/assert"
	"github.com/xpzouying/xiaohongshu-mcp/configs"
)

func TestLoginURL(t *testing.T) {
	t.Run("xiaohongshu uses explore", func(t *testing.T) {
		t.Setenv("XHS_BACKEND", configs.BackendXiaohongshu)
		assert.Equal(t, "https://www.xiaohongshu.com/explore", loginPageURL())
	})

	t.Run("rednote uses login page", func(t *testing.T) {
		t.Setenv("XHS_BACKEND", configs.BackendRednote)
		assert.Equal(t, "https://www.rednote.com/login", loginPageURL())
	})
}
