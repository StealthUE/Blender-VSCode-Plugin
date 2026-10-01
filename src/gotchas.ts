/** Short version-drift sheet injected into session_info. Mirrors BLENDER_AI_NOTES.md §1.6. */
export function gotchasFor(version: string): string[] {
  const major = Number(String(version).split(/[.\s]/).find((part) => /^\d+$/.test(part)) ?? "");
  if (!Number.isFinite(major)) return [];
  const general = [
    "An EEVEE world volume is infinite: it blacks out the sky and extinguishes the sun. Use a bounded volume object (a large cube with Principled Volume), or distance-based haze in the shaders.",
    "view_settings.look is a dynamic enum and RNA lists only NONE. Assign it inside try/except TypeError; api(\"scene.view_settings.look\") lists the real values.",
    "Sky Texture sun direction is (cos(el)*sin(rot), -cos(el)*cos(rot), sin(el)) for sun_elevation el and sun_rotation rot. Point the sun lamp the same way.",
    "ShaderNodeMix socket identifiers depend on data_type: Factor_Float, A_Color, B_Color, Result_Color. By name, \"A\" finds the float socket first; use the identifier or vsblender.sock().",
    "Wave Texture output: identifier Fac, displayed name Factor.",
    "Material fcurves address nodes by name (nodes[\"Wave Texture\"]...). Renaming or recreating a keyframed node breaks its animation: keep the name (vsblender.build reuses nodes by name).",
  ];
  if (major < 5) return general;
  return [
    "Render engine id is BLENDER_EEVEE. BLENDER_EEVEE_NEXT is gone. RNA under-reports engines, so assign it inside try/except TypeError.",
    "Material.use_nodes is deprecated and always on. A new material already contains Principled BSDF and the Material Output.",
    "The compositor is a node group: assign a CompositorNodeTree to scene.compositing_node_group. scene.node_tree is gone.",
    "Glare options are input sockets. Type is a menu socket (for example \"Bloom\"), not a property.",
    "Actions are layered and slotted; Action.fcurves is gone. Read fcurves from anim_utils.action_get_channelbag_for_slot(action, slot), or use vsblender.fcurves / set_keys / scale_keys.",
    "A socket's identifier and its displayed name can differ (Fac versus Factor), and names are translated. Use the identifier.",
    "Video output: set render.image_settings.media_type = \"VIDEO\" before file_format = \"FFMPEG\".",
    ...general,
  ];
}
