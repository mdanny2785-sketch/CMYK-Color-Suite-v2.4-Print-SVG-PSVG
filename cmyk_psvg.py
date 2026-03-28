"""
cmyk_psvg.py  -  Print-SVG (PSVG) Specification v1.0  (v2.4)
=============================================================
Formalises the namespace, schema, and validation rules for
print-safe SVG files.

NAMESPACE
---------
  Primary:   xmlns:psvg="http://printsvg.org/spec/1.0"
  Legacy:    xmlns:cmyk="https://inkscape.org/extensions/cmyk"
             (v2.3 and earlier; read-only support, upgrade on write)

WHAT THIS MODULE PROVIDES
--------------------------
  1. The canonical attribute name set (all psvg:* attributes)
  2. PSVGDocument - top-level document model with full validation
  3. Schema validation - checks a document tree for spec compliance
  4. Version migration - upgrades cmyk:* attrs to psvg:* on load
  5. Transparency detection and flattening advice
  6. Spot colour separation mapping
  7. PDF/X target modes: PDF/X-1a, PDF/X-3, PDF/X-4
  8. Auto-save warning hooks

THE PSVG ATTRIBUTE SCHEMA
--------------------------
Every colour-bearing element MAY carry:

  psvg:c, psvg:m, psvg:y, psvg:k     CMYK 0-1 floats
  psvg:target                          fill | stroke | both
  psvg:ink-total                       float 0-400 (cached)
  psvg:uuid                            UUID4 string
  psvg:spot-name                       canonical spot colour name
  psvg:overprint-fill                  1 | 0
  psvg:overprint-stroke                1 | 0
  psvg:knockout                        auto | on | off
  psvg:overprint-mode                  fill | stroke | both
  psvg:transparency-group              isolated | knockout | passthrough
  psvg:icc-profile                     profile name reference
  psvg:spot-separation                 separated | process | unknown
  psvg:pdfx-target                     pdfx1a | pdfx3 | pdfx4 | none

Document-level (on root <svg>):
  psvg:version                         spec version, currently "1.0"
  psvg:profile                         fogra39 | srgb | custom
  psvg:intent                          perceptual | relative-colorimetric |
                                       saturation | absolute-colorimetric
  psvg:pdfx-mode                       pdfx1a | pdfx3 | pdfx4 | none

No inkex dependency - testable standalone.
"""

from __future__ import annotations

import json
import re
import uuid as _uuid_mod
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

# ---------------------------------------------------------------------------
# Namespaces
# ---------------------------------------------------------------------------
PSVG_NS      = "http://printsvg.org/spec/1.0"
PSVG_PREFIX  = "psvg"
PSVG_VERSION = "1.0"

# Legacy namespace (v2.3 and earlier) - read on import, upgrade on write
CMYK_NS_LEGACY  = "https://inkscape.org/extensions/cmyk"
CMYK_PREFIX_LEGACY = "cmyk"

SVG_NS   = "http://www.w3.org/2000/svg"
XLINK_NS = "http://www.w3.org/1999/xlink"

# ---------------------------------------------------------------------------
# PSVG attribute names
# ---------------------------------------------------------------------------
def _a(name: str) -> str:
    return f"{{{PSVG_NS}}}{name}"

# Colour channels
PSVG_C              = _a("c")
PSVG_M              = _a("m")
PSVG_Y              = _a("y")
PSVG_K              = _a("k")
PSVG_TARGET         = _a("target")
PSVG_INK_TOTAL      = _a("ink-total")
PSVG_UUID           = _a("uuid")
PSVG_ALPHA          = _a("alpha")

# Spot colour
PSVG_SPOT_NAME      = _a("spot-name")
PSVG_SPOT_SEP       = _a("spot-separation")   # separated | process | unknown

# Overprint / knockout
PSVG_OP_FILL        = _a("overprint-fill")
PSVG_OP_STROKE      = _a("overprint-stroke")
PSVG_KNOCKOUT       = _a("knockout")
PSVG_OP_MODE        = _a("overprint-mode")

# Transparency
PSVG_TRANS_GROUP    = _a("transparency-group")  # isolated | knockout | passthrough
PSVG_TRANS_PRESENT  = _a("transparency")        # 1 if element uses opacity/mask/filter

