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

const viewProps = {
  view: { type: "string", description: "camera, front, back, left, right, top, bottom, iso. Default iso." },
  shading: { type: "string", description: "solid (Workbench), material (EEVEE look-dev), rendered (the scene's engine and settings). Default solid." },
  target: { type: "string", description: "Object to frame. Its children are included." },
  isolate: { type: "boolean", description: "Show only target and its children (plus lights). Default false: neighbours stay visible." },
  frame: { type: "number", description: "Frame to show, e.g. 120 for the end of the dial sequence. The user's frame is restored afterwards." },
};

export const TOOLS: ToolDef[] = [
  {
    name: "doctor",
    description: "Read-only check of the Blender path and version, the add-on, client MCP config, whether the bridge is listening, and each .blend's ingest status (current, stale, new, live, diverged). Changes nothing; use doctor_fix to repair.",
    inputSchema: obj({}),
  },
  {
    name: "doctor_fix",
    description: "Repair: reinstall the add-on into Blender (runs a headless Blender, 10-60 s) and rewrite the MCP config and guide files. Restart Blender afterwards to load the new add-on.",
    inputSchema: obj({}),
  },
  {
    name: "session_info",
    description: "Live state of the open Blender: version, file, dirty flag, scene, frame, units, engine, render devices per engine, recent checkpoints, whether the sidecar describes this session (sidecar.status), and gotchas for this Blender version. Read-only.",
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
    description: "Run a workspace .py inside the open Blender, on its main thread: Blender's UI is blocked until it returns. Takes an automatic checkpoint first (dropped when nothing changed), makes the run one undo step, appends a journal entry and a NOTES change-log line, and reports what changed by category (added, removed, recreated, renamed, modified materials/worlds/scene settings/keys...). The namespace is fresh each call; `import vsblender` for helpers; scripts/lib is importable. Set `result`, or pass function and args.",
    inputSchema: obj({
      path: { type: "string", description: "Workspace .py file." },
      reason: { type: "string", description: "Why: goes into the journal and the NOTES change log. Give one for every change." },
      function: { type: "string" },
      args: { type: "object" },
      timeout_ms: { type: "number", description: "Default 60000, max 600000. On timeout the script is asked to stop at its next vsblender.progress() call." },
      checkpoint: { type: "boolean", description: "Default true (from setup). false skips the automatic checkpoint." },
    }, ["path"]),
  },
  {
    name: "preview",
    description: "Offscreen PNG. Never moves the user's viewport, camera or render settings. Axis views are orthographic, iso and camera are perspective. view camera uses the scene camera's aspect. Without a target, the frame is set on the main subject (ground planes and scatter left out of the framing). Compositor (glare, bloom), depth of field and motion blur are off unless asked for. The image is returned, not kept, unless save=true.",
    inputSchema: obj({
      ...viewProps,
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
    }),
  },
  {
    name: "render",
    description: "Final render with the scene's own settings, as a background job: the session is copied, and a separate headless Blender renders the copy, so Blender's UI stays usable and overrides never reach the scene. Waits up to wait_seconds, then returns a job id for job_status. Writes images (or an .mp4) in the workspace.",
    inputSchema: obj({
      frame: { type: "number", description: "One frame. Default: the current frame." },
      frames: { type: "object", description: "{start, end, step} for an animation." },
      as: { type: "string", description: "still (default for one frame), frames, sheet (one labelled contact sheet), mp4." },
      camera: { type: "string", description: "Camera object to render from." },
      output: { type: "string", description: "Workspace path: .png for a still or sheet, .mp4 for video, a folder for frames. Default .blender-ai/renders/." },
      overrides: { type: "object", description: "Only in the copy: engine, samples, resolution_x, resolution_y, resolution_percentage, film_transparent, use_compositing, use_motion_blur, use_denoising." },
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
    description: "Save a copy of the session to .blender-ai/<name>/checkpoints/ (save_as copy). The user's file, its path and its dirty flag do not change. run_script already checkpoints before each script; use this before a larger pass.",
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
    description: "Everything about one datablock from the live session: an object (transform, world bounds, data, modifiers, constraints, materials, animation with readable channel names, role, what uses it), or MA:/WO:/NT:/CO:/ME:/AC: ids. Read-only.",
    inputSchema: obj({ target: { type: "string", description: "Name, or a typed id such as MA:SG Stone." } }, ["target"]),
  },
  {
    name: "find",
    description: "Objects matching a selector, with why each matched. Terms: name glob (\"SG Rock *\"), type:MESH, collection:X, material:X, parent:X, children_of:X, role~text, has:modifier[:TYPE], has:constraint, prop:key[=value], animated, emissive, visible, hidden, selected, active, derived, within:[x0,y0,z0,x1,y1,z1], near:\"X\"<2. Combine with and, or, not, ( ). Read-only.",
    inputSchema: obj({ selector: { type: "string" }, limit: { type: "number" } }, ["selector"]),
  },
  {
    name: "spatial",
    description: "World-space geometry questions, after modifiers. op: bbox (targets), raycast (origin, direction), drop (points [[x,y]...], optional targets: the surface below each point), distance (a, b: gap per axis, overlap), nearest / below / above / touching (target, k), within (min, max). targets is a list of names or a find selector. Metres. Read-only.",
    inputSchema: obj({
      op: { type: "string" },
      targets: { description: "List of object names, or a find selector string." },
      target: { type: "string" },
      a: { type: "string" },
      b: { type: "string" },
      origin: { type: "array", items: { type: "number" } },
      direction: { type: "array", items: { type: "number" } },
      points: { type: "array", items: { type: "array", items: { type: "number" } } },
      min: { type: "array", items: { type: "number" } },
      max: { type: "array", items: { type: "number" } },
      k: { type: "number" },
    }, ["op"]),
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
    description: "Before/after images side by side with a difference heatmap. Each source is live (a preview of the open session), a checkpoint id (rendered from the checkpoint in a headless Blender), or a workspace image path. Same view settings as preview. Read-only; no network.",
    inputSchema: obj({
      a: { type: "string", description: "First source, e.g. a checkpoint id." },
      b: { type: "string", description: "Second source. Default live." },
      ...viewProps,
      size: { type: "number", description: "Default 512." },
      heatmap: { type: "boolean", description: "Default true." },
    }, ["a"]),
  },
  {
    name: "reference",
    description: "Reference images next to a matching preview of the open session, to check accuracy against a real design. images: workspace paths or https URLs the user gave (downloaded, images only, 15 MB max).",
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

export function claudeRule(tool: string): string {
  return `mcp__vsblender__${tool}`;
}
