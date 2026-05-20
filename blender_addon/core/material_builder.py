"""Rebuilding a clean PBR node graph from the d4extract sidecar.

After Blender's glTF importer runs, every material already has a
Principled BSDF with the embedded textures roughly wired. This module
*replaces* that graph with a deterministic one driven by the
``.materials.json`` sidecar so the D4-specific handling is applied
consistently:

* base color in sRGB, AO multiplied into it;
* normal map with the DX→GL green-channel flip;
* roughness / metallic unpacked from d4extract's combined ``MR_`` image;
* emissive wired to ``Emission Color`` + ``Emission Strength``;
* Hero_Hair alpha wired straight from the base-color image's alpha;
* dye / skin masks created as labelled but unconnected nodes so an
  artist can pick them up later.

Idempotency comes for free: the node tree is cleared and rebuilt from
scratch every call, so the node count never grows.

Embedded images are located by d4extract's stable naming scheme
(``<ROLE>_<sno_id>``, ``MR_<r>_<m>``, ``HAIR_BC_<sno>``) which Blender's
glTF importer preserves on the image datablocks.
"""

from __future__ import annotations

import re

import bpy

from .sidecar import Sidecar, SidecarMaterial

# Texture-image nodes carry this custom property so apply_variants can
# find "the node driving role R" without re-deriving it from links.
NODE_ROLE_PROP = "d4_role"

# Parametric skin-tone chain node names (deterministic so the apply
# operator can find / reuse them).
SKIN_HSV_NODE = "d4_skin_hsv"
SKIN_DARKEN_NODE = "d4_skin_darken"
# Parametric hair-colour tint node name (same deterministic-naming
# rationale — the apply operator finds / reuses it by name).
HAIR_TINT_NODE = "d4_hair_tint"
# RGB→luminance node inserted between BASE_COLOR and BSDF.Alpha for
# hair textures whose strand silhouette is encoded in RGB lightness
# rather than the alpha channel — see ``_wire_hair_bsdf_alpha``.
HAIR_ALPHA_FROM_RGB_NODE = "d4_hair_alpha_from_rgb"
# Contrast-boost POW node inserted only when ``_wire_hair_bsdf_alpha``
# takes the runtime-fallback path (sidecar said alpha was real but the
# loaded image arrived as 3-channel, so its alpha output returns 1.0).
# Materials that hit this path (eyelashes) have very dim RGB (≈ 0.02);
# plain RGBToBW alone would render only ~2% of each strand pixel.
HAIR_ALPHA_BOOST_NODE = "d4_hair_alpha_boost"
# Exponent for the boost POW(x, exponent). 0.2 maps 0.02 -> 0.46 (visible),
# 0.005 -> 0.35, while keeping pure-black gaps at 0.0. Tunable: drop to
# 0.15 if eyelashes still look stippled, raise to 0.3 if gaps look noisy.
_HAIR_ALPHA_BOOST_EXPONENT = 0.2
# Average alpha threshold above which a hair texture's alpha channel is
# treated as carrying no per-pixel info (uniformly opaque). Survey of
# every on-disk export: every facial-hair / beard / stubble / mustache
# atlas reports avg alpha = 1.000 exactly; eyelashes 0.256; head hair
# 0.30–0.50. A generous 0.99 ceiling cleanly separates the two camps.
_HAIR_ALPHA_UNIFORM_THRESHOLD = 0.99
# Mid-grey constant fed into the HSV chain when the material has no
# usable BASE_COLOR — either none at all, or a known-black placeholder
# (armor_skin_mat's black.tex). One node name covers both cases.
SKIN_GREY_NODE = "d4_skin_placeholder_grey"

# Roles wired straight into the Principled BSDF.
_DYE_ROLES = ("DYE_MASK", "DYE_RAMP", "DYE_MASK_2", "SKIN_MASK")

_SKIP_IMAGE_NAMES = frozenset({"Render Result", "Viewer Node"})


# ─── Public API ──────────────────────────────────────────────────────

def setup_object_materials(
    obj: bpy.types.Object,
    sidecar,
    *,
    strip_color0: bool = True,
) -> list[str]:
    """Run :func:`rebuild_pbr` for every material slot on ``obj``.

    Returns a list of human-readable warning strings (missing sidecar
    entry, missing image, ...). When ``strip_color0`` is set, D4's
    blend/AO vertex-colour attributes are removed from the mesh too —
    they otherwise tint the base colour.
    """
    warnings: list[str] = []
    if obj.type != "MESH":
        return warnings

    for slot in obj.material_slots:
        mat = slot.material
        if mat is None:
            continue
        sm = sidecar.material_by_name(_base_name(mat.name)) if sidecar else None
        if sm is None:
            warnings.append(f"{mat.name}: no sidecar material entry")
            continue
        warnings.extend(rebuild_pbr(mat, sm))

    if strip_color0:
        strip_color_attributes(obj)
    return warnings


