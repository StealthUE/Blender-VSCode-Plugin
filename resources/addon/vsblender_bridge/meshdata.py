"""Triangle meshes as numpy arrays: read from objects or files, measure, check, write.

    arrays = object_arrays(obj, depsgraph)        evaluated, world space, modifiers applied
    welded, remap = weld(arrays, tol)
    edge_stats(welded)                            open edges, non-manifold edges, flipped neighbours
    components(welded)                            shell label per triangle
    volume_area(welded)                           signed volume and area
    read_stl / read_obj / read_3mf / read_mesh_file(path)
    read_3mf leaves a slicer's negative, modifier and support meshes out of the solid
    write_stl / write_obj / write_3mf

Nothing here changes the scene. bpy is only needed for object_arrays, to_mesh and from_bmesh.
"""
from __future__ import annotations

import io
import json
import math
import os
import re
import struct
import xml.etree.ElementTree as ET
import zipfile

import numpy as np


class MeshArrays:
    """verts (V, 3) float64, tris (T, 3) int64. Optional per-triangle face (source polygon) and material.
    groups: names for the material indices (an OBJ's usemtl materials, or its g/o groups)."""

    __slots__ = ("verts", "tris", "face", "material", "name", "groups")

    def __init__(self, verts, tris, face=None, material=None, name: str = "", groups=None):
        self.verts = np.asarray(verts, dtype=np.float64).reshape(-1, 3)
        self.tris = np.asarray(tris, dtype=np.int64).reshape(-1, 3)
        self.face = None if face is None else np.asarray(face, dtype=np.int64)
        self.material = None if material is None else np.asarray(material, dtype=np.int64)
        self.name = name
        self.groups = list(groups) if groups else None

    def __len__(self) -> int:
        return len(self.tris)

    def copy(self) -> "MeshArrays":
        return MeshArrays(self.verts.copy(), self.tris.copy(),
                          None if self.face is None else self.face.copy(),
                          None if self.material is None else self.material.copy(), self.name, self.groups)

    def select(self, mask) -> "MeshArrays":
        """The triangles where mask is true, with only the vertices they use."""
        mask = np.asarray(mask, dtype=bool)
        tris = self.tris[mask]
        used, inverse = np.unique(tris, return_inverse=True)
        return MeshArrays(self.verts[used], inverse.reshape(-1, 3), None if self.face is None else self.face[mask],
                          None if self.material is None else self.material[mask], self.name, self.groups)

    def part(self, patterns) -> "MeshArrays":
        """The triangles of the named groups (globs allowed): an OBJ material or group, a 3MF item."""
        import fnmatch

        if not self.groups or self.material is None:
            raise ValueError(f"{self.name or 'this mesh'} has no named parts (an OBJ with usemtl or g lines has)")
        if isinstance(patterns, str):
            patterns = [patterns]
        wanted = [i for i, g in enumerate(self.groups) if any(fnmatch.fnmatchcase(g, p) or g == p for p in patterns)]
        if not wanted:
            raise KeyError(f"no part matches {patterns}. Parts: {', '.join(self.groups[:60])}"
                           + (" ..." if len(self.groups) > 60 else ""))
        out = self.select(np.isin(self.material, wanted))
        out.name = ", ".join(self.groups[i] for i in wanted[:4]) + (" ..." if len(wanted) > 4 else "")
        return out

    def bounds(self) -> tuple:
        if not len(self.verts):
            return np.zeros(3), np.zeros(3)
        used = self.verts[np.unique(self.tris)] if len(self.tris) else self.verts
        return used.min(axis=0), used.max(axis=0)

    def transformed(self, matrix) -> "MeshArrays":
        """Apply a 4x4 matrix (mathutils or nested lists). Flips winding for a mirroring matrix."""
        m = np.asarray([list(row) for row in matrix], dtype=np.float64)
        verts = self.verts @ m[:3, :3].T + m[:3, 3]
        tris = self.tris[:, ::-1].copy() if np.linalg.det(m[:3, :3]) < 0 else self.tris
        return MeshArrays(verts, tris, self.face, self.material, self.name, self.groups)

    def scaled(self, factor: float) -> "MeshArrays":
        return MeshArrays(self.verts * float(factor), self.tris, self.face, self.material, self.name, self.groups)

    def moved(self, offset) -> "MeshArrays":
        return MeshArrays(self.verts + np.asarray(offset, dtype=np.float64), self.tris, self.face, self.material,
                          self.name, self.groups)


def concat(parts) -> MeshArrays:
    parts = [p for p in parts if p is not None and len(p.verts)]
    if not parts:
        return MeshArrays(np.zeros((0, 3)), np.zeros((0, 3), dtype=np.int64))
    verts, tris, mats = [], [], []
    base = 0
    for p in parts:
        verts.append(p.verts)
        tris.append(p.tris + base)
        mats.append(p.material if p.material is not None else np.zeros(len(p.tris), dtype=np.int64))
        base += len(p.verts)
    return MeshArrays(np.vstack(verts), np.vstack(tris), material=np.concatenate(mats), name=parts[0].name)


# ----------------------------------------------------------------------------- from Blender
def _mesh_arrays(mesh) -> MeshArrays:
    mesh.calc_loop_triangles()
    nv = len(mesh.vertices)
    nt = len(mesh.loop_triangles)
    verts = np.empty(nv * 3, dtype=np.float32)
    mesh.vertices.foreach_get("co", verts)
    tris = np.empty(nt * 3, dtype=np.int32)
    mesh.loop_triangles.foreach_get("vertices", tris)
    face = np.empty(nt, dtype=np.int32)
    mesh.loop_triangles.foreach_get("polygon_index", face)
    material = np.zeros(nt, dtype=np.int64)
    if len(mesh.polygons):
        poly_mat = np.empty(len(mesh.polygons), dtype=np.int32)
        mesh.polygons.foreach_get("material_index", poly_mat)
        material = poly_mat[face].astype(np.int64)
    return MeshArrays(verts.astype(np.float64), tris.astype(np.int64), face.astype(np.int64), material)


