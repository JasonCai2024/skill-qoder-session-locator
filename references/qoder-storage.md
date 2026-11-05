# Qoder 本地存储与会话架构技术内参

本文档记录 Qoder（版本 0.4.11+）在 Windows 平台下的存储拓扑、数据库模式与文件流格式，供底层定位与提取工具参考。

---

## 1. 存储拓扑总览

Qoder 采用双轨持久化（Dual-Storage Engine）设计：
1. **桌面应用层（Electron AppData）**：负责跨会话的全局索引、侧边栏管理、工作区绑定与归档标记。
   - 路径：`%APPDATA%\com.qoder.app.stable\` (通常为 `C:\Users\<user>\AppData\Roaming\com.qoder.app.stable`)
   - 核心文件：`main.sqlite`、`chat-session-turn-payload-buffer.sqlite`、`sessionMigration.sqlite`
2. **运行时引擎层（User Home）**：负责实际执行会话的事件日志流、快照与任务产物。
   - 路径：`%USERPROFILE%\.qoder\` (通常为 `C:\Users\<user>\.qoder`)
   - 核心目录：`projects/`、`file-history/`、`tasks/`、`logs/`

---

## 2. 桌面端 SQLite 数据库 (`main.sqlite`)

### 2.1 `chat_sessions` 表（核心会话表）

| 字段 | 类型 | 说明 |
| :--- | :--- | :--- |
| `session_id` | TEXT (PK) | 会话唯一 UUID（例如 `0a3b267e-860d-4091-8d71-43aa7a5c5a24`） |
| `title` | TEXT | 侧边栏展示标题（自定义或首轮提示词摘要） |
| `cwd` | TEXT | 会话工作区绝对路径（**原始真实路径**，如 `E:\BaiduSyncdisk\WorkSpace\数据标注`） |
| `workspace_id` | TEXT | 外键关联到 `workspaces.workspace_id` |
| `model` | TEXT | 调用的模型代号（如 `qfmodel`, `dfmodel`） |
| `archived` | INTEGER | 归档标记：`0` = 活跃会话，`1` = 已归档 |
| `deleted_at` | INTEGER / NULL | 软删除毫秒时间戳；若为 `NULL` 则表示会话未被删除 |
| `created_at` | INTEGER | 创建毫秒时间戳（Epoch ms） |
| `updated_at` | INTEGER | 最后活动/更新毫秒时间戳（Epoch ms） |
| `permission_mode` | TEXT | 权限模式（如 `auto`） |
| `conversation_mode`| TEXT | 会话模式（如 `normal`） |
| `product_mode` | TEXT | 产品模式（如 `coding`） |
| `execution_target_json`| TEXT | 执行目标配置（包含本地环境与工作区 ID） |
| `extra_json` | TEXT | 附加扩展元数据（包含来源、标题修订状态等） |

### 2.2 `workspaces` 表（工作区索引）

| 字段 | 类型 | 说明 |
| :--- | :--- | :--- |
| `workspace_id` | TEXT (PK) | 工作区 UUID |
| `name` | TEXT | 工作区名称（如 `"数据标注"`） |
| `root_paths_json` | TEXT | 根目录 JSON 数组（如 `["E:\\BaiduSyncdisk\\WorkSpace\\数据标注"]`） |
| `archived` | INTEGER | 归档标记 |
| `deleted_at` | INTEGER / NULL | 软删除时间戳 |

---

## 3. 运行时文件流 (`~/.qoder/projects/`)

### 3.1 项目目录转义规范 (Slugging Rule)
Qoder 将工作区绝对路径转换为目录名时，遵循以下替换规则：
* 驱动器冒号 `:` 替换为 `-`
* 路径分隔符 `\` 或 `/` 替换为 `-`
* 所有非 ASCII / 非字母数字字符（包含中文字符）逐字替换为 `-`
* 示例：`E:\BaiduSyncdisk\WorkSpace\数据标注` -> `E--BaiduSyncdisk-WorkSpace-----`

### 3.2 `<session_id>.jsonl` 事件流规范
每个会话独立为一个 Append-only 的 JSONL 文件：
1. **第 1 行（强制元数据）**：
   ```json
   {
     "type": "workspace-directories",
     "sessionId": "0a3b267e-860d-4091-8d71-43aa7a5c5a24",
     "directories": ["E:\\BaiduSyncdisk\\WorkSpace\\数据标注"]
   }
   ```
   **该行提供了绝对真实的原始未转义工作区目录，是无 SQLite 依赖时精准反查工作区的基石。**
2. **第 2 行（运行时配置）**：
   ```json
   {
     "type": "runtime-config",
     "sessionId": "...",
     "model": "qfmodel",
     "timestamp": 1792769060903
   }
   ```
3. **用户轮次事件 (`type: "user"`)**：
   ```json
   {
     "type": "user",
     "uuid": "85ab68fc-aa01-427d-a99e-5044b22a403b",
     "timestamp": "2026-10-23T15:24:21.095Z",
     "message": {
       "role": "user",
       "content": [
         {
           "type": "text",
           "text": "用户提示词原汁原味内容..."
         }
       ]
     },
     "cwd": "E:\\BaiduSyncdisk\\WorkSpace\\数据标注",
     "sessionId": "0a3b267e-860d-4091-8d71-43aa7a5c5a24"
   }
   ```
4. **模型轮次事件 (`type: "assistant"`)**：
   包含模型生成的文本、工具调用请求与响应。
5. **系统事件**：
   包含 `active-leaf`、`attachment`、`file-history-snapshot`、`last-prompt` 等辅助流。

### 3.3 `<session_id>/state.json`
记录会话修订版本与时间戳：
```json
{
  "sessionId": "0a3b267e-860d-4091-8d71-43aa7a5c5a24",
  "revision": 134,
  "createdAt": "2026-10-23T15:24:28.227Z",
  "updatedAt": "2026-10-24T02:44:48.922Z",
  "data": {},
  "items": { ... }
}
```

---

## 4. 辅助上下文产物

* **文件快照**：`~/.qoder/file-history/<session_id>/<fileUuid>@v<version>` 记录编辑前后版本。
* **任务产物**：`~/.qoder/tasks/<session_id>/` 记录会话派生的批处理或子任务状态。
