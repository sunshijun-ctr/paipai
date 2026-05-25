# 前端循序渐进改造方案

> 范围：把当前的单文件 `app/api/static/index.html`（12,595 行）改造到能稳定上线、可长期维护。
> 原则：**不大爆炸式重写**，分阶段推进，每个阶段独立可发布。
> 部署目标：自己的云服务器 + Caddy 反代（不走 Vercel）。

---

## 0. 目标与边界

### 本方案要解决的
- 上线前的安全 / 稳定性 / 可观测性
- 长期维护性：让"加新功能"不再每次都要在 12k 行里找位置
- 部署体验：改一次能可靠地推到生产，不需要让用户 Ctrl+F5

### 本方案不做的
- 一次性把整个前端重写成 React
- 改 UI 设计（视觉风格延续 paipai 紫色调）
- 上 Next.js（后端是 FastAPI，不需要 SSR / API routes）
- 上 Vercel（已有云服务器，没必要拆部署链）

### 现状盘点

```
12,595  行     单文件 index.html（HTML+CSS+JS）
   352  函数   全局命名空间
   253  事件   inline onclick= 与 addEventListener 混用
    89  globals  顶层 let/const/var
    88  XSS-prone  innerHTML 直接赋值
    35  responsive @media 规则
    31  a11y     aria-* 属性（少）
     5  external <script src=…>
```

---

## 阶段 1 · 上线前必修（1-2 天，**blocker**）

任一项不做都不应该上线。

### 1.1 XSS 审计 + DOMPurify（半天）

**问题**：88 处 `innerHTML =` 任何一处把 API 返回内容（笔记标题、文献标题、用户名、错误消息……）直接拼接，都是存储型 XSS。一个用户在笔记标题里写 `<img src=x onerror=alert(1)>`，下次自己或管理员打开笔记列表就被执行。

**做法**：
1. `grep -n "innerHTML\s*=" app/api/static/index.html` 全列出来
2. 三类处理：
   - 纯文本场景 → 改 `textContent`（最常见，绝对安全）
   - Markdown 渲染 → 已经用了 `marked`，**末尾接 DOMPurify**：`DOMPurify.sanitize(marked.parse(text))`
   - 必须保留 HTML 标签的（小概率）→ 显式调用 `DOMPurify.sanitize(html, {ALLOWED_TAGS: [...]})`
3. CDN 一行：`<script src="https://cdn.jsdelivr.net/npm/dompurify@3/dist/purify.min.js"></script>`（或下载到 `static/vendor/`）

**验收**：
- [ ] 用户笔记标题输入 `<img src=x onerror=alert('XSS')>`，渲染时不弹窗
- [ ] 文献标题输入同样字符串，列表和详情都不弹窗
- [ ] 错误消息 toast 渲染同样字符串不弹窗

### 1.2 Content-Security-Policy 头（10 分钟）

**问题**：XSS 审计是第一层防御，CSP 是第二层。即使审计漏了一处，CSP 也能拦住外部脚本注入。

**做法**：完整 Caddyfile 模板已经在 [`docs/deploy/Caddyfile.example`](./deploy/Caddyfile.example)，包含 CSP + 1.5 + 1.7 三项一起。等拿到域名 sed 替换 `${YOUR_DOMAIN}` 即可。

`'unsafe-inline'` 短期保留，等阶段 2.3 把 inline 样式/事件清理掉再收紧成 nonce-based CSP。

**验收**：
- [ ] 浏览器 DevTools → Network → 看响应头有 `content-security-policy`
- [ ] 在控制台执行 `eval("alert(1)")` 被拦（'unsafe-eval' 没开）

### 1.3 静态资源缓存破坏（10 分钟）

**问题**：当前 `index.html` 改一版，已访问的用户浏览器缓存到旧版（带新 API 调用 → 报错）。生产上线两周左右就会有人遇到。
更深的问题：阶段 2 会拆出 `chat.js`、`base.css` 等子文件，如果它们带长缓存（max-age=1y）但路径不带版本号，改了用户照样拿旧版。

