#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
DeepSeek Chat 网页账号的 OpenAI 兼容网关(纯 Python 标准库, 无第三方依赖)。

启动:
  python3 deepseek_openai.py                # 默认 127.0.0.1:8000
  python3 deepseek_openai.py --port 8787 --host 0.0.0.0
  python3 deepseek_openai.py --default-session latest
      # 请求不带 ds_session_id 时, 自动复用你最近更新的网页会话(而不是新建);
      # 也可以给一个固定会话 ID: --default-session <你的会话ID>

然后把任何支持 OpenAI 的工具指过来:
  OPENAI_BASE_URL=http://127.0.0.1:8000/v1  OPENAI_API_KEY=任意值
  (OpenAI SDK / LangChain / LlamaIndex / Cline / 各类 agent 框架均可)

接口:
  GET  /health                 健康检查
  GET  /v1/models              模型列表
  POST /v1/chat/completions    对话补全(支持 stream 流式)

模型名:
  deepseek-chat      普通模式(默认)
  deepseek-reasoner  思考模式

额外字段(放进请求 body, Python SDK 可用 extra_body={...}):
  ds_session_id   DeepSeek 网页会话 ID。带上它 = 在网页上同一会话里继续聊,
                  网页端能看到这条消息; 不带 = 用 --default-session 指定的会话,
                  没配置时才新建网页会话。
                  响应里的 ds_session_id / 响应头 X-DS-Session-Id 会返回会话 ID,
                  下一轮带上即可保持上下文。
  thinking        true/false, 等价于模型名 deepseek-reasoner
  search          true 开启联网搜索

工具调用(function calling):
  网页模型调工具时输出的是自家 DSML 标记(如 <｜｜DSML｜｜ invoke name="bash">…),
  网关会自动解析并翻译成标准 OpenAI tool_calls(finish_reason="tool_calls"),
  正文里不再残留标记。把工具结果作为 role:"tool" 消息回传即可, 网关会把
  结果拼成下一轮提示词发给模型。工具定义放 system prompt 里(agent 框架
  通常自动做), 新会话的首轮会把 system 合并进第一条消息。

图片(多模态):
  最后一条 user 消息用标准 OpenAI 多模态格式即可, 网关自动上传并等解析:
    {"role":"user","content":[
        {"type":"text","text":"这是什么?"},
        {"type":"image_url","image_url":{"url":"data:image/png;base64,..."}},
        {"type":"image_url","image_url":{"url":"https://.../x.jpg"}}]}
  image_url.url 支持: data: 内联 base64 / http(s) 链接 / 本地文件路径。
  注意: 图片要先上传+解析(约 10~30 秒)才能进模型, 这段等待发生在首个 chunk 之前。

说明:
  - 每发一条消息网关都要现解一次 POW(约 20 秒), 这是网页服务器的一次性答案规则;
    流式模式下 POW 解完前连接会先挂着。
  - 上下文由 DeepSeek 服务器保存, 不需要每轮重发完整历史; 但请求里的 messages
    仍按 OpenAI 规范解析(取最后一条 user 消息作为要发送的内容)。
  - 全新会话且请求带了多条历史时, 历史会被拼成一段记录附在提示词里。
  - 账号凭证(网页登录 token)在 deepseek_chat.py 顶部的 TOKEN。
