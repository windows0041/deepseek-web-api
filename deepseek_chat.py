#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
DeepSeek Chat 网页版 Python 收发脚本(单文件, 仅依赖 Python 3.8+ 标准库)。

通过逆向 chat.deepseek.com 前端 JS 得到的 REST API 实现:
  - 认证: 请求头 Authorization: Bearer <userToken>(浏览器 localStorage 里的 userToken.value)
  - 发送消息前必须先解一道 POW(工作量证明), 请求头 X-DS-PoW-Response 携带答案
  - 发送走 SSE(服务器发送事件)流, 增量返回回复

用法示例:
  python deepseek_chat.py selftest                     # 自检: 验证 POW 哈希实现
  python deepseek_chat.py list                         # 列出最近会话
  python deepseek_chat.py read  <会话ID>                # 查看某个会话的历史消息
  python deepseek_chat.py chat  <会话ID>                # 交互模式: 连续对话(输 exit 退出)
  python deepseek_chat.py chat  -                      # 交互模式 + 新建会话
  python deepseek_chat.py send  <会话ID> "你好"         # 发一条消息并等回复(一次性)
  python deepseek_chat.py new   "你好"                 # 新建会话并发第一条消息
  python deepseek_chat.py send  <会话ID> "你好" --think  # 开启思考模式
  python deepseek_chat.py send  <会话ID> "你好" --search # 开启联网搜索
  python deepseek_chat.py send  <会话ID> "这是什么?" --file 图片.png  # 发图片

交互模式(chat)里:
  - 每轮回复流式打印, 直接输入下一句继续
  - exit / quit / q / Ctrl-D 退出
  - /think /notthink /search /nosearch 随时切换思考与联网
  - 注意: 服务器把 POW 答案当一次性消费, 每发一句都要重新求解一次(约 20 秒)

说明:
  - 配置自己的 token: 设置环境变量 DS_TOKEN, 或把 CONFIG 里的 TOKEN 改成
    你自己的(浏览器登录 chat.deepseek.com 后, 控制台执行
    JSON.parse(localStorage.getItem("userToken")).value 得到)。
  - 解 POW 需要纯 Python 跑 Keccak 变体, 难度 144000 时一般 20~40 秒。
  - 请求太快会触发腾讯 WAF 返回 429, 脚本里已留了间隔; 若仍遇到, 可在
    CONFIG 的 COOKIE 里带上浏览器里的 HWWAFSESID / HWWAFSESTIME。