**做法 — 一套版本号覆盖入口 + 子文件**：

```python
# app/api/server.py
import hashlib
import pathlib

def _build_version() -> str:
    """Hash of git HEAD (preferred) or mtime of static/ root (fallback)."""
    try:
        head = pathlib.Path(".git/HEAD").read_text().strip()
        if head.startswith("ref:"):
            ref = pathlib.Path(".git") / head.split(" ", 1)[1]
            return ref.read_text().strip()[:8]
        return head[:8]
    except Exception:
        # Fallback: walk static/ and hash mtimes
        h = hashlib.md5()
        for p in sorted(pathlib.Path(_static_dir).rglob("*")):
            if p.is_file():
                h.update(f"{p}:{p.stat().st_mtime_ns}".encode())
        return h.hexdigest()[:8]

_STATIC_VERSION = _build_version()

@app.get("/")
async def app_index(user=Depends(optional_user)):
    return RedirectResponse(f"/static/index.html?v={_STATIC_VERSION}")
```

`index.html` 里所有子资源用同一个版本号 —— 用一个简单的服务端模板替换或者纯前端读取：

```html
<!-- 选项 A：服务端 Jinja2 渲染 index.html 时填模板变量 v -->
<link rel="stylesheet" href="/static/css/base.css?v={{ v }}">
<script type="module" src="/static/js/app.js?v={{ v }}"></script>

<!-- 选项 B：纯前端 — 顶部读取 location.search 注入到所有 link/script -->
<meta name="app-version" content="v=abc12345">
<script>
  const v = document.querySelector('meta[name=app-version]').content;
  // 给所有 <link rel=stylesheet> / <script type=module> 补 ?v=
</script>
```

**收益**：任意子文件改动 → version 变 → 所有引用都拿到新 URL → 浏览器不命中旧缓存。

**验收**：
- [ ] 改 `index.html` 重启，刷新看到根路径 `?v=` 变了
- [ ] 改 `static/css/base.css` 重启，刷新看到 `base.css?v=` 同步变了
- [ ] 未改动的子文件命中缓存（响应头 `304 Not Modified` 或本地缓存命中）

### 1.4 Sentry 前端错误监控（半小时）

**问题**：现在用户反馈"页面卡住 / 白屏"只能靠后端日志猜。前端报错完全不可见。

**做法**：
1. Sentry 免费档（开发者档，5k errors/month）注册
2. 加 4 行 SDK：

```html
<script src="https://browser.sentry-cdn.com/8.0.0/bundle.min.js"
        integrity="..."  crossorigin="anonymous"></script>
<script>
  Sentry.init({
    dsn: "https://xxx@sentry.io/xxx",
    environment: window.location.hostname === "localhost" ? "dev" : "prod",
    tracesSampleRate: 1.0,   // 初期小流量先全采，等 DAU 上 1k 再降到 0.1
  });
</script>
```

3. WS error / fetch error 在 catch 里加 `Sentry.captureException(e)`

**采样率注意**：开发者档 5k errors/月。如果你 DAU 只有几十，0.1 可能几天才出一条 trace，性能问题完全看不见。先 1.0 全采，观察一周用量再决定要不要降。

**验收**：
- [ ] 在控制台执行 `throw new Error("test")`，Sentry 后台几秒内出现
- [ ] dev 环境的错误标记 `environment=dev`，生产 = `prod`

### 1.5 Caddy 静态资源压缩 + 长缓存（10 分钟）

**问题**：12k 行单文件无压缩传输大约 400 KB+。Caddy 默认不开 gzip。

**做法**：模板见 [`docs/deploy/Caddyfile.example`](./deploy/Caddyfile.example) —— `encode zstd gzip` + 路径分级缓存（静态 `max-age=31536000, immutable` / HTML `no-cache` / API `no-store`）已经写好。

**验收**：
- [ ] curl -I 看 `content-encoding: gzip` 或 `zstd`
- [ ] 静态资源响应头有 `cache-control: public, max-age=31536000, immutable`
- [ ] `/api/*` 响应头有 `cache-control: no-store`
- [ ] HTML 响应头有 `cache-control: no-cache`