"""

import argparse
import base64
import json
import mimetypes
import os
import re
import sys
import threading
import time
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import deepseek_chat as ds

MODEL_CHAT = "deepseek-chat"
MODEL_REASONER = "deepseek-reasoner"

# 会话 -> 最后一条响应消息 id(下一条消息的 parent)
_session_parents = {}
_lock = threading.Lock()

# 默认会话: 请求不带 ds_session_id 时复用它, 而不是新建。
# None = 每次新建; "latest" = 每次取列表里最近更新的会话; 其余 = 固定会话 ID。
DEFAULT_SESSION = None


def _pick_default_session():
    if not DEFAULT_SESSION:
        return None
    if DEFAULT_SESSION != "latest":
        return DEFAULT_SESSION
    try:
        sessions = ds.list_sessions(10)  # 按更新时间倒序, 第一个 = 最近用过的
        if sessions:
            return sessions[0]["id"]
    except Exception as e:
        print(f"  [default-session] 获取最新会话失败: {e}", file=sys.stderr)
    return None


def _text_of(content):
    """把 OpenAI 的 content(字符串或分段列表)拍平成文本。"""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for p in content:
            if isinstance(p, str):
                parts.append(p)
            elif isinstance(p, dict) and p.get("type") == "text":
                parts.append(p.get("text", ""))
        return "\n".join(parts)
    return str(content)


def _extract_image_urls(msg):
    """从一条 OpenAI 消息里取出 image_url 部分(标准格式 image_url.url)。"""
    content = msg.get("content") if isinstance(msg, dict) else None
    if not isinstance(content, list):
        return []
    urls = []
    for p in content:
        if not isinstance(p, dict):
            continue
        if p.get("type") in ("image_url", "image"):
            iu = p.get("image_url", p.get("image"))
            if isinstance(iu, dict):
                url = iu.get("url")
            else:
                url = iu
            if url:
                urls.append(url)
    return urls


def _fetch_image(url):
    """
    把 image_url 解析成 (bytes, content_type, filename)。
    支持: data: 内联 base64 / http(s) 链接 / 本地文件路径。
    """
    if url.startswith("data:"):
        head, _, b64 = url.partition(",")
        mime = "image/png"
        m = re.match(r"data:([^;,\s]*)", head)
        if m and m.group(1):
            mime = m.group(1)
        data = base64.b64decode(b64)
        ext = mimetypes.guess_extension(mime) or ".png"
        return data, mime, "image" + ext
    if url.startswith(("http://", "https://")):
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=60) as r:
            data = r.read()
            mime = r.headers.get("Content-Type") or "image/png"
        name = os.path.basename(url.split("?")[0]) or "image.png"
        return data, mime, name
    # 本地文件路径
    if not os.path.exists(url):
        raise ValueError(f"图片文件不存在: {url}")
    with open(url, "rb") as f:
        data = f.read()
    mime = mimetypes.guess_type(url)[0] or "application/octet-stream"
    return data, mime, os.path.basename(url)


def _upload_images(image_urls):
    """上传若干图片并等解析完成, 返回 ref_file_ids。"""
    ids = []
    for i, url in enumerate(image_urls):
        try:
            data, mime, name = _fetch_image(url)
        except Exception as e:
            raise ValueError(f"第{i + 1}张图片读取失败: {e}")
        print(f"  [图片] 上传 {name} ({len(data)} bytes) …", file=sys.stderr, flush=True)
        fobj = ds.upload_file(data, name, content_type=mime)
        fid = fobj.get("id")
        if not fid:
            raise RuntimeError(f"图片上传未返回 id: {fobj}")
        print(f"  [图片] {fid} 等待解析…", file=sys.stderr, flush=True)
        ds.wait_file_ready(fid)
        print(f"  [图片] 解析完成 ✓", file=sys.stderr, flush=True)
        ids.append(fid)
    return ids


# ---------- DSML(网页版工具调用标记) -> OpenAI tool_calls ----------
# 网页模型调工具时输出的原生格式(定界符是全角竖线 U+FF5C):
#   <｜｜DSML｜｜ calls>
#   <｜｜DSML｜｜ invoke name="bash">
#   <｜｜DSML｜｜ parameter name="command" string="true">ls -la</｜｜DSML｜｜ parameter>
#   </｜｜DSML｜｜ invoke>
#   </｜｜DSML｜｜ calls>
_DSML_SEP = "\uFF5C\uFF5C"
_DSML_CALLS_START = "<" + _DSML_SEP + "DSML" + _DSML_SEP + " calls>"
_DSML_CALLS_END = "</" + _DSML_SEP + "DSML" + _DSML_SEP + " calls>"
# invoke 的 name 属性可能整个缺失(模型偶尔输出畸形: 直接从 calls 跳到 parameter),
# 所以 name 设为可选
_RE_INVOKE = re.compile(
    "<" + _DSML_SEP + "DSML" + _DSML_SEP + r'\s*invoke(?:\s+name="([^"]*)")?\s*>(.*?)</'
    + _DSML_SEP + "DSML" + _DSML_SEP + r'\s*invoke\s*>', re.S)
_RE_PARAM = re.compile(
    "<" + _DSML_SEP + "DSML" + _DSML_SEP +
    r'\s*parameter\s+name="([^"]*)"(?:\s+string="([^"]*)")?\s*>(.*?)</'
    + _DSML_SEP + "DSML" + _DSML_SEP + r'\s*parameter\s*>', re.S)


def _extract_params(text):
    """从一段文本里提取所有完整的 parameter 标签, 返回 {name: value}。"""
    args = {}
    for pm in _RE_PARAM.finditer(text):
        pname, ptype, pval = pm.group(1), pm.group(2), pm.group(3)
        if ptype == "false":  # 非字符串: 尝试 JSON 解析
            try:
                args[pname] = json.loads(pval)
            except Exception:
                args[pname] = pval
        else:
            args[pname] = pval
    return args


def _guess_tool_name(args):
    """没有 invoke name 时按参数特征猜工具名(agent 框架的常用工具)。"""
    keys = set(args)
    if "command" in keys:
        return "bash"
    if "file_path" in keys and "old_string" in keys:
        return "edit"
    if "file_path" in keys and "content" in keys:
        return "write"
    if "file_path" in keys:
        return "read"
    return "unknown"


def _make_call(name, args):
    return {
        "id": "call_dsml_" + uuid.uuid4().hex[:8],
        "type": "function",
        "function": {"name": name or _guess_tool_name(args),
                     "arguments": json.dumps(args, ensure_ascii=False)},
    }


def _parse_dsml_block(block):
    """把一个 <｜｜DSML｜｜ calls> 块(含首尾标签)解析成 OpenAI tool_calls 列表。
    容错: invoke 外壳缺失/未闭合时, 把块内的孤儿 parameter 抢救成单个调用。"""
    calls = []
    for im in _RE_INVOKE.finditer(block):
        calls.append(_make_call(im.group(1), _extract_params(im.group(2))))
    # 孤儿参数: 不在任何完整 <invoke>...</invoke> 内的 parameter(畸形输出)
    rest = block
    for im in _RE_INVOKE.finditer(block):
        rest = rest.replace(im.group(0), "\n", 1)
    oargs = _extract_params(rest)
    if oargs:
        calls.append(_make_call("", oargs))
    return calls


def _find_blocks(text):
    """找出文本里所有 calls 块。返回 [(start, end, block, complete), ...],
    end 为闭合标签之后(未闭合时为 len(text))。"""
    blocks = []
    pos = 0
    while True:
        s = text.find(_DSML_CALLS_START, pos)
        if s < 0:
            break
        e = text.find(_DSML_CALLS_END, s)
        if e < 0:
            blocks.append((s, len(text), text[s:], False))
            break
        blocks.append((s, e + len(_DSML_CALLS_END),
                       text[s:e + len(_DSML_CALLS_END)], True))
        pos = e + len(_DSML_CALLS_END)
    return blocks


def split_dsml(text):
    """从整段文本里拆出 DSML 工具调用块。返回 (干净文本, tool_calls 列表)。
    完整块(含解析不出调用的畸形块)都会从正文移除; 未闭合块仅在能抢救出
    调用时移除, 否则按普通文本保留。"""
    tool_calls = []
    out, pos = [], 0
    for s, e, block, complete in _find_blocks(text):
        out.append(text[pos:s])
        calls = _parse_dsml_block(block)
        if complete or calls:
            tool_calls.extend(calls)
        else:
            out.append(block)  # 未闭合且救不出调用: 当普通文本
        pos = e
    out.append(text[pos:])
    return "".join(out).strip(), tool_calls


def _render_call(call):
    """把一个 tool_call 渲染成可读的多行文本。"""
    fn = call["function"]
    try:
        args = json.loads(fn["arguments"])
    except Exception:
        args = {}
    lines = [f"[工具调用 {fn['name']}]"]
    if isinstance(args, dict) and args:
        lines.extend(f"{k} = {v}" for k, v in args.items())
    return "\n".join(lines)


def translate_dsml(text):
    """把文本里内嵌的 DSML 工具块翻译成可读描述(用于回传给模型的提示词)。
    例如工具结果里夹带的 <｜｜DSML｜｜ invoke ...> 会变成:
        [工具调用 edit]
        file_path = ...
        old_string = ...
    与 split_dsml 同样容错: 畸形/未闭合块能救出调用就翻译, 救不出按原文保留。
    """
    out, pos = [], 0
    for s, e, block, complete in _find_blocks(text):
        out.append(text[pos:s])
        calls = _parse_dsml_block(block)
        if complete or calls:
            out.append("\n\n".join(_render_call(c) for c in calls))
        else:
            out.append(block)
        pos = e
    out.append(text[pos:])
    return "".join(out)


class _DsmlStream:
    """流式版 split_dsml: 逐块喂入增量, 返回 (可安全输出的文本, 完整工具块列表)。
    文本会一直输出到工具块开始前; 工具块内部先缓存, 闭合后整体交给调用方解析。"""

    def __init__(self):
        self.buf = ""
        self.in_block = False

    @staticmethod
    def _hold_len(buf, tag):
        """buf 尾部与 tag 前缀的最长匹配长度(标签可能被拆到多个增量里)。"""
        maxk = min(len(buf), len(tag))
        for k in range(maxk, 0, -1):
            if buf.endswith(tag[:k]):
                return k
        return 0

    def feed(self, text):
        self.buf += text
        emitted, blocks = [], []
        while True:
            if not self.in_block:
                i = self.buf.find("<" + _DSML_SEP)
                if i < 0:
                    hold = self._hold_len(self.buf, _DSML_CALLS_START)
                    if hold:
                        if len(self.buf) > hold:
                            emitted.append(self.buf[:-hold])
                        self.buf = self.buf[-hold:]
                    else:
                        emitted.append(self.buf)
                        self.buf = ""
                    break
                emitted.append(self.buf[:i])
                if self.buf.startswith(_DSML_CALLS_START, i):
                    self.buf = self.buf[i + len(_DSML_CALLS_START):]
                    self.in_block = True
                elif _DSML_CALLS_START.startswith(self.buf[i:]):
                    self.buf = self.buf[i:]  # 是标签前缀但数据不全, 等下一个增量
                    break
                else:
                    # '<｜｜' 不是工具标签开头(正文里恰好出现): 按普通文本放出,
                    # 继续扫描剩余部分
                    emitted.append(self.buf[i:i + 2])
                    self.buf = self.buf[i + 2:]
                    continue
            else:
                j = self.buf.find(_DSML_CALLS_END)
                if j < 0:
                    break  # 块未闭合, 继续累积
                blocks.append(self.buf[:j])
                self.buf = self.buf[j + len(_DSML_CALLS_END):]
                self.in_block = False
        return "".join(emitted), blocks

    def finish(self):
        """流结束: 剩余内容吐出。若停在未闭合的 calls 块内, 把块内容作为
        "块"返回, 让调用方尝试抢救出完整的 parameter(畸形输出兜底)。"""
        if self.in_block:
            inner = self.buf
            self.buf = ""
            self.in_block = False
            return "", [inner]
        text = self.buf
        self.buf = ""
        return text, []


def _extract_prompt(messages, is_new_session):
    """从 OpenAI messages 数组取出要发给 DeepSeek 的 prompt。
    规则:
      - 末尾是若干条 role=tool(工具结果): 拼成"工具返回结果"作为 prompt;
      - 否则取最后一条 user 消息;
      - 全新会话且带了更早的历史时, 把历史拼进提示词。"""
    items = []
    for m in messages:
        if not isinstance(m, dict):
            continue
        role = m.get("role")
        content = _text_of(m.get("content")).strip()
        if role in ("system", "user", "assistant", "tool") and content:
            items.append((role, content, m))
    if not items:
        raise ValueError("messages 里没有有效消息")
    # 从历史 assistant 消息的 tool_calls 里建 id -> 工具名 映射
    # (工具结果消息通常只带 tool_call_id, 用它还原真实工具名)
    id2name = {}
    for _role, _content, m in items:
        for tc in (m.get("tool_calls") or []):
            if isinstance(tc, dict) and tc.get("id"):
                fn = tc.get("function") or {}
                if fn.get("name"):
                    id2name[str(tc["id"])] = str(fn["name"])
    # 末尾连续的 tool 结果
    tool_results = []
    idx = len(items) - 1
    while idx >= 0 and items[idx][0] == "tool":
        m = items[idx][2]
        name = (m.get("name") or id2name.get(str(m.get("tool_call_id") or ""))
                or m.get("tool_call_id") or "tool")
        tool_results.insert(0, (str(name), items[idx][1]))
        idx -= 1
    prompt = None
    if tool_results:
        prompt = ("以下是你刚才调用的工具的执行结果:\n"
                  + "\n\n".join(f"[{name}] 返回:\n{c}" for name, c in tool_results))
    else:
        last_user = -1
        for i in range(len(items) - 1, -1, -1):
            if items[i][0] == "user":
                last_user = i
                break
        if last_user < 0:
            raise ValueError("messages 里最后一条有效消息必须是 user")
        prompt = items[last_user][1]
        if is_new_session and last_user > 0:
            lines = []
            for role, content, _m in items[:last_user]:
                tag = {"user": "用户", "assistant": "助手", "tool": "工具"}.get(role, "系统")
                lines.append(f"[{tag}] {content}")
            prompt = ("以下是此前的对话记录:\n" + "\n".join(lines)
                      + "\n\n请基于以上记录, 回答最后一条消息:\n" + prompt)
    if tool_results and is_new_session and idx >= 0:
        # 全新会话里直接就是工具结果(罕见): 把更早历史也拼上
        lines = []
        for role, content, _m in items[:idx + 1]:
            tag = {"user": "用户", "assistant": "助手", "tool": "工具"}.get(role, "系统")
            lines.append(f"[{tag}] {content}")
        prompt = ("以下是此前的对话记录:\n" + "\n".join(lines) + "\n\n" + prompt)
    # 回传的文本里若夹带 DSML 工具块(比如工具结果里带了模型上次的调用),
    # 翻译成可读描述, 免得模型再看到一堆原始标记
    return translate_dsml(prompt)


def _usage(state):
    """从最终消息状态里取 token 用量(accumulated_token_usage 为本次回复的 token 数)。"""
    if not isinstance(state, dict):
        return None
    resp = state.get("response") or {}
    n = resp.get("accumulated_token_usage")
    if isinstance(n, (int, float)) and n > 0:
        n = int(n)
        return {"prompt_tokens": 0, "completion_tokens": n, "total_tokens": n}
    return None


class Handler(BaseHTTPRequestHandler):
    server_version = "DSOpenAI/1.0"
    timeout = 900

    def log_message(self, fmt, *args):
        sys.stderr.write("[%s] %s\n" % (self.log_date_time_string(), fmt % args))
        sys.stderr.flush()

    # ---------- 基础工具 ----------

    def _auth_ok(self):
        key = os.environ.get("DS_API_KEY")
        if not key:
            return True  # 没设 DS_API_KEY 就不校验
        return self.headers.get("Authorization", "") == "Bearer " + key

    def _send_json(self, code, obj, extra_headers=None):
        b = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(b)))
        for k, v in (extra_headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(b)

    def _error(self, code, msg, etype="invalid_request_error", extra_headers=None):
        self._send_json(code, {"error": {"message": msg, "type": etype, "code": code}},
                        extra_headers)

    # ---------- 路由 ----------

    def do_GET(self):
        path = self.path.split("?")[0].rstrip("/")
        if path in ("", "/health"):
            self._send_json(200, {"ok": True, "upstream": "chat.deepseek.com"})
        elif path == "/v1/models":
            now = int(time.time())
            self._send_json(200, {"object": "list", "data": [
                {"id": MODEL_CHAT, "object": "model", "created": now,
                 "owned_by": "deepseek-chat-web"},
                {"id": MODEL_REASONER, "object": "model", "created": now,
                 "owned_by": "deepseek-chat-web"},
            ]})
        else:
            self._error(404, f"no route: {path}", "not_found")

    def do_POST(self):
        path = self.path.split("?")[0].rstrip("/")
        if path != "/v1/chat/completions":
            return self._error(404, f"no route: {path}", "not_found")
        if not self._auth_ok():
            return self._error(401, "invalid API key", "authentication_error")
        try:
            length = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(length) or b"{}")
        except Exception as e:
            return self._error(400, f"bad JSON body: {e}")
        try:
            self._chat_completions(body)
        except ValueError as e:
            self._error(400, str(e))
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as e:
            try:
                self._error(502, f"upstream error: {e}", "upstream_error")
            except Exception:
                pass

    # ---------- 核心: chat/completions ----------

    def _chat_completions(self, body):
        messages = body.get("messages")
        if not isinstance(messages, list) or not messages:
            raise ValueError("messages 不能为空(OpenAI 格式)")
        model = body.get("model") or MODEL_CHAT
        if model not in (MODEL_CHAT, MODEL_REASONER):
            model = MODEL_CHAT
        stream = bool(body.get("stream"))
        thinking = bool(body.get("thinking")) or model == MODEL_REASONER
        search = bool(body.get("search"))

        # --- 会话: 优先请求里的 ds_session_id, 其次默认会话, 都没有才新建 ---
        meta = body.get("metadata") or {}
        sid = body.get("ds_session_id") or meta.get("ds_session_id") or body.get("user")
        if not sid:
            sid = _pick_default_session()
        is_new = not sid
        if is_new:
            sess = ds.create_session()
            sid = sess.get("id") if isinstance(sess, dict) else sess
            _session_parents[sid] = None
        with _lock:
            parent = _session_parents.get(sid)
        if parent is None and sid in _session_parents:
            parent = None  # 本网关注入的新会话, 还没有消息
        elif parent is None:
            parent = ds._resolve_parent(sid)  # 外部会话: 查一次最后消息 id
            with _lock:
                _session_parents[sid] = parent

        prompt = _extract_prompt(messages, is_new)

        # --- 图片: 从最后一条 user 消息里取 image_url, 上传并等解析 ---
        ref_file_ids = []
        last_user = None
        for m in messages:
            if isinstance(m, dict) and m.get("role") == "user":
                last_user = m
        if last_user is not None:
            image_urls = _extract_image_urls(last_user)
            if image_urls:
                ref_file_ids = _upload_images(image_urls)

        cid = f"chatcmpl-ds-{sid[:8]}-{int(time.time() * 1000)}"
        created = int(time.time())
        session_hdr = {"X-DS-Session-Id": sid}

        answer_parts, think_parts = [], []
        final_state = {}

        def collect():
            nonlocal final_state
            for kind, a, b in ds.stream_completion(sid, prompt, thinking,
                                                   search, parent_message_id=parent,
                                                   ref_file_ids=ref_file_ids):
                if kind == "delta":
                    if a == "thinking":
                        think_parts.append(b)
                    else:
                        answer_parts.append(b)
                elif kind == "done":
                    final_state = a

        if stream:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "close")
            self.send_header("X-DS-Session-Id", sid)
            self.end_headers()

            def sse(obj):
                self.wfile.write(b"data: " + json.dumps(obj, ensure_ascii=False).encode("utf-8")
                                 + b"\n\n")
                self.wfile.flush()

            def chunk(delta, finish=None, usage=None):
                c = {"id": cid, "object": "chat.completion.chunk", "created": created,
                     "model": model,
                     "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}
                if usage is not None:
                    c["usage"] = usage
                return c

            sse(chunk({"role": "assistant", "content": ""}))
            dsml = _DsmlStream()
            tool_calls = []
            try:
                for kind, a, b in ds.stream_completion(sid, prompt, thinking,
                                                       search, parent_message_id=parent,
                                                       ref_file_ids=ref_file_ids):
                    if kind == "ready":
                        pass
                    elif kind == "delta":
                        if a == "thinking":
                            think_parts.append(b)
                            sse(chunk({"reasoning_content": b}))
                        else:
                            text, blocks = dsml.feed(b)
                            if text:
                                answer_parts.append(text)
                                sse(chunk({"content": text}))
                            for blk in blocks:
                                tool_calls.extend(
                                    _parse_dsml_block(
                                        _DSML_CALLS_START + blk + _DSML_CALLS_END))
                    elif kind == "done":
                        final_state = a
                tail, tail_blocks = dsml.finish()
                if tail:
                    answer_parts.append(tail)
                    sse(chunk({"content": tail}))
                # 流结束在没闭合的 calls 块里: 抢救出块内完整的 parameter
                for blk in tail_blocks:
                    tool_calls.extend(
                        _parse_dsml_block(_DSML_CALLS_START + blk + _DSML_CALLS_END))
                if tool_calls:
                    sse(chunk({"tool_calls": tool_calls}, finish="tool_calls"))
                else:
                    sse(chunk({}, finish="stop"))
                sse(chunk({}, usage=_usage(final_state)))
                self.wfile.write(b"data: [DONE]\n\n")
                self.wfile.flush()
            except Exception as e:
                try:
                    sse({"error": {"message": str(e), "type": "upstream_error"}})
                    self.wfile.write(b"data: [DONE]\n\n")
                    self.wfile.flush()
                except Exception:
                    pass
        else:
            collect()
            clean_text, tool_calls = split_dsml("".join(answer_parts))
            message = {"role": "assistant", "content": clean_text or None}
            if tool_calls:
                message["tool_calls"] = tool_calls
            if "".join(think_parts):
                message["reasoning_content"] = "".join(think_parts)
            resp = {
                "id": cid,
                "object": "chat.completion",
                "created": created,
                "model": model,
                "choices": [{"index": 0, "message": message,
                             "finish_reason": "tool_calls" if tool_calls else "stop",
                             "logprobs": None}],
                "usage": _usage(final_state),
                "ds_session_id": sid,
            }
            self._send_json(200, resp, session_hdr)

        # 从最终状态里取 response 的 message_id 作为下一轮 parent
        rid = None
        if isinstance(final_state, dict):
            rid = (final_state.get("response") or {}).get("message_id")
        if rid is not None:
            with _lock:
                _session_parents[sid] = rid


def main():
    global DEFAULT_SESSION
    ap = argparse.ArgumentParser(description="DeepSeek Chat 网页账号的 OpenAI 兼容网关")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--default-session", default=None, metavar="ID|latest",
                    help="请求不带 ds_session_id 时复用的网页会话; "
                         "latest=每次自动取最近更新的会话; 指定 ID=固定复用; 不带=每次新建")
    args = ap.parse_args()
    DEFAULT_SESSION = args.default_session
    srv = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"OpenAI 兼容网关已启动: http://{args.host}:{args.port}/v1", file=sys.stderr)
    if DEFAULT_SESSION:
        print(f"  默认会话: {DEFAULT_SESSION}"
              f"{'(每次请求自动解析最新会话)' if DEFAULT_SESSION == 'latest' else ''}",
              file=sys.stderr)
    print(f"  测试: curl http://{args.host}:{args.port}/v1/chat/completions "
          f"-H 'Content-Type: application/json' "
          f"-d '{{\"model\":\"deepseek-chat\",\"messages\":[{{\"role\":\"user\","
          f"\"content\":\"你好\"}}]}}'", file=sys.stderr)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止", file=sys.stderr)


if __name__ == "__main__":
    main()