"""

import base64
import json
import mimetypes
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

# ============================== 配置 ==============================
# 优先读环境变量 DS_TOKEN / DS_COOKIE(推荐, 方便 Docker/CI), 否则用下面的默认值。
TOKEN = os.environ.get("DS_TOKEN", "在这里填入你的 userToken")
COOKIE = os.environ.get("DS_COOKIE", "")  # 可选, 例如 "HWWAFSESID=xxx; HWWAFSESTIME=123"
BASE_URL = "https://chat.deepseek.com"
USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")
REQUEST_GAP = 1.0  # 相邻请求之间的最小间隔(秒), 防 WAF
# ==================================================================

# =====================================================================
# DeepSeekHashV1 —— 前端 JS(模块 88387)里 Keccak 海绵构造的纯 Python 移植
#
# 与标准 SHA3-256 的区别:
#   1) 轮数只有 23 轮(标准 Keccak-f[1600] 是 24 轮);
#   2) 轮常量为自定义表(前几轮与标准相同, 之后不同)。
# 其余(轮结构 theta/rho+pi/chi/iota、速率 136 字节、容量 256 比特、
# padding 0x06…0x80、32 字节输出)与标准 Keccak 一致。
#
# 已用浏览器 worker 的真实计算结果逐 lane 验证通过。
# =====================================================================

_M64 = (1 << 64) - 1

# 第 r 轮(r=1..23)的轮常量(64 位), 从前端 d 数组提取
_RC = [
    0x0000808200000000, 0x0000808A80000000, 0x8000800080000000,
    0x0000808B00000000, 0x8000000100000000, 0x8000808180000000,
    0x0000800980000000, 0x0000008A00000000, 0x0000008800000000,
    0x8000800900000000, 0x8000000A00000000, 0x8000808B00000000,
    0x0000008B80000000, 0x0000808980000000, 0x0000800380000000,
    0x0000800280000000, 0x0000008080000000, 0x0000800A00000000,
    0x8000000A80000000, 0x8000808180000000, 0x0000808080000000,
    0x8000000100000000, 0x8000800880000000,
]

# rho+pi 循环表(与标准 Keccak 相同): 从 lane 1=(x=1,y=0) 出发, 24 步访问
# 除 lane 0 外的全部 lane。DEST[i]=第 i 步目标 lane, ROT[i]=该步旋转量。
_DEST = [10, 7, 11, 17, 18, 3, 5, 16, 8, 21, 24, 4, 15, 23, 19, 13, 12, 2, 20, 14, 22, 9, 6, 1]
_ROT = [1, 3, 6, 10, 15, 21, 28, 36, 45, 55, 2, 14, 27, 41, 56, 8, 25, 43, 62, 18, 39, 61, 20, 44]


def _rotl64(x, a):
    return ((x << a) | (x >> (64 - a))) & _M64


def _keccak_f(A):
    """Keccak-f[1600] 变体(23 轮)。A: 25 个 64 位 lane 的 list, 索引 = y*5+x, 原地修改。"""
    rotl = _rotl64
    dest, rot, rc = _DEST, _ROT, _RC
    for r in range(1, 24):
        # --- theta ---
        c0 = A[0] ^ A[5] ^ A[10] ^ A[15] ^ A[20]
        c1 = A[1] ^ A[6] ^ A[11] ^ A[16] ^ A[21]
        c2 = A[2] ^ A[7] ^ A[12] ^ A[17] ^ A[22]
        c3 = A[3] ^ A[8] ^ A[13] ^ A[18] ^ A[23]
        c4 = A[4] ^ A[9] ^ A[14] ^ A[19] ^ A[24]
        d0 = c4 ^ rotl(c1, 1)
        d1 = c0 ^ rotl(c2, 1)
        d2 = c1 ^ rotl(c3, 1)
        d3 = c2 ^ rotl(c4, 1)
        d4 = c3 ^ rotl(c0, 1)
        A[0] ^= d0; A[5] ^= d0; A[10] ^= d0; A[15] ^= d0; A[20] ^= d0
        A[1] ^= d1; A[6] ^= d1; A[11] ^= d1; A[16] ^= d1; A[21] ^= d1
        A[2] ^= d2; A[7] ^= d2; A[12] ^= d2; A[17] ^= d2; A[22] ^= d2
        A[3] ^= d3; A[8] ^= d3; A[13] ^= d3; A[18] ^= d3; A[23] ^= d3
        A[4] ^= d4; A[9] ^= d4; A[14] ^= d4; A[19] ^= d4; A[24] ^= d4
        # --- rho + pi ---
        B = [0] * 25
        W = A[1]
        for i in range(24):
            t = dest[i]
            B[t] = rotl(W, rot[i])
            W = A[t]
        B[0] = A[0]
        A[:] = B
        # --- chi ---
        for y in range(5):
            b0, b1, b2, b3, b4 = A[y * 5], A[y * 5 + 1], A[y * 5 + 2], A[y * 5 + 3], A[y * 5 + 4]
            A[y * 5] = b0 ^ ((~b1 & _M64) & b2)
            A[y * 5 + 1] = b1 ^ ((~b2 & _M64) & b3)
            A[y * 5 + 2] = b2 ^ ((~b3 & _M64) & b4)
            A[y * 5 + 3] = b3 ^ ((~b4 & _M64) & b0)
            A[y * 5 + 4] = b4 ^ ((~b0 & _M64) & b1)
        # --- iota ---
        A[0] ^= rc[r - 1]
    return A


def _block_to_lanes(block):
    """136 字节块 -> 17 个 64 位 lane。DeepSeek 约定: 块前 4 字节进 lane 高 32 位。"""
    out = []
    for i in range(0, 136, 8):
        hi = int.from_bytes(block[i:i + 4], "little")
        lo = int.from_bytes(block[i + 4:i + 8], "little")
        out.append(lo | (hi << 32))
    return out


def deepseek_hash(data: bytes) -> bytes:
    """DeepSeekHashV1: 任意长度输入 -> 32 字节摘要。"""
    A = [0] * 25
    full = len(data) // 136
    for b in range(full):
        lanes = _block_to_lanes(data[b * 136:(b + 1) * 136])
        for i in range(17):
            A[i] ^= lanes[i]
        _keccak_f(A)
    tail = data[full * 136:]
    block = bytearray(136)
    block[:len(tail)] = tail
    block[len(tail)] |= 0x06
    block[135] |= 0x80
    lanes = _block_to_lanes(bytes(block))
    for i in range(17):
        A[i] ^= lanes[i]
    _keccak_f(A)
    out = bytearray()
    for i in range(4):
        out += (A[i] >> 32).to_bytes(4, "little")
        out += (A[i] & 0xFFFFFFFF).to_bytes(4, "little")
    return bytes(out)


def solve_pow(challenge: str, salt: str, expire_at, difficulty: int,
              progress_cb=None):
    """
    解 POW。
    规则(逆向自前端 worker):
      prefix = salt + "_" + expireAt + "_"
      找 nonce(0 <= nonce < difficulty), 使 deepseek_hash(prefix + str(nonce)) == challenge
    返回 nonce; 找不到返回 None。
    """
    prefix = f"{salt}_{expire_at}_".encode("utf-8")
    assert len(prefix) + 11 <= 136, "前缀过长"
    target = bytes.fromhex(challenge)
    t0 = (int.from_bytes(target[0:4], "little") << 32) | int.from_bytes(target[4:8], "little")
    t1 = (int.from_bytes(target[8:12], "little") << 32) | int.from_bytes(target[12:16], "little")
    t2 = (int.from_bytes(target[16:20], "little") << 32) | int.from_bytes(target[20:24], "little")
    t3 = (int.from_bytes(target[24:28], "little") << 32) | int.from_bytes(target[28:32], "little")
    base = bytearray(136)
    base[:len(prefix)] = prefix
    for nonce in range(difficulty):
        s = str(nonce).encode("ascii")
        block = bytearray(base)
        pos = len(prefix)
        block[pos:pos + len(s)] = s
        pos += len(s)
        block[pos] |= 0x06
        block[135] |= 0x80
        A = _block_to_lanes(bytes(block)) + [0] * 8
        _keccak_f(A)
        if A[0] == t0 and A[1] == t1 and A[2] == t2 and A[3] == t3:
            return nonce
        if progress_cb and nonce % 20000 == 0:
            progress_cb(nonce, difficulty)
    return None


# =====================================================================
# HTTP 封装
# =====================================================================

_last_request = [0.0]


def _headers(extra=None):
    if not TOKEN or "在这里填入" in TOKEN:
        raise RuntimeError(
            "还没配置 token: 设置环境变量 DS_TOKEN, 或把 deepseek_chat.py 顶部 "
            "TOKEN 改成你自己的(浏览器登录 chat.deepseek.com 后, 控制台执行 "
            'JSON.parse(localStorage.getItem("userToken")).value 得到)')
    h = {
        "Authorization": "Bearer " + TOKEN,
        "Content-Type": "application/json",
        "User-Agent": USER_AGENT,
        "Origin": BASE_URL,
        "Referer": BASE_URL + "/",
        "Accept": "application/json",
    }
    if COOKIE:
        h["Cookie"] = COOKIE
    if extra:
        h.update(extra)
    return h


def _throttle():
    wait = REQUEST_GAP - (time.time() - _last_request[0])
    if wait > 0:
        time.sleep(wait)
    _last_request[0] = time.time()


def http_json(method, path, body=None, timeout=60):
    """普通 JSON 请求, 返回 data.biz_data。"""
    _throttle()
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE_URL + path, data=data,
                                 headers=_headers(), method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            doc = json.load(r)
    except urllib.error.HTTPError as e:
        doc = json.load(e) if e.fp else {}
        raise RuntimeError(f"HTTP {e.code} {path}: "
                           f"{doc.get('msg') or doc.get('data', {}).get('biz_msg') or e.reason}")
    biz = doc.get("data", {})
    if biz.get("biz_code", 0) not in (0, None):
        raise RuntimeError(f"业务错误 {biz.get('biz_code')}: {biz.get('biz_msg')} ({path})")
    return biz.get("biz_data")


def sse_stream(path, body, extra_headers, timeout=300):
    """POST 一个 SSE 流式请求, 逐块 yield (event, data_str)。"""
    _throttle()
    h = _headers({"Accept": "text/event-stream", **extra_headers})
    req = urllib.request.Request(BASE_URL + path,
                                 data=json.dumps(body).encode(),
                                 headers=h, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        if r.status != 200:
            raise RuntimeError(f"SSE 连接失败: HTTP {r.status}")
        if "json" in (r.headers.get("content-type") or ""):
            doc = json.load(r)
            raise RuntimeError(f"服务器返回 JSON 错误: code={doc.get('code')} "
                               f"msg={doc.get('msg')} (常见原因: POW 答案无效/过期)")
        event = None
        data_lines = []
        while True:
            try:
                line = r.readline(65536)
            except Exception:
                break
            if not line:
                break
            text = line.decode("utf-8", "replace").rstrip("\r\n")
            if text == "":
                if data_lines:
                    yield (event, "\n".join(data_lines))
                    event, data_lines = None, []
            elif text.startswith("event:"):
                event = text[6:].strip()
            elif text.startswith("data:"):
                data_lines.append(text[5:].lstrip())
        if data_lines:
            yield (event, "\n".join(data_lines))


# =====================================================================
# JSON Patch(SSE 增量更新用)
# =====================================================================

def _patch_decode(seg):
    return seg.replace("~1", "/").replace("~0", "~")


def _nav(obj, segs):
    """沿 JSON Patch 路径导航到父对象。段可以是对象 key 或(负)数组下标, '-' 表示末尾。"""
    for s in segs:
        if s == "-":
            obj = obj[-1]
        elif s.lstrip("-").isdigit():
            obj = obj[int(s)]
        else:
            obj = obj[s]
    return obj


def _apply_patch(state, patch):
    """应用一个 JSON Patch 操作: {p: path, o: SET|APPEND|BATCH, v: value}。
    没有 p 的 {"v": ...} 表示整体替换根对象。"""
    path = patch.get("p")
    op = patch.get("o", "SET")
    v = patch.get("v")
    if path is None:
        return v  # 整体替换
    if op == "BATCH":
        # 子补丁以 path 指向的对象为上下文(如 {"p":"response","o":"BATCH",...}
        # 的子补丁作用在 response 消息对象上)
        ctx = _nav(state, [_patch_decode(x) for x in path.split("/")]) if path else state
        for sub in v:
            _apply_patch(ctx, sub)
        return state
    segs = [_patch_decode(x) for x in path.split("/")] if path else []
    if op == "SET":
        parent = _nav(state, segs[:-1]) if len(segs) > 1 else state
        last = segs[-1] if segs else None
        if last is None:
            return v
        if last == "-":
            parent.append(v)
        elif last.lstrip("-").isdigit():
            idx = int(last)
            if -len(parent) <= idx < len(parent):
                parent[idx] = v
            else:
                parent.append(v)
        else:
            parent[last] = v
        return state
    if op == "APPEND":
        parent = _nav(state, segs[:-1]) if len(segs) > 1 else state
        last = segs[-1]
        if last == "-" or last.lstrip("-").isdigit():
            idx = len(parent) - 1 if last == "-" else int(last)
            target = parent[idx]
            if isinstance(target, str):
                parent[idx] = target + v
            elif isinstance(target, list):
                target.extend(v if isinstance(v, list) else [v])
            else:
                parent[idx] = v
        else:  # 最后一节是 key: 值应为字符串(拼接)或列表(追加元素)
            if isinstance(parent.get(last), list):
                parent[last].extend(v if isinstance(v, list) else [v])
            else:
                parent[last] = (parent.get(last) or "") + v
        return state
    raise RuntimeError(f"未知 patch 操作: {op}")


def extract_reply(message):
    """从一条 ASSISTANT 消息对象里取出 (思考文本, 回复文本)。"""
    thinking, answer = "", ""
    for frag in message.get("fragments") or []:
        if not isinstance(frag, dict):
            continue
        ftype = frag.get("type")
        content = frag.get("content") or ""
        if ftype == "THINK":
            thinking += content
        elif ftype == "RESPONSE":
            answer += content
    return thinking, answer


# =====================================================================
# 业务 API
# =====================================================================

def list_sessions(count=30):
    """最近会话列表。"""
    d = http_json("GET", f"/api/v0/chat_session/fetch_page?count={count}")
    return d.get("chat_sessions", [])


def get_history(session_id):
    """某个会话的全部消息。"""
    d = http_json("GET", f"/api/v0/chat/history_messages?chat_session_id={session_id}")
    return d.get("chat_messages", [])


def create_session():
    """新建空会话, 返回会话对象。"""
    d = http_json("POST", "/api/v0/chat_session/create", {"agent": "chat"})
    return d


def upload_file(data: bytes, filename: str, content_type: str = None,
                verbose=True):
    """
    上传文件(图片/文档)到服务器, 返回文件对象(含 id/status)。
    上传接口也要 POW: 目标路径 /api/v0/file/upload_file, 请求为 multipart/form-data。
    """
    if content_type is None:
        content_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
    pow_header = solve_and_pow_header("/api/v0/file/upload_file", verbose=verbose)
    invalidate_pow_cache()  # 答案一次性
    boundary = "----dsh" + base64.b64encode(os.urandom(8)).decode().translate(
        {ord("+"): "", ord("/"): "", ord("="): ""})[:16]
    head = (f'--{boundary}\r\n'
            f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'
            f'Content-Type: {content_type}\r\n\r\n').encode()
    tail = f'\r\n--{boundary}--\r\n'.encode()
    body = head + data + tail
    _throttle()
    req = urllib.request.Request(
        BASE_URL + "/api/v0/file/upload_file", data=body,
        headers=_headers({"Content-Type": f"multipart/form-data; boundary={boundary}",
                          "X-DS-PoW-Response": pow_header}), method="POST")
    try:
        with urllib.request.urlopen(req, timeout=180) as r:
            doc = json.load(r)
    except urllib.error.HTTPError as e:
        doc = json.load(e) if e.fp else {}
        raise RuntimeError(f"HTTP {e.code} 上传失败: "
                           f"{doc.get('msg') or doc.get('data', {}).get('biz_msg') or e.reason}")
    biz = doc.get("data", {})
    if biz.get("biz_code", 0) not in (0, None):
        raise RuntimeError(f"上传失败 {biz.get('biz_code')}: {biz.get('biz_msg')}")
    return biz.get("biz_data")


def wait_file_ready(file_id: str, timeout=90.0, interval=2.0):
    """
    等服务器把文件解析完(图片要解析成视觉 token), 轮询 fetch_files 直到 status=SUCCESS。
    返回最终的文件对象; 失败/超时抛 RuntimeError。
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        d = http_json("GET", f"/api/v0/file/fetch_files?file_ids={file_id}")
        files = d.get("files") or []
        if files:
            f = files[0]
            status = f.get("status")
            if status == "SUCCESS":
                return f
            if status in ("FAILED", "FAILURE", "ERROR"):
                raise RuntimeError(f"文件解析失败: {f.get('error_code')} "
                                   f"{f.get('error_reason') or ''}")
        time.sleep(interval)
    raise RuntimeError(f"等待文件解析超时({timeout:.0f}s): {file_id}")