### 1.6 认证 / 授权快速审计（半天）

**问题**：XSS 是数据层面，但**认证 + 授权**是入口层面，同样属于上线 blocker。当前代码的几个高风险点：

1. **JWT 存哪？** 必须是 `HttpOnly` cookie；如果存 `localStorage`，XSS 一旦命中就能直接偷走 token —— 1.1 的审计只能减少 XSS 概率，不能消除
2. **CSRF 防御**：用 HttpOnly cookie 鉴权的话，所有改状态的 API 必须有 CSRF 防护（要么 SameSite=Strict cookie，要么显式 CSRF token）
3. **API 路由鉴权清单**：每个 `@app.post / @app.delete / @app.put` 必须有 `Depends(require_user)` 或等价检查；只读 GET 也要看是否泄漏其他用户数据
4. **会话/资源所有权**：编辑/删除接口必须校验"这条数据是当前用户的"，光验登录不够

**做法**：

```bash
# 1. token 存储位置检查
grep -r "localStorage" app/api/static/index.html
# 期望：只有非敏感数据（如折叠状态、主题偏好），绝对不应该有 token / refresh_token

# 2. SameSite cookie 检查
grep -n "set_cookie" app/api/auth_router.py
# 期望：samesite="strict" 或 "lax"，secure=True（生产环境）

# 3. 鉴权覆盖率审计
grep -nE "^@app\.(post|put|delete|patch)" app/api/server.py | \
    grep -v "Depends(require_user\|Depends(optional_user"
# 期望：输出为空。任何输出都是潜在的未鉴权改写接口

# 4. 资源所有权检查
grep -rn "Depends(require_user)" app/api/ | wc -l
grep -rn "owner_user_id\|user_id == user\.id\|_ensure_.*_owner" app/api/ | wc -l
# 两个数字应该接近 —— 每个 require_user 接口都该跟一个所有权检查
```

**验收**：
- [ ] `localStorage` 里搜不出 token / jwt / refresh
- [ ] cookie 设置 `HttpOnly; Secure; SameSite=Lax`（或 Strict）
- [ ] 所有写接口都有 `require_user`
- [ ] 用 user A 的会话 ID 调 user B 的 API 返回 403/404，不是 200
- [ ] 删别人的笔记 / 文献 / 会话不能成功

### 1.7 其他安全响应头（5 分钟）

防御 MIME sniffing、clickjacking、referrer 泄漏。完整配置在 [`docs/deploy/Caddyfile.example`](./deploy/Caddyfile.example) 的 `header { ... }` 段：`X-Content-Type-Options`、`X-Frame-Options`、`Referrer-Policy`、`Permissions-Policy`、`X-XSS-Protection`、`Strict-Transport-Security`。

**验收**：
- [ ] curl -I 看到上述六个头

### 阶段 1 验收清单

整体完成才算可上线：

- [ ] 1.1 88 处 innerHTML 全部审计完，高危处接 DOMPurify
- [ ] 1.2 Caddy 配置 CSP 头
- [ ] 1.3 入口 + 所有子文件统一 `?v=<version>`
- [ ] 1.4 Sentry SDK 接入，初期采样率 1.0
- [ ] 1.5 Caddy 开 gzip + 长缓存 + 短 cache-control 区分
- [ ] 1.6 认证存储 / SameSite / 鉴权覆盖率 / 资源所有权四项过
- [ ] 1.7 X-Content-Type-Options / X-Frame-Options / Referrer-Policy 头

---

## 阶段 2 · 上线后稳定期（2-4 周，可与日常开发并行）

不是 blocker，但每一项都让长期维护更轻松。

### 2.1 拆分思路：按 feature 切，不按类型切

**反模式**：把 7k 行 JS 全剪到一个 `app.js`。问题没解决 — 还是一个巨型文件，只是搬了家。

**正解**：按视图 / 功能模块切，每个模块拿到自己的 CSS + JS 文件对：

