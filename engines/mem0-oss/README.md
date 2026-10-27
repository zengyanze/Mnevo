# mem0 OSS（二号引擎 / engine B）

> v0.2-4：同 pack 双引擎往返验证可插拔。`server.py` 是**引擎侧部署胶水**，
> 暴露 mcp-memory-service 兼容 API 子集——Mnevo 核心（pack/butler）换引擎=换 URL。

## 形态

- **mem0ai 2.2.1**（隔离 venv，pin 见 `requirements.pin.txt`）+ **fastembed** 本地 ONNX 嵌入（`BAAI/bge-small-zh-v1.5`，512 维）+ qdrant 本地模式；
- `infer=False` 原样存取：不做 LLM 提取（ADD-only 是 mem0 OSS 本相），零 LLM 调用、零 API key；
- 哈希口径与引擎 A 对齐：`sha256(content.strip().lower())` → `verify --against-engine` 哈希集合可直接比对；
- tags/谱系经 `metadata.mnevo_*` 往返；遥测关闭（`MEM0_TELEMETRY=false`）。

## 部署（Linux）

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh        # 无 uv 时
mkdir -p ~/.mnevo/engines/mem0-oss && cd ~/.mnevo/engines/mem0-oss
uv venv --python 3.12 venv
uv pip install --python venv/bin/python -r requirements.pin.txt
# 把 server.py 放进本目录，然后：
source ~/.mnevo/env 2>/dev/null || true    # 复用 MCP_API_KEY 做 Bearer（或自设 MNEVO_MEM0_TOKEN）
source venv/bin/activate
MEM0_TELEMETRY=false python server.py      # 127.0.0.1:8100
```

客户端经隧道访问：`ssh -N -L 8100:127.0.0.1:8100 <host>`，
然后 `mnevo_pack.py … --url http://127.0.0.1:8100`（token 同引擎 A）。

## 已验证（2026-10-27）

- 同包（10 条）引擎 A→B import：`verify` **missing=0 extra=0**；B 导出与原包 content/tags/memory_type **逐行零差异**（created_at 重灌，与引擎 A 导入行为一致）；
- 中文语义检索正常（bge-small-zh）；`mnevo_butler.py scan` 换 URL 即用（0 提案）；
- 测试：`tests/test_mem0_shim.py`（离线映射 3 + 活引擎 1，不可达自动 skip）。

## 已知边界

- mem0 写入**无语义查重**（ADD-only）——重复导入不会被拒，幂等靠调用方自律（`verify` 可查）；
- PUT（降权）实现为删旧存新：mem0 内部 id 会变，内容与 sha256 不变；
- Windows 本机若拦 onnxruntime/torch 原生 DLL（Defender/策略，实测会拦）→ 部署在 Linux 侧；
- zai `embedding-3` 现有 key 配额不足（code 1113）→ 嵌入本地化反而是长期正解。
