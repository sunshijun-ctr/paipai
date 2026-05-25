# 用户系统实施方案：邮箱 + 手机 + QQ 登录

> 范围：在现有 FastAPI + 单页 `index.html` 基础上增加用户注册/登录系统，**不重构前端**。
> 部署目标：大陆境内生产环境。
> 主体类型：待确认（个人 / 企业）。

---

## 1. 目标与边界

### 1.1 本期目标
- 用户可以用 **邮箱 + 密码**、**手机号 + 短信验证码**、**QQ OAuth** 三种方式注册和登录
- 一个用户可以绑定多种登录方式，互相之间可以切换
- 现有 `/api/sessions`、`/ws`、`/api/upload` 等接口加上鉴权
- 现有 `index.html` 主应用基本不动，未登录时自动跳转到 `/login`

### 1.2 本期不做
- 微信登录（个人主体不支持网站扫码登录；企业主体留到第二期）
- 第三方账号合并、双因子认证、SSO
- 前端框架重构（Vue/Next 推迟）
- 付费 / 套餐 / 配额系统
- 管理员后台

### 1.3 验收标准
- [ ] 三种登录方式都能在生产环境跑通
- [ ] 未登录用户访问 `/` 被重定向到 `/login`
- [ ] 已登录用户的 WebSocket 会话归属到正确的 user_id
- [ ] 短信验证码限频生效（1 条/60s、5 条/天/手机号）
- [ ] 退出登录后 access token 立即失效
- [ ] `/account` 页可以查看和解绑已绑定的登录方式

---

## 2. 整体架构

```
┌──────────────────────────────────────────────────────────────┐
│  浏览器                                                       │
│   ├── /login.html ──┐                                         │
│   ├── /register.html │  fetch /api/auth/*                     │
│   ├── /account.html  │                                        │
│   └── /index.html ───┘  cookie: ra_at / ra_rt                 │
└──────────────────────────────┬───────────────────────────────┘
                               │
┌──────────────────────────────▼───────────────────────────────┐
│  FastAPI (app/api/server.py)                                  │
│   ├── /api/auth/*       新增：注册/登录/发码/OAuth/登出/刷新  │
│   ├── /api/me           新增：当前用户 + 已绑登录方式         │
│   ├── /api/sessions     现有：加 require_user 依赖            │
│   ├── /ws               现有：握手时校验 cookie               │
│   └── /                 现有：未登录 302 → /login             │
└──────────────────────────────┬───────────────────────────────┘
                               │
        ┌──────────────────────┼──────────────────────┐
        │                      │                      │
┌───────▼──────┐      ┌────────▼────────┐    ┌───────▼────────┐
│  PostgreSQL  │      │      Redis      │    │  外部服务      │
│  users       │      │  sms:code:*     │    │  阿里云短信    │
│  identities  │      │  email:code:*   │    │  SMTP 邮件     │
│  refresh_tk  │      │  ratelimit:*    │    │  QQ 互联 OAuth │
└──────────────┘      └─────────────────┘    └────────────────┘
```

### 2.1 鉴权方案
- **JWT + Refresh Token**（双 token）
- Access Token：JWT，15 分钟有效，放 `ra_at` cookie（HttpOnly + SameSite=Lax + Secure）
- Refresh Token：随机 256bit 字符串，7 天有效，**哈希后存 PG**，放 `ra_rt` cookie
- 退出登录 = 删 cookie + 把 refresh token 标记 `revoked_at`
- WebSocket 握手时从 cookie 读 access token，无效则关闭连接

### 2.2 关键设计原则
1. **用户 ≠ 身份**：一个 `users` 行可以挂多个 `user_identities` 行，每个 identity 是一种登录方式
2. **首次 OAuth = 自动注册**：QQ 登录如果没找到对应 identity，自动创建一个新 user
3. **手机号/邮箱跨方式唯一**：同一个手机号不能既是 A 用户的登录手机号又是 B 用户的；遇冲突时引导用户走"账号合并"提示（本期：先报错"该手机号已注册"）
4. **写到 cookie 而不是 localStorage**：避免 XSS 拿到 token

---

## 3. 数据模型