```
app/api/static/
├─ index.html              # 瘦身后 ~2k 行：只有 DOM 骨架 + script/link 标签
├─ css/
│  ├─ base.css             # CSS 变量、reset、字体
│  ├─ layout.css           # 主网格、左侧栏、顶栏
│  ├─ chat.css             # 聊天气泡、思考动画、流式
│  ├─ sidebar.css          # 会话列表
│  ├─ library.css          # 文献库视图
│  ├─ notes.css            # 便签视图
│  ├─ settings.css         # 设置 / 个人资料
│  └─ research.css         # plan card（HITL）
└─ js/
   ├─ constants.js         # INTENT_LABELS、tool 中文名、固定文案
   ├─ api.js               # fetch 封装 + 鉴权
   ├─ ws.js                # WebSocket 连接 + 消息分发
   ├─ chat.js              # addMsg / showThinking / 流式 markdown
   ├─ sessions.js          # 会话 CRUD / 切换
   ├─ library.js           # 文献库
   ├─ notes.js             # 便签
   ├─ settings.js          # 个人资料 + LLM 配置
   ├─ research.js          # plan card
   └─ app.js               # 入口：启动 + 各模块挂载
```

**HTML 引用**（用原生 ES modules，不需要构建工具）：

```html
<link rel="stylesheet" href="/static/css/base.css?v=...">
<link rel="stylesheet" href="/static/css/layout.css?v=...">
<link rel="stylesheet" href="/static/css/chat.css?v=...">
<!-- ...其他 css -->
<script type="module" src="/static/js/app.js?v=..."></script>
```

`app.js` 里：

```js
import { initChat }     from './chat.js';
import { initSessions } from './sessions.js';
import { initLibrary }  from './library.js';
// ...
initChat();
initSessions();
// ...
```

每个模块 `export` 自己的初始化函数，互相之间通过 `import` 拿到需要的引用，**全局变量会大幅减少 —— 但不会"自然消失"**。如果代码里还有 `window.foo = ...` 这种主动挂全局的写法（旧 inline `onclick` 配套需要），必须刻意清理掉换成事件绑定才会真正干净。审计办法：拆完后 `grep -n "window\." static/js/*.js`，列出来逐个判断是否真的需要全局。

### 2.1.1 关于 bundler：原生 ESM vs 引 Vite

**只用原生 ESM 的风险**：

- 10 个 JS 文件 = 10 个独立 HTTP 请求（HTTP/2 多路复用能缓解但不能消除往返）
- 模块间循环依赖（`a.js → b.js → a.js`）在没有构建工具的情况下只能靠运行时报错排查
- 改个变量名要靠 grep，没有 IDE 级别的重命名

**两个走法**：

**走法 A — 纯原生 ESM（零工具）**：
适合：模块数量少（< 8 个）、模块间引用关系清晰、团队就 1-2 人。
省事：不引入 Node 依赖，部署就是把 `.js` 拷到 `static/`。

**走法 B — 引 Vite 只做 bundle（推荐，如果阶段 3 计划走 React）**：
```bash
npm create vite@latest -- --template vanilla
# 删掉 Vite 模板的 main.js，把 static/js/ 的代码搬进来
```
Vite 配置打包到 `app/api/static/dist/`，FastAPI 直接服务 dist。**不用 React、不用 TS、不用 Tailwind** —— 就只用 Vite 做：
- 自动 bundle + tree-shake + minify
- 内容哈希文件名（`app.abc123.js`），自动解决 1.3 的子文件缓存破坏问题
- dev server 带 HMR，改 JS 不刷页面
- 循环依赖编译期警告

成本：多一个 `npm install` + `npm run build` 步骤。但 Vite 默认配置开箱即用，几乎零配置。

**何时选 A、何时选 B**：
- 如果你确定不走阶段 3 → 选 A，省事
- 如果阶段 3 大概率要走（React 化） → **选 B**，这样从 2.3 拆 JS 开始就习惯 build 流程，阶段 3 是无缝过渡
- 团队 2+ 人 → 选 B（循环依赖排查太痛苦）