def rebuild_pbr(mat: bpy.types.Material, sm: SidecarMaterial) -> list[str]:
    """Replace ``mat``'s node tree with a sidecar-driven PBR graph.

    Idempotent — the tree is cleared first, so repeated calls leave the
    node count unchanged. Returns a list of warning strings.
    """
    warnings: list[str] = []
    mat.use_nodes = True
    nt = mat.node_tree
    nt.nodes.clear()

    output = nt.nodes.new("ShaderNodeOutputMaterial")
    output.location = (900, 0)
    bsdf = nt.nodes.new("ShaderNodeBsdfPrincipled")
    bsdf.location = (500, 0)
    nt.links.new(bsdf.outputs["BSDF"], output.inputs["Surface"])

    treatment = mat.get("d4_alpha_treatment", "")
    is_hair = (sm.shader_map or "").lower() == "hero_hair" or treatment.startswith(
        "hero_hair"
    )

    # ── Base color (+ AO multiply, + hair alpha) ──────────────────
    bc_image = _base_color_image(sm)
    color_out = None
    if bc_image is not None:
        bc_node = _make_tex_node(
            nt, "d4_BASE_COLOR", "BASE_COLOR", bc_image, "sRGB", (-700, 350),
        )
        color_out = bc_node.outputs["Color"]
        ao_image = _role_image(sm, "AO")
        if ao_image is not None:
            ao_node = _make_tex_node(
                nt, "d4_AO", "AO", ao_image, "Non-Color", (-700, 60),
            )
            color_out = _multiply_color(
                nt, color_out, ao_node.outputs["Color"], (-300, 250),
            )
        nt.links.new(color_out, bsdf.inputs["Base Color"])
        # Every D4 hair material — head hair, facial hair, eyelashes,
        # full beards (``hero_hair_2uv``), helmet-attached hair
        # (``*_Hair_mat`` with ``hero_armorHair``) — carries a
        # per-pixel strand silhouette, but D4 encodes it two ways:
        # in the alpha channel for head hair / eyelashes, and in
        # RGB lightness (alpha uniformly 1.0) for facial hair /
        # beards / stubble. ``_wire_hair_bsdf_alpha`` dispatches
        # on the encoding so DITHERED always has a real mask to
        # threshold against, regardless of which texture flavour
        # was authored. Stays in sync with ``is_hair_material`` in
        # ``d4extract/formats/variants.py`` — keep both broadenings
        # together if the shader set grows.
        shader_lc = (sm.shader_map or "").lower()
        name_lc = (sm.name or "").lower()
        is_hair_shader = (
            shader_lc.startswith("hero_hair")
            or shader_lc == "hair_pbr_igc"
            or name_lc.endswith("_hair_mat")
        )
        if (
            treatment in ("hero_hair_layered", "blend_punch_through_alpha")
            or is_hair_shader
        ):
            _wire_hair_bsdf_alpha(nt, bsdf, bc_node, sm)
    else:
        bsdf.inputs["Base Color"].default_value = (
            *sm.base_color_factor[:3], 1.0,
        )

    # ── Roughness / metallic (d4extract packs them into one MR image) ─
    mr_image = _mr_image(sm)
    if mr_image is not None:
        mr_node = _make_tex_node(
            nt, "d4_MR", "ROUGHNESS", mr_image, "Non-Color", (-700, -250),
        )
        sep = nt.nodes.new("ShaderNodeSeparateColor")
        sep.location = (-350, -250)
        nt.links.new(mr_node.outputs["Color"], sep.inputs["Color"])
        # glTF metallicRoughness convention: roughness in G, metallic in B.
        nt.links.new(sep.outputs["Green"], bsdf.inputs["Roughness"])
        nt.links.new(sep.outputs["Blue"], bsdf.inputs["Metallic"])
    else:
        bsdf.inputs["Roughness"].default_value = (
            0.6 if is_hair else _clamp01(sm.roughness_factor, 0.5)
        )
        bsdf.inputs["Metallic"].default_value = 0.0

    # ── Normal (DX→GL: invert green) ──────────────────────────────
    normal_image = _role_image(sm, "NORMAL")
    if normal_image is not None:
        n_node = _make_tex_node(
            nt, "d4_NORMAL", "NORMAL", normal_image, "Non-Color", (-700, -600),
        )
        sep_n = nt.nodes.new("ShaderNodeSeparateColor")
        sep_n.location = (-350, -600)
        nt.links.new(n_node.outputs["Color"], sep_n.inputs["Color"])
        # Flip green: GL expects +Y up, D4's BC5 normals are -Y (DX).
        flip = nt.nodes.new("ShaderNodeMath")
        flip.location = (-150, -650)
        flip.operation = "SUBTRACT"
        flip.inputs[0].default_value = 1.0
        nt.links.new(sep_n.outputs["Green"], flip.inputs[1])
        comb = nt.nodes.new("ShaderNodeCombineColor")
        comb.location = (50, -600)
        nt.links.new(sep_n.outputs["Red"], comb.inputs["Red"])
        nt.links.new(flip.outputs["Value"], comb.inputs["Green"])
        nt.links.new(sep_n.outputs["Blue"], comb.inputs["Blue"])
        nmap = nt.nodes.new("ShaderNodeNormalMap")
        nmap.location = (250, -600)
        nt.links.new(comb.outputs["Color"], nmap.inputs["Color"])
        nt.links.new(nmap.outputs["Normal"], bsdf.inputs["Normal"])

    # ── Emissive ──────────────────────────────────────────────────
    em_image = _role_image(sm, "EMISSIVE")
    em_strength = max(sm.emissive_factor) if sm.emissive_factor else 0.0
    if em_image is not None:
        em_node = _make_tex_node(
            nt, "d4_EMISSIVE", "EMISSIVE", em_image, "sRGB", (-700, -950),
        )
        nt.links.new(em_node.outputs["Color"], bsdf.inputs["Emission Color"])
        bsdf.inputs["Emission Strength"].default_value = em_strength or 1.0
    elif em_strength > 0.0:
        bsdf.inputs["Emission Color"].default_value = (
            *sm.emissive_factor, 1.0,
        )
        bsdf.inputs["Emission Strength"].default_value = em_strength

    # ── Dye / skin masks — created, labelled, left unconnected ────
    mask_y = 350
    for role in _DYE_ROLES:
        img = _role_image(sm, role)
        if img is None:
            continue
        _make_tex_node(
            nt, f"d4_{role.lower()}", role, img, "Non-Color", (-1100, mask_y),
        )
        mask_y -= 320

    return warnings


