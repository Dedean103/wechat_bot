package xiaohongshu

import (
	"context"
	"encoding/json"
	"time"

	"github.com/go-rod/rod"
	"github.com/pkg/errors"
	"github.com/sirupsen/logrus"
	"github.com/xpzouying/xiaohongshu-mcp/configs"
)

type LoginAction struct {
	page *rod.Page
}

func NewLogin(page *rod.Page) *LoginAction {
	return &LoginAction{page: page}
}

func (a *LoginAction) CheckLoginStatus(ctx context.Context) (bool, error) {
	// 加超时保护：只是查登录态的快速检查，不应无限挂（登录扫码的等待在 Login/WaitForLogin 里）
	pp := a.page.Context(ctx).Timeout(30 * time.Second)
	pp.MustNavigate(configs.WebBaseURL() + "/explore").MustWaitLoad()

	time.Sleep(1 * time.Second)

	exists, _, err := pp.Has(`.main-container .user .link-wrapper .channel`)
	if err != nil {
		return false, errors.Wrap(err, "check login status failed")
	}

	state, stateErr := readLoginPageState(pp)
	if stateErr != nil {
		logrus.Warnf("读取登录页状态失败: %v", stateErr)
	} else {
		logrus.Infof(
			"登录诊断: url=%s sidebar=%t authenticated_state=%t guest=%t user_identity=%t login_button=%t login_modal=%t",
			state.URL, exists, state.Authenticated, state.Guest, state.HasIdentity, state.HasLoginButton, state.HasLoginModal,
		)
	}

	return exists || (stateErr == nil && state.Authenticated), nil
}

// loginPageState 只保留诊断登录所需的布尔状态，不记录账号标识和 Cookie 值。
type loginPageState struct {
	URL            string `json:"url"`
	Authenticated  bool   `json:"authenticated"`
	Guest          bool   `json:"guest"`
	HasIdentity    bool   `json:"hasIdentity"`
	HasLoginButton bool   `json:"hasLoginButton"`
	HasLoginModal  bool   `json:"hasLoginModal"`
}

func readLoginPageState(page *rod.Page) (loginPageState, error) {
	res, err := page.Eval(`() => {
		const unwrap = value => {
			if (!value || typeof value !== "object") return value;
			if (value.value !== undefined) return value.value;
			if (value._value !== undefined) return value._value;
			return value;
		};
		const user = window.__INITIAL_STATE__ && window.__INITIAL_STATE__.user;
		const candidates = user ? [user.userInfo, user.currentUser, user.me] : [];
		const info = candidates.map(unwrap).find(value => value && typeof value === "object") || null;
		const guest = Boolean(info && info.guest === true);
		const hasIdentity = Boolean(info && (info.userId || info.user_id || info.nickname));
		return JSON.stringify({
			url: location.href,
			authenticated: Boolean(info && !guest && hasIdentity),
			guest,
			hasIdentity,
			hasLoginButton: Boolean(document.querySelector("#login-btn")),
			hasLoginModal: Boolean(document.querySelector(".login-container")),
		});
	}`)
	if err != nil {
		return loginPageState{}, errors.Wrap(err, "eval login page state failed")
	}

	var state loginPageState
	if err := json.Unmarshal([]byte(res.Value.String()), &state); err != nil {
		return loginPageState{}, errors.Wrap(err, "unmarshal login page state failed")
	}
	return state, nil
}

// CurrentUser 当前登录用户的基础信息。
type CurrentUser struct {
	Nickname string `json:"nickname"`
	UserID   string `json:"userId"`
}

// CurrentUser 从当前页面的 __INITIAL_STATE__ 读取登录用户信息。
// 需在 CheckLoginStatus 之后调用：复用已加载的 explore 页，不做额外导航。
func (a *LoginAction) CurrentUser(ctx context.Context) (*CurrentUser, error) {
	pp := a.page.Context(ctx).Timeout(10 * time.Second)

	res, err := pp.Eval(`() => {
		const u = window.__INITIAL_STATE__ && window.__INITIAL_STATE__.user;
		const info = u && u.userInfo && u.userInfo.value !== undefined ? u.userInfo.value : (u && u.userInfo);
		if (!info || info.guest) return "";
		return JSON.stringify({nickname: info.nickname, userId: info.userId || info.user_id});
	}`)
	if err != nil {
		return nil, errors.Wrap(err, "read current user state failed")
	}

	raw := res.Value.String()
	if raw == "" {
		return nil, errors.New("current user not found in page state")
	}

	var user CurrentUser
	if err := json.Unmarshal([]byte(raw), &user); err != nil {
		return nil, errors.Wrap(err, "unmarshal current user failed")
	}

	return &user, nil
}

func (a *LoginAction) Login(ctx context.Context) error {
	pp := a.page.Context(ctx)

	// 导航到小红书首页，这会触发二维码弹窗
	pp.MustNavigate(configs.WebBaseURL() + "/explore").MustWaitLoad()

	// 等待一小段时间让页面完全加载
	time.Sleep(2 * time.Second)

	// 检查是否已经登录
	if exists, _, _ := pp.Has(".main-container .user .link-wrapper .channel"); exists {
		// 已经登录，直接返回
		return nil
	}

	// 等待扫码成功提示或者登录完成
	// 这里我们等待登录成功的元素出现，这样更简单可靠
	pp.MustElement(".main-container .user .link-wrapper .channel")

	return nil
}

func (a *LoginAction) FetchQrcodeImage(ctx context.Context) (string, bool, error) {
	pp := a.page.Context(ctx)

	// RedNote 首页不会稳定地自动弹出登录框，独立登录页与国内站二维码 DOM 一致。
	pp.MustNavigate(loginPageURL()).MustWaitLoad()

	// 等待一小段时间让页面完全加载
	time.Sleep(2 * time.Second)

	// 检查是否已经登录
	if exists, _, _ := pp.Has(".main-container .user .link-wrapper .channel"); exists {
		return "", true, nil
	}

	// 获取二维码图片
	src, err := pp.MustElement(".login-container .qrcode-img").Attribute("src")
	if err != nil {
		return "", false, errors.Wrap(err, "get qrcode src failed")
	}
	if src == nil || len(*src) == 0 {
		return "", false, errors.New("qrcode src is empty")
	}

	return *src, false, nil
}

func loginPageURL() string {
	if configs.BackendFromEnv() == configs.BackendRednote {
		return configs.WebBaseURL() + "/login"
	}
	return configs.WebBaseURL() + "/explore"
}

func (a *LoginAction) WaitForLogin(ctx context.Context) bool {
	pp := a.page.Context(ctx)
	ticker := time.NewTicker(500 * time.Millisecond)
	defer ticker.Stop()

	for {
		select {
		case <-ctx.Done():
			return false
		case <-ticker.C:
			el, err := pp.Element(".main-container .user .link-wrapper .channel")
			if err == nil && el != nil {
				return true
			}
		}
	}
}
