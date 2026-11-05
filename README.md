# Skill Qoder Session Locator

> **定位**：Qoder 本地会话的专用定位与轨迹读取技能。

---

## 一、 功能特点

1. **双轨秒级定位**：
   - 优先基于桌面端 SQLite 索引库（`main.sqlite`），秒级完成多条件过滤；
   - 支持文件系统流式回退模式，即便在无界面或数据库锁定时也能稳定工作。
2. **绝对真实路径还原**：
   - 绕过目录转义混淆，直接从 SQLite `cwd` 或 JSONL 首行提取 100% 真实的工作区路径。
3. **真实用户 Prompt 提纯**：
   - 智能识别 Claude 协议底层反哺的 `tool_result`，精准提炼真实开发者输入。
4. **标准化输出契约**：
   - 完美对接 `skill-unified-session-searcher` 与 `agent-prompt-pattern-extractor`。

---

## 二、 常用命令

```bash
# 1. 按工作区名称模糊搜索
python scripts/locate_session.py -w "数据标注"

# 2. 按自然语言时间窗过滤
python scripts/locate_session.py -t "yesterday"

# 3. 仅查找已归档会话并输出 JSON
python scripts/locate_session.py --archived-only --json

# 4. 读取会话概览与轮次摘要
python scripts/read_session.py -s "c1f29a54-5be4-4163-a106-c2b26992eeaf" --summary

# 5. 查看指定轮次的完整交互详情
python scripts/read_session.py -s "c1f29a54-5be4-4163-a106-c2b26992eeaf" --turn 1
```