def object_arrays(ob, depsgraph=None, world: bool = True, instances: bool = True, max_instances: int = 10000,
                  warnings: list | None = None) -> MeshArrays:
    """The evaluated mesh of an object (modifiers, geometry nodes, curves and text converted), as triangles.

    world: in world space, with the winding flipped when the object is mirrored (negative scale), so
    normals still point out. instances: include geometry-node, particle and collection instances
    whose parent is this object (up to max_instances).
    """
    import bpy

    if depsgraph is None:
        depsgraph = bpy.context.evaluated_depsgraph_get()
    eo = ob.evaluated_get(depsgraph)
    parts = []
    mesh = None
    try:
        mesh = eo.to_mesh(preserve_all_data_layers=False, depsgraph=depsgraph)
    except RuntimeError:
        mesh = None
    try:
        if mesh is not None and len(mesh.polygons):
            arr = _mesh_arrays(mesh)
            arr.name = ob.name
            parts.append(arr.transformed(eo.matrix_world) if world else arr)
    finally:
        if mesh is not None:
            eo.to_mesh_clear()
    if instances:
        count = 0
        inverse = None if world else eo.matrix_world.inverted_safe()
        for inst in depsgraph.object_instances:
            if not inst.is_instance:
                continue
            parent = inst.parent
            if parent is None or getattr(parent, "original", parent) != ob:
                continue
            if count >= max_instances:
                if warnings is not None:
                    warnings.append(f"{ob.name}: more than {max_instances} instances; the rest are left out")
                break
            data = getattr(inst.object, "data", None)
            if not hasattr(data, "loop_triangles"):
                continue
            try:
                arr = _mesh_arrays(data)
            except Exception:
                continue
            matrix = inst.matrix_world.copy()
            if inverse is not None:
                matrix = inverse @ matrix
            parts.append(arr.transformed(matrix))
            count += 1
    out = concat(parts)
    out.name = ob.name
    if len(parts) == 1:
        out.face = parts[0].face
    return out


def from_bmesh(bm) -> MeshArrays:
    """Triangles of a BMesh, without changing it."""
    bm.verts.index_update()
    verts = np.array([v.co[:] for v in bm.verts], dtype=np.float64).reshape(-1, 3)
    loops = bm.calc_loop_triangles()
    tris = np.array([[l.vert.index for l in tri] for tri in loops], dtype=np.int64).reshape(-1, 3)
    face = np.array([tri[0].face.index for tri in loops], dtype=np.int64) if loops else np.zeros(0, dtype=np.int64)
    material = np.array([tri[0].face.material_index for tri in loops], dtype=np.int64) if loops else np.zeros(0, dtype=np.int64)
    return MeshArrays(verts, tris, face, material)


def to_mesh(arrays: MeshArrays, name: str):
    """A new bpy Mesh from triangle arrays (material_index set when the arrays carry one)."""
    import bpy

    me = bpy.data.meshes.new(name)
    nv, nt = len(arrays.verts), len(arrays.tris)
    try:
        me.vertices.add(nv)
        me.vertices.foreach_set("co", arrays.verts.astype(np.float32).ravel())
        me.loops.add(nt * 3)
        me.loops.foreach_set("vertex_index", arrays.tris.astype(np.int32).ravel())
        me.polygons.add(nt)
        me.polygons.foreach_set("loop_start", np.arange(0, nt * 3, 3, dtype=np.int32))
        if arrays.material is not None and len(arrays.material) == nt:
            me.polygons.foreach_set("material_index", arrays.material.astype(np.int32))
        me.update(calc_edges=True)
        # New faces are smooth by default (4.1+): averaged normals over triangulated flats render as streaks.
        if hasattr(me, "shade_flat"):
            me.shade_flat()
    except Exception:
        bpy.data.meshes.remove(me)
        me = bpy.data.meshes.new(name)
        me.from_pydata(arrays.verts.tolist(), [], arrays.tris.tolist())
        if arrays.material is not None and len(arrays.material) == len(me.polygons):
            me.polygons.foreach_set("material_index", arrays.material.astype(np.int32))
        me.update()
    return me


# ----------------------------------------------------------------------------- topology
def weld(arrays: MeshArrays, tol: float) -> tuple:
    """Merge vertices closer than about tol (quantised to a grid of tol), drop unused ones.

    Returns (welded arrays, remap from old vertex index to new). Triangles that collapse keep their
    repeated indices so callers can count them as degenerate.
    """
    v = arrays.verts
    if not len(v):
        return arrays.copy(), np.zeros(0, dtype=np.int64)
    tol = max(float(tol), 1e-12)
    keys = np.round(v / tol).astype(np.int64)
    _uniq, first, inverse = np.unique(keys, axis=0, return_index=True, return_inverse=True)
    inverse = inverse.reshape(-1)
    verts = v[first]
    tris = inverse[arrays.tris]
    return MeshArrays(verts, tris, arrays.face, arrays.material, arrays.name), inverse


def _edges(tris):
    return np.concatenate([tris[:, [0, 1]], tris[:, [1, 2]], tris[:, [2, 0]]])


