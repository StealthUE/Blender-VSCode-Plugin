# VSBlender

VS Code extension that sets up a Blender workspace for AI clients and drives Blender through a small local MCP server plus a Blender add-on.

It covers the setup wizard, Doctor, the status bar, Launch Blender, auto-ingest, and these MCP tools:

| Group | Tools |
|---|---|
| Connection | `doctor`, `doctor_fix`, `launch_blender`, `launch_blender_background`, `session_info` |
| Understanding | `ingest` (file or `live` session), `context_pack`, `describe`, `find`, `spatial`, `api`, `node_schema`, `set_role` |
| Changing | `run_script`, `checkpoint`, `restore_checkpoint` |
| Seeing | `preview`, `render`, `job_status`, `cancel_job`, `compare`, `reference`, `diff` |

## Clients

| Client | Default | What setup writes |
|---|---|---|
| Claude Code | on | `.mcp.json`, `CLAUDE.md`, and `.claude/settings.json` permission rules. Read-only VSBlender tools are allowed, and reading or editing `.blend` files is denied. `run_script` goes into `.claude/settings.local.json`, and only when you tick it. |
| VS Code / Copilot | on | `.vscode/mcp.json`, `.github/copilot-instructions.md` |
| Grok | on | `.grok/config.toml`. Grok also reads `CLAUDE.md` and Claude's permission files, so neither is written a second time. |
| Cursor | off | `.cursor/mcp.json`, `.cursorrules` |
| Cline | off | `.cline/mcp.json` with `alwaysAllow` for the read-only tools, `.clinerules` |

- A guide that does not start with `<!-- vsblender-guide` is left alone. Existing MCP servers are kept. Entries whose command is `mcp-for-blender` are removed only when that box is ticked (it starts ticked).
- VS Code and Cursor keep tool approvals in their own settings, not in a workspace file, so setup cannot pre-approve tools for them.
- The client MCP files hold paths for this machine. By default setup adds them to `.gitignore`, and each person runs setup. The MCP server also finds the workspace by walking up from its working directory to `.blender-ai/config.json`, so a config without `VSBLENDER_WORKSPACE` still works.

### Tools that ask, and tools that don't

Permission rules match tool names and cannot see arguments, so each tool with a mutating mode is split in two.

| Runs without asking (Claude Code) | Asks |
|---|---|
| `doctor`, `session_info`, `context_pack`, `ingest`, `preview`, `describe`, `find`, `spatial`, `api`, `node_schema`, `job_status`, `cancel_job`, `diff`, `compare`, `checkpoint`, `launch_blender` | `doctor_fix`, `launch_blender_background`, `run_script` (unless ticked in setup), `restore_checkpoint`, `render`, `set_role`, `reference` (it fetches URLs) |

## Develop

```
npm install
npm test                 # TypeScript and the MCP server, no Blender needed
npm run test:blender     # the add-on, in a headless Blender (blender on PATH)
npm run test:e2e         # MCP tools against a headless Blender on port 47911 (set VSBLENDER_BLENDER)
```

Press F5. The launch config opens `TestObject` in the extension host. The setup wizard installs the add-on into the selected Blender and writes that folder's client config.

`Info.txt` has the same commands. The Blender tests build their own scene, so they never open a `.blend` from the repo.

## What the add-on does

It listens on `127.0.0.1:47876` (changeable). `ping` and `cancel` are answered on the socket thread, so they work while a script runs. Everything else runs on Blender's main thread.

- **`run_script`** compiles the workspace `.py` with its path, so a traceback names the file and line. Each call goes through these steps:
  1. A checkpoint is saved first. It is dropped again when the script changed nothing.
  2. The script runs with `scripts/lib` on `sys.path`. Workspace modules are reloaded, and no `.pyc` files are written.
  3. The changes are worked out from before/after snapshots of `bpy.data`, keyed by `session_uid`. Every datablock gets hashes per aspect: transform, data, modifiers, nodes, keys and so on. The depsgraph adds geometry updates.
  4. The run becomes one undo step.
  5. A journal entry is written: reason, script sha, changes, checkpoint id. A line is added to the change log in `NOTES.md`.
- **Changes** are reported as `added`, `removed`, `recreated` (deleted and rebuilt under the same name), `renamed` and `modified[category][name] = [aspects]`. Scene settings are reported by property path, such as `view_settings.look: 'None' -> 'AgX - Punchy'`. Datablocks that would not survive a save are left out, such as a mesh orphaned by a deleted object.
- **The `vsblender` module** gives scripts `sock`, `build`, `layout`, `set_keys`, `scale_keys`, `fcurves`, `bbox_world`, `raycast_down`, `view3d_override`, `progress` and `mark_derived`.
- **Checkpoints** are `save_as_mainfile(copy=True)` files in `.blender-ai/<name>/checkpoints/`. Restore removes the session's data and appends the checkpoint's, so the file path stays the same. It checkpoints the current state first, and it is one undo step.
- **Preview** creates a throwaway scene, so the render does not change the user's viewport, camera or render settings.
- **Live ingest** runs the same ingester inside the session, with offscreen previews.
- **The sidecar status** compares a signature of the session with the one stored at ingest. That gives `current`, `stale`, `new`, `live` or `diverged`.

Ingest shells out to the `blender_ingest.py` shipped inside the add-on (`resources/addon/vsblender_bridge/`), never a copy from the workspace, because auto-ingest runs when a folder opens. It only writes `.blender-ai/<name>/` and does not save the `.blend`. The extension runs only in trusted folders for the same reason: `.blender-ai/config.json` names the Blender executable.

Render jobs, checkpoint previews and manifest builds run in a separate headless Blender through `resources/job.py`, working on a copy of the session. The user's Blender stays usable while they run. On Windows, the first headless Blender started while another Blender is running can take about 20 s to start. Later ones start in about 1 s.

The MCP server runs from a copy of `out/` and `resources/` in the extension's global storage. Client configs point there, so they keep working after the extension updates, even in a client started without VS Code. `launch_blender` loads the add-on shipped with the extension when the one installed in Blender is older.

## `.blender-ai/config.json`

Written by setup.

| Key | Meaning |
|---|---|
| `port`, `blender`, `clients`, `replaceLegacy` | As chosen in setup. |
| `allowScripts` | `run_script` without asking in Claude Code (`.claude/settings.local.json`). |
| `ignoreClientConfig` | Git-ignore the client MCP files. Default `true`. |
| `checkpoints` | `{ "auto": true, "keep": 10, "maxMb": 300 }`. Automatic checkpoints before scripts, how many to keep, and the file size above which they are skipped. |
| `libPaths` | Folders importable from scripts. Default `["scripts/lib"]`. |
