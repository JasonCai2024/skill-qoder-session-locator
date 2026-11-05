#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
read_session.py — 读取并解析 Qoder 会话轨迹与状态。

用法:
    python read_session.py --session-id "c1f29a54-5be4-4163-a106-c2b26992eeaf" --summary
    python read_session.py --session-id "c1f29a54-5be4-4163-a106-c2b26992eeaf" --turn 1
    python read_session.py --session-id "c1f29a54-5be4-4163-a106-c2b26992eeaf" --json
    python read_session.py --path "C:\\Users\\pc\\.qoder\\projects\\...\\xxx.jsonl" --summary
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


def clean_session_id(raw_id: str) -> str:
    """去除可能携带的前缀如 qoder_。"""
    raw = raw_id.strip()
    if raw.startswith("qoder_"):
        return raw[6:]
    return raw


@dataclass
class TurnDetail:
    turn_index: int
    timestamp: Optional[str] = None
    user_prompts: List[str] = field(default_factory=list)
    assistant_messages: List[str] = field(default_factory=list)
    thinking_blocks: List[str] = field(default_factory=list)
    tool_calls: List[Dict[str, Any]] = field(default_factory=list)
    attachments: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def find_jsonl_by_session_id(sid: str, qoder_home: Optional[Path] = None) -> Optional[Path]:
    sid = clean_session_id(sid)
    home = qoder_home or (Path.home() / ".qoder")
    projects_dir = home / "projects"
    if not projects_dir.is_dir():
        return None

    for p in projects_dir.glob(f"*/{sid}.jsonl"):
        if p.is_file():
            return p
    return None


def extract_content_text(content_obj: Any) -> str:
    if isinstance(content_obj, str):
        return content_obj
    if isinstance(content_obj, list):
        parts = []
        for item in content_obj:
            if isinstance(item, dict):
                t = item.get("text") or item.get("content") or ""
                if t:
                    parts.append(str(t))
            elif isinstance(item, str):
                parts.append(item)
        return "\n".join(parts)
    if isinstance(content_obj, dict):
        return content_obj.get("text") or content_obj.get("content") or json.dumps(content_obj, ensure_ascii=False)
    return str(content_obj) if content_obj is not None else ""


