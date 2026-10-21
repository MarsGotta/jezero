/**
 * Jezero plugin for OpenCode: a thin adapter over the shared Python core.
 *
 * - Registers the Jezero MCP server (from ~/.jezero/client.json and its key file).
 * - Adds the Jezero protocol and recent work of the project to the system prompt.
 * - When a session goes idle, exports it through the SDK and lets the core upload it.
 *
 * `jezero_client.py setup --agent opencode` installs this file and replaces the core path.
 */
import type { Plugin } from "@opencode-ai/plugin"
import { spawn, spawnSync } from "node:child_process"
import { mkdirSync, readFileSync, writeFileSync } from "node:fs"
import { homedir } from "node:os"
import { join } from "node:path"

const CORE = process.env.JEZERO_CORE ?? "__JEZERO_CORE__"
const HOME = process.env.JEZERO_CLIENT_HOME ?? join(homedir(), ".jezero")
const EXPORTS = join(HOME, "spool", "opencode")

function core(args: string[], input: object): string {
  const run = spawnSync("python3", [CORE, ...args], {
    input: JSON.stringify(input),
    encoding: "utf8",
    timeout: 5000,
  })
  return run.status === 0 ? (run.stdout ?? "") : ""
}

function clientConfig(): { url: string; token: string } | null {
  try {
    const cfg = JSON.parse(readFileSync(join(HOME, "client.json"), "utf8"))
    const token = readFileSync(join(HOME, "token"), "utf8").trim()
    return cfg.url && token ? { url: String(cfg.url).replace(/\/+$/, ""), token } : null
  } catch {
    return null
  }
}

export const Jezero: Plugin = async ({ client, directory }) => {
  const contextBySession = new Map<string, string>()

  return {
    // Register the Jezero MCP server, unless the user already configured one by hand.
    config: async (cfg: any) => {
      const jezero = clientConfig()
      if (!jezero) return
      cfg.mcp = cfg.mcp ?? {}
      if (cfg.mcp.jezero) return
      cfg.mcp.jezero = {
        type: "remote",
        url: `${jezero.url}/mcp`,
        enabled: true,
        headers: { Authorization: `Bearer ${jezero.token}` },
      }
    },

    "experimental.chat.system.transform": async (input: any, output: any) => {
      const id = String(input?.sessionID ?? "")
      if (!contextBySession.has(id)) {
        contextBySession.set(id, core(["hook", "session-start", "--agent", "opencode"], { session_id: id, cwd: directory }))
      }
      const text = contextBySession.get(id)
      if (text && text.trim()) output.system.push(text)
    },

    event: async ({ event }: any) => {
      if (event?.type !== "session.idle") return
      const id = String(event.properties?.sessionID ?? "")
      if (!id) return
      try {
        const session = await client.session.get({ path: { id } })
        const messages = await client.session.messages({ path: { id } })
        mkdirSync(EXPORTS, { recursive: true })
        const file = join(EXPORTS, `${id}.json`)
        writeFileSync(file, JSON.stringify({ session: session.data, messages: messages.data }))
        const child = spawn("python3", [CORE, "hook", "session-end", "--agent", "opencode"], {
          detached: true,
          stdio: ["pipe", "ignore", "ignore"],
        })
        child.stdin?.end(JSON.stringify({ session_id: id, transcript_path: file, cwd: directory }))
        child.unref()
      } catch {
        // Never break OpenCode: the next idle event will try again.
      }
    },
  }
}
