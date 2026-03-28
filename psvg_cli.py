#!/usr/bin/env python3
"""
psvg  -  Print-SVG Command-Line Toolchain  (v2.4)
==================================================
Standalone CLI tool for working with Print-SVG files.

USAGE
-----
  psvg validate  <file.svg>  [--pdfx pdfx4]
  psvg convert   <file.svg>  --to pdfx-1a --out output.pdf  [--gs /path/gs]
  psvg strip     <file.svg>  [--out clean.svg]
  psvg inspect   <file.svg>
  psvg migrate   <file.svg>  [--out migrated.svg]
  psvg preflight <file.svg>  [--ink-limit 300] [--format json]
  psvg annotate  <file.svg>  [--out annotated.svg]
  psvg spec                  [--out psvg-spec.txt]

COMMANDS
--------
  validate   Validate a SVG against the PSVG spec and optional PDF/X target.
             Reports errors and warnings; exits 1 on errors.

  convert    Export a CMYK-annotated SVG to a PDF/X-compliant PDF.
             Drives Ghostscript. Blocks on transparency for PDF/X-1a.

  strip      Remove all PSVG/CMYK metadata from an SVG (clean handoff).

  inspect    Show a full report: CMYK colours, spots, separations, ink totals,
             transparency, and PSVG spec compliance.

  migrate    Upgrade legacy cmyk:* attributes to psvg:* namespace in place.

  preflight  Run the full press preflight check.

  annotate   Back-calculate CMYK from RGB fill/stroke and annotate the file.

  spec       Print or export the PSVG v1.0 specification.

EXAMPLES
--------
  # Validate for PDF/X-4
  psvg validate artwork.svg --pdfx pdfx4

  # Export press-ready PDF/X-4
  psvg convert artwork.svg --to pdfx4 --out artwork_pdfx4.pdf

  # Full inspection report
  psvg inspect artwork.svg

  # Migrate a v2.3 file to PSVG namespace
  psvg migrate artwork.svg --out artwork_psvg.svg

  # Preflight with custom ink limit, JSON output
  psvg preflight artwork.svg --ink-limit 330 --format json

INSTALL
-------
  Place psvg_cli.py in the same directory as the extension files, then:

  Linux / macOS:
    chmod +x psvg_cli.py
    ln -s /path/to/psvg_cli.py /usr/local/bin/psvg

  Windows:
    Add the directory to your PATH
    Run as:  python3 psvg_cli.py <command> ...
"""

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Optional

# Add extension directory to path
_DIR = Path(__file__).parent
sys.path.insert(0, str(_DIR))

try:
    from lxml import etree as ET
    _LXML = True
except ImportError:
    _LXML = False
    print("WARNING: lxml not installed. Some commands require it.", file=sys.stderr)
    print("         pip install lxml", file=sys.stderr)

from cmyk_core import (
    PLUGIN_VERSION, SPOT_COLORS,
    rgb_to_cmyk, hex_to_rgb, cmyk_to_hex, ink_total,
    run_preflight, PreflightConfig,
    PF_TRANSPARENCY_X1A, PF_TRANSPARENCY_OP_CONFLICT,
)
from cmyk_io import (
    read_cmyk_svg, write_cmyk_svg, apply_cmyk_document,
    CmykDocument, ElementCmykData,
    strip_icc_color, parse_style_string,
    METADATA_CMYK_ID,
)
from cmyk_psvg import (
    PDFXMode, PSVG_VERSION,
    validate_psvg_document, detect_transparency,
    build_separation_map_from_elements,
    migrate_cmyk_to_psvg, write_spec_document,
    PSVG_NS, PSVG_PREFIX, PSVG_DOC_VERSION,
    PSVG_ERR_TRANSPARENCY_X1A,
)


# ===========================================================================
# Utilities
# ===========================================================================

def _load_svg(path: str):
    if not _LXML:
        _die("lxml is required. pip install lxml")
    if not os.path.isfile(path):
        _die(f"File not found: {path}")
    return ET.parse(path)


def _die(msg: str, code: int = 1):
    print(f"error: {msg}", file=sys.stderr)
    sys.exit(code)


def _find_ghostscript() -> Optional[str]:
    for exe in ("gs", "gswin64c", "gswin32c", "gsc"):
        try:
            r = subprocess.run([exe, "--version"], capture_output=True, timeout=5)
            if r.returncode == 0:
                return exe
        except (FileNotFoundError, subprocess.TimeoutExpired):
            pass
    return None


