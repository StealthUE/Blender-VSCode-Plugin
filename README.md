# VSBlender

VS Code extension that sets up a Blender workspace for AI clients and drives Blender through a small local MCP server plus a Blender add-on.

The first version covers the setup wizard, Doctor, the status bar, Launch Blender, auto-ingest, and these MCP tools: `session_info`, `ingest`, `context_pack`, `run_script`, and offscreen `preview`.

## Clients

| Client | Default | What setup writes |
|---|---|---|
| Claude Code | on | `.mcp.json`, `CLAUDE.md`, `.claude/settings.json` deny rules for `.blend` |
| VS Code / Copilot | on | `.vscode/mcp.json`, `.github/copilot-instructions.md` |
| Grok | on | `.grok/config.toml`. Grok also reads `CLAUDE.md`, so the guide is not written a second time. |
| Cursor | off | `.cursor/mcp.json`, `.cursorrules` |
| Cline | off | `.cline/mcp.json`, `.clinerules` |

A guide that does not start with `<!-- vsblender-guide` is left alone. Existing MCP servers are kept. Entries whose command is `mcp-for-blender` are removed only when that box is ticked (it starts ticked).

## Develop

```
npm install
npm test
```

Press F5. The launch config opens `TestObject` in the extension host. The setup wizard installs the add-on into the selected Blender and writes that folder's client config.

`Info.txt` has the same commands.

## What the add-on does

It listens on `127.0.0.1:47876` (changeable). Launch Blender from the extension starts it even before preferences are saved. Preview creates a throwaway scene, so the render does not change the user's viewport, camera, or render settings. `run_script` compiles the workspace `.py` with that path, so a traceback names the file and line.

Ingest shells out to the `blender_ingest.py` shipped in `resources/`, never a copy from the workspace, because auto-ingest runs when a folder opens. It only writes `.blender-ai/<name>/` and does not save the `.blend`. The extension runs only in trusted folders for the same reason: `.blender-ai/config.json` names the Blender executable.

The MCP server runs from a copy of `out/` and `resources/` in the extension's global storage. Client configs point there, so they keep working after the extension updates, even in a client started without VS Code.
