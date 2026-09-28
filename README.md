# DeepSeek Chat 网页版逆向 API + OpenAI 兼容网关

把 [chat.deepseek.com](https://chat.deepseek.com) 网页版当 API 用的纯 Python 实现：
**零第三方依赖**（仅 Python 3.8+ 标准库），用你自己的网页登录账号收发消息、
上传图片、调用工具，并提供一个 **OpenAI 兼容网关**，让任何支持 OpenAI 的
工具/agent 框架（OpenAI SDK、LangChain、LlamaIndex、Cline、各类 coding agent）
直接接入。

```
你的程序 ──OpenAI 协议──▶ deepseek_openai.py(网关) ──逆向 REST/SSE──▶ chat.deepseek.com 网页后端
                          (会话管理/DSML 翻译/图片上传/POW 求解)
```

---

## 文件结构

| 文件 | 说明 |
|---|---|
| `deepseek_chat.py` | 核心库 + 命令行工具。包含：DeepSeekHashV1 哈希、POW 求解、SSE 流解析、JSON-Patch 状态还原、会话/历史/图片 API、交互式聊天 REPL |
| `deepseek_openai.py` | OpenAI 兼容网关（`/v1/chat/completions`、`/v1/models`、`/health`），基于 `deepseek_chat` 库 |
| `deepseek_pow.py` | 早期独立版 POW 模块（逻辑已并入 `deepseek_chat.py`，保留供参考） |

---

## 快速开始

### 1. 获取你的 token

浏览器登录 [chat.deepseek.com](https://chat.deepseek.com)，打开 DevTools 控制台执行：

```js
JSON.parse(localStorage.getItem("userToken")).value
```

得到一串字符串，就是你的 Bearer token。

### 2. 配置

二选一：

```bash
# 方式 A: 环境变量(推荐, Docker/CI 友好)
export DS_TOKEN="粘贴你的 token"
# 可选: WAF 拦截时带上浏览器 Cookie 里的 HWWAF 两项
export DS_COOKIE="HWWAFSESID=xxx; HWWAFSESTIME=123"

# 方式 B: 直接改 deepseek_chat.py 顶部 CONFIG 区的 TOKEN
```

### 3. 自检 + 试发

```bash
python3 deepseek_chat.py selftest     # 验证 POW 哈希实现(纯离线, 不花流量)
python3 deepseek_chat.py list         # 列出你最近的网页会话
python3 deepseek_chat.py read <会话ID>  # 打印某个会话的历史
python3 deepseek_chat.py send <会话ID> "你好"          # 发一条并等回复
python3 deepseek_chat.py new "你好"                 # 新建会话并发消息
python3 deepseek_chat.py chat -                      # 交互模式, 连续对话
```

交互模式（`chat`）里直接输入继续聊，`exit`/`q`/Ctrl-D 退出；
`/think` `/notthink` 切换思考模式，`/search` `/nosearch` 切换联网搜索。

> ⏱ **注意**：网页服务器把 POW 答案当**一次性**消费，所以每发一条消息都要现解一次
> POW（纯 Python 约 15~40 秒）。这是网页端机制，不是本项目的瓶颈——
> 想更快可以换 C/JS 实现哈希部分（接口已隔离在 `solve_pow` 里）。

---

## OpenAI 兼容网关

### 启动

```bash
python3 deepseek_openai.py                          # 默认 127.0.0.1:8000
python3 deepseek_openai.py --port 8787 --host 0.0.0.0
python3 deepseek_openai.py --default-session latest # 推荐(见下)
```

`--default-session` 三种取值：

| 取值 | 行为 |
|---|---|
| 不配置 | 请求不带 `ds_session_id` 时**每次新建**网页会话 |
| `latest` | 自动复用你**最近更新的网页会话**（agent 单轮发消息时上下文不丢，推荐） |
| 固定会话 ID | 永远复用该会话 |

### 接入你的程序

```bash
export OPENAI_BASE_URL=http://127.0.0.1:8000/v1
export OPENAI_API_KEY=任意值        # 网关默认不校验; 想校验就设 DS_API_KEY
```

然后 OpenAI SDK 直接用：

```python
from openai import OpenAI
client = OpenAI(base_url="http://127.0.0.1:8000/v1", api_key="x")
r = client.chat.completions.create(
    model="deepseek-chat",
    messages=[{"role": "user", "content": "你好"}],
    timeout=180,          # POW 求解耗时, 超时给足
)
print(r.choices[0].message.content)
```

curl 示例：

```bash
curl http://127.0.0.1:8000/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{"model":"deepseek-chat","messages":[{"role":"user","content":"你好"}]}'

# 流式
curl -N http://127.0.0.1:8000/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{"model":"deepseek-chat","stream":true,"messages":[{"role":"user","content":"讲个笑话"}]}'
```

### 模型名

| 模型名 | 对应网页 |
|---|---|
| `deepseek-chat`（默认） | 普通模式 |
| `deepseek-reasoner` | 思考模式（回复带 `reasoning_content` 字段） |

### 额外请求字段

放进请求 body（Python SDK 用 `extra_body={...}`）：

| 字段 | 说明 |
|---|---|
| `ds_session_id` | 指定在哪个网页会话里继续聊。**响应 JSON 和响应头 `X-DS-Session-Id` 都会返回会话 ID**，下一轮带上即可保持上下文；网页端也能同步看到这条消息 |
| `thinking` | `true`/`false`，等价于用 `deepseek-reasoner` |
| `search` | `true` 开启联网搜索 |

### 工具调用（function calling）

网页模型调工具时输出的是它私有的 **DSML 标记**：

```
<｜｜DSML｜｜ calls>
<｜｜DSML｜｜ invoke name="bash">
<｜｜DSML｜｜ parameter name="command" string="true">ls -la</｜｜DSML｜｜ parameter>
</｜｜DSML｜｜ invoke>
</｜｜DSML｜｜ calls>
```

（定界符是**全角竖线** U+FF5C，不是普通 `|`）

网关自动做**双向翻译**：

- **出站**：模型输出里的 DSML 块 → 标准 OpenAI `tool_calls`，
  `finish_reason` 置为 `tool_calls`，正文里不再残留标记；
- **入站**：你把工具结果作为 `role:"tool"` 消息回传，网关自动拼成下一轮提示词。
  结果文本里若夹带 DSML 块，会先翻译成可读的 `[工具调用 xxx]` 描述。

并且对模型输出的**畸形 DSML** 做了容错（真实观测到过这些坏情况）：
`invoke` 开头标签整个缺失、闭合标签丢 `</` 前缀、同一参数重复上百次、
流在块中间被 token 上限截断——都能把完整的 `parameter` 抢救成 tool call；
`invoke` 没写 `name` 时按参数特征猜（`command`→bash、`file_path`+`old_string`→edit、
`file_path`+`content`→write、`file_path`→read）。

**注意**：工具定义放在 system prompt 里（agent 框架通常自动做）；模型**不保证**
每次都走工具，它可能选择直接文字回答——这是网页模型的采样行为，按标准逻辑
处理 `finish_reason` 即可。

### 图片（多模态）

最后一条 user 消息用标准 OpenAI 多模态格式，网关自动上传并等解析：

```json
{"role": "user", "content": [
  {"type": "text", "text": "这是什么?"},
  {"type": "image_url", "image_url": {"url": "data:image/png;base64,..."}},
  {"type": "image_url", "image_url": {"url": "https://example.com/x.jpg"}}
]}
```

`url` 支持 `data:` 内联 base64 / `http(s)` 链接 / 本地文件路径。
图片要先上传+解析（约 10~30 秒）才能进模型，等待发生在首个 chunk 之前。

---

## 逆向要点（给想深挖的人）

- **认证**：`Authorization: Bearer <userToken>`，外加浏览器 UA/Origin/Referer。
- **POW（工作量证明）**：发消息前先 `POST /api/v0/chat/create_pow_challenge`
  拿题目，解 `DeepSeekHashV1(salt_expireAt_nonce) == challenge`
  （nonce 在 `[0, difficulty)` 内），答案 base64(JSON) 放 `X-DS-PoW-Response` 头。
  **答案一次性**：服务器消费后失效，每条消息都要重新求解。
- **DeepSeekHashV1**：Keccak-f[1600] 变体——23 轮（标准 24 轮）、自定义轮常量表、
  rate 136B / capacity 256b、lane 字节序约定与标准不同。项目里附了离线自检向量。
- **完成接口**：`POST /api/v0/chat/completion` 走 SSE。事件流 = 初始完整消息对象 +
  JSON-Patch 增量（`p/o/v`，BATCH 子补丁相对路径）+ 裸 `{"v":"str"}` 文本增量
  （追加到最后一个 fragment）+ `event: title` / `event: close`。
- **消息模型**：消息 ID 是**整数**（`message_id`/`parent_id` 构成链表）；
  会话列表 `fetch_page` 的 `count` 最小为 5；删除会话 body 是
  `{"chat_session_ids": [...]}`（数组）。
- **WAF**：请求太快会撞腾讯 WAF 429；脚本内置 1 秒间隔，仍被拦就配 `DS_COOKIE`
  带上 `HWWAFSESID`/`HWWAFSESTIME`。
- **图片**：multipart 上传 `/api/v0/file/upload_file`（也要单独解一次 POW），
  轮询 `fetch_files` 到 `SUCCESS` 后，把文件 ID 放进 completion 的 `ref_file_ids`。

---

## Docker

```bash
docker build -t deepseek-chat-api .
docker run -d --name ds-gateway -p 8000:8000 \
  -e DS_TOKEN="你的token" \
  --restart unless-stopped deepseek-chat-api
# 之后 http://<主机>:8000/v1 就是 OpenAI 兼容端点
```

（Dockerfile 默认以 `latest` 会话复用模式启动；想改参数覆盖 CMD 即可。）

---

## 已知限制

1. **POW 耗时**：纯 Python 求解约 15~40 秒/条消息，agent 客户端超时建议 ≥2 分钟。
2. **网页逆向，非官方 API**：字段/协议可能随网页改版变化；token 长期不用可能失效。
3. **单账号限流**：网页端有并发/频率限制，高并发场景需要排队或换实现。
4. **usage 字段**：`completion_tokens` 取自最终消息的累计用量，`prompt_tokens` 目前报 0。
5. **模型行为**：是否输出工具调用、输出是否规范，取决于网页模型的采样（见上文容错说明）。

## 免责声明

个人逆向项目，仅供学习交流；请控制并发、勿商用，以免打扰 DeepSeek 服务端。