def attach_files(paths, verbose=True):
    """
    CLI 便捷入口: 把本地文件列表上传并等解析完, 返回 ref_file_ids 列表。
    """
    ids = []
    for p in paths or []:
        if not os.path.exists(p):
            raise RuntimeError(f"文件不存在: {p}")
        name = os.path.basename(p)
        if verbose:
            print(f"  [文件] 上传 {name} …", file=sys.stderr, flush=True)
        with open(p, "rb") as fh:
            fobj = upload_file(fh.read(), name, verbose=verbose)
        fid = fobj.get("id")
        if verbose:
            print(f"  [文件] 上传完成 {fid}, 等待解析…", file=sys.stderr, flush=True)
        wait_file_ready(fid)
        ids.append(fid)
        if verbose:
            print(f"  [文件] 解析完成 ✓", file=sys.stderr, flush=True)
    return ids


def fetch_pow_challenge(target_path):
    """向服务器要一道 POW 题。"""
    d = http_json("POST", "/api/v0/chat/create_pow_challenge",
                  {"target_path": target_path})
    return d["challenge"]


# POW 答案缓存: 题目 5 分钟内有效, 但服务器把答案当"一次性"消费
# (网页端每次取完答案立即清缓存), 所以 send_message 每用一次就作废一次。
# 缓存仍保留: 连续快速调用 solve_and_pow_header(比如并发取头)时可用。
_pow_cache = {"header": None, "expire_at": 0}
POW_EXPIRE_MARGIN_MS = 60000  # 提前 60 秒视为过期(与网页端一致)


