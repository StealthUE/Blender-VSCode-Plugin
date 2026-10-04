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
const targets = { description: "Objects: a name, a list of names, or a find selector (\"type:MESH and children_of:Lamp\"). Default: the visible mesh, curve and text objects, without references (vsblender_reference) and boolean cutters." };
const overlay = {
  type: "array",
  items: { type: "object" },
  description: "Draw other geometry over the image, from a second pass, so hidden, wire-display or external references show: [{\"objects\": \"_REF *\" | \"file\": \"refs/part.stl\" (STL, OBJ, 3MF, SVG; transform {location, rotation_deg, scale, units: mm}) | \"ref\": name in references.json, \"style\": \"wire\" | \"xray\" | \"silhouette\", \"color\": \"#ff3010\", \"opacity\": 0.35}].",
};

const viewProps = {
  view: { type: "string", description: "camera, front, back, left, right, top, bottom, iso. Default iso." },
  shading: { type: "string", description: "solid (Workbench, with cavity and outlines), material (EEVEE look-dev), rendered (the scene's engine and settings). Default solid." },
  target: { type: "string", description: "Object to frame. Its children are included." },
  isolate: { type: "boolean", description: "Show only target and its children (plus lights). Default false: neighbours stay visible." },
  frame: { type: "number", description: "Frame to show, e.g. 120 for the end of the dial sequence. The user's frame is restored afterwards." },
  region: { type: "object", description: "Frame a part of the scene instead: {center: [x, y, z], radius} or {min: [...], max: [...]}, scene units." },
  crop: { type: "array", items: { type: "number" }, description: "Zoom into part of the framed image at full resolution: [x0, y0, x1, y1] as fractions, (0, 0) top left." },
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
    description: "Repair: reinstall the add-on into Blender (runs a headless Blender, 10-60 s) and rewrite the MCP config and guide files. Restart Blender afterwards to load the new add-on.",
    inputSchema: obj({}),
  },
  {
    name: "session_info",
    description: "Live state of the open Blender: version, file, dirty flag, scene, frame, units (what one Blender unit is), template, engine, render devices, recent checkpoints, whether the sidecar describes this session, the printer profile, and gotchas for this Blender version. Read-only.",
    inputSchema: obj({}),
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
    description: "Run a workspace .py inside the open Blender, on its main thread: Blender's UI is blocked until it returns. Takes an automatic checkpoint first (dropped when nothing changed), makes the run one undo step, appends a journal entry and a NOTES change-log line, and reports what changed by category (added, removed, recreated, renamed, modified materials/worlds/scene settings/keys, lost animation). Atomic: if the script raises or is cancelled, the session is rolled back to that checkpoint. Objects it builds are stamped with the script (ai_built_by). The namespace is fresh each call; `import vsblender` for helpers, `from vsblender import geo` for modelling. Importable: the script's folder, every lib/ from there up to the workspace root, and libPaths. Header lines: `# vsblender: read-only`, `# vsblender: atomic off`, `# vsblender: rerun-after <script>`. Set `result`, or pass function and args.",
    inputSchema: obj({
      path: { type: "string", description: "Workspace .py file. Keep model scripts next to their .blend: <blend folder>/scripts/, shared code in <blend folder>/scripts/lib/." },
      reason: { type: "string", description: "Why: goes into the journal and the NOTES change log. Give one for every change." },
      function: { type: "string" },
      args: { type: "object" },
      timeout_ms: { type: "number", description: "Default 60000, max 600000. On timeout the script is asked to stop at its next vsblender.progress() call." },
      checkpoint: { type: "boolean", description: "Default true (from setup). false skips the automatic checkpoint (and with it the rollback on failure)." },
      atomic: { type: "boolean", description: "Default true: a failed or cancelled run is rolled back. false keeps the partial changes." },
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
    }, ["path"]),
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
    }),
  },
  {
    name: "preview",
    description: "Offscreen image. Never moves the user's viewport, camera or render settings. Axis views are orthographic, iso and camera are perspective. view camera uses the scene camera's aspect. Without a target, the frame is set on the main subject. Compositor, depth of field and motion blur are off unless asked for. region and crop frame a detail; overlay draws references (objects, or STL/OBJ/3MF/SVG files) as wire, x-ray or silhouette; material shows one material on a sphere. Images that would be too large come back as a smaller JPEG. Works on the saved file when Blender is not running. Not kept unless save=true.",
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
      path: blendPath,
    }),
  },
  {
    name: "check_model",
    description: "Is the model fit for its purpose? Read-only. purpose general/render/game/print decides which checks run and how serious each is: open edges are fine for a render, a warning for a game asset, an error for a print. Checks the evaluated meshes (modifiers applied): open edges and holes, non-manifold edges, flipped or inside-out normals, zero-area and coincident (z-fighting) faces, unapplied transforms, triangle budget, materials, UVs, flat shading, unit mix-ups. For print, in mm against the printer profile: fits the build volume, self-intersections, bed contact, parts floating in mid-air, overhangs over the limit, thin walls (ray samples), and a rough filament estimate; suggest_orientation ranks orientations. Returns an image with problem faces coloured (temporary copies only). Works on the saved file when Blender is not running.",
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
    description: "Final render with the scene's own settings, as a background job: the session is copied, and a separate headless Blender renders the copy, so Blender's UI stays usable and overrides never reach the scene. Waits up to wait_seconds, then returns a job id for job_status. Writes images (or an .mp4) in the workspace.",
    inputSchema: obj({
      frame: { type: "number", description: "One frame. Default: the current frame." },
      frames: { description: "{start, end, step} for a range, or a list such as [1, 36, 216, 260] (not for mp4)." },
      as: { type: "string", description: "still (default for one frame), frames, sheet (one labelled contact sheet, at most max_tiles frames), mp4." },
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
    description: "Blender Python API lookup, compact (identifier: TYPE = default {enum items}). query: a type (ShaderNodeTexSky), Type.property (ColorManagedViewSettings.look), a live path (scene.view_settings.look, scene.eevee, object.data), or a word to search. Live paths list dynamic enum values RNA leaves out (looks, engines). Read-only.",
    inputSchema: obj({ query: { type: "string" } }, ["query"]),
  },
  {
    name: "node_schema",
    description: "A node's properties and sockets, with socket identifier and displayed name (Fac \"Factor\"), for given settings, e.g. ShaderNodeMix with {\"data_type\": \"RGBA\"}. Adds a scratch node group and removes it.",
    inputSchema: obj({ bl_idname: { type: "string" }, props: { type: "object" } }, ["bl_idname"]),
  },
  {
    name: "describe",
    description: "Everything about one datablock: an object (transform, world bounds, data, modifiers, constraints, materials, animation with readable channel names, role, which script built it, its spec, what uses it), or MA:/WO:/NT:/CO:/ME:/AC: ids. Read-only. Works on the saved file when Blender is not running.",
    inputSchema: obj({ target: { type: "string", description: "Name, or a typed id such as MA:SG Stone." }, path: blendPath }, ["target"]),
  },
  {
    name: "find",
    description: "Objects matching a selector, with why each matched. Terms: name glob (\"SG Rock *\"), type:MESH, collection:X, material:X, parent:X, children_of:X, role~text, has:modifier[:TYPE], has:constraint, prop:key[=value], built_by:script, animated, emissive, visible, hidden, selected, active, derived, built, reference, within:[x0,y0,z0,x1,y1,z1], near:\"X\"<2 (scene units). Combine with and, or, not, ( ). Read-only. Works on the saved file when Blender is not running.",
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
    description: "Shapes, not just bounds. Read-only. op section (outline of a slice: plane {axis: z, at: 1.2} or {point, normal}; width, depth, area, points), profile (min/max of one axis binned along another or along a radius: a ring's cross-section; value, along, axis, center, bins), depthmap (a height map seen from view top/front/...: ASCII rows and an image; res up to 128), angular (occupancy against angle about an axis within radius [r0, r1] and height bands: notches, gaps), pitch (the repeat period of that pattern). On scene objects (targets) or an external file (file + transform {location, rotation_deg, scale, units: mm}) without importing it; compare_to measures a second source the same way and reports differences. Scene units. Works on the saved file when Blender is not running.",
    inputSchema: obj({
      op: { type: "string" },
      targets,
      file: { type: "string", description: "STL, OBJ, 3MF or SVG in the workspace." },
      transform: { type: "object" },
      plane: { type: "object" },
      value: { type: "string" },
      along: { type: "string" },
      axis: { type: "string" },
      center: { type: "array", items: { type: "number" } },
      bins: { type: "number" },
      radius: { type: "array", items: { type: "number" } },
      height: { type: "array", items: { type: "number" } },
      view: { type: "string" },
      res: { type: "number" },
      compare_to: { description: "Another source: {targets} or {file, transform}." },
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
    description: "Reference images next to a matching preview of the open session, to check accuracy against a real design. images: workspace paths or https URLs the user gave (downloaded, images only, 15 MB max). For a reference mesh (STL, OBJ, 3MF), use preview with overlay instead.",
    inputSchema: obj({
      images: { type: "array", items: { type: "string" } },
      ...viewProps,
      size: { type: "number", description: "Default 512." },
    }, ["images"]),
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
];

/** Runs arbitrary Python in the user's Blender. Only auto-approved when the user ticks it in setup. */
export const SCRIPT_TOOL = "run_script";
/** Runs a pipeline of workspace scripts: approved together with run_script. */
export const PIPELINE_TOOL = "run_pipeline";
/** run_script restricted to trusted folders: approved on its own setup tick. */
export const TRUSTED_SCRIPT_TOOL = "run_project_script";

export function claudeRule(tool: string): string {
  return `mcp__vsblender__${tool}`;
}