### 3.1 新增表

```sql
-- 用户主表
CREATE TABLE users (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    display_name    TEXT NOT NULL,
    avatar_url      TEXT,
    primary_email   TEXT,                  -- 仅展示，登录看 identities
    primary_phone   TEXT,
    status          TEXT NOT NULL DEFAULT 'active',  -- active / suspended / deleted
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- 登录身份表（一个 user 可有多行）
CREATE TABLE user_identities (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id         UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    provider        TEXT NOT NULL,         -- email / phone / qq
    provider_uid    TEXT NOT NULL,         -- 邮箱 / 手机号 / QQ openid
    credential      TEXT,                  -- email/phone：bcrypt(password)；qq: NULL
    verified_at     TIMESTAMPTZ,
    metadata        JSONB NOT NULL DEFAULT '{}'::jsonb,  -- OAuth 原始 profile
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (provider, provider_uid)
);
CREATE INDEX idx_user_identities_user ON user_identities(user_id);

-- Refresh token（哈希存储）
CREATE TABLE refresh_tokens (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id         UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    token_hash      TEXT NOT NULL UNIQUE,  -- sha256(token)
    expires_at      TIMESTAMPTZ NOT NULL,
    revoked_at      TIMESTAMPTZ,
    user_agent      TEXT,
    ip              INET,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX idx_refresh_tokens_user ON refresh_tokens(user_id) WHERE revoked_at IS NULL;
```

### 3.2 现有表的关联
现有 `data/memory/sessions/*.json` 文件格式的会话记录暂时**不强制**关联 user_id，本期只在新建会话时写入 `owner_user_id` 字段，旧数据兼容（visibility="public"）。后续清理由独立任务负责。

### 3.3 验证码（不用 PG，全部走 Redis）

```
Key                              Value          TTL
─────────────────────────────────────────────────────
sms:code:{phone}:{purpose}       hash(code)     5min
email:code:{email}:{purpose}     hash(code)     10min
ratelimit:sms:{phone}:60s        counter        60s
ratelimit:sms:{phone}:1d         counter        24h
ratelimit:login_fail:{ident}     counter        15min
```
`purpose` 取值：`register` / `login` / `bind` / `reset_password`。

---

## 4. 后端 API 设计

所有接口前缀 `/api/auth`，错误返回标准结构：
```json
{ "ok": false, "code": "INVALID_CODE", "message": "验证码错误或已过期" }
```

### 4.1 邮箱
| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/api/auth/email/register` | body: `{email, password, display_name}` → 发激活邮件 |
| GET  | `/api/auth/email/verify?token=...` | 邮件激活 |
| POST | `/api/auth/email/login` | body: `{email, password}` → 写 cookie |
| POST | `/api/auth/email/forgot` | 发找回密码邮件 |
| POST | `/api/auth/email/reset` | body: `{token, new_password}` |

### 4.2 手机
| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/api/auth/phone/send-code` | body: `{phone, purpose}` → 限频校验 + 下发短信 |
| POST | `/api/auth/phone/login` | body: `{phone, code}` → 不存在则自动注册 |
| POST | `/api/auth/phone/bind` | （已登录）绑定手机号到当前用户 |

### 4.3 QQ OAuth
| 方法 | 路径 | 说明 |
|------|------|------|
| GET  | `/api/auth/qq/start` | 302 → QQ authorize URL（带 state） |
| GET  | `/api/auth/qq/callback?code&state` | QQ 回调 → 拿 openid → 找/建 user → 写 cookie → 302 → `/` |

### 4.4 通用
| 方法 | 路径 | 说明 |
|------|------|------|
| GET  | `/api/me` | 返回 `{id, display_name, avatar_url, identities: [...]}` |
| POST | `/api/auth/refresh` | 用 refresh cookie 换新的 access token |
| POST | `/api/auth/logout` | 删 cookie + revoke refresh token |
| DELETE | `/api/auth/identity/{id}` | 解绑某个登录方式（至少保留 1 个） |

### 4.5 鉴权依赖

