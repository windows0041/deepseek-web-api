#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
上下文窗口实测: 新建会话, 发一段长文本(在 4 个位置埋独特代码标记),
再发验证问题, 看模型能记住哪些标记 => 推断窗口大小与截断行为。

用法: python3 test_context.py <目标字符数>   (如 80000)
"""
import sys
import time

import deepseek_chat as ds

FILLER = "窗台上摆放着一盆绿色的多肉植物，阳光透过半开的窗帘缝隙洒在桌面上，空气里弥漫着淡淡的茶香，远处偶尔传来几声鸟鸣，一切都显得宁静而祥和。"
CODES = {
    "A": "ALPHA-1111",
    "B": "BRAVO-2222",
    "C": "CHARLIE-3333",
    "D": "DELTA-4444",
}


def _pad(text: str, n: int) -> str:
    """把 text 重复填充到 n 字符。"""
    if len(text) >= n:
        return text[:n]
    return (text * (n // len(text) + 1))[:n]


def build_message(n_chars: int) -> str:
    markers = [f"【标记{letter}】密码是: {code}" for letter, code in CODES.items()]
    marker_total = sum(len(m) for m in markers)
    filler_total = max(n_chars - marker_total - 100, 100)
    seg = filler_total // 4
    parts = []
    for i, m in enumerate(markers):
        if i > 0:
            parts.append(_pad(FILLER, seg))
        parts.append(m)
    parts.append(_pad(FILLER, filler_total - seg * 3))
    body = "".join(parts)
    return ("请仔细阅读下面这段长文, 其中包含 4 个标记(标记A/标记B/标记C/标记D), "
            "每个标记后面跟着一串密码。读完后只需回答: 已读完。\n\n"
            + body
            + "\n\n(全文结束)")


def run(n_chars: int):
    sess = ds.create_session()
    sid = sess["id"]
    text = build_message(n_chars)
    print(f"新会话 {sid} | 长文本实际长度 {len(text)} 字符 (约 {int(len(text)*0.9)}~{int(len(text)*1.1)} tokens)",
          file=sys.stderr, flush=True)

    t0 = time.time()
    req, resp, think, answer = ds.send_message(sid, text)
    print(f"\n[长文本已发送, 耗时 {time.time()-t0:.0f}s] 模型确认: {answer[:100].strip()}",
          file=sys.stderr, flush=True)

    q = ("我刚才发你的长文中包含 4 个标记: 标记A、标记B、标记C、标记D。"
         "请逐个告诉我每个标记后面跟着的确切密码字符串, 格式: A=xxx, B=xxx, C=xxx, D=xxx。"
         "如果某个记不清了, 就在那一项写'记不清'。")
    t0 = time.time()
    req, resp, think, answer2 = ds.send_message(sid, q)
    print(f"\n[验证问题, 耗时 {time.time()-t0:.0f}s]", file=sys.stderr, flush=True)
    print("=" * 60)
    print(f"会话: {sid}")
    print(f"长文本: {len(text)} 字符")
    print("模型回答:\n" + answer2)
    print("=" * 60)
    return answer2


if __name__ == "__main__":
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 80000
    run(n)
