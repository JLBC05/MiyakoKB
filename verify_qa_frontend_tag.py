# -*- coding: utf-8 -*-
"""Verification script for Manga QA Stream and Frontend Entity Tag Rendering.

Validates:
1. SSE streaming response from backend QA endpoint (/api/v2/insight/books/{book_id}/qa).
2. 'context' event containing 'resolvedEntities' with canonical_name "斯科特·朝圣者".
3. 'chunk' events streaming back the LLM answer text.
4. 'done' event completing the QA session.
5. Frontend closed-loop rendering logic and bundle verification.
"""

from __future__ import annotations

import json
from pathlib import Path
import sys
import urllib.request
import urllib.error

# Ensure UTF-8 output on Windows console
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

BOOK_ID = "92daa1f4-ce28-4cfe-945c-1deb95cf5939"
BASE_URL = "http://127.0.0.1:5000"
QA_ENDPOINT = f"{BASE_URL}/api/v2/insight/books/{BOOK_ID}/qa"
QUESTION = "斯科特是谁？"


def parse_sse_stream(response):
    """Parse raw SSE stream into a generator of (event, data_json_or_text)."""
    current_event = "message"
    for raw_line in response:
        line = raw_line.decode("utf-8")
        line_clean = line.rstrip("\r\n")
        if not line_clean or line_clean.startswith(":"):
            # Empty line or heartbeat comment
            continue
        if line_clean.startswith("event:"):
            current_event = line_clean[len("event:"):].strip()
        elif line_clean.startswith("data:"):
            data_str = line_clean[len("data:"):].strip()
            try:
                data_val = json.loads(data_str)
            except json.JSONDecodeError:
                data_val = data_str
            yield current_event, data_val
            current_event = "message"


