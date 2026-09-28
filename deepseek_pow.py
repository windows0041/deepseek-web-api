# -*- coding: utf-8 -*-
"""
DeepSeekHashV1 —— DeepSeek Chat 网页版 POW(工作量证明) 纯 Python 实现。

算法来源: 反编译 chat.deepseek.com 前端 JS(模块 88387 中的 U 类 Keccak 海绵构造)。
与标准 SHA3-256 的区别:
  1. 轮数只有 23 轮(标准 Keccak-f[1600] 是 24 轮);
  2. 轮常量表是自定义的(前几轮与标准相同, 之后不同)。
其余部分(轮结构 theta/rho+pi/chi/iota、速率 136 字节、容量 256 比特、
padding 0x06 ... 0x80、32 字节输出)均与标准 Keccak 一致。

POW 规则(来自 worker 源码):
  prefix = salt + "_" + expireAt + "_"          (注意: 是 expireAt, 不是 signature!)
  寻找 nonce(0 <= nonce < difficulty), 使得
      DeepSeekHashV1(prefix + str(nonce)) == challenge
  解出的 answer 必须小于 difficulty。
"""

import struct

M64 = (1 << 64) - 1

# 轮常量: 第 r 轮(1..23)使用 RC[r-1]
# 从前端 d 数组(48 个 32 位字, 低字在前)提取: RC = pair[i], i=1..23
RC = [
    0x0000808200000000,
    0x0000808A80000000,
    0x8000800080000000,
    0x0000808B00000000,
    0x8000000100000000,
    0x8000808180000000,
    0x0000800980000000,
    0x0000008A00000000,
    0x0000008800000000,
    0x8000800900000000,
    0x8000000A00000000,
    0x8000808B00000000,
    0x0000008B80000000,
    0x0000808980000000,
    0x0000800380000000,
    0x0000800280000000,
    0x0000008080000000,
    0x0000800A00000000,
    0x8000000A80000000,
    0x8000808180000000,
    0x0000808080000000,
    0x8000000100000000,
    0x8000800880000000,
]

# rho+pi 循环: 从 lane 1=(x=1,y=0) 出发, 24 步访问除 lane 0 外的全部 lane
# DEST[i] = 第 i 步的目标 lane 索引(y*5+x), ROT[i] = 该步旋转量(源 lane 的 rho 值)
DEST = [10, 7, 11, 17, 18, 3, 5, 16, 8, 21, 24, 4, 15, 23, 19, 13, 12, 2, 20, 14, 22, 9, 6, 1]
ROT = [1, 3, 6, 10, 15, 21, 28, 36, 45, 55, 2, 14, 27, 41, 56, 8, 25, 43, 62, 18, 39, 61, 20, 44]


def _rotl64(x, a):
    return ((x << a) | (x >> (64 - a))) & M64


def _keccak_f(A):
    """Keccak-f[1600] 变体: 23 轮, A 为 25 个 64 位 lane 的 list(索引 = y*5+x)。原地修改并返回。"""
    rotl = _rotl64
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
            t = DEST[i]
            B[t] = rotl(W, ROT[i])
            W = A[t]
        B[0] = A[0]
        A[:] = B
        # --- chi ---
        for y in range(5):
            b0, b1, b2, b3, b4 = A[y * 5], A[y * 5 + 1], A[y * 5 + 2], A[y * 5 + 3], A[y * 5 + 4]
            A[y * 5] = b0 ^ (~b1 & b2) & M64
            A[y * 5 + 1] = b1 ^ (~b2 & b3) & M64
            A[y * 5 + 2] = b2 ^ (~b3 & b4) & M64
            A[y * 5 + 3] = b3 ^ (~b4 & b0) & M64
            A[y * 5 + 4] = b4 ^ (~b0 & b1) & M64
        # --- iota ---
        A[0] ^= RC[r - 1]
    return A


def _block_to_lanes(block):
    """
    136 字节块 -> 17 个 64 位 lane。
    注意 DeepSeek 的约定(与标准不同): 块的前 4 字节进 lane 的高 32 位,
    后 4 字节进低 32 位, 均按小端解释。
    """
    out = []
    for i in range(0, 136, 8):
        hi = int.from_bytes(block[i:i + 4], "little")
        lo = int.from_bytes(block[i + 4:i + 8], "little")
        out.append(lo | (hi << 32))
    return out


