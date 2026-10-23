# WorkBuddy 习惯书 v0.1（草案，接入验证后转正）

> WorkBuddy = 雇主侧 agent。**只接 work 区**（PLAN §8 反渗透：雇主日志可见，personal 永不接入雇主可及的任何系统）。

## 连接配置

- MCP 端点：`http://127.0.0.1:8000/mcp`（**本机 SSH 隧道**，不是服务器直连——§14-6 的更省解：WorkBuddy 与 pi 同机，共用隧道即可，服务器无需暴露任何端口，OAuth 延后）
- 鉴权：Bearer token（环境变量 `MNEVO_WORK_TOKEN`，值与 pi 侧相同，已在系统级设置）
- 隧道依赖：`mnevo-tunnel.cmd`（登录自启 + 断线自愈）。隧道断则记忆工具不可见——**静默降级**，不阻塞任务

## 读写纪律（与 pi.md 同源，不重复展开）

1. **写**：阶段性完成/学到/卡住/东西在哪+指针，1~2 条，tags 带 `["work", "<项目>"]`；**禁入**：数据本体、客户信息、secrets、数字载荷
2. **读**：接续性工作先 search 召回，召回内容按"历史经验"消费（非当前事实），冲突以现状为准
3. **不解释记忆系统本身**——用户要的是接得住上下文，不是记忆科普

## 与 pi 的分工

- pi（开发者侧）：日常主力，记忆写入量主要来源
- WorkBuddy（业务侧）：工作流内的上下文延续；其写的记忆 pi 侧可读（tags 区分来源 agent）

## 待验证（接入时勾掉）

- [ ] WorkBuddy 自定义 MCP 配置界面支持 Bearer token header
- [ ] 隧道运行时 WorkBuddy 内工具可见、读写正常
- [ ] X-Agent-ID 等价物（WorkBuddy 侧的 agent 标识，tags 里补 `agent:workbuddy`）