def invalidate_pow_cache():
    _pow_cache["header"] = None
    _pow_cache["expire_at"] = 0


def solve_and_pow_header(target_path, verbose=True, use_cache=True):
    """取题 + 本地求解, 返回 X-DS-PoW-Response 头的值(带缓存)。"""
    if use_cache and _pow_cache["header"] and \
            _pow_cache["expire_at"] - POW_EXPIRE_MARGIN_MS > time.time() * 1000:
        if verbose:
            print("  [POW] 复用缓存答案(有效期 5 分钟, 免求解)", file=sys.stderr, flush=True)
        return _pow_cache["header"]
    ch = fetch_pow_challenge(target_path)
    if verbose:
        print(f"  [POW] 取题成功: 难度={ch['difficulty']}, 开始本地求解(约 20~40 秒)…",
              file=sys.stderr, flush=True)
    t0 = time.time()
    nonce = solve_pow(ch["challenge"], ch["salt"], ch["expire_at"], ch["difficulty"],
                      progress_cb=lambda a, b: print(f"  [POW] 进度 {a}/{b}",
                                                     file=sys.stderr, flush=True))
    if nonce is None:
        raise RuntimeError("POW 求解失败: 在难度范围内没有找到解")
    if verbose:
        print(f"  [POW] 求解完成: nonce={nonce}, 用时 {time.time() - t0:.1f}s",
              file=sys.stderr, flush=True)
    obj = {
        "algorithm": ch["algorithm"],
        "challenge": ch["challenge"],
        "salt": ch["salt"],
        "answer": nonce,
        "signature": ch["signature"],
        "target_path": target_path,
    }
    header = base64.b64encode(json.dumps(obj).encode()).decode()
    _pow_cache["header"] = header
    _pow_cache["expire_at"] = ch["expire_at"]
    return header