# Gradient
PSVG_GRAD_STOPS     = _a("gradient-stops")
PSVG_GRAD_REF       = _a("gradient-ref")

# Pattern
PSVG_PATTERN        = _a("pattern-colors")

# ICC
PSVG_ICC_PROFILE    = _a("icc-profile")
PSVG_ICC_HREF       = _a("icc-href")

# PDF/X targeting
PSVG_PDFX_TARGET    = _a("pdfx-target")

# Preflight
PSVG_PREFLIGHT_WARN = _a("preflight-warn")
PSVG_DESYNC_WARN    = _a("desync-warn")

# Document-level (on root <svg>)
PSVG_DOC_VERSION    = _a("version")
PSVG_DOC_PROFILE    = _a("profile")
PSVG_DOC_INTENT     = _a("rendering-intent")
PSVG_DOC_PDFX_MODE  = _a("pdfx-mode")
PSVG_DOC_BLEED_MM   = _a("bleed-mm")

# Metadata
PSVG_META_ID        = "psvg-data"
PSVG_META_VER_ID    = "psvg-version"

# Legacy attribute mapping: cmyk:* -> psvg:*
LEGACY_ATTR_MAP: Dict[str, str] = {
    f"{{{CMYK_NS_LEGACY}}}c":               PSVG_C,
    f"{{{CMYK_NS_LEGACY}}}m":               PSVG_M,
    f"{{{CMYK_NS_LEGACY}}}y":               PSVG_Y,
    f"{{{CMYK_NS_LEGACY}}}k":               PSVG_K,
    f"{{{CMYK_NS_LEGACY}}}target":          PSVG_TARGET,
    f"{{{CMYK_NS_LEGACY}}}ink-total":       PSVG_INK_TOTAL,
    f"{{{CMYK_NS_LEGACY}}}uuid":            PSVG_UUID,
    f"{{{CMYK_NS_LEGACY}}}spot-name":       PSVG_SPOT_NAME,
    f"{{{CMYK_NS_LEGACY}}}overprint-fill":  PSVG_OP_FILL,
    f"{{{CMYK_NS_LEGACY}}}overprint-stroke":PSVG_OP_STROKE,
    f"{{{CMYK_NS_LEGACY}}}knockout":        PSVG_KNOCKOUT,
    f"{{{CMYK_NS_LEGACY}}}overprint-mode":  PSVG_OP_MODE,
    f"{{{CMYK_NS_LEGACY}}}gradient-stops":  PSVG_GRAD_STOPS,
    f"{{{CMYK_NS_LEGACY}}}gradient-ref":    PSVG_GRAD_REF,
    f"{{{CMYK_NS_LEGACY}}}pattern-colors":  PSVG_PATTERN,
    f"{{{CMYK_NS_LEGACY}}}icc-href":        PSVG_ICC_HREF,
    f"{{{CMYK_NS_LEGACY}}}preflight-warn":  PSVG_PREFLIGHT_WARN,
    f"{{{CMYK_NS_LEGACY}}}desync-warn":     PSVG_DESYNC_WARN,
    f"{{{CMYK_NS_LEGACY}}}sep-channel":     _a("sep-channel"),
    f"{{{CMYK_NS_LEGACY}}}trap-pairs":      _a("trap-pairs"),
    f"{{{CMYK_NS_LEGACY}}}heatmap-ink":     _a("heatmap-ink"),
    f"{{{CMYK_NS_LEGACY}}}orig-bytes":      _a("orig-bytes"),
}


# ===========================================================================
# SECTION 1 - PDF/X TARGET MODES
# ===========================================================================