def _find_inkscape() -> Optional[str]:
    for exe in ("inkscape",):
        try:
            r = subprocess.run([exe, "--version"], capture_output=True, timeout=5)
            if r.returncode == 0:
                return exe
        except (FileNotFoundError, subprocess.TimeoutExpired):
            pass
    for p in (
        r"C:\Program Files\Inkscape\bin\inkscape.exe",
        "/Applications/Inkscape.app/Contents/MacOS/inkscape",
    ):
        if os.path.isfile(p):
            return p
    return None


# ===========================================================================
# COMMAND: validate
# ===========================================================================

def cmd_validate(args):
    tree  = _load_svg(args.file)
    root  = tree.getroot()
    pdfx  = args.pdfx or PDFXMode.NONE
    fmt   = args.format or "text"

    def get_doc(a, d=""): return root.get(a) or d

    report = validate_psvg_document(
        get_doc, root.iter(),
        pdfx_mode=pdfx,
        spot_colors_table=SPOT_COLORS,
    )

    if fmt == "json":
        out = {
            "file":    args.file,
            "pdfx":    pdfx,
            "passed":  report.passed,
            "errors":  len(report.errors()),
            "warnings":len(report.warnings()),
            "issues":  [i.__dict__ for i in report.issues],
        }
        print(json.dumps(out, indent=2))
    else:
        print(report.to_text())

    sys.exit(0 if report.passed else 1)


# ===========================================================================
# COMMAND: convert
# ===========================================================================

def cmd_convert(args):
    import tempfile

    tree    = _load_svg(args.file)
    mode    = getattr(args, "to", PDFXMode.X4) or PDFXMode.X4
    out_pdf = args.out
    if not out_pdf:
        stem    = Path(args.file).stem
        out_pdf = f"{stem}_{mode}.pdf"

    gs_exe = args.gs or _find_ghostscript()
    if not gs_exe:
        _die("Ghostscript not found. Install it or pass --gs /path/to/gs")

    inkscape = _find_inkscape()
    if not inkscape:
        _die("Inkscape CLI not found. Install Inkscape 1.x.")

    # Enforce PDF/X-1a: block on live transparency
    if mode == PDFXMode.X1A:
        root    = tree.getroot()
        results = detect_transparency(root.iter())
        errors  = [r for r in results if r.severity == "error"]
        if errors:
            print(f"BLOCKED: PDF/X-1a requires no live transparency.", file=sys.stderr)
            print(f"  {len(errors)} element(s) have transparency:", file=sys.stderr)
            for info in errors[:5]:
                print(f"    [{info.element_id}]: {info.flattening_advice()}", file=sys.stderr)
            if len(errors) > 5:
                print(f"    ... and {len(errors)-5} more", file=sys.stderr)
            print("", file=sys.stderr)
            print("Options:", file=sys.stderr)
            print("  1. Use --to pdfx4 instead (supports live transparency)", file=sys.stderr)
            print("  2. Run: psvg validate --pdfx pdfx1a to see full report", file=sys.stderr)
            print("  3. Flatten transparency manually then retry", file=sys.stderr)
            sys.exit(2)

    from cmyk_core import get_fogra39_icc_path, get_srgb_icc_path
    icc_path = get_fogra39_icc_path() or get_srgb_icc_path()

    with tempfile.NamedTemporaryFile(suffix=".svg", delete=False) as tmp_svg:
        tree.write(tmp_svg.name, xml_declaration=True, encoding="UTF-8")
        tmp_svg_path = tmp_svg.name

    tmp_rgb = tmp_svg_path.replace(".svg", "_rgb.pdf")

    try:
        print(f"Step 1/2  SVG -> RGB PDF via Inkscape...")
        r = subprocess.run(
            [inkscape, "--export-type=pdf",
             f"--export-filename={tmp_rgb}", tmp_svg_path],
            capture_output=True
        )
        if r.returncode != 0:
            _die(f"Inkscape export failed:\n{r.stderr.decode()[-500:]}")

        gs_args = [gs_exe] + PDFXMode.ghostscript_args(mode)
        gs_args += [f"-sOutputFile={out_pdf}"]
        if icc_path:
            gs_args += [
                f"-sOutputICCProfile={icc_path}",
                "-c", "true setoverprint <</OPM 1>> setuserparams",
                "-f",
            ]
        gs_args.append(tmp_rgb)

        print(f"Step 2/2  RGB PDF -> {mode.upper()} PDF via Ghostscript...")
        r = subprocess.run(gs_args, capture_output=True, text=True)
        if r.returncode != 0:
            _die(f"Ghostscript failed:\n{r.stderr[-1000:]}")

        size = os.path.getsize(out_pdf) // 1024
        print(f"\nDone:  {out_pdf}  ({size} KB)")
        print(f"Mode:  {PDFXMode.DESCRIPTIONS.get(mode, mode)}")
        print(f"ICC:   {icc_path or '(not found)'}")
        print("")
        print("Verify: Acrobat Pro > Tools > Print Production > Preflight")
        print(f"        Run 'PDF/X-{mode.replace('pdfx','')} compliance' check")

    finally:
        for p in (tmp_svg_path, tmp_rgb):
            try: os.unlink(p)
            except OSError: pass