def stream_completion(session_id, prompt, thinking=False, search=False,
                      parent_message_id=None, model_type="default",
                      ref_file_ids=None):
    """
    底层流式收发(生成器)。在会话里发一条消息, 逐个 yield 事件:
      ("ready", request_message_id, response_message_id)  服务器接受消息
      ("delta", "thinking" | "answer", 新增文本)          流式增量
      ("done", 最终消息状态 dict, None)                    流结束
    ref_file_ids: 已上传并解析完成的文件 id 列表(图片/文档), 模型可见。
    """
    if parent_message_id is None:
        parent_message_id = _resolve_parent(session_id)

    pow_header = solve_and_pow_header("/api/v0/chat/completion")
    invalidate_pow_cache()  # 答案是一次性的, 用过即作废(下一句重新求解)
    body = {
        "chat_session_id": session_id,
        "parent_message_id": parent_message_id,
        "model_type": model_type,
        "prompt": prompt,
        "ref_file_ids": list(ref_file_ids or []),
        "thinking_enabled": thinking,
        "search_enabled": search,
        "source": "default",
        "action": None,
        "preempt": False,
    }
    request_id = response_id = None
    state = {}
    pending_text = ""  # 完整消息对象到达之前收到的文本块
    shown = {"thinking": 0, "answer": 0}  # 已产出增量的累计长度
    for event, data in sse_stream("/api/v0/chat/completion", body,
                                  {"X-DS-PoW-Response": pow_header}):
        if not data:
            continue
        try:
            payload = json.loads(data)
        except json.JSONDecodeError:
            continue
        if event == "ready":
            request_id = payload.get("request_message_id")
            response_id = payload.get("response_message_id")
            yield ("ready", request_id, response_id)
            continue
        if event in ("update_session", "close", "title", "finish"):
            continue
        # data 事件: 首个是完整消息对象, 之后是 JSON Patch 增量/流式文本块
        if isinstance(payload, dict):
            try:
                if "v" in payload and "p" not in payload:
                    v = payload["v"]
                    if isinstance(v, dict):
                        state = _apply_patch(state, payload)  # 完整消息对象
                        # 把之前缓存的文本块接到当前最后一个 fragment 上
                        if pending_text:
                            resp = state.get("response") or {}
                            frags = resp.get("fragments") or []
                            if frags:
                                frags[-1]["content"] = \
                                    (frags[-1].get("content") or "") + pending_text
                                pending_text = ""
                    elif isinstance(v, str):
                        # 流式文本块: 追加到响应消息的最后一个 fragment
                        resp = state.get("response") if isinstance(state, dict) else None
                        frags = (resp or {}).get("fragments") or []
                        if frags:
                            frags[-1]["content"] = (frags[-1].get("content") or "") + v
                        else:
                            pending_text += v  # 完整对象还没到, 先缓存
                elif "p" in payload:
                    state = _apply_patch(state, payload)
            except (KeyError, IndexError, TypeError) as e:
                print(f"  [SSE] 忽略无法应用的增量: {e}", file=sys.stderr)
            if isinstance(state, dict):
                t_now, a_now = extract_reply(state.get("response") or {})
                if len(a_now) > shown["answer"]:
                    yield ("delta", "answer", a_now[shown["answer"]:])
                    shown["answer"] = len(a_now)
                elif len(t_now) > shown["thinking"]:
                    yield ("delta", "thinking", t_now[shown["thinking"]:])
                    shown["thinking"] = len(t_now)
    yield ("done", state, None)


