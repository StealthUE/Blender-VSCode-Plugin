/**
 * MCP tool definitions. Each description says what the tool changes and how long it can block, so a
 * client (and the person approving it) knows before calling. Tools with a mutating mode are split in
 * two, because permission rules match tool names and cannot see arguments.
 */
export interface ToolDef {
  name: string;
  description: string;
  inputSchema: Record<string, unknown>;
}

const obj = (properties: Record<string, unknown>, required: string[] = []): Record<string, unknown> => ({
  type: "object",
  properties,
  ...(required.length ? { required } : {}),
});

const blendPath = { type: "string", description: "Workspace .blend to read when Blender is not running (then the saved file is used, in a background Blender). Optional when only one exists." };
const targets = { description: "Objects: a name, a list of names, or a find selector (\"type:MESH and children_of:Lamp\"). Default: the visible mesh, curve and text objects, without references (vsblender_reference), staging (vsblender_stage: template floor, build plate) and boolean cutters." };
const overlay = {
  type: "array",
  items: { type: "object" },
  description: "Draw other geometry or a show plate with the image. Geometry: [{\"objects\": \"_REF *\" | \"file\": \"refs/part.stl\" (STL, OBJ, 3MF, SVG; part: an OBJ material/group; transform {location, rotation_deg, scale, units: mm}) | \"ref\": a references.json name (\"peg\", or \"peg/Chevron\" for one part), \"style\": \"wire\" | \"xray\" | \"silhouette\" (drawn over the image) | \"solid\" | \"cavity\" (shaded in the scene, hidden behind nearer geometry; temporary copies, never in the file), \"color\": \"#ff3010\", \"opacity\": 0.35}]. A plate: {\"image\": \"plates/chevron.png\"} or {\"video\": \"plates/dial.mp4\", \"time\": 52}, placed with box [x0, y0, x1, y1] (fractions, (0, 0) top left; default the whole frame), corners (four [x, y] fractions), or region (a 3D box projected through this camera). mode \"overlay\" (default) draws it in the frame; mode \"diff\" returns a triptych of the render, the plate and where they disagree.",
};
const saveArg = { type: "boolean", description: "Save the .blend after a successful run (journaled). Only when the user allowed it in setup (allowSave); otherwise refused." };
const allowReferences = { type: "boolean", description: "With save: save even though reference objects (vsblender_reference, _REF*) are in the scene." };

const viewProps = {
  view: { type: "string", description: "camera, front, back, left, right, top, bottom, iso. Default iso." },
  shading: { type: "string", description: "solid (Workbench, with cavity and outlines), material (EEVEE look-dev), rendered (the scene's engine and settings). Default solid." },
  target: { type: "string", description: "Object to frame. Its children are included." },
  isolate: { type: "boolean", description: "Show only target and its children (plus lights). Default false: neighbours stay visible." },
  frame: { type: "number", description: "Frame to show, e.g. 120 for the end of the dial sequence. The user's frame is restored afterwards." },
  region: { type: "object", description: "Frame a part of the scene instead: {center: [x, y, z], radius} or {min: [...], max: [...]}, scene units." },
  crop: { type: "array", items: { type: "number" }, description: "Zoom into part of the framed image at full resolution: [x0, y0, x1, y1] as fractions, (0, 0) top left." },
  camera: { type: "string", description: "Render from this camera object (view camera with another camera than the scene's)." },
  ref: { description: "Look at a registered reference (references.json; import_reference makes one) on its own, shaded, without importing it: \"peg\", \"peg/Chevron\", or a list. with_scene: true shows it in the scene instead." },
  with_scene: { type: "boolean", description: "With ref: draw the reference shaded inside the scene (occluded like the rest) instead of alone." },
  overlay,
};

const solidProps = {
  cavity: { type: "boolean", description: "Solid shading: shade creases and ridges (default true)." },
  outline: { type: "boolean", description: "Solid shading: object outlines (default true)." },
  shadow: { type: "boolean", description: "Solid shading: cast shadows (default false)." },
  matcap: { type: "string", description: "Solid shading with a matcap, e.g. basic_1.exr, clay_studio.exr, metal_carpaint.exr." },
  xray: { type: "boolean", description: "Solid shading: see-through, to show inner parts." },
  color_type: { type: "string", description: "Solid shading colours: material (default, the materials' viewport colour), object, random, single." },
};

const purpose = { type: "string", description: "general (default; print_mm files default to print, game files to game), render, game, or print." };
const printer = { description: "Printer for purpose print: a preset (bambu_p1s, bambu_x1c, bambu_a1, bambu_a1_mini, prusa_mk4, prusa_core_one, prusa_mini, creality_ender3, generic_220) or {preset, buildVolume, nozzle, layerHeight, minWall, maxOverhangDeg, material}. Default: .blender-ai/config.json \"printer\"." };

