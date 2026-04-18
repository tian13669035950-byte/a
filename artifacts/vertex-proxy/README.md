# Vertex AI Proxy 部署说明

把 Google Gemini（通过 Vertex AI 控制台的匿名接口）包装成 OpenAI 兼容 API，给 SillyTavern / Open-WebUI / 任何 OpenAI 客户端用。**无需 Google Cloud 账号，无需 API Key，免费**。

---

## 🚀 5 分钟上手（新 Replit 账号）

### 第 1 步：导入仓库

1. 登录 [replit.com](https://replit.com) → 右上角 **Create Repl** → **Import from GitHub**
2. 填本仓库地址，等导入完成（含 xray 二进制约 30MB，需要 1-2 分钟）

### 第 2 步：启动

工作区会自动出现 **`artifacts/vertex-proxy: Vertex AI Proxy`** 工作流，点 **Run**。

启动日志看到这两行就 OK：
```
[Admin] 使用内置默认密码：1966197012
✅ 服务启动完成，系统运行中...
```

### 第 3 步：登录 admin 配置

| 项 | 值 |
|---|---|
| URL | `https://你的域名.replit.dev/admin` |
| 密码 | `1966197012`（已硬编码，可用 `ADMIN_PASSWORD` 环境变量覆盖） |

进 admin 后：
- **API Keys** tab：默认自带 `sk-123456`，建议改成你自己的随机串
- **Proxy & Nodes** tab：检查 git 里带的 `cached_nodes.json` 是否还能用；如果订阅过期就换一条新订阅链接

### 第 4 步：连接 SillyTavern

| 设置项 | 值 |
|---|---|
| Chat Completion Source | OpenAI Compatible / Custom |
| API URL | `https://你的域名.replit.dev/v1` |
| API Key | 第 3 步设的那个 |
| Model | `gemini-2.5-flash`（推荐起手）|

发一条消息，能回就成功。

> **配额提示**：`gemini-2.5-pro` 的免费配额很容易耗尽，**日常一律用 `gemini-2.5-flash`**。配额用光会等几小时滚动恢复。

---

## 📦 发布到 Replit Deployments（生产）

### 部署类型选择

| | Reserved VM | **Autoscale**（推荐免费）| Workspace 工作流 |
|---|---|---|---|
| 月费 | 要钱 | 有免费额度 | 完全免费 |
| 闲置时 | 一直跑 | 缩到 0，不烧额度 | 工作区关了就睡 |
| 冷启动 | 无 | 3-5 秒（git 里已打包 xray + 节点缓存）| 唤醒 5-30 秒 |
| 适合 | 重度使用 | **酒馆个人用，最佳** | 临时测试 |

### 发布前检查清单

#### ✅ 必须确认 git 包含这些文件

```
artifacts/vertex-proxy/
├── bin/xray                       (29MB 二进制，必须 commit)
├── config/cached_nodes.json       (节点缓存，决定冷启动速度)
├── config/active_node.json        (上次激活的节点)
├── config/sub_urls.json           (订阅链接)
└── config/custom_nodes.json       (自定义节点，可空)
```

如果你最近换过节点，建议在工作区 admin 里点一下"刷新节点"，让缓存重写到磁盘后再 commit + deploy，否则部署出去用的是老缓存。

#### ✅ Deployment Secrets（按需设置）

⚠️ Replit 的 dev 环境 secret **不会自动同步到部署**。如果要覆盖默认值，在 Deployments 页面单独加：

| Secret | 必要程度 | 说明 |
|---|---|---|
| `API_KEY` | 可选 | 不加就用默认 `sk-123456`。**如果只是你自己用、URL 不外传，默认就行**；要给别人用就改成自定义随机串 |
| `ADMIN_PASSWORD` | 可选 | 不加就用硬编码的 `1966197012` |
| `SESSION_SECRET` | 推荐 | 从 dev 复制过去 |
| `STRICT_PROXY` | 已默认 `1` | **强制所有出网走 SOCKS5 代理，禁止任何直连**。设 `0` 才允许代理失败时降级直连（仅本地调试用，部署绝对不要关）|

#### ✅ 不需要

- ❌ Google Cloud 项目 / Service Account / GCP 凭据
- ❌ 配置出站白名单（Replit 默认全放行）
- ❌ 给 Vertex API 绑信用卡
- ❌ **手动填运行命令、端口、健康检查** —— 全部由 `artifact.toml` 自动配置

### 部署后第一次操作

1. 访问 `https://你的应用.replit.app/admin`，用你设的 `ADMIN_PASSWORD` 登录
2. 进 "Proxy & Nodes" tab → 检查节点状态。如果显示无节点，加一条订阅链接 → 点"刷新节点列表"
3. 用 SillyTavern 发一条 `gemini-2.5-flash` 测试

### 体感

- **5 分钟内连续聊天**：秒回（实例热的）
- **隔了 10 分钟再发**：第一句等 3-8 秒（冷启动），之后秒回
- **隔了几小时**：同上，第一句稍慢，之后正常

---

## 🤖 给另一个账号 / 另一个 AI 的部署指引

如果要把这个仓库交给另一个 Replit 账号（或者让另一个 AI 帮忙部署），告诉它三步就够：

### 1. 导入仓库
GitHub URL 给它，让它在 Replit 用 **Create Repl → Import from GitHub** 导入。

### 2. 部署
进 Deployments → 选 **Autoscale** → 点 **Deploy**。

**不需要**手动填任何运行命令、端口、健康检查路径——全部已经在 `artifacts/vertex-proxy/.replit-artifact/artifact.toml` 里配好了：

```toml
[services.production.run]
args = ["bash", "-c", 
  "python3 -m pip install -r artifacts/vertex-proxy/requirements.txt -q && cd artifacts/vertex-proxy && python3 main.py"
]

[services.production.run.env]
PORT = "8000"

[services.production.health.startup]
path = "/health"
```

Replit 平台会自动读这个文件配好一切。

### 3. （可选）加 Secret
**自己用的话直接默认就行，跳过这步**。要给别人用再去 Deployments → Secrets 加 `API_KEY` 自定义值。

### ⚠️ 不要碰的文件

- 仓库根目录的 `.replit` 里有一段 `[deployment]` 配置（路径写的是 `cd vertex-proxy` 而不是 `artifacts/vertex-proxy`），但**这个会被 `artifact.toml` 覆盖，实际不生效**
- **不要去改那个 `.replit`** —— 平台认 `artifact.toml`，碰了反而出问题

---

## 🌐 接口列表

服务监听端口由 Replit 注入的 `PORT` 环境变量决定（dev 默认 8000，部署自动注入）。

| 路径 | 用途 |
|---|---|
| `GET /` | 重定向到 `/admin` |
| `GET /health` | 健康检查（200 = 服务正常）|
| `GET /admin` | 管理后台 UI |
| `POST /admin/api/login` | admin 登录（注意是 `/admin/api/*` 不是 `/api/admin/*`，**见已知问题 #23**）|
| `GET /v1/models` | OpenAI 兼容模型列表 |
| `POST /v1/chat/completions` | OpenAI 兼容聊天接口（**SillyTavern 用这个**）|
| `GET /v1beta/models` | Gemini 原生模型列表 |
| `POST /v1beta/models/{model}:generateContent` | Gemini 原生接口 |
| `GET /proxy-manager` | 完整代理管理 UI（订阅 / 节点 / 测速 / 自定义节点）|
| `GET /proxy-manager/status` | 代理状态 JSON |
| `GET /proxy-manager/list` | 节点列表（加 `?refresh=true` 重拉订阅）|
| `POST /proxy-manager/bench` | 启动并发测速 |

---

## 📚 模型清单

### 真流式（默认，推荐）

直接用模型名，不加前缀：

| 模型 | 速度 | 配额 | 推荐 |
|---|---|---|---|
| `gemini-2.5-flash` | 快 | 宽松 | ⭐ **日常首选** |
| `gemini-2.5-flash-lite` | 最快 | 宽松 | 简单任务 |
| `gemini-2.5-flash-image` | 快 | 宽松 | 支持图片输入 |
| `gemini-2.5-pro` | 中 | **极易耗尽** | 仅特定难任务 |
| `gemini-3-flash-preview` | 慢（30-60s）| 中 | 模型本身慢，不是 bug |
| `gemini-3-pro-image-preview` | 中 | 中 | 支持图片 |
| `gemini-3.1-flash-lite-preview` | 快 | 中 | |
| `gemini-3.1-flash-image-preview` | 快 | 中 | 支持图片 |
| `gemini-3.1-pro-preview` | 中 | 中 | |

### 假流式（`fs-` 前缀）

格式：`fs-` + 任意上面的模型名（如 `fs-gemini-2.5-pro`）。

**真假流式区别**（实测后的结论，跟字面意思有点反直觉）：

| | 真流式（默认） | 假流式（`fs-`）|
|---|---|---|
| 上游接口 | 一样的 batchGraphql | 一样的 batchGraphql |
| 服务端行为 | 拿到一块 → 立刻转发 | 等全部到齐 → 聚合 → 拆字符发 |
| 客户端体感 | 最终也是一坨出（上游本身不是真流，**见已知问题 #7**）| 一坨等待后字符往外冒 |
| 思考过程展示 | 散落 | 聚合后整齐 |
| 出错时能否重试 | ✅ 能 | ❌ 已发就不能 |

**99% 情况选默认真流式**。真要看模型思考过程结构再用 `fs-`。

---

## 🔧 OpenAI 请求参数兼容

| OpenAI 参数 | Gemini 对应 | 说明 |
|---|---|---|
| `temperature` | `temperature` | 0-2，越大越随机 |
| `max_tokens` | `maxOutputTokens` | 输出上限 |
| `top_p` | `topP` | 核采样 |
| `top_k` | `topK` | 候选词数 |
| `stop` | `stopSequences` | 停止词 |
| `n` | `candidateCount` | 候选数 |
| `response_format` | `responseMimeType` | `{"type": "json_object"}` 强制 JSON |
| `tools` / `functions` | `tools.functionDeclarations` | Function Calling |
| `stream` | — | 流式开关 |

---

## ⚙️ 配置文件

### `config/config.json`

```json
{
  "port_api": 2156,           // dev 默认端口（部署时被 PORT 覆盖）
  "max_retries": 4,           // 普通错误重试上限（配额错误另算，见 #26）
  "debug": false,             // true = DEBUG 级别日志
  "log_dir": "logs",
  "admin_password": "1966197012"  // admin 后台密码（被 ADMIN_PASSWORD 环境变量覆盖）
}
```

### `config/api_keys.txt`

每行一个，支持两种格式：
```
sk-123456                              # 简单格式
my-key:sk-abcdef:给 SillyTavern 用     # name:key:description
```

### `config/sub_urls.json`

订阅链接数组：
```json
["https://example.com/sub?token=xxx"]
```

支持 Clash YAML / base64 vless+vmess / 明文 vless+vmess。

### `config/custom_nodes.json`

手动添加的节点（vless:// / vmess:// / 完整 xray JSON），不受订阅刷新影响。

### `config/models.json`

允许的模型名清单。Google 出新模型在这里加一行就能用。

### 状态文件（运行时生成）

| 文件 | 内容 | 说明 |
|---|---|---|
| `config/cached_nodes.json` | 上次拉到的节点列表 | 冷启动跳过订阅拉取的关键 |
| `config/active_node.json` | 上次激活的节点 | 重启时自动恢复 |
| `bin/xray` | xray 二进制 | 缺失时自动下载 |

---

## 🔐 环境变量

| 变量 | 作用 | 不设时 |
|---|---|---|
| `API_KEY` | 客户端访问密钥（自动补 `sk-` 前缀）| 用 `config/api_keys.txt` 里的 `sk-123456` |
| `ADMIN_PASSWORD` | admin 后台密码（最高优先级）| 用 config.json，再 fall back 到硬编码 `1966197012` |
| `SESSION_SECRET` | admin session 签名 | 自动生成临时值（重启失效）|
| `SUB_URL` | 默认订阅链接 | 代码内置 |
| `PORT` | API 端口 | Replit 部署自动注入；dev 用 config.json |
| `KEEPALIVE` | `1` 启用内部保活循环（每 3-7 分随机自 ping）| 关闭。**Autoscale 部署不要开**，会阻止缩到 0 |

---

## 🧭 admin 后台功能

`/admin`，三个 tab：

### 1. Server
- 查看/修改 `port` / `debug` / `max_retries` / 改密码
- 改密码后老 token 不会被踢，重启服务才彻底失效

### 2. API Keys
- 三段式 CRUD（`name : key : description`）
- 同名添加自动覆盖
- 删除立即热加载到 `api_key_manager`

### 3. Proxy & Nodes
- **Status**：当前出口代理 / xray 状态 / Google 可达性 / 节点数
- **Subscriptions**：订阅链接增删
- **Nodes**：节点列表 + 一键启用
- **Manual proxy**：直接填 `socks5://` 或 `http://` 代理

需要测速、配额扫描、country 检测等高级操作，点 "Advanced →" 跳到 `/proxy-manager` 完整面板。

---

## 🛠 /proxy-manager 完整面板

### 订阅链接管理
- 多条订阅链接合并
- 刷新时显示每条结果（✅ 成功几个 / ❌ 失败原因）

### 节点可用性检测（额度扫描）
- 用真实 Gemini 模型试一句话
- 结果：✅ 正常 / 🚫 配额耗尽 / 💀 超时
- 一键删除无效节点

### 一键测速排序
- 并发 TCP ping 所有节点
- 按延迟从低到高重排
- 标 👑 的是最优

> CF 节点测速可能全部相同延迟（任播网络特性），见已知问题 #15

### 出口 IP 检查
- 同时测直连 IP 和代理 IP
- 不同 = 代理生效；相同 = 代理没换出口

### 自定义节点
- 粘贴 vless:// / vmess:// / 完整 xray JSON
- 不受订阅刷新影响

---

## 🔄 请求流程

```
SillyTavern
    ↓  POST /v1/chat/completions  (Authorization: Bearer sk-xxx)
    ↓
API Key 验证中间件
    ↓
OpenAI → Gemini 格式转换
    ↓
抓取 Recaptcha Token（primp 伪装 Chrome TLS 指纹）
    ↓
通过 xray SOCKS5 → cloudconsole-pa.clients6.google.com
    ↓
检测响应：
  - 空回复 → 换节点重试（最多 max_retries=4 次）
  - 配额错误 → 换节点重试（最多 quota_max_retries=2 次）
  - 4xx/5xx → 转 OpenAI 错误格式透传
    ↓
Gemini → OpenAI 格式转换
拆分末包 finish_reason（SillyTavern 兼容）
响应头 Content-Encoding: identity（防压缩）
    ↓
SSE 返回客户端
```

---

## 📋 上传到 GitHub

```bash
git init
git remote add origin https://github.com/你的用户名/你的仓库名.git
git add .
git commit -m "initial commit"
git push -u origin main
```

**建议加进 `.gitignore`**：
```
logs/
*.log
__pycache__/
*.pyc
errors/
```

**不要**加进 `.gitignore`：
- `bin/xray` —— 不打包就要每次冷启动重新下载 10MB
- `config/cached_nodes.json` —— 不打包就要每次拉订阅，冷启动慢
- `config/api_keys.txt` —— 用 `API_KEY` 环境变量覆盖更安全

---

## ⚠️ 已知问题 & 踩坑记录

> 改代码前必读。这些都是真实踩过的坑。

### 1. 生产环境 404（没有注册 artifact.toml）

**现象**：本地开发正常，发布后访问直接 404。  
**原因**：`artifacts/vertex-proxy/.replit-artifact/artifact.toml` 不存在，Replit 部署系统找不到这个服务。  
**解决**：确保该文件存在（本仓库已修复）。

---

### 2. 生产环境 TLS 错误（curl_cffi 不兼容）

**现象**：开发环境正常，生产容器报 `TLS connect error: invalid library`。  
**原因**：`curl_cffi` 编译时依赖系统 OpenSSL，生产容器版本不同。  
**解决**：换用 `primp`（Rust 静态链接 BoringSSL）。

---

### 3. 生产环境每次重启没有代理节点

**现象**：生产容器无状态，重启后节点丢失。  
**解决**：启动时自动从 `config/sub_urls.json` 拉订阅 + 选第一个可用节点。

---

### 4. SOCKS5 代理对 Vertex AI 端点无效（旧 CF 节点）

**现象**：CF 节点代理 `cloudconsole-pa.clients6.google.com` 失败。  
**原因**：部分 CF 优选节点只代理 Cloudflare IP 段，Google 端点不在内。  
**解决**：代理失败时自动降级直连。

---

### 5. 单 IP 配额很快耗尽（429 Resource Exhausted）

**现象**：连续几次请求就报 `Resource has been exhausted`。  
**解决**：代理优先 + 429 自动切节点。CF 节点出口 IP 可能仍在同一 ASN 下被集体限速，要真正换出口需添加非 CF 节点。

---

### 6. SillyTavern 流式输出内容被截断

**现象**：AI 回复不完整或空白。  
**原因**：Gemini 的最后一个 SSE chunk 同时包含内容和 `finish_reason: stop`，SillyTavern 看到 `finish_reason` 就停。  
**解决**：拆成两个 chunk 发送。

---

### 7. batchGraphql 接口本身不是真流式

**现象**：`stream: true` 但 AI 回复还是等很久才一次性出现。  
**原因**：底层 `batchGraphql` 等全部生成完才一次性返回。**这是上游限制，无法绕过**（除非换需要付费的 API）。  
**说明**：所谓"真流式"和"假流式"在客户端体感上几乎一样，详见模型清单章节。

---

### 8. httpx 不支持 SOCKS5（忘记装 socksio）

**解决**：`requirements.txt` 写 `httpx[socks]`。

---

### 9. MockSession 不支持异步上下文管理器

**解决**：补全 `__aenter__` / `__aexit__` / `aclose`。

---

### 10. Token 计数失败（不影响功能）

**说明**：偶尔出现，不影响请求和回复，只是 usage token 数可能为 0。

---

### 11. Gemini 有时返回空回复

**现象**：结构完整但无文字内容。  
**解决**：自动换节点重试。

---

### 12. SillyTavern 报 "error decoding response body"

**原因**：① Replit 中间代理对 SSE 做了 gzip 压缩；② Gemini 错误 chunk 被静默丢弃导致流不完整。  
**解决**：① 响应头加 `Content-Encoding: identity`；② 错误 chunk 转 OpenAI 错误格式透传。

---

### 13. 新订阅链接拉取成功但节点没更新（明文格式问题）

**原因**：旧代码看到内容先做 base64 解码，把已经是明文的 `vmess://` 当成 base64 处理。  
**解决**：先检测 `vless://` / `vmess://` 头，有就直接按明文解析。

---

### 14. 回复内容被截断（parser parts 覆盖 bug）

**现象**：AI 回复说到一半突然停。  
**原因**：batchGraphql 同一 path_index 后到的 part 是**增量片段**，旧代码用 `dict[index] = part` 赋值会丢失之前的 part。  
**解决**：改成存 list，所有 parts 追加，最后按顺序拼接。  
**⚠️ 不要再"优化"成 dict 覆盖**——表面简洁，实际立刻退化成空回复或截断。

---

### 15. 所有 CF 节点测速延迟相同

**原因**：CF 任播网络，TCP ping 都路由到最近的同一个 CF 机房。  
**说明**：TCP ping 对 CF 节点只能检测"完全不可达"，不能区分性能。要看出节点差异需添加非 CF 节点。

---

### 16. 给 OpenAI-compat SSE 流加心跳会破坏 SillyTavern 解析

**现象**：每隔几秒发 `: ping\n\n`，SillyTavern 直接停止显示。  
**结论**：**不要给 SSE 流加心跳包装器**。客户端断开应该让客户端调超时，不是协议层塞心跳。

---

### 17. 强制 `fake_stream=True` 会让客户端"看起来卡死"

**原因**：假流式必须等 buffer 收齐才开始拆包，慢模型首字节可能等 30-60 秒。  
**结论**：**不要全局强制 `fake_stream`**。只在客户端选 `fs-` 前缀模型时启用。

---

### 18. "代理全失败兜底走直连" 看似稳健，实际违背设计目的

**问题**：兜底直连消耗 Replit 全局共享 IP 配额，赢了一局输了战略。  
**结论**：**代理全部失败应明确报错**，让用户去 `/proxy-manager` 加新节点。

---

### 19. UI 轮询日志刷屏（已修复）

**原因**：`/proxy-manager` 前端每 8 秒拉 status、每 15 秒拉 logs。  
**解决**：logger 加 `_NoiseFilter` 静默这三个路径的 access 日志。

---

### 20. OAI 真流式：上游 finish_reason 顺序错乱（已修复）

**原因**：上游 finish_reason 块有时排在内容块前面（thinking 模式尤其），客户端读到就以为结束。  
**解决**：`routes.py` 加 `deferred_finish` 缓存，等所有内容块发完再发 finish。**禁止**改回"按到达顺序原样转发"。

---

### 21. OAI 真流式：thinking 模式产生空 functionCall 噪音（已修复）

**原因**：上游 thinking 模式塞 `functionCall: {}` 作为内部标记。  
**解决**：`openai_compat.py::gemini_sse_chunk_to_openai` 过滤 `name` 为空的 functionCall。**禁止**移除这个过滤。

---

### 22. /admin 后台已加（独立于 /proxy-manager，不冲突）

**功能**：
- 密码三级 fallback：环境变量 `ADMIN_PASSWORD` > `config.json` > 硬编码 `1966197012`
- 7 天 cookie/Bearer 会话，重启失效
- Server / Keys / Proxy & Nodes 三个 tab

**踩坑**：
- 中间件 `excluded_paths` 必须用前缀匹配（`path == p OR path.startswith(p + "/")`），改回精确匹配会让 `/admin/api/login` 被拦回 401
- API_KEYS 文件支持两段（`name:key`）和三段（`name:key:description`）两种格式，**禁止**移除兼容逻辑
- 改密码后老 token 不吊销（in-memory session），要彻底踢人需重启

---

### 23. ⚠️ Replit 路由陷阱：/admin 后台不能用 /api/admin/* 前缀

**现象**：admin 登录页 404 / 401，请求没到达 vertex-proxy。

**原因**：本 monorepo 里 `api-server` 工件抢走了 `/api/*`，**任何 `/api/*` 路径都会被路由到 api-server，永远不到 vertex-proxy**。

**解决**：admin 接口必须挂 `/admin/api/*`，**禁止**改回 `/api/admin/*`。

---

### 24. 真流式（`_stream_realtime_inner`）的空回复检测

`fs-` 模型走真流式路径，先 buffer chunks → 用 `_extract_text_from_dict_chunks` 拼文本 → 空就重试。

**禁止**改成"边收边发"，会导致空回复时 SSE 头已发出，无法重试也无法报错。

---

### 25. OAI 兼容层错误处理（typed errors + error snapshots）

- `RateLimitError` / `AuthenticationError` / `VertexError` 继承 `VertexBaseError`，都有 `to_sse()` 转 OpenAI 错误格式
- 4 个响应路径全用 `save_error_snapshot` 包装
- 异常落盘到 `errors/` 目录

**禁止**用 `except Exception: yield ""` 静默吞噬。

---

### 26. 配额错误（429）有独立的重试上限

**问题历史**：以前所有错误共用 `max_retries=10`，配额耗尽时换 IP 重试 10 次（每次 5-7 秒），用户在 SillyTavern 等 1-2 分钟最后还是空回。

**现在**：`quota_max_retries = 2`（独立计数）。试 2 次就立刻返回明确错误，SillyTavern 弹真实错误信息。

**禁止**调到 5+ 或合并回 `max_retries`。换 IP 是兜住"恰好这个 IP 触发 Replit IP 级限流"的边角情况，不是硬刚 Google 配额（按项目算的，换 IP 救不回大部分）。

---

### 27. Token 计数器对 HttpxStreamingFakeResponse 的兼容

**问题**：`HttpxStreamingFakeResponse` 只有 `.text` / `.aread()` / `.aiter_lines()`，没有 `.json()`，会刷错误日志。

**修复**：try `.json()`，`AttributeError` 时 fall back 到 `await response.aread()` + `json.loads()`。**禁止**给 `HttpxStreamingFakeResponse` 加同步 `.json()`，body 可能没读完会阻塞或炸。

---

### 28. /admin Proxy & Nodes tab 复用 /proxy-manager 后端

第三个 tab 的所有接口直接调 `/proxy-manager/*` 现成 endpoint，没新增后端。

**禁止**单独建 `admin/api/proxy/*` 一套并行 API，会跟现有状态不同步。

---

## 🚧 改动禁区（写给改这个项目的人）

这些是血泪教训。改之前去查对应的"已知问题"条目。

| 不要做 | 为什么 | 见 |
|---|---|---|
| 把 `parser.py` 的 `parts_by_path` 从 list 改回 dict 覆盖 | 同一 path_index 后到的 part 是增量片段，改成赋值会立刻退化成空回复/截断 | #14 |
| 给 `/v1/chat/completions` SSE 流加心跳（`: ping\n\n`）| SillyTavern 等客户端遇到非 `data:` 行会异常 | #16 |
| 全局强制 `fake_stream=True` | 慢模型首字节等 30-60 秒，看起来卡死 | #17 |
| 加"代理全失败兜底走直连"的 ContextVar | 违背用代理保护 Replit IP 配额的初衷 | #18 |
| 把 `STRICT_PROXY` 默认值改成 `0`，或在严格模式下加任何"直连兜底"路径 | 即使 Vertex 调用走了代理，recaptcha 走直连也会让 Google 关联 Replit IP，整个池子被限流 | #29 |
| 把 `fetch_recaptcha_token` 改回"直连优先"策略 | 直连等于把 Replit 出口 IP 暴露给 Google，几次后整个共享 IP 段被全局限流 | #29 |
| 给 vertex 响应加"无 finishReason 就当截断重试" | 部分模型合法响应末包就是无 finishReason，会触发不必要重试浪费配额 | 历史 |
| 直接编辑 `artifact.toml` / `.replit` | 应该用平台工件管理工具改 | — |
| 把 admin 接口路径用 `/api/admin/*` | 会被同 monorepo 里的 api-server 抢走，永远 404 | #23 |
| 把中间件 `excluded_paths` 改回精确匹配（`path in 列表`）| 子路径如 `/admin/api/login` 会被拦回 401 | #22/#23 |
| 把真流式（`fs-` 模型）改成"边收边发不缓冲" | 空回复时 SSE 头已发出，无法重试也无法报错 | #24 |
| 把 `quota_max_retries` 调到 5+ 或合并回 `max_retries` | 配额是项目级，换 IP 救不回，硬重试只是让用户等 1-2 分钟最后空回 | #26 |
| 给 `HttpxStreamingFakeResponse` 加同步 `.json()` | 包装的是 httpx 流式 response，body 可能没读完，同步调用会阻塞或炸 | #27 |
| 给 admin 的 Proxy & Nodes tab 单独建 admin/api/proxy/* 后端 | 等于建两套并行 API，会跟现有 /proxy-manager 状态不同步 | #28 |

**遇到 bug 优先去查"已知问题"列表（共 28 条）。新增逻辑前先确认 README 没写过这条已被否决的方案。**

---

## 📦 依赖清单

| 包 | 用途 |
|---|---|
| `fastapi` | Web 框架 |
| `uvicorn` | ASGI 服务器 |
| `primp` | HTTP 客户端 + Chrome TLS 指纹伪装 |
| `httpx[socks]` | 异步 HTTP + SOCKS5（**必须带 `[socks]`**）|
| `beautifulsoup4` + `lxml` | 抓 Recaptcha Token |
| `pyyaml` | Clash 订阅 YAML 解析 |
| `pydantic` | 配置验证 |

---

## ❓ 常见问题

**Q：所有节点测速延迟一样？**  
A：订阅全是 CF 节点。CF 任播网络特性，详见 #15。要差异需加非 CF 节点。

**Q：服务刚启动第一次请求慢？**  
A：启动后约 4 秒完成订阅拉取和节点选择。这段时间内的请求会走直连。

**Q：为什么回复要等很久然后文字一下子全出来？**  
A：底层 batchGraphql 不支持真流式（详见 #7）。换 `gemini-2.5-flash` 等待会短很多。

**Q：能用多个 Google 账号轮换吗？**  
A：不能。匿名接口没有账号概念，所有请求共用同一个 Recaptcha Token 池。

**Q：xray 二进制从哪来？**  
A：启动时检测 `bin/xray`，不存在自动下载。git 里已打包，部署不用再下。

**Q：SillyTavern 报 "error sending request" / 超时？**  
A：99% 是 Vertex 配额耗尽。看部署日志找 `Resource has been exhausted`。换 `gemini-2.5-flash` 或等几小时。

**Q：admin 密码忘了？**  
A：`config/config.json` 里有，或者环境变量 `ADMIN_PASSWORD`，或者硬编码默认 `1966197012`。

**Q：换了订阅但节点没变？**  
A：要点 admin 或 /proxy-manager 里的"刷新节点列表"按钮。刷新后会显示每条订阅的拉取结果（✅/❌）。

**Q：Autoscale 部署冷启动太慢？**  
A：检查 git 里 `bin/xray` + `config/cached_nodes.json` + `config/active_node.json` 是否打包了。这三样齐全冷启动 3-5 秒，缺任何一个都会变成 30 秒以上。