class PDFXMode:
    NONE    = "none"
    X1A     = "pdfx1a"
    X3      = "pdfx3"
    X4      = "pdfx4"

    DESCRIPTIONS = {
        NONE: "No PDF/X conformance",
        X1A:  "PDF/X-1a:2003 — flattened transparency, CMYK/spot only, "
              "embedded ICC output intent",
        X3:   "PDF/X-3:2003 — device-independent colour allowed, "
              "embedded ICC required",
        X4:   "PDF/X-4:2010 — live transparency, ICC, layers preserved",
    }

    @classmethod
    def ghostscript_args(cls, mode: str) -> List[str]:
        """Return Ghostscript args to target a PDF/X conformance level."""
        base = [
            "-dNOPAUSE", "-dBATCH", "-dSAFER",
            "-sDEVICE=pdfwrite",
            "-sColorConversionStrategy=CMYK",
            "-dProcessColorModel=/DeviceCMYK",
            "-dCompressFonts=true", "-dSubsetFonts=true",
            "-dEmbedAllFonts=true",
            "-dColorImageResolution=300",
            "-dGrayImageResolution=300",
            "-dCompressPages=true", "-dOptimize=true",
        ]
        if mode == cls.X1A:
            return base + [
                "-dCompatibilityLevel=1.3",   # PDF 1.3 required for X-1a
                "-dPDFX=true",
                "-sDocumentUUID=",
                "-dFlattenTransparency=true",
                "-dFastWebView=false",
            ]
        elif mode == cls.X3:
            return base + [
                "-dCompatibilityLevel=1.4",
                "-dPDFX=true",
            ]
        elif mode == cls.X4:
            return base + [
                "-dCompatibilityLevel=1.6",
                "-dPDFX=true",
                "-dOverrideICC=true",
                "-dSimulateOverprint=true",
            ]
        else:
            return base + ["-dCompatibilityLevel=1.4"]

    @classmethod
    def requires_flattening(cls, mode: str) -> bool:
        """PDF/X-1a requires transparency flattening; X-3 and X-4 do not."""
        return mode == cls.X1A


# ===========================================================================
# SECTION 2 - TRANSPARENCY DETECTION AND FLATTENING
# ===========================================================================

# CSS/SVG properties that introduce transparency or compositing
TRANSPARENCY_PROPS = frozenset([
    "opacity", "fill-opacity", "stroke-opacity", "stop-opacity",
    "mix-blend-mode", "isolation", "mask", "filter",
    "clip-path", "enable-background",
])

# SVG elements that introduce transparency
TRANSPARENCY_TAGS = frozenset([
    "mask", "filter", "feBlend", "feComposite",
    "feColorMatrix", "feMerge",
])

# Blend modes that affect compositing
BLEND_MODES = frozenset([
    "multiply", "screen", "overlay", "darken", "lighten",
    "color-dodge", "color-burn", "hard-light", "soft-light",
    "difference", "exclusion", "hue", "saturation", "color", "luminosity",
])


@dataclass
class TransparencyInfo:
    """Transparency analysis for one element."""
    element_id:    str
    has_opacity:   bool  = False   # opacity < 1 (not fill/stroke-opacity)
    has_blend:     bool  = False   # mix-blend-mode != normal
    has_mask:      bool  = False   # mask attribute
    has_filter:    bool  = False   # filter attribute
    opacity_value: float = 1.0
    blend_mode:    str   = "normal"

    @property
    def has_transparency(self) -> bool:
        return self.has_opacity or self.has_blend or self.has_mask or self.has_filter

    @property
    def severity(self) -> str:
        """'error' if problematic for PDF/X-1a, 'warning' otherwise."""
        if self.has_opacity or self.has_blend or self.has_mask:
            return "error"
        return "warning"

    def flattening_advice(self) -> str:
        parts = []
        if self.has_opacity:
            parts.append(
                f"opacity:{self.opacity_value:.2f} — "
                "rasterise to CMYK at 300dpi or merge with background"
            )
        if self.has_blend:
            parts.append(
                f"mix-blend-mode:{self.blend_mode} — "
                "flatten to DeviceCMYK before PDF/X-1a export"
            )
        if self.has_mask:
            parts.append("mask — expand and rasterise before PDF/X-1a export")
        if self.has_filter:
            parts.append("filter — rasterise effect to CMYK bitmap")
        return "; ".join(parts) if parts else "no action needed"


