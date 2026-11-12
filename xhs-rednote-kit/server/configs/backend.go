package configs

import (
	"os"
	"strings"

	"github.com/sirupsen/logrus"
)

const (
	BackendXiaohongshu = "xiaohongshu"
	BackendRednote     = "rednote"
)

// BackendFromEnv selects which web product the browser automation uses.
// RedNote accounts have cookies scoped to .rednote.com and must not be
// validated against xiaohongshu.com.
func BackendFromEnv() string {
	backend := strings.ToLower(strings.TrimSpace(os.Getenv("XHS_BACKEND")))
	switch backend {
	case "", BackendXiaohongshu:
		return BackendXiaohongshu
	case BackendRednote:
		return BackendRednote
	default:
		logrus.Warnf("invalid XHS_BACKEND=%q, using %s", backend, BackendXiaohongshu)
		return BackendXiaohongshu
	}
}

func WebBaseURL() string {
	if BackendFromEnv() == BackendRednote {
		return "https://www.rednote.com"
	}
	return "https://www.xiaohongshu.com"
}

func CreatorBaseURL() string {
	if BackendFromEnv() == BackendRednote {
		return "https://creator.rednote.com"
	}
	return "https://creator.xiaohongshu.com"
}