def send_message(session_id, prompt, thinking=False, search=False,
                 parent_message_id=None, model_type="default", stream=True,
                 ref_file_ids=None):
    """
    在会话里发一条消息, 等模型回复(流式读取 SSE), 返回
    (request_message_id, response_message_id, 思考文本, 回复文本)。
    stream=True 时把增量实时打印到 stdout。
    ref_file_ids: 已上传并解析完成的文件 id 列表(图片/文档)。
    """
    request_id = response_id = None
    final_state = {}
    shown = {"thinking": 0, "answer": 0}
    for kind, a, b in stream_completion(session_id, prompt, thinking, search,
                                        parent_message_id, model_type,
                                        ref_file_ids=ref_file_ids):
        if kind == "ready":
            request_id, response_id = a, b
        elif kind == "delta" and stream:
            if a == "thinking":
                if shown["thinking"] == 0:
                    print("💭 思考:", flush=True)
                print(b, end="", flush=True)
                shown["thinking"] += len(b)
            else:
                if shown["answer"] == 0:
                    if shown["thinking"]:
                        print()
                    print("助手回复:", flush=True)
                print(b, end="", flush=True)
                shown["answer"] += len(b)
        elif kind == "done":
            final_state = a
    thinking_text, answer_text = extract_reply(
        (final_state.get("response") or {}) if isinstance(final_state, dict) else {})
    if stream and (shown["thinking"] or shown["answer"]):
        print()
    return request_id, response_id, thinking_text, answer_text