def edge_stats(arrays: MeshArrays) -> dict:
    """Open (boundary) edges, non-manifold edges (shared by 3+ triangles), and inconsistent winding.

    Run on welded arrays. Returns counts plus up to 2000 sample edge midpoints for each problem, and
    per-triangle flags (tri_open, tri_nonmanifold, tri_flipped) for highlighting.
    """
    tris = arrays.tris
    nv = max(1, len(arrays.verts))
    ok = (tris[:, 0] != tris[:, 1]) & (tris[:, 1] != tris[:, 2]) & (tris[:, 0] != tris[:, 2])
    good = tris[ok]
    idx = np.nonzero(ok)[0]
    e = _edges(good)
    owner = np.concatenate([idx, idx, idx])
    und = np.sort(e, axis=1)
    key = und[:, 0] * nv + und[:, 1]
    _u, inv, counts = np.unique(key, return_inverse=True, return_counts=True)
    inv = inv.reshape(-1)
    per = counts[inv]
    dkey = e[:, 0] * nv + e[:, 1]
    _du, dinv, dcounts = np.unique(dkey, return_inverse=True, return_counts=True)
    dper = dcounts[dinv.reshape(-1)]
    open_mask = per == 1
    nm_mask = per > 2
    flip_mask = (per == 2) & (dper > 1)
    out = {
        "edges": int(len(counts)),
        "open_edges": int((counts == 1).sum()),
        "non_manifold_edges": int((counts > 2).sum()),
        "flipped_edges": int(len(np.unique(key[flip_mask]))),
        "degenerate_tris": int((~ok).sum()),
    }
    t = len(tris)
    flags = {}
    for name, mask in (("tri_open", open_mask), ("tri_nonmanifold", nm_mask), ("tri_flipped", flip_mask)):
        f = np.zeros(t, dtype=bool)
        f[owner[mask]] = True
        flags[name] = f
    out["flags"] = flags
    mids = (arrays.verts[e[:, 0]] + arrays.verts[e[:, 1]]) / 2
    out["at"] = {
        "open_edges": _sample(mids[open_mask]),
        "non_manifold_edges": _sample(mids[nm_mask]),
        "flipped_edges": _sample(mids[flip_mask]),
    }
    if out["open_edges"]:
        out["holes"] = _count_loops(und[open_mask])
    return out


def _sample(points, k: int = 2000) -> np.ndarray:
    if len(points) <= k:
        return points
    step = len(points) / k
    return points[(np.arange(k) * step).astype(int)]


def _count_loops(edges) -> int:
    """Connected groups of boundary edges: the number of holes."""
    if not len(edges):
        return 0
    verts, inv = np.unique(edges.ravel(), return_inverse=True)
    pairs = inv.reshape(-1, 2)
    labels = _union(len(verts), pairs)
    return int(len(np.unique(labels)))


def _union(n: int, pairs) -> np.ndarray:
    """Connected-component labels of n nodes linked by pairs (min-label propagation with pointer jumping)."""
    parent = np.arange(n, dtype=np.int64)
    if not len(pairs):
        return parent
    a, b = pairs[:, 0], pairs[:, 1]
    for _ in range(200):
        pa, pb = parent[a], parent[b]
        low = np.minimum(pa, pb)
        new = parent.copy()
        np.minimum.at(new, pa, low)
        np.minimum.at(new, pb, low)
        while True:
            jumped = new[new]
            if np.array_equal(jumped, new):
                break
            new = jumped
        if np.array_equal(new, parent):
            break
        parent = new
    return parent


def components(arrays: MeshArrays) -> np.ndarray:
    """Shell label (0..k-1) per triangle: triangles connected through shared vertices."""
    tris = arrays.tris
    if not len(tris):
        return np.zeros(0, dtype=np.int64)
    pairs = np.concatenate([tris[:, [0, 1]], tris[:, [1, 2]]])
    labels = _union(len(arrays.verts), pairs)
    _u, shell = np.unique(labels[tris[:, 0]], return_inverse=True)
    return shell.reshape(-1)


def tri_normals_areas(arrays: MeshArrays) -> tuple:
    v = arrays.verts
    t = arrays.tris
    a, b, c = v[t[:, 0]], v[t[:, 1]], v[t[:, 2]]
    cross = np.cross(b - a, c - a)
    length = np.linalg.norm(cross, axis=1)
    areas = 0.5 * length
    normals = np.divide(cross, length[:, None], out=np.zeros_like(cross), where=length[:, None] > 0)
    return normals, areas


def volume_area(arrays: MeshArrays, labels=None) -> dict:
    """Signed volume (positive when normals point out) and area, in total and per shell."""
    v = arrays.verts
    t = arrays.tris
    if not len(t):
        return {"volume": 0.0, "area": 0.0, "shell_volume": np.zeros(0), "shell_area": np.zeros(0)}
    a, b, c = v[t[:, 0]], v[t[:, 1]], v[t[:, 2]]
    # Relative to the centre of the bounds, so large offsets do not cost precision.
    centre = (v.min(axis=0) + v.max(axis=0)) / 2
    a, b, c = a - centre, b - centre, c - centre
    vol = np.einsum("ij,ij->i", a, np.cross(b, c)) / 6.0
    _n, areas = tri_normals_areas(arrays)
    out = {"volume": float(vol.sum()), "area": float(areas.sum())}
    if labels is not None and len(labels):
        k = int(labels.max()) + 1
        out["shell_volume"] = np.bincount(labels, weights=vol, minlength=k)
        out["shell_area"] = np.bincount(labels, weights=areas, minlength=k)
    return out


# ----------------------------------------------------------------------------- reading files
_STL_DTYPE = np.dtype([("normal", "<f4", (3,)), ("v", "<f4", (3, 3)), ("attr", "<u2")])


def read_stl(path: str, weld_tol: float | None = 1e-6) -> MeshArrays:
    """Binary or ASCII STL, in the file's units (usually mm). Vertices are welded when weld_tol is set."""
    size = os.path.getsize(path)
    with open(path, "rb") as handle:
        head = handle.read(84)
    binary = False
    if len(head) == 84:
        count = struct.unpack("<I", head[80:84])[0]
        binary = size == 84 + 50 * count
    if binary:
        data = np.fromfile(path, dtype=_STL_DTYPE, count=count, offset=84)
        corners = data["v"].astype(np.float64).reshape(-1, 3)
    else:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            text = handle.read()
        nums = re.findall(r"vertex\s+(\S+)\s+(\S+)\s+(\S+)", text)
        if not nums:
            raise ValueError(f"{path} is neither a binary nor an ASCII STL")
        corners = np.array(nums, dtype=np.float64)
    tris = np.arange(len(corners), dtype=np.int64).reshape(-1, 3)
    arrays = MeshArrays(corners, tris, name=os.path.splitext(os.path.basename(path))[0])
    if weld_tol:
        arrays, _remap = weld(arrays, weld_tol)
    return arrays