```python
# app/api/deps.py
async def require_user(request: Request) -> User: ...      # 401 if missing
async def optional_user(request: Request) -> User | None: ... # 不阻断
```
现有路由按需加 `Depends(require_user)`：
- `/api/sessions` 全部加
- `/api/upload` 加
- `/ws` 在 `accept` 前校验
- `/api/qq_webhook` **不加**（来自 QQ 平台的回调）

---

## 5. 前端改动（轻量路线）

### 5.1 新增文件
```
app/api/static/
├── index.html              ← 现有，仅小改
├── login.html              ← 新增 ~250 行
├── register.html           ← 新增 ~200 行
├── account.html            ← 新增 ~180 行
├── auth.js                 ← 新增 ~150 行
└── auth.css                ← 新增 ~200 行
```

### 5.2 `auth.js` 提供的能力
```js
window.Auth = {
  async getMe(),                    // GET /api/me，401 时返回 null
  async loginEmail(email, pwd),
  async loginPhone(phone, code),
  async sendSmsCode(phone, purpose),
  async logout(),
  async refresh(),                  // 401 时被 fetch 拦截器自动调用
  redirectToLogin(),
  bindIdentity(provider),
  unbindIdentity(identityId),
};
```

### 5.3 `index.html` 的改动（< 30 行）
1. 顶部导航栏右侧加用户头像菜单（昵称 / 账号设置 / 退出）
2. 加一个全局 fetch 拦截器：响应 401 时调 `/api/auth/refresh`，再失败则 `redirectToLogin()`
3. WebSocket onclose 收到 `4401` 状态码时跳登录页
4. 启动时调 `Auth.getMe()`，无用户则直接跳登录

### 5.4 后端路由层面的重定向
```python
@app.get("/")
async def index(user = Depends(optional_user)):
    if user is None:
        return RedirectResponse("/login")
    return FileResponse("app/api/static/index.html")
```

---

## 6. 配置与环境变量

`.env` 新增：
```ini
# JWT
AUTH_JWT_SECRET=<openssl rand -hex 64>
AUTH_JWT_ALGORITHM=HS256
AUTH_ACCESS_TOKEN_TTL_MINUTES=15
AUTH_REFRESH_TOKEN_TTL_DAYS=7
AUTH_COOKIE_DOMAIN=                    # 留空 = 当前域；生产填主域
AUTH_COOKIE_SECURE=true                # 生产 true，本地 false

# Email (SMTP)
SMTP_HOST=smtpdm.aliyun.com
SMTP_PORT=465
SMTP_USER=noreply@yourdomain.com
SMTP_PASSWORD=
SMTP_FROM_NAME=Research Assistant

# 阿里云短信
ALIYUN_SMS_ACCESS_KEY_ID=
ALIYUN_SMS_ACCESS_KEY_SECRET=
ALIYUN_SMS_SIGN_NAME=
ALIYUN_SMS_TEMPLATE_LOGIN=SMS_xxxxx
ALIYUN_SMS_TEMPLATE_REGISTER=SMS_xxxxx

# QQ 互联（注意：和现有 QQ_BOT_* 是两套不同的凭证）
QQ_OAUTH_APP_ID=
QQ_OAUTH_APP_KEY=
QQ_OAUTH_REDIRECT_URI=https://yourdomain.com/api/auth/qq/callback
```

`requirements.txt` 新增：
```
passlib[bcrypt]>=1.7.4
python-jose[cryptography]>=3.3.0
aiosmtplib>=3.0.0
alibabacloud-dysmsapi20170525>=2.0.24
httpx>=0.27.0      # QQ OAuth 调用，已有则跳过
```

---

## 7. 实施计划（4 个 PR）

### PR-1：鉴权骨架 + 邮箱登录（合并第 1、2 项）
**预期工时**：1.5 天
**外部依赖**：无（可立即开始）

文件改动：
```
新增 app/api/auth_router.py
新增 app/api/deps.py
新增 app/services/auth_service.py
新增 app/services/email_service.py
新增 app/storage/postgres/users_repo.py
新增 app/storage/postgres/migrations/001_users.sql
新增 app/api/static/login.html
新增 app/api/static/register.html
新增 app/api/static/auth.js
新增 app/api/static/auth.css
修改 app/api/server.py        加入路由 + 给现有接口套依赖
修改 requirements.txt
修改 .env.example
```