def detect_transparency(iter_elements) -> List[TransparencyInfo]:
    """
    Scan all elements for transparency-introducing properties.
    Returns one TransparencyInfo per affected element.
    """
    results: List[TransparencyInfo] = []
    for el in iter_elements:
        tag = el.tag if isinstance(el.tag, str) else ""
        if not tag.startswith("{"):
            continue
        eid = el.get("id", "")

        # Style-based transparency
        style_str = el.get("style", "")
        style: Dict[str, str] = {}
        for part in style_str.split(";"):
            part = part.strip()
            if ":" in part:
                k, v = part.split(":", 1)
                style[k.strip()] = v.strip()

        info = TransparencyInfo(element_id=eid)

        # Check opacity (whole-element, not fill/stroke-opacity)
        op_str = style.get("opacity", el.get("opacity", ""))
        if op_str:
            try:
                op = float(op_str)
                if op < 0.9999:
                    info.has_opacity   = True
                    info.opacity_value = op
            except ValueError:
                pass

        # Check blend mode
        blend = style.get("mix-blend-mode", "")
        if blend and blend != "normal":
            info.has_blend  = True
            info.blend_mode = blend

        # Check mask
        if el.get("mask") or style.get("mask"):
            info.has_mask = True

        # Check filter
        if el.get("filter") or style.get("filter"):
            info.has_filter = True

        if info.has_transparency:
            results.append(info)

    return results


def build_gs_flatten_args(icc_path: Optional[str] = None) -> List[str]:
    """
    Ghostscript arguments that flatten transparency to CMYK.
    Used as the first pass before PDF/X-1a export.
    """
    args = [
        "-dNOPAUSE", "-dBATCH", "-dSAFER",
        "-sDEVICE=pdfwrite",
        "-dCompatibilityLevel=1.3",      # Force PDF 1.3 = no live transparency
        "-sColorConversionStrategy=CMYK",
        "-dProcessColorModel=/DeviceCMYK",
        "-dFlattenTransparency=true",    # Key flag
        "-dAutoFilterColorImages=false",
        "-dColorImageFilter=/FlateEncode",
        "-dColorImageResolution=300",
        "-dGrayImageResolution=300",
        "-dMonoImageResolution=1200",
        "-dCompressFonts=true",
        "-dSubsetFonts=true",
        "-dEmbedAllFonts=true",
    ]
    if icc_path:
        args.append(f"-sOutputICCProfile={icc_path}")
    return args


# ===========================================================================
# SECTION 3 - SPOT COLOUR SEPARATION MAPPING
# ===========================================================================

@dataclass
class SpotSeparation:
    """Defines how one spot colour maps to output separations."""
    spot_name:       str
    cmyk:            Tuple[float, float, float, float]
    mode:            str    = "separated"  # separated | process | unknown
    plate_name:      str    = ""           # custom plate name for RIP
    density_pct:     float  = 100.0       # nominal density 0-100%
    is_metallic:     bool   = False
    is_fluorescent:  bool   = False
    icc_profile_ref: str    = ""          # profile name for spot-accurate proof

    def to_dict(self) -> Dict:
        return {
            "name":        self.spot_name,
            "cmyk":        list(self.cmyk),
            "mode":        self.mode,
            "plate":       self.plate_name or self.spot_name,
            "density_pct": self.density_pct,
            "metallic":    self.is_metallic,
            "fluorescent": self.is_fluorescent,
        }

    def ghostscript_sep_arg(self) -> str:
        """
        Return the Ghostscript -sSeparationColorNames argument fragment
        for this spot colour. Combine multiple with comma separator.
        """
        return self.plate_name or self.spot_name