export const TOOLS: ToolDef[] = [
  {
    name: "doctor",
    description: "Read-only check of the Blender path and version, the add-on, client MCP config, whether the bridge is listening, the printer profile, and each .blend's ingest status (current, stale, new, live, diverged). Changes nothing; use doctor_fix to repair.",
    inputSchema: obj({}),
  },
  {
    name: "doctor_fix",
    description: "Repair: reinstall the add-on into Blender (runs a headless Blender, 10-60 s) and rewrite the MCP config and guide files. Restart Blender afterwards to load the new add-on. archive: true also moves orphan sidecars (a .blend that was renamed or deleted) into .blender-ai/_archive/.",
    inputSchema: obj({
      archive: { type: "boolean", description: "Move orphan sidecars to .blender-ai/_archive/. Default false: they are reported and left where they are." },
    }),
  },
  {
    name: "session_info",
    description: "Live state of the open Blender: version, file, dirty flag and unsaved runs (how many runs since the last save, since when), scene, frame, units (what one Blender unit is), template, engine, render devices, recent checkpoints, whether the sidecar describes this session, the printer profile, and gotchas for this Blender version (sent once per session). Read-only.",
    inputSchema: obj({ gotchas: { type: "string", description: "new (default: the sheet the first time per session), all (again), none." } }),
  },
  {
    name: "launch_blender",
    description: "Open Blender with its window and the bridge started, or open a file in the Blender already listening. Never starts a second Blender while one holds the port, and never discards unsaved changes (it refuses instead).",
    inputSchema: obj({ file: { type: "string", description: "Workspace .blend. Optional when only one exists." } }),
  },
  {
    name: "launch_blender_background",
    description: "Start a headless Blender (no window) with the bridge, and leave it running for later tools. Never starts a second Blender while one holds the port.",
    inputSchema: obj({ file: { type: "string", description: "Workspace .blend. Optional when only one exists." } }),
  },
  {
    name: "new_blend",
    description: "Create a new .blend in the workspace from a template, in a background Blender (the open session is not touched). Never overwrites a file. Templates: empty (metres), render (metres; camera, three-light rig, floor), game (metres; check_model defaults to purpose game), print_mm (1 unit = 1 mm, build plate outline from the printer profile; checks and exports default to purpose print). open=true opens it in Blender (refused when the open file has unsaved changes).",
    inputSchema: obj({
      path: { type: "string", description: "New workspace .blend, e.g. Models/lamp.blend." },
      template: { type: "string", description: "empty (default), render, game, print_mm." },
      printer,
      open: { type: "boolean" },
    }, ["path"]),
  },
  {
    name: "ingest",
    description: "Write .blender-ai/<name>/ (NOTES.md, manifest.json, roles.json, journal entry) so the AI can understand a file. Never saves the .blend. Default: reads the saved file in a headless Blender (5 s, about 30 s with previews). live=true: reads the open session instead, including unsaved changes, with offscreen previews; this blocks Blender's UI for a few seconds.",
    inputSchema: obj({
      path: { type: "string", description: "Workspace .blend. Optional when only one exists. Ignored with live." },
      live: { type: "boolean", description: "Describe the open Blender session, not the file on disk." },
      force: { type: "boolean", description: "Re-ingest even when nothing changed." },
      previews: { type: "boolean", description: "Render overview previews. Default true." },
      reason: { type: "string", description: "Why the scene changed; goes into the journal and change log." },
    }),
  },
  {
    name: "context_pack",
    description: "NOTES.md cut to a token budget. Before you modify and Intent & constraints are always included. focus keeps one object with its children, materials and animation. Says which sections were cut and by how much. Read-only.",
    inputSchema: obj({
      path: { type: "string" },
      budget_tokens: { type: "number", description: "Approximate token budget. Default 2000." },
      focus: { type: "string", description: "Object (with its subtree), material or name fragment to keep." },
    }),
  },
  {
    name: "run_script",
    description: "Run a workspace .py inside the open Blender, on its main thread: Blender's UI is blocked until it returns. Takes an automatic checkpoint first (dropped when nothing changed; the reply gives its size and what all checkpoints take), makes the run one undo step, appends a journal entry and a NOTES change-log line, and reports what changed by category (added, removed, recreated, renamed, modified materials/worlds/scene settings/keys, lost animation, temporary). Atomic: if the script raises or is cancelled, the session is rolled back to that checkpoint. Objects it builds are stamped with the script (ai_built_by). The namespace is fresh each call; `import vsblender` for helpers, `from vsblender import geo` for modelling. Importable: the script's folder, every lib/ from there up to the workspace root, libPaths and sharedLibs. Header lines: `# vsblender: read-only`, `# vsblender: atomic off`, `# vsblender: rerun-after <script>`, `# vsblender: reads NAME`, `# vsblender: exports NAME` (an out-of-date warning then names the values that changed). `result` is a global, or the value main() returns. Or pass function and args. background: true runs the script on a copy in a headless Blender and throws the copy away: nothing is checkpointed, journaled, or written to the open file. The reply starts with how many runs the file on disk does not have; the .blend is never saved unless save is passed (and allowed).",
    inputSchema: obj({
      path: { type: "string", description: "Workspace .py file. Keep model scripts next to their .blend: <blend folder>/scripts/, shared code in <blend folder>/scripts/lib/." },
      reason: { type: "string", description: "Why: goes into the journal and the NOTES change log. Give one for every change." },
      function: { type: "string" },
      args: { type: "object" },
      timeout_ms: { type: "number", description: "Default 60000, max 600000. On timeout the script is asked to stop at its next vsblender.progress() call." },
      checkpoint: { type: "boolean", description: "Default true (from setup). false skips the automatic checkpoint (and with it the rollback on failure)." },
      atomic: { type: "boolean", description: "Default true: a failed or cancelled run is rolled back. false keeps the partial changes." },
      save: saveArg,
      allow_references: allowReferences,
      background: { type: "boolean", description: "Run on a copy in a headless Blender and throw the copy away. Nothing is checkpointed, journaled, or written to the open file. The reply is stdout, stderr and result." },
    }, ["path"]),
  },
  {
    name: "run_project_script",
    description: "Exactly run_script, but only for scripts inside the workspace's trusted folders (\"trustedScripts\" in .blender-ai/config.json), so it can be allowed without asking while run_script still asks. Never accepts scripts under .blender-ai/.",
    inputSchema: obj({
      path: { type: "string", description: "A .py inside a trusted folder." },
      reason: { type: "string" },
      function: { type: "string" },
      args: { type: "object" },
      timeout_ms: { type: "number" },
      checkpoint: { type: "boolean" },
      atomic: { type: "boolean" },
      save: saveArg,
      allow_references: allowReferences,
      background: { type: "boolean", description: "Run on a copy in a headless Blender and throw the copy away. The open file is unchanged." },
    }, ["path"]),
  },
  {
    name: "open_blend",
    description: "Open a workspace .blend in the Blender that is already listening. Refuses when that file has unsaved changes, and refuses a path outside the workspace. Journaled as an open. To start Blender, use launch_blender.",
    inputSchema: obj({
      path: { type: "string", description: "Workspace .blend." },
      reason: { type: "string" },
    }, ["path"]),
  },
  {
    name: "replay",
    description: "The journaled script runs since the last save, in order: script, sha and reason. Lists them unless run is true, which executes them on the open file (each run is journaled again). A script whose sha no longer matches the file on disk is named and skipped unless force is true.",
    inputSchema: obj({
      run: { type: "boolean", description: "Execute the runs. Default false: list them." },
      force: { type: "boolean", description: "Run a script even when its sha does not match the file on disk." },
    }),
  },
  {
    name: "run_pipeline",
    description: "Run the steps of a pipeline.json (in a scripts folder: {\"steps\": [\"01_base\", \"02_detail\", ...], \"after\": {\"01_base\": [\"03_materials\"]}}) from one step to another, as ONE checkpoint, ONE undo step and one journal entry, rolled back if a step fails. mode affected runs only the step and the steps its after map says to re-run. Blocks Blender's UI while it runs; reports progress per step.",
    inputSchema: obj({
      pipeline: { type: "string", description: "pipeline.json (or its folder). Optional when one pipeline in the workspace has the from step." },
      from: { type: "string", description: "First step (default the first)." },
      to: { type: "string", description: "Last step (default the last)." },
      mode: { type: "string", description: "chain (default: every step from..to) or affected (from, and what its after map re-runs)." },
      reason: { type: "string" },
      atomic: { type: "boolean" },
      checkpoint: { type: "boolean" },
      timeout_ms: { type: "number", description: "Default 300000, max 600000." },
      save: saveArg,
      allow_references: allowReferences,
    }),
  },
  {
    name: "run_project_pipeline",
    description: "Exactly run_pipeline, but only for a pipeline.json inside the workspace's trusted folders (\"trustedScripts\"), whose steps are trusted scripts too: allowed without asking together with run_project_script.",
    inputSchema: obj({
      pipeline: { type: "string", description: "pipeline.json (or its folder) inside a trusted folder. Optional when one trusted pipeline has the from step." },
      from: { type: "string" },
      to: { type: "string" },
      mode: { type: "string" },
      reason: { type: "string" },
      atomic: { type: "boolean" },
      checkpoint: { type: "boolean" },
      timeout_ms: { type: "number" },
      save: saveArg,
      allow_references: allowReferences,
    }),
  },
  {
    name: "save",
    description: "Save the open .blend: the user's file, so call it when the user asked for a save (or allowed it in setup). Journaled with the reason; the watcher's re-ingest then logs the save as the AI's, pointing at its runs. Refuses while reference objects (vsblender_reference, _REF*) are in the scene unless allow_references. path saves as another workspace .blend (which becomes the open file; an existing file needs overwrite). compress defaults to true.",
    inputSchema: obj({
      reason: { type: "string", description: "Why now: what this save holds." },
      path: { type: "string", description: "Save as this workspace .blend instead." },
      compress: { type: "boolean" },
      allow_references: { type: "boolean" },
      overwrite: { type: "boolean", description: "With path: replace an existing other file." },
    }),
  },
  {
    name: "append",
    description: "Copy objects from another workspace .blend into the open one (append, not link): their meshes, materials and parents come along, and with_children (default, when the source has been ingested) their children too. Each copy records where it came from (ai_appended_from/object: describe shows it, find from:<file> selects them). One checkpoint, one undo step, journaled, like run_script. For reusing a built mesh; to rebuild a part with other parameters, use vsblender.part in a script.",
    inputSchema: obj({
      from: { type: "string", description: "Workspace .blend, e.g. TestObject/SG1/stargate.blend." },
      objects: { description: "Names or globs: [\"SG DHD*\"]." },
      collection: { type: "string", description: "Collection to put them in. Default \"Appended <file>\"." },
      with_children: { type: "boolean" },
      reason: { type: "string" },
      checkpoint: { type: "boolean" },
    }, ["from", "objects"]),
  },
  {
    name: "import_reference",
    description: "Bring a big reference (a scan, a show model, a CAD export: OBJ, FBX, glTF/GLB, STL, PLY, from any folder) into the sidecar without touching the .blend: Blender's own importer runs in a background Blender, the mesh is split into parts (by material, object, or none), and each part is written as binary STL to the sidecar (<blend folder>/.blender-ai/<blend>/refs/<name>/), plus a light overlay of the whole. Registered in references.json with units and transform, so preview overlay {ref}, preview ref= and measure ref= use it by name (\"peg\", \"peg/Chevron\"). Returns a parts table (triangles, bounds, radial bands with radial). Called again with the same file it reuses the parts and only updates transform or units. Writes only under .blender-ai/.",
    inputSchema: obj({
      file: { type: "string", description: "The reference file: absolute, or relative to the workspace." },
      name: { type: "string", description: "Its name in references.json. Default: the file name." },
      split: { type: "string", description: "material (default: one part per material, e.g. an OBJ's usemtl names), object, or none." },
      units: { type: "string", description: "What the file's numbers are: mm, cm, m, in. Default: mm for STL, m for glTF/FBX, m (assumed) for OBJ and PLY." },
      axes: { type: "string", description: "blender (default: as File > Import, Y-up files stand up in Z) or native (the file's own coordinates; not for glTF)." },
      transform: { type: "object", description: "Where it sits in the scene: {location, rotation_deg, scale}, scene units. Applied when it is used, so it can be changed later." },
      overlay_cell: { type: "number", description: "Grid (file units) the light overlay is simplified to. Default: the size / 600." },
      radial: { type: "object", description: "{axis: z, center: [x, y, z]} (file coordinates): add radius, depth and angle coverage per part to the table." },
      force: { type: "boolean", description: "Import again even when the parts are cached." },
      path: { type: "string", description: "The .blend whose sidecar gets it. Default: the open one." },
    }, ["file"]),
  },
  {
    name: "notes",
    description: "Write what the next session must know into NOTES.md's Intent & constraints section, which re-ingests keep and context_pack always includes: where the sizes live, conventions (angles, axes, units), which script to re-run after which, which nodes are keyed, what must not change. Write it after the first successful build, and when a decision changes. Writes only the sidecar.",
    inputSchema: obj({
      intent: { type: "string", description: "Markdown: short bullets." },
      mode: { type: "string", description: "append (default) or replace." },
      path: { type: "string", description: "Workspace .blend. Default: the open one." },
    }, ["intent"]),
  },
  {
    name: "preview",
    description: "Offscreen image. Never moves the user's viewport, camera or render settings. Axis views are orthographic, iso and camera are perspective. view camera uses the scene camera's aspect (camera picks another camera). Without a target, the frame is set on the main subject. Compositor, depth of field and motion blur are off unless asked for. region and crop frame a detail; overlay draws references (objects, STL/OBJ/3MF/SVG files, or references.json names) as wire, x-ray, silhouette, or shaded solid/cavity inside the scene; ref shows a registered reference on its own, shaded, without importing it; material shows one material on a sphere. Images that would be too large come back as a smaller JPEG. Works on the saved file when Blender is not running. Not kept unless save=true.",
    inputSchema: obj({
      ...viewProps,
      ...solidProps,
      material: { type: "string", description: "Preview one material on a sphere and floor (look-dev), e.g. MA:Brass. Material shading, 8 samples." },
      views: { type: "array", items: { type: "string" }, description: "Several views in one labelled contact sheet, e.g. [\"camera\", \"iso\", \"top\"]. Replaces view." },
      size: { type: "number", description: "Longest side in pixels, 64 to 4096. Default 512." },
      width: { type: "number" },
      height: { type: "number" },
      aspect: { type: "string", description: "camera, square, or a ratio like 1.777. Default camera for view camera, otherwise square." },
      projection: { type: "string", description: "ortho or persp." },
      samples: { type: "number", description: "Render samples for material/rendered. Default: the scene's, at most 32." },
      framing: { type: "string", description: "subject (default without target): leave ground planes, skies and scattered series out of the framing. all: frame every object." },
      compositor: { type: "boolean", description: "Apply the scene's compositor (glare, bloom). Blender 5+." },
      dof: { type: "boolean", description: "Use the camera's depth of field (view camera)." },
      motion_blur: { type: "boolean" },
      save: { type: "boolean", description: "Keep a copy in .blender-ai/live/." },
      name: { type: "string", description: "Keep the preview as .blender-ai/live/<camera>-<name>.png (implies save). The last few per camera are kept." },
      path: blendPath,
    }),
  },
  {
    name: "check_model",
    description: "Is the model fit for its purpose? Read-only. purpose general/render/game/print decides which checks run and how serious each is: open edges are fine for a render, a warning for a game asset, an error for a print. Checks the evaluated meshes (modifiers applied) at the current frame, or at frame: open edges and holes, non-manifold edges, flipped or inside-out normals, zero-area and coincident (z-fighting) faces, unapplied transforms, triangle budget, materials, UVs, flat shading, unit mix-ups. An object hidden by keyed visibility is reported as such (and from which frame it shows), and its own mesh is checked. For print, in mm against the printer profile: fits the build volume, self-intersections, bed contact, parts floating in mid-air, overhangs over the limit, thin walls (ray samples), and a rough filament estimate; suggest_orientation ranks orientations. Returns an image with problem faces coloured (temporary copies only). Works on the saved file when Blender is not running.",
    inputSchema: obj({
      targets,
      purpose,
      printer,
      max_tris: { type: "number", description: "Triangle budget for purpose game. Default 50000." },
      deep: { type: "boolean", description: "Raise the limits of the slow checks (self-intersection, wall samples)." },
      suggest_orientation: { type: "boolean", description: "Print: rank orientations by overhang and bed contact." },
      mm_per_unit: { type: "number", description: "Override the scene's unit for print, e.g. 1 for a file modelled at 1 unit = 1 mm with unit scale 1.0." },
      image: { type: "boolean", description: "Default true." },
      views: { type: "array", items: { type: "string" }, description: "Image views, default iso (print: iso and bottom)." },
      size: { type: "number" },
      frame: { type: "number", description: "Check the model as it is at this frame (the user's frame is restored)." },
      save: { type: "boolean" },
      path: blendPath,
    }),
  },
  {
    name: "export_model",
    description: "Write models to files in the workspace (never overwrites unless overwrite=true). 3mf and stl are written from the evaluated meshes in millimetres; for purpose print, parts are placed on the bed and centred (3MF on the bed centre), each part named, ready for Bambu Studio, OrcaSlicer and PrusaSlicer. glb/gltf, fbx, usd/usdz, obj and ply use Blender's exporters in a background Blender on a copy of the session (your selection and settings are untouched); glTF and USD are written in metres. Default format: print 3mf, otherwise glb. Default folder: <blend folder>/exports/. Journaled.",
    inputSchema: obj({
      targets,
      format: { type: "string", description: "3mf, stl, glb, gltf, fbx, obj, usd, usdz, ply." },
      purpose,
      output: { type: "string", description: "A file or folder in the workspace. Default <blend folder>/exports/<name>.<ext>." },
      split: { type: "string", description: "one (one file; default for 3mf, glb...) or objects (a file per object; default for stl and ply)." },
      overwrite: { type: "boolean" },
      place_on_bed: { type: "boolean", description: "Lowest point to z = 0 (default true for print)." },
      center: { type: "boolean", description: "Centre on the bed (3mf) or the origin (default true for print)." },
      assembly: { type: "boolean", description: "3mf: one object made of the parts, instead of separate objects." },
      ascii: { type: "boolean", description: "stl: ASCII instead of binary." },
      apply_modifiers: { type: "boolean", description: "Default true." },
      mm_per_unit: { type: "number", description: "Override the scene's unit for stl/3mf." },
      printer,
      reason: { type: "string" },
      path: blendPath,
    }),
  },
  {
    name: "render",
    description: "Final render with the scene's own settings, as a background job: the session is copied, and a separate headless Blender renders the copy, so Blender's UI stays usable and overrides never reach the scene. Waits up to wait_seconds, then returns a job id for job_status (frames rendered of frames expected). Writes images, or an .mp4 that appears at its path only when complete: with ffmpeg (found on PATH or set as \"ffmpeg\" in .blender-ai/config.json) every frame is rendered, then encoded as H.264, yuv420p, +faststart, which plays everywhere; without it, Blender's own writer. frames and mp4 without frames render the scene's whole frame range.",
    inputSchema: obj({
      frame: { type: "number", description: "One frame. Default: the current frame." },
      frames: { description: "{start, end, step} for a range, or a list such as [1, 36, 216, 260] (not for mp4). Default for frames and mp4: the scene's frame range." },
      as: { type: "string", description: "still (default for one frame), frames, sheet (one labelled contact sheet, at most max_tiles frames), mp4 (2 frames or more)." },
      keep_frames: { type: "boolean", description: "mp4 with ffmpeg: keep the rendered PNG frames next to the video." },
      max_tiles: { type: "number", description: "Most frames in a sheet. Default 16, up to 36." },
      camera: { type: "string", description: "Camera object to render from." },
      output: { type: "string", description: "Workspace path: .png for a still or sheet, .mp4 for video, a folder for frames. Default .blender-ai/renders/." },
      overrides: { type: "object", description: "Only in the copy: engine, samples, resolution_x, resolution_y, resolution_percentage, film_transparent, use_compositing, use_motion_blur, use_denoising, frame_step." },
      wait_seconds: { type: "number", description: "Default 90. 0 returns the job id at once." },
    }),
  },
  {
    name: "job_status",
    description: "State and progress of render jobs, and the image once a job is done. Without id: every job of this session. Read-only.",
    inputSchema: obj({ id: { type: "string" } }),
  },
  {
    name: "cancel_job",
    description: "Stop a render job (the headless Blender it started). Does not touch the user's Blender.",
    inputSchema: obj({ id: { type: "string" } }, ["id"]),
  },
  {
    name: "checkpoint",
    description: "Save a copy of the session to .blender-ai/<name>/checkpoints/ (save_as copy, compressed). The user's file, its path and its dirty flag do not change. run_script already checkpoints before each script; use this before a larger pass.",
    inputSchema: obj({ label: { type: "string" }, reason: { type: "string" } }, ["label"]),
  },
  {
    name: "restore_checkpoint",
    description: "Replace the open session's data with a checkpoint's (\"last\" for the newest). Checkpoints the current state first, keeps the file path, and is one undo step. The file on disk is not touched.",
    inputSchema: obj({ id: { type: "string" }, reason: { type: "string" } }, ["id"]),
  },
  {
    name: "api",
    description: "Blender Python API lookup, compact (identifier: TYPE = default {enum items}). query: a type (ShaderNodeTexSky), Type.property (ColorManagedViewSettings.look), a live path (scene.view_settings.look, scene.eevee, object.data), or a word to search. Live paths list dynamic enum values RNA leaves out (looks, engines). The properties every Node or ID has (location, width, select, users...) are left out unless inherited. Read-only.",
    inputSchema: obj({ query: { type: "string" }, inherited: { type: "boolean" } }, ["query"]),
  },
  {
    name: "node_schema",
    description: "A node's properties and sockets, with socket identifier and displayed name (Fac \"Factor\"), for given settings, e.g. ShaderNodeMix with {\"data_type\": \"RGBA\"}. Adds a scratch node group and removes it.",
    inputSchema: obj({ bl_idname: { type: "string" }, props: { type: "object" } }, ["bl_idname"]),
  },
  {
    name: "describe",
    description: "Everything about one datablock: an object (transform, world bounds, data, modifiers, constraints, materials, animation with readable channel names, keyed visibility, role, which script built it, its spec, which file it was appended from, what uses it), or MA:/WO:/NT:/CO:/ME:/AC: ids. frame describes it at that frame. Read-only. Works on the saved file when Blender is not running.",
    inputSchema: obj({
      target: { type: "string", description: "Name, or a typed id such as MA:SG Stone." },
      frame: { type: "number", description: "Describe it as it is at this frame (the user's frame is restored)." },
      path: blendPath,
    }, ["target"]),
  },
  {
    name: "find",
    description: "Objects matching a selector, with why each matched. Terms: name glob (\"SG Rock *\"), type:MESH, collection:X, material:X, parent:X, children_of:X, role~text, has:modifier[:TYPE], has:constraint, prop:key[=value], built_by:script, part:name (built by vsblender.part), from:file (appended), animated, emissive, visible, hidden, selected, active, derived, built, reference (measuring references), stage (template floor, build plate), within:[x0,y0,z0,x1,y1,z1], near:\"X\"<2 (scene units). Combine with and, or, not, ( ). Read-only. Works on the saved file when Blender is not running.",
    inputSchema: obj({ selector: { type: "string" }, limit: { type: "number" }, path: blendPath }, ["selector"]),
  },
  {
    name: "spatial",
    description: "World-space geometry questions, after modifiers. op: bbox (targets), raycast (origin, direction, max_distance), drop (points [[x,y]...], optional targets, from_z: the surface below each point), distance (a, b: gap per axis, overlap), nearest / below / above / touching (target, k, tolerance), within (min, max). targets is a list of names or a find selector. Scene units (the reply's units field; 1 BU = 1 mm in print projects). Read-only. Works on the saved file when Blender is not running.",
    inputSchema: obj({
      op: { type: "string" },
      targets: { description: "List of object names, or a find selector string." },
      target: { type: "string" },
      a: { type: "string" },
      b: { type: "string" },
      origin: { type: "array", items: { type: "number" } },
      direction: { type: "array", items: { type: "number" } },
      max_distance: { type: "number" },
      points: { type: "array", items: { type: "array", items: { type: "number" } } },
      from_z: { type: "number" },
      min: { type: "array", items: { type: "number" } },
      max: { type: "array", items: { type: "number" } },
      k: { type: "number" },
      tolerance: { type: "number", description: "touching: largest gap that counts. Default 0.5% of the target's size." },
      path: blendPath,
    }, ["op"]),
  },
  {
    name: "measure",
    description: "Shapes, not just bounds. Read-only. op section (outline of a slice: plane {axis: z, at: 1.2}, {point, normal}, or {angle: 20}: the half-plane through the ring axis, radius across and depth up; the points are in the reply, image draws the cut on a grid), profile (min/max of value binned along r (radius), angle, depth, x, y or z: a ring's cross-section), depthmap (a height map seen from view top/front/...), angular (occupancy against angle: notches, gaps, arcs; segment folds a repeat onto one), pitch (the repeat period). Frames: axis + center (angles counter-clockwise from the first other axis), or ring {up, front, center, clockwise} (clock angles from the top seen from the front, depth toward the viewer, as geo.ring). Every op takes the windows angle [a0, a1], radius [r0, r1], height (or depth) [d0, d1]. Sources: targets, file (+ part: an OBJ material or group; outside the workspace only under referenceRoots), or ref (\"peg\", \"peg/Chevron\": references.json, with its units and transform). compare_to measures a second source on the same bins and reports per-bin deltas, the largest deviation, and for sections the outline distance; image draws both. Scene units; warns when a file's units look wrong. Works on the saved file when Blender is not running.",
    inputSchema: obj({
      op: { type: "string", description: "section (default), profile, depthmap, angular, pitch." },
      targets,
      file: { type: "string", description: "STL, OBJ, 3MF or SVG." },
      part: { description: "With file or ref: the part(s) to measure, names or globs." },
      ref: { type: "string", description: "A references.json name: \"peg\" (all parts), \"peg/Chevron\" (one part)." },
      units: { type: "string", description: "The file's units when it has none: mm (default for files), cm, m, in." },
      transform: { type: "object" },
      plane: { type: "object" },
      value: { type: "string", description: "profile: what is measured (min and max): depth (ring), x, y, z, r, angle. Default z, or depth with ring." },
      along: { type: "string", description: "profile: the binned coordinate: r (radius; default), angle, depth, x, y, z." },
      axis: { type: "string" },
      center: { type: "array", items: { type: "number" } },
      ring: { type: "object", description: "{up: \"+Y\", front: \"+Z\", center: [x, y, z], clockwise: true}: clock angles, radius and depth like geo.ring." },
      angle: { type: "array", items: { type: "number" }, description: "Angle window [a0, a1] in degrees (wraps; [-20, 20] is the 40 degrees around 0)." },
      bins: { type: "number" },
      step: { type: "number", description: "angular: bin size in degrees instead of bins." },
      segment: { type: "number", description: "angular: fold every this many degrees onto one segment (from segment_start or the window start)." },
      radius: { type: "array", items: { type: "number" } },
      height: { type: "array", items: { type: "number" }, description: "Height (along the axis) or depth (ring) window [d0, d1]." },
      min: { type: "number", description: "profile: first bin edge." },
      max: { type: "number", description: "profile: last bin edge." },
      view: { type: "string" },
      res: { type: "number" },
      image: { type: "boolean", description: "section and profile: draw the result (and compare_to) on a labelled grid." },
      max_points: { type: "number", description: "section: points kept per reply (default 200; text_points shown in the text, default 60)." },
      compare_to: { description: "Another source measured the same way: {targets}, {file, transform, units}, or {ref}." },
      out: { type: "string", description: "section: a workspace .json path. Writes {loops, plane, units} (the simplified loops). An image path is used when image or depthmap draws a picture." },
      path: blendPath,
    }),
  },
  {
    name: "timeline",
    description: "Animation in time order. Read-only. op keys (default): every keyframe across objects (targets: names or a selector), with readable channel names, the value, the previous value and the easing: \"frame 36: SG Glyph Ring rotation_euler[1] = -142.000° (from -100° at 12), bezier ease out\". frames {start, end} or a list narrows it. op evaluate: target and path (rotation_euler, or \"Principled BSDF/Emission Strength\" on a material) at frames [...]. Works on the saved file when Blender is not running.",
    inputSchema: obj({
      op: { type: "string" },
      targets,
      target: { type: "string" },
      path: { type: "string", description: "evaluate: the property path." },
      frames: { description: "keys: {start, end} or a list; evaluate: a list of frames." },
      limit: { type: "number" },
    }),
  },
  {
    name: "set_role",
    description: "Record what an object is for in roles.json (source ai or human), including objects the sidecar does not know yet. Reviewed roles survive re-ingests. write_to_file=true also stamps an ai_role property on the object in Blender (one undo step).",
    inputSchema: obj({
      target: { type: "string" },
      role: { type: "string" },
      source: { type: "string", description: "ai (default) or human." },
      path: { type: "string", description: "Workspace .blend, when there is more than one." },
      write_to_file: { type: "boolean" },
    }, ["target", "role"]),
  },
  {
    name: "diff",
    description: "What changed between two states: objects, transforms, geometry, materials, scene settings, animation, renames. a and b: sidecar (the last ingest), live (the open session), or a checkpoint id. A checkpoint's manifest is built once in a headless Blender and cached. Read-only.",
    inputSchema: obj({ a: { type: "string" }, b: { type: "string" } }, ["a", "b"]),
  },
  {
    name: "compare",
    description: "Before/after images side by side with a difference heatmap. Each source is live (a preview of the open session), a checkpoint id (rendered from the checkpoint in a headless Blender), or a workspace image path. Same view settings as preview, overlay included. Read-only; no network.",
    inputSchema: obj({
      a: { type: "string", description: "First source, e.g. a checkpoint id." },
      b: { type: "string", description: "Second source. Default live." },
      ...viewProps,
      ...solidProps,
      size: { type: "number", description: "Default 512." },
      heatmap: { type: "boolean", description: "Default true." },
    }, ["a"]),
  },
  {
    name: "reference",
    description: "Reference images next to a matching preview of the open session, to check accuracy against a real design. images: workspace paths or https URLs the user gave (downloaded, images only, 15 MB max). video: frames of a reference video (workspace or referenceRoots; needs ffmpeg) as one labelled sheet, at times [...] or every 1/fps seconds from start to end, optionally cropped; diff marks in red what changed since the frame before (a small moving light). For a reference mesh, use preview with ref or overlay instead.",
    inputSchema: obj({
      images: { type: "array", items: { type: "string" } },
      video: { type: "string", description: "A video file: frames instead of images." },
      times: { type: "array", items: { type: "number" }, description: "video: seconds, e.g. [3.5, 8.3, 13.1]." },
      fps: { type: "number", description: "video: frames per second between start and end (default 1)." },
      start: { type: "number" },
      end: { type: "number" },
      diff: { type: "boolean", description: "video: mark what changed since the frame before." },
      max_tiles: { type: "number", description: "video: most frames in the sheet. Default 16, up to 36." },
      ...viewProps,
      size: { type: "number", description: "Default 512." },
    }),
  },
];

