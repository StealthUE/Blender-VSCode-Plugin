# VSBlender

This workspace is driven through the **vsblender** MCP server and the sidecar files under `.blender-ai/`. The `.blend` file is a binary. Do not read it, search it, or edit it. If you are changing the VSBlender extension source itself, that guidance is about the `.blend` files only.

The bridge listens on `127.0.0.1:{{PORT}}`. Most tools need Blender open with the VSBlender add-on running; if a tool cannot connect, call `doctor`, then `launch_blender`. Read-only tools (`describe`, `find`, `spatial`, `measure`, `timeline`, `check_model`, `preview`, `export_model`) also work with Blender closed: they read the saved file in a background Blender and say `source: saved file`.

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
3. Put Python in `<blend folder>/scripts/*.py` (shared code in `<blend folder>/scripts/lib/`) and run it with `run_script`, and always pass a `reason`. Each call is checkpointed, journaled and made one undo step, and a failed or cancelled run is rolled back. The reply says what changed, by category, and warns when animated data was lost.
4. Check the result with `preview`, or with `compare` against the checkpoint from before the script. Use `render` for a final image with the scene's own settings.
5. If a pass goes wrong, use `restore_checkpoint` with the id from the `run_script` reply, or `last`.

## Building a model

1. A new model starts with `new_blend`: `empty` (metres), `render` (metres; camera, three lights, floor), `game` (metres), or `print_mm` (1 unit = 1 mm, with the printer's build plate). It never overwrites a file.
2. Write a script with `from vsblender import geo` (below) and `vsblender.material`, and run it. Re-running it updates the same objects in place.
3. Look at it with `preview` (solid shading shows the materials' colours, cavity and outlines; `views`, `crop`, `region`, `material` for one material on a sphere).
4. `check_model` with the model's purpose: `general`, `render`, `game` or `print`. It returns an image with the problem faces coloured. Fix what it reports and check again.
5. `export_model`: 3MF or STL for a slicer, GLB/glTF, FBX, USD, OBJ or PLY for other tools. Files go to `<blend folder>/exports/`; nothing is overwritten unless `overwrite: true`.

## Tools

| Tool | Use it for |
|---|---|
| `doctor` | Read-only check of config, Blender, the add-on, the bridge, the printer profile, and each `.blend`'s ingest status. |
| `doctor_fix` | Reinstall the add-on and rewrite client config. Restart Blender afterwards. |
| `launch_blender` / `launch_blender_background` | Open Blender with the bridge, with a window or headless. Never starts a second Blender while one holds the port. |
| `new_blend` | A new `.blend` from a template (`empty`, `render`, `game`, `print_mm`), made in a background Blender. |
| `session_info` | Live file, dirty flag, frame, units, template, engine, render devices, sidecar status, recent checkpoints, printer, and gotchas for this Blender version. |
| `ingest` | Write `.blender-ai/<name>/` from the saved file (headless), or from the open session with `live: true`. |
| `context_pack` | `NOTES.md` cut to a token budget. Reports what was cut. |
| `run_script` | Run a workspace `.py` in Blender, with a `reason`. Blocks Blender's UI while it runs. Rolled back on failure (`atomic: false` keeps partial changes). |
| `run_pipeline` | Run the steps of a `pipeline.json` from one step to another as one checkpoint and one undo step. |
| `run_project_script` | `run_script` for files in the trusted folders of `.blender-ai/config.json` only. |
| `preview` | Offscreen image. `views`, `crop`, `region`, `overlay` (reference objects or STL/OBJ/3MF/SVG files as wire, x-ray or silhouette), `material`, `cavity`, `matcap`, `aspect`, `isolate`, `projection`, `compositor`, `dof`, `samples`. |
| `check_model` | Fitness for a purpose: open, non-manifold, flipped, coincident and degenerate faces, transforms, triangle budget, materials, UVs; for print: fits the printer, self-intersections, bed contact, floating parts, overhangs, thin walls, filament. |
| `export_model` | 3MF/STL in mm (on the bed for print), or GLB, glTF, FBX, USD, OBJ, PLY through Blender's exporters on a copy. |
| `measure` | Sections, profiles, depth maps, angular occupancy and repeat pitch, of scene objects or of external mesh files. |
| `timeline` | Every keyframe in time order with readable channels, or a channel's values at given frames. |
| `render` / `job_status` / `cancel_job` | Final render as a background job on a copy of the session: `still`, `frames`, `sheet` (16 frames at most), or `mp4`. `frames` is a range or a list. |
| `checkpoint` / `restore_checkpoint` | Compressed snapshots in `.blender-ai/<name>/checkpoints/`. The user's file is not touched. |
| `diff` | What changed between `sidecar`, `live` and checkpoint ids. |
| `compare` / `reference` | Before and after with a difference heatmap. Reference images next to a matching preview. |
| `describe` / `find` / `spatial` | One datablock in full (with the script that built it and its spec). Objects by selector (`type:MESH and children_of:"X"`, `built_by:lamp.py`). Bounds, raycasts, drops, distances. |
| `api` / `node_schema` | Blender API lookup, including live enum values. A node's sockets by identifier and name. |
| `set_role` | Record what an object is for in `roles.json`. It survives re-ingests. |

## Scripts

- `import vsblender` gives helpers that hide Blender version drift:
  - `sock(node, "Fac")` gets a socket by identifier or by name. `build(tree, {...}, links=[...])` creates and lays out a node graph.
  - `set_keys` and `scale_keys` edit keyframes. They handle layered actions, and accept paths like `"Principled BSDF/Emission Strength"`.
  - `material(name, color="#c8a24a", metallic=1, roughness=0.3)` makes or updates a Principled material and its viewport colour. Names like `"brass"`, `"steel"`, `"red"` work as colours.
  - `modifier(obj, "BEVEL", width=0.002)` gets or creates a modifier by name, so re-runs do not stack them. `apply_modifiers(obj)` bakes them without operators.
  - `place_on_ground(objs)`, `center_on_origin(objs)`, `set_origin(obj, "base")`, `orient_flat(obj)`, `stats(obj)`.
  - `units()`, `mm(20)`, `m(1.5)`, `to_mm(x)`: what a Blender unit is, and real sizes in Blender units. `printer()` is the printer profile.
  - `bbox_world`, `raycast_down` and `view3d_override` cover bounds, ground contact and operators that need a 3D view.
  - `progress(fraction, message)` reports progress, and lets the client cancel the script.
  - `spec(obj, teeth=24, pitch_deg=15)` records the parameters an object was built from; `mark_derived(obj, sources)` records what it is generated from. Objects a script builds are stamped with the script automatically.
  - `read_stl`, `read_obj`, `read_3mf`, `svg_loops(path)` read reference files.
- `from vsblender import geo` builds meshes. Solids are values: nothing exists in Blender until `to_object`.
  - Primitives stand on z = 0, centred in x and y: `geo.box(x, y, z, fillet=, chamfer=, edges="vertical")`, `cylinder(d=, h=)`, `cone`, `tube`, `sphere`, `torus`, `revolve(profile)`, `extrude(loops, h)`, `sweep(profile, path)`, `loft(sections)`, `text("A1", size, depth)`.
  - Mechanical parts (sizes from ISO tables): `thread(m=8, length=)`, `hole(m=3, depth=, fit="normal"|"tap"|"insert", counterbore=True)`, `nut_trap(3)`. `m=` sizes are millimetres; every other length is in scene units.
  - `a + b`, `a - b`, `a & b` are booleans (Manifold solver when the inputs are closed). `geo.join(a, b)` combines without a boolean, which is enough for render and game models.
  - `.move()`, `.rotate(x=, y=, z=)`, `.scale()`, `.mirror()`, `.bevel(width)`, `.array(n, offset)`, `.polar(n)`, `.on_ground()`, `.check()`.
  - `.to_object("Name", materials=[...], modifiers=[("BEVEL", {...})], smooth_deg=30, uv="box")` creates the object or updates it in place: material slots, modifiers and animation survive.
  - For detailed shapes, builders add faces to a BMesh: `geo.lathe(bm, profile)`, `geo.prism(bm, loops, h0, h1, to3d)`, `geo.sweep(bm, ...)`, `geo.polar_block`, `geo.mirror_weld`, `geo.finish(bm)`, `geo.replace_mesh(obj, bm)`.
  - Let parts that are joined or cut overlap a little (0.1 mm in a print project): coplanar faces make booleans fail. Emboss text by sinking it into the surface.
- Every lib/ folder from the script's folder up to the workspace root is importable, as are libPaths. Edits are picked up on the next call.
- Header lines: `# vsblender: read-only` (no checkpoint), `# vsblender: atomic off` (keep partial changes on failure), `# vsblender: rerun-after 03_mesh` (this script repairs what 03_mesh resets). A `pipeline.json` next to the scripts (`{"steps": [...], "after": {"03_mesh": ["07_animation"]}}`) makes `run_script` say which steps are now out of date; `run_pipeline` re-runs them as one transaction.
- Call `vsblender.progress()` in long loops. On a timeout or a cancel, the script stops at its next call. Use `render` for renders, not `bpy.ops.render.render` in a script.

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
  journal.jsonl   ingests, AI scripts (reason, script, changes, checkpoint), pipelines, exports, roles, restores
  checkpoints/    compressed copies of the session from before AI scripts
  references.json optional: reference meshes for preview overlays ({ref: "name"})
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
- Saving the `.blend` is the user's decision. Do not save it unless you were asked to. Checkpoints keep unsaved work safe in the meantime.

## Clients

The setup wizard writes this guide for the clients that were switched on. Cursor and Cline are off unless the user enabled them. Grok uses this file when it is `CLAUDE.md`, and `AGENTS.md` only when Claude Code was left off, so the guide is not injected twice.

Setup also writes Claude Code permission rules: the read-only tools run without asking, and `.blend` files cannot be read or edited. `run_script` and `run_pipeline` ask each time unless the user ticked "run scripts without asking" in setup; `run_project_script` has its own tick for trusted folders.