# =====================================================================
# CLI
# =====================================================================

def _print_session(s):
    ts = time.strftime("%Y-%m-%d %H:%M", time.localtime(s.get("updated_at", 0)))
    mark = "📌" if s.get("pinned") else "  "
    print(f"{mark} {ts}  {s.get('title') or '(无标题)'}  [{s.get('id')}]")


def cmd_selftest():
    # 用之前抓取的真实题目验证哈希实现(答案 142256)
    prefix = "d3838172222a6c15821f_1790523315417_"
    h = deepseek_hash((prefix + "142256").encode()).hex()
    ok = h == "88f5cc9eeecd18479683e49a4ab03d9b2930f23d36cf175832edf85166253898"
    print("POW 哈希自检:", "通过 ✓" if ok else f"失败 ✗ (got {h})")
    return ok


def cmd_list():
    for s in list_sessions():
        _print_session(s)


def _fmt_message(m):
    role = m.get("role")
    ts = time.strftime("%m-%d %H:%M", time.localtime(m.get("inserted_at", 0)))
    if role == "USER":
        for frag in m.get("fragments") or []:
            if frag.get("type") == "REQUEST":
                print(f"[{ts}] 你: {frag.get('content', '')}")
        return
    thinking, answer = extract_reply(m)
    print(f"[{ts}] 助手:")
    if thinking:
        print(f"  💭 思考: {thinking}")
    print(f"  {answer}")