我**推荐 B**。

### 2.2 CSS 拆分

**实际推进（已完成第 1 步）**：

**Step 1 — 单文件提取（已合，2025-05-15）**：
- 12,668 行 `index.html` → **6,402 行** + `static/css/app.css`（6,265 行）
- FastAPI 加 `app.mount("/static", StaticFiles(directory=...))` mount
- `<style>` 块替换为 `<link rel="stylesheet" href="/static/css/app.css?v={{V}}">`
- 配合 1.3 的 `{{V}}` 版本号 → 改 CSS 不刷 JS 缓存，改 JS 不刷 CSS 缓存

**Step 2 — 按视图细分（暂缓）**：
现状 CSS 块里有 26+ 个分节注释，但**很多是迭代叠加的覆盖层**（`warm beige`、`teal UI refactor`、`rice-white theme switcher` 等），简单按"哪个视图用"切容易破坏 cascade 顺序。安全分两步走：

1. **先收敛重复 / 覆盖层**：把同视图的多份 refactor 合并成单份，去除 dead rule
2. **再按视图切**：合并后再做 8 文件拆分

Step 2 收益边际：CSS 文件本身可以编辑了（IDE 大文件好打开），并行下载收益已经拿到，按视图切的主要价值是"多人改不同视图零冲突"——单人开发暂时不痛。

**做法（Step 2 启动条件，按需）**：
1. 在 `app.css` 顶部按分节注释切出 8 个候选文件
2. 共用的 CSS 变量、`*` reset、`body` / `h1-h6` 默认样式 → `base.css`
3. 各视图独立的 → 对应文件
4. `<link>` 顺序严格匹配原始块内顺序，cascade 不变

**收益**：
- CSS 改一行不刷 JS 缓存 ✅（Step 1 已经拿到）
- 多人改不同视图零冲突（Step 2 才能拿到）
- HTTP/2 并行下载（Step 2 才能拿到，单文件 240KB 也不算大）

### 2.3 JS 拆分 + inline event handler 清理

**做法**：
1. 按上面的模块图把 `<script>` 块切到 `static/js/*.js`
2. 每个文件 `export` 公开 API，内部细节不导出
3. **同时**：253 处事件处理里 inline 的 `onclick="foo()"` 全改成 `addEventListener('click', foo)`
   - inline handler 强制 `foo` 是全局函数，跟模块化思路冲突
   - 这一步省下来后续阶段 3 React 化时要回来重做

**收益**：
- 改一个功能只需进一个文件
- 模块导出的函数有名字空间，全局变量大幅减少
- 给阶段 3 引入 bundler 直接打好基础

### 2.4 HTML 是否拆分？（建议不拆）

**做法 A（不拆，推荐）**：HTML 保持单文件 `index.html`。瘦身后只剩约 2k 行 DOM 结构 + script/link 标签，已经可读了。

**做法 B（用 Jinja2 partials，可选）**：把视图块抽到 `templates/partials/*.html`，FastAPI 路由换成 `TemplateResponse`。

**为什么推荐 A**：
- 收益边际：HTML 拆完后单文件只是不到 2k 行，搜索定位不痛
- 引入 Jinja2 模板渲染要改 FastAPI 路由，跟将来 React + Caddy 静态托管不兼容
- 阶段 3 真上 React 后 HTML 几乎不需要写，partial 文件也是要删的

### 2.5 ARIA + 键盘可访问性体检

**现状**：352 函数才 31 个 ARIA 属性。键盘用户可能无法用主要功能。

**做法**：
- 用 Chrome DevTools → Lighthouse → Accessibility 跑分
- 重点修：聊天输入框、会话列表、文献库列表、模态框（Esc 关闭 / Tab 锁定）
- 不强求 100 分，70+ 即可

**何时做**：如果目标用户是个人研究者可以晚做；如果要谈 ToB 客户必须做。

### 2.6 重复字符串 + magic numbers 抽常量

**举例**：进度条文案、INTENT_LABELS、tool 中文名 …… 现在散落在前后端各处。

