/**
 * 3D-printer profiles. Only tools called with purpose "print" (or a scene made from the print_mm
 * template) use them. Lengths are millimetres, angles degrees, density g/cm³.
 */
export interface PrinterProfile {
  preset: string;
  name: string;
  buildVolume: [number, number, number];
  nozzle: number;
  layerHeight: number;
  minWall: number;
  maxOverhangDeg: number;
  material: string;
  density: number;
  filamentDiameter: number;
  /** Added to modelled hole diameters: FDM prints holes undersize. */
  holeCompensation: number;
}

/** What .blender-ai/config.json may hold under "printer": a preset name, or an object of overrides. */
export type PrinterSetting = string | Partial<PrinterProfile>;

const FDM = { nozzle: 0.4, layerHeight: 0.2, minWall: 0.8, maxOverhangDeg: 45, material: "PLA", filamentDiameter: 1.75, holeCompensation: 0.15 };

export const PRINTER_PRESETS: Record<string, Omit<PrinterProfile, "preset" | "density">> = {
  generic_220: { name: "Generic 220 mm FDM", buildVolume: [220, 220, 250], ...FDM },
  bambu_x1c: { name: "Bambu Lab X1 Carbon", buildVolume: [256, 256, 256], ...FDM },
  bambu_p1s: { name: "Bambu Lab P1S", buildVolume: [256, 256, 256], ...FDM },
  bambu_p1p: { name: "Bambu Lab P1P", buildVolume: [256, 256, 256], ...FDM },
  bambu_a1: { name: "Bambu Lab A1", buildVolume: [256, 256, 256], ...FDM },
  bambu_a1_mini: { name: "Bambu Lab A1 mini", buildVolume: [180, 180, 180], ...FDM },
  prusa_mk4: { name: "Prusa MK4", buildVolume: [250, 210, 220], ...FDM },
  prusa_core_one: { name: "Prusa CORE One", buildVolume: [250, 220, 270], ...FDM },
  prusa_mini: { name: "Prusa MINI", buildVolume: [180, 180, 180], ...FDM },
  creality_ender3: { name: "Creality Ender-3", buildVolume: [220, 220, 250], ...FDM },
};

export const DEFAULT_PRESET = "generic_220";

/** Marketing names that mean a built-in preset. Checked in order; MK3 is not here, it is looked up. */
const PRINTER_ALIASES: { test: RegExp; id: string }[] = [
  { test: /\ba1\s*mini\b|\ba1mini\b/, id: "bambu_a1_mini" },
  { test: /\bx1c\b|\bx1\s*carbon\b/, id: "bambu_x1c" },
  { test: /\bp1s\b/, id: "bambu_p1s" },
  { test: /\bp1p\b/, id: "bambu_p1p" },
  { test: /\ba1\b/, id: "bambu_a1" },
  { test: /\bmk4\b/, id: "prusa_mk4" },
  { test: /\bcore\s*one\b|\bcoreone\b/, id: "prusa_core_one" },
  { test: /\bmini\b/, id: "prusa_mini" },
  { test: /\bender[\s-]*3\b/, id: "creality_ender3" },
];

/** Presets plus machines this workspace has already looked up. Values match PRINTER_PRESETS. */
export type PrinterCatalog = Record<string, Omit<PrinterProfile, "preset" | "density">>;

export function printerSlug(name: string): string {
  return name.toLowerCase().replace(/[^a-z0-9]+/g, "_").replace(/^_+|_+$/g, "") || "printer";
}

/** A preset id for this name: the id itself, its slug, a marketing alias, or a catalog entry. */
export function canonicalPreset(name: string, extras?: PrinterCatalog): string | undefined {
  const trimmed = name.trim();
  if (!trimmed) return undefined;
  if (PRINTER_PRESETS[trimmed] || extras?.[trimmed]) return trimmed;
  const slug = printerSlug(trimmed);
  if (PRINTER_PRESETS[slug] || extras?.[slug]) return slug;
  const folded = trimmed.toLowerCase();
  for (const alias of PRINTER_ALIASES) {
    if (alias.test.test(folded) && (PRINTER_PRESETS[alias.id] || extras?.[alias.id])) return alias.id;
  }
  return undefined;
}