def _base_color_is_placeholder(sm: SidecarMaterial) -> bool:
    """Whether ``sm``'s BASE_COLOR is a known-black placeholder.

    Triggered for the ``armor_skin_mat`` exposed-skin cutouts, whose
    static export points BASE_COLOR at ``black.tex`` (``avg_rgba`` ~ 0).
    Feeding that through the HSV chain yields black-from-black, so the
    chain takes the mid-grey input instead. A material with no
    BASE_COLOR texture at all also counts — that is the original
    "no texture" fallback, kept here so both share one code path.
    """
    base = sm.texture_for_role("BASE_COLOR")
    if base is None:
        return True
    path = (base.path or "").lower()
    if "black.tex" in path or "black_color" in path:
        return True
    # Texture present but the resolved base colour is essentially black
    # — covers other-named placeholders.
    bcf = sm.base_color_factor
    return max(bcf[:3]) < 0.05


def _placeholder_grey(
    nt: bpy.types.NodeTree, bsdf: bpy.types.Node,
) -> bpy.types.NodeSocket:
    """Get-or-create the mid-grey RGB input node for the skin chain.

    Idempotent — reuses an existing ``d4_skin_placeholder_grey`` node.
    """
    existing = nt.nodes.get(SKIN_GREY_NODE)
    if existing is not None:
        return existing.outputs[0]
    rgb = nt.nodes.new("ShaderNodeRGB")
    rgb.name = rgb.label = SKIN_GREY_NODE
    rgb.location = (bsdf.location.x - 900, bsdf.location.y + 320)
    rgb.outputs[0].default_value = (0.5, 0.5, 0.5, 1.0)
    return rgb.outputs[0]


def setup_skin_tone_chain(
    mat: bpy.types.Material,
    sidecar_mat: SidecarMaterial | None = None,
) -> tuple[bpy.types.Node, bpy.types.Node] | None:
    """Insert the parametric skin-tone chain on ``mat``, idempotently.

    Wires ``[base colour] -> d4_skin_hsv -> d4_skin_darken -> BSDF Base
    Color``:

    * ``d4_skin_hsv`` — a ``ShaderNodeHueSaturation``; the apply operator
      writes the tone's Hue / Saturation / Value into its sockets.
    * ``d4_skin_darken`` — a ``ShaderNodeMix`` (RGBA, Multiply); the
      apply operator writes ``(darken, darken, darken)`` into input B,
      so the colour is multiplied by ``flDarken`` directly. (``flDarken``
      in the data is a *brightness multiplier* — 1.0 = unchanged, 0.3 =
      strongly darkened — so the chain multiplies by it as-is rather
      than by ``1 - flDarken``.)

    The chain's input is:

    * the existing BASE_COLOR socket, normally;
    * the ``d4_skin_placeholder_grey`` mid-grey constant when the
      material has no usable BASE_COLOR — either none at all, or a
      known-black placeholder (``armor_skin_mat``'s ``black.tex``).
      Pass ``sidecar_mat`` to enable the black-placeholder check;
      without it only the genuinely-unlinked case falls back to grey.

    Returns ``(hsv_node, darken_node)``, or ``None`` when the material
    has no Principled BSDF. Idempotent: a second call finds the existing
    nodes and returns them without duplicating anything.
    """
    if not mat.use_nodes or mat.node_tree is None:
        mat.use_nodes = True
    nt = mat.node_tree
    bsdf = next(
        (n for n in nt.nodes if n.type == "BSDF_PRINCIPLED"), None,
    )
    if bsdf is None:
        return None

    existing_hsv = nt.nodes.get(SKIN_HSV_NODE)
    existing_darken = nt.nodes.get(SKIN_DARKEN_NODE)
    if existing_hsv is not None and existing_darken is not None:
        return existing_hsv, existing_darken  # already inserted — reuse

    base_in = bsdf.inputs["Base Color"]
    # A known-black placeholder (or no BASE_COLOR at all) must not feed
    # the HSV chain — it would produce black-from-black. Substitute the
    # mid-grey constant so the tone has something real to tint.
    is_placeholder = (
        sidecar_mat is not None and _base_color_is_placeholder(sidecar_mat)
    )
    if is_placeholder:
        src_socket = _placeholder_grey(nt, bsdf)
    elif base_in.is_linked:
        src_socket = base_in.links[0].from_socket
    else:
        # No BASE_COLOR image (the pitch-black body case) — same grey
        # fallback.
        src_socket = _placeholder_grey(nt, bsdf)

    hsv = nt.nodes.new("ShaderNodeHueSaturation")
    hsv.name = hsv.label = SKIN_HSV_NODE
    hsv.location = (bsdf.location.x - 600, bsdf.location.y + 280)

    darken = nt.nodes.new("ShaderNodeMix")
    darken.name = darken.label = SKIN_DARKEN_NODE
    darken.data_type = "RGBA"
    darken.blend_type = "MULTIPLY"
    darken.location = (bsdf.location.x - 300, bsdf.location.y + 280)
    # ShaderNodeMix RGBA layout: inputs[0]=Factor, [6]=A, [7]=B, out[2].
    darken.inputs[0].default_value = 1.0
    darken.inputs[7].default_value = (1.0, 1.0, 1.0, 1.0)  # no darken yet

    nt.links.new(src_socket, hsv.inputs["Color"])
    nt.links.new(hsv.outputs["Color"], darken.inputs[6])
    nt.links.new(darken.outputs[2], base_in)
    return hsv, darken


