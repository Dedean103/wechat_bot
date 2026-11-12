package xiaohongshu

import (
	"strings"
	"testing"

	"github.com/stretchr/testify/require"
)

func TestRednoteBackendURLs(t *testing.T) {
	t.Setenv("XHS_BACKEND", "rednote")

	urls := []string{
		makeSearchURL("深圳粤菜"),
		makeFeedDetailURL("feed-id", "token"),
		makeUserProfileURL("user-id", "token"),
		publishURL(),
	}
	for _, got := range urls {
		require.True(t, strings.HasPrefix(got, "https://"), got)
		require.Contains(t, got, ".rednote.com", got)
		require.NotContains(t, got, ".xiaohongshu.com", got)
	}
}
