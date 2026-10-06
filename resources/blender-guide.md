# VSBlender

This workspace is driven through the **vsblender** MCP server and the sidecar files under `.blender-ai/`. The `.blend` file is a binary. Do not read it, search it, or edit it. If you are changing the VSBlender extension source itself, that guidance is about the `.blend` files only.

## When the tools are not connected

The tools in the table below (`doctor`, `ingest`, `run_script`, `session_info`, and the rest) are in this session only when the vsblender MCP server is connected. A `.grok/config.toml` or `.mcp.json` entry means it is configured. It does not mean this session can call it.

If `search_tool` does not list those tools, stop. Tell the user the vsblender server is configured and not connected, and ask them to turn it on (`/mcps`, enable **vsblender**) and send the request again. Do not search again. Do not try another way. Wait.

Do not work around a missing server:

- Do not launch `blender.exe`, including `--background`, and do not save a `.blend` from a process you started. A second Blender does not update the window the user has open.
- Do not open a socket to the bridge port and send JSON.
- Do not load the extension's `mcp.js` or `tools.js` yourself.
- Do not create or edit FreeCAD (`.FCStd`), OpenSCAD, STEP, Fusion, or any other CAD project. A CAD file that is already there is a reference: once the tools are connected, `import_reference` reads it. It is not the model you build.
- Do not write a substitute model in any other format.

The only model is the `.blend`. With the tools connected, the files you may write are the scripts next to that file (`<blend folder>/scripts/`), the `.blender-ai` sidecar, and an export the user asked for (`export_model`). Nothing else.

The bridge listens on `127.0.0.1:{{PORT}}`. Most tools need Blender open with the VSBlender add-on running. If a tool cannot connect, call `doctor`, then `launch_blender`. Do not start Blender yourself. Read-only tools (`describe`, `find`, `spatial`, `measure`, `timeline`, `check_model`, `preview`, `export_model`) also work with Blender closed: the tool reads the saved file in a background Blender and says `source: saved file`. That background process is the tool's, and it does not save over a file a window already has open.

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
2. Call `context_pack` and read **Before you modify** and **Intent & constraints** before any edit. Keyframed values, generated objects and named constraints are listed there; **Animation beats** says what happens when. `focus` keeps one object with its children, materials and animation.
3. Put Python in `<blend folder>/scripts/*.py` (shared code in `<blend folder>/scripts/lib/`) and run it with `run_script`, and always pass a `reason`. Each call is checkpointed, journaled and made one undo step, and a failed or cancelled run is rolled back. The reply says what changed, by category, warns when animated data was lost, gives the checkpoint's size, and ends with what is unsaved.
4. Check the result with `preview`, or with `compare` against the checkpoint from before the script. Use `render` for a final image with the scene's own settings.
5. If a pass goes wrong, use `restore_checkpoint` with the id from the `run_script` reply, or `last`.
6. After the first successful build, call `notes` with what the next session must know: where the sizes live, the angle convention, what to re-run after what, which nodes are keyed.

## Saving

Saving the `.blend` is the user's decision. When a run or a pipeline leaves the file on disk behind, the first line of the reply is the count and the file, and the editor offers Save. Run replies and `session_info` also say how many runs are unsaved and since when. When they ask for a save, call `save` with a reason: it is journaled, and the change log then says the saved changes came from your runs. `save` refuses while reference objects are in the scene, and it refuses a plain save of a file that lives outside the workspace. An autosave (`<name>_<pid>_autosave.blend`) or `quit.blend` is named in `session_info`; save it back with `path` set to the project file and `overwrite: true`. `open_blend` opens a workspace `.blend` and journals it. `replay` lists the journaled runs since the last save, and runs them when `run: true`. If the user ticked "Let the AI save the .blend" in setup, `save` runs without asking and the script tools accept `save: true`.

## Building a model