# ===========================================================================
# COMMAND: strip
# ===========================================================================

def cmd_strip(args):
    tree = _load_svg(args.file)
    root = tree.getroot()

    cmyk_ns   = "https://inkscape.org/extensions/cmyk"
    psvg_ns   = PSVG_NS
    svg_ns    = "http://www.w3.org/2000/svg"
    cleared   = 0

    for el in root.iter():
        changed = False
        for attr in list(el.attrib):
            if (attr.startswith(f"{{{cmyk_ns}}}") or
                    attr.startswith(f"{{{psvg_ns}}}")):
                del el.attrib[attr]
                changed = True
        style_str = el.get("style","")
        if "icc-color(" in style_str:
            props = parse_style_string(style_str)
            for prop in ("fill","stroke"):
                if prop in props:
                    props[prop] = strip_icc_color(props[prop])
            from cmyk_io import build_style_string
            el.set("style", build_style_string(props))
            changed = True
        if changed:
            cleared += 1

    # Remove metadata blobs
    meta_el = root.find(f"{{{svg_ns}}}metadata")
    if meta_el is not None:
        for old in list(meta_el):
            old_id = old.get("id","")
            if old_id in ("cmyk-plugin-data","cmyk-plugin-version",
                          "psvg-data","psvg-version"):
                meta_el.remove(old)
            if old.tag.startswith(f"{{{cmyk_ns}}}") or old.tag.startswith(f"{{{psvg_ns}}}"):
                meta_el.remove(old)

    # Remove ICC color-profile from defs
    defs = root.find(f"{{{svg_ns}}}defs")
    if defs is not None:
        for el in list(defs):
            tag = el.tag.split("}")[-1] if "}" in el.tag else el.tag
            if tag == "color-profile":
                defs.remove(el)
            if el.tag.startswith(f"{{{cmyk_ns}}}") or el.tag.startswith(f"{{{psvg_ns}}}"):
                defs.remove(el)

    if root.get("color-profile"):
        del root.attrib["color-profile"]

    out_path = args.out or args.file
    tree.write(out_path, xml_declaration=True, encoding="UTF-8",
               pretty_print=True)
    print(f"Stripped CMYK/PSVG data from {cleared} element(s).")
    print(f"Output: {out_path}")


# ===========================================================================
# COMMAND: inspect
# ===========================================================================