def parse_session_jsonl(jsonl_path: Path) -> Tuple[Dict[str, Any], List[TurnDetail]]:
    metadata: Dict[str, Any] = {
        "session_id": jsonl_path.stem,
        "workspace": "",
        "model": "",
        "created_at": None,
        "updated_at": None,
        "total_turns": 0,
    }

    turns: List[TurnDetail] = []
    current_turn: Optional[TurnDetail] = None

    with open(jsonl_path, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except Exception:
                continue

            t_type = obj.get("type")

            # 1. 元数据
            if t_type == "workspace-directories":
                dirs = obj.get("directories", [])
                if dirs:
                    metadata["workspace"] = dirs[0]
                continue
            elif t_type == "runtime-config":
                metadata["model"] = obj.get("model", "")
                continue

            # 2. 用户轮次 (需区分真正的用户 Prompt 与 tool_result 回调)
            if t_type == "user":
                origin = obj.get("origin")
                human = obj.get("humanInput")
                msg = obj.get("message", {})
                content = msg.get("content", [])

                is_tool_res = False
                if isinstance(content, list):
                    for c in content:
                        if isinstance(c, dict) and c.get("type") == "tool_result":
                            is_tool_res = True
                            break

                if is_tool_res:
                    # 属于当前轮次内工具执行的返回，不作为新的用户 Prompt 轮次
                    continue

                text = ""
                if human and isinstance(human, dict) and human.get("text"):
                    text = human.get("text")
                else:
                    text = extract_content_text(content)

                text = text.strip()
                if not text:
                    continue

                if current_turn is not None:
                    turns.append(current_turn)

                turn_idx = len(turns) + 1
                current_turn = TurnDetail(
                    turn_index=turn_idx,
                    timestamp=obj.get("timestamp"),
                )
                current_turn.user_prompts.append(text)

                if not metadata["created_at"]:
                    metadata["created_at"] = obj.get("timestamp")
                metadata["updated_at"] = obj.get("timestamp")

            # 3. 助手轮次
            elif t_type == "assistant":
                if current_turn is None:
                    current_turn = TurnDetail(turn_index=1, timestamp=obj.get("timestamp"))

                msg = obj.get("message", {})
                if isinstance(msg, dict):
                    content = msg.get("content", [])
                    if isinstance(content, list):
                        for c in content:
                            if not isinstance(c, dict):
                                continue
                            ctype = c.get("type")
                            if ctype == "text":
                                txt = c.get("text", "")
                                if txt:
                                    current_turn.assistant_messages.append(txt)
                            elif ctype == "thinking":
                                th = c.get("thinking", "")
                                if th:
                                    current_turn.thinking_blocks.append(th)
                            elif ctype in ("tool_use", "tool_call"):
                                current_turn.tool_calls.append(c)
                    elif isinstance(content, str):
                        current_turn.assistant_messages.append(content)

                if obj.get("timestamp"):
                    metadata["updated_at"] = obj.get("timestamp")

            # 4. 附件事件
            elif t_type == "attachment":
                if current_turn is not None:
                    current_turn.attachments.append(obj.get("name") or obj.get("path") or json.dumps(obj, ensure_ascii=False))

    if current_turn is not None:
        turns.append(current_turn)

    metadata["total_turns"] = len(turns)
    return metadata, turns


def render_summary(meta: Dict[str, Any], turns: List[TurnDetail]) -> str:
    lines = [
        "=" * 80,
        f"Qoder 会话摘要: {meta['session_id']}",
        "=" * 80,
        f"工作区目录 : {meta.get('workspace') or '(未记录)'}",
        f"调用模型   : {meta.get('model') or '(未知)'}",
        f"创建时间   : {meta.get('created_at') or '(未知)'}",
        f"更新时间   : {meta.get('updated_at') or '(未知)'}",
        f"交互总轮数 : {meta.get('total_turns', 0)} 轮",
        "-" * 80,
    ]

    if not turns:
        lines.append("该会话暂无有效对话轮次。")
        return "\n".join(lines)

    first_prompt = turns[0].user_prompts[0] if turns[0].user_prompts else ""
    first_snip = (first_prompt[:120] + "...") if len(first_prompt) > 120 else first_prompt
    lines.append(f"【首轮 Prompt 摘要】:\n{first_snip}\n")

    lines.append(f"{'轮次':<5} {'时间戳':<24} {'工具调用':<8} {'用户指令摘要'}")
    lines.append("-" * 80)
    for t in turns[:20]:
        ts = t.timestamp or ""
        tools_cnt = str(len(t.tool_calls))
        p = t.user_prompts[0].replace("\n", " ") if t.user_prompts else "(无文本)"
        p_snip = (p[:45] + "...") if len(p) > 45 else p
        lines.append(f"#{t.turn_index:<4} {ts:<24} {tools_cnt:<8} {p_snip}")

    if len(turns) > 20:
        lines.append(f"... 其余 {len(turns) - 20} 轮交互已省略，可指定 --turn <N> 查看详情")

    lines.append("=" * 80)
    return "\n".join(lines)


def render_turn_detail(meta: Dict[str, Any], turn: TurnDetail) -> str:
    lines = [
        "=" * 80,
        f"Qoder 会话 #{turn.turn_index} 轮交互详情 [Session: {meta['session_id']}]",
        "=" * 80,
        f"时间戳: {turn.timestamp or '(未知)'}",
        "-" * 80,
        "【用户 Prompt】:",
    ]
    if turn.user_prompts:
        for p in turn.user_prompts:
            lines.append(p)
    else:
        lines.append("(无文本提示词)")

    if turn.attachments:
        lines.append("\n【附件】:")
        for att in turn.attachments:
            lines.append(f"  - {att}")

    if turn.thinking_blocks:
        lines.append("\n【模型思考链路 (Thinking)】:")
        for th in turn.thinking_blocks:
            lines.append(th)

    if turn.tool_calls:
        lines.append(f"\n【工具调用 ({len(turn.tool_calls)} 次)】:")
        for tc in turn.tool_calls:
            name = tc.get("name", "unknown")
            args = json.dumps(tc.get("input", {}), ensure_ascii=False)
            lines.append(f"  - {name}: {args[:150]}")

    if turn.assistant_messages:
        lines.append("\n【助手回答】:")
        for am in turn.assistant_messages:
            lines.append(am)

    lines.append("=" * 80)
    return "\n".join(lines)


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="read_session.py",
        description="读取并解析 Qoder 会话事件轨迹",
    )
    p.add_argument("--session-id", "-s", help="会话 ID (UUID 或 qoder_<UUID>)")
    p.add_argument("--path", "-p", help="直接指定 .jsonl 文件路径")
    p.add_argument("--summary", action="store_true", help="打印会话概览与轮次摘要列表")
    p.add_argument("--turn", "-t", type=int, help="查看指定轮次的详细提示词与回复")
    p.add_argument("--json", action="store_true", help="以 JSON 格式输出完整结构")
    return p


def main(argv: Optional[Iterable[str]] = None) -> int:
    args = build_arg_parser().parse_args(argv)

    jsonl_path: Optional[Path] = None
    if args.path:
        jsonl_path = Path(args.path).expanduser().resolve()
    elif args.session_id:
        jsonl_path = find_jsonl_by_session_id(args.session_id)
    else:
        print("错误: 必须提供 --session-id 或 --path 参数", file=sys.stderr)
        return 2

    if not jsonl_path or not jsonl_path.is_file():
        print(f"错误: 未找到会话 JSONL 文件: {jsonl_path or args.session_id}", file=sys.stderr)
        return 1

    meta, turns = parse_session_jsonl(jsonl_path)

    if args.json:
        payload = {
            "metadata": meta,
            "turns": [t.to_dict() for t in turns],
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0

    if args.turn:
        target_turn = next((t for t in turns if t.turn_index == args.turn), None)
        if not target_turn:
            print(f"错误: 会话中不存在轮次 #{args.turn} (总轮数: {len(turns)})", file=sys.stderr)
            return 1
        print(render_turn_detail(meta, target_turn))
        return 0

    # 默认打印 summary
    print(render_summary(meta, turns))
    return 0


if __name__ == "__main__":
    sys.exit(main())
