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
 * name or an object). Unknown preset names fall back to the default with a warning.
 */
export function resolvePrinter(setting?: unknown, override?: unknown): { profile: PrinterProfile; warnings: string[] } {
  const warnings: string[] = [];
  const layers = [normalizePrinterSetting(setting), normalizePrinterSetting(override)].filter((item): item is PrinterSetting => item !== undefined);
  let preset = DEFAULT_PRESET;
  for (const layer of layers) {
    const name = typeof layer === "string" ? layer : layer.preset;
    if (!name) continue;
    if (PRINTER_PRESETS[name]) preset = name;
    else warnings.push(`unknown printer preset "${name}"; using ${preset}. Presets: ${Object.keys(PRINTER_PRESETS).join(", ")}`);
  }
  const base = PRINTER_PRESETS[preset] ?? PRINTER_PRESETS[DEFAULT_PRESET]!;
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
