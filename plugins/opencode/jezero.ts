/**
 * Jezero plugin for OpenCode: a thin adapter over the shared Python core.
 *
 * - Registers the Jezero MCP server (from ~/.jezero/client.json, its `mcp_path` and its
 *   key file).
 * - Adds the Jezero protocol, recent work of the project and, after the first prompt,
 *   related past work to the system prompt.
 * - When a session goes idle, exports it through the SDK and lets the core upload it
 *   (unless this computer only reads Jezero: `init --no-upload`).
 * - Installs the `jezero` skill in OpenCode's skills folder when it ships next to it.
 *
 * Installed as a folder (npm package or `plugins/opencode` of the repository) it finds
 * the core in `./core`; `jezero_client.py setup --agent opencode` installs a single file
 * and writes the core's path in place of the placeholder below.
 */
import type { Plugin } from "@opencode-ai/plugin"
import { spawn, spawnSync } from "node:child_process"
import { chmodSync, copyFileSync, existsSync, mkdirSync, readFileSync, writeFileSync } from "node:fs"
import { homedir } from "node:os"
import { dirname, join } from "node:path"
import { fileURLToPath } from "node:url"

const HERE = dirname(fileURLToPath(import.meta.url))
const INSTALLED_CORE = "__JEZERO_CORE__"
const CORE =
  process.env.JEZERO_CORE ??
  (INSTALLED_CORE.includes("JEZERO_CORE") ? join(HERE, "core", "jezero_client.py") : INSTALLED_CORE)
const CONFIG_DIR = join(process.env.XDG_CONFIG_HOME ?? join(homedir(), ".config"), "opencode")
// Like the core: JEZERO_CLIENT_HOME, else a key kept in OpenCode's own config folder
// (sandboxed agents that cannot see the user's home), else ~/.jezero.
const AGENT_HOME = join(CONFIG_DIR, "jezero")
const HOME =
  process.env.JEZERO_CLIENT_HOME ??
  (existsSync(join(AGENT_HOME, "client.json")) ? AGENT_HOME : join(homedir(), ".jezero"))
const CORE_ENV = { ...process.env, JEZERO_CLIENT_HOME: HOME }
const EXPORTS = join(HOME, "spool", "opencode")

function core(args: string[], input: object): string {
  const run = spawnSync("python3", [CORE, ...args], {
    input: JSON.stringify(input),
    encoding: "utf8",
    timeout: 5000,
    env: CORE_ENV,
  })
  return run.status === 0 ? (run.stdout ?? "") : ""
}

// `mcp_path` in client.json, like the core's mcp_path(): the server's [server].path.
function mcpPath(value: unknown): string {
  const path = String(value ?? "").trim().replace(/^\/+|\/+$/g, "")
  return path ? `/${path}` : "/mcp"
}

function clientConfig(): { url: string; mcpUrl: string; token: string; upload: boolean } | null {
  try {
    const cfg = JSON.parse(readFileSync(join(HOME, "client.json"), "utf8"))
    const token = readFileSync(join(HOME, "token"), "utf8").trim()
    if (!cfg.url || !token) return null
    const url = String(cfg.url).replace(/\/+$/, "")
    return { url, mcpUrl: url + mcpPath(cfg.mcp_path), token, upload: cfg.upload !== false }
  } catch {
    return null
  }
}

function installSkill(): void {
  const source = join(HERE, "skills", "jezero", "SKILL.md")
  const target = join(CONFIG_DIR, "skills", "jezero", "SKILL.md")
  try {
    if (!existsSync(source)) return
    if (existsSync(target) && readFileSync(target, "utf8") === readFileSync(source, "utf8")) return
    mkdirSync(dirname(target), { recursive: true })
    copyFileSync(source, target)
  } catch {
    // The skill is a help, not a requirement.
  }
}

function promptText(parts: any[]): string {
  return (parts ?? [])
    .filter((p: any) => p?.type === "text" && !p?.synthetic && typeof p.text === "string")
    .map((p: any) => p.text)
    .join("\n")
}

export const Jezero: Plugin = async ({ client, directory }) => {
  const contextBySession = new Map<string, string>()
  const relatedBySession = new Map<string, string>()
  installSkill()

  return {
    // Register the Jezero MCP server, unless the user already configured one by hand.
    config: async (cfg: any) => {
      const jezero = clientConfig()
      if (!jezero) return
      cfg.mcp = cfg.mcp ?? {}
      if (cfg.mcp.jezero) return
      cfg.mcp.jezero = {
        type: "remote",
        url: jezero.mcpUrl,
        enabled: true,
        headers: { Authorization: `Bearer ${jezero.token}` },
      }
    },

    // First prompt of a session: past work that really matches it (checked by the core).
    "chat.message": async (input: any, output: any) => {
      const id = String(input?.sessionID ?? "")
      if (!id || relatedBySession.has(id)) return
      relatedBySession.set(id, "")
      const prompt = promptText(output?.parts)
      if (!prompt.trim()) return
      relatedBySession.set(
        id,
        core(["hook", "user-prompt", "--agent", "opencode"], { session_id: id, prompt, cwd: directory }),
      )
    },

    "experimental.chat.system.transform": async (input: any, output: any) => {
      const id = String(input?.sessionID ?? "")
      if (!contextBySession.has(id)) {
        contextBySession.set(id, core(["hook", "session-start", "--agent", "opencode"], { session_id: id, cwd: directory }))
      }
      for (const text of [contextBySession.get(id), relatedBySession.get(id)]) {
        if (text && text.trim()) output.system.push(text)
      }
    },

    event: async ({ event }: any) => {
      if (event?.type !== "session.idle") return
      const id = String(event.properties?.sessionID ?? "")
      const jezero = clientConfig()
      if (!id || !jezero?.upload) return
      try {
        const session = await client.session.get({ path: { id } })
        const messages = await client.session.messages({ path: { id } })
        // Only this user may read the exports (copies of sessions): folders 0700, files 0600.
        mkdirSync(EXPORTS, { recursive: true, mode: 0o700 })
        for (const dir of [HOME, join(HOME, "spool"), EXPORTS]) chmodSync(dir, 0o700)
        const file = join(EXPORTS, `${id.replace(/[^A-Za-z0-9._-]/g, "_")}.json`)
        writeFileSync(file, JSON.stringify({ session: session.data, messages: messages.data }), {
          mode: 0o600,
        })
        chmodSync(file, 0o600)
        const child = spawn("python3", [CORE, "hook", "session-end", "--agent", "opencode"], {
          detached: true,
          stdio: ["pipe", "ignore", "ignore"],
          env: CORE_ENV,
        })
        child.stdin?.end(JSON.stringify({ session_id: id, transcript_path: file, cwd: directory }))
        child.unref()
      } catch {
        // Never break OpenCode: the next idle event will try again.
      }
    },
  }
}
