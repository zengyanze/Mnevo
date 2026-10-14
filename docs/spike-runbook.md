# Spike Runbook：mcp-memory-service 成色观察（v0.1 任务 1）

> **目标**：一周真实使用后回答三个问题，决定 Mnevo 管家的接管范围——
> ① 内置 consolidation 是**代谢**（合并/去重/衰减）还是**膨胀**（派生新增）？
> ② 检索质量是否够日常驱动（不靠人工翻 recent）？
> ③ localhost + 隧道 + token 形态是否稳定？OAuth 何时才真正需要？
>
> **环境**：公司开发服务器（ssh 免密已配）· work 区 · 预计 30–60 分钟搭好，之后每天 5 分钟观察。

## 1. 服务器端安装

在开发服务器上（Python ≥ 3.10）：

```bash
python3 -m venv ~/.mnevo/venv && source ~/.mnevo/venv/bin/activate
pip install mcp-memory-service        # 或 uv pip install mcp-memory-service
```

Docker 备选：`docker pull doobidoo/mcp-memory-service`（注意把数据目录和 HF 模型缓存挂 volume，否则每次重建重新下载 embedding 模型）。

## 2. 配置与启动

`~/.mnevo/env`（`chmod 600`，不进 git）：

```bash
export MCP_MEMORY_STORAGE_BACKEND=sqlite_vec      # 零配置本地向量库
export MCP_HTTP_ENABLED=true
export MCP_API_KEY="$(openssl rand -base64 32)"   # Bearer token，记下来
export MCP_CONSOLIDATION_ENABLED=true             # 本 spike 的观察对象
export MCP_CONSOLIDATION_QUALITY_BOOST_ENABLED=true   # v8.47+，可选
```

启动（默认只绑 `127.0.0.1:8000`，符合"不暴露"原则）：

```bash
source ~/.mnevo/env
memory launch                # 后台启动
memory info                  # 状态/健康检查
memory logs --lines 50       # 日志
memory stop                  # 停止
```

内置 consolidation 调度（默认）：**daily 02:00 / weekly SUN 03:00 / monthly 1st 04:00**。

## 3. 工作站侧：SSH 隧道

```bash
ssh -N -L 8000:127.0.0.1:8000 <你的服务器别名>
```

建议写进 `~/.ssh/config` 对应 Host 的 `LocalForward 8000 127.0.0.1:8000`，随登录自动带起。

## 4. 冒烟（隧道侧 curl）

```bash
curl -H "Authorization: Bearer $MCP_API_KEY" http://127.0.0.1:8000/api/health

curl -X POST http://127.0.0.1:8000/api/memories \
  -H "Authorization: Bearer $MCP_API_KEY" -H "Content-Type: application/json" \
  -H "X-Agent-ID: pi" \
  -d '{"content":"spike: 第一条冒烟记忆","tags":["spike"]}'

curl -X POST http://127.0.0.1:8000/api/search \
  -H "Authorization: Bearer $MCP_API_KEY" -H "Content-Type: application/json" \
  -d '{"query":"冒烟"}'
```

端点各版本略有差异，以 `http://127.0.0.1:8000/api/docs` 的实时列表为准。

## 5. pi 接入

```bash
export MNEVO_WORK_TOKEN=<MCP_API_KEY 的值>    # 放进 shell profile
pi mcp add mnevo-work --url http://127.0.0.1:8000/mcp --bearer-token-env-var MNEVO_WORK_TOKEN
pi mcp list                                   # 应显示 connected + 工具列表
```

会话内 `/mcp` 检查连接与工具；习惯约束见 `adapters/pi.md`。REST 写入带 `X-Agent-ID: pi`（MCP 工具走参数/标签），便于按 agent 溯源。

## 6. 一周观察协议（每天 5 分钟）

在 `observations/2026-Wxx.md`（不入库）逐日记录：

| 日期 | 库存条数 | consolidation 跑了吗（时刻/jobs_executed） | 新增 vs 合并 | 误合/漏合样例 | 检索抽查（query→相关？） | 异常 |
| --- | --- | --- | --- | --- | --- | --- |
|  |  |  |  |  |  |  |

判读要点：

- **膨胀信号**：条数单调上涨、派生"关联记忆"质量低。官方 2025-12 生产实测：5308 条库一天 +349 条——重点核实这些新增是否值得留；
- **代谢信号**：有无自动 merge/dedupe？还是只有手动 maintenance 脚本（`scripts/maintenance/fast_cleanup_duplicates.sh`、`consolidate_memory_types.py`——注意 `--dry-run` 先行）；
- 手动触发验证：

```bash
curl -X POST http://127.0.0.1:8000/api/consolidation/trigger \
  -H "Authorization: Bearer $MCP_API_KEY" -H "Content-Type: application/json" \
  -d '{"time_horizon":"daily","immediate":true}'
```

- 周末汇总三问答案 → `observations/spike-verdict.md`，据此定管家 v0.1 接管清单（合并/判重/身份层提案哪些自己写）。

## 7. WorkBuddy 延伸（暂缓——先查 PLAN §14-6 可达性）

WorkBuddy 需要可达的 HTTPS 端点（SaaS 出网 or 内网直连，待查证）。方案草案：TLS 反代终止 + `MCP_HTTPS_ENABLED=true` + OAuth 2.1（`MCP_OAUTH_ENABLED=true`，服务端已内置动态客户端注册 RFC 7591）。**查证前不暴露任何端口。**

## 8. 安全与回滚

- 永不设 `MCP_HTTP_HOST=0.0.0.0` / `--host 0.0.0.0`（除非走第 7 节且有完整认证）；
- token / 密钥不进 git、不进聊天记录、不进 issue；
- 依赖 pin 版本：2026-09 MemTensor（MemOS/OpenClaw 记忆工具链）npm/PyPI 遭供应链投毒（sckit 窃密木马）——升级前核对 release 说明，隔离环境试跑；
- 回滚 = `memory stop` + 删除 venv 与数据文件（SQLite 单文件，天然易迁易删）。

## 参考

- 官方仓库：<https://github.com/doobidoo/mcp-memory-service>（wiki：Installation Guide / OAuth 2.1 Setup / Memory Consolidation System Guide；Codeberg 同步镜像）
- 设计决策：`docs/PLAN.md` §7（管家）、§9.0（双宿主拓扑）
