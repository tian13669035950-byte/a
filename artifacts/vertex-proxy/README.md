# Vertex AI Proxy 部署说明

这是一个将 Google Gemini（通过 Vertex AI 控制台）包装成 OpenAI 兼容接口的代理服务，支持 SillyTavern、Open-WebUI 等任何兼容 OpenAI API 的客户端直接接入。无需 Google Cloud 账号，无需 API Key，使用谷歌账号登录控制台即可免费使用 Gemini。

---

## 核心特性

- **OpenAI 兼容接口**：`/v1/chat/completions`、`/v1/models`，无缝替换 OpenAI API
- **免费 Gemini 访问**：通过 Google Cloud 控制台的匿名接口，不消耗 Gemini API 配额
- **SOCKS5 代理轮换**：内置 xray 代理管理，支持 Clash 订阅节点，自动轮换 IP 避免单 IP 配额耗尽
- **浏览器 TLS 指纹伪装**：使用 primp（Rust 静态链接 BoringSSL），通过 Google 的浏览器检测
- **假流式模式（fs- 前缀）**：非流式底层 + 模拟逐字输出，解决 batchGraphql 接口本身不支持真流式的问题
- **Web 管理界面**：`/proxy-manager`，可切换节点、查看日志、添加自定义节点
- **SillyTavern 兼容**：SSE 格式严格遵守 OpenAI 规范（内容块与 finish_reason 分开发送）

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
2. 拉取 Clash 订阅，获取节点列表（约 50 个节点）
3. 依次尝试节点，选出第一个可用的（默认是 CF 官方优选）
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
- `bin/xray` 是二进制文件，体积较大，可以加入 `.gitignore` 让其他人部署时自动下载

建议的 `.gitignore`：

```
bin/xray
config/api_keys.txt
config/custom_nodes.json
logs/
__pycache__/
*.pyc
```

---

## 支持的模型

### 普通模式（底层是 batchGraphql，结果一次性返回再流式转发）

| 模型名 | 说明 |
|--------|------|
| `gemini-2.5-pro` | 最强推理，较慢 |
| `gemini-2.5-flash` | 均衡（推荐） |
| `gemini-2.5-flash-lite` | 最快，较轻量 |
| `gemini-2.5-flash-image` | 支持图像输入 |
| `gemini-3-flash-preview` | 下一代预览版 |
| `gemini-3.1-flash-lite-preview` | 等其他预览版… |

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
| `temperature` | 随机性（0=固定，2=最发散）。调高会让回复更有创意/多样，调低更严谨/重复 | `temperature` |
| `max_tokens` | 最大输出 token 数 | `maxOutputTokens` |
| `top_p` | 核采样概率阈值 | `topP` |
| `top_k` | 每步候选词数量 | `topK` |
| `stop` | 停止词，字符串或列表 | `stopSequences` |
| `n` | 生成几个候选结果 | `candidateCount` |
| `response_format` | `{"type": "json_object"}` 时强制 JSON 输出 | `responseMimeType` |
| `tools` / `functions` | Function Calling | `tools.functionDeclarations` |
| `stream` | 是否流式输出 | — |

> **关于 `temperature`**：值越高 AI 回复越随机、有创意、"有趣"（funny），值越低越保守、严谨。一般聊天用 0.7~1.0，创意写作用 1.2~1.8，需要精确答案用 0.1~0.3。

---

## 代理管理

访问 `/proxy-manager` 打开管理界面，可以：

- 查看当前节点状态和 xray 是否在运行
- 切换订阅节点（当当前 IP 配额耗尽时换一个）
- 添加自定义节点（支持 Clash YAML 格式、xray JSON 格式）
- 查看最近的请求日志

**当出现 429 配额耗尽时**：在管理界面切换到下一个节点，换一个 IP 继续用。

---

## 配置文件说明

### `config/config.json`

```json
{
  "port_api": 2156,
  "max_retries": 8,
  "debug": false,
  "log_dir": "logs"
}
```

| 字段 | 说明 |
|------|------|
| `port_api` | API 服务端口（Replit 部署时被 PORT 环境变量覆盖，改这个没用） |
| `max_retries` | 单次请求最多重试次数（遇到 401/403/429 自动重试） |
| `debug` | 改成 `true` 会输出详细的请求/响应日志，方便排错 |

### `config/api_keys.txt`

每行一个有效的 API 密钥。客户端在 Authorization 头里带上一样的值才能访问。

### `config/models.json`

可用的模型名称列表。如果 Google 新出了模型，在这里加上模型名即可。

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

---

## 已知问题 & 踩坑记录

以下是开发过程中遇到的所有问题，供部署时参考：

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