# Threshold on the BASE_COLOR texture's average RGB max channel below
# which a Multiply blend would drown the chosen tint. Sits comfortably
# between observed near-white head-hair textures (avg ≈ 0.97) and dark
# facial-hair / eyelash textures (avg ≈ 0.02–0.19) in D4's hair atlas.
_HAIR_DARK_BASE_COLOR_THRESHOLD = 0.30


def _hair_base_color_is_dark(
    sidecar_mat: SidecarMaterial | None,
    threshold: float = _HAIR_DARK_BASE_COLOR_THRESHOLD,
) -> bool:
    """Whether the hair material's BASE_COLOR is too dark to multiply.

    D4 ships hair textures in two flavours:

    * **head hair** — RGB near-white, intended to be Multiply-tinted at
      runtime by the chosen ``HairColor`` preset;
    * **facial hair / eyelashes** — RGB near-black; the strand
      silhouette lives in the alpha channel, and D4's runtime
      overrides the dark RGB with the chosen tone via a shader uniform
      rather than a multiply.

    Multiply blend against a near-black ``Color1`` produces near-black
    output regardless of ``Color2`` (the tint), so for dark BASE_COLOR
    textures :func:`setup_hair_color_chain` leaves ``Color1`` at its
    neutral-white default instead of linking the dark texture in —
    ``Multiply(white, tint) = tint``, and the strand silhouette still
    works because :func:`rebuild_pbr` wires BSDF.Alpha from
    BASE_COLOR.Alpha separately.

    Returns ``False`` when ``sidecar_mat`` is missing or has no
    BASE_COLOR texture — the caller falls back to its existing
    link-the-source path.
    """
    if sidecar_mat is None:
        return False
    base = sidecar_mat.texture_for_role("BASE_COLOR")
    if base is None:
        return False
    return max(base.avg_rgba[:3]) < threshold


def _hair_alpha_is_in_rgb(
    sidecar_mat: SidecarMaterial | None,
    rgb_threshold: float = _HAIR_DARK_BASE_COLOR_THRESHOLD,
    alpha_uniform_threshold: float = _HAIR_ALPHA_UNIFORM_THRESHOLD,
) -> bool:
    """Whether the strand silhouette is encoded in BASE_COLOR's RGB.

    D4 hair textures use two encodings of the per-pixel strand mask:

    * **Real per-pixel alpha** — head hair (RGB near-white,
      ``avg_rgba.a`` ≈ 0.3–0.5) and eyelashes (``hair_pbr_igc`` —
      RGB very dark *but* ``avg_rgba.a`` ≈ 0.26). The alpha channel
      carries the mask; wire ``BASE_COLOR.Alpha → BSDF.Alpha``.
    * **RGB lightness** — facial hair / beards / stubble / mustaches
      (RGB near-black, ``avg_rgba.a`` = 1.000 exactly). The alpha
      channel is uniformly opaque and carries no information; the
      mask is white-on-black in RGB. Wire
      ``BASE_COLOR.Color → RGBToBW → BSDF.Alpha``.

    The discriminant requires **both**: alpha at the uniform-opaque
    ceiling (no per-pixel variation) **and** RGB dark enough that
    "white strands on black" is the only plausible content. Either
    signal alone would misclassify a corner case:

    * Dark RGB alone misclassifies eyelashes (real alpha, dark RGB).
    * Uniform alpha alone could misclassify a hypothetical
      ``alpha=1.0`` mid-RGB texture as a strand atlas.

    Returns ``False`` when ``sidecar_mat`` is missing or has no
    BASE_COLOR — the caller falls back to the alpha-channel path
    (preserves pre-fix behaviour).

    Distinct from :func:`_hair_base_color_is_dark` on purpose: that
    predicate drives the tint chain's ``Color1`` decision and fires
    for *any* dark-RGB hair texture (including eyelashes, where
    multiplying tint into near-black RGB would drown the colour).
    """
    if sidecar_mat is None:
        return False
    base = sidecar_mat.texture_for_role("BASE_COLOR")
    if base is None:
        return False
    rgb_max = max(base.avg_rgba[:3])
    alpha_avg = base.avg_rgba[3]
    return rgb_max < rgb_threshold and alpha_avg >= alpha_uniform_threshold


