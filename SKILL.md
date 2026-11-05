---
name: skill-qoder-session-locator
description: 定位 Qoder 本地会话。支持按工作区目录(CWD/Workspace)、自然语言时间窗、归档状态(Archived/Active)、会话标题与对话关键词过滤，并极速提取桌面端 SQLite 索引(chat_sessions)与完整交互事件轨迹(jsonl)。当用户通过项目路径、时间或关键词查找 Qoder 会话时使用。触发话术包括 找Qoder会话, 定位Qoder历史, resume Qoder session, 查找Qoder会话.
disable-model-invocation: true
user-invocable: true
argument-hint: [workspace-path, time-hint]
---

# Skill Qoder Session Locator

## Goal

根据用户提供的工作区路径（Workspace / CWD）、更新时间提示、归档状态或对话关键词，快速定位 Qoder 本地会话，秒级读取桌面端 SQLite 索引元数据（`main.sqlite`），并按需直接流式读取底层会话事件轨迹（`<session_id>.jsonl`），供跨智能体接力、历史复盘、断点续跑或提示词模式提炼使用。

## Required Inputs

| 参数 | 类型 | 必填 | 说明 |
| :--- | :--- | :--- | :--- |
| `workspace` | 路径字符串 | 否* | 会话绑定的工作区目录或子目录关键词（如 `数据标注`、`ForAgent`、`LocalHub`）；大小写不敏感模糊匹配，自动处理中文字符 |
| `time_hint` | 时间字符串 | 否* | 会话更新时间提示，支持自然语言（"昨天" / "yesterday" / "上周" / "3天前"）或 ISO 格式（"2026-10-23"） |
| `title` | 字符串 | 否 | 会话标题关键词过滤 |
| `keyword` | 字符串 | 否 | 对话交互关键词过滤（优先查首轮提示词与标题，按需穿透 jsonl 全文检索） |
| `archived_mode` | 枚举 | 否 | `all`（默认，全部）、`archived_only`（仅已归档）、`active_only`（仅活跃未归档） |
| `qoder_home` | 路径 | 否 | Qoder 主数据目录，覆盖 `QODER_HOME`；默认 `~/.qoder/` |
| `limit` | 整数 | 否 | 候选返回上限，默认 `10` |

> \*注：`workspace`、`time_hint`、`title`、`keyword` 中至少提供一个过滤条件；若全不提供，默认返回按最新更新时间排序的前 10 个会话。

## Workflow

### Step 1：解析 Qoder 根路径与数据库
1. 读取 `--qoder-home` 参数或环境变量 `QODER_HOME`，默认 `~/.qoder`。
2. 读取桌面端 SQLite 数据库 `%APPDATA%\com.qoder.app.stable\main.sqlite`。
3. 验证 `projects/` 目录存在性。

### Step 2：解析时间提示为窗口
1. 若用户提供了自然语言时间提示，调用 `scripts/locate_session.py --time "<hint>"` 解析为 `(start_utc, end_utc)`。
2. 时间窗跨度超过 30 天时报错，引导用户缩窄范围。

### Step 3：双轨扫描定位会话
1. **优先轨 (SQLite)**：以只读模式连接 `main.sqlite`，查询 `chat_sessions` 表与 `workspaces` 表，过滤 `deleted_at IS NULL`，提取 `session_id`、`cwd`、`title`、`model`、`archived`、`updated_at`。
2. **回退轨 (文件系统)**：若 SQLite 锁定或缺失，直接遍历 `projects/*/*.jsonl`，读取第 1 行解析真实 `directories`。
3. 关联 `projects/*/<session_id>.jsonl` 提取首轮真实用户 Prompt 与交互轮数（智能剔除内部 `tool_result` 回传）。
4. 若指定了 `--keyword`，穿透检索对话内容。

### Step 4：候选判定与用户决策
1. **1 个候选**：直接进入 Step 5 读取详情。
2. **多个候选**：展示会话列表（包含 Session ID、状态、轮次、更新时间、标题及工作区），由用户确认。
3. **0 个候选**：返回 0 匹配；输出本地已发现的工作区候选列表供用户纠偏。

### Step 5：提取会话轨迹与产物
1. 调用 `scripts/read_session.py --session-id <id> --summary` 输出关键元数据、首轮 Prompt 与各轮交互摘要。
2. 若用户需要复现具体对话，调用 `scripts/read_session.py --session-id <id> --turn <N>`。

## Decision Rules

1. **真实路径还原规则**：Qoder 在物理目录名中将冒号、斜杠与非 ASCII 字符替换为 `-`。定位器通过读取 `chat_sessions.cwd` 或 `.jsonl` 首行 `workspace-directories`，直接获取 100% 原始真实工作区路径，无需脆弱的逆向反猜。
2. **真假用户轮次判定**：Qoder 基于 Claude 协议将底层工具执行结果（`tool_result`）包装为 `role: "user"`。定位器严格识别 `origin={'kind': 'human'}` 或 `humanInput`，并将 `tool_result` 归属为当前轮次的工具反馈，确保用户 Prompt 轮数统计精准。
3. **归档状态判定规则**：以 `main.sqlite` 中的 `chat_sessions.archived` 字段为准（`1` 为已归档，`0` 为活跃）。

## Output Requirements

### 定位命令输出 (`scripts/locate_session.py --json`)
```json
{
  "qoder_home": "C:\\Users\\pc\\.qoder",
  "query": {
    "workspace_keyword": "数据标注",
    "time_window": {
      "start_utc": null,
      "end_utc": null,
      "parsed_from": null
    },
    "archived_mode": "all",
    "title_keyword": null,
    "keyword": null
  },
  "matches": [
    {
      "session_id": "c1f29a54-5be4-4163-a106-c2b26992eeaf",
      "session_uid": "qoder_c1f29a54-5be4-4163-a106-c2b26992eeaf",
      "tool": "qoder",
      "title": "5",
      "workspace": "E:\\BaiduSyncdisk\\WorkSpace\\数据标注",
      "is_archived": false,
      "model": "dfmodel",
      "created_at": "2026-10-23 23:11:07",
      "updated_at": "2026-10-24 10:58:50",
      "turns": 9,
      "first_prompt": "当前题目目录为：\n- E:\\BaiduSyncdisk\\WorkSpace\\数据标注\\Xpert\\...",
      "jsonl_path": "C:\\Users\\pc\\.qoder\\projects\\E--BaiduSyncdisk-WorkSpace-----\\c1f29a54-5be4-4163-a106-c2b26992eeaf.jsonl",
      "state_path": "C:\\Users\\pc\\.qoder\\projects\\E--BaiduSyncdisk-WorkSpace-----\\c1f29a54-5be4-4163-a106-c2b26992eeaf\\state.json"
    }
  ],
  "match_count": 1
}
```

### 读取命令输出 (`scripts/read_session.py --summary`)
输出会话 ID、工作区、模型、创建与更新时间、真实交互总轮数、首轮 Prompt 摘要以及每轮的工具调用计数与用户指令摘要。