完成后里程碑：本地能用邮箱注册、激活、登录、退出、找回密码；现有功能未登录时被拦截。

### PR-2：手机短信登录
**预期工时**：0.5 天
**外部依赖**：阿里云短信签名 + 模板审核（提前 3 天提交）

文件改动：
```
新增 app/services/sms_service.py
修改 app/api/auth_router.py    加 phone/* 路由
修改 app/api/static/login.html 加手机 Tab
```

### PR-3：QQ OAuth
**预期工时**：1 天
**外部依赖**：ICP 备案完成 + QQ 互联应用审核通过

文件改动：
```
新增 app/services/oauth/base.py        OAuthProvider 抽象类
新增 app/services/oauth/qq_provider.py
修改 app/api/auth_router.py            加 qq/* 路由
修改 app/api/static/login.html         加 QQ 图标按钮
```

### PR-4：账号管理页 + 收尾
**预期工时**：0.5 天

文件改动：
```
新增 app/api/static/account.html
修改 app/api/static/index.html         顶部用户菜单 + 401 拦截器（< 30 行）
新增 tests/test_auth_*.py              单元测试
```

**累计工时**：3.5 天编码 + 等审核期间穿插。

---

## 8. 上线时间表（含合规等待）

| 周 | 编码 | 并行进行的合规事项 |
|----|------|--------------------|
| W1 Day 1 | 提交域名 ICP 备案、阿里云短信签名/模板申请 | — |
| W1 Day 1–3 | PR-1（鉴权骨架 + 邮箱） | ICP 等待中 |
| W1 Day 4 | PR-2（手机），假设短信审核已过 | ICP 等待中 |
| W2 Day 1 | ICP 通过 → 立即提 QQ 互联应用审核 | — |
| W2 Day 2–4 | PR-4（账号管理页 + 测试） | QQ 审核中 |
| W3 | QQ 审核通过 → PR-3（QQ OAuth） | — |
| W3 末 | 灰度上线 | — |

**最早上线时间：T+3 周**（瓶颈是 ICP 备案 7–20 天）。

---

## 9. 风险与对策

| 风险 | 概率 | 影响 | 对策 |
|------|------|------|------|
| ICP 备案被退回 | 中 | 整体上线推迟 1–2 周 | 提前确认主体材料齐全；准备 2 个备选域名 |
| 短信被恶意刷 | 中 | 短信费暴涨 | 限频 + 图形验证码（本期占位，下期接 hCaptcha） |
| QQ 互联回调域名变更 | 低 | QQ 登录失效 | 配置走 `.env`，不写死 |
| 现有 `index.html` 401 拦截器影响功能 | 低 | 用户掉登录 | 只在明确返回 401 时跳转；其它错误码不动 |
| `.env` 里 `QQ_BOT_*` 和 `QQ_OAUTH_*` 混淆 | 高 | 配错登录不通 | 命名清晰 + 在 `.env.example` 显式注释两者区别 |
| 现有 sessions 文件没有 owner_user_id | — | 本期兼容处理 | 旧 session 可被任何登录用户读到；下期做迁移脚本 |

---

## 10. 后续待办（不在本期）

- 微信网站扫码登录（待企业主体确认）
- 实名认证（工信部要求 IM 类产品做，看产品形态）
- 双因子认证（TOTP）
- 旧 session 数据迁移脚本（赋 owner_user_id）
- 管理员后台
- 套餐 / 配额 / 计费

---

## 11. 决策待办

在 PR-1 启动前需要明确：

1. **主体类型**：个人 / 企业？（影响微信登录是否可做、备案材料） #个人
2. **生产域名**：是否已经有？是否已备案？                      #无，没有备案
3. **短信服务商**：阿里云 / 腾讯云？（默认阿里云）              #腾讯云吧，我的服务器是腾讯云
4. **邮件服务商**：阿里云邮件推送 / Resend / 自建 SMTP？       #阿里云邮件
5. **是否同步加图形验证码**：本期占位 vs 直接接 hCaptcha       #可以加