def deepseek_hash(data: bytes) -> bytes:
    """DeepSeekHashV1: 任意长度输入 -> 32 字节输出。"""
    A = [0] * 25
    n = len(data)
    full = n // 136
    for b in range(full):
        lanes = _block_to_lanes(data[b * 136:(b + 1) * 136])
        for i in range(17):
            A[i] ^= lanes[i]
        _keccak_f(A)
    # 最后一个(不完整的)块 + padding
    tail = data[full * 136:]
    block = bytearray(136)
    block[:len(tail)] = tail
    block[len(tail)] |= 0x06
    block[135] |= 0x80
    lanes = _block_to_lanes(bytes(block))
    for i in range(17):
        A[i] ^= lanes[i]
    _keccak_f(A)
    # 输出: 前 4 个 lane, 每个 lane 先输出高 32 位的小端字节, 再输出低 32 位
    out = bytearray()
    for i in range(4):
        out += (A[i] >> 32).to_bytes(4, "little")
        out += (A[i] & 0xFFFFFFFF).to_bytes(4, "little")
    return bytes(out)


def pow_prefix(salt: str, expire_at) -> str:
    """POW 前缀 = salt + "_" + expireAt + "_"。"""
    return f"{salt}_{expire_at}_"


def solve_pow(challenge: str, salt: str, expire_at, difficulty: int,
              progress_cb=None):
    """
    求解 POW: 返回 nonce 使得 deepseek_hash(prefix + str(nonce)) == challenge。
    解保证在 [0, difficulty) 内(找不到返回 None)。
    """
    prefix = pow_prefix(salt, expire_at).encode("utf-8")
    assert len(prefix) + 11 <= 136, "前缀过长, 单块求解不适用"
    target = bytes.fromhex(challenge)
    # 预先把 challenge 拆成 4 个 lane 值, 循环内直接整字比较(避免每次转字节)
    t0 = (int.from_bytes(target[0:4], "little") << 32) | int.from_bytes(target[4:8], "little")
    t1 = (int.from_bytes(target[8:12], "little") << 32) | int.from_bytes(target[12:16], "little")
    t2 = (int.from_bytes(target[16:20], "little") << 32) | int.from_bytes(target[20:24], "little")
    t3 = (int.from_bytes(target[24:28], "little") << 32) | int.from_bytes(target[28:32], "little")
    # 前缀部分固定, 先算出固定 lane(前 10 个 lane 完全由前缀决定; 后面随 nonce 变化)
    base = bytearray(136)
    base[:len(prefix)] = prefix
    for nonce in range(difficulty):
        s = str(nonce).encode("ascii")
        block = bytearray(base)
        block[len(prefix):len(prefix) + len(s)] = s
        pos = len(prefix) + len(s)
        block[pos] |= 0x06
        block[135] |= 0x80
        A = _block_to_lanes(bytes(block)) + [0] * 8
        _keccak_f(A)
        if A[0] == t0 and A[1] == t1 and A[2] == t2 and A[3] == t3:
            return nonce
        if progress_cb and nonce % 20000 == 0:
            progress_cb(nonce, difficulty)
    return None


def build_pow_response(challenge_obj: dict) -> dict:
    """
    由 create_pow_challenge 返回的 challenge 对象构造 pow-answer 对象。
    challenge_obj 字段: algorithm/challenge/salt/difficulty/expire_at(或 expireAt)/signature。
    """
    expire_at = challenge_obj.get("expireAt", challenge_obj.get("expire_at"))
    nonce = solve_pow(
        challenge_obj["challenge"],
        challenge_obj["salt"],
        expire_at,
        challenge_obj["difficulty"],
    )
    if nonce is None:
        raise RuntimeError("POW 求解失败: 在 difficulty 范围内没有找到解")
    return {
        "algorithm": challenge_obj["algorithm"],
        "challenge": challenge_obj["challenge"],
        "salt": challenge_obj["salt"],
        "answer": nonce,
        "signature": challenge_obj["signature"],
    }


if __name__ == "__main__":
    import sys, time

    # ---- 自检: 用之前抓取的真实 challenge 验证 ----
    test_prefix = "d3838172222a6c15821f_1790523315417_"
    h142256 = deepseek_hash((test_prefix + "142256").encode()).hex()
    print("H(prefix||'142256') =", h142256)
    assert h142256 == "88f5cc9eeecd18479683e49a4ab03d9b2930f23d36cf175832edf85166253898", "自检失败"
    print("自检通过 ✓ (与浏览器 worker 结果一致)")

    if len(sys.argv) > 1 and sys.argv[1] == "--speed":
        t0 = time.time()
        for _ in range(1000):
            deepseek_hash((test_prefix + "142256").encode())
        dt = time.time() - t0
        print(f"单块哈希: {dt/1000*1000:.3f} ms/次,  144000 次约需 {dt*144:.0f} 秒")