def cmd_inspect(args):
    tree    = _load_svg(args.file)
    root    = tree.getroot()
    cmyk_doc= read_cmyk_svg(tree)
    fmt     = args.format or "text"

    # Transparency
    trans = detect_transparency(root.iter())

    # Separations
    sep_map = build_separation_map_from_elements(root.iter(), SPOT_COLORS)

    # Validation
    def get_doc(a, d=""): return root.get(a) or d
    pdfx   = args.pdfx or PDFXMode.NONE
    val_rpt= validate_psvg_document(
        get_doc, root.iter(), pdfx_mode=pdfx,
        spot_colors_table=SPOT_COLORS
    )

    if fmt == "json":
        out = {
            "file":          args.file,
            "psvg_version":  root.get(PSVG_DOC_VERSION, ""),
            "import_method": cmyk_doc.import_method,
            "elements":      len(cmyk_doc.elements),
            "spots":         cmyk_doc.unique_spot_names(),
            "icc_profile":   cmyk_doc.icc_path,
            "transparency":  len(trans),
            "plates":        sep_map.all_plate_names(),
            "validation":    {
                "passed":  val_rpt.passed,
                "errors":  len(val_rpt.errors()),
                "warnings":len(val_rpt.warnings()),
            },
            "colours": [
                {"id": e.element_id,
                 "cmyk": [round(e.c,3),round(e.m,3),round(e.y,3),round(e.k,3)],
                 "ink_pct": round(e.ink_total,1),
                 "spot": e.spot_name}
                for e in cmyk_doc.elements
            ],
        }
        print(json.dumps(out, indent=2))
        return

    # Text output
    print(f"\nPSVG Inspection Report")
    print(f"======================")
    print(f"File:          {args.file}")
    psvg_ver = root.get(PSVG_DOC_VERSION,"")
    print(f"PSVG version:  {psvg_ver or '(not set — legacy or plain SVG)'}")
    print(f"Import method: {cmyk_doc.import_method}")
    print(f"ICC profile:   {cmyk_doc.icc_path or '(not embedded)'}")
    print("")

    print(f"Colours ({len(cmyk_doc.elements)} annotated elements)")
    print(f"{'─'*60}")
    if cmyk_doc.elements:
        max_ink = max(e.ink_total for e in cmyk_doc.elements)
        for e in cmyk_doc.elements[:20]:
            spot_str = f"  [{e.spot_name}]" if e.spot_name else ""
            flag     = "  !! OVER LIMIT" if e.ink_total > 300 else ""
            print(f"  {e.element_id:30s}  C={e.c*100:.0f}% M={e.m*100:.0f}% "
                  f"Y={e.y*100:.0f}% K={e.k*100:.0f}%  "
                  f"ink={e.ink_total:.0f}%{spot_str}{flag}")
        if len(cmyk_doc.elements) > 20:
            print(f"  ... and {len(cmyk_doc.elements)-20} more")
        print(f"  Max ink total: {max_ink:.1f}%")
    else:
        print("  (none found)")

    print("")
    print(f"Separations")
    print(f"{'─'*60}")
    for plate in sep_map.all_plate_names():
        print(f"  {plate}")
    sep_issues = sep_map.validation_report()
    for issue in sep_issues:
        print(f"  !! {issue}")

    print("")
    print(f"Transparency ({len(trans)} element(s))")
    print(f"{'─'*60}")
    if trans:
        for t in trans[:10]:
            print(f"  [{t.element_id}]: {t.flattening_advice()}")
        if len(trans) > 10:
            print(f"  ... and {len(trans)-10} more")
    else:
        print("  (none — compatible with all PDF/X modes)")

    print("")
    print(f"PSVG Validation  (PDF/X target: {pdfx})")
    print(f"{'─'*60}")
    print(val_rpt.to_text())


# ===========================================================================
# COMMAND: migrate
# ===========================================================================

def cmd_migrate(args):
    tree = _load_svg(args.file)
    root = tree.getroot()

    n = migrate_cmyk_to_psvg(root)
    root.set(PSVG_DOC_VERSION, PSVG_VERSION)

    out_path = args.out or args.file
    tree.write(out_path, xml_declaration=True, encoding="UTF-8",
               pretty_print=True)

    print(f"Migrated {n} element(s) from cmyk:* to psvg:* namespace.")
    print(f"psvg:version=\"{PSVG_VERSION}\" added to root <svg>.")
    print(f"Output: {out_path}")


# ===========================================================================
# COMMAND: preflight
# ===========================================================================

def cmd_preflight(args):
    tree  = _load_svg(args.file)
    root  = tree.getroot()
    fmt   = args.format or "text"
    pdfx  = args.pdfx or "none"

    cfg = PreflightConfig(
        ink_limit_pct        = float(args.ink_limit or 300),
        check_transparency   = True,
        pdfx_mode            = pdfx,
        check_trapping       = False,
    )

    def get_doc(a, d=""): return root.get(a) or d
    report = run_preflight(root.iter(), get_doc, cfg)

    if fmt == "json":
        print(report.to_json())
    else:
        print(report.to_text())

    sys.exit(0 if report.passed else 1)


# ===========================================================================
# COMMAND: annotate
# ===========================================================================