@dataclass
class SeparationMap:
    """Complete map of all spot separations in a document."""
    spots:           List[SpotSeparation] = field(default_factory=list)
    process_plates:  List[str]            = field(default_factory=lambda: ["Cyan","Magenta","Yellow","Black"])

    def all_plate_names(self) -> List[str]:
        return self.process_plates + [s.plate_name or s.spot_name
                                       for s in self.spots
                                       if s.mode == "separated"]

    def ghostscript_separation_args(self) -> List[str]:
        """
        Build GS args to output all separations (process + spot).
        Used with -sDEVICE=separations or -sDEVICE=tiffsep.
        """
        names = ",".join(
            f"({s.plate_name or s.spot_name})"
            for s in self.spots
            if s.mode == "separated"
        )
        args = ["-sDEVICE=tiffsep"]
        if names:
            args.append(f"-sSeparationColorNames={names}")
        return args

    def to_scribus_color_defs(self) -> str:
        """Return Scribus SLA <COLORS> block XML for all spots."""
        lines = []
        for s in self.spots:
            c,m,y,k = s.cmyk
            cmyk_hex = "".join(f"{int(v*255):02X}" for v in (c,m,y,k))
            is_spot  = 1 if s.mode == "separated" else 0
            lines.append(
                f'<COLOR Spot="{is_spot}" CMYK="#{cmyk_hex}" '
                f'NAME="{s.spot_name}" />'
            )
        return "\n    ".join(lines)

    def validation_report(self) -> List[str]:
        """Return list of issues with the separation map."""
        issues = []
        seen_names: Dict[str, List] = {}
        for s in self.spots:
            key = s.spot_name.strip().upper()
            seen_names.setdefault(key, []).append(s)
        for name, entries in seen_names.items():
            if len(entries) > 1:
                # Check CMYK consistency
                ref = entries[0].cmyk
                for dup in entries[1:]:
                    diff = max(abs(a-b) for a,b in zip(ref, dup.cmyk))
                    if diff > 0.02:
                        issues.append(
                            f"SPOT_MISMATCH: '{name}' has inconsistent CMYK "
                            f"across {len(entries)} occurrences (max diff {diff*100:.1f}%)"
                        )
        unknown = [s for s in self.spots if s.mode == "unknown"]
        if unknown:
            issues.append(
                f"SPOT_NOT_SEPARATED: {len(unknown)} spot colour(s) have "
                f"mode='unknown': "
                + ", ".join(s.spot_name for s in unknown)
            )
        return issues


def build_separation_map_from_elements(iter_elements,
                                        spot_colors_table: Dict) -> SeparationMap:
    """
    Build a SeparationMap by scanning annotated elements.
    spot_colors_table: name -> (c,m,y,k) from cmyk_core.SPOT_COLORS
    """
    sep_map = SeparationMap()
    seen: Dict[str, SpotSeparation] = {}

    for el in iter_elements:
        spot_name = el.get(PSVG_SPOT_NAME, "") or el.get(
            f"{{{CMYK_NS_LEGACY}}}spot-name", "")
        if not spot_name:
            continue

        c_val = (el.get(PSVG_C) or
                 el.get(f"{{{CMYK_NS_LEGACY}}}c"))
        if c_val is None:
            continue

        c = float(c_val)
        m = float(el.get(PSVG_M) or el.get(f"{{{CMYK_NS_LEGACY}}}m") or 0)
        y = float(el.get(PSVG_Y) or el.get(f"{{{CMYK_NS_LEGACY}}}y") or 0)
        k = float(el.get(PSVG_K) or el.get(f"{{{CMYK_NS_LEGACY}}}k") or 0)

        key = spot_name.strip().upper()
        if key not in seen:
            # Determine separation mode
            in_table = spot_colors_table.get(spot_name.strip())
            mode = "separated" if in_table else "process"
            sep = SpotSeparation(
                spot_name  = spot_name,
                cmyk       = (c, m, y, k),
                mode       = mode,
                plate_name = spot_name,
            )
            seen[key] = sep
            sep_map.spots.append(sep)

    return sep_map


# ===========================================================================
# SECTION 4 - SCHEMA VALIDATION
# ===========================================================================

# Validation error codes
PSVG_ERR_MISSING_VERSION    = "MISSING_PSVG_VERSION"
PSVG_ERR_MISSING_ICC        = "MISSING_ICC_PROFILE"
PSVG_ERR_TRANSPARENCY_X1A   = "TRANSPARENCY_INCOMPATIBLE_X1A"
PSVG_ERR_SPOT_NOT_SEPARATED  = "SPOT_NOT_SEPARATED"
PSVG_ERR_MISSING_UUID       = "MISSING_UUID"
PSVG_ERR_INVALID_PDFX_MODE  = "INVALID_PDFX_MODE"
PSVG_WARN_LEGACY_NAMESPACE  = "LEGACY_CMYK_NAMESPACE"
PSVG_WARN_NO_BLEED          = "NO_BLEED_DEFINED"
PSVG_WARN_UNRESOLVED_SPOT   = "UNRESOLVED_SPOT_NAME"
PSVG_INFO_TRANSPARENCY_X3X4 = "TRANSPARENCY_OK_FOR_X3_X4"


