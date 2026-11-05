#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
locate_session.py — 定位 Qoder 本地会话。

用法:
    python locate_session.py --workspace "数据标注"
    python locate_session.py --time "yesterday"
    python locate_session.py --archived-only --limit 10
    python locate_session.py --keyword "出题" --json

输出:
    默认人类可读表格；--json 时输出标准化结构化 JSON。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

DEFAULT_LIMIT = 10
MAX_WINDOW_DAYS = 30


@dataclass
class TimeWindow:
    start_utc: datetime
    end_utc: datetime
    parsed_from: str

    def width_days(self) -> float:
        return (self.end_utc - self.start_utc).total_seconds() / 86400


_CN_TOD = [
    ("凌晨", (0, 6)),
    ("上午", (6, 12)),
    ("中午", (11, 14)),
    ("下午", (12, 18)),
    ("傍晚", (17, 20)),
    ("晚上", (18, 24)),
    ("夜里", (20, 24)),
    ("深夜", (22, 24)),
]

_EN_TOD = [
    ("early morning", (0, 6)),
    ("late night", (22, 24)),
    ("morning", (6, 12)),
    ("noon", (11, 14)),
    ("afternoon", (12, 18)),
    ("evening", (17, 20)),
    ("night", (18, 24)),
]


def parse_time_hint(hint: str, now: Optional[datetime] = None) -> TimeWindow:
    if now is None:
        now = datetime.now().astimezone()

    hint_norm = hint.strip().lower()
    if not hint_norm:
        raise ValueError("时间提示为空")

    # 1. 尝试 ISO 格式
    iso = _try_parse_iso(hint_norm, now)
    if iso:
        return iso

    # 2. 相对天数
    day_anchor, consumed = _parse_day_anchor(hint_norm, now)
    if day_anchor is None:
        today = now.replace(hour=0, minute=0, second=0, microsecond=0)
        day_anchor, consumed = today, ""

    rem = hint_norm.replace(consumed, "").strip() if consumed else hint_norm

    # 3. 解析当天时段
    tod_range = None
    for kw, r in _CN_TOD:
        if kw in rem:
            tod_range = r
            break
    if not tod_range:
        for kw, r in _EN_TOD:
            if kw in rem:
                tod_range = r
                break

    if tod_range:
        start_hour, end_hour = tod_range
        start = day_anchor.replace(hour=start_hour, minute=0, second=0, microsecond=0)
        if end_hour == 24:
            end = (day_anchor + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
        else:
            end = day_anchor.replace(hour=end_hour, minute=0, second=0, microsecond=0)
    else:
        start = day_anchor.replace(hour=0, minute=0, second=0, microsecond=0)
        end = start + timedelta(days=1)

    return TimeWindow(
        start.astimezone(timezone.utc),
        end.astimezone(timezone.utc),
        hint,
    )


def _try_parse_iso(s: str, now: datetime) -> Optional[TimeWindow]:
    m = re.match(r"^(\d{4})-(\d{1,2})-(\d{1,2})$", s)
    if m:
        y, mth, d = (int(x) for x in m.groups())
        tz = now.tzinfo
        start = datetime(y, mth, d, 0, 0, 0, tzinfo=tz)
        end = start + timedelta(days=1)
        return TimeWindow(start.astimezone(timezone.utc), end.astimezone(timezone.utc), s)
    return None


def _parse_day_anchor(s: str, now: datetime) -> Tuple[Optional[datetime], str]:
    today = now.replace(hour=0, minute=0, second=0, microsecond=0)

    if "前天" in s or "the day before yesterday" in s:
        consumed = "前天" if "前天" in s else "the day before yesterday"
        return today - timedelta(days=2), consumed
    if "昨天" in s or "yesterday" in s:
        consumed = "昨天" if "昨天" in s else "yesterday"
        return today - timedelta(days=1), consumed
    if "今天" in s or "today" in s:
        consumed = "今天" if "今天" in s else "today"
        return today, consumed
    if "上周" in s or "last week" in s:
        consumed = "上周" if "上周" in s else "last week"
        return today - timedelta(days=7), consumed

    m = re.search(r"(\d+)\s*(天|day|days)\s*(前|ago)", s)
    if m:
        n = int(m.group(1))
        return today - timedelta(days=n), m.group(0)

    return None, ""


@dataclass
class QoderSessionRecord:
    session_id: str
    session_uid: str
    tool: str
    title: str
    workspace: str
    is_archived: bool
    model: str
    created_at: str
    updated_at: str
    turns: int
    first_prompt: str
    jsonl_path: Optional[str] = None
    state_path: Optional[str] = None
    created_at_ms: int = 0
    updated_at_ms: int = 0

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d.pop("created_at_ms", None)
        d.pop("updated_at_ms", None)
        return d


def resolve_qoder_paths(
    qoder_home_arg: Optional[str] = None,
    appdata_arg: Optional[str] = None,
) -> Tuple[Path, Path, Path]:
    """返回 (qoder_home, main_sqlite, projects_dir)"""
    home = Path(qoder_home_arg).expanduser().resolve() if qoder_home_arg else (Path.home() / ".qoder").resolve()
    
    if appdata_arg:
        appdata_dir = Path(appdata_arg).expanduser().resolve()
    else:
        appdata_env = os.environ.get("APPDATA")
        if appdata_env:
            appdata_dir = Path(appdata_env) / "com.qoder.app.stable"
        else:
            appdata_dir = Path.home() / "AppData" / "Roaming" / "com.qoder.app.stable"
            
    sqlite_path = appdata_dir / "main.sqlite"
    projects_dir = home / "projects"
    return home, sqlite_path, projects_dir


def extract_prompt_from_jsonl(jsonl_path: Path) -> Tuple[str, int]:
    """读取首个真实用户 prompt 及用户交互总轮数（过滤底层 tool_result 反馈）"""
    if not jsonl_path.is_file():
        return "", 0
    first_prompt = ""
    human_turns = 0
    try:
        with open(jsonl_path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                if not line.strip():
                    continue
                try:
                    obj = json.loads(line)
                except Exception:
                    continue
                if obj.get("type") == "user":
                    origin = obj.get("origin")
                    human = obj.get("humanInput")
                    msg = obj.get("message", {})
                    content = msg.get("content", [])

                    # 检查是否仅为工具执行结果反哺 (tool_result)
                    is_tool_res = False
                    if isinstance(content, list):
                        for c in content:
                            if isinstance(c, dict) and c.get("type") == "tool_result":
                                is_tool_res = True
                                break

                    if is_tool_res:
                        continue

                    # 只要不是 tool_result，且具有 human 标记或文本内容
                    text = ""
                    if human and isinstance(human, dict) and human.get("text"):
                        text = human.get("text")
                    elif isinstance(content, list):
                        for p in content:
                            if isinstance(p, dict) and p.get("type") == "text":
                                text += p.get("text", "")
                    elif isinstance(content, str):
                        text = content

                    text = text.strip()
                    if text:
                        human_turns += 1
                        if not first_prompt:
                            first_prompt = text
    except Exception:
        pass
    return first_prompt, human_turns


def query_from_sqlite(
    sqlite_path: Path,
    projects_dir: Path,
    workspace_kw: Optional[str] = None,
    title_kw: Optional[str] = None,
    archived_mode: str = "all",  # all | archived_only | active_only
    window: Optional[TimeWindow] = None,
) -> List[QoderSessionRecord]:
    if not sqlite_path.is_file():
        return []

    # 建立 jsonl 文件快速索引 map: session_id -> jsonl_path
    jsonl_map: Dict[str, Path] = {}
    if projects_dir.is_dir():
        for p in projects_dir.glob("*/*.jsonl"):
            jsonl_map[p.stem] = p

    # 只读打开 sqlite
    uri = f"file:{sqlite_path.as_posix()}?mode=ro"
    records: List[QoderSessionRecord] = []
    try:
        conn = sqlite3.connect(uri, uri=True)
        cur = conn.cursor()

        sql = """
            SELECT 
                session_id, 
                title, 
                cwd, 
                model, 
                archived, 
                created_at, 
                updated_at,
                deleted_at
            FROM chat_sessions
            WHERE deleted_at IS NULL
        """
        params: List[Any] = []

        if archived_mode == "archived_only":
            sql += " AND archived = 1"
        elif archived_mode == "active_only":
            sql += " AND archived = 0"

        if workspace_kw:
            sql += " AND LOWER(cwd) LIKE ?"
            params.append(f"%{workspace_kw.lower()}%")

        if title_kw:
            sql += " AND LOWER(title) LIKE ?"
            params.append(f"%{title_kw.lower()}%")

        if window:
            start_ms = int(window.start_utc.timestamp() * 1000)
            end_ms = int(window.end_utc.timestamp() * 1000)
            sql += " AND updated_at >= ? AND updated_at <= ?"
            params.extend([start_ms, end_ms])

        sql += " ORDER BY updated_at DESC"

        cur.execute(sql, params)
        rows = cur.fetchall()
        conn.close()

        for sid, title, cwd, model, archived, c_at, u_at, _ in rows:
            jsonl_p = jsonl_map.get(sid)
            first_prompt, turns = extract_prompt_from_jsonl(jsonl_p) if jsonl_p else ("", 0)
            
            c_dt = datetime.fromtimestamp(c_at / 1000, tz=timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M:%S")
            u_dt = datetime.fromtimestamp(u_at / 1000, tz=timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M:%S")
            state_p = jsonl_p.parent / sid / "state.json" if jsonl_p else None

            records.append(
                QoderSessionRecord(
                    session_id=sid,
                    session_uid=f"qoder_{sid}",
                    tool="qoder",
                    title=title or "(无标题)",
                    workspace=cwd or "",
                    is_archived=bool(archived),
                    model=model or "",
                    created_at=c_dt,
                    updated_at=u_dt,
                    turns=turns,
                    first_prompt=first_prompt,
                    jsonl_path=str(jsonl_p) if jsonl_p else None,
                    state_path=str(state_p) if (state_p and state_p.is_file()) else None,
                    created_at_ms=c_at or 0,
                    updated_at_ms=u_at or 0,
                )
            )
    except Exception as e:
        sys.stderr.write(f"[WARN] SQLite 读取失败或锁定: {e}\n")

    return records


def scan_from_filesystem(
    projects_dir: Path,
    workspace_kw: Optional[str] = None,
    title_kw: Optional[str] = None,
    archived_mode: str = "all",
    window: Optional[TimeWindow] = None,
) -> List[QoderSessionRecord]:
    if not projects_dir.is_dir():
        return []

    records: List[QoderSessionRecord] = []
    for jsonl_p in projects_dir.glob("*/*.jsonl"):
        sid = jsonl_p.stem
        cwd = ""
        model = ""
        c_at_str = ""
        u_at_str = ""
        c_at_ms = 0
        u_at_ms = 0
        
        # 读首行 workspace
        try:
            with open(jsonl_p, "r", encoding="utf-8", errors="replace") as f:
                first_line = f.readline()
                if first_line:
                    obj1 = json.loads(first_line)
                    if obj1.get("type") == "workspace-directories":
                        dirs = obj1.get("directories", [])
                        if dirs:
                            cwd = dirs[0]
                second_line = f.readline()
                if second_line:
                    obj2 = json.loads(second_line)
                    if obj2.get("type") == "runtime-config":
                        model = obj2.get("model", "")
        except Exception:
            pass

        # 过滤 workspace
        if workspace_kw and workspace_kw.lower() not in cwd.lower():
            continue

        # 读 state.json 获取时间戳
        state_p = jsonl_p.parent / sid / "state.json"
        if state_p.is_file():
            try:
                with open(state_p, "r", encoding="utf-8") as sf:
                    s_data = json.load(sf)
                    c_iso = s_data.get("createdAt")
                    u_iso = s_data.get("updatedAt")
                    if c_iso:
                        c_dt = datetime.fromisoformat(c_iso.replace("Z", "+00:00"))
                        c_at_ms = int(c_dt.timestamp() * 1000)
                        c_at_str = c_dt.astimezone().strftime("%Y-%m-%d %H:%M:%S")
                    if u_iso:
                        u_dt = datetime.fromisoformat(u_iso.replace("Z", "+00:00"))
                        u_at_ms = int(u_dt.timestamp() * 1000)
                        u_at_str = u_dt.astimezone().strftime("%Y-%m-%d %H:%M:%S")
            except Exception:
                pass

        if not u_at_str:
            # fallback mtime
            mtime = jsonl_p.stat().st_mtime
            u_at_ms = int(mtime * 1000)
            u_at_str = datetime.fromtimestamp(mtime).astimezone().strftime("%Y-%m-%d %H:%M:%S")
            c_at_str = u_at_str

        # 时间窗过滤
        if window:
            u_dt = datetime.fromtimestamp(u_at_ms / 1000, tz=timezone.utc)
            if not (window.start_utc <= u_dt <= window.end_utc):
                continue

        first_prompt, turns = extract_prompt_from_jsonl(jsonl_p)
        title = first_prompt[:30].replace("\n", " ") if first_prompt else "(无标题)"

        if title_kw and title_kw.lower() not in title.lower():
            continue

        records.append(
            QoderSessionRecord(
                session_id=sid,
                session_uid=f"qoder_{sid}",
                tool="qoder",
                title=title,
                workspace=cwd,
                is_archived=False,  # 文件系统无法直接知晓 archived，默认 false
                model=model,
                created_at=c_at_str,
                updated_at=u_at_str,
                turns=turns,
                first_prompt=first_prompt,
                jsonl_path=str(jsonl_p),
                state_path=str(state_p) if state_p.is_file() else None,
                created_at_ms=c_at_ms,
                updated_at_ms=u_at_ms,
            )
        )

    records.sort(key=lambda r: r.updated_at_ms, reverse=True)
    return records


def filter_by_keyword(records: List[QoderSessionRecord], kw: str) -> List[QoderSessionRecord]:
    kw_l = kw.lower()
    matched = []
    for r in records:
        if kw_l in r.title.lower() or kw_l in r.first_prompt.lower():
            matched.append(r)
            continue
        # 穿透检索 jsonl
        if r.jsonl_path and Path(r.jsonl_path).is_file():
            found = False
            try:
                with open(r.jsonl_path, "r", encoding="utf-8", errors="replace") as f:
                    for line in f:
                        if kw_l in line.lower():
                            matched.append(r)
                            found = True
                            break
            except Exception:
                pass
            if found:
                continue
    return matched


def render_table(records: List[QoderSessionRecord]) -> str:
    if not records:
        return "未找到匹配的 Qoder 会话。"

    lines = [
        f"{'#':<3} {'Session ID':<38} {'状态':<6} {'轮数':<4} {'更新时间':<19} {'标题与工作区'}",
        "-" * 105,
    ]
    for i, r in enumerate(records, 1):
        status = "[归档]" if r.is_archived else "[活跃]"
        t = (r.title[:24] + "...") if len(r.title) > 24 else r.title
        ws = (r.workspace[:32] + "...") if len(r.workspace) > 32 else r.workspace
        lines.append(f"{i:<3} {r.session_id:<38} {status:<6} {r.turns:<4} {r.updated_at:<19} {t} ({ws})")
    return "\n".join(lines)


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="locate_session.py",
        description="按工作区、时间窗、归档状态及关键词定位 Qoder 会话",
    )
    p.add_argument("--workspace", "-w", help="项目工作区路径或关键词（模糊匹配，支持中文）")
    p.add_argument("--time", "-t", help="自然语言时间提示，如 'yesterday', '上周', 'today', '2026-10-23'")
    p.add_argument("--after", help="时间窗起点 (ISO 格式)，如 2026-10-01")
    p.add_argument("--before", help="时间窗终点 (ISO 格式)，如 2026-10-31")
    p.add_argument("--title", help="按会话标题过滤（模糊匹配）")
    p.add_argument("--keyword", "-k", help="按对话全文关键词过滤")
    p.add_argument("--archived-only", action="store_true", help="仅检索已归档会话")
    p.add_argument("--active-only", action="store_true", help="仅检索未归档活跃会话")
    p.add_argument("--qoder-home", help="自定义 Qoder 主目录路径（默认 ~/.qoder）")
    p.add_argument("--appdata", help="自定义 Qoder AppData 路径（默认 %APPDATA%/com.qoder.app.stable）")
    p.add_argument("--limit", "-n", type=int, default=DEFAULT_LIMIT, help=f"返回上限，默认 {DEFAULT_LIMIT}")
    p.add_argument("--json", action="store_true", help="输出结构化 JSON 格式")
    return p


def main(argv: Optional[Iterable[str]] = None) -> int:
    args = build_arg_parser().parse_args(argv)

    window = None
    try:
        if args.time:
            window = parse_time_hint(args.time)
        elif args.after or args.before:
            now = datetime.now().astimezone()
            after = (
                datetime.fromisoformat(args.after).replace(tzinfo=now.tzinfo)
                if args.after
                else now - timedelta(days=MAX_WINDOW_DAYS)
            )
            before = (
                datetime.fromisoformat(args.before).replace(tzinfo=now.tzinfo)
                if args.before
                else now
            )
            window = TimeWindow(
                after.astimezone(timezone.utc),
                before.astimezone(timezone.utc),
                f"{args.after or ''} ~ {args.before or ''}",
            )
    except ValueError as e:
        print(f"错误: 时间解析失败: {e}", file=sys.stderr)
        return 2

    if window and window.width_days() > MAX_WINDOW_DAYS:
        print(
            f"错误: 时间窗跨度 {window.width_days():.1f} 天超过 {MAX_WINDOW_DAYS} 天上限，请缩窄窗口",
            file=sys.stderr,
        )
        return 2

    arch_mode = "all"
    if args.archived_only:
        arch_mode = "archived_only"
    elif args.active_only:
        arch_mode = "active_only"

    qoder_home, sqlite_path, projects_dir = resolve_qoder_paths(args.qoder_home, args.appdata)

    # 优先查 SQLite，失败则回退查文件系统
    records = query_from_sqlite(
        sqlite_path=sqlite_path,
        projects_dir=projects_dir,
        workspace_kw=args.workspace,
        title_kw=args.title,
        archived_mode=arch_mode,
        window=window,
    )
    if not records and not args.archived_only:
        # 如果 SQLite 没找到且不是强制仅已归档，回退文件系统遍历
        records = scan_from_filesystem(
            projects_dir=projects_dir,
            workspace_kw=args.workspace,
            title_kw=args.title,
            archived_mode=arch_mode,
            window=window,
        )

    if args.keyword:
        records = filter_by_keyword(records, args.keyword)

    records = records[: args.limit]

    if args.json:
        payload = {
            "qoder_home": str(qoder_home),
            "query": {
                "workspace_keyword": args.workspace,
                "time_window": {
                    "start_utc": window.start_utc.isoformat() if window else None,
                    "end_utc": window.end_utc.isoformat() if window else None,
                    "parsed_from": window.parsed_from if window else None,
                },
                "archived_mode": arch_mode,
                "title_keyword": args.title,
                "keyword": args.keyword,
            },
            "matches": [r.to_dict() for r in records],
            "match_count": len(records),
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(render_table(records))

    return 0


if __name__ == "__main__":
    sys.exit(main())