_SLASH = re.compile(rb"/\S*")


def _obj_vertices(lines: list) -> np.ndarray:
    """'v x y z [w | r g b]' bodies to (N, 3)."""
    if not lines:
        return np.zeros((0, 3))
    flat = np.array(b" ".join(lines).split(), dtype=np.float64)
    if len(flat) == 3 * len(lines):
        return flat.reshape(-1, 3)
    return np.array([[float(x) for x in line.split()[:3]] for line in lines], dtype=np.float64)


def _obj_faces(lines: list, nverts: int):
    """'f a/b/c ...' bodies to (triangles, polygon index per triangle), fan-triangulated, 0-based.
    Relative (negative) indices count back from the vertices read so far."""
    if not lines:
        return np.zeros((0, 3), dtype=np.int64), np.zeros(0, dtype=np.int64)
    # A 0 between polygons marks where each ends: OBJ indices are never 0.
    flat = np.array(_SLASH.sub(b"", b" 0 ".join(lines)).split(), dtype=np.int64)
    ends = np.flatnonzero(flat == 0)
    starts = np.concatenate([[0], ends + 1])
    stops = np.concatenate([ends, [len(flat)]])
    counts = stops - starts
    idx = flat.copy()
    neg = idx < 0
    if neg.any():
        idx[neg] += nverts + 1
    idx -= 1
    keep = counts >= 3
    starts, counts = starts[keep], counts[keep]
    poly = np.flatnonzero(keep)
    ntri = counts - 2
    owner = np.repeat(np.arange(len(starts)), ntri)
    local = np.arange(int(ntri.sum())) - np.repeat(np.cumsum(ntri) - ntri, ntri)
    first = starts[owner]
    tris = np.stack([idx[first], idx[first + local + 1], idx[first + local + 2]], axis=1)
    return tris, poly[owner]


def read_obj(path: str) -> MeshArrays:
    """Vertices and faces of an OBJ, vectorised (a 500 MB file in seconds). Polygons are
    fan-triangulated. Each triangle keeps its usemtl material, or its g/o group when the file has no
    materials: arrays.material indexes arrays.groups, and arrays.part("Chevron") selects one."""
    verts_parts, tri_parts, poly_group = [], [], []
    nverts = 0
    npoly = 0
    names = {"mtl": [], "grp": []}
    runs = {"mtl": [], "grp": []}  # (first polygon, name index)

    def group_index(kind, name):
        table = names[kind]
        if name not in table:
            table.append(name)
        return table.index(name)

    with open(path, "rb") as handle:
        rest = b""
        while True:
            chunk = handle.read(1 << 26)
            data = rest + chunk
            if chunk:
                cut = data.rfind(b"\n")
                if cut < 0:
                    rest = data
                    continue
                rest, data = data[cut + 1:], data[:cut]
            vlines, flines = [], []
            for line in data.split(b"\n"):
                head = line[:2]
                if head == b"v ":
                    vlines.append(line[2:])
                elif head == b"f ":
                    flines.append(line[2:].strip())
                elif line.startswith(b"usemtl "):
                    runs["mtl"].append((npoly + len(flines), group_index("mtl", line[7:].strip().decode("utf-8", "replace"))))
                elif head in (b"g ", b"o "):
                    runs["grp"].append((npoly + len(flines), group_index("grp", line[2:].strip().decode("utf-8", "replace"))))
            verts = _obj_vertices(vlines)
            nverts += len(verts)
            verts_parts.append(verts)
            tris, poly = _obj_faces(flines, nverts)
            tri_parts.append(tris)
            poly_group.append(poly + npoly)
            npoly += len(flines)
            if not chunk:
                break
    tris = np.vstack(tri_parts) if tri_parts else np.zeros((0, 3), dtype=np.int64)
    if not len(tris):
        raise ValueError(f"{path} has no faces")
    poly = np.concatenate(poly_group)
    kind = "mtl" if names["mtl"] else "grp"
    groups = names[kind] or None
    material = None
    if groups:
        starts = np.array([r[0] for r in runs[kind]], dtype=np.int64)
        ids = np.array([r[1] for r in runs[kind]], dtype=np.int64)
        where = np.searchsorted(starts, poly, side="right") - 1
        material = np.where(where >= 0, ids[np.clip(where, 0, None)], 0)
    verts = np.vstack(verts_parts)
    if len(tris) and (tris.min() < 0 or tris.max() >= len(verts)):
        raise ValueError(f"{path}: a face uses a vertex the file does not define")
    return MeshArrays(verts, tris, material=material, name=os.path.splitext(os.path.basename(path))[0], groups=groups)


_UNIT_MM = {"micron": 0.001, "millimeter": 1.0, "centimeter": 10.0, "inch": 25.4, "foot": 304.8, "meter": 1000.0}


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _matrix_3mf(text: str | None) -> np.ndarray:
    m = np.identity(4)
    if not text:
        return m
    v = [float(x) for x in text.split()]
    if len(v) != 12:
        return m
    # 3MF: row vectors, p' = p * M with M 4x3 given row by row.
    m3 = np.array(v, dtype=np.float64).reshape(4, 3)
    m[:3, :3] = m3[:3, :].T
    m[:3, 3] = m3[3, :]
    return m


class ThreeMF(list):
    """(name, MeshArrays in mm) for each returned mesh. project describes slicer volumes and the machine the file was sliced for."""

    def __init__(self, items, project: dict):
        super().__init__(items)
        self.project = project


def _is_model_volume(kind: str) -> bool:
    """ModelPart / normal_part are the solid. Negative, modifier and support meshes are subtracted or ignored at slice time."""
    token = re.sub(r"[^a-z]", "", (kind or "").lower())
    return token in ("", "model", "modelpart", "normalpart")


def _zip_text(archive, *candidates: str) -> str | None:
    names = {n.lower(): n for n in archive.namelist()}
    for cand in candidates:
        actual = names.get(cand.lower())
        if actual:
            return archive.read(actual).decode("utf-8", "replace")
    return None