@dataclass
class PSVGValidationIssue:
    code:       str
    severity:   str   # error | warning | info
    element_id: str
    message:    str

    def __str__(self) -> str:
        loc = f" [{self.element_id}]" if self.element_id else ""
        return f"[{self.severity.upper():7}] {self.code}{loc}: {self.message}"


@dataclass
class PSVGValidationReport:
    issues:  List[PSVGValidationIssue] = field(default_factory=list)
    passed:  bool = True

    def add(self, issue: PSVGValidationIssue):
        self.issues.append(issue)
        if issue.severity == "error":
            self.passed = False

    def errors(self)   -> List[PSVGValidationIssue]:
        return [i for i in self.issues if i.severity == "error"]

    def warnings(self) -> List[PSVGValidationIssue]:
        return [i for i in self.issues if i.severity == "warning"]

    def to_text(self) -> str:
        status = "PASS" if self.passed else "FAIL"
        lines  = [f"PSVG Schema Validation: {status}",
                  f"  {len(self.errors())} error(s), "
                  f"{len(self.warnings())} warning(s)",
                  ""]
        for i in self.issues:
            lines.append(str(i))
        return "\n".join(lines)


def validate_psvg_document(get_doc_attr, iter_elements,
                            pdfx_mode: str = PDFXMode.NONE,
                            spot_colors_table: Optional[Dict] = None
                            ) -> PSVGValidationReport:
    """
    Validate a document tree against the PSVG spec.

    Args:
        get_doc_attr:       callable(attr, default) for root SVG attributes
        iter_elements:      iterable of SVG elements
        pdfx_mode:          target PDF/X mode (PDFXMode constant)
        spot_colors_table:  name->cmyk dict from cmyk_core.SPOT_COLORS
    """
    report   = PSVGValidationReport()
    elements = list(iter_elements)

    # Document-level checks
    psvg_ver = get_doc_attr(PSVG_DOC_VERSION, "")
    if not psvg_ver:
        report.add(PSVGValidationIssue(
            PSVG_ERR_MISSING_VERSION, "warning", "",
            "Document does not declare psvg:version. "
            "Add psvg:version=\"1.0\" to the root <svg> element."
        ))

    bleed = get_doc_attr(PSVG_DOC_BLEED_MM, "")
    if not bleed:
        report.add(PSVGValidationIssue(
            PSVG_WARN_NO_BLEED, "warning", "",
            "No psvg:bleed-mm declared. "
            "Standard press requires 3mm bleed on all sides."
        ))

    # Check for legacy cmyk:* namespace usage
    for el in elements:
        for attr in el.attrib:
            if attr.startswith(f"{{{CMYK_NS_LEGACY}}}"):
                report.add(PSVGValidationIssue(
                    PSVG_WARN_LEGACY_NAMESPACE, "warning", el.get("id",""),
                    "Element uses legacy cmyk:* namespace. "
                    "Migrate to psvg:* with the Upgrade action."
                ))
                break  # one warning per element is enough

    # PDF/X-1a specific checks
    if pdfx_mode == PDFXMode.X1A:
        for el in elements:
            style_str = el.get("style","")
            eid       = el.get("id","")
            for part in style_str.split(";"):
                part = part.strip()
                if ":" not in part:
                    continue
                prop, val = part.split(":",1)
                prop = prop.strip()
                if prop == "opacity":
                    try:
                        if float(val.strip()) < 0.9999:
                            report.add(PSVGValidationIssue(
                                PSVG_ERR_TRANSPARENCY_X1A, "error", eid,
                                f"opacity:{val.strip()} is incompatible with PDF/X-1a. "
                                "Flatten transparency before export."
                            ))
                    except ValueError:
                        pass
                if prop == "mix-blend-mode" and val.strip() not in ("normal",""):
                    report.add(PSVGValidationIssue(
                        PSVG_ERR_TRANSPARENCY_X1A, "error", eid,
                        f"mix-blend-mode:{val.strip()} is incompatible with PDF/X-1a. "
                        "Flatten transparency before export."
                    ))

    # Spot colour validation
    if spot_colors_table:
        spot_registry: Dict[str, Tuple] = {}
        for el in elements:
            spot = (el.get(PSVG_SPOT_NAME) or
                    el.get(f"{{{CMYK_NS_LEGACY}}}spot-name",""))
            if not spot:
                continue
            eid    = el.get("id","")
            in_tbl = spot_colors_table.get(spot.strip())
            if not in_tbl:
                report.add(PSVGValidationIssue(
                    PSVG_WARN_UNRESOLVED_SPOT, "warning", eid,
                    f"Spot colour '{spot}' is not in the known spot table. "
                    "Verify name and CMYK values for accurate separation."
                ))

    # UUID check
    missing_uuid = []
    for el in elements:
        c_val = (el.get(PSVG_C) or el.get(f"{{{CMYK_NS_LEGACY}}}c"))
        if c_val is None:
            continue
        has_uuid = (el.get(PSVG_UUID) or el.get(f"{{{CMYK_NS_LEGACY}}}uuid",""))
        if not has_uuid:
            missing_uuid.append(el.get("id",""))

    if missing_uuid:
        report.add(PSVGValidationIssue(
            PSVG_ERR_MISSING_UUID, "warning", "",
            f"{len(missing_uuid)} CMYK-annotated element(s) missing psvg:uuid. "
            "Run Export CMYK SVG to assign UUIDs."
        ))

    return report


