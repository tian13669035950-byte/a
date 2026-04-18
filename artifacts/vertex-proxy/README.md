# Vertex AI Proxy 部署说明

这是一个将 Google Gemini（通过 Vertex AI 控制台）包装成 OpenAI 兼容接口的代理服务，支持 SillyTavern、Open-WebUI 等任何兼容 OpenAI API 的客户端直接接入。无需 Google Cloud 账号，无需 API Key，使用谷歌账号登录控制台即可免费使用 Gemini。

---

## 核心特性

- **OpenAI 兼容接口**：`/v1/chat/completions`、`/v1/models`，无缝替换 OpenAI API
- **免费 Gemini 访问**：通过 Google Cloud 控制台的匿名接口，不消耗 Gemini API 配额
- **SOCKS5 代理轮换**：内置 xray 代理管理，支持多条订阅链接、节点自动轮换
- **浏览器 TLS 指纹伪装**：使用 primp（Rust 静态链接 BoringSSL），通过 Google 的浏览器检测
- **假流式模式（fs- 前缀）**：非流式底层 + 模拟逐字输出，解决 batchGraphql 不支持真流式的问题
- **空回复自动重试**：检测到 Gemini 返回空内容时自动换节点重试
- **节点耗尽自动重拉**：全部节点轮换一圈后自动重新拉取订阅获取新 IP 列表
- **多订阅链接管理**：管理界面支持添加/删除多条订阅链接，刷新时合并所有链接的节点
- **一键测速排序**：并发 TCP ping 所有节点，按延迟从低到高排序，自动选最优
- **出口 IP 检查**：对比直连 IP 和代理 IP，判断节点是否真的换了出口
- **Web 管理界面**：`/proxy-manager`，集中管理订阅、节点、测速、日志
- **SillyTavern 兼容**：SSE 格式严格遵守 OpenAI 规范，禁用中间层压缩，防止解码错误

---

## 在新 Replit 账号部署

### 第一步：导入代码