def _meta_children(el) -> dict:
    out = {}
    for child in el:
        if _local(child.tag) != "metadata":
            continue
        key = child.get("key")
        if key:
            out[key] = child.get("value") or ""
    return out


def _slic3r_volumes(text: str) -> dict:
    """object id -> [{first, last, name, type}]. first/last are inclusive triangle indexes, in file order."""
    root = ET.fromstring(text)
    by_object = {}
    for obj in root.iter():
        if _local(obj.tag) != "object":
            continue
        oid = obj.get("id")
        if not oid:
            continue
        vols = []
        for vol in obj:
            if _local(vol.tag) != "volume":
                continue
            meta = _meta_children(vol)
            try:
                first = int(vol.get("firstid") or 0)
                last = int(vol.get("lastid") or -1)
            except ValueError:
                continue
            vols.append({"first": first, "last": last, "name": meta.get("name") or "", "type": meta.get("volume_type") or "ModelPart"})
        if vols:
            by_object[str(oid)] = vols
    return by_object


def _bambu_roles(text: str) -> dict:
    """object or part id -> {name, type} for Bambu Studio / OrcaSlicer model_settings.config."""
    root = ET.fromstring(text)
    roles = {}
    for obj in root.iter():
        if _local(obj.tag) != "object":
            continue
        oid = obj.get("id")
        parts = [child for child in obj if _local(child.tag) == "part"]
        for part in parts:
            meta = _meta_children(part)
            role = {"name": meta.get("name") or "", "type": part.get("subtype") or "normal_part"}
            pid = part.get("id")
            if pid:
                roles[str(pid)] = role
            if oid and len(parts) == 1:
                roles.setdefault(str(oid), role)
    return roles


def _ini(text: str) -> dict:
    out = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line[0] in "#;" or "=" not in line:
            continue
        key, value = line.split("=", 1)
        out[key.strip()] = value.strip().strip('"')
    return out


def _first_token(value: str) -> str:
    return re.split(r"[;,]", value or "", maxsplit=1)[0].strip()


def _float_token(value) -> float | None:
    if isinstance(value, (list, tuple)):
        value = value[0] if value else None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    try:
        return float(_first_token(str(value)))
    except (TypeError, ValueError):
        return None


def _bed_size(shape) -> list | None:
    """Prusa bed_shape '0x0,250x0,250x210,0x210' or a list of those points. Returns [width, depth] in mm."""
    if isinstance(shape, (list, tuple)):
        parts = [str(p) for p in shape]
    elif isinstance(shape, str):
        parts = [p.strip() for p in shape.split(",")]
    else:
        return None
    pts = []
    for part in parts:
        token = part.lower().replace("×", "x")
        if "x" not in token:
            continue
        a, b = token.split("x", 1)
        try:
            pts.append((float(a), float(b)))
        except ValueError:
            continue
    if len(pts) < 2:
        return None
    width = max(p[0] for p in pts) - min(p[0] for p in pts)
    depth = max(p[1] for p in pts) - min(p[1] for p in pts)
    if width <= 0 or depth <= 0:
        return None
    return [width, depth]


def _printer_from_ini(text: str) -> dict:
    cfg = _ini(text)
    bed = _bed_size(cfg.get("bed_shape", ""))
    height = _float_token(cfg.get("max_print_height"))
    volume = [bed[0], bed[1], height] if bed and height and height > 0 else None
    model = cfg.get("printer_model") or ""
    name = cfg.get("printer_settings_id") or model
    return {
        "model": model,
        "name": name,
        "nozzle": _float_token(cfg.get("nozzle_diameter")),
        "layer_height": _float_token(cfg.get("layer_height")),
        "material": _first_token(cfg.get("filament_type", "")).upper() or None,
        "density": _float_token(cfg.get("filament_density")),
        "bed": bed,
        "build_volume": volume,
    }


def _printer_from_json(text: str) -> dict:
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return {}
    if not isinstance(data, dict):
        return {}
    bed = _bed_size(data.get("printable_area") or data.get("bed_shape") or "")
    height = _float_token(data.get("printable_height") or data.get("max_print_height"))
    volume = [bed[0], bed[1], height] if bed and height and height > 0 else None
    model = str(data.get("printer_model") or "")
    name = str(data.get("printer_settings_id") or model)
    material = data.get("filament_type") or ""
    return {
        "model": model,
        "name": name,
        "nozzle": _float_token(data.get("nozzle_diameter")),
        "layer_height": _float_token(data.get("layer_height")),
        "material": _first_token(material if isinstance(material, str) else str(material[0] if material else "")).upper() or None,
        "density": _float_token(data.get("filament_density")),
        "bed": bed,
        "build_volume": volume,
    }


def _merge_printer(*parts: dict) -> dict | None:
    out = {"model": "", "name": "", "nozzle": None, "layer_height": None, "material": None, "density": None, "bed": None, "build_volume": None}
    for part in parts:
        for key, value in part.items():
            if value not in (None, "", []):
                out[key] = value
    if not (out["model"] or out["name"] or out["build_volume"] or out["bed"]):
        return None
    return out


def _split_volumes(mesh: MeshArrays, vols: list, object_name: str, warnings: list) -> list:
    """Cut one mesh on slicer triangle ranges. The volume matrix is not applied: slicers bake it into the vertices."""
    n = len(mesh.tris)
    assigned = np.zeros(n, dtype=bool)
    pieces = []
    for vol in vols:
        first, last = int(vol["first"]), int(vol["last"])
        if last < first or first >= n or last < 0:
            warnings.append(f"{object_name}: volume '{vol.get('name') or vol.get('type')}' range {first}..{last} is outside the {n} triangles")
            continue
        first = max(0, first)
        last = min(n - 1, last)
        if assigned[first:last + 1].any():
            warnings.append(f"{object_name}: volume ranges overlap near triangle {first}")
        mask = np.zeros(n, dtype=bool)
        mask[first:last + 1] = ~assigned[first:last + 1]
        assigned[first:last + 1] = True
        if not mask.any():
            continue
        kind = vol.get("type") or "ModelPart"
        pieces.append({"name": vol.get("name") or object_name, "type": kind, "model": _is_model_volume(kind),
                       "arrays": mesh.select(mask)})
    if n and not assigned.all():
        mask = ~assigned
        pieces.append({"name": object_name, "type": "ModelPart", "model": True, "arrays": mesh.select(mask)})
        warnings.append(f"{object_name}: {int(mask.sum())} triangles were not listed in a slicer volume and stayed with the part")
    return pieces


