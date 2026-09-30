# VSBlender

This workspace is driven through the **vsblender** MCP server and the sidecar files under `.blender-ai/`. The `.blend` file is a binary. Do not read it, search it, or edit it. If you are changing the VSBlender extension source itself, that guidance is about the `.blend` files only.

The bridge listens on `127.0.0.1:{{PORT}}`. Blender has to be open with the VSBlender add-on running. If a tool cannot connect, call `doctor`, then `launch_blender`.

## Blend files

{{BLEND_TABLE}}

`current` means the sidecar matches the file. `stale` means the `.blend` changed since the last ingest. `new` means it has not been ingested.

## Before you change a model

1. `ingest` the file if the table does not say `current`. Ingest only writes sidecar files. It does not save the `.blend`.
2. `context_pack` and read **Before you modify** before any edit. Keyframed values and named constraints are in there.
3. Put Python in `scripts/*.py` in this workspace and run it with `run_script`. A traceback points at that file and line. Set a `result` variable, or pass `function`.
4. Check the result with `preview`. That render is offscreen. It does not move the user's viewport, camera, or render settings.
5. `session_info` is the live file, frame, units, engine, and the gotchas for this Blender version.

## Tools

| Tool | Use it for |
|---|---|
| `doctor` | Config, Blender path, add-on, and whether the bridge is listening. `fix` rewrites client config and reinstalls the add-on. |
| `launch_blender` | Open Blender with the bridge started. Opens the only `.blend` when there is one, or pass `file`. Never starts a second Blender while one holds the port. `background: true` keeps a headless Blender running. |
| `session_info` | Version, open file, dirty flag, scene, frame, units, engine, gotchas. |
| `ingest` | First contact, or a refresh after the file changes. Writes `.blender-ai/<name>/`. |
| `context_pack` | A token-budget summary of `NOTES.md`. Pass `focus` to keep one object or material. |
| `run_script` | Run a workspace `.py` inside the open Blender. Returns stdout, warnings, and ids that changed. |
| `preview` | One offscreen PNG. `view`: camera, front, back, left, right, top, bottom, iso. `shading`: solid, material, rendered. |

## Sidecar layout

```
.blender-ai/<name>/
  NOTES.md        overview. Hand-written text after the generated marker is kept.
  manifest.json   structure. Diff it to see what changed.
  roles.json      what each object is for
  journal.jsonl   ingest history
  previews/       generated images, not the source of truth
```

Reviewed roles (`source` `ai` or `human`) survive a re-ingest. Everything between the `ai:generated` markers in `NOTES.md` is rewritten.

## Scene rules

- Units in and out of tools are metres and degrees. Blender is Z-up. Front looks along +Y from -Y.
- `run_script` starts from a fresh Python namespace each call, with `bpy` and `mathutils` defined. Import anything else you need, or write it in the file.
- `run_script` runs on Blender's main thread from a timer, not from a 3D view. Prefer `bpy.data` and `bmesh` over `bpy.ops`. An operator that needs a view needs `with bpy.context.temp_override(window=..., area=...)` with a `VIEW_3D` area you looked up.
- Each script that changes the scene is one undo step in Blender, so the user can Ctrl+Z it as a whole. On failure the error starts at your file and line, and lists what had already changed.
- Do not change `scene.render`, the compositor, or the user's viewport to take a picture. Use `preview`.
- Do not invent Blender 4.x API. `session_info` includes the gotchas for the running version. On Blender 5 the engine id is `BLENDER_EEVEE`, `Material.use_nodes` is gone, and the compositor is `scene.compositing_node_group`.
- Socket identifiers and displayed names differ (`Fac` versus `Factor`). Use the identifier.
- Saving the `.blend` is the user's file. Do not save it unless you were asked to.

## Clients

The setup wizard writes this guide for the clients that were switched on. Cursor and Cline are off unless the user enabled them. Grok uses this file when it is `CLAUDE.md`, and `AGENTS.md` only when Claude Code was left off, so the guide is not injected twice.