/** g/cm³, typical datasheet values. The slicer's own estimate is the one to trust. */
export const MATERIAL_DENSITY: Record<string, number> = {
  PLA: 1.24,
  PETG: 1.27,
  ABS: 1.04,
  ASA: 1.07,
  TPU: 1.21,
  PA: 1.14,
  PC: 1.2,
};

const NUMBER_KEYS = ["nozzle", "layerHeight", "minWall", "maxOverhangDeg", "density", "filamentDiameter", "holeCompensation"] as const;

/** Keep only well-formed fields, so a typo in config.json cannot reach the add-on as NaN. */
export function normalizePrinterSetting(value: unknown): PrinterSetting | undefined {
  if (typeof value === "string") return value.trim() ? value.trim() : undefined;
  if (!value || typeof value !== "object" || Array.isArray(value)) return undefined;
  const raw = value as Record<string, unknown>;
  const out: Partial<PrinterProfile> = {};
  if (typeof raw["preset"] === "string" && raw["preset"].trim()) out.preset = raw["preset"].trim();
  if (typeof raw["name"] === "string" && raw["name"].trim()) out.name = raw["name"].trim();
  if (typeof raw["material"] === "string" && raw["material"].trim()) out.material = raw["material"].trim().toUpperCase();
  const volume = raw["buildVolume"];
  if (Array.isArray(volume) && volume.length === 3 && volume.every((v) => typeof v === "number" && v > 0)) {
    out.buildVolume = [volume[0], volume[1], volume[2]] as [number, number, number];
  }
  for (const key of NUMBER_KEYS) {
    const v = raw[key];
    if (typeof v === "number" && Number.isFinite(v) && v >= 0) out[key] = v;
  }
  return out;
}

/**
 * The profile a call uses: preset, then the workspace setting, then the per-call value (a preset
 * name or an object). extras holds machines looked up earlier. An unknown name with no build
 * volume falls back to the default with a warning. A full profile object is used as given.
 */
export function resolvePrinter(setting?: unknown, override?: unknown, extras?: PrinterCatalog): { profile: PrinterProfile; warnings: string[] } {
  const warnings: string[] = [];
  const layers = [normalizePrinterSetting(setting), normalizePrinterSetting(override)].filter((item): item is PrinterSetting => item !== undefined);
  let preset = DEFAULT_PRESET;
  for (const layer of layers) {
    const name = typeof layer === "string" ? layer : layer.preset;
    if (!name) continue;
    const known = canonicalPreset(name, extras);
    if (known) preset = known;
    else if (typeof layer !== "string" && layer.buildVolume) preset = printerSlug(name);
    else warnings.push(`unknown printer preset "${name}"; using ${preset}. Presets: ${Object.keys(PRINTER_PRESETS).join(", ")}`);
  }
  const base = extras?.[preset] ?? PRINTER_PRESETS[preset] ?? PRINTER_PRESETS[DEFAULT_PRESET]!;
  let profile: PrinterProfile = { ...base, preset, density: MATERIAL_DENSITY[base.material] ?? 1.24 };
  for (const layer of layers) {
    if (typeof layer === "string") continue;
    const { preset: _preset, ...fields } = layer;
    profile = { ...profile, ...fields };
    // A material change without an explicit density picks up that material's density.
    if (fields.material && fields.density === undefined) profile.density = MATERIAL_DENSITY[fields.material] ?? profile.density;
  }
  return { profile, warnings };
}

/** The add-on is Python: it reads snake_case. */
export function printerParams(profile: PrinterProfile): Record<string, unknown> {
  return {
    preset: profile.preset,
    name: profile.name,
    build_volume: profile.buildVolume,
    nozzle: profile.nozzle,
    layer_height: profile.layerHeight,
    min_wall: profile.minWall,
    max_overhang_deg: profile.maxOverhangDeg,
    material: profile.material,
    density: profile.density,
    filament_diameter: profile.filamentDiameter,
    hole_compensation: profile.holeCompensation,
  };
}

export function describePrinter(profile: PrinterProfile): string {
  const [x, y, z] = profile.buildVolume;
  return `${profile.name} (${profile.preset}): ${x}x${y}x${z} mm, ${profile.nozzle} mm nozzle, ${profile.layerHeight} mm layers, ${profile.material}`;
}
