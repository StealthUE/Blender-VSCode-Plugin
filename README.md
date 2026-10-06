# VSBlender

VS Code extension that sets up a Blender workspace for AI clients and drives Blender through a small local MCP server plus a Blender add-on. The AI can understand existing `.blend` files, build new models with a geometry kit, check them for their purpose (render, game, or 3D printing), and export them.

It covers the setup wizard, Doctor, the status bar, Launch Blender, auto-ingest, and these MCP tools:

| Group | Tools |
|---|---|
| Connection | `doctor`, `doctor_fix`, `launch_blender`, `launch_blender_background`, `session_info` |
| Understanding | `ingest` (file or `live` session), `context_pack`, `notes`, `describe`, `find`, `spatial`, `measure`, `timeline`, `api`, `node_schema`, `set_role` |
| Modelling | `new_blend`, `check_model`, `export_model`, `import_reference`, `append` |
| Changing | `run_script`, `run_pipeline`, `run_project_script`, `run_project_pipeline`, `open_blend`, `replay`, `checkpoint`, `restore_checkpoint`, `save` |
| Seeing | `preview`, `render`, `job_status`, `cancel_job`, `compare`, `reference`, `diff` |

## Modelling

Scripts run in Blender with `import vsblender` and `from vsblender import geo`:

```python
from vsblender import geo
import vsblender

plate = geo.box(60, 40, 4, fillet=3, edges="vertical")
part = plate + geo.cylinder(d=10, h=12).move(15, 0, 0) - geo.hole(m=3, depth=20, counterbore=True).move(15, 0, 12)
part.to_object("Bracket", materials=[vsblender.material("PLA Grey", color="#8a8d91")])
```