**现象**：生产容器是无状态的，每次部署/重启都是全新环境，之前选好的节点不见了，所有请求直连 Google，很快被限速。  
**解决**：在 `main.py` 启动时加了自动初始化逻辑——检测到没有活跃节点时，自动拉取订阅并选第一个可用节点。

---

### 4. SOCKS5 代理对 Vertex AI 端点无效（旧 CF 节点）

**现象**：配置了 CF 系列代理节点，但 `cloudconsole-pa.clients6.google.com` 通过代理访问失败（ConnectError）。  
**原因**：部分 CF 优选节点只代理 Cloudflare 的 IP 段，Google 的 API 端点不在其中。  
**解决**：代码加了自动降级——代理失败时直连，两条路都不通才报错。实测 CF 官方优选节点可以到达该端点（返回 404 或 401 是正常的认证流程，不代表不可达）。

---

### 5. 单 Replit IP 配额很快耗尽（429 Resource Exhausted）

**现象**：连续发几次请求就报 `Resource has been exhausted`，隔一段时间又好了。  
**原因**：Google 按 IP 限速，Replit 的 IP 是共享的，很容易触发。  
**解决**：请求改为代理优先——走 xray SOCKS5 代理，用代理节点的 IP 发请求，每个节点有独立配额。可以在管理界面手动切换节点。

---

### 6. SillyTavern 流式输出内容被截断

**现象**：SillyTavern 里 AI 回复不完整，有时直接空白。  
**原因**：Gemini 的最后一个 SSE chunk 同时包含内容和 `finish_reason: stop`。SillyTavern 严格遵守 OpenAI 规范——看到 `finish_reason` 就停止读取，内容就丢了。  
**解决**：在 `openai_compat.py` 里，当一个 chunk 同时含内容和 finish_reason 时，拆成两个 chunk 发送：第一个只含内容（finish_reason=null），第二个只含 finish_reason（delta 为空）。

---

### 7. batchGraphql 接口本身不是真流式

**现象**：配置了流式模式（`stream: true`），但 AI 回复还是等很长时间才一次性出现。  
**原因**：服务底层调用的 `batchGraphql` 接口是"假流式"设计，Google 那边就是等全部内容生成完再一次性返回，无论客户端怎么设置。  
**解决**：引入 `fs-` 前缀模型——底层照样是非流式拿数据，但拿到数据后把内容拆成每 3 个字符一组逐包发送，客户端看起来是逐字打印效果。

---

### 8. httpx 不支持 SOCKS5（忘记装 socksio）

**现象**：代码里用 `httpx.AsyncClient(proxy="socks5://...")` 但代理没生效，或者直接报错。  
**原因**：httpx 需要额外安装 `socksio` 包才支持 SOCKS5 协议，否则直接忽略代理设置。  
**解决**：在 `requirements.txt` 里写 `httpx[socks]`（自动安装 socksio）。

---

### 9. MockSession 不支持异步上下文管理器

**现象**：服务启动报错：  
```
'MockSession' object does not support the asynchronous context manager protocol
```
**原因**：`vertex_client.py` 调用了 `async with session: ...` 或 `await session.close()`，但 MockSession 类没有实现 `__aenter__`/`__aexit__`/`aclose` 方法。  
**解决**：在 MockSession 上补全这些方法。

---

### 10. Token 计数失败（不影响功能）

**现象**：日志里偶尔出现：  
```
远程 Token 计数失败: 'MockSession' object does not support the asynchronous context manager protocol
```
**说明**：这是 Token 计数模块的问题，不影响 AI 请求正常响应。Usage 统计里的 token 数可能为 0，但实际请求和回复都是正常的。

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
              （primp 伪装 Chrome TLS 指纹）
                        ↓
              发送到 cloudconsole-pa.clients6.google.com
              优先走 xray SOCKS5 代理（换 IP）
              失败则直连降级
                        ↓
              gemini 响应 → openai 格式转换
              拆分最后一个 chunk（SillyTavern 兼容）
                        ↓
              SSE 流式返回给客户端
```

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
A：单个 IP 的配额用完了。去管理界面 `/proxy-manager` 切换到下一个节点换 IP 即可。

**Q：为什么服务刚启动时第一次请求比较慢？**  
A：启动后大约 4 秒才完成订阅拉取和节点选择。这段时间内的请求会走直连，速度正常，只是配额用的是 Replit IP。

**Q：能不能同时用多个 Google 账号？**  
A：目前没有多账号轮换，所有请求用同一个控制台接口（匿名 Recaptcha Token）。

**Q：为什么日志里有 token 计数失败？**  
A：不影响使用，忽略即可。

**Q：xray 二进制是从哪来的？**  
A：启动时自动检测 `bin/xray`，如果不存在会自动下载。