export const TOOL_NAMES = TOOLS.map((tool) => tool.name);

/**
 * Safe to run without asking: they read, or write only VSBlender's own files under .blender-ai/
 * (sidecar notes, previews, checkpoints). Setup writes these as Claude Code allow rules.
 */
export const AUTO_APPROVE = [
  "doctor",
  "session_info",
  "context_pack",
  "ingest",
  "preview",
  "describe",
  "find",
  "spatial",
  "measure",
  "timeline",
  "check_model",
  "api",
  "node_schema",
  "job_status",
  "cancel_job",
  "diff",
  "compare",
  "checkpoint",
  "launch_blender",
  "notes",
];

/** Runs arbitrary Python in the user's Blender. Only auto-approved when the user ticks it in setup. */
export const SCRIPT_TOOL = "run_script";
/** Runs a pipeline of workspace scripts: approved together with run_script. */
export const PIPELINE_TOOL = "run_pipeline";
/** run_script restricted to trusted folders: approved on its own setup tick. */
export const TRUSTED_SCRIPT_TOOL = "run_project_script";
/** run_pipeline restricted to trusted folders: approved with run_project_script. */
export const TRUSTED_PIPELINE_TOOL = "run_project_pipeline";
/** Saves the user's .blend: only auto-approved when the user ticks it in setup. */
export const SAVE_TOOL = "save";

export function claudeRule(tool: string): string {
  return `mcp__vsblender__${tool}`;
}