- **Solids:** `box`, `cylinder`, `cone`, `tube`, `sphere`, `torus`, `revolve`, `extrude`, `sweep`, `loft`, `text`, `thread`, `hole`, `nut_trap`. They combine with `+ - &` (Blender's boolean, Manifold solver when the inputs are closed), or with `geo.join` without a boolean. `to_object` updates an existing object in place, so material slots, modifiers and animation survive a re-run, and `materials=` keeps each face's slot. A fillet stays a handful of segments unless `segments=` is set.
- **Builders:** `lathe`, `prism` (holes by even-odd, concave outlines handled), `sweep`, `loft`, `polar_block`, `mirror_weld`, `finish` and `replace_mesh`, for detailed meshes.
- **Rings:** `geo.ring(up=, front=)` works in clock angles (clockwise from the top, seen from the front) for gates, dials, wheels and clock faces: points, lathe angles, surface mappings for `prism`, bands that follow a surface, one segment repeated round (`pattern`), `ring.svgs` for numerals and `ring.chevron`. `geom2d.arch` is a window outline. `vsblender.aim` points a camera along world Y, and `vsblender.dial` keeps a dial's unwrapped angle.
- **In place and in frame:** `to_object(parent=, space="local")` writes geometry in a parent's frame and never touches a keyed transform. `.clean()` removes boolean slivers without welding shells; `difference(..., union_cutters=True)` cuts overlapping cutter shells as their union.
- **Helpers:** `material`, `modifier`, `place_on_ground`, `set_origin`, `orient_flat`, `stats`, `units`/`mm`, `set_keys(replace=True)` and `clear_keys` for re-runnable animation, `part()` for reusable builders in `parts/`, `intent()` for the notes, and readers for STL, OBJ (vectorised, with its materials and groups as parts), 3MF and SVG.
- **References:** `import_reference` brings a big model (OBJ, FBX, glTF, STL, PLY) into the sidecar as cached STL parts, never into the `.blend`. `preview` shows it with `ref=` or a `solid` overlay; `measure` measures it with `ref=`, in clock angles, windows and radial sections, against your model with `compare_to`. A section's `out` path ending in `.json` writes the loops, and `vsblender.ref_section` does the same from a script.
- **`check_model`:** reports what matters for the purpose. It names a mesh that has several material slots and uses only slot 0, and notes an extreme triangle density. For printing it covers watertightness, self-intersections, bed contact, overhangs, thin walls, the printer's build volume, and a filament estimate. Its image colours the problem faces.
- **`export_model`:** writes 3MF or STL in millimetres, placed on the bed, for Bambu Studio, OrcaSlicer and PrusaSlicer. It also writes GLB, glTF, FBX, USD, OBJ and PLY, using Blender's exporters in a background Blender on a copy of the session.
- **`new_blend`:** starts a project from the `empty`, `render`, `game` or `print_mm` template. In `print_mm`, 1 unit = 1 mm and the printer's build plate is outlined. The scripts folder next to the new file is trusted.
- **Seeing the camera:** a preview from a named camera says where that camera is, its lens, what the frame spans, and what landed in it. An overlay can be a PNG or a video frame, and `mode: "diff"` sets that plate beside the render. `name` or `save: true` keeps the picture under `.blender-ai/live/`.

## Clients

| Client | Default | What setup writes |
|---|---|---|
| Claude Code | on | `.mcp.json`, `CLAUDE.md`, and `.claude/settings.json` permission rules. Read-only VSBlender tools are allowed, and reading or editing `.blend` files is denied. The script tools go into `.claude/settings.local.json`, and only when you tick them. |
| VS Code / Copilot | on | `.vscode/mcp.json`, `.github/copilot-instructions.md` |
| Grok | on | `.grok/config.toml`. Grok also reads `CLAUDE.md` and Claude's permission files, so neither is written a second time. If that session has no vsblender tools, the guide tells it to stop and ask for the server to be turned on in `/mcps`. |
| Cursor | off | `.cursor/mcp.json`, `.cursorrules` |
| Cline | off | `.cline/mcp.json` with `alwaysAllow` for the read-only tools, `.clinerules` |

- A guide that does not start with `<!-- vsblender-guide` is left alone. Existing MCP servers are kept. Entries whose command is `mcp-for-blender` are removed only when that box is ticked (it starts ticked).
- VS Code and Cursor keep tool approvals in their own settings, not in a workspace file, so setup cannot pre-approve tools for them.
- The client MCP files hold paths for this machine. By default setup adds them to `.gitignore`, and each person runs setup. The MCP server also finds the workspace by walking up from its working directory to `.blender-ai/config.json`, so a config without `VSBLENDER_WORKSPACE` still works.

### Tools that ask, and tools that don't

Permission rules match tool names and cannot see arguments, so each tool with a mutating mode is split in two.

| Runs without asking (Claude Code) | Asks |
|---|---|
| `doctor`, `session_info`, `context_pack`, `notes`, `ingest`, `preview`, `describe`, `find`, `spatial`, `measure`, `timeline`, `check_model`, `api`, `node_schema`, `job_status`, `cancel_job`, `diff`, `compare`, `checkpoint`, `launch_blender` | `doctor_fix`, `launch_blender_background`, `new_blend`, `export_model`, `run_script` and `run_pipeline` (unless ticked in setup), `run_project_script` and `run_project_pipeline` (unless either tick), `open_blend`, `replay`, `save` (unless its own tick), `append`, `import_reference` (it reads files from anywhere), `restore_checkpoint`, `render`, `set_role`, `reference` (it fetches URLs) |

`run_project_script` and `run_project_pipeline` only run scripts inside the `trustedScripts` folders. Ticking them lets the AI run anything it can write into those folders without asking. That is only as safe as the client's file-edit permission for those folders; Claude Code still asks before editing files.

Saving the `.blend` is the user's decision. When a run leaves the file on disk behind, the first line of the reply is the count and the file, and the editor offers Save. `session_info` and `doctor` say how many runs are unsaved and since when. `save` saves when asked, refuses while reference objects are in the scene, refuses a plain save of a file outside the workspace, and is journaled. An open autosave or `quit.blend` is named, with the project file to save back to. The "Let the AI save the .blend" tick in setup lets it run without asking and allows `save: true` on the script tools. `doctor_fix` reinstalls the add-on and rewrites config; it archives orphan sidecars only when `archive: true`. Opening the extension installs the add-on when the installed copy is a different version.

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
  1. A compressed checkpoint is saved first. It is dropped again when the script changed nothing (or skipped for `# vsblender: read-only`).
  2. The script runs with its own folder, every `lib/` from there up to the workspace root, and `libPaths` on `sys.path`. Workspace modules are reloaded, and no `.pyc` files are written.
  3. The changes are worked out from before/after snapshots of `bpy.data`, keyed by `session_uid`. Every datablock gets hashes per aspect: transform, data, modifiers, nodes, keys and so on. The depsgraph adds geometry updates.
  4. If the script raised or was cancelled, the session is rolled back to the checkpoint (`atomic: false` keeps the partial changes).
  5. Objects it built are stamped with the script (`ai_built_by`, sha, time, reason), and the run becomes one undo step.
  6. A journal entry is written: reason, script sha, changes, checkpoint id. A line is added to the change log in `NOTES.md`. Steps that are now out of date (from `pipeline.json` or `rerun-after` headers) are named.
- **Changes** are reported as `added`, `removed`, `recreated` (deleted and rebuilt under the same name), `renamed`, `modified[category][name] = [aspects]`, `lost_animation` (removed or rebuilt datablocks that had keys) and `temporary` (built and removed within the run, not a change). Scene settings are reported by property path, such as `view_settings.look: 'None' -> 'AgX - Punchy'`. Datablocks that would not survive a save are left out, such as a mesh orphaned by a deleted object.
- **Unsaved work** is counted: runs since the last save and when the first was. A save through the `save` tool is journaled with its reason, and the watcher's re-ingest then logs the save as the AI's, pointing at those runs.
- **The `vsblender` module** gives scripts node and keyframe helpers (`sock`, `build`, `set_keys`, `scale_keys`, `fcurves`), modelling (`geo`, `material`, `modifier`, `place_on_ground`, `set_origin`, `orient_flat`, `stats`), units (`units`, `mm`, `m`), `spec`, `mark_derived`, `progress`, file readers, and more.
- **Checkpoints** are compressed `save_as_mainfile(copy=True)` files in `.blender-ai/<name>/checkpoints/`, kept to a count and a disk budget. Run replies give each one's size and the total. Restore removes the session's data and appends the checkpoint's, so the file path stays the same. It checkpoints the current state first, and it is one undo step.
- **Preview** creates a throwaway scene, so the render does not change the user's viewport, camera or render settings. Overlays, references, material balls and check images use temporary copies in that scene. An image too large for a tool reply comes back as a smaller JPEG.
- **Videos** (`render` as `mp4`) cover the scene's frame range unless told otherwise. With ffmpeg (on PATH, in the usual folders, or `"ffmpeg"` in the config) every frame is rendered and then encoded as H.264, yuv420p, `+faststart`; without it, Blender's own writer. Either way the file appears at its path only when it is complete.
- **Live ingest** runs the same ingester inside the session, with offscreen previews.
- **The sidecar status** compares a signature of the session with the one stored at ingest. That gives `current`, `stale`, `new`, `live` or `diverged`.

Ingest shells out to the `blender_ingest.py` shipped inside the add-on (`resources/addon/vsblender_bridge/`), never a copy from the workspace, because auto-ingest runs when a folder opens. It only writes `.blender-ai/<name>/` and does not save the `.blend`. The extension runs only in trusted folders for the same reason: `.blender-ai/config.json` names the Blender executable.

Render jobs, exports with Blender's exporters, new files, checkpoint previews, manifest builds and the read-only tools used while Blender is closed run in a separate headless Blender through `resources/job.py`, working on a copy of the session or on the saved file. The user's Blender stays usable while they run. On Windows, the first headless Blender started while another Blender is running can take about 20 s to start. Later ones start in about 1 s.

The MCP server runs from a copy of `out/` and `resources/` in the extension's global storage. Client configs point there, so they keep working after the extension updates, even in a client started without VS Code. That copy is refreshed when the server is rebuilt or when `resources/blender-guide.md` changes. `launch_blender` loads the add-on shipped with the extension when the one installed in Blender is older.

## `.blender-ai/config.json`

Written by setup. Keys setup does not manage are kept when it rewrites the file.

| Key | Meaning |
|---|---|
| `port`, `blender`, `clients`, `replaceLegacy` | As chosen in setup. |
| `allowScripts` | `run_script` and `run_pipeline` without asking in Claude Code (`.claude/settings.local.json`). |
| `allowTrustedScripts` | `run_project_script` and `run_project_pipeline` without asking. |
| `trustedScripts` | Workspace folders whose scripts `run_project_script` and pipelines `run_project_pipeline` accept, e.g. `["Models/scripts"]`. |
| `allowSave` | The AI may save the `.blend`: `save` without asking, and `save: true` on the script tools. Default off. |
| `referenceRoots` | Folders outside the workspace that `measure`, preview overlays and reference videos may read, e.g. `["Z:/Stargate"]`. |
| `sharedLibs` | Another project's script folders that every script can import from, e.g. `["TestObject/SG1/scripts/lib"]`. |
| `ffmpeg` | ffmpeg for videos, when it is not on PATH. |
| `printer` | The 3D printer for purpose print: a preset (`bambu_p1s`, `bambu_x1c`, `bambu_p1p`, `bambu_a1`, `bambu_a1_mini`, `prusa_mk4`, `prusa_core_one`, `prusa_mini`, `creality_ender3`, `generic_220`) or an object such as `{"preset": "bambu_p1s", "material": "PETG", "minWall": 1.2}`. Fields: `buildVolume` (mm), `nozzle`, `layerHeight`, `minWall`, `maxOverhangDeg`, `material`, `density`, `filamentDiameter`, `holeCompensation`. |
| `ignoreClientConfig` | Git-ignore the client MCP files. Default `true`. |
| `checkpoints` | `{ "auto": true, "keep": 10, "maxMb": 300, "budgetMb": 500 }`. Automatic checkpoints before scripts, how many to keep, the file size above which they are skipped, and the disk they may take per `.blend` (the oldest go first). |
| `libPaths` | Folders importable from scripts, besides the `lib/` folders next to each script. Default `["scripts/lib"]`. |