def _object_pieces(entry: dict, oid, volume_table: dict, bambu_roles: dict, warnings: list) -> list:
    mesh = entry["mesh"]
    vols = volume_table.get(str(oid))
    role = bambu_roles.get(str(oid))
    if vols is None and role and not _is_model_volume(role.get("type") or ""):
        last = len(mesh.tris) - 1
        if last >= 0:
            vols = [{"first": 0, "last": last, "name": role.get("name") or entry["name"], "type": role["type"]}]
    if not vols:
        return [{"name": entry["name"], "type": "ModelPart", "model": True, "arrays": mesh}]
    return _split_volumes(mesh, vols, entry["name"], warnings)


def read_3mf(path: str, volumes: str = "model") -> list:
    """Every build item of a 3MF as [(name, MeshArrays in mm)]. Components and the build-item transform are applied.

    volumes:
      model (default): the solid only. A Prusa/SuperSlicer negative, modifier or support volume, and a
        Bambu/Orca negative part, are left out and listed on the result's project.
      negative: those left-out meshes, one item each.
      all: every slicer volume as its own item.
      raw: the mesh as stored, negatives included. project still names them.
    The triangle ranges in Metadata/*_model.config are indexes into that object's triangles, in file
    order. The volume matrix there is already baked into the vertices, so it is not applied again.
    """
    if volumes not in ("model", "all", "raw", "negative"):
        raise ValueError("volumes must be model, all, raw or negative")
    warnings: list = []
    with zipfile.ZipFile(path) as archive:
        model_names = [n for n in archive.namelist() if n.lower().endswith(".model")]
        if not model_names:
            raise ValueError(f"{path} has no 3D model part")
        main = next((n for n in model_names if n.lower().endswith("3d/3dmodel.model")), model_names[0])
        roots = {n: ET.fromstring(archive.read(n)) for n in model_names}
        volume_table: dict = {}
        for cand in ("Metadata/Slic3r_PE_model.config", "Metadata/Prusa_Slicer_model.config", "Metadata/SuperSlicer_model.config"):
            text = _zip_text(archive, cand)
            if not text:
                continue
            try:
                volume_table.update(_slic3r_volumes(text))
            except ET.ParseError:
                warnings.append(f"{cand} could not be read; volumes were not split")
        bambu_roles: dict = {}
        bambu = _zip_text(archive, "Metadata/model_settings.config")
        if bambu:
            try:
                bambu_roles = _bambu_roles(bambu)
            except ET.ParseError:
                warnings.append("Metadata/model_settings.config could not be read")
        printers = []
        for cand in ("Metadata/Slic3r_PE.config", "Metadata/Prusa_Slicer.config", "Metadata/SuperSlicer.config"):
            text = _zip_text(archive, cand)
            if text:
                printers.append(_printer_from_ini(text))
        project_json = _zip_text(archive, "Metadata/project_settings.config")
        if project_json and project_json.lstrip().startswith("{"):
            printers.append(_printer_from_json(project_json))
    root = roots[main]
    factor = _UNIT_MM.get(root.get("unit", "millimeter"), 1.0)
    objects = {}
    for model_name, model in roots.items():
        for res in model.iter():
            if _local(res.tag) != "object":
                continue
            oid = res.get("id")
            mesh_el = next((c for c in res if _local(c.tag) == "mesh"), None)
            comps = next((c for c in res if _local(c.tag) == "components"), None)
            entry = {"name": res.get("name") or f"object {oid}", "mesh": None, "components": []}
            if mesh_el is not None:
                verts, tris = [], []
                for el in mesh_el.iter():
                    tag = _local(el.tag)
                    if tag == "vertex":
                        verts.append((float(el.get("x")), float(el.get("y")), float(el.get("z"))))
                    elif tag == "triangle":
                        tris.append((int(el.get("v1")), int(el.get("v2")), int(el.get("v3"))))
                entry["mesh"] = MeshArrays(verts, tris)
            if comps is not None:
                for comp in comps:
                    if _local(comp.tag) == "component":
                        entry["components"].append((comp.get("objectid"), _matrix_3mf(comp.get("transform"))))
            key = oid if model_name == main else f"{model_name}#{oid}"
            objects.setdefault(oid, entry)
            objects[key] = entry

    used_ids: set = set()

    def flatten(oid, matrix, depth=0, whole=False):
        entry = objects.get(oid)
        if entry is None or depth > 16:
            return []
        out = []
        if entry["mesh"] is not None:
            if str(oid) in volume_table or str(oid) in bambu_roles:
                used_ids.add(str(oid))
            if whole:
                out.append({"name": entry["name"], "type": "ModelPart", "model": True,
                            "arrays": entry["mesh"].transformed(matrix), "object": entry["name"]})
            else:
                for piece in _object_pieces(entry, oid, volume_table, bambu_roles, warnings):
                    arrays = piece["arrays"].transformed(matrix)
                    out.append({**piece, "arrays": arrays, "object": entry["name"]})
        for child, m in entry["components"]:
            out += flatten(child, matrix @ m, depth + 1, whole)
        return out

    items_raw = []
    records = []
    build = next((c for c in root if _local(c.tag) == "build"), None)
    for item in (build if build is not None else []):
        if _local(item.tag) != "item":
            continue
        oid = item.get("objectid")
        obj_name = objects.get(oid, {}).get("name", oid)
        matrix = _matrix_3mf(item.get("transform"))
        split = flatten(oid, matrix, whole=False)
        for piece in split:
            records.append({"object": obj_name, "name": piece["name"], "type": piece["type"],
                            "triangles": int(len(piece["arrays"])), "model": bool(piece["model"])})
        # raw keeps file order, negatives included. The other modes use the split pieces.
        pieces = flatten(oid, matrix, whole=True) if volumes == "raw" else split
        for piece in pieces:
            piece["arrays"] = piece["arrays"].scaled(factor)
        items_raw.append((obj_name, pieces))
    if volume_table:
        missed = [i for i in volume_table if i not in used_ids]
        if volume_table and not used_ids:
            warnings.append("slicer volume ranges did not match a 3MF object id; the meshes were read whole")
        elif missed:
            warnings.append(f"slicer volumes for object id {', '.join(missed[:6])} did not match a mesh")
    project = {"printer": _merge_printer(*printers), "volumes": records, "warnings": warnings}

    def chosen(pieces):
        if volumes == "raw" or volumes == "all":
            return pieces
        want_model = volumes == "model"
        return [p for p in pieces if bool(p["model"]) == want_model]

    items = ThreeMF([], project)
    for obj_name, pieces in items_raw:
        pick = chosen(pieces)
        if volumes in ("all", "negative"):
            for piece in pick:
                label = piece["name"] or obj_name
                if label != obj_name:
                    label = f"{obj_name} / {label}"
                if not piece["model"]:
                    label = f"{label} [{piece['type']}]"
                arrays = piece["arrays"]
                arrays.name = label
                items.append((label, arrays))
            continue
        if not pick:
            continue
        arrays = concat([p["arrays"] for p in pick]).scaled(1.0)
        arrays.name = obj_name
        items.append((obj_name, arrays))
    if not items:
        dropped = [r for r in records if not r["model"]]
        if dropped and volumes == "model":
            names = ", ".join(f"{r['name'] or r['type']} ({r['triangles']} tris)" for r in dropped[:8])
            raise ValueError(f"{path} has no model solid; slicer volumes left out: {names}")
        raise ValueError(f"{path} has no build items with geometry")
    return items