# Three-way enum for the hair-alpha wiring decision. Kept as string
# constants instead of an Enum so the pytest tests stay free of bpy
# import side-effects (this module already imports ``bpy`` at top, but
# the routing helper itself touches no Blender API and is loaded
# through the bpy-stubbed test harness).
_HAIR_ALPHA_ROUTE_ALPHA = "alpha"          # BASE_COLOR.Alpha -> BSDF.Alpha
_HAIR_ALPHA_ROUTE_RGB = "rgb"              # BASE_COLOR.Color -> RGBToBW -> BSDF.Alpha
_HAIR_ALPHA_ROUTE_RGB_BOOST = "rgb_boost"  # ... -> RGBToBW -> POW boost -> BSDF.Alpha


def _image_alpha_is_effectively_uniform(
    img,
    ceiling: float = _HAIR_ALPHA_UNIFORM_THRESHOLD,
    sample_count: int = 1024,
) -> bool:
    """Whether a loaded ``bpy.types.Image``'s alpha channel carries no info.

    Returns ``True`` when:

    * the image has fewer than 4 channels (the PNG-embed step dropped
      alpha and Blender loaded the texture as RGB), OR
    * the image has 4 channels but every sampled alpha pixel sits at
      the ``ceiling`` (default ``_HAIR_ALPHA_UNIFORM_THRESHOLD``) —
      i.e. alpha is uniformly opaque despite being present.

    This is the **authoritative** "no usable alpha mask" check; it
    catches the case the sidecar's ``avg_rgba.a`` misses. Empirically
    eyelash textures arrive with 4 channels but uniformly-opaque
    alpha, contradicting the sidecar's reported ``avg_rgba.a`` ≈
    0.256 — so the sidecar metadata cannot be trusted alone.

    A stride-sampled scan (default ~1024 pixels) keeps the cost
    bounded even for 2k×2k textures; the loop exits early on the
    first non-uniform sample, so non-uniform images are decided in
    O(few samples).

    ``img=None`` returns ``False`` (the caller cannot decide → trust
    the sidecar / fall to the alpha-channel path).
    """
    if img is None:
        return False
    channels = getattr(img, "channels", 4)
    if channels < 4:
        return True
    size = getattr(img, "size", None)
    if not size or size[0] <= 0 or size[1] <= 0:
        return False
    pixels = getattr(img, "pixels", None)
    if pixels is None or len(pixels) < channels:
        return False
    n = int(size[0]) * int(size[1])
    if n <= 0:
        return False
    stride = max(1, n // sample_count)
    for i in range(0, n, stride):
        a = pixels[i * channels + 3]
        if a < ceiling:
            return False
    return True


def _hair_alpha_routing(
    sidecar_mat: SidecarMaterial | None,
    image_alpha_uniform: bool,
) -> str:
    """Decide which alpha-wiring path a hair material needs.

    Two signals feed three routes:

    1. **Sidecar says RGB-mask** — :func:`_hair_alpha_is_in_rgb` is
       True (uniform-opaque alpha + dark RGB *per sidecar*). Used by
       facial hair / beards / stubble where the sidecar metadata is
       reliable. → ``"rgb"`` (plain RGBToBW, no boost — the RGB
       strand pixels at 0.11–0.29 are bright enough for DITHERED).
    2. **Sidecar disagreed but the loaded image's alpha is in fact
       uniformly opaque** — checked by
       :func:`_image_alpha_is_effectively_uniform`. Used by
       eyelashes whose source ``.tex`` reports a per-pixel alpha but
       whose loaded image arrives with alpha == 1.0 everywhere (the
       PNG round-trip flattens the alpha channel). Wiring the alpha
       channel here would yield constant 1.0 → DITHERED has nothing
       to discard. Fall back to RGB luminance + a POW contrast
       boost (their RGB content is dim and needs lifting). →
       ``"rgb_boost"``.
    3. **Otherwise** — the loaded image has a real per-pixel alpha
       mask. Used by head hair. → ``"alpha"``.

    The boost is applied **only** on the runtime-fallback path
    (``"rgb_boost"``) because that path serves materials with very
    dim RGB; the plain ``"rgb"`` materials are bright enough.

    The original prompt phrased the fallback trigger as
    ``image.channels < 4`` (PNG dropped its alpha entirely). The
    pixel-uniformity check is a strict generalisation: it fires for
    both the dropped-channel case and the kept-but-uniform-alpha
    case observed empirically on the eyelash texture.
    """
    if _hair_alpha_is_in_rgb(sidecar_mat):
        return _HAIR_ALPHA_ROUTE_RGB
    if image_alpha_uniform:
        return _HAIR_ALPHA_ROUTE_RGB_BOOST
    return _HAIR_ALPHA_ROUTE_ALPHA


def _disconnect_node_outputs(
    nt: bpy.types.NodeTree, node_name: str,
) -> None:
    """Remove every outgoing link from a named node, if it exists.

    Used to keep the graph deterministic across reclassifications —
    a material that previously hit the boost path but now does not
    must not retain a stale wire from ``d4_hair_alpha_boost`` into
    ``BSDF.Alpha``. No-op when the node is absent.
    """
    node = nt.nodes.get(node_name)
    if node is None:
        return
    for output in node.outputs:
        for link in list(output.links):
            nt.links.remove(link)


def _wire_hair_bsdf_alpha(
    nt: bpy.types.NodeTree,
    bsdf: bpy.types.Node,
    bc_node: bpy.types.Node,
    sidecar_mat: SidecarMaterial | None,
) -> None:
    """Wire BSDF.Alpha for a hair material, picking the right source.

    Routing is decided by :func:`_hair_alpha_routing`, which fuses
    sidecar metadata with the loaded image's actual channel count:

    * ``"alpha"`` — link ``bc_node.outputs["Alpha"]`` directly into
      ``bsdf.inputs["Alpha"]``. Used for head hair, and for
      eyelashes whose loaded image still has 4 channels.
    * ``"rgb"`` — insert :data:`HAIR_ALPHA_FROM_RGB_NODE`
      (``ShaderNodeRGBToBW``) and wire
      ``bc_node.outputs["Color"] → RGBToBW → bsdf.inputs["Alpha"]``.
      Used for facial hair / beards / stubble (the sidecar's
      uniform-opaque alpha tells us the mask is in RGB).
    * ``"rgb_boost"`` — same as ``"rgb"`` plus a POW contrast boost
      via :data:`HAIR_ALPHA_BOOST_NODE`. Used only for eyelashes
      whose loaded image's alpha is effectively uniform (either
      dropped during PNG embed, or kept but flat 1.0 — the
      observed eyelash case); their dim RGB needs the boost or
      DITHERED renders nearly nothing.

    Idempotent: clears existing Alpha links and any outgoing links
    from a stale ``d4_hair_alpha_boost`` before rewiring, so repeat
    calls and reclassification flips both converge to the right
    final state. Helper nodes intentionally stay in the graph even
    when a flip leaves them unused — they have no outbound links
    and removing them could surprise a user mid-edit.
    """
    base_in_alpha = bsdf.inputs["Alpha"]
    # Drop the existing Alpha link(s) so the rebuild is deterministic.
    for link in list(base_in_alpha.links):
        nt.links.remove(link)

    img = getattr(bc_node, "image", None)
    alpha_uniform = _image_alpha_is_effectively_uniform(img)
    route = _hair_alpha_routing(sidecar_mat, alpha_uniform)

    if route == _HAIR_ALPHA_ROUTE_ALPHA:
        # Alpha-channel path. Disconnect any stale boost wires so a
        # prior eyelash classification doesn't leak through.
        _disconnect_node_outputs(nt, HAIR_ALPHA_BOOST_NODE)
        nt.links.new(bc_node.outputs["Alpha"], base_in_alpha)
        return

    # Both rgb paths need the RGBToBW node. Get-or-create idempotently.
    bw = nt.nodes.get(HAIR_ALPHA_FROM_RGB_NODE)
    if bw is None:
        bw = nt.nodes.new("ShaderNodeRGBToBW")
        bw.name = bw.label = HAIR_ALPHA_FROM_RGB_NODE
        # Layout: park just above the tint node if present;
        # otherwise near the BSDF. Cosmetic — does not affect output.
        anchor = nt.nodes.get(HAIR_TINT_NODE) or bsdf
        bw.location = (anchor.location.x - 250, anchor.location.y + 180)
    # Rewire the BW node's input deterministically.
    for link in list(bw.inputs["Color"].links):
        nt.links.remove(link)
    nt.links.new(bc_node.outputs["Color"], bw.inputs["Color"])

    if route == _HAIR_ALPHA_ROUTE_RGB_BOOST:
        boost = nt.nodes.get(HAIR_ALPHA_BOOST_NODE)
        if boost is None:
            boost = nt.nodes.new("ShaderNodeMath")
            boost.name = boost.label = HAIR_ALPHA_BOOST_NODE
            boost.operation = "POWER"
            boost.location = (bw.location.x + 220, bw.location.y)
        boost.inputs[1].default_value = _HAIR_ALPHA_BOOST_EXPONENT
        for link in list(boost.inputs[0].links):
            nt.links.remove(link)
        nt.links.new(bw.outputs["Val"], boost.inputs[0])
        nt.links.new(boost.outputs["Value"], base_in_alpha)
    else:
        # Plain rgb path (beards) — RGBToBW drives Alpha directly,
        # no boost. Clear any stale boost wiring left from a prior
        # eyelash classification.
        _disconnect_node_outputs(nt, HAIR_ALPHA_BOOST_NODE)
        nt.links.new(bw.outputs["Val"], base_in_alpha)


def setup_hair_color_chain(
    mat: bpy.types.Material,
    sidecar_mat: SidecarMaterial | None = None,
) -> bpy.types.Node | None:
    """Insert the parametric hair-colour tint chain on ``mat``, idempotently.

    Wires ``[base colour] -> d4_hair_tint -> BSDF Base Color``:

    * ``d4_hair_tint`` — a ``ShaderNodeMixRGB`` (blend ``MULTIPLY``,
      ``Fac`` 1.0). The apply operator writes the chosen hair colour
      into ``Color2`` and the entry's ``flHairColorInfluence`` into
      ``Fac``.

    The MixRGB's ``Color1`` source depends on the BASE_COLOR texture's
    flavour, classified via :func:`_hair_base_color_is_dark`:

    * **Head hair** — ``avg_rgba`` near-white. The existing BASE_COLOR
      chain is linked into ``Color1`` so the Multiply tint inherits
      the per-strand shading variation baked into the texture.
    * **Facial hair / eyelashes** — ``avg_rgba`` near-black. ``Color1``
      stays at its neutral-white default; Multiply(white, tint) = tint
      so the chosen colour shows through. The strand silhouette is
      still preserved because BSDF.Alpha is wired separately by
      :func:`rebuild_pbr`.
    * **No BASE_COLOR image at all** — ``Color1`` stays at white for
      the same neutral-input reason.

    ``sidecar_mat`` is optional; when omitted the function falls
    through to the link-the-source path so legacy callers (without
    sidecar info) keep their pre-fix behaviour.

    Returns the ``d4_hair_tint`` node, or ``None`` when the material has
    no Principled BSDF. Idempotent: a second call finds the existing
    node and returns it without duplicating anything.
    """
    if not mat.use_nodes or mat.node_tree is None:
        mat.use_nodes = True
    nt = mat.node_tree
    bsdf = next(
        (n for n in nt.nodes if n.type == "BSDF_PRINCIPLED"), None,
    )
    if bsdf is None:
        return None

    existing = nt.nodes.get(HAIR_TINT_NODE)
    if existing is not None:
        return existing  # already inserted — reuse

    base_in = bsdf.inputs["Base Color"]
    # Capture the current BASE_COLOR source before any relinking — once
    # the tint output is wired into base_in the old link is gone.
    src_socket = base_in.links[0].from_socket if base_in.is_linked else None

    tint = nt.nodes.new("ShaderNodeMixRGB")
    tint.name = tint.label = HAIR_TINT_NODE
    tint.blend_type = "MULTIPLY"
    tint.location = (bsdf.location.x - 300, bsdf.location.y + 280)
    tint.inputs["Fac"].default_value = 1.0
    # Color2 is the tint; left white until the apply operator sets it.
    tint.inputs["Color2"].default_value = (1.0, 1.0, 1.0, 1.0)
    # Color1 default: neutral white. The existing BASE_COLOR source is
    # *only* linked in when the texture's RGB content is light enough
    # that Multiply makes sense. Dark facial-hair / eyelash textures
    # would otherwise multiply the tint into near-black.
    tint.inputs["Color1"].default_value = (1.0, 1.0, 1.0, 1.0)
    use_base_color_for_multiply = (
        src_socket is not None
        and not _hair_base_color_is_dark(sidecar_mat)
    )
    if use_base_color_for_multiply:
        nt.links.new(src_socket, tint.inputs["Color1"])

    nt.links.new(tint.outputs["Color"], base_in)
    return tint


def strip_color_attributes(obj: bpy.types.Object) -> int:
    """Delete D4's blend/AO vertex-colour attributes from ``obj``'s mesh.

    They are masks, not PBR tints — leaving them as ``COLOR_0`` makes
    Blender multiply the base colour by them. Returns the count removed.
    """
    mesh = obj.data
    color_attrs = getattr(mesh, "color_attributes", None)
    if color_attrs is None:
        return 0
    removed = 0
    for attr in list(color_attrs):
        if attr.name.lower() in ("color", "color_0", "col"):
            color_attrs.remove(attr)
            removed += 1
    return removed


def swap_armor_skin_materials(
    objects: list[bpy.types.Object],
    sidecar: Sidecar,
) -> tuple[int, str | None]:
    """Replace ``armor_skin_mat`` datablocks with the body skin material.

    Every armor piece ships ``armor_skin_mat`` SubObjectAppearances —
    placeholder stubs (shader ``hero_opaque``, BASE_COLOR pointing at
    ``black.tex``) for the exposed-skin cutouts where the body shows
    through. D4's runtime replaces them with the player's body skin
    material; the static export cannot, so they import pitch black.

    This walks every mesh object's material slots and reassigns any
    slot referencing an ``armor_skin_mat`` datablock to the body skin
    datablock. Afterwards every exposed-skin slot shares the one body
    skin material — applying a skin tone tints it once and all the
    patches inherit the change. Orphaned ``armor_skin_mat.*`` datablocks
    are removed so the Outliner stays clean.

    Body skin material: the first sidecar material whose ``shader_map``
    starts with ``hero_opaque_skin`` (case-insensitive) **and** carries
    a ``SKIN_MASK`` texture (slot 145) — the head skin has the skin
    shader but no SKIN_MASK, so SKIN_MASK is the head/body discriminator.

    Returns ``(swap_count, reason)``:

    * ``(N, None)`` — N slots were reassigned (N may be 0 when the
      import simply contains no ``armor_skin_mat`` slots).
    * ``(0, "<reason>")`` — no body skin material is available (monster
      / weapon-only / partial imports). The caller surfaces this as an
      INFO report; the placeholder-grey fallback in
      :func:`setup_skin_tone_chain` keeps handling those materials.

    Idempotent: a second call finds no ``armor_skin_mat`` datablocks
    left and returns ``(0, None)``.
    """
    # 1. Locate the body skin material in the sidecar.
    body_candidates = sidecar.body_skin_materials()
    if not body_candidates:
        return 0, (
            "no body skin material in sidecar — leaving armor_skin_mat "
            "materials in place"
        )
    body_sm = body_candidates[0]
    if len(body_candidates) > 1:
        names = ", ".join(m.name for m in body_candidates)
        print(
            f"[d4extract] swap_armor_skin_materials: {len(body_candidates)} "
            f"body-skin candidates ({names}); using {body_sm.name!r}"
        )

    # 2. Resolve it to a Blender datablock (Blender may have appended a
    #    .NNN dedup suffix on a re-import — strip it for the match).
    body_datablock = next(
        (mat for mat in bpy.data.materials
         if _base_name(mat.name) == body_sm.name),
        None,
    )
    if body_datablock is None:
        return 0, (
            f"body skin material datablock {body_sm.name!r} not found in "
            f"bpy.data.materials"
        )

    # 3. Collect the armor_skin_mat placeholder datablocks.
    armor_skin = [
        mat for mat in bpy.data.materials
        if _base_name(mat.name) == "armor_skin_mat"
    ]
    if not armor_skin:
        return 0, None

    # 4. Reassign every armor_skin_mat material slot to the body skin.
    swap_count = 0
    for obj in objects:
        if obj.type != "MESH":
            continue
        for slot in obj.material_slots:
            mat = slot.material
            if mat is not None and _base_name(mat.name) == "armor_skin_mat":
                slot.material = body_datablock
                swap_count += 1

    # 5. Drop the now-orphaned placeholder datablocks (only those with
    #    no remaining users — never force-remove something still in use).
    for mat in armor_skin:
        if mat.users == 0:
            bpy.data.materials.remove(mat, do_unlink=True)

    return swap_count, None


# ─── Internal helpers ────────────────────────────────────────────────

def _make_tex_node(
    nt: bpy.types.NodeTree,
    node_name: str,
    role: str,
    image: bpy.types.Image,
    colorspace: str,
    location: tuple[float, float],
) -> bpy.types.ShaderNodeTexImage:
    """Create a tagged ``ShaderNodeTexImage`` bound to ``image``."""
    node = nt.nodes.new("ShaderNodeTexImage")
    node.name = node_name
    node.label = node_name
    node.location = location
    node.image = image
    node[NODE_ROLE_PROP] = role
    try:
        image.colorspace_settings.name = colorspace
    except (TypeError, RuntimeError):
        pass  # colorspace name not available in this build — harmless
    return node


def _multiply_color(
    nt: bpy.types.NodeTree,
    a_socket: bpy.types.NodeSocket,
    b_socket: bpy.types.NodeSocket,
    location: tuple[float, float],
):
    """Insert a ``ShaderNodeMix`` (RGBA, Multiply, factor 1.0)."""
    mix = nt.nodes.new("ShaderNodeMix")
    mix.location = location
    mix.data_type = "RGBA"
    mix.blend_type = "MULTIPLY"
    # ShaderNodeMix socket layout for data_type='RGBA':
    #   inputs[0]=Factor, inputs[6]=A (Color), inputs[7]=B (Color)
    #   outputs[2]=Result (Color)
    mix.inputs[0].default_value = 1.0
    nt.links.new(a_socket, mix.inputs[6])
    nt.links.new(b_socket, mix.inputs[7])
    return mix.outputs[2]


def _base_name(name: str) -> str:
    """Strip Blender's ``.001`` dedup suffix from a datablock name."""
    return re.sub(r"\.\d{3}$", "", name)


def _clamp01(value: float, default: float) -> float:
    if value is None:
        return default
    return max(0.0, min(1.0, value))


def _role_image(sm: SidecarMaterial, role: str) -> bpy.types.Image | None:
    tex = sm.texture_for_role(role)
    if tex is None:
        return None
    return _image_for_sno(tex.sno_id, prefer_prefix=role)


def _base_color_image(sm: SidecarMaterial) -> bpy.types.Image | None:
    """Base-colour image — handles both ``BASE_COLOR_*`` and ``HAIR_BC_*``."""
    tex = sm.texture_for_role("BASE_COLOR")
    if tex is None:
        return None
    return (
        _image_for_sno(tex.sno_id, prefer_prefix="BASE_COLOR")
        or _image_for_sno(tex.sno_id, prefer_prefix="HAIR_BC")
    )


def _mr_image(sm: SidecarMaterial) -> bpy.types.Image | None:
    """d4extract's combined metallic-roughness image (``MR_<r>_<m>``)."""
    for role in ("ROUGHNESS", "METALLIC"):
        tex = sm.texture_for_role(role)
        if tex is None:
            continue
        token = str(tex.sno_id)
        for img in bpy.data.images:
            if img.name.startswith("MR_") and token in img.name:
                return img
    return None


def _image_for_sno(
    sno_id: int, *, prefer_prefix: str,
) -> bpy.types.Image | None:
    """Find the imported image datablock for a texture SNO id.

    d4extract names embedded glTF images ``<ROLE>_<sno>``; Blender's
    importer keeps that name. Matching falls back progressively so a
    ``.001`` dedup suffix or a role-prefix mismatch still resolves.
    """
    exact = bpy.data.images.get(f"{prefer_prefix}_{sno_id}")
    if exact is not None:
        return exact
    token = str(sno_id)
    # Prefer an image whose name starts with the expected role.
    for img in bpy.data.images:
        if img.name.startswith(prefer_prefix) and token in img.name:
            return img
    # Last resort: any embedded image carrying this SNO id.
    for img in bpy.data.images:
        if img.name in _SKIP_IMAGE_NAMES:
            continue
        if token in img.name:
            return img
    return None
