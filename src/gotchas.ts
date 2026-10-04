/** Short version-drift sheet injected into session_info. Mirrors BLENDER_AI_NOTES.md §1.6 and IMPROVEMENTS.md §5. */
export function gotchasFor(version: string): string[] {
  const major = Number(String(version).split(/[.\s]/).find((part) => /^\d+$/.test(part)) ?? "");
  if (!Number.isFinite(major)) return [];
  const general = [
    "An EEVEE world volume is infinite: it blacks out the sky and extinguishes the sun. Use a bounded volume object (a large cube with Principled Volume), or distance-based haze in the shaders.",
    "view_settings.look is a dynamic enum and RNA lists only NONE. Assign it inside try/except TypeError; api(\"scene.view_settings.look\") lists the real values.",
    "Sky Texture sun direction is (cos(el)*sin(rot), -cos(el)*cos(rot), sin(el)) for sun_elevation el and sun_rotation rot. Point the sun lamp the same way.",
    "ShaderNodeMix socket identifiers depend on data_type: Factor_Float, A_Color, B_Color, Result_Color. By name, \"A\" finds the float socket first; use the identifier or vsblender.sock().",
    "Wave Texture output: identifier Fac, displayed name Factor. Its Color output used as a vector offset pushes along (1,1,1): diagonal streaks, not ripples. Use Fac times a direction.",
    "Material fcurves address nodes by name (nodes[\"Wave Texture\"]...). Renaming or recreating a keyframed node breaks its animation: keep the name (vsblender.build reuses nodes by name).",
    "mathutils.geometry.tessellate_polygon mis-fills concave outlines (a \"Λ\" becomes a solid triangle). Use delaunay_2d_cdt and keep triangles by the even-odd rule, or vsblender.geo (prism, extrude).",
    "matrix_world is stale inside a script right after changing location, rotation or parent. Call bpy.context.view_layer.update() before reading it.",
    "Replacing ob.data with a new mesh keeps the object's animation but drops the mesh's material slots; materials keyed per slot then lose their last user and are deleted. Use vsblender.geo.replace_mesh or Solid.to_object, which rewrite the mesh in place.",
    "animation_data_clear() then removing \"the old action\" by name can remove the same action twice. Collect actions in a set and remove each once.",
    "Under AgX, bright saturated emission turns pastel (orange at strength 7 reads peach). Keep coloured emission around 1.5-3, or use the AgX Punchy look.",
    "A ring of coincident vertices on an axis (a lathe or dome pole) leaves non-manifold edges after remove_doubles. Close it with one apex vertex (vsblender.geo.lathe does).",
    "The MANIFOLD boolean solver needs closed operands; vsblender.geo picks it only when every input is closed and falls back to EXACT. Let cutters pass through the surface instead of ending on it (coplanar faces).",
    "A negative scale flips the winding: normals point in after applying it. Exporters that ignore the sign flip the model inside out; check_model flags it.",
    "Lengths are Blender units. In a print_mm project 1 unit = 1 mm: the default cube is 2 mm, and light power, physics and camera clipping are still per unit. vsblender.mm()/m() convert real sizes.",
    "glTF and USD are metres by definition. export_model scales a millimetre scene to metres for them; STL and 3MF are written in millimetres.",
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
    "preview and compare draw objects hidden for render, or displayed as wire, only through overlay: [{\"objects\": \"...\", \"style\": \"wire\"}].",
    ...general,
  ];
}
