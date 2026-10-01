# VSBlender

This workspace is driven through the **vsblender** MCP server and the sidecar files under `.blender-ai/`. The `.blend` file is a binary. Do not read it, search it, or edit it. If you are changing the VSBlender extension source itself, that guidance is about the `.blend` files only.

The bridge listens on `127.0.0.1:{{PORT}}`. Blender has to be open with the VSBlender add-on running. If a tool cannot connect, call `doctor`, then `launch_blender`.

## Blend files

{{BLEND_LIST}}

Call `doctor` for each file's ingest status, and `session_info` for the file open in Blender. A status written here would be wrong as soon as anything changed.

| Status | Meaning |
|---|---|
| `current` | The sidecar describes the file, or the open session. |
| `stale` | The `.blend` on disk changed since the last ingest. Call `ingest`. |
| `new` | Never ingested. Call `ingest`. |
| `live` | The sidecar was built from a Blender session with unsaved changes, not from the file on disk. |
| `diverged` | Blender has unsaved changes the sidecar does not describe. Call `ingest` with `live: true`. |

## Before you change a model

1. Call `doctor`, and `ingest` the file if its status is not `current`. Use `live: true` when Blender has unsaved changes. Ingest only writes sidecar files. It never saves the `.blend`.
2. Call `context_pack` and read **Before you modify** before any edit. Keyframed values, generated objects and named constraints are listed there. `focus` keeps one object with its children, materials and animation.
3. Put Python in `scripts/*.py` and run it with `run_script`, and always pass a `reason`. Each call is checkpointed, journaled, and made one undo step. The reply says what changed, by category.
4. Check the result with `preview`, or with `compare` against the checkpoint from before the script. Use `render` for a final image with the scene's own settings.
5. If a pass goes wrong, use `restore_checkpoint` with the id from the `run_script` reply, or `last`.

## Tools

| Tool | Use it for |
|---|---|
| `doctor` | Read-only check of config, Blender, the add-on, the bridge, and each `.blend`'s ingest status. |
| `doctor_fix` | Reinstall the add-on and rewrite client config. Restart Blender afterwards. |
| `launch_blender` / `launch_blender_background` | Open Blender with the bridge, with a window or headless. Never starts a second Blender while one holds the port. |
| `session_info` | Live file, dirty flag, frame, units, engine, render devices, sidecar status, recent checkpoints, and gotchas for this Blender version. |
| `ingest` | Write `.blender-ai/<name>/` from the saved file (headless), or from the open session with `live: true`. |
| `context_pack` | `NOTES.md` cut to a token budget. Reports what was cut. |
| `run_script` | Run a workspace `.py` in Blender, with a `reason`. Blocks Blender's UI while it runs. |
| `preview` | Offscreen PNG. `views: [...]` returns several views as one contact sheet. `aspect`, `isolate`, `projection`, `compositor`, `dof`, `samples`. |
| `render` / `job_status` / `cancel_job` | Final render as a background job on a copy of the session: `still`, `frames`, `sheet`, or `mp4`. |
| `checkpoint` / `restore_checkpoint` | Snapshots in `.blender-ai/<name>/checkpoints/`. The user's file is not touched. |
| `diff` | What changed between `sidecar`, `live` and checkpoint ids. |
| `compare` / `reference` | Before and after with a difference heatmap. Reference images next to a matching preview. |
| `describe` / `find` / `spatial` | One datablock in full. Objects by selector (`type:MESH and children_of:"X"`). Bounds, raycasts, drops, distances. |
| `api` / `node_schema` | Blender API lookup, including live enum values. A node's sockets by identifier and name. |
| `set_role` | Record what an object is for in `roles.json`. It survives re-ingests. |

## Scripts

- `import vsblender` gives helpers that hide Blender version drift:
  - `sock(node, "Fac")` gets a socket by identifier or by name.
  - `build(tree, {...}, links=[...])` creates and lays out a node graph.
  - `set_keys` and `scale_keys` edit keyframes. They handle layered actions, and accept paths like `"Principled BSDF/Emission Strength"`.
  - `bbox_world`, `raycast_down` and `view3d_override` cover bounds, ground contact and operators that need a 3D view.
  - `progress(fraction, message)` reports progress, and lets the client cancel the script.
  - `mark_derived(obj, sources)` records that an object is generated from other objects.
- Shared code goes in `scripts/lib/`, which is importable from any script. Edits are picked up on the next call.
- Call `vsblender.progress()` in long loops. On a timeout or a cancel, the script stops at its next call. Use `render` for renders, not `bpy.ops.render.render` in a script.

## Sidecar layout

```
.blender-ai/<name>/
  NOTES.md        overview. Hand-written text after the generated marker is kept.
  manifest.json   structure. Diff it to see what changed.
  roles.json      what each object is for
  journal.jsonl   ingests, AI scripts (reason, script, changes, checkpoint), roles, restores
  checkpoints/    copies of the session from before AI scripts
  previews/       generated images, not the source of truth
```

Reviewed roles (`source` `ai` or `human`) survive a re-ingest, and a rename keeps them. Everything between the `ai:generated` markers in `NOTES.md` is rewritten.

## Scene rules

- Units in and out of tools are metres and degrees. Blender is Z-up. Front looks along +Y from -Y.
- `run_script` starts from a fresh Python namespace each call, with `bpy`, `mathutils` and `vsblender` defined.
- `run_script` runs on Blender's main thread from a timer, not from a 3D view. Prefer `bpy.data` and `bmesh` over `bpy.ops`. An operator that needs a view needs `with vsblender.view3d_override():`.
- Do not change `scene.render`, the compositor, or the user's viewport to take a picture. Use `preview` or `render`.
- Do not invent Blender 4.x API. `session_info` includes the gotchas for the running version, and `api` and `node_schema` answer from the running Blender. On Blender 5 the engine id is `BLENDER_EEVEE`, `Material.use_nodes` is gone, actions are slotted, and the compositor is `scene.compositing_node_group`.
- Socket identifiers and displayed names differ (`Fac` versus `Factor`). Use the identifier.
- Material keyframes address nodes by name. Keep a keyframed node's name when you rebuild a material.
- Saving the `.blend` is the user's decision. Do not save it unless you were asked to. Checkpoints keep unsaved work safe in the meantime.

## Clients

The setup wizard writes this guide for the clients that were switched on. Cursor and Cline are off unless the user enabled them. Grok uses this file when it is `CLAUDE.md`, and `AGENTS.md` only when Claude Code was left off, so the guide is not injected twice.

Setup also writes Claude Code permission rules: the read-only tools run without asking, and `.blend` files cannot be read or edited. `run_script` asks each time unless the user ticked "run scripts without asking" in setup.
