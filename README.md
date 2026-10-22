# Mnevo

> 会睡觉的文件柜 — The filing cabinet that sleeps.

**Mnevo 是一个活的个人记忆层（living memory layer）**：管家（巩固/代谢）+ 身份层 + 记忆包 + 各 Agent 习惯适配（adapters），架在**可插拔的记忆引擎**之上。引擎永远只经 MCP/REST 调用，绝不 import 内部——引擎可换，记忆不换。

*(English)* Mnevo is a self-hosted, personal memory layer for AI agents. It is deliberately **not** another memory server: storage and retrieval are delegated to a pluggable engine (currently [mcp-memory-service](https://github.com/doobidoo/mcp-memory-service)). Mnevo adds the part the ecosystem is missing — **metabolism**: a butler that consolidates what agents record, an identity layer curated by hand, portable memory packs, and a habit book per agent.

## 为什么是"代谢"

积累不等于懂你，策展质量才等于懂你。当前开源记忆方案普遍 ADD-only（只积累、不合并、不遗忘，如 mem0 OSS v2.0 的提取即终点），"记录一切"会退化成死文档堆。Mnevo 的主角不是存储，而是代谢：

- **管家**：夜间/阈值触发的异步蒸馏——情景→语义、判重、身份层 diff 提案（人工确认）、逻辑遗忘，每一步落审计日志；
- **身份层**：persona / values / goals / active threads，markdown + git，机器只有提案权；
- **记忆包**：版本化导出/导入，跨引擎迁移与"用户拥有"的物理证据；
- **习惯书**：MCP 封装的是"手"，不封装"习惯"——每个 Agent 一页 habit book。

## 双宿主拓扑

| 区 | 宿主 | 说明 |
| --- | --- | --- |
| work | 雇主开发服务器 | 引擎只绑 127.0.0.1，SSH 隧道接入；工作数据留在公司基础设施内 |
| personal | 自有 VPS / 本地 | **永不落公司硬件**；age 加密备份到个人私有仓 |

两区之间唯一的桥梁是**记忆包**——它同时是离职逃生舱。

## 纪律（节选）

1. **经历/载荷分界**：记"做了什么、学到什么、卡在哪、东西在哪"；数据本体（财务文件、数字、客户信息）永不入脑，只写指针；
2. secrets / API key 任何 namespace 拒存；
3. 逻辑遗忘 = 召回与蒸馏双管道排除，hard-delete 兜底；
4. 身份层：机器只提案，人工确认。

完整设计见 **[docs/PLAN.md](docs/PLAN.md)**（v0.2 共识计划书，19 问拷打定稿）。

## 状态

v0.1 spike 进行中：**[docs/spike-runbook.md](docs/spike-runbook.md)** — 对 mcp-memory-service 做一周 consolidation 成色观察（代谢 or 膨胀？），结果决定管家 v0.1 的接管范围。

### 记忆包 v0.1（首个可运行组件）

```bash
python scripts/mnevo_pack.py export --out my.mempack --identity identity/live   # 引擎→包
python scripts/mnevo_pack.py verify --pack my.mempack --against-engine           # 完整性+对引擎
python scripts/mnevo_pack.py import --pack my.mempack --dry-run                  # 包→引擎（幂等）
```

格式：`manifest.json`（版本+每文件 sha256）+ `memories.jsonl`（内容真源，向量不入包，导入端重算）+ `identity/`（仅已确认文件）。测试：`pytest tests/`（引擎不可达时真机用例自动 skip）。

命名：Mnevo（希腊语 μνήμη 词根自造词，2026-10-14 定名；查证记录见 PLAN §14-3）。设计哲学：记忆 → 叙事自我 → 延展心智（PLAN §0.4）。

## License

Apache-2.0
