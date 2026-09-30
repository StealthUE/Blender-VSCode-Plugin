/** Short version-drift sheet injected into session_info. Mirrors BLENDER_AI_NOTES.md §1.6. */
export function gotchasFor(version: string): string[] {
  const major = Number(String(version).split(/[.\s]/).find((part) => /^\d+$/.test(part)) ?? "");
  if (!Number.isFinite(major) || major < 5) return [];
  return [
    "Render engine id is BLENDER_EEVEE. BLENDER_EEVEE_NEXT is gone. RNA under-reports engines, so assign it inside try/except TypeError.",
    "Material.use_nodes is deprecated and always on. A new material already contains Principled BSDF and the Material Output.",
    "The compositor is a node group: assign a CompositorNodeTree to scene.compositing_node_group. scene.node_tree is gone.",
    "Glare options are input sockets. Type is a menu socket (for example \"Bloom\"), not a property.",
    "Actions are layered and slotted. Read fcurves from anim_utils.action_get_channelbag_for_slot(action, slot).",
    "A socket's identifier and its displayed name can differ (Fac versus Factor), and names are translated. Use the identifier.",
  ];
}