**做法**：建 `static/js/constants.js`（已在 2.1 模块图里预留），所有"展示文案 + 后端 enum 字符串"集中到这里。后端如果有对应的 enum 用脚本一对一同步。

### 2.7 拆分前的回归测试清单（必做）

**问题**：拆 7k 行 JS 没有 safety net，改完一个模块很容易悄悄把另一个模块打坏。这个项目目前没有前端自动化测试，所以至少要有一份**手动测试清单**走完才能合并。

**做法**：在每次合并拆分 PR 前，按这个清单点一遍：

```markdown
- [ ] 登录 / 注册 / 忘记密码 / 登出
- [ ] 创建会话 / 切换会话 / 删除会话
- [ ] 发一条普通消息，看到流式回复
- [ ] 发一条 research_task（"调研一下 …"），plan card 出现，approve / cancel / modify 三种交互各走一遍
- [ ] 文献库：新建、上传 PDF、检索、删除
- [ ] 便签：新建、编辑、检索、删除
- [ ] 设置页：改头像、改 LLM 配置、保存后生效
- [ ] 切换暗色模式（如果有）/ 折叠侧栏 / 移动端宽度
- [ ] WebSocket 断线重连
- [ ] 上传图片 → 触发 image_understanding workflow
```

**何时演化为自动化**：当上面这套手测一遍超过 15 分钟，就该花一天上 Playwright 写自动化。在那之前，手测清单比假装我们有测试更诚实。

### 阶段 2 推荐顺序

按"收益/成本 + 依赖关系"排：

1. **2.7 落实回归测试清单** — 先有 safety net，不然拆什么都心虚
2. **2.2 CSS 拆分** — 风险最低，热个手
3. **2.1.1 决定 bundler 走法**（A 原生 / B Vite-only），把工程脚手架先搭好
4. **2.3 JS 拆分** — 主战场，按模块逐个迁，每迁完一个跑一遍 2.7 清单
5. **2.6 常量抽出** — 跟 2.3 顺手做
6. **2.5 ARIA** — 看合规需求
7. **2.4 HTML 不拆**（推荐保持单文件）

---

## 阶段 3 · 渐进迁移到 React（3-6 个月，仅当需要时）

**触发条件**（满足任一个再启动）：
- 阶段 1+2 做完后**新增**功能你仍然觉得在 vanilla JS 里写痛苦
- 要做复杂交互（拖拽、虚拟列表、富文本编辑器）
- 有第二个前端开发者加入团队

**核心原则：新功能用 React，旧功能不动**。

### 3.1 Vite + React + TS + Tailwind + shadcn/ui 工程初始化

```bash
npm create vite@latest paipai-web -- --template react-ts
cd paipai-web
npm install -D tailwindcss postcss autoprefixer
npx tailwindcss init -p
npx shadcn@latest init
```

**目录建议**：
```
paipai-web/
├─ src/
│  ├─ components/      # React 组件
│  ├─ widgets/         # 嵌入旧 HTML 的"岛屿"组件
│  ├─ lib/             # API 客户端、WS 封装
│  ├─ types/           # 从 FastAPI /openapi.json 生成的 TS 类型
│  └─ main.tsx
├─ vite.config.ts
└─ ...
```

**Vite 配置打包到 `app/api/static/widgets/`**：
```ts
build: {
  outDir: "../research-assistant/app/api/static/widgets",
  emptyOutDir: false,  // 不要清空 — 还要保留 index.html
  rollupOptions: {
    output: {
      entryFileNames: "[name]-[hash].js",
      assetFileNames: "[name]-[hash][ext]",
    },
  },
}
```

### 3.2 Island 模式：把单个组件嵌进旧 HTML

挑一个**最痛的视图**先做，建议从 **plan 卡片**或**设置页**开始（独立性强）。

```html
<!-- index.html 里某处 -->
<div id="plan-checkpoint-root"></div>
<script type="module" src="/static/widgets/plan-checkpoint-abc123.js"></script>
```