def cmd_annotate(args):
    tree     = _load_svg(args.file)
    root     = tree.getroot()
    svg_ns   = "http://www.w3.org/2000/svg"
    cmyk_ns  = "https://inkscape.org/extensions/cmyk"
    count    = 0

    from cmyk_core import ATTR_C, ATTR_M, ATTR_Y, ATTR_K, ATTR_TARGET, ATTR_INK_TOTAL
    from cmyk_io   import parse_style_string, strip_icc_color

    for el in root.iter():
        tag = el.tag if isinstance(el.tag, str) else ""
        if not tag.startswith("{"): continue
        style_str = el.get("style","")
        if not style_str: continue
        props = parse_style_string(style_str)
        for prop in ("fill","stroke"):
            val = strip_icc_color(props.get(prop,"")).strip()
            if not val or val in ("none","inherit","transparent"): continue
            if not val.startswith("#"): continue
            rgb = hex_to_rgb(val)
            c,m,y,k = rgb_to_cmyk(*rgb)
            el.set(ATTR_C, f"{c:.6f}")
            el.set(ATTR_M, f"{m:.6f}")
            el.set(ATTR_Y, f"{y:.6f}")
            el.set(ATTR_K, f"{k:.6f}")
            el.set(ATTR_TARGET, prop)
            el.set(ATTR_INK_TOTAL, f"{ink_total(c,m,y,k):.2f}")
            count += 1

    try:
        ET.register_namespace("cmyk", cmyk_ns)
    except (AttributeError, Exception):
        pass

    out_path = args.out or args.file
    tree.write(out_path, xml_declaration=True, encoding="UTF-8",
               pretty_print=True)

    print(f"Annotated {count} fill/stroke colour(s) with CMYK metadata.")
    print(f"Output: {out_path}")


# ===========================================================================
# COMMAND: spec
# ===========================================================================

def cmd_spec(args):
    out = args.out
    if out:
        write_spec_document(out)
        print(f"PSVG specification written to: {out}")
    else:
        from cmyk_psvg import PSVG_SPEC_TEXT
        print(PSVG_SPEC_TEXT)


# ===========================================================================
# CLI entry point
# ===========================================================================

def main():
    parser = argparse.ArgumentParser(
        prog="psvg",
        description=f"Print-SVG toolchain v{PLUGIN_VERSION} (PSVG spec v{PSVG_VERSION})",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--version", action="version",
                        version=f"psvg {PLUGIN_VERSION} / PSVG {PSVG_VERSION}")

    sub = parser.add_subparsers(dest="command", metavar="COMMAND")
    sub.required = True

    # validate
    p = sub.add_parser("validate", help="Validate SVG against PSVG spec")
    p.add_argument("file");  p.add_argument("--pdfx", default=None)
    p.add_argument("--format", choices=["text","json"], default="text")

    # convert
    p = sub.add_parser("convert", help="Export to PDF/X-compliant PDF")
    p.add_argument("file")
    p.add_argument("--to",  default="pdfx4", dest="to",
                   choices=["pdfx1a","pdfx3","pdfx4"])
    p.add_argument("--out", default=None)
    p.add_argument("--gs",  default=None, metavar="PATH")

    # strip
    p = sub.add_parser("strip", help="Remove all PSVG/CMYK metadata")
    p.add_argument("file");  p.add_argument("--out", default=None)

    # inspect
    p = sub.add_parser("inspect", help="Full inspection report")
    p.add_argument("file")
    p.add_argument("--pdfx", default=None)
    p.add_argument("--format", choices=["text","json"], default="text")

    # migrate
    p = sub.add_parser("migrate", help="Upgrade cmyk:* attrs to psvg:*")
    p.add_argument("file");  p.add_argument("--out", default=None)

    # preflight
    p = sub.add_parser("preflight", help="Run press preflight checks")
    p.add_argument("file")
    p.add_argument("--ink-limit", type=float, default=300.0)
    p.add_argument("--pdfx", default=None)
    p.add_argument("--format", choices=["text","json"], default="text")

    # annotate
    p = sub.add_parser("annotate",
                       help="Back-calculate CMYK from RGB and annotate")
    p.add_argument("file");  p.add_argument("--out", default=None)

    # spec
    p = sub.add_parser("spec", help="Print or export the PSVG specification")
    p.add_argument("--out", default=None)

    args = parser.parse_args()

    dispatch = {
        "validate":  cmd_validate,
        "convert":   cmd_convert,
        "strip":     cmd_strip,
        "inspect":   cmd_inspect,
        "migrate":   cmd_migrate,
        "preflight": cmd_preflight,
        "annotate":  cmd_annotate,
        "spec":      cmd_spec,
    }
    dispatch[args.command](args)


if __name__ == "__main__":
    main()