1. 登录 [replit.com](https://replit.com)，点击右上角 **Create Repl**
2. 选择 **Import from GitHub**，填入仓库地址
3. 等待导入完成

### 第二步：启动服务

在 Replit 控制台找到 **Vertex AI Proxy** 工作流，点击运行。

服务会自动：
1. 检测是否有保存的代理节点（生产环境每次部署是全新容器，所以首次启动没有）
2. 拉取所有已保存的订阅链接，合并节点列表
3. 依次尝试节点，选出第一个可用的
4. 启动 xray 进程监听 `127.0.0.1:1080`

### 第三步：配置 API 密钥

编辑 `config/api_keys.txt`，每行一个密钥，默认已有一个：

```
sk-123456
```

可以改成任意字符串，只要客户端填一样的就行。

### 第四步：连接客户端

| 设置项 | 值 |
|--------|-----|
| API Base URL | `https://你的replit域名.replit.app` |
| API Key | `sk-123456`（或你改的值）|
| 模型名 | 见下方模型列表 |

---

## 管理界面功能说明

访问 `/proxy-manager` 打开管理界面，功能如下：

### 订阅链接管理

- 支持添加多条订阅链接（Clash YAML、base64 vless/vmess 列表、明文 vless/vmess 列表均支持）
- 点"＋ 添加"保存链接，点"🔄 刷新节点列表"从所有链接拉取并合并节点
- 刷新后每条链接会显示拉取结果（✅ 成功几个节点 / ❌ 失败原因），不再静默失败
- 链接保存在 `config/sub_urls.json`，服务重启后自动加载

### 节点可用性检测（额度扫描）

- 点"🔍 检测可用节点"，依次启动每个节点，**用真实的 Gemini 模型试一句话**（轮流用 `gemini-2.5-pro` 和 `gemini-3.1-pro-preview` 两个模型），看能不能拿到内容
- 结果显示在节点列表"可用"列：✅ 模型正常返回内容 / 🚫 429 或空响应（额度耗尽） / 💀 超时/无法连接
- 扫描结束后出现"🗑 删除无效节点"按钮，一键清除所有不可用节点
- 每次最多检测前 30 个节点（可配合测速排序先把最快的排到前面）

> **注意**：检测时会逐个切换节点，期间正常 AI 请求可能受影响。建议在空闲时检测。  
> **关于额度**：✅ 表示节点能连到 Google，不代表 Gemini 配额一定充足。🚫 才是明确的 429（额度耗尽）信号。

### 一键测速排序

- 并发 TCP ping 所有节点的 server:port，几秒内测完全部节点
- 测完后节点列表按延迟从低到高重新排序
- 延迟颜色：绿色 < 100ms，黄色 100~300ms，红色 > 300ms
- 最优节点标 👑，点"🏆 选最优节点"一键连接

> **注意（CF 节点特有现象）**：如果你订阅里全是 CF 优选节点，测速结果可能显示所有节点延迟一样（例如都是 29ms）。这是正常的——CF 使用任播网络，TCP ping 打过去都连到最近的同一个 CF 机房，所以延迟相同，并不是代码有问题。TCP ping 在这里的价值是检测哪些节点完全不可达（超时），而不是区分 CF 节点之间的性能差异。

### 出口 IP 检查

- 点"🌐 查看出口 IP"，同时测直连 IP 和代理 IP
- 如果两个 IP 相同，说明代理节点实际上没有改变 Google 看到的 IP
- 如果不同，说明代理确实在换出口

### 自定义节点

- 支持粘贴 vless:// 或 vmess:// 链接
- 支持粘贴完整 xray JSON 配置（含 TLS 分片 fragment）
- 自定义节点保存在 `config/custom_nodes.json`，不受订阅刷新影响

---

## 上传到 GitHub

```bash
# 在 Replit Shell 里执行
git init
git remote add origin https://github.com/你的用户名/你的仓库名.git
git add .
git commit -m "initial commit"
git push -u origin main
```

注意事项：
- `config/api_keys.txt` 里的密钥会被上传，建议上传前改成示例值（如 `sk-yourkey`）或在 `.gitignore` 里排除
- `config/custom_nodes.json` 里有你的节点配置信息，同样建议排除或清空再上传
- `config/sub_urls.json` 里有你的订阅链接，如果包含私人 token 建议排除
- `bin/xray` 是二进制文件，体积较大，可以加入 `.gitignore` 让其他人部署时自动下载

建议的 `.gitignore`：

```
bin/xray
config/api_keys.txt
config/custom_nodes.json
config/sub_urls.json
logs/
__pycache__/
*.pyc
```

---

## 支持的模型

所有模型均已通过实测验证可用。

### 普通模式（底层是 batchGraphql，结果一次性返回再流式转发）

| 模型名 | 速度 | 说明 |
|--------|------|------|
| `gemini-2.5-flash` | 快 | 均衡，**日常推荐** |
| `gemini-2.5-flash-lite` | 最快 | 轻量，适合简单任务 |
| `gemini-2.5-flash-image` | 快 | 支持图片输入 |
| `gemini-2.5-pro` | 中 | 最强推理 |
| `gemini-2.5-flash-lite-preview-09-2025` | 快 | 配额较小，容易 429 |
| `gemini-3-flash-preview` | **慢（30~60秒）** | 模型本身响应慢，不是出错 |
| `gemini-3-pro-image-preview` | 中 | 支持图片 |
| `gemini-3.1-flash-lite-preview` | 快 | |
| `gemini-3.1-flash-image-preview` | 快 | 支持图片 |
| `gemini-3.1-pro-preview` | 中 | |

### 假流式模式（fs- 前缀，逐字符分包发送，适合对流式体验有要求的场景）

所有模型都有对应的 `fs-` 版本，例如 `fs-gemini-2.5-flash`。

两者的区别：
- 普通模式：等 Gemini 返回全部内容后一次转发（客户端看起来有延迟，然后文字一下子全出来）
- 假流式：等 Gemini 返回全部内容后，把文字拆成每 3 个字符一组，逐包发送，模拟逐字打印效果

---

## 支持的请求参数

兼容 OpenAI Chat Completions 格式，以下参数会被正确转换传给 Gemini：

| 参数名 | 说明 | Gemini 对应 |
|--------|------|-------------|
| `temperature` | 随机性（0=固定，2=最发散） | `temperature` |
| `max_tokens` | 最大输出 token 数 | `maxOutputTokens` |
| `top_p` | 核采样概率阈值 | `topP` |
| `top_k` | 每步候选词数量 | `topK` |
| `stop` | 停止词，字符串或列表 | `stopSequences` |
| `n` | 生成几个候选结果 | `candidateCount` |
| `response_format` | `{"type": "json_object"}` 时强制 JSON 输出 | `responseMimeType` |
| `tools` / `functions` | Function Calling | `tools.functionDeclarations` |
| `stream` | 是否流式输出 | — |

---

## 请求路径说明

服务监听在端口 8000，对外暴露以下路径：

| 路径 | 说明 |
|------|------|
| `GET /` | 重定向到 `/proxy-manager` |
| `GET /health` | 健康检查（返回 200 表示服务正常） |
| `POST /v1/chat/completions` | OpenAI 兼容聊天接口 |
| `GET /v1/models` | 可用模型列表 |
| `GET /v1beta/models` | Gemini 原生格式模型列表 |
| `POST /v1beta/models/{model}:generateContent` | Gemini 原生格式请求 |
| `GET /proxy-manager` | 代理管理界面 |
| `GET /proxy-manager/status` | 当前代理状态 JSON |
| `GET /proxy-manager/sub-urls` | 订阅链接列表 |
| `POST /proxy-manager/sub-urls` | 添加订阅链接 |
| `DELETE /proxy-manager/sub-urls/{index}` | 删除订阅链接 |
| `GET /proxy-manager/list` | 节点列表（加 `?refresh=true` 重新拉取） |
| `POST /proxy-manager/bench` | 启动并发测速 |
| `GET /proxy-manager/bench-status` | 测速进度 |
| `GET /proxy-manager/ip-check` | 直连 IP vs 代理 IP 对比 |

---

## 已知问题 & 踩坑记录

### 1. 生产环境 404（没有注册 artifact.toml）

**现象**：本地开发正常，发布后访问直接 404。  
**原因**：`artifacts/vertex-proxy/.replit-artifact/artifact.toml` 不存在，Replit 部署系统找不到这个服务，不会启动它。  
**解决**：确保该文件存在且内容正确（本仓库已修复）。

---

### 2. 生产环境 TLS 错误（curl_cffi 不兼容）

**现象**：开发环境正常，生产容器报错：  
```
TLS connect error: error:00000000:invalid library (0):OPENSSL_internal:invalid library (0)
```
**原因**：`curl_cffi` 在 NixOS 开发环境编译，依赖系统 OpenSSL；生产容器是不同的 Linux 系统，库不兼容。  
**解决**：换用 `primp`（Rust 静态链接 BoringSSL，不依赖系统 OpenSSL，任何 Linux 下都能跑）。

---

### 3. 生产环境每次重启没有代理节点

**现象**：生产容器是无状态的，每次部署/重启都是全新环境，之前选好的节点不见了。  
**解决**：启动时自动初始化——从 `config/sub_urls.json` 读取订阅链接，拉取节点列表，选第一个可用节点。

---

### 4. SOCKS5 代理对 Vertex AI 端点无效（旧 CF 节点）

**现象**：配置了 CF 系列代理节点，但 `cloudconsole-pa.clients6.google.com` 通过代理访问失败。  
**原因**：部分 CF 优选节点只代理 Cloudflare 的 IP 段，Google 的 API 端点不在其中。  
**解决**：代理失败时自动降级直连，两条路都不通才报错。

---

### 5. 单 IP 配额很快耗尽（429 Resource Exhausted）

**现象**：连续发几次请求就报 `Resource has been exhausted`。  
**原因**：Google 按 IP 限速，Replit 的 IP 是共享的，很容易触发。  
**解决**：请求改为代理优先，遇到 429 自动切换下一个节点重试。

> **关于 CF 节点的额外说明**：即使换了不同的 CF 节点，出口 IP 可能仍在 Cloudflare 的同一个 ASN 下，Google 可能会对整段 CF IP 集体限速。要真正换出口，需要添加非 CF 的节点（如你自己的 VPS、Shadowsocks 节点等）。

---

### 6. SillyTavern 流式输出内容被截断

**现象**：SillyTavern 里 AI 回复不完整，有时直接空白。  
**原因**：Gemini 的最后一个 SSE chunk 同时包含内容和 `finish_reason: stop`。SillyTavern 看到 `finish_reason` 就停止读取，内容就丢了。  
**解决**：把最后一个 chunk 拆成两个发送——第一个只含内容，第二个只含 finish_reason。

---

### 7. batchGraphql 接口本身不是真流式

**现象**：配置了 `stream: true`，但 AI 回复还是等很长时间才一次性出现。  
**原因**：服务底层调用的 `batchGraphql` 接口等全部内容生成完再一次性返回。  
**解决**：引入 `fs-` 前缀模型——拿到数据后拆成每 3 个字符一组逐包发送，客户端看起来是逐字打印效果。

---

### 8. httpx 不支持 SOCKS5（忘记装 socksio）

**现象**：用 `httpx.AsyncClient(proxy="socks5://...")` 但代理没生效。  
**解决**：在 `requirements.txt` 里写 `httpx[socks]`（自动安装 socksio）。

---

### 9. MockSession 不支持异步上下文管理器

**现象**：`'MockSession' object does not support the asynchronous context manager protocol`  
**解决**：在 MockSession 上补全 `__aenter__`/`__aexit__`/`aclose` 方法。

---

### 10. Token 计数失败（不影响功能）

**现象**：日志里偶尔出现 MockSession 相关错误。  
**说明**：Token 计数模块的问题，不影响 AI 请求正常响应。Usage 里 token 数可能为 0，但实际请求和回复都正常。

---

### 11. Gemini 有时返回空回复

**现象**：AI 回复是空白消息，没有报错。  
**原因**：Gemini 偶尔返回结构完整但没有文字内容的响应。  
**解决**：检测到空回复后自动换节点重试，直到拿到有内容的回复。

---

### 12. SillyTavern 报 "error decoding response body"

**现象**：使用流式模式时 SillyTavern 弹出该错误。  
**原因**：① Replit 中间代理对 SSE 流做了 gzip 压缩；② Gemini 错误 chunk 被静默丢弃导致流不完整。  
**解决**：① 响应头加 `Content-Encoding: identity` 禁止中间层压缩；② 上游错误 chunk 转换成 OpenAI 错误格式透传。

---

### 13. 新订阅链接拉取成功但节点没更新（明文格式问题）

**现象**：添加了明文 vless/vmess 订阅链接（内容直接是 `vmess://...` 一行一行），刷新后节点数量没变化或报"解析出 0 个节点"。  
**原因**：旧代码看到内容先做 base64 解码，把已经是明文的 `vmess://` 链接当成 base64 字符串处理，解码后变成乱码，一个节点都认不出来。  
**解决**：现在先检测内容里有没有 `vless://` 或 `vmess://` 开头的行，有就直接按明文解析，不做 base64 解码。同时解析失败时会明确报错，显示内容预览，不再静默使用旧缓存。

---

### 14. 回复内容被截断（parser parts 覆盖 bug）

**现象**：AI 回复说到一半突然停了，比如"我是由谷歌训练的旅行。语言模型你可以，"然后什么都没了。  
**原因**：batchGraphql 响应的某个 streaming chunk（path index）可能携带多个 `parts`（比如内容分多块返回，或者同时有思考块+内容块）。旧代码用 `dict[path_index] = part` 赋值，后面的 part 会覆盖前面的，丢失了中间的内容。  
**解决**：改为 `dict[path_index]` 存列表，所有 parts 都追加进去，最后按顺序拼接，不再丢失任何块。  
**⚠️ 不要再"优化"成 dict 覆盖**：表面上看 `state['parts_by_path'][path_index] = part` 更简洁，注释里写"后到的是完整态"也很容易让人信，但实际上 batchGraphql 同一 path_index 后到的 part 是**增量片段**而不是完整覆盖。一旦改成赋值，立刻退化成空回复或截断输出。

---

### 16. 给 OpenAI-compat SSE 流加心跳会破坏 SillyTavern 解析

**现象**：为了防止"首字节超时"在 SSE 流里每隔几秒发一行 `: ping\n\n` 注释，结果 SillyTavern 直接停止显示输出。  
**原因**：SSE 规范虽然允许注释行，但部分挑剔的客户端对非 `data:` 开头的行处理有 bug。Gemini 的真流式间隔本来就够短，根本不需要心跳。  
**结论**：**不要给 `/v1/chat/completions` 流加心跳包装器**。如果客户端真的因为长等待断开，应该让客户端调超时，不是在协议层塞心跳。

---

### 17. 强制 `fake_stream=True` 会让客户端"看起来卡死"

**现象**：为了"统一体验"把所有流式请求强制走假流式（buffered + 拆字节模拟流），结果用户反馈"输出不动了"。  
**原因**：假流式必须等 buffer 收齐才开始拆包发送，对于慢模型（pro / 3.x preview）首字节延迟可能 30~60 秒，期间客户端完全收不到任何字节。真流式虽然底层 batchGraphql 也是 buffer 的，但 Gemini 自身分块返回会让首字节更早到达。  
**结论**：**不要全局强制 `fake_stream`**。这个开关只在客户端明确选了 `fs-` 前缀模型时才启用。

---

### 18. "代理全失败兜底走直连" 看似稳健，实际违背设计目的

**现象**：在 vertex_client 里加 `force_direct_var` ContextVar，所有代理节点都试过仍失败时，最后一次重试改走 Replit 直连。  
**问题**：这个项目用代理本来就是为了**保护 Replit IP 配额**——免费 Replit IP 是共享的，被 Google 标记一次后所有共用 IP 的人都受影响。"兜底直连"为了一次成功而消耗 Replit 全局配额，赢了一局输了战略。  
**结论**：**代理全部失败应该明确报错给客户端**，让用户去 `/proxy-manager` 刷新订阅或加新节点。不要悄悄降级直连。

---

### 19. UI 轮询日志刷屏（已修复）

**现象**：打开 `/proxy-manager` 网页后，日志窗口被 `/proxy-manager/status`、`/proxy-manager/logs`、`/proxy-manager/ip-check` 这三个轮询接口的访问行刷满，多开几个标签页更严重。  
**原因**：管理界面前端每 8 秒拉 status、每 15 秒拉 logs，用 uvicorn 默认 access 日志会无差别打印。  
**解决**：在 logger 里加了 `_NoiseFilter`，对这三个路径的访问日志静默处理（请求功能照常工作，只是不打日志）。

---

### 20. OAI 真流式：上游 finish_reason 顺序错乱（已修复）

**现象**：把 `/v1/chat/completions` 改成真流式后（按上游 result 块逐个 yield），SillyTavern 偶尔提前截断输出，只看到很短一段就结束了。  
**原因**：上游 batchGraphql 返回的 JSON 数组里，**finish_reason 块有时排在内容块前面**（thinking 模式下尤其明显）。客户端按顺序读到 `finish_reason: stop` 就以为结束了，后续内容被丢弃。  
**解决**：在 `routes.py` 的真流式生成器里加 `deferred_finish` 缓存——任何带 finish 的事件都先存起来，等所有内容块全发完，再把最后一个 finish 事件发出去。**禁止**改回"按到达顺序原样转发"。

---

### 21. OAI 真流式：thinking 模式产生空 functionCall 噪音（已修复）

**现象**：用 gemini-2.5-flash 真流式时，每条回复前会涌出 5~10 个 `tool_calls: [{"function": {"name": "", "arguments": "{}"}}]` 空块，酒馆显示一堆"调用了未命名工具"提示。  
**原因**：上游 thinking 模式会在 parts 里塞 `functionCall: {}`（无 name 无 args）作为内部标记。我们的旧 SSE 转换函数照单全收。原来的"假流式"路径把所有 chunk 合并后只发一次，所以这些噪音被合并掉看不见；切真流式后才暴露出来。  
**解决**：在 `openai_compat.py::gemini_sse_chunk_to_openai` 里过滤 `name` 为空的 functionCall。**禁止**移除这个过滤，否则空 tool_calls 会再次冒出来。

---

### 22. /admin 后台已加（独立于 /proxy-manager，不冲突）

**功能**：
- 自动密码：首次启动若 `config.json` 没有 `admin_password` 字段，自动生成 12 位随机密码并写入，**WARN 级别**打印到日志（Replit 这种环境唯一能看到密码的地方）
- 7 天 cookie/Bearer 会话，重启服务即失效
- 设置端：端口 / debug / max_retries / 改密码
- API 密钥三段式 CRUD（`name:key:description`）
- 同名添加自动覆盖；删除后立即热加载到 `api_key_manager`

**踩坑**：
- 中间件 `excluded_paths` 之前是 `path in 列表`（精确匹配），加 `/admin` 子路径会失败。已改成 `path == p OR path.startswith(p + "/")`。**禁止**改回精确匹配，否则 `/admin/api/login` 会被拦回 401。
- 顺带"修复"了 `/proxy-manager` 子路径之前必须用硬编码 sk-123456 才能访问的问题，现在 `/proxy-manager/*` 也走前缀豁免（**这是有意为之**，proxy-manager UI 行为不变）。
- API_KEYS 文件支持两种格式：旧 `name:key`（两段）+ 新 `name:key:description`（三段）。**禁止**移除 `parts[2] if len(parts)>=3` 的兼容逻辑。
- 改了密码后**老 token 不会被吊销**（in-memory session 不知道密码变了）。这是 known limitation，要彻底踢人需重启服务。

---

### 23. ⚠️ Replit 路由陷阱：/admin 后台不能用 /api/admin/* 前缀

**现象**：admin 面板登录页一直 404 / 401，浏览器看到请求 `/api/admin/login` 没到达 vertex-proxy。

**原因**：当前 monorepo 里有两个工件：
- `vertex-proxy` 接管 `/`（FastAPI）
- `api-server` 抢走 `/api`（独立工件）

任何 `/api/*` 路径都会被 Replit 路由器送到 api-server，**永远不会到 vertex-proxy**。

**解决**：admin 后台所有接口必须挂在 `/admin/api/*`（前缀属于 vertex-proxy 命名空间），不能用 `/api/admin/*`。

**禁止**改回 `/api/admin/*` 命名风格，会立刻死掉。中间件 `excluded_paths = ["/", "/health", "/proxy-manager", "/admin"]` 的 `/admin` 通过前缀匹配自动覆盖 `/admin/api/*`。

---

### 24. 真流式（`_stream_realtime_inner`）的空回复检测

`fs-` 模型走真流式路径，先把 chunks buffer 起来，用 `_extract_text_from_dict_chunks` 拼出文本。如果是空就触发节点切换重试，最多 `max_retries` 次。

**禁止**改成"边收边发"，会导致空回复时已经把 SSE 头/前几个 data 行发出去了，客户端拿到一个看似正常但内容空的响应，无法重试也无法报错。

---

### 25. OAI 兼容层错误处理（typed errors + error snapshots）

- `RateLimitError` / `AuthenticationError` / `VertexError` 继承 `VertexBaseError`，都有 `to_sse()` 把错误转成 OpenAI 错误格式（`{"error":{"message":..., "type":..., "code":...}}`）
- 4 个响应路径全部用 `save_error_snapshot` 包装：OAI 流式 / OAI 非流式 / Gemini 流式 / Gemini 非流式
- 上游异常会落盘到 `errors/` 目录（带请求/响应/堆栈），方便复盘

**禁止**用 `except Exception: yield ""` 这种静默吞噬，所有错误必须以 OpenAI 错误格式返回客户端。

---

### 26. 配额错误（429 Resource Exhausted）有独立的重试上限

**问题历史**：以前所有错误共用 `max_retries=10`，遇到 Vertex 项目级配额耗尽时，会换 IP 重试 10 次（每次 5-7 秒），用户在 SillyTavern 等 1-2 分钟最后还是空回。但是**配额是按 GCP 项目算的，不是按 IP**，换 IP 救不回。

**现在**：
```python
quota_max_retries = 2  # 配额错误专用上限
quota_attempts = 0     # 单独计数
```

试 2 次还失败就立刻返回明确错误（`Resource has been exhausted`），SillyTavern 会弹出真实错误信息，用户知道发生了什么、不用傻等。

**禁止**移除 `quota_attempts` 单独计数，或把这个值调高到 5+。换 IP 试 1-2 次是为了兜住"恰好这个 IP 触发了 Replit 的 IP 级限流"的边角情况，不是为了硬刚 Google 配额。

---

### 27. Token 计数器对 HttpxStreamingFakeResponse 的兼容

**问题**：`token_counter._count_tokens_with_session` 直接调 `response.json()`。`HttpxStreamingFakeResponse` 类只暴露了 `.text` / `.aread()` / `.aiter_lines()`，没有 `.json()` 方法，导致日志被刷：
```
❌ 远程 Token 计数失败: 'HttpxStreamingFakeResponse' object has no attribute 'json'
```

不影响主流程，但是噪音很大且会让真正的错误被淹没。

**修复**：try `.json()`；`AttributeError` 时 fall back 到 `await response.aread()` + `json.loads()`。**禁止**给 `HttpxStreamingFakeResponse` 加同步 `.json()` 方法 —— 它包装的是 httpx 流式 response，body 可能没读完，同步调用会阻塞或炸。

---

### 28. /admin 加了 Proxy & Nodes tab，复用 /proxy-manager 后端

admin 面板第三个标签页 "Proxy & Nodes"：
- Status：当前出口代理 / xray 状态 / Google 可达性 / 节点数
- Subscriptions：订阅链接 CRUD
- Nodes：列表 + 一键启用
- Manual proxy：直填 socks5/http 代理

**所有接口直接调 `/proxy-manager/*` 现成 endpoint**，没新增后端代码。这是有意为之：
- /admin tab 是常用操作的简化入口
- /proxy-manager 完整页（"Advanced →" 链接）保留所有进阶功能（测速、配额扫描、country detect、自定义节点）

**禁止**把 /admin tab 的逻辑改成调一套新的 admin/api/proxy/* 端点。等于建两套并行 API，难维护，且会跟现有 /proxy-manager 状态不同步。

---

### 15. 所有 CF 节点测速延迟相同

**现象**：点"一键测速排序"后，订阅里所有节点显示完全相同的延迟（如全部 29ms）。  
**原因**：测速用的是 TCP ping（连接 server:port 测握手时间）。Cloudflare 使用任播网络，无论打哪个 CF IP，TCP 连接都会被路由到距离 Replit 最近的同一个 CF 机房，所以测出来延迟相同。这是网络特性，不是代码 bug。  
**说明**：TCP ping 对 CF 节点的实际作用是检测哪些节点完全不可达（超时），而不是区分各节点性能。要获得有意义的节点间性能差异，需要添加来自不同运营商/地区的非 CF 节点。

---

## 请求流程

```
客户端（SillyTavern/Open-WebUI）
    ↓  POST /v1/chat/completions
    ↓  Authorization: Bearer sk-123456
                        ↓
              API Key 验证中间件
                        ↓
              openai → gemini 格式转换
                        ↓
              获取 Recaptcha Token
              （primp 伪装 Chrome TLS 指纹，直连优先）
                        ↓
              发送到 cloudconsole-pa.clients6.google.com
              优先走 xray SOCKS5 代理（换 IP）
              失败则直连降级
                        ↓
              检测响应：空回复 → 换节点重试
              有内容 → gemini 格式转换为 openai 格式
              拆分最后一个 chunk（SillyTavern 兼容）
              响应头加 Content-Encoding: identity（防压缩）
                        ↓
              SSE 流式返回给客户端
```

---

## 配置文件说明

### `config/config.json`

| 字段 | 说明 |
|------|------|
| `port_api` | API 服务端口（Replit 部署时被 PORT 环境变量覆盖） |
| `max_retries` | 单次请求最多重试次数（遇到 401/403/429/空回复自动重试） |
| `debug` | 改成 `true` 会输出详细的请求/响应日志 |

### `config/api_keys.txt`

每行一个有效的 API 密钥。客户端在 Authorization 头里带上一样的值才能访问。

### `config/sub_urls.json`

订阅链接列表，通过管理界面添加/删除，也可以直接编辑这个文件：

```json
[
  "https://example.com/sub?token=xxx",
  "https://raw.githubusercontent.com/xxx/sub.txt"
]
```

### `config/custom_nodes.json`

自定义节点列表，通过管理界面添加，支持 vless://、vmess:// 或完整 xray JSON。

### `config/models.json`

可用的模型名称列表。如果 Google 新出了模型，在这里加上模型名即可。

---

## 环境变量（部署时推荐）

在 Replit Secrets 里设置以下变量可以覆盖默认行为，**不要把生产值写进配置文件再 push 到 GitHub**。

| 变量名 | 作用 | 不设置时 |
|--------|------|---------|
| `API_KEY` | 客户端访问密钥（自动补 `sk-` 前缀） | 用 `config/api_keys.txt` 里的 `sk-123456`，并在启动日志里打警告 |
| `ADMIN_PASSWORD` | `/admin` 后台登录密码（优先级高于 `config.json`）| 首次启动随机生成 12 位密码，**WARN 级别**打印到日志 |
| `SESSION_SECRET` | admin session token 签名（不设也能跑，但重启即失效）| 自动生成临时值 |
| `SUB_URL` | 默认订阅链接（覆盖代码内置那条） | 用代码里硬编码的内置订阅 |
| `PORT` | API 监听端口（Replit 部署自动注入） | 走 `config/config.json` 的 `port_api` |

---

## 部署到 Replit Deployments

### 部署类型选择

**用 Reserved VM**，不要用 Autoscale。

| 维度 | Reserved VM ✅ | Autoscale ❌ |
|------|---------------|-------------|
| 状态文件持久化 | 跨重启保留（订阅、自定义节点、激活节点都在） | 每次冷启动都重置 |
| xray 子进程 | 一次启动长跑 | 每个新实例都要重启 xray，节点切换状态丢失 |
| xray 二进制 | 下载一次缓存到 `bin/xray` | 每个新实例都要重新下载（10MB+ 启动延时） |
| 长流式响应（60-120s）| 不会被切 | 可能触发请求超时 |
| 多 IP 轮换 | 单实例，IP 由 xray 节点决定（你想要的） | 自动横向扩，每个实例 IP 不同，反而扰乱 |

### 发布前必做的检查

部署是个**全新的容器**，不是当前 dev 环境的快照。需要确认：

#### 1. Secrets 同步到 Deployment

Replit 的 dev 环境 secrets 跟 deployment secrets 是**两套独立的池**。在 Deployments 页面单独添加：

| Secret | 必须？ | 备注 |
|--------|-------|------|
| `ADMIN_PASSWORD` | **强烈推荐** | 不设的话每次重启会随机生成新密码，只在日志里打印，生产环境很难看到 |
| `API_KEY` | **推荐** | 不设会用默认 `sk-123456`，谁都能调你的 API |
| `SESSION_SECRET` | 推荐 | 已有 dev 端，复制过去即可 |
| `SUB_URL` | 可选 | 如果不想依赖默认订阅 |

#### 2. 生产环境状态文件是空的

下面这些文件**不会**从 dev 带过去（`.gitignore` 里）：

- `config/cached_nodes.json` — 节点缓存
- `config/active_node.json` — 当前激活的节点
- `config/sub_urls.json` — 订阅链接
- `config/custom_nodes.json` — 自定义节点
- `config/api_keys.txt` — API 密钥（如果没用 `API_KEY` env）
- `bin/xray` — xray 二进制

→ **第一次部署后**，进 `/admin` 或 `/proxy-manager` 重新加一遍订阅链接，刷新一次节点列表。xray 会自动下载。整个过程 ~30 秒。

#### 3. 不需要任何 GCP / Google 凭据

本服务用的是 Vertex AI **匿名 console 接口**（Recaptcha Token 鉴权），无需：
- ❌ Google Cloud 项目
- ❌ Service Account JSON
- ❌ `GOOGLE_APPLICATION_CREDENTIALS`
- ❌ API Key 计费绑定

#### 4. 出站网络要能通到这些域名

Replit Deployments 默认放行所有出站，**通常不需要做任何配置**。但如果未来有限制，需要保证可达：

| 目标 | 用途 |
|------|------|
| `cloudconsole-pa.clients6.google.com` | Vertex AI 实际请求落点 |
| `console.cloud.google.com` | Recaptcha Token 抓取 |
| `www.google.com` | Recaptcha 子资源 |
| `github.com` / `releases.githubusercontent.com` | 首次启动下载 xray 二进制 |
| 你的订阅链接 host | 拉节点 |
| 你订阅里的节点 IP/域名 | xray socks5 出口 |

#### 5. 端口绑定

代码已经读 `PORT` 环境变量（Replit 注入）。**不要**在 `config.json` 里硬写 8080，否则 Replit 路由器找不到端口会显示 502。

### 部署后第一件事

1. 看 deployment logs 找这一行（首次启动）：  
   ```
   ✅ admin 自动密码：xxxxxxxxxxxx (请尽快通过环境变量 ADMIN_PASSWORD 覆盖)
   ```
   除非你已经设了 `ADMIN_PASSWORD`，否则记下这串密码。
2. 访问 `https://<your-app>.replit.app/admin` 用这个密码登录。
3. 进 "Proxy & Nodes" tab → 加订阅 → 刷新节点 → 启用一个节点。
4. 用 SillyTavern 测一下 `https://<your-app>.replit.app/v1/chat/completions`。

### 已知部署陷阱

- **首次冷启动 + 第一个请求** 容易 401/超时：xray 还没起来，Recaptcha Token 还没抓到。耐心等 5-10 秒重试一次就好。
- **Replit deployment 重启 = 节点缓存还在但 xray 进程没了**：会自动恢复（main.py 里有 `restore_active_node`）。
- **配额错误**（`Resource has been exhausted`）：不是 bug。匿名 Vertex 配额按项目算，跟你的部署机器数量无关，跟你换什么 IP 也无关。等几小时滚动重置，或换 `gemini-2.5-flash`（配额比 pro 宽松）。

---

## 启动行为补充

- **节点磁盘缓存**：上次成功使用的节点列表存在 `config/cached_nodes.json`，重启时优先读它（冷启动跳过订阅拉取，几乎瞬间就绪）。缓存为空才会去拉订阅。
- **多源订阅 fallback**：拉订阅时先按 `config/sub_urls.json` 顺序尝试，全部失败才回退到代码内置 `SUB_URL`，每条失败原因都进日志。
- **xray 并发锁**：`proxy_state` 里有 `_xray_lock`，防止主请求和节点扫描同时重启 xray 杀掉对方连接。
- **扫描期 rotate 节流**：管理界面在跑"检测可用节点"时，主请求遇到 429 不会再触发节点切换，避免抢 xray。

---

## 改动禁区（写给改这个项目的人）

这个项目反复被"看着合理"的改动搞坏过。下面这些是**血泪教训**，改之前请先读对应的"已知问题"条目：

| 不要做 | 为什么 | 见 |
|--------|-------|-----|
| 把 `parser.py` 的 `parts_by_path` 从 list 改回 dict 覆盖 | 同一 path_index 后到的 part 是增量片段，不是完整态。改成赋值会立刻退化成空回复/截断 | 第 14 条 |
| 给 `/v1/chat/completions` SSE 流加心跳（`: ping\n\n`）| SillyTavern 等客户端遇到非 `data:` 行会异常，输出停止 | 第 16 条 |
| 全局强制 `fake_stream=True` | 慢模型会让客户端首字节等 30~60 秒，看起来卡死 | 第 17 条 |
| 加"代理全失败兜底走直连"的 ContextVar | 违背项目用代理保护 Replit IP 的初衷；应该明确报错让用户换节点 | 第 18 条 |
| 给 vertex 响应加"无 finishReason 就当截断重试"的判定 | 部分模型/部分调用确实会出现末包没有 finishReason 的合法响应，会触发不必要的重试，浪费配额 | 历史 |
| 直接编辑 `artifact.toml` / `.replit` | 应该用平台提供的工件管理工具改 | — |
| 把 admin 接口路径用 `/api/admin/*` | 会被同 monorepo 里的 api-server 工件抢走，永远 404 | 第 23 条 |
| 把中间件 `excluded_paths` 改回精确匹配（`path in 列表`）| 子路径如 `/admin/api/login` 会被拦回 401，登录页直接死 | 第 22/23 条 |
| 把真流式（`fs-` 模型）改成"边收边发不缓冲" | 空回复时 SSE 头已发出，无法重试也无法报错 | 第 24 条 |
| 把 `quota_max_retries` 调到 5 以上、或合并回 `max_retries` | 配额是 GCP 项目级，换 IP 救不回，硬重试只是让用户在酒馆等 1-2 分钟最后还是空 | 第 26 条 |
| 给 `HttpxStreamingFakeResponse` 加同步 `.json()` | 包装的是 httpx 流式 response，body 可能没读完，同步调用会阻塞或炸 | 第 27 条 |
| 给 admin 的 Proxy & Nodes tab 单独建一套 admin/api/proxy/* 后端 | 等于建两套并行 API，会跟现有 /proxy-manager 状态不同步 | 第 28 条 |

**遇到 bug 优先去查"已知问题"列表（共 28 条）。如果要新增逻辑，请先确认 README 里没写过这条已经被否决的方案。**

---

## 依赖清单

| 包名 | 用途 |
|------|------|
| `fastapi` | Web 框架 |
| `uvicorn` | ASGI 服务器 |
| `primp` | HTTP 客户端 + Chrome TLS 指纹伪装（Rust 静态链接） |
| `httpx[socks]` | 异步 HTTP + SOCKS5 代理支持（真流式传输） |
| `beautifulsoup4` + `lxml` | 解析 HTML 抓取 Recaptcha Token |
| `pyyaml` | 解析 Clash 订阅的 YAML 格式 |
| `pydantic` | 配置验证 |

---

## 常见问题

**Q：为什么有时会报 429？**  
A：单个 IP 的配额用完了。服务会自动换节点重试。如果还是失败，去管理界面 `/proxy-manager` 手动切换节点，或点"一键测速排序"选最优节点。

**Q：为什么所有节点测速延迟一样？**  
A：你的订阅全是 CF 节点，见上方第 14 条已知问题。解决方法是添加非 CF 的节点。

**Q：为什么服务刚启动时第一次请求比较慢？**  
A：启动后大约 4 秒才完成订阅拉取和节点选择。这段时间内的请求会走直连。

**Q：为什么回复前要等一段时间，然后文字一下子全出来？**  
A：底层接口（batchGraphql）本身不支持真流式。如果想要逐字打印效果，换用 `fs-` 前缀的模型（如 `fs-gemini-2.5-flash`）。

**Q：能不能同时用多个 Google 账号？**  
A：目前没有多账号轮换，所有请求用同一个控制台接口（匿名 Recaptcha Token）。

**Q：xray 二进制是从哪来的？**  
A：启动时自动检测 `bin/xray`，如果不存在会自动下载。

**Q：SillyTavern 报 "error decoding response body" 怎么办？**  
A：已在服务端修复。如果还出现，检查连接的是不是最新发布的地址。

**Q：添加了新订阅链接但节点没变化？**  
A：添加链接后需要点"刷新节点列表"按钮才会重新拉取。刷新后会显示每条链接的拉取结果（✅/❌），如果某条链接有问题会直接显示原因。