```tsx
// paipai-web/src/widgets/plan-checkpoint.tsx
import { createRoot } from "react-dom/client";
import { PlanCheckpointCard } from "../components/PlanCheckpointCard";

window.renderPlanCheckpoint = (data) => {
  const el = document.getElementById("plan-checkpoint-root");
  if (el) createRoot(el).render(<PlanCheckpointCard {...data} />);
};
```

旧的 vanilla JS 里 `renderResearchPlanCheckpoint(d)` 改成调 `window.renderPlanCheckpoint(d)`。这样 plan 卡片就是 React 写的，其他全不动。

### 3.3 OpenAPI → TypeScript 类型生成（一次性，长期收益）

```bash
npx openapi-typescript http://localhost:8000/openapi.json -o src/types/api.ts
```

每次 FastAPI 接口改了重跑这条命令。前端 fetch 就有类型补全 + 编译期检查。

### 3.4 替换顺序建议

按"独立性 + 痛点"双维度排：

| 视图 | 独立性 | 痛点 | 建议顺序 |
|---|---|---|---|
| Plan card (HITL) | 高 | 中 | 1（练手） |
| 设置 / 个人资料 | 高 | 低 | 2 |
| 文献库管理 | 中 | 高 | 3 |
| 便签管理 | 中 | 中 | 4 |
| 会话侧栏 | 低 | 中 | 5 |
| 聊天主区 + WS 流式 | 低 | 高 | **最后**（最复杂） |

**坚持的纪律**：任何**新功能**只用 React 写；任何 React 化的视图就**删掉**旧 HTML 里的对应代码。这样 `index.html` 行数随时间单调下降，终态可能就只剩一个 `<div id="root">` + vendor scripts。

### 3.5 终态切换

当 React 那边覆盖了 80%+ 的视图后，把 React 整个挂到 `/app/*` 路由，旧 `index.html` 重命名为 `legacy.html` 留作 fallback / 紧急回滚。再过一两个月没人反馈再删。

---

## 决策点（先定再动）

### D1：阶段 1 谁来做？什么时候做？

- 推荐：上线前一周专门花 1-2 天做完。集中比分散在每天 30 分钟里做更可靠。

### D2：阶段 2 全做还是挑做？

- 必做：**2.7 测试清单** → **2.2 CSS 拆分** → **2.1.1 选 bundler** → **2.3 JS 拆分 + 2.6 常量** —— 这条主线是后续工程化的前置。
- 可延后：**2.5 ARIA**（除非有合规需求）。
- 推荐不做：**2.4 HTML 拆 partials**（边际收益低，跟阶段 3 思路冲突）。

### D2.1：bundler 选 A 还是 B？

- 不确定阶段 3 走不走 → 选 A（原生 ESM），省事
- 倾向阶段 3 走 React → 选 B（Vite-only-bundle），现在搭好脚手架，阶段 3 直接加 React 插件就行，不用重做工程

### D3：阶段 3 走不走？什么时候启动？

- 不必预先决定。先做完 1+2 上线，观察 2-3 个月。
- 如果"加新功能不痛苦"→ 不动 React，省事。
- 如果"加新功能时频繁踩坑全局变量 / 找代码"→ 启动 3.1，从一个 widget 开始。

---

## 不会做的事

| 反模式 | 为什么不做 |
|---|---|
| 一次性把 12k 行重写成 React | 几周不能上线，风险全压在一次大切换 |
| 引入 Next.js | 你不需要 SSR / API routes / 文件路由，FastAPI 全包了 |
| 拆前端到 Vercel | 已有云服务器，拆开多一个域名 + CORS + cookie domain 配置 |
| 引入 Redux / Zustand 大型状态管理 | 你的状态主要在 WS + 后端 session，前端不需要复杂全局 store |
| 上 SSR | 应用都在登录后，SEO 不相关 |

---

## 跟踪

- 阶段 1 完成情况：见 1.1-1.5 各 checkbox
- 阶段 2 完成情况：每完成一项在本文档勾掉
- 阶段 3 启动决策：满足触发条件后另起一个 `frontend-react-migration.md`