1. A new model starts with `new_blend`: `empty` (metres), `render` (metres; camera, three lights, floor), `game` (metres), or `print_mm` (1 unit = 1 mm, with the printer's build plate). It never overwrites a file. It trusts `<blend folder>/scripts` for `run_project_script`, including before that folder exists.
2. Write a script with `from vsblender import geo` (below) and `vsblender.material`, and run it. Re-running it updates the same objects in place.
3. Look at it with `preview`. A named camera's first line is where the camera is, its lens, and how much of the scene the frame spans, then what landed in the frame. `region` is ignored for `view: camera`; use `crop` to zoom. Solid shading shows the materials' colours, cavity and outlines. An overlay can be an image or a video frame (`{"image": "plates/chevron.png"}`, `{"video": "plates/dial.mp4", "time": 52}`), and `mode: "diff"` puts the render, the plate and the difference side by side. Pass `name` or `save: true` to keep the PNG under `.blender-ai/live/`.
4. `check_model` with the model's purpose: `general`, `render`, `game` or `print`. It returns an image with the problem faces coloured. Fix what it reports and check again. Animated models: pass `frame`.
5. `export_model`: 3MF or STL for a slicer, GLB/glTF, FBX, USD, OBJ or PLY for other tools. Files go to `<blend folder>/exports/`; nothing is overwritten unless `overwrite: true`.

## Working from a reference

1. A reference model (a scan, a show model, a CAD export) goes through `import_reference`, never into the scene: it is imported in a background Blender, split into parts by material or object, cached as STL in the sidecar, and registered by name in `references.json` with its units and transform. Call it again with another `transform` to align it; the parts are reused.
2. Look at it with `preview` `ref: "peg"` (alone, shaded), or `overlay: [{"ref": "peg", "style": "solid"}]` (in the scene, hidden behind nearer geometry), `wire`, `xray` or `silhouette`. `"peg/Chevron"` is one part at full resolution.
3. Measure it with `measure` `ref: "peg/Chevron"`. `ring: {up, front}` measures in clock angles; `angle: [a0, a1]`, `radius` and `height` windows narrow any op; `plane: {angle: 20}` cuts a ring's cross-section (`image: true` draws it); `compare_to: {targets: "AG Gate Ring"}` measures your model on the same bins and reports the deviations. `out: "scripts/lib/chevron.json"` on a section writes the loops. From a script, `vsblender.ref_section(name, plane=, ring=)` measures a registered reference and leaves it out of the scene.
4. Files outside the workspace need their folder in `"referenceRoots"` of `.blender-ai/config.json` (`import_reference` reads from anywhere, after the user approves it). Reference videos: `reference` with `video`, `times` or `fps`, `crop`, and `diff: true` to see what moved.

## Tools

| Tool | Use it for |
|---|---|
| `doctor` | Read-only check of config, Blender, the installed add-on, the bridge, the printer profile, unsaved runs, orphan sidecars, and each `.blend`'s ingest status. |
| `doctor_fix` | Reinstall the add-on and rewrite client config. Orphan sidecars are listed; `archive: true` moves them to `.blender-ai/_archive/`. The extension installs the add-on on startup when the installed copy is a different version. Restart Blender afterwards. |
| `launch_blender` / `launch_blender_background` | Open Blender with the bridge, with a window or headless. Never starts a second Blender while one holds the port. |
| `new_blend` | A new `.blend` from a template (`empty`, `render`, `game`, `print_mm`), made in a background Blender. Trusts `<blend folder>/scripts`. |
| `session_info` | Live file, dirty flag, unsaved runs, frame, units, template, engine, render devices, sidecar status, recent checkpoints, printer, an autosave or `quit.blend` when that is the open file, and gotchas for this Blender version (once per session). |
| `ingest` | Write `.blender-ai/<name>/` from the saved file (headless), or from the open session with `live: true`. |
| `context_pack` | `NOTES.md` cut to a token budget. Reports what was cut. |
| `notes` | Write the Intent & constraints section of `NOTES.md` (kept across re-ingests). |
| `run_script` | Run a workspace `.py` in Blender, with a `reason`. Blocks Blender's UI while it runs. Rolled back on failure (`atomic: false` keeps partial changes). `background: true` runs it on a copy in a headless Blender and throws the copy away. |
| `run_pipeline` | Run the steps of a `pipeline.json` from one step to another as one checkpoint and one undo step. |
| `run_project_script` / `run_project_pipeline` | `run_script` and `run_pipeline` for files in the trusted folders of `.blender-ai/config.json` only. |
| `save` | Save the open `.blend` when the user asks; journaled. A plain save of a file outside the workspace is refused. |
| `open_blend` | Open a workspace `.blend`. The same unsaved-edits refusal as opening any file, and a journal line. |
| `replay` | Journaled runs since the last save. `run: true` runs them; a script whose file changed is skipped unless `force: true`. |
| `append` | Copy objects from another workspace `.blend`, recording where they came from. |
| `import_reference` | A big reference model as cached STL parts in the sidecar, registered by name. |
| `preview` | Offscreen image. A named camera leads with its location, lens and frame span, then what is in the frame. `views`, `crop`, `region`, `camera`, `ref`, `overlay` (objects, files, references, a PNG or a video frame; `mode: "diff"` for the plate), `name` or `save` to keep it, `material`, `cavity`, `matcap`, `aspect`, `isolate`, `projection`, `compositor`, `dof`, `samples`. |
| `check_model` | Fitness for a purpose: open, non-manifold, flipped, coincident and degenerate faces, transforms, triangle budget, materials (including several slots with only slot 0 used), an extreme triangle density, UVs; for print: fits the printer, self-intersections, bed contact, floating parts, overhangs, thin walls, filament. `frame` for animated models. |
| `export_model` | 3MF/STL in mm (on the bed for print), or GLB, glTF, FBX, USD, OBJ, PLY through Blender's exporters on a copy. |
| `measure` | Sections (with points and images), profiles, depth maps, angular occupancy and repeat pitch, of scene objects, files or references, with angle/radius/height windows, ring frames and `compare_to` deltas. `out` ending in `.json` on a section writes the loops. |
| `timeline` | Every keyframe in time order with readable channels, or a channel's values at given frames. |
| `render` / `job_status` / `cancel_job` | Final render as a background job on a copy of the session: `still`, `frames`, `sheet` (16 frames at most), or `mp4` (the scene's frame range by default; H.264 through ffmpeg when found). |
| `checkpoint` / `restore_checkpoint` | Compressed snapshots in `.blender-ai/<name>/checkpoints/`, within a disk budget. The user's file is not touched. |
| `diff` | What changed between `sidecar`, `live` and checkpoint ids. |
| `compare` / `reference` | Before and after with a difference heatmap. Reference images or video frames next to a matching preview. |
| `describe` / `find` / `spatial` | One datablock in full (with the script that built it, its spec, where it was appended from; `frame`). Objects by selector (`type:MESH and children_of:"X"`, `built_by:lamp.py`, `part:dhd`, `stage`, `reference`). Bounds, raycasts, drops, distances. |
| `api` / `node_schema` | Blender API lookup, including live enum values (inherited Node/ID properties left out). A node's sockets by identifier and name. |
| `set_role` | Record what an object is for in `roles.json`. It survives re-ingests. |

## Scripts

- `import vsblender` gives helpers that hide Blender version drift:
  - `sock(node, "Fac")` gets a socket by identifier or by name. `build(tree, {...}, links=[...])` creates and lays out a node graph.
  - `set_keys` and `scale_keys` edit keyframes. They handle layered actions, and accept paths like `"Principled BSDF/Emission Strength"`. `set_keys(..., replace=True)` rebuilds a channel; `clear_keys(target, path=None)` removes keys (all of them, and the orphaned action, without a path), so animation scripts can be re-run.
  - `material(name, color="#c8a24a", metallic=1, roughness=0.3)` makes or updates a Principled material and its viewport colour. Names like `"brass"`, `"steel"`, `"red"` work as colours.
  - `modifier(obj, "BEVEL", width=0.002)` gets or creates a modifier by name, so re-runs do not stack them. `apply_modifiers(obj)` bakes them without operators.
  - `place_on_ground(objs)`, `center_on_origin(objs)`, `set_origin(obj, "base")`, `orient_flat(obj)`, `stats(obj)`.
  - `units()`, `mm(20)`, `m(1.5)`, `to_mm(x)`: what a Blender unit is, and real sizes in Blender units. `printer()` is the printer profile.
  - `bbox_world`, `raycast_down` and `view3d_override` cover bounds, ground contact and operators that need a 3D view.
  - `progress(fraction, message)` reports progress, and lets the client cancel the script.
  - `spec(obj, teeth=24, pitch_deg=15)` records the parameters an object was built from; `mark_derived(obj, sources)` records what it is generated from. Objects a script builds are stamped with the script automatically. `intent("...")` writes to NOTES' Intent & constraints.
  - `part("dhd", keys_per_ring=18)` builds a reusable part from `parts/dhd.py` (`def build(**params)`, `PARAMS` defaults) in the script's folder or above it, and records the part and its parameters on the objects.
  - `read_stl`, `read_obj` (keeps OBJ materials and groups as parts), `read_3mf`, `svg_loops(path)` read reference files. `svg_loops` reads simple CSS classes. `mode="fill"` warns when it drops strokes. A repeated path point is dropped.
  - `aim(ob, target)` points an object when the view runs along world Y. `dial(current, target, clockwise=, min_travel=)` is the unwrapped travel for a spinning dial. `ref_section(name, plane=, ring=)` sections a registered reference.
- `from vsblender import geo` builds meshes. Solids are values: nothing exists in Blender until `to_object`.
  - Primitives stand on z = 0, centred in x and y: `geo.box(x, y, z, fillet=, chamfer=, edges="vertical")`, `cylinder(d=, h=)`, `cone`, `tube`, `sphere`, `torus`, `revolve(profile)`, `extrude(loops, h)`, `sweep(profile, path)`, `loft(sections)`, `text("A1", size, depth)`.
  - Mechanical parts (sizes from ISO tables): `thread(m=8, length=)`, `hole(m=3, depth=, fit="normal"|"tap"|"insert", counterbore=True)`, `nut_trap(3)`. `m=` sizes are millimetres; every other length is in scene units.
  - `a + b`, `a - b`, `a & b` are booleans (Manifold solver when the inputs are closed). `geo.join(a, b)` combines without a boolean, which is enough for render and game models. A cutter whose shells overlap each other: `a.difference(b, union_cutters=True)` (the warning says when). `.clean()` removes the zero-area slivers booleans leave, without welding shells.
  - `.move()`, `.rotate(x=, y=, z=)`, `.scale()`, `.mirror()`, `.bevel(width)`, `.array(n, offset)`, `.polar(n)`, `.on_ground()`, `.check()`.
  - `.to_object("Name", materials=[...], modifiers=[("BEVEL", {...})], smooth_deg=30, uv="box")` creates the object or updates it in place: material slots, modifiers and animation survive, and per-face material indices are kept when `materials=` replaces the slots. `parent="Root", space="local"` writes geometry in the parent's frame and never touches a keyed transform. A fillet uses a handful of segments unless `segments=` is passed; on a cylinder `segments=` is the circle.
  - Rings, dials, gates and wheels: `ring = geo.ring(up="+Y", front="+Z")` works in clock angles (clockwise from the top, seen from the front): `ring.pt(a, r, depth)`, `ring.theta(a)` for `lathe`, `ring.on(depth_fn, a)` as a `prism` mapping, `ring.band(bm, a0, a1, r0, r1, depth_fn, lo, hi)`, `ring.pattern(segment, 9, mirror="back", symmetric=True)`, `ring.svgs(bm, paths, radius, height, z0, z1)` and `ring.chevron(bm, angle, outer, inner)`. `geom2d.arch(width, length)` is a window outline (straight sides, round outer end).
  - For detailed shapes, builders add faces to a BMesh: `geo.lathe(bm, profile)`, `geo.prism(bm, loops, h0, h1, to3d)`, `geo.sweep(bm, ...)`, `geo.polar_block`, `geo.mirror_weld`, `geo.finish(bm)`, `geo.replace_mesh(obj, bm)`.
  - Let parts that are joined or cut overlap a little (0.1 mm in a print project): coplanar faces make booleans fail. Emboss text by sinking it into the surface.
- Every lib/ folder from the script's folder up to the workspace root is importable, as are libPaths and sharedLibs (another project's script folders). Edits are picked up on the next call.
- Keep build code in functions and the run code under `if __name__ == "__main__":` (or `if vsblender.is_main():`), so another project can import the builder without running it.
- `result` is a global, or the value `main()` returns. A `result = ...` inside `main()` stays local unless the function says `global result`.
- Header lines: `# vsblender: read-only` (no checkpoint; a `frame_set` is put back and not journaled), `# vsblender: atomic off` (keep partial changes on failure), `# vsblender: rerun-after 03_mesh` (this script repairs what 03_mesh resets), `# vsblender: reads HEIGHT` and `# vsblender: exports HEIGHT` (a stale warning names the value that changed). A `pipeline.json` next to the scripts (`{"steps": [...], "after": {"03_mesh": ["07_animation"]}}`) makes `run_script` say which steps are now out of date; `run_pipeline` re-runs them as one transaction.
- Call `vsblender.progress()` in long loops. On a timeout or a cancel, the script stops at its next call. Use `render` for renders, not `bpy.ops.render.render` in a script, and the `save` tool for saving, not `bpy.ops.wm.save_mainfile`.

## 3D printing

- Start with `new_blend` template `print_mm`: 1 unit = 1 mm, and the build plate is outlined at the origin.
- The printer comes from `"printer"` in `.blender-ai/config.json`: a preset (`bambu_p1s`, `bambu_x1c`, `bambu_a1`, `bambu_a1_mini`, `prusa_mk4`, `prusa_core_one`, `prusa_mini`, `creality_ender3`, `generic_220`) or an object with `preset`, `buildVolume`, `nozzle`, `layerHeight`, `minWall`, `maxOverhangDeg`, `material`, `holeCompensation`. Tools accept `printer` per call.
- In a print project, `geo` adds the printer's hole compensation to holes, gives threads clearance, and warns about threads finer than the layers allow.
- `check_model` with `purpose: "print"` must show no errors before export. `suggest_orientation: true` ranks orientations; `vsblender.orient_flat(obj)` turns a part onto its largest flat face.
- `export_model` writes 3MF by default for print, in millimetres, placed on the bed and centred, each part named: Bambu Studio, OrcaSlicer and PrusaSlicer open it directly. STL is there for other slicers.

## Sidecar layout

```
.blender-ai/<name>/
  NOTES.md        overview. Hand-written text after the generated marker is kept.
  manifest.json   structure. Diff it to see what changed.
  roles.json      what each object is for
  journal.jsonl   ingests, AI scripts (reason, script, changes, checkpoint), pipelines, saves, appends, exports, roles, restores
  checkpoints/    compressed copies of the session from before AI scripts, within a disk budget
  references.json reference meshes by name, with units and transform (import_reference writes it)
  refs/           cached reference parts (import_reference)
  previews/       generated images, not the source of truth
```

Reviewed roles (`source` `ai` or `human`) survive a re-ingest, and a rename keeps them. Everything between the `ai:generated` markers in `NOTES.md` is rewritten.

## Scene rules

- Lengths in and out of tools are Blender units; `session_info.units` says what one unit is. Most scenes are 1 unit = 1 m, print projects 1 unit = 1 mm. Angles are degrees. Blender is Z-up. Front looks along +Y from -Y.
- `run_script` starts from a fresh Python namespace each call, with `bpy`, `mathutils` and `vsblender` defined.
- `run_script` runs on Blender's main thread from a timer, not from a 3D view. Prefer `bpy.data`, `bmesh` and `vsblender.geo` over `bpy.ops`. An operator that needs a view needs `with vsblender.view3d_override():`.
- Do not change `scene.render`, the compositor, or the user's viewport to take a picture. Use `preview` or `render`.
- Do not invent Blender 4.x API. `session_info` includes the gotchas for the running version, and `api` and `node_schema` answer from the running Blender. On Blender 5 the engine id is `BLENDER_EEVEE`, `Material.use_nodes` is gone, actions are slotted, and the compositor is `scene.compositing_node_group`.
- Socket identifiers and displayed names differ (`Fac` versus `Factor`). Use the identifier.
- Material keyframes address nodes by name. Keep a keyframed node's name when you rebuild a material.
- Objects marked `vsblender_stage` (the render template's floor, the print template's build plate) belong in the file but not in checks and exports. Objects marked `vsblender_reference`, or named `_REF ...`, are measuring references: keep them out of the file (`import_reference`).
- After `mesh.transform` or a scale change, `dimensions` and `bound_box` stay stale until `bpy.context.view_layer.update()`. Read sizes with `vsblender.stats`, or update the view layer first. A check that runs too early will scale the mesh again.
- Changing `scale_length` does not reframe the view. Set `view_distance` in the new Blender units. `view3d_override()` is the first 3D view; frame every workspace by walking `window.screen.areas`.

## Clients

The setup wizard writes this guide for the clients that were switched on. Cursor and Cline are off unless the user enabled them. Grok uses this file when it is `CLAUDE.md`, and `AGENTS.md` only when Claude Code was left off, so the guide is not injected twice.

Setup also writes Claude Code permission rules: the read-only tools run without asking, and `.blend` files cannot be read or edited. `run_script` and `run_pipeline` ask each time unless the user ticked "run scripts without asking" in setup; `run_project_script` and `run_project_pipeline` have their own tick for trusted folders, and `save` its own tick.