# ===========================================================================
# SECTION 5 - VERSION MIGRATION  (cmyk:* -> psvg:*)
# ===========================================================================

def migrate_cmyk_to_psvg(root) -> int:
    """
    Migrate all cmyk:* attributes on a document tree to psvg:* equivalents.
    Returns count of elements migrated.
    """
    migrated = 0
    for el in root.iter():
        changed   = False
        to_delete = []
        to_set    = []

        for attr, value in list(el.attrib.items()):
            new_attr = LEGACY_ATTR_MAP.get(attr)
            if new_attr:
                to_set.append((new_attr, value))
                to_delete.append(attr)
                changed = True

        for attr in to_delete:
            del el.attrib[attr]
        for attr, val in to_set:
            el.set(attr, val)

        if changed:
            migrated += 1

    # Register new namespace on root
    try:
        from lxml import etree
        etree.register_namespace(PSVG_PREFIX, PSVG_NS)
    except (ImportError, AttributeError):
        pass

    return migrated


# ===========================================================================
# SECTION 6 - DOCUMENT MODEL
# ===========================================================================

@dataclass
class PSVGDocument:
    """
    Top-level Print-SVG document model.
    Wraps CmykDocument with spec-level metadata.
    """
    version:         str = PSVG_VERSION
    profile:         str = "fogra39"           # fogra39 | srgb | custom
    rendering_intent:str = "relative-colorimetric"
    pdfx_mode:       str = PDFXMode.NONE
    bleed_mm:        float = 3.0
    icc_path:        Optional[str] = None
    separation_map:  Optional[SeparationMap] = None
    transparency:    List[TransparencyInfo] = field(default_factory=list)
    validation:      Optional[PSVGValidationReport] = None

    def is_x1a_compliant(self) -> bool:
        if not self.validation:
            return False
        return (self.pdfx_mode == PDFXMode.X1A and
                self.validation.passed and
                not self.transparency)

    def export_summary(self) -> str:
        lines = [
            f"PSVG Document v{self.version}",
            f"  Profile:     {self.profile}",
            f"  Intent:      {self.rendering_intent}",
            f"  PDF/X mode:  {PDFXMode.DESCRIPTIONS.get(self.pdfx_mode, self.pdfx_mode)}",
            f"  Bleed:       {self.bleed_mm}mm",
            f"  ICC:         {self.icc_path or 'not set'}",
        ]
        if self.separation_map:
            lines.append(
                f"  Plates:      "
                + ", ".join(self.separation_map.all_plate_names())
            )
        if self.transparency:
            lines.append(
                f"  Transparency:{len(self.transparency)} element(s) "
                "(check for PDF/X-1a compliance)"
            )
        if self.validation:
            lines.append(
                f"  Validation:  {'PASS' if self.validation.passed else 'FAIL'} "
                f"({len(self.validation.errors())} error(s))"
            )
        return "\n".join(lines)


# ===========================================================================
# SECTION 7 - SPEC DOCUMENT WRITER
# ===========================================================================

