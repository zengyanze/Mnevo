/**
 * mnevo-session-write — pi 会话收尾记忆钩子（Mnevo v0.1 判读第二发现的机制性解）
 *
 * 行为：agent 结算（settle）时，若本段工作 ≥MIN_TOOL_CALLS 次工具调用且未写过
 * mcp__mnevo-work__memory_store，则注入一条收尾提醒并请求最后一次续跑。
 * 作用域：仅当 cwd 上溯的 AGENTS.md 含 Mnevo 习惯标记（"mnevo-work__memory_store"）。
 * 安装：cp 到 ~/.pi/agent/extensions/（用户级，所有会话加载，非标记目录静默）。
 */
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import * as fs from "node:fs";
import * as path from "node:path";

const STORE_TOOL = "mcp__mnevo-work__memory_store";
const MARKER = "mnevo-work__memory_store";
const MIN_TOOL_CALLS = 3;

function habitInstalled(cwd: string): boolean {
  let dir = path.resolve(cwd);
  for (let i = 0; i < 6; i++) {
    const f = path.join(dir, "AGENTS.md");
    try {
      if (fs.existsSync(f) && fs.readFileSync(f, "utf8").includes(MARKER)) return true;
    } catch {
      /* unreadable — treat as absent */
    }
    const parent = path.dirname(dir);
    if (parent === dir) break;
    dir = parent;
  }
  return false;
}

export default function (pi: ExtensionAPI) {
  const active = habitInstalled(process.cwd());
  if (!active) return;

  let toolCalls = 0;
  let stored = false;

  pi.on("tool_execution_start", (e) => {
    if (e.toolName === STORE_TOOL) stored = true;
    else toolCalls++;
  });

  pi.on("agent_before_settle", (e) => {
    if (stored || toolCalls < MIN_TOOL_CALLS) return {};
    if (e.continue) return {}; // 已有续跑排队，等下一次结算
    toolCalls = 0; // 自复位：再来一波工作才可能再提醒
    return {
      continue: true,
      entries: [
        ...e.entries,
        {
          type: "custom_message",
          customType: "mnevo_session_write",
          content:
            "【Mnevo 收尾提醒】本段工作已有实际产出但尚未写记忆。请按本工作区 AGENTS.md 的 Mnevo 记忆习惯收尾：调用 mcp__mnevo-work__memory_store 写 1~2 条经历类记忆（做了什么/学到什么/卡在哪/东西在哪+指针；tags 带项目名；数据本体/客户信息/secrets/数字载荷禁入）。无可记内容或工具不可见则静默跳过，不要编造。写完只回一句「已写」。",
          display: true,
        },
      ],
    };
  });
}
