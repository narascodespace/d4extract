"""Click-based CLI entry point for d4extract."""

from __future__ import annotations

import logging
import random
from pathlib import Path

import click
from rich.console import Console
from rich.table import Table

from d4extract.casc.rustydemon import CASCExtractionError, RustyDemonCLI

console = Console()
err_console = Console(stderr=True)

log = logging.getLogger(__name__)


def _setup_logging(verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.WARNING
    logging.basicConfig(
        level=level,
        format="%(levelname)s: %(message)s",
    )


def _tact_keys_option(func):
    """Shared ``--tact-keys`` click option for every command that
    instantiates :class:`RustyDemonCLI`.

    Defining the option once keeps help text consistent across the six
    subcommands and means a future change (renaming, default override,
    etc.) only happens in one place. When the user doesn't pass the
    flag, the CLI falls through to the same QSettings lookup the GUI
    writes to via ``File → Load TACT Keys…``.
    """
    return click.option(
        "--tact-keys", "tact_keys",
        type=click.Path(exists=True, dir_okay=False, path_type=Path),
        default=None,
        help="TACT key file (wowdev space/semicolon format). Overrides "
             "the GUI-saved path and D4EXTRACT_TACT_KEYS env var. "
             "Optional — only needed for encrypted (unreleased) content.",
    )(func)


@click.group()
@click.option("-v", "--verbose", is_flag=True, help="Enable debug logging.")
def cli(verbose: bool) -> None:
    """d4extract — Diablo IV model asset extractor."""
    _setup_logging(verbose)


@cli.command()
@click.argument("game_dir", type=click.Path(exists=True, path_type=Path))
@_tact_keys_option
def info(game_dir: Path, tact_keys: Path | None) -> None:
    """Validate the game directory and check tool availability."""
    try:
        rd = RustyDemonCLI(game_dir, tact_keys_path=tact_keys)
    except CASCExtractionError as exc:
        err_console.print(f"[red]Error:[/red] {exc}")
        raise SystemExit(1)

    console.print(f"[green]Game directory:[/green] {rd.game_dir}")
    console.print(f"[green]rustydemon-cli:[/green] {rd.binary}")

    try:
        ver = rd.version()
        console.print(f"[green]Version:[/green] {ver}")
    except CASCExtractionError:
        console.print("[yellow]Warning:[/yellow] Could not determine rustydemon-cli version.")

    # Detect install type
    if (rd.game_dir / ".build.info").exists():
        console.print("[green]Install type:[/green] Battle.net")
    elif (rd.game_dir / "Data").is_dir():
        console.print("[green]Install type:[/green] Steam (or standalone)")
    else:
        console.print("[yellow]Install type:[/yellow] Unknown")


@cli.command("list")
@click.argument("game_dir", type=click.Path(exists=True, path_type=Path))
@click.option(
    "--filter", "path_filter",
    default="base/meta/Appearance/*",
    show_default=True,
    help="Glob pattern to filter file listing.",
)
@click.option("--limit", "-n", type=int, default=None, help="Max entries to display.")
@_tact_keys_option
def list_files(
    game_dir: Path, path_filter: str, limit: int | None,
    tact_keys: Path | None,
) -> None:
    """List files in the CASC archive."""
    try:
        rd = RustyDemonCLI(game_dir, tact_keys_path=tact_keys)
        entries = rd.list_files(path_filter)
    except CASCExtractionError as exc:
        err_console.print(f"[red]Error:[/red] {exc}")
        raise SystemExit(1)

    if not entries:
        console.print("[yellow]No files matched the filter.[/yellow]")
        return

    if limit:
        entries = entries[:limit]

    table = Table(title=f"CASC files ({len(entries)} shown)")
    table.add_column("Path", style="cyan", no_wrap=False)
    table.add_column("Size", justify="right", style="green")

    for entry in entries:
        size_str = _format_size(entry.size) if entry.size > 0 else "—"
        table.add_row(entry.path, size_str)

    console.print(table)


@cli.command()
@click.argument("game_dir", type=click.Path(exists=True, path_type=Path))
@click.argument("pattern")
@click.option(
    "--output", "-o",
    type=click.Path(path_type=Path),
    default=Path("./extracted"),
    show_default=True,
    help="Output directory for extracted files.",
)
@click.option("--workers", "-w", type=int, default=4, show_default=True)
@_tact_keys_option
def extract(
    game_dir: Path, pattern: str, output: Path, workers: int,
    tact_keys: Path | None,
) -> None:
    """Extract files matching PATTERN from the CASC archive."""
    try:
        rd = RustyDemonCLI(game_dir, tact_keys_path=tact_keys)
    except CASCExtractionError as exc:
        err_console.print(f"[red]Error:[/red] {exc}")
        raise SystemExit(1)

    console.print(f"Extracting [cyan]{pattern}[/cyan] to [cyan]{output}[/cyan] …")

    try:
        extracted = rd.extract([pattern], output, workers=workers)
    except CASCExtractionError as exc:
        err_console.print(f"[red]Extraction failed:[/red] {exc}")
        raise SystemExit(1)

    console.print(f"[green]Extracted {len(extracted)} file(s).[/green]")
    for p in extracted:
        console.print(f"  {p}")


@cli.command("extract-anim")
@click.argument("game_dir", type=click.Path(exists=True, path_type=Path))
@click.argument("anim_name")
@click.option(
    "--output", "-o",
    type=click.Path(path_type=Path),
    default=Path("./extracted"),
    show_default=True,
    help="Output directory for extracted files.",
)
@click.option(
    "--d4data-path", "d4data_path",
    type=click.Path(exists=True, path_type=Path), default=None,
    help="Path to d4data 'json/' directory; resolves shared/aliased "
         "animation payloads (~7,500 of ~45,000 .ani files reuse another "
         "animation's payload).",
)
@_tact_keys_option
def extract_anim_cmd(
    game_dir: Path, anim_name: str, output: Path,
    d4data_path: Path | None,
    tact_keys: Path | None,
) -> None:
    """Extract a meta + payload .ani pair for ANIM_NAME.

    Pulls ``base/meta/Anim/*ANIM_NAME*`` and the matching payload from
    the CASC archive. If the same-name payload is missing and a d4data
    path is supplied, the shared-payload mapping is consulted to find
    the real payload file.

    Example:

      d4extract extract-anim "C:\\Program Files (x86)\\Diablo IV" \\
        barM_HTH_nav_idle --d4data-path ../d4data/json
    """
    try:
        rd = RustyDemonCLI(game_dir, tact_keys_path=tact_keys)
    except CASCExtractionError as exc:
        err_console.print(f"[red]Error:[/red] {exc}")
        raise SystemExit(1)

    console.print(
        f"Extracting animation [cyan]{anim_name}[/cyan] to "
        f"[cyan]{output}[/cyan] …"
    )

    try:
        meta, payload, is_shared = rd.extract_anim_pair(
            anim_name, output, d4data_path=d4data_path,
        )
    except CASCExtractionError as exc:
        err_console.print(f"[red]Extraction failed:[/red] {exc}")
        raise SystemExit(1)

    console.print(f"  meta:    {meta}")
    tag = "  (shared payload)" if is_shared else ""
    console.print(f"  payload: {payload}{tag}")


@cli.command("parse-anim")
@click.argument(
    "meta_json", type=click.Path(exists=True, dir_okay=False, path_type=Path),
)
@click.argument(
    "payload_file",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
)
@click.option(
    "--permutation", "permutation_index", type=int, default=0,
    show_default=True,
    help="Index into ptPermutations[] (0 = first/canonical variant).",
)
@click.option(
    "--hex-limit", type=int, default=64, show_default=True,
    help="Bytes of each curve's raw key blob to render (0 = unlimited).",
)
@click.option(
    "--max-curves", type=int, default=None,
    help="Cap how many bones' curves are dumped (default: all). "
         "Useful for 190-bone player animations.",
)
@click.option(
    "--skeleton-app", "skeleton_app",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    default=None,
    help="Optional .app meta path to load for bone-hash cross-reference. "
         "Pair with --skeleton-payload.",
)
@click.option(
    "--skeleton-payload", "skeleton_payload",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    default=None,
    help="Optional .app payload path; required when --skeleton-app is set.",
)
def parse_anim_cmd(
    meta_json: Path, payload_file: Path,
    permutation_index: int, hex_limit: int,
    max_curves: int | None,
    skeleton_app: Path | None,
    skeleton_payload: Path | None,
) -> None:
    """Parse an animation META_JSON + PAYLOAD_FILE pair and print a dump.

    The diagnostic dump shows the 13 AnimPayloadData sub-array headers,
    every bone hash, and the raw keyframe bytes of each translation /
    rotation / scale curve — the inputs Phase 3 will need for
    compression decoding.

    Example:

      d4extract parse-anim \\
        ../d4data/json/base/meta/Anim/barM_HTH_nav_idle.ani.json \\
        ./extracted/base/payload/Anim/barM_HTH_nav_idle.ani \\
        --permutation 0 --max-curves 8
    """
    from d4extract.formats.anim_parser import (
        AnimFormatError, cross_reference_bone_hashes,
        format_diagnostic, parse_anim,
    )

    try:
        perm = parse_anim(
            meta_json, payload_file, permutation_index=permutation_index,
        )
    except AnimFormatError as exc:
        err_console.print(f"[red]Parse error:[/red] {exc}")
        raise SystemExit(1)

    console.print(format_diagnostic(
        perm, hex_limit=hex_limit, max_curves=max_curves,
    ))

    # Optional: cross-reference bone hashes against a skeleton .app pair.
    if skeleton_app is not None or skeleton_payload is not None:
        if skeleton_app is None or skeleton_payload is None:
            err_console.print(
                "[yellow]--skeleton-app and --skeleton-payload must be "
                "supplied together; skipping cross-reference.[/yellow]"
            )
            return

        from d4extract.formats.app_parser import AppFormatError, parse_app

        try:
            mesh = parse_app(skeleton_app, skeleton_payload)
        except AppFormatError as exc:
            err_console.print(
                f"[yellow]Skeleton parse failed:[/yellow] {exc}"
            )
            return

        if mesh.skeleton is None:
            err_console.print(
                "[yellow]Skeleton .app has no embedded skeleton; "
                "cross-reference skipped.[/yellow]"
            )
            return

        skel_hashes = [b.name_hash for b in mesh.skeleton.bones]
        matched, unmatched = cross_reference_bone_hashes(
            perm.header.bone_names, skel_hashes,
        )
        console.print("")
        console.print(
            f"  Bone-hash cross-reference vs {skeleton_app.stem} "
            f"({len(skel_hashes)} skeleton bones):"
        )
        console.print(
            f"    matched:   {len(matched)} / {len(perm.header.bone_names)}"
        )
        if unmatched:
            preview = ", ".join(f"0x{h:08X}" for h in unmatched[:8])
            tail = (
                f" (+{len(unmatched) - 8} more)"
                if len(unmatched) > 8 else ""
            )
            console.print(
                f"    [yellow]unmatched:[/yellow] {len(unmatched)} — "
                f"{preview}{tail}"
            )


@cli.command("decode-anim")
@click.argument(
    "meta_json", type=click.Path(exists=True, dir_okay=False, path_type=Path),
)
@click.argument(
    "payload_file",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
)
@click.option(
    "--permutation", "permutation_index", type=int, default=0,
    show_default=True,
    help="Index into ptPermutations[] (0 = first/canonical variant).",
)
@click.option(
    "--skeleton-app", "skeleton_app",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    default=None,
    help="Optional .app meta path for rest-pose validation.  Pair with "
         "--skeleton-payload.",
)
@click.option(
    "--skeleton-payload", "skeleton_payload",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    default=None,
    help="Optional .app payload path; required when --skeleton-app is set.",
)
@click.option(
    "--max-bones", type=int, default=8, show_default=True,
    help="How many bones to print frame-0 transforms for (0 = all).",
)
@click.option(
    "--frame", type=int, default=0, show_default=True,
    help="Which frame to dump and validate against the rest pose.",
)
def decode_anim_cmd(
    meta_json: Path, payload_file: Path,
    permutation_index: int,
    skeleton_app: Path | None,
    skeleton_payload: Path | None,
    max_bones: int,
    frame: int,
) -> None:
    """Parse + decode a comp=0 animation and print frame transforms.

    Phase 3 supports flCompression=0 only — comp=3 raises a clear error
    (Phase 4 will pick that up).  When a paired .app skeleton is
    supplied, the command also reports rest-pose deltas which is the
    primary signal that the decode is correct.
    """
    from d4extract.formats.anim_parser import (
        AnimFormatError, decode_permutation, parse_anim,
        validate_against_rest_pose,
    )

    try:
        perm = parse_anim(
            meta_json, payload_file, permutation_index=permutation_index,
        )
    except AnimFormatError as exc:
        err_console.print(f"[red]Parse error:[/red] {exc}")
        raise SystemExit(1)

    rest_map: dict[int, tuple] = {}
    skel_label = ""
    if skeleton_app is not None or skeleton_payload is not None:
        if skeleton_app is None or skeleton_payload is None:
            err_console.print(
                "[yellow]--skeleton-app and --skeleton-payload must be "
                "supplied together; skipping rest-pose validation.[/yellow]"
            )
        else:
            from d4extract.formats.app_parser import (
                AppFormatError, parse_app,
            )
            try:
                mesh = parse_app(skeleton_app, skeleton_payload)
            except AppFormatError as exc:
                err_console.print(
                    f"[yellow]Skeleton parse failed:[/yellow] {exc}"
                )
                mesh = None
            if mesh is not None and mesh.skeleton is not None:
                rest_map = {
                    b.name_hash: (b.local_trs.q, b.local_trs.wp, b.local_trs.scale)
                    for b in mesh.skeleton.bones
                }
                skel_label = skeleton_app.stem

    try:
        decoded = decode_permutation(perm, rest_pose=rest_map)
    except AnimFormatError as exc:
        err_console.print(f"[red]Decode error:[/red] {exc}")
        raise SystemExit(1)

    console.print(
        f"Decoded [cyan]{decoded.name or '?'}[/cyan] "
        f"perm {decoded.permutation_index}: "
        f"{len(decoded.bone_animations)} bones × {decoded.frame_count} "
        f"frames @ {decoded.frame_rate:g}fps  (comp={decoded.compression})"
    )

    if frame < 0 or frame >= decoded.frame_count:
        err_console.print(
            f"[yellow]--frame {frame} out of range "
            f"(0..{decoded.frame_count - 1}); clamping to 0[/yellow]"
        )
        frame = 0

    limit = max_bones if max_bones > 0 else len(decoded.bone_animations)
    console.print(f"\nFrame {frame} transforms (first {limit} bones):")
    for i, bone in enumerate(decoded.bone_animations[:limit]):
        t = bone.translations[frame]
        q = bone.rotations[frame]
        s = bone.scales[frame]
        console.print(
            f"  bone[{i:>3}] hash 0x{bone.bone_hash:08X}  "
            f"T=({t[0]: .4f}, {t[1]: .4f}, {t[2]: .4f})  "
            f"Q=({q[0]: .4f}, {q[1]: .4f}, {q[2]: .4f}, {q[3]: .4f})  "
            f"S=({s[0]:.3f}, {s[1]:.3f}, {s[2]:.3f})"
        )
    if max_bones > 0 and len(decoded.bone_animations) > limit:
        console.print(
            f"  ... ({len(decoded.bone_animations) - limit} more bones)"
        )

    if rest_map:
        _, summary = validate_against_rest_pose(
            decoded, rest_map, frame=frame,
        )
        console.print(f"\nRest-pose comparison vs {skel_label}:")
        console.print(summary)


@cli.command("extract-textures-for")
@click.argument("game_dir", type=click.Path(exists=True, path_type=Path))
@click.argument("model_stem")
@click.option(
    "--d4data-path", "d4data_path",
    type=click.Path(exists=True, path_type=Path), required=True,
    help="Path to d4data 'json/' directory; resolves material/texture chain.",
)
@click.option(
    "--output", "-o", type=click.Path(path_type=Path),
    default=Path("./samples"), show_default=True,
    help="Destination root (CASC paths are mirrored under this dir).",
)
@click.option(
    "--workers", "-w", type=int, default=4, show_default=True,
    help="rustydemon parallel workers per extraction call.",
)
@click.option(
    "--group-size", type=int, default=80, show_default=True,
    help="Max texture paths combined into one rustydemon glob call. "
         "Higher = fewer rustydemon invocations but larger command lines.",
)
@click.option(
    "--include-existing", is_flag=True,
    help="Re-extract textures that are already on disk (default: skip).",
)
@_tact_keys_option
def extract_textures_for_cmd(
    game_dir: Path, model_stem: str, d4data_path: Path,
    output: Path, workers: int, group_size: int,
    include_existing: bool,
    tact_keys: Path | None,
) -> None:
    """Bulk-extract every CASC texture referenced by MODEL_STEM's materials.

    Walks the model's d4data material chain to collect every referenced
    .tex path, then bundles them into a small number of brace-expanded
    glob calls to ``rustydemon-cli`` — bypassing the per-call CASC index
    re-load that makes one-file-at-a-time extraction painfully slow.

    Example:

      d4extract extract-textures-for "C:\\Program Files (x86)\\Diablo IV" \\
        warM_H01 --d4data-path ../d4data/json
    """
    import time

    from d4extract.formats.material_parser import (
        MaterialResolutionError, load_materials,
    )

    try:
        rd = RustyDemonCLI(game_dir, tact_keys_path=tact_keys)
    except CASCExtractionError as exc:
        err_console.print(f"[red]Error:[/red] {exc}")
        raise SystemExit(1)

    try:
        materials = load_materials(model_stem, d4data_path)
    except MaterialResolutionError as exc:
        err_console.print(f"[red]Material resolution failed:[/red] {exc}")
        raise SystemExit(1)

    # Collect every referenced texture path, transformed to its payload
    # form. Deduplicate — the same texture is often used by multiple
    # materials.
    def _payload(meta_path: str) -> str:
        return meta_path.replace(
            "base/meta/Texture/", "base/payload/Texture/", 1,
        )

    wanted: set[str] = set()
    for m in materials:
        for t in m.textures:
            wanted.add(_payload(t.path))

    # Also pull the customization-variant textures (skin / makeup /
    # markings) so the Blender addon's dropdowns have images to swap to.
    #
    # extract-textures-for resolves a bare materials list (no MeshData is
    # parsed, no sidecar written) — but the discovery input is still
    # routed through ``combined_materials_for_export`` so every
    # ``discover_variants`` caller shares one source of truth for the
    # materials roster. For a single model the helper just returns this
    # list; the carrier exposes the only attribute the helper reads.
    from types import SimpleNamespace

    from d4extract.export.gltf_export import combined_materials_for_export
    from d4extract.formats.variants import (
        all_variant_textures, discover_variants,
    )

    combined = combined_materials_for_export(
        primary_mesh=SimpleNamespace(materials=materials),
    )
    variant_texs = all_variant_textures(
        discover_variants(model_stem, d4data_path, combined)
    )
    for t in variant_texs:
        wanted.add(_payload(t.path))
    if variant_texs:
        console.print(
            f"  including [cyan]{len(variant_texs)}[/cyan] "
            f"customization-variant texture(s)"
        )

    wanted_sorted = sorted(wanted)
    if not wanted_sorted:
        console.print(
            f"[yellow]No textures referenced by {model_stem}.[/yellow]"
        )
        return

    if include_existing:
        missing = wanted_sorted
    else:
        missing = [p for p in wanted_sorted if not (output / p).exists()]

    console.print(
        f"Texture chain for [cyan]{model_stem}[/cyan]: "
        f"{len(wanted_sorted)} unique referenced, "
        f"{len(wanted_sorted) - len(missing)} on disk, "
        f"{len(missing)} to extract"
    )

    if not missing:
        console.print("[green]Nothing to extract.[/green]")
        return

    start = time.time()
    try:
        rd.extract_many(
            missing, output, group_size=group_size, workers=workers,
        )
    except CASCExtractionError as exc:
        err_console.print(f"[red]Extraction failed:[/red] {exc}")
        raise SystemExit(1)

    elapsed = time.time() - start
    # Verify what landed on disk.
    landed = sum(1 for p in missing if (output / p).exists())
    failed = len(missing) - landed
    rate = len(missing) / elapsed if elapsed > 0 else float("inf")
    console.print(
        f"[green]{landed}/{len(missing)} files extracted "
        f"in {elapsed:.1f}s ({rate:.1f}/s avg)[/green]"
    )
    if failed:
        console.print(
            f"[yellow]{failed} file(s) did not land on disk — they may "
            f"not exist in the archive (e.g. unreleased content).[/yellow]"
        )


@cli.command()
@click.argument("game_dir", type=click.Path(exists=True, path_type=Path))
@click.option("--count", "-n", type=int, default=5, show_default=True,
              help="Number of model pairs to sample.")
@click.option(
    "--output", "-o",
    type=click.Path(path_type=Path),
    default=Path("./samples"),
    show_default=True,
    help="Output directory for sampled files.",
)
@click.option(
    "--d4data-path", "d4data_path",
    type=click.Path(exists=True, path_type=Path), default=None,
    help="Path to d4data 'json/' directory; resolves shared/aliased payloads "
         "for the ~15%% of appearances whose payload differs from the meta name.",
)
@_tact_keys_option
def sample(
    game_dir: Path, count: int, output: Path,
    d4data_path: Path | None,
    tact_keys: Path | None,
) -> None:
    """Extract random model pairs (meta + data .app) for format analysis."""
    try:
        rd = RustyDemonCLI(game_dir, tact_keys_path=tact_keys)
    except CASCExtractionError as exc:
        err_console.print(f"[red]Error:[/red] {exc}")
        raise SystemExit(1)

    console.print("Listing available models …")
    try:
        entries = rd.list_files("base/meta/Appearance/*")
    except CASCExtractionError as exc:
        err_console.print(f"[red]Error:[/red] {exc}")
        raise SystemExit(1)

    if not entries:
        err_console.print("[red]No Appearance files found in the archive.[/red]")
        raise SystemExit(1)

    # Group entries by top-level subdirectory for variety
    by_prefix: dict[str, list[str]] = {}
    for e in entries:
        # e.g. base/meta/Appearance/Characters/... → "Characters"
        parts = e.path.split("/")
        prefix = parts[3] if len(parts) > 3 else "_root"
        by_prefix.setdefault(prefix, []).append(e.path)

    # Sample across prefixes for variety
    selected: list[str] = []
    prefixes = list(by_prefix.keys())
    random.shuffle(prefixes)

    while len(selected) < count and prefixes:
        for prefix in list(prefixes):
            if len(selected) >= count:
                break
            paths = by_prefix[prefix]
            if not paths:
                prefixes.remove(prefix)
                continue
            choice = random.choice(paths)
            paths.remove(choice)
            selected.append(choice)
            if not paths:
                prefixes.remove(prefix)

    console.print(f"Sampling [cyan]{len(selected)}[/cyan] model(s) …")

    for model_path in selected:
        # Derive model name from the meta path
        model_name = Path(model_path).stem
        console.print(f"\n[bold]{model_name}[/bold]")
        try:
            meta, data, is_shared = rd.extract_model_pair(
                model_name, output, d4data_path=d4data_path,
            )
            console.print(f"  meta:    {meta}")
            tag = "  (shared payload)" if is_shared else ""
            console.print(f"  payload: {data}{tag}")
        except CASCExtractionError as exc:
            err_console.print(f"  [yellow]Skipped:[/yellow] {exc}")

    console.print(f"\n[green]Done. Files written to {output}[/green]")


@cli.command("export")
@click.argument("meta_file", type=click.Path(exists=True, path_type=Path))
@click.argument("data_file", type=click.Path(exists=True, path_type=Path))
@click.option(
    "--output", "-o", type=click.Path(path_type=Path), default=None,
    help="Output root directory; the model lands at "
         "<output>/<stem>/<stem>.<fmt> with loose textures alongside in "
         "textures/. Passing a .glb/.gltf path still works for backwards "
         "compat — the file's parent is treated as the root and a "
         "warning is printed if its stem differs from the meta stem. "
         "Default: same parent dir as the meta file.",
)
@click.option(
    "--format", "fmt", type=click.Choice(["glb", "gltf"]), default="glb",
    show_default=True,
)
@click.option(
    "--coords", type=click.Choice(["none", "z_up_to_y_up", "left_to_right", "auto"]),
    default="z_up_to_y_up", show_default=True,
    help="Coordinate transform preset (D4 is LH Z-up; glTF is RH Y-up).",
)
@click.option("--no-normals", is_flag=True, help="Omit normals from export.")
@click.option("--no-tangents", is_flag=True, help="Omit tangents from export.")
@click.option("--no-uvs", is_flag=True, help="Omit UVs from export.")
@click.option("--no-colors", is_flag=True, help="Omit vertex colors from export.")
@click.option("--no-skin", is_flag=True, help="Omit skeleton + skin (export as static).")
@click.option("--prune-bones", is_flag=True,
              help="Trim skeleton to weighted bones + parent chain.")
@click.option(
    "--d4data-path", "d4data_path", type=click.Path(exists=True, path_type=Path),
    default=None,
    help="Path to d4data 'json/' directory; resolves materials and textures.",
)
@click.option(
    "--no-materials-sidecar", "no_materials_sidecar",
    is_flag=True, default=False,
    help="Skip writing <model>.materials.json (default: write it).",
)
@click.option(
    "--with-textures", is_flag=True,
    help="Decode .tex payloads and embed PBR textures in the .glb.",
)
@click.option(
    "--texture-dir", "texture_dir",
    type=click.Path(exists=True, path_type=Path), default=None,
    help="Root directory holding base/payload/Texture/*.tex (CASC extract).",
)
@click.option(
    "--include-cloth", is_flag=True,
    help="Include physics-only cloth submeshes (default: drop them — they "
         "appear as flat-white geometry without D4's cloth simulation).",
)
@click.option(
    "--anim-meta", "anim_meta", multiple=True,
    type=click.Path(exists=True, path_type=Path),
    help="Path to an animation .ani.json (d4data meta). Repeat to embed "
         "multiple animations into the same .glb. Pair each with the "
         "corresponding --anim-payload in the same order.",
)
@click.option(
    "--anim-payload", "anim_payload", multiple=True,
    type=click.Path(exists=True, path_type=Path),
    help="Path to an animation .ani payload extracted from CASC. Pair "
         "with --anim-meta in the same order.",
)
@click.option(
    "--anim-permutation", "anim_permutation", multiple=True, type=int,
    help="Permutation index for each --anim-meta entry (default 0). "
         "Repeat to set per-animation values.",
)
@click.option("--validate", is_flag=True, help="Run Khronos glTF validator if available.")
def export_cmd(
    meta_file: Path, data_file: Path, output: Path | None, fmt: str,
    coords: str, no_normals: bool, no_tangents: bool, no_uvs: bool,
    no_colors: bool, no_skin: bool, prune_bones: bool,
    d4data_path: Path | None, no_materials_sidecar: bool,
    with_textures: bool, texture_dir: Path | None,
    include_cloth: bool,
    anim_meta: tuple[Path, ...],
    anim_payload: tuple[Path, ...],
    anim_permutation: tuple[int, ...],
    validate: bool,
) -> None:
    """Export a .app model pair to glTF (.glb)."""
    from d4extract.export.gltf_export import (
        GltfExporter, GltfExportError, resolve_export_paths,
        validate_glb_external,
    )
    from d4extract.formats.app_parser import AppFormatError, parse_app
    from d4extract.formats.material_parser import (
        MaterialResolutionError, load_materials,
    )

    # Resolve the per-model layout: ``<root>/<stem>/<stem>.<fmt>`` with
    # a sibling ``textures/`` folder for the loose-texture dump. The
    # historical ``--output <path>.glb`` form is still accepted — the
    # file's parent becomes the root, and a warning fires if the user's
    # chosen stem disagrees with the meta-file stem.
    ext = "gltf" if fmt == "gltf" else "glb"
    stem = meta_file.stem
    if output is None:
        output_root = meta_file.parent
    else:
        suffix = output.suffix.lower()
        if suffix in (".glb", ".gltf"):
            if output.stem != stem:
                err_console.print(
                    f"[yellow]Warning:[/yellow] --output stem "
                    f"{output.stem!r} disagrees with meta stem "
                    f"{stem!r}; using {stem!r} for the model folder."
                )
            output_root = output.parent
        else:
            output_root = output
    _model_dir, output, _textures_dir = resolve_export_paths(
        output_root, stem, ext,
    )

    try:
        mesh = parse_app(meta_file, data_file)
    except AppFormatError as exc:
        err_console.print(f"[red]Parse error:[/red] {exc}")
        raise SystemExit(1)

    if d4data_path is not None:
        try:
            mesh.materials = load_materials(meta_file.stem, d4data_path)
        except MaterialResolutionError as exc:
            err_console.print(f"[yellow]Material resolution skipped:[/yellow] {exc}")
            mesh.materials = None

    # Customization-variant discovery feeds the sidecar's ``variants``
    # block (Blender addon dropdowns). Only worth doing when the sidecar
    # is being written — without it the discovered entries have nowhere
    # to land. Discovery is best-effort and never raises.
    variants = None
    if d4data_path is not None and not no_materials_sidecar and mesh.materials:
        from d4extract.export.gltf_export import combined_materials_for_export
        from d4extract.formats.variants import discover_variants

        # Discover against the same concatenated materials list the
        # sidecar will hold, so ``applies_to_materials`` indices align.
        # Single-mesh today; the helper future-proofs the call if
        # assembly export ever lands on the CLI.
        combined = combined_materials_for_export(primary_mesh=mesh)
        variants = discover_variants(meta_file.stem, d4data_path, combined)
        counts = " ".join(
            f"{k}={len(v.entries)}" for k, v in variants.items()
        )
        console.print(f"  variants discovered: {counts}")

    if with_textures and texture_dir is None:
        err_console.print(
            "[red]--with-textures requires --texture-dir <path> "
            "(CASC-extracted .tex files).[/red]"
        )
        raise SystemExit(2)

    # Decode any animations before invoking the exporter — failures
    # here should fail the whole command rather than silently shipping
    # an animation-less .glb under flags the user explicitly set.
    decoded_anims: list = []
    if anim_meta or anim_payload:
        if len(anim_meta) != len(anim_payload):
            err_console.print(
                f"[red]--anim-meta count ({len(anim_meta)}) must match "
                f"--anim-payload count ({len(anim_payload)}).[/red]"
            )
            raise SystemExit(2)
        if anim_permutation and len(anim_permutation) != len(anim_meta):
            err_console.print(
                f"[red]--anim-permutation count ({len(anim_permutation)}) "
                f"must match --anim-meta count ({len(anim_meta)}).[/red]"
            )
            raise SystemExit(2)
        if mesh.skeleton is None or no_skin:
            err_console.print(
                "[red]Animation export requires a skeleton (got "
                f"{'no skeleton' if mesh.skeleton is None else '--no-skin'}). "
                "Animations need bone nodes to target.[/red]"
            )
            raise SystemExit(2)

        from d4extract.formats.anim_parser import (
            AnimFormatError, build_rest_pose_animation, decode_permutation,
            parse_anim,
        )

        rest_pose = {
            b.name_hash: (b.local_trs.q, b.local_trs.wp, b.local_trs.scale)
            for b in mesh.skeleton.bones
        }
        for i, (meta, payload) in enumerate(zip(anim_meta, anim_payload)):
            perm_idx = anim_permutation[i] if anim_permutation else 0
            try:
                perm = parse_anim(meta, payload, permutation_index=perm_idx)
                decoded = decode_permutation(perm, rest_pose=rest_pose)
            except AnimFormatError as exc:
                err_console.print(
                    f"[red]Animation parse failed ({meta.name} p{perm_idx}):"
                    f"[/red] {exc}"
                )
                raise SystemExit(1)
            decoded_anims.append(decoded)
            console.print(
                f"  decoded animation: [cyan]{decoded.name}[/cyan] "
                f"(p{decoded.permutation_index}, {decoded.frame_count} frames "
                f"@ {decoded.frame_rate:g}fps, "
                f"{len(decoded.bone_animations)} bones)"
            )

        # Append a synthetic rest-pose Action so Blender users can click
        # back to bind without clearing the active action by hand. The
        # skeleton + skin guard above guarantees both exist here, and
        # decoded_anims is non-empty (one entry per --anim-meta).
        rest_anim = build_rest_pose_animation(mesh.skeleton)
        rest_anim.force_static_channels = True
        decoded_anims.append(rest_anim)
        console.print(
            f"  decoded animation: [cyan]{rest_anim.name}[/cyan] "
            f"(synthetic rest pose, {len(rest_anim.bone_animations)} bones)"
        )

    try:
        exporter = GltfExporter(
            coordinate_transform=coords,
            export_normals=not no_normals,
            export_tangents=not no_tangents,
            export_uvs=not no_uvs,
            export_colors=not no_colors,
            export_skin=not no_skin,
            prune_bones=prune_bones,
            write_materials_sidecar=not no_materials_sidecar,
            texture_dir=texture_dir,
            embed_textures=with_textures,
            include_cloth=include_cloth,
            # Parallel loose-texture dump alongside the .glb. Empty
            # when nothing is being embedded (no decoded data to dump).
            loose_textures_dir=_textures_dir if with_textures else None,
        )
        out = exporter.export(
            mesh, output,
            animations=decoded_anims or None,
            variants=variants,
        )
    except (GltfExportError, ValueError) as exc:
        err_console.print(f"[red]Export error:[/red] {exc}")
        raise SystemExit(1)

    size = out.stat().st_size
    has_tangents = bool(mesh.tangents)
    has_colors = bool(mesh.colors)
    has_uv1 = bool(mesh.uvs_1)
    has_skin_data = bool(mesh.joints) and bool(mesh.weights)
    skin_emitted = (
        not no_skin and mesh.skeleton is not None and has_skin_data
    )
    attrs_present = []
    attrs_present.append("POS")
    if mesh.normals:  attrs_present.append("NORMAL")
    if has_tangents and not no_tangents: attrs_present.append("TANGENT")
    if mesh.uvs and not no_uvs: attrs_present.append("UV0")
    if has_uv1 and not no_uvs: attrs_present.append("UV1")
    if has_colors and not no_colors: attrs_present.append("COLOR0")
    if skin_emitted:
        attrs_present.extend(("JOINTS_0", "WEIGHTS_0"))

    console.print(f"[green]Exported:[/green] {out}")
    # Counts reflect what the exporter actually wrote — cloth submeshes
    # filtered out by default are excluded here. Source totals for the
    # full mesh are also printed when filtering changed anything.
    console.print(
        f"  vertices: {exporter.last_vertex_count:,}  "
        f"triangles: {exporter.last_triangle_count:,}  "
        f"submeshes: {exporter.last_submesh_count}  "
        f"size: {_format_size(size)}"
    )
    if exporter.last_skipped_cloth:
        names = ", ".join(f"[{i}] {n}" for i, n in exporter.last_skipped_cloth)
        console.print(
            f"  [yellow]skipped {len(exporter.last_skipped_cloth)} "
            f"cloth-only submesh(es)[/yellow]: {names}"
        )
        console.print(
            f"  source totals: {mesh.vertex_count:,} verts, "
            f"{mesh.index_count // 3:,} tris, "
            f"{len(mesh.submeshes) or mesh.submesh_count} submeshes "
            f"(use --include-cloth to keep)"
        )
    console.print(f"  attributes: {' '.join(attrs_present)}")
    if mesh.materials:
        n_with_tex = sum(1 for m in mesh.materials if m.textures)
        console.print(
            f"  materials: {len(mesh.materials)} resolved "
            f"({n_with_tex} with texture inventories)"
        )
        for i, mat in enumerate(mesh.materials):
            roles = ",".join(sorted({t.role for t in mat.textures}))
            tag = ""
            if mat.is_cloth_only:
                tag = " [yellow](cloth-only, dropped)[/yellow]" if not include_cloth else " (cloth-only)"
            console.print(
                f"    [{i}] [cyan]{mat.name}[/cyan]{tag} — "
                f"{len(mat.textures)} tex, "
                f"baseColor=({mat.base_color_factor[0]:.2f},"
                f"{mat.base_color_factor[1]:.2f},"
                f"{mat.base_color_factor[2]:.2f}); {roles or '—'}"
            )
        if with_textures:
            console.print(
                f"  embedded textures: {exporter.last_image_count} images, "
                f"{_format_size(exporter.last_embedded_bytes)} of PNG payload"
            )

    if mesh.skeleton is not None:
        skel = mesh.skeleton
        if skin_emitted:
            if prune_bones:
                from d4extract.export.gltf_export import _compute_bone_pruning
                _, remap = _compute_bone_pruning(skel, mesh.submeshes)
                console.print(
                    f"  skeleton: {len(remap)} of {len(skel.bones)} bones "
                    f"(pruned), template=0x{skel.template_id:08x}"
                )
            else:
                console.print(
                    f"  skeleton: {len(skel.bones)} bones (full), "
                    f"template=0x{skel.template_id:08x}"
                )
        else:
            console.print(
                f"  [yellow]skeleton present[/yellow] "
                f"({len(skel.bones)} bones) but skin export disabled"
            )

    if validate:
        passed, msg = validate_glb_external(out)
        style = "green" if passed else "red"
        console.print(f"  [{style}]Validation: {msg}[/{style}]")


@cli.command("export-batch")
@click.argument("input_dir", type=click.Path(exists=True, path_type=Path))
@click.option(
    "--output", "-o", type=click.Path(path_type=Path), default=Path("./exported"),
    show_default=True, help="Output directory for .glb files.",
)
@click.option(
    "--coords", type=click.Choice(["none", "z_up_to_y_up", "left_to_right", "auto"]),
    default="z_up_to_y_up", show_default=True,
)
@click.option(
    "--csv", "csv_path", type=click.Path(path_type=Path), default=None,
    help="Path for the export-summary CSV (default: <output>/export_summary.csv).",
)
@click.option(
    "--d4data-path", "d4data_path", type=click.Path(exists=True, path_type=Path),
    default=None,
    help="Path to d4data 'json/' directory; resolves materials and textures.",
)
@click.option(
    "--no-materials-sidecar", "no_materials_sidecar",
    is_flag=True, default=False,
    help="Skip writing <model>.materials.json next to each .glb "
         "(default: write it).",
)
@click.option(
    "--with-textures", is_flag=True,
    help="Decode .tex payloads and embed PBR textures in each .glb.",
)
@click.option(
    "--texture-dir", "texture_dir",
    type=click.Path(exists=True, path_type=Path), default=None,
    help="Root directory holding base/payload/Texture/*.tex (CASC extract).",
)
@click.option(
    "--include-cloth", is_flag=True,
    help="Include physics-only cloth submeshes (default: drop them).",
)
@click.option("--validate", is_flag=True, help="Run Khronos glTF validator on each output.")
def export_batch_cmd(
    input_dir: Path, output: Path, coords: str,
    csv_path: Path | None,
    d4data_path: Path | None, no_materials_sidecar: bool,
    with_textures: bool, texture_dir: Path | None,
    include_cloth: bool,
    validate: bool,
) -> None:
    """Batch export all .app model pairs found under INPUT_DIR.

    Expects meta files in INPUT_DIR/base/meta/Appearance/ and corresponding
    payload files in INPUT_DIR/base/payload/Appearance/.
    """
    from rich.progress import Progress

    from d4extract.export.gltf_export import GltfExporter, GltfExportError, validate_glb_external
    from d4extract.formats.app_parser import AppFormatError, parse_app
    from d4extract.formats.material_parser import (
        MaterialResolutionError, load_materials,
    )

    meta_dir = input_dir / "base" / "meta" / "Appearance"
    payload_dir = input_dir / "base" / "payload" / "Appearance"

    if not meta_dir.is_dir():
        err_console.print(f"[red]Meta directory not found:[/red] {meta_dir}")
        raise SystemExit(1)

    meta_files = sorted(meta_dir.glob("*.app"))
    if not meta_files:
        err_console.print("[yellow]No .app files found.[/yellow]")
        return

    if with_textures and texture_dir is None:
        err_console.print(
            "[red]--with-textures requires --texture-dir <path>.[/red]"
        )
        raise SystemExit(2)

    exporter = GltfExporter(
        coordinate_transform=coords,
        write_materials_sidecar=not no_materials_sidecar,
        texture_dir=texture_dir,
        embed_textures=with_textures,
        include_cloth=include_cloth,
    )
    output.mkdir(parents=True, exist_ok=True)

    results: list[dict[str, object]] = []
    ok_count = 0
    fail_count = 0

    with Progress(console=console) as progress:
        task = progress.add_task("Exporting models", total=len(meta_files))

        for meta_path in meta_files:
            name = meta_path.stem
            payload_path = payload_dir / meta_path.name
            progress.update(task, description=f"Exporting {name}")

            if not payload_path.exists():
                progress.console.print(f"  [yellow]SKIP[/yellow] {name}: no payload file")
                fail_count += 1
                progress.advance(task)
                continue

            try:
                mesh = parse_app(meta_path, payload_path)
                if d4data_path is not None:
                    try:
                        mesh.materials = load_materials(name, d4data_path)
                    except MaterialResolutionError as exc:
                        progress.console.print(
                            f"  [yellow]MAT[/yellow] {name}: {exc}"
                        )
                        mesh.materials = None
                out = exporter.export(mesh, output / f"{name}.glb")
                size = out.stat().st_size
                results.append({
                    "name": name,
                    "vertices": exporter.last_vertex_count,
                    "triangles": exporter.last_triangle_count,
                    "submeshes": exporter.last_submesh_count,
                    "skipped_cloth": len(exporter.last_skipped_cloth),
                    "file_size": size,
                    "status": "ok",
                })
                ok_count += 1

                if validate:
                    passed, msg = validate_glb_external(out)
                    if not passed:
                        progress.console.print(f"  [yellow]WARN[/yellow] {name}: {msg}")

            except (AppFormatError, GltfExportError) as exc:
                progress.console.print(f"  [yellow]FAIL[/yellow] {name}: {exc}")
                results.append({"name": name, "status": "fail", "error": str(exc)})
                fail_count += 1
            except Exception as exc:
                progress.console.print(f"  [yellow]ERROR[/yellow] {name}: {type(exc).__name__}: {exc}")
                results.append({"name": name, "status": "error", "error": str(exc)})
                fail_count += 1

            progress.advance(task)

    console.print(f"\n[green]{ok_count} exported[/green], [yellow]{fail_count} failed[/yellow]")

    # Write summary CSV
    if csv_path is None:
        csv_path = output / "export_summary.csv"
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    with open(csv_path, "w") as f:
        f.write("name,status,vertices,triangles,submeshes,skipped_cloth,file_size,error\n")
        for r in results:
            f.write(
                f"{r['name']},{r.get('status','')},{r.get('vertices','')}"
                f",{r.get('triangles','')},{r.get('submeshes','')}"
                f",{r.get('skipped_cloth','')}"
                f",{r.get('file_size','')},{r.get('error','')}\n"
            )
    console.print(f"Summary written to {csv_path}")


@cli.command("extract-skin-tones")
@click.option(
    "--d4data-path", "d4data_path", required=True,
    type=click.Path(exists=True, path_type=Path),
    help="Path to the d4data 'json/' directory.",
)
@click.option(
    "--out", "out_path", type=click.Path(path_type=Path), default=None,
    help="Output JSON path (default: d4extract/data/skin_tones.json).",
)
def extract_skin_tones_cmd(d4data_path: Path, out_path: Path | None) -> None:
    """Extract the parametric PersonaSkinColor palette into a JSON cache.

    Reads ``GlobalNPCCustomizationData`` from the d4data dump
    (``base/meta/Global/npc_customization.glo.json``) and writes the
    skin-tone palette — flHue / flSaturation / flValue / flDarken plus
    the UI swatch colour — to a JSON file that the variant-discovery
    code and the Blender addon consume.
    """
    from d4extract.formats.skin_tones import (
        DEFAULT_CACHE_PATH, SkinToneError, load_skin_tones, save_skin_tones,
    )

    out = out_path or DEFAULT_CACHE_PATH
    try:
        # cache_path=None forces a fresh read straight from the d4data dump.
        tones = load_skin_tones(d4data_path=d4data_path, cache_path=None)
    except (FileNotFoundError, SkinToneError) as exc:
        err_console.print(f"[red]Skin-tone extraction failed:[/red] {exc}")
        raise SystemExit(1)

    save_skin_tones(tones, out)
    console.print(f"[green]Wrote {len(tones)} skin tone(s)[/green] to {out}")
    for t in tones:
        console.print(
            f"  [cyan]{t.id}[/cyan] {t.label}  "
            f"hue={t.hue:.3f} sat={t.saturation:.3f} "
            f"val={t.value:.3f} darken={t.darken:.3f}"
        )


@cli.command("extract-hair-colors")
@click.option(
    "--d4data-path", "d4data_path", required=True,
    type=click.Path(exists=True, path_type=Path),
    help="Path to the d4data 'json/' directory.",
)
@click.option(
    "--out", "out_path", type=click.Path(path_type=Path), default=None,
    help="Output JSON path (default: d4extract/data/hair_colors.json).",
)
def extract_hair_colors_cmd(d4data_path: Path, out_path: Path | None) -> None:
    """Extract the parametric HairColorDefinition palette into a JSON cache.

    Reads every ``base/meta/HairColor/NNN<Name>.hcl.json`` from the
    d4data dump — skipping the dev-garbage ``Axe Bad Data.hcl.json`` —
    and writes the 31-entry hair-colour palette (RGBA tints + influence)
    to a JSON file the variant-discovery code and the Blender addon
    consume.
    """
    from d4extract.formats.hair_colors import (
        DEFAULT_CACHE_PATH, HairColorError, load_hair_colors, save_hair_colors,
    )

    out = out_path or DEFAULT_CACHE_PATH
    try:
        # cache_path=None forces a fresh read straight from the d4data dump.
        colors = load_hair_colors(d4data_path=d4data_path, cache_path=None)
    except (FileNotFoundError, HairColorError) as exc:
        err_console.print(f"[red]Hair-colour extraction failed:[/red] {exc}")
        raise SystemExit(1)

    save_hair_colors(colors, out)
    console.print(f"[green]Wrote {len(colors)} hair colour(s)[/green] to {out}")
    for c in colors:
        r, g, b, _ = c.rgba_colors[0]
        console.print(
            f"  [cyan]{c.id}[/cyan] {c.display_name}  "
            f"primary=({r:.2f}, {g:.2f}, {b:.2f})  "
            f"influence={c.influence:.2f}"
        )


@cli.group()
def setup() -> None:
    """First-run setup helpers (d4data fetcher, etc.)."""


@setup.command("d4data")
@click.option(
    "--target",
    type=click.Path(path_type=Path),
    default=None,
    help="Where to install. Defaults to %LOCALAPPDATA%/d4extract/d4data.",
)
@click.option(
    "--ref",
    default="master",
    show_default=True,
    help="Branch or tag to fetch from the d4data repo.",
)
def setup_d4data(target: Path | None, ref: str) -> None:
    """Download the d4data community metadata repo.

    The fetch is atomic — an existing install is only replaced once
    the new copy has been validated. Run again after a Diablo IV
    patch to pick up the latest community updates.
    """
    from d4extract.setup import (
        D4DataInstallError, D4DataSource,
        download_and_install_d4data,
    )

    def _progress(done: int, total: int | None, stage: str) -> None:
        # \r updates a single line so the terminal stays clean. The
        # ``rich`` console handles cursor positioning correctly even
        # when stderr is being redirected.
        if total:
            pct = (done * 100) // total
            console.print(f"  {stage}: {pct}%", end="\r")
        else:
            console.print(f"  {stage}: {done // 1024} KiB", end="\r")

    src = D4DataSource(ref=ref)
    try:
        installed = download_and_install_d4data(
            target_dir=target, source=src, progress=_progress,
        )
    except D4DataInstallError as exc:
        # Newline so the failure message isn't written on top of the
        # last \r-updated progress line.
        console.print()
        err_console.print(f"[red]d4data install failed:[/red] {exc}")
        raise SystemExit(1)

    console.print()  # newline after \r-updated progress line
    console.print(f"[green]d4data installed at:[/green] {installed}")


def _format_size(size_bytes: int) -> str:
    """Human-readable file size."""
    for unit in ("B", "KB", "MB", "GB"):
        if size_bytes < 1024:
            return f"{size_bytes:.0f} {unit}" if unit == "B" else f"{size_bytes:.1f} {unit}"
        size_bytes /= 1024  # type: ignore[assignment]
    return f"{size_bytes:.1f} TB"
