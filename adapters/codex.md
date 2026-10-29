# Codex 习惯书（work 区 · v0.1）

> 一页纸：教 Codex 怎么用好 Mnevo work 记忆引擎。MCP 封装"手"，这一页封装"习惯"。
> Codex 看不到 pi 的 MCP 工具——**REST 就是它的手**（`scripts/mnevo_rest.py`），
> AGENTS.md 就是它的注入面（会话自动读取）。

## 连接

- 引擎经 SSH 隧道在 `127.0.0.1:8000`（备胎引擎 B：`8100`，mem0，接口兼容）；token 在环境变量 `MNEVO_WORK_TOKEN`；
- 习惯 CLI：`python Mnevo/scripts/mnevo_rest.py store|search|recent --url … --token-env MNEVO_WORK_TOKEN`；
- 工具箱直连：`mnevo_pack`（包/校验）、`mnevo_butler`（提案确认面）、`mnevo_bootstrap`（冷启动注入）。

## 三件事

1. **会话启动**（接手跨会话任务时）：先注入身份层——
   `python Mnevo/scripts/mnevo_bootstrap.py build --identity Mnevo/identity/live`；
   再 recall：`mnevo_rest.py search "上次进展 <项目>"`。不要凭空重来；
2. **会话收尾**：把本段工作**经历**写入库——做了什么、决定与理由、教训、产物在哪（指针）。多条离散事实分开存；长文本走管道 stdin（免引号地狱）；
3. **写前自问**：这值得三个月后的我知道吗？任务垃圾（临时调试输出、可重建的中间态）不写。

## 写入纪律

- **经历/载荷分界（铁律）**：公司数据本体（表格内容、数字、客户信息）**永不入库**——写"在哪能找到"（路径/系统/负责人），不写内容本身；
- **少用跨条目复用的模板长句**：不同事实共享大段句式会在嵌入空间近重合（实测 0.92+），给判重/衰减挖坑；
- secrets / API key / token：任何情况不写（CLI 有客户端闸门，`--force` 须人工确认是占位符才可用）；
- 标注来源：`mnevo_rest.py store` 默认打 `agent:codex` 标签——管家需要知道记忆是谁的；
- namespace=work：个人生活内容不进 work 引擎。

## 期望行为

- 召回内容按"历史经验非当前事实"消费：与现状冲突时以现状为准，必要时写回纠正；
- 引擎不可达（隧道断）：照常干活，静默降级，不阻塞任务、不复制降级话术；
- 发现误合/漏合：记入 `observations/`（脱敏），作为管家迭代输入；
- AGENTS.md 的记忆习惯段是本页的工作区投影，两处同步改。