def read_svg_mesh(path: str, plane: str = "xy") -> MeshArrays:
    """An SVG's outline as a flat triangulated region (no thickness), in SVG user units, y up."""
    from . import geom2d

    loops = geom2d.svg_loops(path)
    pts, tris = geom2d.triangulate(loops)
    if not len(tris):
        raise ValueError(f"{path} has no filled region")
    zeros = np.zeros(len(pts))
    if plane == "xz":
        verts = np.column_stack([pts[:, 0], zeros, pts[:, 1]])
    elif plane == "yz":
        verts = np.column_stack([zeros, pts[:, 0], pts[:, 1]])
    else:
        verts = np.column_stack([pts[:, 0], pts[:, 1], zeros])
    return MeshArrays(verts, tris, name=os.path.splitext(os.path.basename(path))[0])


_FILE_CACHE: dict = {}
_CACHE_VERTS = 12_000_000


def read_mesh_file(path: str, plane: str = "xy", part=None) -> MeshArrays:
    """STL, OBJ, 3MF (all build items) or SVG (flat outline in plane) as one mesh, in the file's units.
    part: a group name or glob (an OBJ material or group, a 3MF item) to keep only that part. Parsed
    files are cached by path, size and modification time, so measuring a big reference again is quick."""
    stat = os.stat(path)
    key = (os.path.normcase(os.path.abspath(path)), stat.st_size, stat.st_mtime_ns, plane)
    arrays = _FILE_CACHE.get(key)
    if arrays is None:
        arrays = _read_mesh_file(path, plane)
        _FILE_CACHE[key] = arrays
        # Keep the cache to a few large files.
        while sum(len(a.verts) for a in _FILE_CACHE.values()) > _CACHE_VERTS and len(_FILE_CACHE) > 1:
            _FILE_CACHE.pop(next(iter(_FILE_CACHE)))
    return arrays.part(part) if part else arrays


def _read_mesh_file(path: str, plane: str = "xy") -> MeshArrays:
    ext = os.path.splitext(path)[1].lower()
    if ext == ".stl":
        return read_stl(path)
    if ext == ".obj":
        return read_obj(path)
    if ext == ".3mf":
        items = read_3mf(path)
        out = concat([a.scaled(1.0) for _n, a in items])
        out.material = np.concatenate([np.full(len(a.tris), i, dtype=np.int64) for i, (_n, a) in enumerate(items)])
        out.groups = [n for n, _a in items]
        out.name = os.path.splitext(os.path.basename(path))[0]
        return out
    if ext == ".svg":
        return read_svg_mesh(path, plane)
    raise ValueError(f"unsupported file type {ext}; use .stl, .obj, .3mf or .svg")


# ----------------------------------------------------------------------------- writing files
def _atomic_write(path: str, write) -> None:
    os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
    tmp = path + ".tmp"
    try:
        write(tmp)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass


def write_stl(path: str, arrays: MeshArrays, binary: bool = True, name: str = "") -> int:
    """Write triangles as STL (binary by default). Returns the file size."""
    normals, _areas = tri_normals_areas(arrays)
    corners = arrays.verts[arrays.tris]
    label = (name or arrays.name or "vsblender")[:60]

    def write(tmp):
        if binary:
            data = np.zeros(len(arrays.tris), dtype=_STL_DTYPE)
            data["normal"] = normals.astype(np.float32)
            data["v"] = corners.astype(np.float32)
            with open(tmp, "wb") as handle:
                header = f"VSBlender {label}".encode("ascii", "replace")[:80].ljust(80, b" ")
                handle.write(header)
                handle.write(struct.pack("<I", len(arrays.tris)))
                handle.write(data.tobytes())
        else:
            safe = re.sub(r"\s+", "_", label) or "vsblender"
            with open(tmp, "w", encoding="ascii", newline="\n") as handle:
                handle.write(f"solid {safe}\n")
                for n, (a, b, c) in zip(normals, corners):
                    handle.write(f"  facet normal {n[0]:.6e} {n[1]:.6e} {n[2]:.6e}\n    outer loop\n")
                    for p in (a, b, c):
                        handle.write(f"      vertex {p[0]:.6e} {p[1]:.6e} {p[2]:.6e}\n")
                    handle.write("    endloop\n  endfacet\n")
                handle.write(f"endsolid {safe}\n")

    _atomic_write(path, write)
    return os.path.getsize(path)