PSVG_SPEC_TEXT = """Print-SVG Specification v1.0
============================

Namespace
---------
  xmlns:psvg="http://printsvg.org/spec/1.0"

Purpose
-------
  Print-SVG (PSVG) extends SVG with a standardised schema for carrying
  CMYK colour data, spot separations, overprint intent, and PDF/X metadata
  through the SVG format without conflicts with rendering tools.

  The schema is designed to be:
    - Additive: existing SVG renders correctly without PSVG awareness
    - Redundant: icc-color() provides ICC-standard fallback
    - Versioned: psvg:version enables future migration
    - Tool-agnostic: usable from any SVG editor

Element-level attributes
------------------------
  psvg:c, psvg:m, psvg:y, psvg:k       CMYK channel values, 0.0-1.0
  psvg:target                            fill | stroke | both
  psvg:ink-total                         C+M+Y+K * 100, 0-400
  psvg:uuid                              UUID4, stable element identity
  psvg:alpha                             opacity, 0.0-1.0
  psvg:spot-name                         canonical spot colour name
  psvg:spot-separation                   separated | process | unknown
  psvg:overprint-fill                    1 | 0
  psvg:overprint-stroke                  1 | 0
  psvg:knockout                          auto | on | off
  psvg:overprint-mode                    fill | stroke | both
  psvg:transparency-group                isolated | knockout | passthrough
  psvg:transparency                      1 if element uses opacity/blend/mask
  psvg:gradient-stops                    JSON: [{offset,c,m,y,k}, ...]
  psvg:gradient-ref                      id of linked <psvg:gradient> in <defs>
  psvg:pattern-colors                    JSON: [{i,prop,c,m,y,k,hex}, ...]
  psvg:icc-profile                       profile name reference
  psvg:pdfx-target                       pdfx1a | pdfx3 | pdfx4 | none
  psvg:preflight-warn                    JSON: ["CODE1", "CODE2", ...]
  psvg:desync-warn                       1 if RGB display drifted from CMYK

Document-level attributes (on root <svg>)
------------------------------------------
  psvg:version                           spec version, currently "1.0"
  psvg:profile                           fogra39 | srgb | custom
  psvg:rendering-intent                  perceptual | relative-colorimetric |
                                         saturation | absolute-colorimetric
  psvg:pdfx-mode                         pdfx1a | pdfx3 | pdfx4 | none
  psvg:bleed-mm                          bleed size in mm (default 3.0)

Gradient definition (in <defs>)
--------------------------------
  <psvg:gradient id="psvg-grad1" linked-gradient="svgGrad1">
    <psvg:stop offset="0.0" c="0" m="0" y="0" k="1" profile="cmyk-icc"/>
    <psvg:stop offset="1.0" c="0" m="0" y="0" k="0" profile="cmyk-icc"/>
  </psvg:gradient>

Metadata JSON schema
--------------------
  <metadata>
    <psvg:data id="psvg-data">
      [{"id":"...", "uuid":"...", "c":0.0, "m":0.0, "y":0.0, "k":0.0,
        "target":"fill", "spot":"...", "op_fill":"0", "op_stroke":"0",
        "knockout":"auto", "grad_stops":[...], "pattern":[...]}, ...]
    </psvg:data>
    <psvg:version id="psvg-version">1.0</psvg:version>
  </metadata>

Compliance levels
-----------------
  Basic:     psvg:c/m/y/k + psvg:uuid on all colour elements
  Standard:  Basic + icc-color() paint + <metadata> JSON + ICC profile
  Full:      Standard + spot separations + PDF/X target + transparency map

Legacy compatibility
--------------------
  Files using xmlns:cmyk="https://inkscape.org/extensions/cmyk" (v2.3 and
  earlier) are read transparently. The migration function upgrades cmyk:*
  attributes to psvg:* on write. Both namespaces are accepted during import.

PDF/X conformance
-----------------
  PDF/X-1a: transparency must be flattened; CMYK/spot only; ICC output intent
  PDF/X-3:  device-independent colour allowed; ICC required
  PDF/X-4:  live transparency preserved; ICC; optional layers

Versioning
----------
  psvg:version="1.0" — this specification
  Future versions will maintain backward read compatibility.
"""


def write_spec_document(output_path: str) -> None:
    """Write the PSVG specification as a plain-text file."""
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(PSVG_SPEC_TEXT)
