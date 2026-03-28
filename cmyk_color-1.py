#!/usr/bin/env python3
"""
cmyk_color.py  –  CMYK Color Suite for Inkscape  (v2.4)
========================================================
New tabs in v2.1
----------------
  Overprint   – Set overprint fill/stroke per object; screen preview via
                mix-blend-mode:multiply; GS overprint preamble generation.
  Preflight   – Full press-readiness check: ink limits, hairlines, rich
                black, spot consistency, bleed, gradients, overprint warnings.
  Compression – Round path precision, normalise styles, produce SVGZ;
                file-size report and per-pass statistics.

Separations – plate preview, 4-up grid, spot plates, tinted mode
Trapping    – misregistration gap detection, auto trap stroke
Patterns    – annotate <pattern> tile colours with CMYK metadata
Heatmap     – ink density overlay, green/amber/red visual map

All v2.1 tabs unchanged.
"""

import inkex
import json
import os
import re
import subprocess
import sys
import tempfile
from typing import Optional

from cmyk_core import (
    # Namespaces / attributes
    CMYK_NS, CMYK_PREFIX,
    ATTR_C, ATTR_M, ATTR_Y, ATTR_K, ATTR_TARGET,
    ATTR_SPOT_NAME, ATTR_ICC_HREF, ATTR_GRAD_STOPS, ATTR_PATTERN,
    ATTR_OVERPRINT_FILL, ATTR_OVERPRINT_STROKE, ATTR_INK_TOTAL,
    ATTR_PREFLIGHT_WARN, ATTR_COMP_ORIG_BYTES,
    METADATA_CMYK_ID, METADATA_VERSION_ID, PLUGIN_VERSION,
    # Colour math
    cmyk_to_rgb, rgb_to_cmyk, hex_to_rgb, rgb_to_hex, cmyk_to_hex,
    clamp_percent, ink_total,
    # Gradient helpers
    parse_gradient_stop_color, build_stop_style,
    # ICC
    get_fogra39_icc_path, get_srgb_icc_path, icc_profile_to_data_uri,
    # Metadata
    encode_cmyk_metadata, decode_cmyk_metadata,
    # Spot
    spot_to_cmyk, list_spot_names, SPOT_COLORS,
    # Overprint (v2.1)
    apply_overprint_style, composite_overprint,
    is_rich_black, overprint_gs_preamble, build_gs_overprint_args,
    OverprintState,
    # Preflight (v2.1)
    run_preflight, PreflightConfig, PreflightReport,
    # Compression (v2.1)
    compress_svg_document, compress_svg_bytes,
    CompressionStats, build_gs_compression_args,
    round_path_data, normalise_style_string,
    # Separations (v2.2)
    channel_to_greyscale, separation_style, spot_coverage_style,
    SeparationPlate, build_separation_layer_name,
    four_up_transforms, separation_plates_for_document,
    SEPARATION_LAYER_PREFIX, SEPARATION_CHANNELS, SEPARATION_LABELS,
    # Trapping (v2.2)
    trap_needed, shares_ink_channel, lighter_cmyk, trap_stroke_style,
    find_trap_pairs, TrapPair, TrapReport,
    DEFAULT_TRAP_WIDTH_PT,
    # Patterns (v2.2)
    build_pattern_cmyk_metadata, parse_pattern_cmyk_metadata, pattern_ink_total,
    # Heatmap (v2.2)
    ink_heatmap_color, ink_heatmap_hex, ink_heatmap_style,
    HEATMAP_LAYER_NAME, HEATMAP_COLOR_SAFE, HEATMAP_COLOR_CAUTION, HEATMAP_COLOR_DANGER,
    # New v2.2 attributes
    ATTR_SEP_CHANNEL, ATTR_TRAP_PAIRS, ATTR_HEATMAP_INK,
    # New v2.2 preflight codes
    PF_PATTERN_OVER_LIMIT, PF_TRAP_NEEDED,
)

# cmyk_io imports (v2.3)
try:
    from cmyk_io import (
        read_cmyk_svg, write_cmyk_svg, apply_cmyk_document,
        CmykDocument, ElementCmykData, CmykAutoSave,
        ICC_PROFILE_NAME, build_icc_paint,
        parse_icc_color, strip_icc_color,
        parse_style_string, build_style_string,
        diff_cmyk_documents, find_desynced_elements,
        ATTR_UUID, ATTR_KNOCKOUT, ATTR_OVERPRINT_MODE,
    )
    from cmyk_export_svg import CmykSaveHook
    _IO_AVAILABLE = True
except ImportError:
    _IO_AVAILABLE = False

# cmyk_psvg imports (v2.4)
try:
    from cmyk_psvg import (
        PSVGDocument, PDFXMode, PSVGValidationReport,
        validate_psvg_document, detect_transparency,
        build_separation_map_from_elements, SeparationMap,
        migrate_cmyk_to_psvg, write_spec_document,
        PSVG_NS, PSVG_PREFIX, PSVG_VERSION,
        PSVG_C, PSVG_M, PSVG_Y, PSVG_K,
        PSVG_DOC_VERSION, PSVG_DOC_PDFX_MODE, PSVG_DOC_BLEED_MM,
        build_gs_flatten_args,
    )
    _PSVG_AVAILABLE = True
except ImportError:
    _PSVG_AVAILABLE = False

SVG_NS      = "http://www.w3.org/2000/svg"
XLINK_NS    = "http://www.w3.org/1999/xlink"
INKSCAPE_NS = "http://www.inkscape.org/namespaces/inkscape"