def write_obj(path: str, parts) -> int:
    """Write [(name, MeshArrays)] as one OBJ with an object per part (geometry only)."""
    def write(tmp):
        base = 1
        with open(tmp, "w", encoding="utf-8", newline="\n") as handle:
            handle.write("# VSBlender\n")
            for name, arrays in parts:
                handle.write(f"o {re.sub(r'\s+', '_', name) or 'part'}\n")
                buf = io.StringIO()
                np.savetxt(buf, arrays.verts, fmt="v %.6f %.6f %.6f")
                handle.write(buf.getvalue())
                buf = io.StringIO()
                np.savetxt(buf, arrays.tris + base, fmt="f %d %d %d")
                handle.write(buf.getvalue())
                base += len(arrays.verts)

    _atomic_write(path, write)
    return os.path.getsize(path)


_CT = """<?xml version="1.0" encoding="UTF-8"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
 <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
 <Default Extension="model" ContentType="application/vnd.ms-package.3dmanufacturing-3dmodel+xml"/>
 <Default Extension="png" ContentType="image/png"/>
</Types>
"""


def _rels(thumbnail: bool) -> str:
    thumb = ('\n <Relationship Target="/Metadata/thumbnail.png" Id="rel1" '
             'Type="http://schemas.openxmlformats.org/package/2006/relationships/metadata/thumbnail"/>') if thumbnail else ""
    return ('<?xml version="1.0" encoding="UTF-8"?>\n'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">\n'
            ' <Relationship Target="/3D/3dmodel.model" Id="rel0" '
            'Type="http://schemas.microsoft.com/3dmanufacturing/2013/01/3dmodel"/>' + thumb + "\n</Relationships>\n")


def _xml(text: str) -> str:
    return (str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;"))


def write_3mf(path: str, parts, title: str = "", application: str = "VSBlender", assembly: bool = False,
              thumbnail: str | None = None, metadata: dict | None = None) -> int:
    """Write [(name, MeshArrays in mm)] as a 3MF package (core spec, unit millimeter).

    One object and one build item per part, so slicers show each part by name on one plate.
    assembly=True wraps the parts in one object with components (one object with several parts).
    """
    model = io.StringIO()
    model.write('<?xml version="1.0" encoding="UTF-8"?>\n')
    model.write('<model unit="millimeter" xml:lang="en-US" xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02">\n')
    meta = {"Title": title or (parts[0][0] if parts else "model"), "Application": application}
    meta.update(metadata or {})
    for key, value in meta.items():
        model.write(f' <metadata name="{_xml(key)}">{_xml(value)}</metadata>\n')
    model.write(" <resources>\n")
    ids = []
    for index, (name, arrays) in enumerate(parts, start=1):
        model.write(f'  <object id="{index}" type="model" name="{_xml(name)}">\n   <mesh>\n    <vertices>\n')
        buf = io.StringIO()
        np.savetxt(buf, arrays.verts, fmt='     <vertex x="%.5f" y="%.5f" z="%.5f"/>')
        model.write(buf.getvalue())
        model.write("    </vertices>\n    <triangles>\n")
        buf = io.StringIO()
        np.savetxt(buf, arrays.tris, fmt='     <triangle v1="%d" v2="%d" v3="%d"/>')
        model.write(buf.getvalue())
        model.write("    </triangles>\n   </mesh>\n  </object>\n")
        ids.append(index)
    build_ids = ids
    if assembly and len(ids) > 1:
        top = len(ids) + 1
        model.write(f'  <object id="{top}" type="model" name="{_xml(title or "Assembly")}">\n   <components>\n')
        for i in ids:
            model.write(f'    <component objectid="{i}"/>\n')
        model.write("   </components>\n  </object>\n")
        build_ids = [top]
    model.write(" </resources>\n <build>\n")
    for i in build_ids:
        model.write(f'  <item objectid="{i}"/>\n')
    model.write(" </build>\n</model>\n")
    has_thumb = bool(thumbnail and os.path.isfile(thumbnail))

    def write(tmp):
        with zipfile.ZipFile(tmp, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("[Content_Types].xml", _CT)
            archive.writestr("_rels/.rels", _rels(has_thumb))
            archive.writestr("3D/3dmodel.model", model.getvalue())
            if has_thumb:
                archive.write(thumbnail, "Metadata/thumbnail.png")

    _atomic_write(path, write)
    return os.path.getsize(path)


def mesh_stats(arrays: MeshArrays, weld_tol: float) -> dict:
    """Quick watertightness summary: counts only, for export warnings and Solid.check."""
    welded, _remap = weld(arrays, weld_tol)
    stats = edge_stats(welded)
    labels = components(welded)
    va = volume_area(welded)
    return {"tris": int(len(arrays.tris)), "open_edges": stats["open_edges"],
            "non_manifold_edges": stats["non_manifold_edges"], "flipped_edges": stats["flipped_edges"],
            "degenerate_tris": stats["degenerate_tris"], "shells": int(labels.max() + 1) if len(labels) else 0,
            "volume": va["volume"], "area": va["area"],
            "watertight": stats["open_edges"] == 0 and stats["non_manifold_edges"] == 0 and stats["flipped_edges"] == 0}


def fmt_num(value: float, digits: int = 4) -> str:
    """value with at most digits decimals, trailing zeros of the decimals removed (90.0 -> "90")."""
    if not math.isfinite(value):
        return str(value)
    text = f"{value:.{max(0, int(digits))}f}"
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text if text not in ("", "-0") else "0"