def cmd_read(session_id):
    for m in get_history(session_id):
        _fmt_message(m)


def cmd_send(session_id, text, thinking, search, files=None):
    print(f"发送消息到会话 {session_id} …", file=sys.stderr)
    ref_ids = attach_files(files) if files else []
    req_id, resp_id, thinking_text, answer = send_message(
        session_id, text, thinking=thinking, search=search, ref_file_ids=ref_ids)
    # 若流式输出未发生(例如非流式或没有增量), 兜底完整打印
    if not thinking_text.strip() and not answer.strip():
        print("(没有收到回复内容)")
    print(f"\n(消息 id: request={req_id}, response={resp_id})", file=sys.stderr)


def cmd_new(text, thinking, search, files=None):
    sess = create_session()
    sid = sess.get("id") if isinstance(sess, dict) else sess
    print(f"新建会话: {sid}")
    cmd_send(sid, text, thinking, search, files=files)


def _resolve_parent(session_id):
    """取会话当前最后一条消息 id(作为下一条消息的 parent)。"""
    for s in list_sessions(50):
        if s.get("id") == session_id:
            return s.get("current_message_id")
    hist = get_history(session_id)
    return hist[-1]["message_id"] if hist else None


def cmd_chat(session_id, thinking, search):
    """交互模式: 连续对话, 输入 exit/quit/q 或 Ctrl-D 退出。"""
    parent = _resolve_parent(session_id)
    print(f"进入会话 {session_id} (exit/quit/q 或 Ctrl-D 退出; "
          f"/think /notthink /search /nosearch 切换选项)", file=sys.stderr)
    while True:
        try:
            text = input("你: ")
        except (EOFError, KeyboardInterrupt):
            print()
            break
        text = text.strip()
        if not text:
            continue
        if text.lower() in ("exit", "quit", "q"):
            break
        if text in ("/think", "/notthink", "/search", "/nosearch"):
            if text == "/think":
                thinking = True
            elif text == "/notthink":
                thinking = False
            elif text == "/search":
                search = True
            else:
                search = False
            print(f"(当前: 思考={'开' if thinking else '关'}, 联网={'开' if search else '关'})",
                  file=sys.stderr)
            continue
        try:
            req_id, resp_id, _, _ = send_message(
                session_id, text, thinking=thinking, search=search,
                parent_message_id=parent)
            if resp_id is not None:
                parent = resp_id  # 下一句接着这句发, 免去重复查询
        except RuntimeError as e:
            print(f"错误: {e}", file=sys.stderr)


def main():
    args = sys.argv[1:]
    if not args:
        print(__doc__)
        return
    cmd = args[0]
    think = "--think" in args
    search = "--search" in args
    # --file 可多次出现: --file a.png --file b.pdf
    files = []
    rest = []
    i = 0
    while i < len(args):
        if args[i] == "--file" and i + 1 < len(args):
            files.append(args[i + 1])
            i += 2
        else:
            rest.append(args[i])
            i += 1
    args = [a for a in rest if a not in ("--think", "--search")]
    try:
        if cmd == "selftest":
            sys.exit(0 if cmd_selftest() else 1)
        elif cmd == "list":
            cmd_list()
        elif cmd == "read" and len(args) == 2:
            cmd_read(args[1])
        elif cmd == "send" and len(args) == 3:
            cmd_send(args[1], " ".join(args[2:]), think, search, files=files)
        elif cmd == "new" and len(args) >= 2:
            cmd_new(" ".join(args[1:]), think, search, files=files)
        elif cmd == "chat" and len(args) == 2:
            sid = args[1]
            if sid == "-":  # 新建一个会话进入交互模式
                sess = create_session()
                sid = sess.get("id") if isinstance(sess, dict) else sess
                print(f"新建会话: {sid}", file=sys.stderr)
            cmd_chat(sid, think, search)
        else:
            print(__doc__)
    except RuntimeError as e:
        print(f"错误: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