# ===========================================================================
class CMYKColor(inkex.Effect):
# ===========================================================================

    def add_arguments(self, pars):
        pars.add_argument("--tab", default="cmyk")

        # ── CMYK Color tab ──────────────────────────────────────────────────
        pars.add_argument("--cyan",     type=float, default=0.0)
        pars.add_argument("--magenta",  type=float, default=0.0)
        pars.add_argument("--yellow",   type=float, default=0.0)
        pars.add_argument("--black",    type=float, default=0.0)
        pars.add_argument("--alpha",    type=float, default=100.0)
        pars.add_argument("--target",   default="fill")
        pars.add_argument("--action",   default="apply")

        # ── Gradient tab ────────────────────────────────────────────────────
        pars.add_argument("--grad_action", default="apply_gradient")
        pars.add_argument("--grad_stops",  default="0,0,0,100;0,0,0,0")

        # ── Spot tab ────────────────────────────────────────────────────────
        pars.add_argument("--spot_name",   default="PANTONE 485 C")
        pars.add_argument("--spot_target", default="fill")
        pars.add_argument("--spot_action", default="apply_spot")

        # ── ICC tab ─────────────────────────────────────────────────────────
        pars.add_argument("--icc_action",  default="embed_icc")
        pars.add_argument("--icc_profile", default="fogra39")
        pars.add_argument("--icc_path",    default="")

        # ── SVG Preserve tab ────────────────────────────────────────────────
        pars.add_argument("--svg_action",  default="save_metadata")

        # ── Export tab ──────────────────────────────────────────────────────
        pars.add_argument("--export_action", default="export_pdf_gs")
        pars.add_argument("--export_path",   default="")
        pars.add_argument("--gs_path",       default="")

        # ── Overprint tab (v2.1) ────────────────────────────────────────────
        pars.add_argument("--op_fill",        type=inkex.Boolean, default=False)
        pars.add_argument("--op_stroke",      type=inkex.Boolean, default=False)
        pars.add_argument("--op_preview",     type=inkex.Boolean, default=True)
        pars.add_argument("--op_action",      default="set_overprint")

        # ── Preflight tab (v2.1) ────────────────────────────────────────────
        pars.add_argument("--pf_ink_limit",   type=float, default=300.0)
        pars.add_argument("--pf_min_stroke",  type=float, default=0.25)
        pars.add_argument("--pf_bleed",       type=inkex.Boolean, default=True)
        pars.add_argument("--pf_images",      type=inkex.Boolean, default=True)
        pars.add_argument("--pf_gradients",   type=inkex.Boolean, default=True)
        pars.add_argument("--pf_overprint",   type=inkex.Boolean, default=True)
        pars.add_argument("--pf_action",      default="run_preflight")
        pars.add_argument("--pf_format",      default="text")   # text | json

        # ── Compression tab (v2.1) ──────────────────────────────────────────
        pars.add_argument("--cmp_precision",  type=int,           default=3)
        pars.add_argument("--cmp_dedup",      type=inkex.Boolean, default=True)
        pars.add_argument("--cmp_svgz",       type=inkex.Boolean, default=False)
        pars.add_argument("--cmp_svgz_path",  default="")
        pars.add_argument("--cmp_action",     default="compress_document")

        # ── Separations tab (v2.2) ─────────────────────────────────────────
        pars.add_argument("--sep_channel",  default="c")       # c|m|y|k|spot
        pars.add_argument("--sep_spot",     default="")        # spot name for spot plate
        pars.add_argument("--sep_tinted",   type=inkex.Boolean, default=False)
        pars.add_argument("--sep_fourup",   type=inkex.Boolean, default=False)
        pars.add_argument("--sep_action",   default="preview_channel")

        # ── Trapping tab (v2.2) ────────────────────────────────────────────
        pars.add_argument("--trap_width",   type=float, default=0.25)
        pars.add_argument("--trap_action",  default="find_traps")

        # ── Patterns tab (v2.2) ────────────────────────────────────────────
        pars.add_argument("--pat_action",   default="annotate_patterns")

        # ── IO tab (v2.3) ──────────────────────────────────────────────
        pars.add_argument("--io_action",    default="export_cmyk_svg")
        pars.add_argument("--io_out_path",  default="")
        pars.add_argument("--io_embed_icc", type=inkex.Boolean, default=True)
        pars.add_argument("--io_icc_color", type=inkex.Boolean, default=True)
        pars.add_argument("--io_src_path",  default="")
        pars.add_argument("--io_overwrite", type=inkex.Boolean, default=False)

        # ── Transparency tab (v2.4) ────────────────────────────────────
        pars.add_argument("--tr_action",    default="detect_transparency")
        pars.add_argument("--tr_pdfx_mode", default="none")

        # ── PDF/X tab (v2.4) ───────────────────────────────────────────
        pars.add_argument("--px_mode",      default="pdfx4")
        pars.add_argument("--px_output",    default="")
        pars.add_argument("--px_gs_path",   default="")
        pars.add_argument("--px_action",    default="export_pdfx")

        # ── PSVG Spec tab (v2.4) ───────────────────────────────────────
        pars.add_argument("--ps_action",    default="validate_psvg")
        pars.add_argument("--ps_migrate",   type=inkex.Boolean, default=False)
        pars.add_argument("--ps_spec_out",  default="")

        # ── Heatmap tab (v2.2) ─────────────────────────────────────────────
        pars.add_argument("--hm_opacity",   type=float, default=0.75)
        pars.add_argument("--hm_action",    default="show_heatmap")

    # -----------------------------------------------------------------------
    def effect(self):
        tab = self.options.tab

        dispatch = {
            "cmyk":       self._route_cmyk,
            "doc":        self._annotate_document,
            "gradient":   self._route_gradient,
            "spot":       self._route_spot,
            "icc":        self._route_icc,
            "svg":        self._route_svg,
            "export":     self._route_export,
            "overprint":  self._route_overprint,   # v2.1
            "preflight":  self._route_preflight,   # v2.1
            "compression":  self._route_compression, # v2.1
            "separations":  self._route_separations,  # v2.2
            "trapping":     self._route_trapping,     # v2.2
            "patterns":     self._route_patterns,     # v2.2
            "heatmap":      self._route_heatmap,      # v2.2
            "io":           self._route_io,           # v2.3
            "transparency": self._route_transparency,  # v2.4
            "pdfx":         self._route_pdfx,          # v2.4
            "psvg":         self._route_psvg,          # v2.4
        }
        handler = dispatch.get(tab)
        if handler:
            handler()
        else:
            inkex.errormsg(f"Unknown tab: {tab}")

    # =======================================================================
    # CMYK Color tab
    # =======================================================================

    def _route_cmyk(self):
        a = self.options.action
        if a == "apply":         self._apply_solid()
        elif a == "read":        self._read_color()
        elif a == "convert_doc": self._annotate_document()

    def _apply_solid(self):
        c = clamp_percent(self.options.cyan)    / 100.0
        m = clamp_percent(self.options.magenta) / 100.0
        y = clamp_percent(self.options.yellow)  / 100.0
        k = clamp_percent(self.options.black)   / 100.0
        a = clamp_percent(self.options.alpha)   / 100.0
        target = self.options.target

        if not self.svg.selected:
            inkex.errormsg("No objects selected.")
            return

        r, g, b = cmyk_to_rgb(c, m, y, k)
        hex_color = rgb_to_hex(r, g, b)
        it = ink_total(c, m, y, k)

        for node in self.svg.selected.values():
            self._set_solid_color(node, hex_color, a, c, m, y, k, target)
            node.set(ATTR_INK_TOTAL, f"{it:.2f}")

        self._ensure_namespace()
        inkex.utils.debug(
            f"Applied CMYK({c*100:.1f}% {m*100:.1f}% {y*100:.1f}% {k*100:.1f}%)"
            f"  Ink total: {it:.1f}%  → RGB({r},{g},{b})  [{target}]"
            f"  to {len(self.svg.selected)} object(s)."
        )

    def _set_solid_color(self, node, hex_color, alpha, c, m, y, k, target):
        style = node.style

        def _apply(prop):
            style[prop] = hex_color
            style[f"{prop}-opacity"] = f"{alpha:.6f}"

        if target in ("fill",   "both"): _apply("fill")
        if target in ("stroke", "both"): _apply("stroke")
        node.style = style

        node.set(ATTR_C,      f"{c:.6f}")
        node.set(ATTR_M,      f"{m:.6f}")
        node.set(ATTR_Y,      f"{y:.6f}")
        node.set(ATTR_K,      f"{k:.6f}")
        node.set(ATTR_TARGET, target)
        node.set(ATTR_INK_TOTAL, f"{ink_total(c,m,y,k):.2f}")

    def _read_color(self):
        if not self.svg.selected:
            inkex.errormsg("No objects selected.")
            return
        node = next(iter(self.svg.selected.values()))
        c  = node.get(ATTR_C)
        m  = node.get(ATTR_M)
        y  = node.get(ATTR_Y)
        k  = node.get(ATTR_K)
        tgt  = node.get(ATTR_TARGET, "fill")
        spot = node.get(ATTR_SPOT_NAME, "")
        op_f = node.get(ATTR_OVERPRINT_FILL,   "0") == "1"
        op_s = node.get(ATTR_OVERPRINT_STROKE, "0") == "1"

        if None in (c, m, y, k):
            fill = node.style.get("fill", "#000000")
            if fill and fill not in ("none", "inherit"):
                c, m, y, k = rgb_to_cmyk(*hex_to_rgb(fill))
                tgt = "fill"
            else:
                c = m = y = k = 0.0
        else:
            c, m, y, k = float(c), float(m), float(y), float(k)

        it = ink_total(c, m, y, k)
        lines = [
            f"CMYK [{tgt}]:",
            f"  C={c*100:.1f}%  M={m*100:.1f}%  Y={y*100:.1f}%  K={k*100:.1f}%",
            f"  Ink total: {it:.1f}%",
        ]
        if spot: lines.append(f"  Spot: {spot}")
        if op_f: lines.append("  Overprint fill: ON")
        if op_s: lines.append("  Overprint stroke: ON")
        if is_rich_black(c, m, y, k): lines.append("  ⚠ Rich black")
        inkex.utils.debug("\n".join(lines))

    # =======================================================================
    # Gradient tab
    # =======================================================================

    def _route_gradient(self):
        a = self.options.grad_action
        if a == "apply_gradient": self._apply_gradient()
        elif a == "read_gradient": self._read_gradient()

    def _apply_gradient(self):
        if not self.svg.selected:
            inkex.errormsg("No objects selected.")
            return

        cmyk_stops = []
        for entry in self.options.grad_stops.strip().split(";"):
            entry = entry.strip()
            if not entry: continue
            parts = [float(x) / 100.0 for x in entry.split(",")]
            if len(parts) == 4:
                cmyk_stops.append(tuple(parts))

        if len(cmyk_stops) < 2:
            inkex.errormsg("Need at least 2 stops (format: C,M,Y,K;C,M,Y,K).")
            return

        for node in self.svg.selected.values():
            self._apply_gradient_to_node(node, cmyk_stops)

        self._ensure_namespace()
        inkex.utils.debug(
            f"Applied {len(cmyk_stops)}-stop CMYK gradient "
            f"to {len(self.svg.selected)} object(s)."
        )

    def _apply_gradient_to_node(self, node, cmyk_stops):
        defs    = self._get_or_create_defs()
        grad_id = self._unique_id("cmykGrad")
        n       = len(cmyk_stops)

        grad_el = inkex.etree.SubElement(
            defs, f"{{{SVG_NS}}}linearGradient",
            attrib={"id": grad_id, "x1":"0","y1":"0","x2":"1","y2":"0",
                    "gradientUnits":"objectBoundingBox"}
        )
        stop_data = []
        for i, (c, m, y, k) in enumerate(cmyk_stops):
            offset = i / (n - 1)
            r, gb, b = cmyk_to_rgb(c, m, y, k)
            inkex.etree.SubElement(
                grad_el, f"{{{SVG_NS}}}stop",
                attrib={
                    "offset": f"{offset:.6f}",
                    "style":  f"stop-color:{rgb_to_hex(r,gb,b)};stop-opacity:1",
                    ATTR_C: f"{c:.6f}", ATTR_M: f"{m:.6f}",
                    ATTR_Y: f"{y:.6f}", ATTR_K: f"{k:.6f}",
                }
            )
            stop_data.append({"offset": round(offset,6),
                               "c":round(c,6),"m":round(m,6),
                               "y":round(y,6),"k":round(k,6)})

        style = node.style
        style["fill"] = f"url(#{grad_id})"
        node.style = style
        node.set(ATTR_GRAD_STOPS, json.dumps(stop_data, separators=(",",":")))
        node.set(ATTR_TARGET, "fill")

    def _read_gradient(self):
        if not self.svg.selected:
            inkex.errormsg("No objects selected.")
            return
        node = next(iter(self.svg.selected.values()))
        blob = node.get(ATTR_GRAD_STOPS)
        if blob:
            stops = json.loads(blob)
            lines = [f"  Stop {i+1}: C={s['c']*100:.1f}% M={s['m']*100:.1f}% "
                     f"Y={s['y']*100:.1f}% K={s['k']*100:.1f}%  @{s['offset']*100:.0f}%"
                     for i, s in enumerate(stops)]
            inkex.utils.debug("Gradient CMYK stops:\n" + "\n".join(lines))
        else:
            inkex.utils.debug("No CMYK gradient metadata found.")

    # =======================================================================
    # Spot Colors tab
    # =======================================================================

    def _route_spot(self):
        a = self.options.spot_action
        if a == "apply_spot":  self._apply_spot()
        elif a == "list_spots": self._list_spots()

    def _apply_spot(self):
        if not self.svg.selected:
            inkex.errormsg("No objects selected.")
            return
        name = self.options.spot_name.strip()
        cmyk = spot_to_cmyk(name)
        if cmyk is None:
            inkex.errormsg(f"Unknown spot: '{name}'. Use List to see available names.")
            return
        c, m, y, k = cmyk
        hex_color = cmyk_to_hex(c, m, y, k)
        target = self.options.spot_target

        for node in self.svg.selected.values():
            self._set_solid_color(node, hex_color, 1.0, c, m, y, k, target)
            node.set(ATTR_SPOT_NAME, name)

        self._ensure_namespace()
        inkex.utils.debug(
            f"Spot: {name}\n"
            f"  CMYK: {c*100:.0f}% {m*100:.0f}% {y*100:.0f}% {k*100:.0f}%\n"
            f"  RGB:  {hex_color}"
        )

    def _list_spots(self):
        lines = [f"  {n}: C={v[0]*100:.0f}% M={v[1]*100:.0f}% Y={v[2]*100:.0f}% K={v[3]*100:.0f}%"
                 for n, v in sorted(SPOT_COLORS.items())]
        inkex.utils.debug("Spot colours:\n" + "\n".join(lines))

    # =======================================================================
    # ICC Profile tab
    # =======================================================================

    def _route_icc(self):
        a = self.options.icc_action
        if a == "embed_icc":   self._embed_icc()
        elif a == "remove_icc": self._remove_icc()

    def _embed_icc(self):
        choice = self.options.icc_profile
        icc_path = None

        if choice == "fogra39":
            icc_path = get_fogra39_icc_path()
            if not icc_path:
                inkex.errormsg(
                    "Fogra39 ICC not found. Download ISOcoated_v2_eci.icc from eci.org\n"
                    "and place at ~/.color/icc/ISOcoated_v2_eci.icc\n"
                    "Falling back to sRGB."
                )
                choice = "srgb"
        if choice == "srgb":
            icc_path = get_srgb_icc_path()
        if choice == "custom":
            p = self.options.icc_path.strip()
            icc_path = p if os.path.isfile(p) else None

        if not icc_path:
            inkex.errormsg("No valid ICC profile found.")
            return

        data_uri = icc_profile_to_data_uri(icc_path)
        if not data_uri:
            inkex.errormsg(f"Failed to read: {icc_path}")
            return

        defs = self._get_or_create_defs()
        for old in defs.findall(f"{{{SVG_NS}}}color-profile[@id='cmyk-icc-profile']"):
            defs.remove(old)

        profile_name = {
            "fogra39": "Fogra39 (ISO Coated v2)",
            "srgb":    "sRGB",
        }.get(choice, os.path.splitext(os.path.basename(icc_path))[0])

        inkex.etree.SubElement(
            defs, f"{{{SVG_NS}}}color-profile",
            attrib={
                "id": "cmyk-icc-profile",
                "name": profile_name,
                f"{{{XLINK_NS}}}href": data_uri,
                "rendering-intent": "relative-colorimetric",
                ATTR_ICC_HREF: icc_path,
            }
        )
        self.document.getroot().set("color-profile", "url(#cmyk-icc-profile)")
        inkex.utils.debug(f"Embedded ICC: {os.path.basename(icc_path)}")

    def _remove_icc(self):
        defs = self._get_or_create_defs()
        n = 0
        for old in defs.findall(f"{{{SVG_NS}}}color-profile[@id='cmyk-icc-profile']"):
            defs.remove(old)
            n += 1
        root = self.document.getroot()
        if root.get("color-profile"):
            del root.attrib["color-profile"]
        inkex.utils.debug(f"Removed {n} ICC profile(s).")

    # =======================================================================
    # SVG Preserve tab
    # =======================================================================

    def _route_svg(self):
        a = self.options.svg_action
        if a == "save_metadata":    self._save_metadata()
        elif a == "restore_metadata": self._restore_metadata()

    def _save_metadata(self):
        root = self.document.getroot()
        records = []
        for node in root.iter():
            eid = node.get("id")
            if not eid or node.get(ATTR_C) is None:
                continue
            rec = {
                "id": eid,
                "c":  float(node.get(ATTR_C,0)),
                "m":  float(node.get(ATTR_M,0)),
                "y":  float(node.get(ATTR_Y,0)),
                "k":  float(node.get(ATTR_K,0)),
                "target": node.get(ATTR_TARGET,"fill"),
            }
            spot = node.get(ATTR_SPOT_NAME)
            grad = node.get(ATTR_GRAD_STOPS)
            op_f = node.get(ATTR_OVERPRINT_FILL,  "0")
            op_s = node.get(ATTR_OVERPRINT_STROKE,"0")
            if spot:  rec["spot"] = spot
            if grad:  rec["grad_stops"] = json.loads(grad)
            if op_f != "0": rec["op_fill"]   = op_f
            if op_s != "0": rec["op_stroke"] = op_s
            records.append(rec)

        if not records:
            inkex.utils.debug("No annotated elements to save.")
            return

        blob = encode_cmyk_metadata(records)
        meta_el = root.find(f"{{{SVG_NS}}}metadata")
        if meta_el is None:
            meta_el = inkex.etree.SubElement(root, f"{{{SVG_NS}}}metadata")
        el = meta_el.find(f".//*[@id='{METADATA_CMYK_ID}']")
        if el is None:
            el = inkex.etree.SubElement(
                meta_el, f"{{{CMYK_NS}}}data",
                attrib={"id": METADATA_CMYK_ID}
            )
        el.text = blob
        # Write version stamp so future migrations know what wrote the file
        ver_el = meta_el.find(f".//*[@id='{METADATA_VERSION_ID}']")
        if ver_el is None:
            ver_el = inkex.etree.SubElement(
                meta_el, f"{{{CMYK_NS}}}version",
                attrib={"id": METADATA_VERSION_ID}
            )
        ver_el.text = PLUGIN_VERSION
        self._ensure_namespace()
        inkex.utils.debug(
            f"Saved {len(records)} element(s) to <metadata>.\n"
            "Overprint flags included. Safe to export as Plain SVG."
        )

    def _restore_metadata(self):
        root = self.document.getroot()
        meta_el = root.find(f"{{{SVG_NS}}}metadata")
        if meta_el is None:
            inkex.errormsg("No <metadata> found.")
            return
        el = meta_el.find(f".//*[@id='{METADATA_CMYK_ID}']")
        if el is None or not el.text:
            inkex.errormsg("No CMYK metadata found in <metadata>.")
            return

        records  = decode_cmyk_metadata(el.text)
        id_map   = {n.get("id"): n for n in root.iter() if n.get("id")}
        restored = 0

        for rec in records:
            node = id_map.get(rec.get("id"))
            if node is None: continue
            c,m,y,k = rec["c"],rec["m"],rec["y"],rec["k"]
            target   = rec.get("target","fill")
            self._set_solid_color(node, cmyk_to_hex(c,m,y,k), 1.0, c,m,y,k, target)
            if "spot"       in rec: node.set(ATTR_SPOT_NAME, rec["spot"])
            if "grad_stops" in rec: node.set(ATTR_GRAD_STOPS,
                                              json.dumps(rec["grad_stops"],separators=(",",":")))
            if rec.get("op_fill"):   node.set(ATTR_OVERPRINT_FILL,   rec["op_fill"])
            if rec.get("op_stroke"): node.set(ATTR_OVERPRINT_STROKE, rec["op_stroke"])
            restored += 1

        self._ensure_namespace()
        inkex.utils.debug(f"Restored {restored} element(s) from metadata.")

    # =======================================================================
    # Export tab
    # =======================================================================

    def _route_export(self):
        a = self.options.export_action
        if a == "export_pdf_gs": self._export_pdf_ghostscript()
        elif a == "export_sla":  self._export_scribus_sla()

    def _export_pdf_ghostscript(self):
        out_path = self.options.export_path.strip()
        if not out_path:
            inkex.errormsg("Specify an output PDF path.")
            return

        gs_exe = self._find_ghostscript()
        if not gs_exe:
            inkex.errormsg("Ghostscript not found. Install it or set the path.")
            return

        with tempfile.NamedTemporaryFile(suffix=".svg", delete=False) as tmp:
            tmp_svg = tmp.name
            self.document.write(tmp_svg)

        tmp_rgb = tmp_svg.replace(".svg", "_rgb.pdf")
        try:
            inkscape = self._find_inkscape()
            if not inkscape:
                inkex.errormsg("Inkscape CLI not found.")
                return
            subprocess.run([inkscape, "--export-type=pdf",
                            f"--export-filename={tmp_rgb}", tmp_svg],
                           check=True, capture_output=True)

            icc_path = get_fogra39_icc_path() or get_srgb_icc_path()

            # Combine compression + overprint GS args
            gs_args = [gs_exe] + build_gs_compression_args()
            gs_args += [f"-sOutputFile={out_path}"]
            if icc_path:
                gs_args.append(f"-sOutputICCProfile={icc_path}")
            # Overprint preamble via -c ... -f
            gs_args += ["-c", "true setoverprint <</OPM 1>> setuserparams", "-f"]
            gs_args.append(tmp_rgb)

            result = subprocess.run(gs_args, capture_output=True, text=True)
            if result.returncode != 0:
                inkex.errormsg(f"Ghostscript error:\n{result.stderr[-1000:]}")
                return

            inkex.utils.debug(
                f"CMYK PDF → {out_path}\n"
                f"  GS: {gs_exe}\n"
                f"  ICC: {icc_path or '(none)'}\n"
                "  Overprint mode 1 (OPM 1) enabled.\n\n"
                "Verify separations:\n"
                "  Adobe Acrobat Pro: Tools > Print Production > Output Preview > Separations\n"
                "  CLI: pdfinfo output.pdf | grep -i color"
            )
        finally:
            for p in (tmp_svg, tmp_rgb):
                try: os.unlink(p)
                except OSError: pass

    def _export_scribus_sla(self):
        out_path = self.options.export_path.strip()
        if not out_path: out_path = "output.sla"
        if not out_path.endswith(".sla"): out_path += ".sla"

        root   = self.document.getroot()
        colors = {}
        for node in root.iter():
            c = node.get(ATTR_C)
            if c is None: continue
            cf,mf,yf,kf = float(c),float(node.get(ATTR_M,0)),\
                          float(node.get(ATTR_Y,0)),float(node.get(ATTR_K,0))
            spot = node.get(ATTR_SPOT_NAME,"")
            name = spot or f"CMYK_{cf*100:.0f}_{mf*100:.0f}_{yf*100:.0f}_{kf*100:.0f}"
            colors[name] = (cf,mf,yf,kf)

        color_defs = "\n    ".join(
            f'<COLOR Spot="{1 if "PANTONE" in n.upper() else 0}" '
            f'CMYK="#{"".join(f"{int(v*255):02X}" for v in cmyk)}" NAME="{n}" />'
            for n,cmyk in colors.items()
        )
        w = root.get("width","210mm"); h = root.get("height","297mm")
        sla = (
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            f'<SCRIBUSDOC Version="1.5.8" UNIT="0">\n'
            f'  <DOCUMENT PAGEWIDTH="{w}" PAGEHEIGHT="{h}">\n'
            f'    <COLORS>\n      {color_defs}\n    </COLORS>\n'
            '  </DOCUMENT>\n</SCRIBUSDOC>\n'
        )
        with open(out_path,"w",encoding="utf-8") as f:
            f.write(sla)
        inkex.utils.debug(f"Scribus SLA → {out_path}  ({len(colors)} colours)")

    # =======================================================================
    # Document annotation
    # =======================================================================

    def _annotate_document(self):
        root = self.document.getroot()
        count = 0
        for node in root.iter():
            tag = node.tag if isinstance(node.tag,str) else ""
            if not tag.startswith("{") or not hasattr(node,"style"): continue
            style = node.style
            for prop in ("fill","stroke"):
                val = style.get(prop,"")
                if val and val not in ("none","inherit","transparent",""):
                    try:
                        c,m,y,k = rgb_to_cmyk(*hex_to_rgb(val))
                        node.set(ATTR_C, f"{c:.6f}")
                        node.set(ATTR_M, f"{m:.6f}")
                        node.set(ATTR_Y, f"{y:.6f}")
                        node.set(ATTR_K, f"{k:.6f}")
                        node.set(ATTR_TARGET, prop)
                        node.set(ATTR_INK_TOTAL, f"{ink_total(c,m,y,k):.2f}")
                        count += 1
                    except Exception: pass
        self._ensure_namespace()
        inkex.utils.debug(f"Annotated {count} colours in document.")

    # =======================================================================
    # ─────────────────────────────────────────────────────────────────────
    # TAB: OVERPRINT  (v2.1)
    # ─────────────────────────────────────────────────────────────────────
    # =======================================================================

    def _route_overprint(self):
        a = self.options.op_action
        if a == "set_overprint":     self._set_overprint()
        elif a == "clear_overprint": self._clear_overprint()
        elif a == "read_overprint":  self._read_overprint()
        elif a == "gs_preamble":     self._show_overprint_gs_preamble()

    def _set_overprint(self):
        if not self.svg.selected:
            inkex.errormsg("No objects selected.")
            return

        fill_op    = bool(self.options.op_fill)
        stroke_op  = bool(self.options.op_stroke)
        preview    = bool(self.options.op_preview)

        for node in self.svg.selected.values():
            node.set(ATTR_OVERPRINT_FILL,   "1" if fill_op   else "0")
            node.set(ATTR_OVERPRINT_STROKE, "1" if stroke_op else "0")

            # Update inline style with mix-blend-mode:multiply for screen preview
            raw_style = node.get("style") or ""
            updated   = apply_overprint_style(raw_style, fill_op, stroke_op, preview)
            node.set("style", updated)

        self._ensure_namespace()
        mode_str = []
        if fill_op:   mode_str.append("fill")
        if stroke_op: mode_str.append("stroke")
        inkex.utils.debug(
            f"Overprint set on {len(self.svg.selected)} object(s).\n"
            f"  Channels: {', '.join(mode_str) or 'none'}\n"
            f"  Preview (mix-blend-mode:multiply): {'ON' if preview else 'OFF'}\n"
            "  In the exported PDF, use Ghostscript with OPM 1 (Export tab)."
        )

    def _clear_overprint(self):
        if not self.svg.selected:
            inkex.errormsg("No objects selected.")
            return

        for node in self.svg.selected.values():
            node.set(ATTR_OVERPRINT_FILL,   "0")
            node.set(ATTR_OVERPRINT_STROKE, "0")
            # Remove blend mode from style
            raw_style = node.get("style") or ""
            cleaned   = apply_overprint_style(raw_style, False, False, preview_mode=False)
            node.set("style", cleaned)

        inkex.utils.debug(
            f"Overprint cleared on {len(self.svg.selected)} object(s)."
        )

    def _read_overprint(self):
        if not self.svg.selected:
            inkex.errormsg("No objects selected.")
            return
        node = next(iter(self.svg.selected.values()))
        op   = OverprintState.from_element_attrs(node.get)
        c    = float(node.get(ATTR_C,0) or 0)
        m    = float(node.get(ATTR_M,0) or 0)
        y    = float(node.get(ATTR_Y,0) or 0)
        k    = float(node.get(ATTR_K,0) or 0)

        lines = [
            f"Overprint state for [{node.get('id','')}]:",
            f"  Fill overprint:   {'ON' if op.fill_overprint   else 'OFF'}",
            f"  Stroke overprint: {'ON' if op.stroke_overprint else 'OFF'}",
            f"  Ink total:        {ink_total(c,m,y,k):.1f}%",
            f"  Rich black:       {'YES' if is_rich_black(c,m,y,k) else 'NO'}",
        ]
        inkex.utils.debug("\n".join(lines))

    def _show_overprint_gs_preamble(self):
        inkex.utils.debug(
            "─── Ghostscript Overprint Preamble ───\n"
            + overprint_gs_preamble()
            + "\n─── Ghostscript CLI flags ───\n"
            + " ".join(build_gs_overprint_args())
            + "\n\nPaste the preamble into a .ps prologue file,\n"
            "or use the Export tab which applies it automatically."
        )

    # =======================================================================
    # ─────────────────────────────────────────────────────────────────────
    # TAB: PREFLIGHT  (v2.1)
    # ─────────────────────────────────────────────────────────────────────
    # =======================================================================

    def _route_preflight(self):
        a = self.options.pf_action
        if a == "run_preflight":   self._run_preflight()
        elif a == "mark_warnings": self._mark_preflight_warnings()
        elif a == "clear_marks":   self._clear_preflight_marks()

    def _run_preflight(self):
        cfg = PreflightConfig(
            ink_limit_pct    = float(self.options.pf_ink_limit),
            min_stroke_pt    = float(self.options.pf_min_stroke),
            check_bleed      = bool(self.options.pf_bleed),
            check_images     = bool(self.options.pf_images),
            check_gradients  = bool(self.options.pf_gradients),
            check_overprint  = bool(self.options.pf_overprint),
        )

        root = self.document.getroot()

        def _get_doc(attr, default=""):
            return root.get(attr) or default

        report = run_preflight(root.iter(), _get_doc, cfg)

        fmt = self.options.pf_format
        if fmt == "json":
            inkex.utils.debug(report.to_json())
        else:
            inkex.utils.debug(report.to_text())

    def _mark_preflight_warnings(self):
        """
        Run preflight and store the warning codes as a JSON attr on each
        offending element so the user can find them via XML editor.
        Also applies a red stroke to error elements for visual identification.
        """
        cfg = PreflightConfig(
            ink_limit_pct = float(self.options.pf_ink_limit),
            min_stroke_pt = float(self.options.pf_min_stroke),
        )
        root = self.document.getroot()
        report = run_preflight(root.iter(), lambda a,d="": root.get(a) or d, cfg)

        # Gather issues by element id
        issues_by_id: dict = {}
        for issue in report.issues:
            eid = issue.element_id
            if not eid: continue
            issues_by_id.setdefault(eid, []).append(issue.to_dict())

        id_map = {n.get("id"): n for n in root.iter() if n.get("id")}
        marked = 0
        for eid, issues in issues_by_id.items():
            node = id_map.get(eid)
            if node is None: continue
            node.set(ATTR_PREFLIGHT_WARN,
                     json.dumps([i["code"] for i in issues], separators=(",",":")))
            # Visual: red stroke for errors
            if any(i["severity"] == "error" for i in issues):
                style = node.style
                style["stroke"]       = "#ff0000"
                style["stroke-width"] = "2px"
                node.style = style
            marked += 1

        self._ensure_namespace()
        inkex.utils.debug(
            f"Preflight: {len(report.errors())} error(s), "
            f"{len(report.warnings())} warning(s).\n"
            f"Marked {marked} element(s) with cmyk:preflight-warn attribute.\n"
            "Red stroke applied to error elements."
        )

    def _clear_preflight_marks(self):
        root = self.document.getroot()
        cleared = 0
        for node in root.iter():
            if node.get(ATTR_PREFLIGHT_WARN) is not None:
                del node.attrib[ATTR_PREFLIGHT_WARN]
                # Remove red stroke if we added it
                try:
                    style = node.style
                    if style.get("stroke") == "#ff0000":
                        del style["stroke"]
                        del style["stroke-width"]
                        node.style = style
                except Exception:
                    pass
                cleared += 1
        inkex.utils.debug(f"Cleared preflight marks from {cleared} element(s).")

    # =======================================================================
    # ─────────────────────────────────────────────────────────────────────
    # TAB: COMPRESSION  (v2.1)
    # ─────────────────────────────────────────────────────────────────────
    # =======================================================================

    def _route_compression(self):
        a = self.options.cmp_action
        if a == "compress_document": self._compress_document()
        elif a == "report_sizes":    self._report_sizes()
        elif a == "save_svgz":       self._save_svgz()

    def _compress_document(self):
        """
        Serialise the current document, run all compression passes,
        then parse the result back and replace the document tree.
        """
        import io as _io
        from lxml import etree as _etree

        precision = int(self.options.cmp_precision)
        dedup     = bool(self.options.cmp_dedup)
        make_svgz = bool(self.options.cmp_svgz)
        svgz_path = self.options.cmp_svgz_path.strip()

        # Serialise current document to string
        buf = _io.BytesIO()
        self.document.write(buf, encoding="utf-8", xml_declaration=True)
        original_svg = buf.getvalue().decode("utf-8")

        compressed_svg, stats = compress_svg_document(
            original_svg,
            path_precision   = precision,
            dedup_styles     = dedup,
            produce_svgz     = make_svgz,
            compression_level= 9,
        )

        # Parse compressed back and swap the document
        try:
            new_tree = _etree.parse(_io.StringIO(compressed_svg))
            # Replace our document tree's root content
            old_root = self.document.getroot()
            new_root = new_tree.getroot()
            old_root.clear()
            old_root.tag  = new_root.tag
            old_root.text = new_root.text
            old_root.tail = new_root.tail
            for k, v in new_root.attrib.items():
                old_root.set(k, v)
            for child in new_root:
                old_root.append(child)
        except Exception as e:
            inkex.errormsg(f"Failed to parse compressed SVG: {e}")
            return

        # Optionally write SVGZ
        if make_svgz and svgz_path:
            svgz_bytes = compress_svg_bytes(compressed_svg.encode("utf-8"))
            try:
                with open(svgz_path, "wb") as f:
                    f.write(svgz_bytes)
                inkex.utils.debug(f"SVGZ written → {svgz_path}")
            except OSError as e:
                inkex.errormsg(f"Could not write SVGZ: {e}")

        inkex.utils.debug(stats.summary())

    def _report_sizes(self):
        """Report path count and estimated byte budget per element."""
        import io as _io
        buf = _io.BytesIO()
        self.document.write(buf, encoding="utf-8")
        total_bytes = len(buf.getvalue())

        root  = self.document.getroot()
        paths = []
        for node in root.iter():
            d_attr = node.get("d","")
            if d_attr:
                b = len(d_attr.encode("utf-8"))
                eid = node.get("id","(no id)")
                paths.append((b, eid, d_attr[:60]))

        paths.sort(reverse=True)
        lines = [
            f"SVG total: {total_bytes:,} bytes",
            f"Path elements: {len(paths)}",
            "",
            f"{'Bytes':>8}  {'ID':30}  {'d (preview)'}"
        ]
        for b, eid, preview in paths[:20]:
            lines.append(f"{b:>8,}  {eid:30}  {preview}…")
        if len(paths) > 20:
            lines.append(f"  … and {len(paths)-20} more paths")

        inkex.utils.debug("\n".join(lines))

    def _save_svgz(self):
        """Write the current document as SVGZ directly."""
        import io as _io
        svgz_path = self.options.cmp_svgz_path.strip()
        if not svgz_path:
            inkex.errormsg("Enter an output .svgz path in the Compression tab.")
            return
        if not svgz_path.endswith(".svgz"):
            svgz_path += ".svgz"

        buf = _io.BytesIO()
        self.document.write(buf, encoding="utf-8", xml_declaration=True)
        svg_bytes  = buf.getvalue()
        svgz_bytes = compress_svg_bytes(svg_bytes, level=9)

        with open(svgz_path,"wb") as f:
            f.write(svgz_bytes)

        ratio = (1 - len(svgz_bytes)/len(svg_bytes)) * 100
        inkex.utils.debug(
            f"SVGZ written → {svgz_path}\n"
            f"  SVG:  {len(svg_bytes):,} bytes\n"
            f"  SVGZ: {len(svgz_bytes):,} bytes  ({ratio:.1f}% reduction)"
        )

    # =======================================================================

    # =======================================================================
    # TAB: SEPARATIONS PREVIEW  (v2.2)
    # =======================================================================

    def _route_separations(self):
        a = self.options.sep_action
        if   a == "preview_channel": self._sep_preview_channel()
        elif a == "preview_spot":    self._sep_preview_spot()
        elif a == "preview_fourup":  self._sep_preview_fourup()
        elif a == "list_plates":     self._sep_list_plates()
        elif a == "restore":         self._sep_restore()

    def _sep_get_page_size(self):
        root = self.document.getroot()
        try:
            w = float(root.get("width",  "794").rstrip("pxm"))
            h = float(root.get("height", "1123").rstrip("pxm"))
        except ValueError:
            w, h = 794.0, 1123.0
        return w, h

    def _sep_collect_annotated(self):
        result = []
        for node in self.document.getroot().iter():
            c_val = node.get(ATTR_C)
            if c_val is None:
                continue
            c = float(c_val or 0)
            m = float(node.get(ATTR_M, 0) or 0)
            y = float(node.get(ATTR_Y, 0) or 0)
            k = float(node.get(ATTR_K, 0) or 0)
            spot = node.get(ATTR_SPOT_NAME, "")
            result.append((node, c, m, y, k, spot))
        return result

    def _sep_remove_preview_layers(self):
        root = self.document.getroot()
        INS  = "http://www.inkscape.org/namespaces/inkscape"
        to_remove = [
            child for child in root
            if child.get(f"{{{INS}}}label", "").startswith(SEPARATION_LAYER_PREFIX)
        ]
        for el in to_remove:
            root.remove(el)
        return len(to_remove)

    def _sep_unhide_originals(self):
        root = self.document.getroot()
        INS  = "http://www.inkscape.org/namespaces/inkscape"
        for child in root:
            label = child.get(f"{{{INS}}}label", "")
            if not label.startswith(SEPARATION_LAYER_PREFIX):
                st = child.get("style", "")
                child.set("style", st.replace("display:none", "display:inline"))

    def _sep_create_layer(self, label, channel):
        root     = self.document.getroot()
        INS      = "http://www.inkscape.org/namespaces/inkscape"
        layer_id = self._unique_id("cmykSepLayer")
        return inkex.etree.SubElement(
            root, f"{{{SVG_NS}}}g",
            attrib={
                "id":                          layer_id,
                f"{{{INS}}}label":             label,
                f"{{{INS}}}groupmode":         "layer",
                ATTR_SEP_CHANNEL:              channel,
            }
        )

    def _sep_clone_element(self, layer, node, style_override, transform=""):
        clone_id = self._unique_id("cmykSepEl")
        attrib   = {"id": clone_id, "style": style_override}
        if transform:
            attrib["transform"] = transform
        for attr in ("d","cx","cy","r","rx","ry","x","y","width","height",
                     "x1","y1","x2","y2","points"):
            val = node.get(attr)
            if val:
                attrib[attr] = val
        if not transform:
            tr = node.get("transform")
            if tr:
                attrib["transform"] = tr
        tag = node.tag.split("}")[-1] if "}" in node.tag else node.tag
        return inkex.etree.SubElement(layer, f"{{{SVG_NS}}}{tag}", attrib=attrib)

    def _sep_preview_channel(self):
        channel = self.options.sep_channel.lower().strip()
        if channel not in SEPARATION_CHANNELS:
            inkex.errormsg(f"Unknown channel '{channel}'. Use c, m, y, or k.")
            return
        tinted = bool(self.options.sep_tinted)
        self._sep_remove_preview_layers()
        root = self.document.getroot()
        INS  = "http://www.inkscape.org/namespaces/inkscape"
        for child in root:
            lbl = child.get(f"{{{INS}}}label", "")
            if not lbl.startswith(SEPARATION_LAYER_PREFIX):
                st = child.get("style", "")
                if "display:none" not in st:
                    child.set("style", st + ";display:none")
        pw, ph   = self._sep_get_page_size()
        bg_layer = self._sep_create_layer(f"{SEPARATION_LAYER_PREFIX}:bg", "bg")
        inkex.etree.SubElement(
            bg_layer, f"{{{SVG_NS}}}rect",
            attrib={"x":"0","y":"0","width":str(pw),"height":str(ph),
                    "style":"fill:#ffffff;stroke:none"}
        )
        label    = SEPARATION_LABELS.get(channel, channel.upper())
        layer    = self._sep_create_layer(build_separation_layer_name(channel), channel)
        annotated= self._sep_collect_annotated()
        count    = 0
        for node, c, m, y, k, spot in annotated:
            new_style = separation_style(c, m, y, k, channel,
                                         tinted=tinted,
                                         existing_style=node.get("style",""))
            self._sep_clone_element(layer, node, new_style)
            count += 1
        self._ensure_namespace()
        inkex.utils.debug(
            "Separations: {} plate preview\n"
            "  {} element(s) rendered\n"
            "  Mode: {}\n"
            "  Use 'Restore original' to return to colour view.".format(
                label, count, "tinted" if tinted else "greyscale"
            )
        )

    def _sep_preview_spot(self):
        target = self.options.sep_spot.strip()
        if not target:
            inkex.errormsg("Enter a spot colour name in the Spot plate field.")
            return
        self._sep_remove_preview_layers()
        root = self.document.getroot()
        INS  = "http://www.inkscape.org/namespaces/inkscape"
        for child in root:
            lbl = child.get(f"{{{INS}}}label", "")
            if not lbl.startswith(SEPARATION_LAYER_PREFIX):
                st = child.get("style", "")
                if "display:none" not in st:
                    child.set("style", st + ";display:none")
        pw, ph   = self._sep_get_page_size()
        bg_layer = self._sep_create_layer(f"{SEPARATION_LAYER_PREFIX}:bg","bg")
        inkex.etree.SubElement(
            bg_layer, f"{{{SVG_NS}}}rect",
            attrib={"x":"0","y":"0","width":str(pw),"height":str(ph),
                    "style":"fill:#ffffff;stroke:none"}
        )
        layer    = self._sep_create_layer(f"{SEPARATION_LAYER_PREFIX}:{target[:30]}", target)
        annotated= self._sep_collect_annotated()
        count = on_plate = 0
        for node, c, m, y, k, spot in annotated:
            new_style = spot_coverage_style(c, m, y, k, spot, target,
                                            existing_style=node.get("style",""))
            self._sep_clone_element(layer, node, new_style)
            count += 1
            if spot.strip().lower() == target.strip().lower():
                on_plate += 1
        self._ensure_namespace()
        inkex.utils.debug(
            "Spot plate: {}\n  {} element(s) on plate of {} total.\n"
            "  Use 'Restore original' to return to colour view.".format(
                target, on_plate, count
            )
        )

    def _sep_preview_fourup(self):
        self._sep_remove_preview_layers()
        root = self.document.getroot()
        INS  = "http://www.inkscape.org/namespaces/inkscape"
        for child in root:
            lbl = child.get(f"{{{INS}}}label", "")
            if not lbl.startswith(SEPARATION_LAYER_PREFIX):
                st = child.get("style", "")
                if "display:none" not in st:
                    child.set("style", st + ";display:none")
        pw, ph     = self._sep_get_page_size()
        transforms = four_up_transforms(pw, ph)
        annotated  = self._sep_collect_annotated()
        tinted     = bool(self.options.sep_tinted)
        for i, channel in enumerate(SEPARATION_CHANNELS):
            label = SEPARATION_LABELS[channel]
            layer = self._sep_create_layer(build_separation_layer_name(channel), channel)
            inkex.etree.SubElement(
                layer, f"{{{SVG_NS}}}rect",
                attrib={"x":"0","y":"0","width":str(pw),"height":str(ph),
                        "style":"fill:#ffffff;stroke:none","transform":transforms[i]}
            )
            text_el = inkex.etree.SubElement(
                layer, f"{{{SVG_NS}}}text",
                attrib={"x":"8","y":"20",
                        "style":"font-family:sans-serif;font-size:12px;font-weight:bold;fill:#888;",
                        "transform":transforms[i]}
            )
            text_el.text = label
            for node, c, m, y, k, spot in annotated:
                new_style = separation_style(c, m, y, k, channel,
                                              tinted=tinted,
                                              existing_style=node.get("style",""))
                self._sep_clone_element(layer, node, new_style, transform=transforms[i])
        self._ensure_namespace()
        inkex.utils.debug(
            "Four-up plate preview (C/M/Y/K).\n"
            "  {} element(s) per plate.\n"
            "  Use 'Restore original' to return to colour view.".format(len(annotated))
        )

    def _sep_list_plates(self):
        annotated = self._sep_collect_annotated()
        plates    = separation_plates_for_document(n for n, *_ in annotated)
        lines     = ["Plates in document ({} total):".format(len(plates))]
        for p in plates:
            spot_tag = " [SPOT]" if p.is_spot else ""
            lines.append("  {:<30}  {} element(s){}".format(
                p.label, p.element_count, spot_tag))
        inkex.utils.debug("\n".join(lines))

    def _sep_restore(self):
        removed = self._sep_remove_preview_layers()
        self._sep_unhide_originals()
        inkex.utils.debug(
            "Restored original view. Removed {} preview layer(s).".format(removed))

    # =======================================================================
    # TAB: TRAPPING  (v2.2)
    # =======================================================================

    def _route_trapping(self):
        a = self.options.trap_action
        if   a == "find_traps":  self._trap_find()
        elif a == "apply_traps": self._trap_apply()
        elif a == "clear_traps": self._trap_clear()

    def _trap_find(self):
        root = self.document.getroot()
        elements = []
        for node in root.iter():
            c_val = node.get(ATTR_C)
            if c_val is None or not node.get("id"):
                continue
            elements.append({
                "id": node.get("id"),
                "c": float(c_val or 0),
                "m": float(node.get(ATTR_M,0) or 0),
                "y": float(node.get(ATTR_Y,0) or 0),
                "k": float(node.get(ATTR_K,0) or 0),
            })
        report = find_trap_pairs(elements)
        if not report.pairs:
            inkex.utils.debug(
                "Trap analysis: no trapping needed.\n"
                "  {} element(s) checked.".format(report.n_checked))
            return
        lines = [report.summary(), ""]
        for pair in report.pairs:
            tc = pair.trap_color
            lines.append("  {:<20} + {:<20}  trap: C={:.0f}% M={:.0f}% Y={:.0f}% K={:.0f}%".format(
                pair.id_a, pair.id_b,
                tc[0]*100, tc[1]*100, tc[2]*100, tc[3]*100))
        lines.append("\nRun 'Apply trap strokes' to add strokes.")
        inkex.utils.debug("\n".join(lines))

    def _trap_apply(self):
        root      = self.document.getroot()
        width_pt  = float(self.options.trap_width)
        elements  = {}
        cmyk_list = []
        for node in root.iter():
            c_val = node.get(ATTR_C)
            if c_val is None or not node.get("id"):
                continue
            eid = node.get("id")
            elements[eid] = node
            cmyk_list.append({
                "id": eid,
                "c": float(c_val or 0),
                "m": float(node.get(ATTR_M,0) or 0),
                "y": float(node.get(ATTR_Y,0) or 0),
                "k": float(node.get(ATTR_K,0) or 0),
            })
        report  = find_trap_pairs(cmyk_list)
        applied = 0
        for pair in report.pairs:
            node = elements.get(pair.id_a)
            if node is None:
                continue
            new_st = trap_stroke_style(pair.trap_color, width_pt, node.get("style",""))
            node.set("style", new_st)
            existing = node.get(ATTR_TRAP_PAIRS,"[]")
            try:
                trap_ids = json.loads(existing)
            except Exception:
                trap_ids = []
            if pair.id_b not in trap_ids:
                trap_ids.append(pair.id_b)
            node.set(ATTR_TRAP_PAIRS, json.dumps(trap_ids, separators=(",",":")))
            applied += 1
        self._ensure_namespace()
        inkex.utils.debug(
            "Applied {} trap stroke(s) at {:.3f}pt.\n"
            "  Trap strokes are set to overprint.".format(applied, width_pt))

    def _trap_clear(self):
        root    = self.document.getroot()
        cleared = 0
        for node in root.iter():
            if node.get(ATTR_TRAP_PAIRS) is not None:
                del node.attrib[ATTR_TRAP_PAIRS]
                try:
                    style = node.style
                    for prop in ("stroke-width","stroke","stroke-opacity"):
                        style.pop(prop, None)
                    node.style = style
                except Exception:
                    pass
                cleared += 1
        inkex.utils.debug("Cleared trap strokes from {} element(s).".format(cleared))

    # =======================================================================
    # TAB: PATTERNS  (v2.2)
    # =======================================================================

    def _route_patterns(self):
        a = self.options.pat_action
        if   a == "annotate_patterns": self._pat_annotate()
        elif a == "read_patterns":     self._pat_read()
        elif a == "clear_patterns":    self._pat_clear()

    def _pat_annotate(self):
        root = self.document.getroot()
        defs = root.find(f"{{{SVG_NS}}}defs")
        if defs is None:
            inkex.utils.debug("No <defs> block found.")
            return
        annotated_count = 0
        for pattern in defs.findall(f"{{{SVG_NS}}}pattern"):
            child_colors = []
            for idx, child in enumerate(pattern):
                if not hasattr(child, "style"):
                    continue
                style = child.style
                for prop in ("fill","stroke"):
                    val = style.get(prop,"")
                    if val and val not in ("none","inherit","transparent"):
                        rgb = hex_to_rgb(val)
                        c,m,y,k = rgb_to_cmyk(*rgb)
                        child_colors.append({
                            "child_index": idx, "prop": prop,
                            "c": c, "m": m, "y": y, "k": k,
                        })
                        child.set(ATTR_C, f"{c:.6f}")
                        child.set(ATTR_M, f"{m:.6f}")
                        child.set(ATTR_Y, f"{y:.6f}")
                        child.set(ATTR_K, f"{k:.6f}")
            if child_colors:
                pattern.set(ATTR_PATTERN, build_pattern_cmyk_metadata(child_colors))
                annotated_count += 1
        self._ensure_namespace()
        inkex.utils.debug(
            "Annotated {} pattern element(s) with CMYK metadata.".format(annotated_count))

    def _pat_read(self):
        root = self.document.getroot()
        defs = root.find(f"{{{SVG_NS}}}defs")
        if defs is None:
            inkex.utils.debug("No <defs> block found.")
            return
        lines = []
        for pattern in defs.findall(f"{{{SVG_NS}}}pattern"):
            blob = pattern.get(ATTR_PATTERN,"")
            if not blob:
                continue
            pid   = pattern.get("id","?")
            tiles = parse_pattern_cmyk_metadata(blob)
            lines.append("Pattern #{}: max ink {:.1f}%, {} tile(s)".format(
                pid, pattern_ink_total(blob), len(tiles)))
            for t in tiles:
                lines.append("    child[{}] {}: C={:.0f}% M={:.0f}% Y={:.0f}% K={:.0f}%  {}".format(
                    t.get("i",0), t.get("prop","fill"),
                    t["c"]*100, t["m"]*100, t["y"]*100, t["k"]*100, t.get("hex","")))
        inkex.utils.debug("\n".join(lines) if lines else "No annotated patterns found.")

    def _pat_clear(self):
        root    = self.document.getroot()
        cleared = 0
        for node in root.iter():
            if node.get(ATTR_PATTERN) is not None:
                del node.attrib[ATTR_PATTERN]
                cleared += 1
        inkex.utils.debug("Cleared pattern CMYK from {} element(s).".format(cleared))

    # =======================================================================
    # TAB: INK HEATMAP  (v2.2)
    # =======================================================================

    def _route_heatmap(self):
        a = self.options.hm_action
        if   a == "show_heatmap":   self._hm_show()
        elif a == "remove_heatmap": self._hm_remove()

    def _hm_show(self):
        self._hm_remove()
        root    = self.document.getroot()
        INS     = "http://www.inkscape.org/namespaces/inkscape"
        opacity = float(self.options.hm_opacity)
        layer_id = self._unique_id("cmykHeatmapLayer")
        hm_layer = inkex.etree.SubElement(
            root, f"{{{SVG_NS}}}g",
            attrib={
                "id":                layer_id,
                f"{{{INS}}}label":   HEATMAP_LAYER_NAME,
                f"{{{INS}}}groupmode":"layer",
            }
        )
        count = safe_n = caution_n = danger_n = 0
        for node in root.iter():
            c_val = node.get(ATTR_C)
            if c_val is None:
                continue
            c = float(c_val or 0)
            m = float(node.get(ATTR_M,0) or 0)
            y = float(node.get(ATTR_Y,0) or 0)
            k = float(node.get(ATTR_K,0) or 0)
            total    = ink_total(c, m, y, k)
            hm_style = ink_heatmap_style(c, m, y, k, opacity)
            attrib   = {"id": self._unique_id("cmykHmEl"),
                        "style": hm_style,
                        ATTR_HEATMAP_INK: f"{total:.2f}"}
            for attr in ("d","cx","cy","r","rx","ry","x","y",
                         "width","height","x1","y1","x2","y2","points","transform"):
                val = node.get(attr)
                if val:
                    attrib[attr] = val
            tag = node.tag.split("}")[-1] if "}" in node.tag else node.tag
            inkex.etree.SubElement(hm_layer, f"{{{SVG_NS}}}{tag}", attrib=attrib)
            count += 1
            if total < 250:   safe_n    += 1
            elif total < 300: caution_n += 1
            else:             danger_n  += 1
        self._ensure_namespace()
        inkex.utils.debug(
            "Ink heatmap overlay ({} element(s)):\n"
            "  Green  (< 250%): {}\n"
            "  Amber  (250-300%): {}\n"
            "  Red    (> 300%): {}\n"
            "  Run 'Remove heatmap' to restore original view.".format(
                count, safe_n, caution_n, danger_n
            )
        )

    def _hm_remove(self):
        root = self.document.getroot()
        INS  = "http://www.inkscape.org/namespaces/inkscape"
        removed = 0
        for child in list(root):
            if child.get(f"{{{INS}}}label","") == HEATMAP_LAYER_NAME:
                root.remove(child)
                removed += 1
        if removed:
            inkex.utils.debug("Removed {} heatmap layer(s).".format(removed))


    # =======================================================================
    # TAB: TRANSPARENCY  (v2.4)
    # =======================================================================

    def _route_transparency(self):
        a = self.options.tr_action
        if   a == "detect_transparency": self._tr_detect()
        elif a == "mark_transparency":   self._tr_mark()
        elif a == "flatten_advice":      self._tr_flatten_advice()

    def _tr_detect(self):
        if not _PSVG_AVAILABLE:
            inkex.errormsg("cmyk_psvg.py must be installed for transparency detection.")
            return
        root    = self.document.getroot()
        results = detect_transparency(root.iter())
        pdfx    = self.options.tr_pdfx_mode

        if not results:
            inkex.utils.debug(
                "Transparency check: CLEAN\n"
                "  No transparency-introducing properties found.\n"
                "  Document is compatible with all PDF/X modes."
            )
            return

        errors   = [r for r in results if r.severity == "error"]
        warnings = [r for r in results if r.severity == "warning"]
        lines    = ["Transparency detected in {} element(s):".format(len(results))]
        if pdfx == PDFXMode.X1A and errors:
            lines.append("  !! {} element(s) INCOMPATIBLE with PDF/X-1a".format(len(errors)))
        lines.append("")

        for info in results:
            sev = "!!" if info.severity == "error" else "  "
            lines.append("{} [{}]: {}".format(
                sev, info.element_id or "no-id", info.flattening_advice()))

        lines.extend([
            "",
            "To fix for PDF/X-1a: run 'Flatten advice' for Ghostscript commands.",
            "PDF/X-3 and PDF/X-4 support live transparency — no action needed.",
        ])
        inkex.utils.debug("\n".join(lines))

    def _tr_mark(self):
        if not _PSVG_AVAILABLE:
            inkex.errormsg("cmyk_psvg.py required.")
            return
        root    = self.document.getroot()
        results = detect_transparency(root.iter())
        if not results:
            inkex.utils.debug("No transparency found — nothing to mark.")
            return

        id_map  = {el.get("id",""):el for el in root.iter() if el.get("id")}
        marked  = 0
        for info in results:
            el = id_map.get(info.element_id)
            if el is None:
                continue
            from cmyk_psvg import PSVG_TRANS_PRESENT
            el.set(PSVG_TRANS_PRESENT, "1")
            if info.severity == "error":
                try:
                    style = el.style
                    style["stroke"]       = "#ff6600"
                    style["stroke-width"] = "1.5px"
                    el.style = style
                except Exception:
                    pass
            marked += 1

        self._ensure_namespace()
        inkex.utils.debug(
            "Marked {} transparency element(s).\n"
            "  Orange stroke = incompatible with PDF/X-1a.".format(marked)
        )

    def _tr_flatten_advice(self):
        if not _PSVG_AVAILABLE:
            inkex.errormsg("cmyk_psvg.py required.")
            return
        icc_path = get_fogra39_icc_path() or get_srgb_icc_path()
        args     = build_gs_flatten_args(icc_path)
        lines    = [
            "Ghostscript transparency flattening commands:",
            "",
            "# Step 1: export SVG to PDF (preserves transparency)",
            "inkscape --export-type=pdf --export-filename=artwork_live.pdf artwork.svg",
            "",
            "# Step 2: flatten transparency and convert to CMYK",
            "gs \\",
        ]
        for arg in args:
            lines.append("  {} \\".format(arg))
        lines.extend([
            "  -sOutputFile=artwork_flat.pdf \\",
            "  artwork_live.pdf",
            "",
            "# Result: artwork_flat.pdf has no live transparency",
            "# Safe for PDF/X-1a, legacy RIPs, and CMYK separation.",
        ])
        if icc_path:
            lines.append("# ICC profile: {}".format(icc_path))
        inkex.utils.debug("\n".join(lines))

    # =======================================================================
    # TAB: PDF/X EXPORT  (v2.4)
    # =======================================================================

    def _route_pdfx(self):
        a = self.options.px_action
        if   a == "export_pdfx":   self._px_export()
        elif a == "validate_pdfx": self._px_validate()
        elif a == "show_modes":    self._px_show_modes()

    def _px_export(self):
        if not _PSVG_AVAILABLE:
            inkex.errormsg("cmyk_psvg.py must be installed for PDF/X export.")
            return

        out_path = self.options.px_output.strip()
        if not out_path:
            inkex.errormsg("Enter an output PDF path in the PDF/X tab.")
            return

        mode     = self.options.px_mode
        gs_exe   = self._find_ghostscript()
        if not gs_exe:
            inkex.errormsg(
                "Ghostscript not found.\n"
                "Install it or set the path in the Export PDF tab.")
            return

        inkscape = self._find_inkscape()
        if not inkscape:
            inkex.errormsg("Inkscape CLI not found.")
            return

        import tempfile, os as _os
        icc_path = get_fogra39_icc_path() or get_srgb_icc_path()

        # If PDF/X-1a, check for transparency first
        if mode == PDFXMode.X1A:
            root    = self.document.getroot()
            results = detect_transparency(root.iter())
            errors  = [r for r in results if r.severity == "error"]
            if errors:
                inkex.errormsg(
                    "PDF/X-1a export blocked: {} element(s) have transparency.\n"
                    "Run Transparency tab > Detect to see details.\n"
                    "Use PDF/X-4 instead, or flatten transparency first.".format(len(errors))
                )
                return

        with tempfile.NamedTemporaryFile(suffix=".svg", delete=False) as tmp_svg:
            self.document.write(tmp_svg.name)
            tmp_svg_path = tmp_svg.name

        tmp_rgb = tmp_svg_path.replace(".svg","_rgb.pdf")
        try:
            import subprocess
            subprocess.run(
                [inkscape, "--export-type=pdf",
                 "--export-filename={}".format(tmp_rgb), tmp_svg_path],
                check=True, capture_output=True
            )

            gs_args  = [gs_exe] + PDFXMode.ghostscript_args(mode)
            gs_args += ["-sOutputFile={}".format(out_path)]
            if icc_path:
                gs_args += [
                    "-sOutputICCProfile={}".format(icc_path),
                    "-c",
                    "true setoverprint <</OPM 1>> setuserparams",
                    "-f"
                ]
            gs_args.append(tmp_rgb)

            result = subprocess.run(gs_args, capture_output=True, text=True)
            if result.returncode != 0:
                inkex.errormsg(
                    "Ghostscript failed:\n{}".format(result.stderr[-1000:]))
                return

            mode_desc = PDFXMode.DESCRIPTIONS.get(mode, mode)
            inkex.utils.debug(
                "PDF/X export complete:\n"
                "  Output:  {}\n"
                "  Mode:    {}\n"
                "  ICC:     {}\n"
                "  Verify:  Acrobat Pro > Tools > Print Production > Preflight\n"
                "           Run 'PDF/X-{} compliance' check".format(
                    out_path, mode_desc,
                    icc_path or "(not found)",
                    mode.replace("pdfx","")
                )
            )
        finally:
            for p in (tmp_svg_path, tmp_rgb):
                try: _os.unlink(p)
                except OSError: pass

    def _px_validate(self):
        if not _PSVG_AVAILABLE:
            inkex.errormsg("cmyk_psvg.py required.")
            return
        root     = self.document.getroot()
        mode     = self.options.px_mode

        def get_doc(a, d=""): return root.get(a) or d

        report = validate_psvg_document(
            get_doc, root.iter(), pdfx_mode=mode,
            spot_colors_table=SPOT_COLORS
        )
        inkex.utils.debug(report.to_text())

    def _px_show_modes(self):
        lines = ["PDF/X Export Modes:", ""]
        for mode, desc in PDFXMode.DESCRIPTIONS.items():
            if mode == PDFXMode.NONE: continue
            lines.append("  {}: {}".format(mode.upper(), desc))
        lines.extend([
            "",
            "Recommended for most print workflows: PDF/X-4",
            "  (supports transparency, modern RIPs, ICC colour management)",
            "",
            "For legacy RIPs and maximum compatibility: PDF/X-1a",
            "  (requires flattening transparency first)",
        ])
        inkex.utils.debug("\n".join(lines))

    # =======================================================================
    # TAB: PSVG SPEC  (v2.4)
    # =======================================================================

    def _route_psvg(self):
        a = self.options.ps_action
        if   a == "validate_psvg":  self._ps_validate()
        elif a == "migrate_psvg":   self._ps_migrate()
        elif a == "sep_map":        self._ps_sep_map()
        elif a == "mark_doc":       self._ps_mark_doc()
        elif a == "export_spec":    self._ps_export_spec()

    def _ps_validate(self):
        if not _PSVG_AVAILABLE:
            inkex.errormsg("cmyk_psvg.py required.")
            return
        root = self.document.getroot()
        mode = self.options.px_mode if hasattr(self.options,"px_mode") else "none"

        def get_doc(a, d=""): return root.get(a) or d

        report = validate_psvg_document(
            get_doc, root.iter(), pdfx_mode=mode,
            spot_colors_table=SPOT_COLORS
        )
        inkex.utils.debug(report.to_text())

    def _ps_migrate(self):
        if not _PSVG_AVAILABLE:
            inkex.errormsg("cmyk_psvg.py required.")
            return
        root = self.document.getroot()
        n    = migrate_cmyk_to_psvg(root)
        try:
            from lxml import etree
            etree.register_namespace(PSVG_PREFIX, PSVG_NS)
        except (ImportError, AttributeError):
            pass
        root.set(PSVG_DOC_VERSION, PSVG_VERSION)
        inkex.utils.debug(
            "Migrated {} element(s) from cmyk:* to psvg:* namespace.\n"
            "  psvg:version=\"{}\" added to root <svg>.\n"
            "  Old cmyk:* attributes removed.".format(n, PSVG_VERSION)
        )

    def _ps_sep_map(self):
        if not _PSVG_AVAILABLE:
            inkex.errormsg("cmyk_psvg.py required.")
            return
        root    = self.document.getroot()
        sep_map = build_separation_map_from_elements(root.iter(), SPOT_COLORS)

        plates = sep_map.all_plate_names()
        lines  = ["Separation map ({} plate(s)):".format(len(plates)), ""]
        lines.append("  Process plates: {}".format(
            ", ".join(sep_map.process_plates)))

        if sep_map.spots:
            lines.append("  Spot plates:")
            for s in sep_map.spots:
                c,m,y,k = s.cmyk
                lines.append(
                    "    {} [{}]  C={:.0f}% M={:.0f}% Y={:.0f}% K={:.0f}%".format(
                        s.spot_name, s.mode, c*100, m*100, y*100, k*100))
        else:
            lines.append("  No spot colours found.")

        issues = sep_map.validation_report()
        if issues:
            lines.extend(["", "Issues:"] + ["  " + i for i in issues])

        # Show GS separation args
        gs_args = sep_map.ghostscript_separation_args()
        if len(gs_args) > 1:
            lines.extend([
                "",
                "Ghostscript separation output args:",
                "  " + " ".join(gs_args)
            ])

        inkex.utils.debug("\n".join(lines))

    def _ps_mark_doc(self):
        if not _PSVG_AVAILABLE:
            inkex.errormsg("cmyk_psvg.py required.")
            return
        root = self.document.getroot()
        root.set(PSVG_DOC_VERSION,   PSVG_VERSION)
        mode = self.options.px_mode if hasattr(self.options,"px_mode") else "none"
        root.set(PSVG_DOC_PDFX_MODE, mode)
        root.set(PSVG_DOC_BLEED_MM,  "3.0")
        try:
            from lxml import etree
            etree.register_namespace(PSVG_PREFIX, PSVG_NS)
        except (ImportError, AttributeError):
            pass
        inkex.utils.debug(
            "PSVG document metadata applied to root <svg>:\n"
            "  psvg:version=\"{}\"\n"
            "  psvg:pdfx-mode=\"{}\"\n"
            "  psvg:bleed-mm=\"3.0\"".format(PSVG_VERSION, mode)
        )

    def _ps_export_spec(self):
        if not _PSVG_AVAILABLE:
            inkex.errormsg("cmyk_psvg.py required.")
            return
        out = self.options.ps_spec_out.strip()
        if not out:
            import tempfile
            out = tempfile.mktemp(suffix=".txt")
        write_spec_document(out)
        inkex.utils.debug("Print-SVG specification written to:\n  {}".format(out))

    # =======================================================================
    # TAB: SVG IMPORT / EXPORT  (v2.3)
    # =======================================================================

    def _route_io(self):
        if not _IO_AVAILABLE:
            inkex.errormsg(
                "cmyk_io.py and cmyk_export_svg.py must be installed "
                "alongside this extension for import/export."
            )
            return
        a = self.options.io_action
        if   a == "export_cmyk_svg": self._io_export()
        elif a == "import_cmyk_svg": self._io_import()
        elif a == "autosave_sync":   self._io_autosave()
        elif a == "validate":        self._io_validate()
        elif a == "strip_cmyk":      self._io_strip()
        elif a == "desync_check":    self._io_desync_check()

    def _io_collect_doc(self):
        root     = self.document.getroot()
        icc_path = get_fogra39_icc_path() or get_srgb_icc_path()
        cmyk_doc = CmykDocument(icc_path=icc_path,
                                icc_name=ICC_PROFILE_NAME,
                                version=PLUGIN_VERSION)
        for node in root.iter():
            c_val = node.get(ATTR_C)
            if c_val is None:
                continue
            eid = node.get("id","")
            if not eid:
                continue
            try:
                data = ElementCmykData(
                    element_id       = eid,
                    element_uuid     = node.get(ATTR_UUID,""),
                    c                = float(c_val or 0),
                    m                = float(node.get(ATTR_M,0) or 0),
                    y                = float(node.get(ATTR_Y,0) or 0),
                    k                = float(node.get(ATTR_K,0) or 0),
                    target           = node.get(ATTR_TARGET,"fill"),
                    spot_name        = node.get(ATTR_SPOT_NAME,""),
                    overprint_fill   = node.get(ATTR_OVERPRINT_FILL,"0")=="1",
                    overprint_stroke = node.get(ATTR_OVERPRINT_STROKE,"0")=="1",
                    knockout         = node.get(ATTR_KNOCKOUT,"auto"),
                )
            except (ValueError, TypeError):
                continue
            grad = node.get(ATTR_GRAD_STOPS)
            if grad:
                try:
                    data.grad_stops = json.loads(grad)
                except Exception:
                    pass
            cmyk_doc.elements.append(data)
        return cmyk_doc

    def _io_export(self):
        out_path = self.options.io_out_path.strip()
        if not out_path:
            inkex.errormsg("Enter an output file path in the IO tab.")
            return
        cmyk_doc = self._io_collect_doc()
        if not cmyk_doc.elements:
            inkex.utils.debug("No CMYK-annotated elements to export.")
            return
        write_cmyk_svg(self.document, cmyk_doc, out_path,
                       embed_icc=bool(self.options.io_embed_icc),
                       write_icc_color=bool(self.options.io_icc_color))
        inkex.utils.debug(
            "CMYK SVG exported: {}\n"
            "  {} element(s)\n"
            "  ICC: {}\n"
            "  Data written to cmyk:* attrs, <metadata> JSON, and icc-color() paint.".format(
                out_path, len(cmyk_doc.elements),
                cmyk_doc.icc_path or "not found"
            )
        )

    def _io_import(self):
        src_path = self.options.io_src_path.strip()
        root     = self.document.getroot()
        if src_path:
            from lxml import etree
            try:
                tree = etree.parse(src_path)
            except Exception as e:
                inkex.errormsg("Could not open: {}\n{}".format(src_path, e))
                return
            cmyk_doc = read_cmyk_svg(tree)
        else:
            cmyk_doc = read_cmyk_svg(self.document)
        if not cmyk_doc.elements:
            inkex.utils.debug(
                "No CMYK data found. "
                "Checked cmyk:* attrs, <metadata> JSON, icc-color(), RGB fallback."
            )
            return
        if not self.options.io_overwrite:
            existing = {n.get("id","") for n in root.iter()
                        if n.get(ATTR_C) is not None and n.get("id")}
            cmyk_doc.elements = [e for e in cmyk_doc.elements
                                 if e.element_id not in existing]
        n = apply_cmyk_document(root, cmyk_doc,
                                write_icc_color=bool(self.options.io_icc_color))
        self._ensure_namespace()
        inkex.utils.debug(
            "CMYK Import: {} element(s) restored\n"
            "  Method: {}\n"
            "  Source: {}".format(
                n, cmyk_doc.import_method,
                src_path or "(current document)"
            )
        )

    def _io_autosave(self):
        root     = self.document.getroot()
        icc_path = get_fogra39_icc_path() or get_srgb_icc_path()
        hook     = CmykAutoSave(root, ICC_PROFILE_NAME, icc_path)
        n        = hook()
        if n:
            inkex.utils.debug(
                "Auto-sync: {} element(s) updated.\n"
                "  <metadata> JSON and icc-color() paint values refreshed.".format(n)
            )
        else:
            inkex.utils.debug("Auto-sync: no CMYK-annotated elements found.")

    def _io_validate(self):
        import tempfile, os as _os
        cmyk_doc_a = self._io_collect_doc()
        if not cmyk_doc_a.elements:
            inkex.utils.debug("No CMYK data to validate.")
            return
        with tempfile.NamedTemporaryFile(suffix=".svg", delete=False) as tmp:
            tmp_path = tmp.name
        try:
            write_cmyk_svg(self.document, cmyk_doc_a, tmp_path,
                           embed_icc=True, write_icc_color=True)
            from lxml import etree
            cmyk_doc_b = read_cmyk_svg(etree.parse(tmp_path))
            diffs = diff_cmyk_documents(cmyk_doc_a, cmyk_doc_b)
            if not diffs:
                inkex.utils.debug(
                    "Round-trip PASSED - {} element(s) match exactly.".format(
                        len(cmyk_doc_a.elements)))
            else:
                inkex.utils.debug(
                    "Round-trip DIFFERENCES:\n" +
                    "\n".join("  " + d for d in diffs))
        finally:
            try: _os.unlink(tmp_path)
            except OSError: pass

    def _io_strip(self):
        root    = self.document.getroot()
        cleared = 0
        attrs_to_clear = [
            ATTR_C, ATTR_M, ATTR_Y, ATTR_K, ATTR_TARGET,
            ATTR_SPOT_NAME, ATTR_OVERPRINT_FILL, ATTR_OVERPRINT_STROKE,
            ATTR_INK_TOTAL, ATTR_GRAD_STOPS, ATTR_PATTERN,
            ATTR_UUID, ATTR_KNOCKOUT, ATTR_OVERPRINT_MODE,
        ]
        for node in root.iter():
            changed = False
            for attr in attrs_to_clear:
                if node.get(attr) is not None:
                    del node.attrib[attr]
                    changed = True
            style_str = node.get("style","")
            if "icc-color(" in style_str:
                from cmyk_io import parse_style_string, build_style_string, strip_icc_color
                props = parse_style_string(style_str)
                for prop in ("fill","stroke"):
                    if prop in props:
                        props[prop] = strip_icc_color(props[prop])
                node.set("style", build_style_string(props))
                changed = True
            if changed:
                cleared += 1
        meta_el = root.find(f"{{{SVG_NS}}}metadata")
        if meta_el is not None:
            for old in list(meta_el):
                if old.get("id") in (METADATA_CMYK_ID, METADATA_VERSION_ID):
                    meta_el.remove(old)
        defs = root.find(f"{{{SVG_NS}}}defs")
        if defs is not None:
            for cp in list(defs):
                tag = cp.tag.split("}")[-1] if "}" in cp.tag else cp.tag
                if tag == "color-profile" and cp.get("id") == "cmyk-icc-profile":
                    defs.remove(cp)
                if cp.tag == f"{{{CMYK_NS}}}gradient":
                    defs.remove(cp)
        if root.get("color-profile"):
            del root.attrib["color-profile"]
        inkex.utils.debug("Stripped all CMYK data from {} element(s).".format(cleared))

    def _io_desync_check(self):
        """Find elements where the display RGB has drifted from stored CMYK."""
        if not _IO_AVAILABLE:
            return
        root    = self.document.getroot()
        results = find_desynced_elements(root.iter())
        if not results:
            inkex.utils.debug("Desync check PASSED - all elements in sync.")
            return
        lines = ["CMYK/RGB desync found in {} element(s):".format(len(results))]
        for r in results:
            c,m,y,k = r["cmyk"]
            lines.append(
                "  {}: expected {} (C={:.0f}% M={:.0f}% Y={:.0f}% K={:.0f}%)"
                " but displays {}".format(
                    r["id"], r["expected_rgb"],
                    c*100, m*100, y*100, k*100,
                    r["current_rgb"]
                )
            )
        lines.append("\nRun Preflight to flag these, or re-apply CMYK colours.")
        inkex.utils.debug("\n".join(lines))

    # =======================================================================
    # Shared utilities
    # =======================================================================

    def _get_or_create_defs(self):
        root = self.document.getroot()
        defs = root.find(f"{{{SVG_NS}}}defs")
        if defs is None:
            defs = inkex.etree.SubElement(root, f"{{{SVG_NS}}}defs")
        return defs

    def _unique_id(self, prefix: str) -> str:
        existing = {el.get("id") for el in self.document.getroot().iter() if el.get("id")}
        i = 1
        while f"{prefix}{i}" in existing:
            i += 1
        return f"{prefix}{i}"

    def _ensure_namespace(self):
        root   = self.document.getroot()
        ns_key = f"xmlns:{CMYK_PREFIX}"
        if root.get(ns_key) is None:
            root.set(ns_key, CMYK_NS)

    def _find_ghostscript(self) -> Optional[str]:
        custom = self.options.gs_path.strip()
        if custom and os.path.isfile(custom): return custom
        for exe in ("gs","gswin64c","gswin32c","gsc"):
            try:
                r = subprocess.run([exe,"--version"], capture_output=True, timeout=5)
                if r.returncode == 0: return exe
            except (FileNotFoundError, subprocess.TimeoutExpired): pass
        for path in (r"C:\Program Files\gs\gs10.03.1\bin\gswin64c.exe",
                     r"C:\Program Files\gs\gs10.02.1\bin\gswin64c.exe"):
            if os.path.isfile(path): return path
        return None

    def _find_inkscape(self) -> Optional[str]:
        for exe in ("inkscape",):
            try:
                r = subprocess.run([exe,"--version"], capture_output=True, timeout=5)
                if r.returncode == 0: return exe
            except (FileNotFoundError, subprocess.TimeoutExpired): pass
        for path in (r"C:\Program Files\Inkscape\bin\inkscape.exe",
                     "/Applications/Inkscape.app/Contents/MacOS/inkscape"):
            if os.path.isfile(path): return path
        return None


# ---------------------------------------------------------------------------
if __name__ == "__main__":
    CMYKColor().run()