def verify_qa_stream(question: str = QUESTION):
    print("=" * 70)
    print(f"【步骤 1】向后端 QA 接口发送提问请求")
    print(f"接口地址: {QA_ENDPOINT}")
    print(f"书籍 ID:  {BOOK_ID} (《歪小子斯科特》)")
    print(f"问题内容: {question}")
    print("=" * 70)

    payload = {
        "question": question,
        "mode": "global",
        "topK": 5,
        "threshold": 0,
        "useReranker": True,
    }

    req = urllib.request.Request(
        QA_ENDPOINT,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )

    received_events = []
    context_data = None
    answer_chunks = []
    done_received = False

    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            for event_name, data in parse_sse_stream(resp):
                received_events.append(event_name)
                if event_name == "context":
                    context_data = data
                elif event_name == "chunk":
                    if isinstance(data, dict) and "text" in data:
                        answer_chunks.append(data["text"])
                    elif isinstance(data, str):
                        answer_chunks.append(data)
                elif event_name == "done":
                    done_received = True
    except urllib.error.URLError as e:
        print(f"❌ 请求失败: {e}")
        sys.exit(1)

    print(f"\n【步骤 2】验证 SSE 事件流捕获结果:")
    print(f"  - 捕获的所有事件类型: {list(dict.fromkeys(received_events))}")
    
    # 验证 1: 收到 context 事件
    assert "context" in received_events, "未收到 event: context 事件！"
    print("  ✅ 成功接收到 event: context")

    # 验证 2: context 中包含 resolvedEntities
    assert context_data is not None, "未解析到 context 数据！"
    assert "resolvedEntities" in context_data, "context 中未包含 resolvedEntities！"
    print("  ✅ context 中的 data 正确包含 resolvedEntities 字段")

    resolved_entities = context_data["resolvedEntities"]
    matched_entities = resolved_entities.get("matched_entities", [])
    print(f"  - 匹配到的实体数量: {len(matched_entities)}")

    # 验证 3: matched_entities 中包含 canonical_name == '斯科特·朝圣者'
    canonical_names = [e.get("canonical_name") for e in matched_entities]
    assert "斯科特·朝圣者" in canonical_names, f"matched_entities 中未找到 '斯科特·朝圣者'，实际匹配: {canonical_names}"
    print(f"  ✅ matched_entities 中包含 canonical_name: '斯科特·朝圣者' (匹配实体: {canonical_names})")

    # 验证 4: 收到 text / chunk 流式回答
    assert len(answer_chunks) > 0, "未收到任何 chunk 流式回答文本！"
    full_answer = "".join(answer_chunks)
    print(f"  ✅ 成功接收到流式输出的回答文本 (共 {len(answer_chunks)} 个 chunk, {len(full_answer)} 字符)")

    # 验证 5: 收到 done 事件
    assert done_received, "未收到 event: done 事件！"
    print("  ✅ 成功接收到 event: done 标志结束")

    print("\n" + "=" * 70)
    print("【步骤 3】前端标签渲染闭环验证")
    print("=" * 70)

    # 检查前端打包代码
    project_root = Path(__file__).resolve().parent
    static_js_dir = project_root / "src" / "backend_v2" / "static" / "vue" / "js"
    qaview_files = list(static_js_dir.glob("QaView.*.js"))
    assert qaview_files, "未找到前端打包的 QaView.*.js 产物！"
    bundle_file = qaview_files[0]

    with open(bundle_file, "r", encoding="utf-8") as f:
        bundle_content = f.read()

    assert "qa-resolved-entities" in bundle_content, "前端打包产物中未找到 qa-resolved-entities 类名！"
    assert "基于实体知识库：" in bundle_content, "前端打包产物中未找到 '基于实体知识库：' 模板文本！"
    print(f"  ✅ 前端打包产物 ({bundle_file.name}) 包含实体标签类名与模板逻辑")

    # 模拟前端状态赋值与模板渲染
    frontend_message = {
        "id": "msg-assist-test",
        "role": "assistant",
        "content": full_answer,
        "mode": context_data.get("mode", "precise"),
        "citations": context_data.get("citations", []),
        "resolvedEntities": resolved_entities,
        "isLoading": False,
    }

    # 模拟 Vue 模板条件：
    # v-if="message.resolvedEntities?.matched_entities?.length"
    matched = frontend_message.get("resolvedEntities", {}).get("matched_entities", [])
    condition_active = bool(matched and len(matched) > 0)
    assert condition_active, "前端组件模板条件 v-if 未激活！"
    print("  ✅ 前端模板条件 message.resolvedEntities?.matched_entities?.length 准确激活")

    # 模拟渲染出的标签 DOM 结构与文字内容
    rendered_icon = "🏷️"
    rendered_label = "基于实体知识库："
    rendered_list = "、".join(e.get("canonical_name", "") for e in matched)
    rendered_tag_text = f"{rendered_icon} {rendered_label}{rendered_list}"
    
    print(f"  ✅ 模拟渲染标签文字: {rendered_tag_text}")

    print("\n" + "=" * 70)
    print("【步骤 4】最终验证数据输出汇总")
    print("=" * 70)

    print("\n1. 实际的 SSE context 事件内容:")
    print("-" * 50)
    print(json.dumps(context_data, ensure_ascii=False, indent=2))

    print("\n2. 最终生成的回答文本:")
    print("-" * 50)
    print(full_answer)

    print("\n3. 前端渲染出的标签文字:")
    print("-" * 50)
    print(rendered_tag_text)

    print("\n" + "=" * 70)
    print("🎉 全部闭环验证通过！")
    print("=" * 70)

    return {
        "context_data": context_data,
        "full_answer": full_answer,
        "rendered_tag_text": rendered_tag_text,
    }


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Verify QA SSE stream and frontend tags")
    parser.add_argument("--question", default=QUESTION, help="Question to ask")
    parser.add_argument("--all", action="store_true", help="Run both test questions")
    args = parser.parse_args()

    if args.all:
        print("\n>>> 正在运行全部测试问题验证 <<<\n")
        verify_qa_stream("斯科特是谁？")
        print("\n\n")
        verify_qa_stream("斯科特在第几页出现最多？")
    else:
        verify_qa_stream(args.question)
