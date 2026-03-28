"""
cmyk_core.py  –  Shared colour math, ICC helpers, and SVG utilities  (v2.4)
============================================================================
New in v2.2
-----------
  SEPARATIONS  – channel_to_greyscale(), separation_style(),
                 spot_coverage_style(), SeparationPlate dataclass,
                 four_up_transforms(), separation_plates_for_document().
                 Full non-destructive plate preview architecture.

  TRAPPING     – shares_ink_channel(), trap_needed(), lighter_cmyk(),
                 trap_stroke_style(), TrapPair, TrapReport,
                 find_trap_pairs().  Industry-standard 0.25pt spread trap.

  PATTERNS     – build_pattern_cmyk_metadata(), parse_pattern_cmyk_metadata(),
                 pattern_ink_total().  Enumerates <pattern> tile colours
                 and stores as cmyk:pattern-colors JSON blob.

  INK HEATMAP  – ink_heatmap_color(), ink_heatmap_hex(), ink_heatmap_style().
                 Maps total ink % to a green→amber→red RGB ramp for
                 document-wide visual ink density overlay.

Sections 1-7 unchanged from v2.1 (colour math, ICC, metadata, spots,
overprint, preflight, compression).

No inkex dependency — fully testable standalone.
"""

from __future__ import annotations

import gzip
import io
import json
import math
import os
import re
import struct
import zlib
from dataclasses import dataclass, field
from typing import Dict, Iterator, List, Optional, Tuple, Union

# ---------------------------------------------------------------------------
# Type aliases
# ---------------------------------------------------------------------------
RGB  = Tuple[int, int, int]
CMYK = Tuple[float, float, float, float]
RGBA = Tuple[int, int, int, int]

# ---------------------------------------------------------------------------
# CMYK namespace
# ---------------------------------------------------------------------------
CMYK_NS     = "https://inkscape.org/extensions/cmyk"
CMYK_PREFIX = "cmyk"

ATTR_C         = f"{{{CMYK_NS}}}c"
ATTR_M         = f"{{{CMYK_NS}}}m"
ATTR_Y         = f"{{{CMYK_NS}}}y"
ATTR_K         = f"{{{CMYK_NS}}}k"
ATTR_TARGET    = f"{{{CMYK_NS}}}target"
ATTR_SPOT_NAME = f"{{{CMYK_NS}}}spot-name"
ATTR_ICC_HREF  = f"{{{CMYK_NS}}}icc-href"
ATTR_GRAD_STOPS= f"{{{CMYK_NS}}}gradient-stops"
ATTR_PATTERN   = f"{{{CMYK_NS}}}pattern-colors"

# v2.1 attributes
ATTR_OVERPRINT_FILL   = f"{{{CMYK_NS}}}overprint-fill"
ATTR_OVERPRINT_STROKE = f"{{{CMYK_NS}}}overprint-stroke"
ATTR_INK_TOTAL        = f"{{{CMYK_NS}}}ink-total"
ATTR_PREFLIGHT_WARN   = f"{{{CMYK_NS}}}preflight-warn"
ATTR_COMP_ORIG_BYTES  = f"{{{CMYK_NS}}}orig-bytes"

# v2.2 new attributes
ATTR_SEP_CHANNEL = f"{{{CMYK_NS}}}sep-channel"   # active sep channel: c|m|y|k|spot
ATTR_TRAP_PAIRS  = f"{{{CMYK_NS}}}trap-pairs"    # JSON list of trap pair IDs
ATTR_HEATMAP_INK = f"{{{CMYK_NS}}}heatmap-ink"  # float, ink% on heatmap clone


# ===========================================================================
# SECTION 1 – COLOUR MATH
# ===========================================================================

def cmyk_to_rgb(c: float, m: float, y: float, k: float) -> RGB:
    """CMYK (0-1) -> RGB (0-255). Standard ICC DeviceCMYK formula."""
    r = 255.0 * (1.0 - c) * (1.0 - k)
    g = 255.0 * (1.0 - m) * (1.0 - k)
    b = 255.0 * (1.0 - y) * (1.0 - k)
    return int(round(r)), int(round(g)), int(round(b))


def rgb_to_cmyk(r: int, g: int, b: int) -> CMYK:
    """RGB (0-255) -> CMYK (0-1). Maximum K under-colour removal."""
    if r == 0 and g == 0 and b == 0:
        return 0.0, 0.0, 0.0, 1.0
    rp, gp, bp = r / 255.0, g / 255.0, b / 255.0
    k = 1.0 - max(rp, gp, bp)
    if k >= 1.0:
        return 0.0, 0.0, 0.0, 1.0
    d = 1.0 - k
    c = (1.0 - rp - k) / d
    m = (1.0 - gp - k) / d
    y = (1.0 - bp - k) / d
    return _clamp01(c), _clamp01(m), _clamp01(y), _clamp01(k)


def _clamp01(v: float) -> float:
    return max(0.0, min(1.0, v))


def hex_to_rgb(hex_color: str) -> RGB:
    """'#rrggbb' or '#rgb' -> (r, g, b). Returns (0,0,0) on error."""
    try:
        h = hex_color.lstrip("#")
        if len(h) == 3:
            h = h[0]*2 + h[1]*2 + h[2]*2
        if len(h) != 6:
            return 0, 0, 0
        return (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16))
    except (ValueError, AttributeError):
        return 0, 0, 0


def rgb_to_hex(r: int, g: int, b: int) -> str:
    return "#{:02x}{:02x}{:02x}".format(int(r), int(g), int(b))


def cmyk_to_hex(c: float, m: float, y: float, k: float) -> str:
    return rgb_to_hex(*cmyk_to_rgb(c, m, y, k))


def clamp_percent(value, name: str = "") -> float:
    return max(0.0, min(100.0, float(value)))


def ink_total(c: float, m: float, y: float, k: float) -> float:
    """Return total ink coverage as 0-400% value."""
    return (c + m + y + k) * 100.0


def ink_total_from_rgb(r: int, g: int, b: int) -> float:
    return ink_total(*rgb_to_cmyk(r, g, b))


def parse_gradient_stop_color(stop_style: str) -> Optional[RGB]:
    for part in stop_style.split(";"):
        part = part.strip()
        if part.startswith("stop-color:"):
            val = part[len("stop-color:"):].strip()
            if val.startswith("#"):
                return hex_to_rgb(val)
            if val.startswith("rgb("):
                try:
                    nums = val[4:-1].split(",")
                    return tuple(int(x.strip()) for x in nums)
                except Exception:
                    pass
    return None


def build_stop_style(r: int, g: int, b: int, opacity: float, existing: str) -> str:
    hex_c = rgb_to_hex(r, g, b)
    parts: Dict[str, str] = {}
    for part in existing.split(";"):
        part = part.strip()
        if ":" in part:
            pk, pv = part.split(":", 1)
            parts[pk.strip()] = pv.strip()
    parts["stop-color"]   = hex_c
    parts["stop-opacity"] = f"{opacity:.6f}"
    return ";".join(f"{k}:{v}" for k, v in parts.items())


# ===========================================================================
# SECTION 2 – ICC PROFILE HELPERS
# ===========================================================================

def get_fogra39_icc_path() -> Optional[str]:
    candidates = [
        "/usr/share/color/icc/colord/Fogra39L.icc",
        "/usr/share/color/icc/Fogra39L.icc",
        "/usr/share/ghostscript/icc/fogra39.icc",
        "/Library/ColorSync/Profiles/Recommended/ISOcoated_v2_eci.icc",
        "/Library/Application Support/Adobe/Color/Profiles/Recommended/ISOcoated_v2_eci.icc",
        r"C:\Windows\System32\spool\drivers\color\ISOcoated_v2_eci.icc",
        r"C:\Program Files\Common Files\Adobe\Color\Profiles\Recommended\ISOcoated_v2_eci.icc",
        os.path.expanduser("~/.color/icc/ISOcoated_v2_eci.icc"),
        os.path.expanduser("~/Library/ColorSync/Profiles/ISOcoated_v2_eci.icc"),
    ]
    for p in candidates:
        if os.path.isfile(p):
            return p
    return None


def get_srgb_icc_path() -> Optional[str]:
    candidates = [
        "/usr/share/color/icc/colord/sRGB.icc",
        "/usr/share/color/icc/sRGB.icc",
        "/System/Library/ColorSync/Profiles/sRGB Profile.icc",
        r"C:\Windows\System32\spool\drivers\color\sRGB Color Space Profile.icm",
        os.path.expanduser("~/.color/icc/sRGB.icc"),
    ]
    for p in candidates:
        if os.path.isfile(p):
            return p
    return None


def icc_profile_to_data_uri(path: str) -> Optional[str]:
    try:
        import base64
        with open(path, "rb") as f:
            data = f.read()
        b64 = base64.b64encode(data).decode("ascii")
        return f"data:application/vnd.iccprofile;base64,{b64}"
    except Exception:
        return None


# ===========================================================================
# SECTION 3 – PLAIN-SVG METADATA
# ===========================================================================

METADATA_CMYK_ID   = "cmyk-plugin-data"
PLUGIN_VERSION     = "2.4"
METADATA_VERSION_ID = "cmyk-plugin-version"


def encode_cmyk_metadata(elements_data: List[Dict]) -> str:
    return json.dumps(elements_data, separators=(",", ":"))


def decode_cmyk_metadata(json_str: str) -> List[Dict]:
    try:
        return json.loads(json_str)
    except Exception:
        return []


# ===========================================================================
# SECTION 4 – SPOT COLOURS
# ===========================================================================

SPOT_COLORS: Dict[str, CMYK] = {
    "PANTONE PROCESS CYAN C":    (1.00, 0.00, 0.00, 0.00),
    "PANTONE PROCESS MAGENTA C": (0.00, 1.00, 0.00, 0.00),
    "PANTONE PROCESS YELLOW C":  (0.00, 0.00, 1.00, 0.00),
    "PANTONE BLACK C":           (0.00, 0.00, 0.00, 1.00),
    "PANTONE WHITE":             (0.00, 0.00, 0.00, 0.00),
    "PANTONE 485 C":    (0.00, 0.95, 1.00, 0.00),
    "PANTONE 032 C":    (0.00, 0.91, 0.87, 0.00),
    "PANTONE 021 C":    (0.00, 0.55, 1.00, 0.00),
    "PANTONE 1235 C":   (0.00, 0.30, 0.90, 0.00),
    "PANTONE 1665 C":   (0.00, 0.68, 1.00, 0.00),
    "PANTONE 116 C":    (0.00, 0.16, 1.00, 0.00),
    "PANTONE 012 C":    (0.00, 0.09, 1.00, 0.00),
    "PANTONE 109 C":    (0.00, 0.07, 1.00, 0.00),
    "PANTONE 354 C":    (0.85, 0.00, 0.97, 0.00),
    "PANTONE 362 C":    (0.75, 0.00, 1.00, 0.00),
    "PANTONE 375 C":    (0.48, 0.00, 1.00, 0.00),
    "PANTONE 347 C":    (1.00, 0.00, 0.87, 0.08),
    "PANTONE 286 C":    (1.00, 0.75, 0.00, 0.02),
    "PANTONE 2925 C":   (0.79, 0.23, 0.00, 0.00),
    "PANTONE 3005 C":   (1.00, 0.35, 0.00, 0.00),
    "PANTONE REFLEX BLUE C": (1.00, 0.80, 0.00, 0.06),
    "PANTONE 2728 C":   (0.90, 0.68, 0.00, 0.00),
    "PANTONE 300 C":    (1.00, 0.50, 0.00, 0.05),
    "PANTONE 2685 C":   (0.92, 1.00, 0.00, 0.03),
    "PANTONE 2593 C":   (0.52, 0.90, 0.00, 0.00),
    "PANTONE 266 C":    (0.77, 0.88, 0.00, 0.00),
    "PANTONE 877 C":    (0.00, 0.00, 0.00, 0.40),
    "PANTONE 872 C":    (0.00, 0.15, 0.60, 0.25),
    "PANTONE 7547 C":   (0.77, 0.56, 0.29, 0.74),
    "PANTONE COOL GRAY 11 C": (0.00, 0.00, 0.00, 0.70),
    "PANTONE COOL GRAY 5 C":  (0.00, 0.00, 0.00, 0.30),
    "PANTONE WARM GRAY 11 C": (0.04, 0.08, 0.12, 0.58),
}


def spot_to_cmyk(name: str) -> Optional[CMYK]:
    return SPOT_COLORS.get(name.strip()) or SPOT_COLORS.get(name.upper().strip())


def list_spot_names() -> List[str]:
    return sorted(SPOT_COLORS.keys())


# ===========================================================================
# SECTION 5 – OVERPRINT
# ===========================================================================

OVERPRINT_BLEND_MODE = "multiply"
RICH_BLACK_THRESHOLD = {"c": 0.50, "m": 0.40, "y": 0.40, "k": 1.00}


@dataclass
class OverprintState:
    fill_overprint:   bool  = False
    stroke_overprint: bool  = False
    ink_total:        float = 0.0

    def to_dict(self) -> Dict:
        return {"fill_overprint":   self.fill_overprint,
                "stroke_overprint": self.stroke_overprint,
                "ink_total":        round(self.ink_total, 2)}

    @classmethod
    def from_element_attrs(cls, get_fn) -> "OverprintState":
        fp = get_fn(ATTR_OVERPRINT_FILL,   "0") == "1"
        sp = get_fn(ATTR_OVERPRINT_STROKE, "0") == "1"
        it = float(get_fn(ATTR_INK_TOTAL,  "0") or "0")
        return cls(fill_overprint=fp, stroke_overprint=sp, ink_total=it)


def apply_overprint_style(existing_style: str, fill_overprint: bool,
                          stroke_overprint: bool,
                          preview_mode: bool = True) -> str:
    parts: Dict[str, str] = {}
    for part in existing_style.split(";"):
        part = part.strip()
        if ":" in part:
            k, v = part.split(":", 1)
            parts[k.strip()] = v.strip()
    wants_blend = preview_mode and (fill_overprint or stroke_overprint)
    if wants_blend:
        parts["mix-blend-mode"] = OVERPRINT_BLEND_MODE
        parts["isolation"]      = "auto"
    else:
        parts.pop("mix-blend-mode", None)
        parts.pop("isolation",      None)
    return ";".join(f"{k}:{v}" for k, v in parts.items() if v)


def composite_overprint(bottom: CMYK, top: CMYK,
                        top_fill_overprint: bool,
                        top_stroke_overprint: bool,
                        is_fill: bool) -> CMYK:
    overprint_active = (is_fill and top_fill_overprint) or \
                       (not is_fill and top_stroke_overprint)
    if not overprint_active:
        return top
    bc, bm, by, bk = bottom
    tc, tm, ty, tk = top
    rc = tc if tc > 0 else bc
    rm = tm if tm > 0 else bm
    ry = ty if ty > 0 else by
    rk = tk if tk > 0 else bk
    return _clamp01(rc), _clamp01(rm), _clamp01(ry), _clamp01(rk)


def is_rich_black(c: float, m: float, y: float, k: float) -> bool:
    t = RICH_BLACK_THRESHOLD
    return (k >= t["k"] and c >= t["c"] and m >= t["m"] and y >= t["y"])


def overprint_gs_preamble() -> str:
    return (
        "% CMYK plugin: enable overprint mode 1\n"
        "<< /OPM 1 >> setuserparams\n"
        "true setoverprint\n"
        "% Verify separations in Acrobat Pro:\n"
        "%   Tools > Print Production > Output Preview > Separations\n"
    )


def build_gs_overprint_args() -> List[str]:
    return [
        "-dOverrideICC",
        "-dSimulateOverprint=true",
        "-c", "true setoverprint <</OPM 1>> setuserparams",
        "-f",
    ]


# ===========================================================================
# SECTION 6 – PREFLIGHT
# ===========================================================================

PF_INK_OVER_LIMIT     = "INK_OVER_LIMIT"
PF_RICH_BLACK         = "RICH_BLACK"
PF_THIN_STROKE        = "THIN_STROKE"
PF_NO_CMYK_ANNOT      = "NO_CMYK_ANNOT"
PF_SPOT_MISMATCH      = "SPOT_MISMATCH"
PF_MISSING_BLEED      = "MISSING_BLEED"
PF_LOW_RESOLUTION     = "LOW_RESOLUTION"
PF_TEXT_OVERPRINT     = "TEXT_OVERPRINT"
PF_GRADIENT_NONCMYK   = "GRADIENT_NONCMYK"
PF_OBJECT_OUTSIDE_PAGE= "OUTSIDE_PAGE"
PF_PURE_K_RECOMMENDED = "PURE_K_RECOMMENDED"
PF_HAIRLINE           = "HAIRLINE"
# v2.2 new preflight codes
PF_PATTERN_OVER_LIMIT = "PATTERN_INK_OVER_LIMIT"  # pattern tile ink exceeds limit
PF_TRAP_NEEDED        = "TRAP_NEEDED"              # adjacent elements need trapping
# v2.4 new preflight codes
PF_TRANSPARENCY_X1A   = "TRANSPARENCY_X1A"         # transparency incompatible with PDF/X-1a
PF_SPOT_NOT_SEPARATED = "SPOT_NOT_SEPARATED"       # spot colour not mapped to a plate
PF_CMYK_RGB_DESYNC    = "CMYK_RGB_DESYNC"          # display colour drifted from stored CMYK
PF_PDFX_COMPLIANCE    = "PDFX_COMPLIANCE"          # document not compliant with target PDF/X mode
PF_TRANSPARENCY_OP_CONFLICT = "TRANSPARENCY_OVERPRINT_CONFLICT"  # overprint + opacity interaction

DEFAULT_INK_LIMIT_PCT  = 300.0
DEFAULT_MIN_STROKE_PT  = 0.25
DEFAULT_HAIRLINE_PT    = 0.25
DEFAULT_MIN_IMAGE_DPI  = 150
DEFAULT_BLEED_MM       = 3.0


@dataclass
class PreflightIssue:
    code:       str
    severity:   str
    element_id: str
    message:    str
    value:      Optional[float] = None

    def to_dict(self) -> Dict:
        d = {"code": self.code, "severity": self.severity,
             "id": self.element_id, "msg": self.message}
        if self.value is not None:
            d["val"] = round(self.value, 2)
        return d

    def __str__(self) -> str:
        loc = f" [{self.element_id}]" if self.element_id else ""
        val = f" ({self.value:.1f})" if self.value is not None else ""
        return f"[{self.severity.upper():7}] {self.code}{val}{loc}: {self.message}"


@dataclass
class PreflightConfig:
    ink_limit_pct:    float = DEFAULT_INK_LIMIT_PCT
    min_stroke_pt:    float = DEFAULT_MIN_STROKE_PT
    hairline_pt:      float = DEFAULT_HAIRLINE_PT
    min_image_dpi:    int   = DEFAULT_MIN_IMAGE_DPI
    bleed_mm:         float = DEFAULT_BLEED_MM
    check_rich_black: bool  = True
    check_bleed:      bool  = True
    check_images:     bool  = True
    check_overprint:  bool  = True
    check_gradients:  bool  = True
    check_patterns:   bool  = True   # v2.2
    check_trapping:   bool  = False  # v2.2 — off by default (expensive)
    check_transparency:bool = True   # v2.4 — detect opacity/blend issues
    pdfx_mode:        str  = "none"  # v2.4 — target PDF/X mode for compliance check
    check_desync:     bool = True    # v2.4 — detect RGB/CMYK drift


@dataclass
class PreflightReport:
    issues:             List[PreflightIssue] = field(default_factory=list)
    config:             PreflightConfig      = field(default_factory=PreflightConfig)
    n_elements_checked: int                  = 0
    passed:             bool                 = True

    def add(self, issue: PreflightIssue):
        self.issues.append(issue)
        if issue.severity == "error":
            self.passed = False

    def errors(self)   -> List[PreflightIssue]:
        return [i for i in self.issues if i.severity == "error"]

    def warnings(self) -> List[PreflightIssue]:
        return [i for i in self.issues if i.severity == "warning"]

    def infos(self)    -> List[PreflightIssue]:
        return [i for i in self.issues if i.severity == "info"]

    def summary(self) -> str:
        e, w, i = len(self.errors()), len(self.warnings()), len(self.infos())
        status  = "PASS" if self.passed else "FAIL"
        return (f"Preflight {status}: {e} error(s)  {w} warning(s)  {i} info(s)  "
                f"— {self.n_elements_checked} elements checked")

    def to_text(self) -> str:
        status_line = ("OK  PREFLIGHT PASS" if self.passed
                       else "!!  PREFLIGHT FAIL -- fix errors before sending to press")
        lines = [status_line, self.summary(), ""]
        for severity in ("error", "warning", "info"):
            group = [i for i in self.issues if i.severity == severity]
            if group:
                lines.append("-" * 60)
                lines.append(f"{severity.upper()}S ({len(group)})")
                lines.append("-" * 60)
                lines.extend(str(i) for i in group)
                lines.append("")
        return "\n".join(lines)

    def to_json(self) -> str:
        return json.dumps({
            "passed":  self.passed,
            "summary": self.summary(),
            "issues":  [i.to_dict() for i in self.issues],
            "checked": self.n_elements_checked,
        }, indent=2)


_PT_PER_MM = 72.0 / 25.4
_PX_PER_IN = 96.0
_PT_PER_IN = 72.0
_PX_PER_MM = _PX_PER_IN / 25.4


def _parse_length_to_pt(value_str: str) -> Optional[float]:
    if not value_str:
        return None
    value_str = value_str.strip()
    m = re.fullmatch(r"([+-]?[\d.]+(?:e[+-]?\d+)?)\s*(mm|cm|in|pt|px|pc|em|%|)",
                     value_str, re.I)
    if not m:
        return None
    val  = float(m.group(1))
    unit = m.group(2).lower()
    return {
        "mm": val * _PT_PER_MM,
        "cm": val * _PT_PER_MM * 10,
        "in": val * _PT_PER_IN,
        "pt": val,
        "pc": val * 12.0,
        "px": val * (_PT_PER_IN / _PX_PER_IN),
        "":   val * (_PT_PER_IN / _PX_PER_IN),
    }.get(unit)


def mm_to_px(mm: float) -> float:
    return mm * _PX_PER_MM


def run_preflight(iter_elements, get_doc_attrib,
                  config: Optional[PreflightConfig] = None) -> PreflightReport:
    """Run all enabled preflight checks. iter_elements is consumed once."""
    cfg    = config or PreflightConfig()
    report = PreflightReport(config=cfg)

    # Materialise iterator so we can use it for both trapping and element checks
    elements = list(iter_elements)

    # --- Document-level bleed check ---
    if cfg.check_bleed:
        has_bleed = bool(get_doc_attrib("inkscape:bleed", ""))
        if not has_bleed:
            vbox      = get_doc_attrib("viewBox", "")
            w_str     = get_doc_attrib("width",   "")
            h_str     = get_doc_attrib("height",  "")
            detected  = False
            if vbox and w_str and h_str:
                try:
                    vb   = [float(x) for x in vbox.split()]
                    w    = _parse_length_to_pt(w_str)
                    h    = _parse_length_to_pt(h_str)
                    vb_w = _parse_length_to_pt(f"{vb[2]}px") or vb[2]
                    vb_h = _parse_length_to_pt(f"{vb[3]}px") or vb[3]
                    if w and h and vb_w > w + 0.5 and vb_h > h + 0.5:
                        detected = True
                except (ValueError, IndexError):
                    pass
            if not detected:
                report.add(PreflightIssue(
                    code=PF_MISSING_BLEED, severity="warning", element_id="",
                    message=(f"No bleed area detected.  "
                             f"Press standard is {cfg.bleed_mm:.1f}mm on all sides.")))

    spot_registry: Dict[str, CMYK] = {}
    cmyk_elements: List[Dict]      = []   # for trapping pass

    for el in elements:
        tag = el.tag if isinstance(el.tag, str) else ""
        if not tag.startswith("{"):
            continue
        eid = el.get("id") or ""
        report.n_elements_checked += 1

        style: Dict[str, str] = {}
        try:
            st = el.style
            if hasattr(st, "items"):
                style = dict(st.items())
        except AttributeError:
            pass

        c_attr     = el.get(ATTR_C)
        has_cmyk   = c_attr is not None
        fill_val   = style.get("fill",   "")
        stroke_val = style.get("stroke", "")
        has_color  = any(v and v not in ("none","inherit","transparent")
                         for v in (fill_val, stroke_val))

        if has_color and not has_cmyk:
            report.add(PreflightIssue(
                code=PF_NO_CMYK_ANNOT, severity="warning", element_id=eid,
                message="Element has fill/stroke but no CMYK annotation."))

        if has_cmyk:
            c = float(c_attr or 0)
            m = float(el.get(ATTR_M, 0) or 0)
            y = float(el.get(ATTR_Y, 0) or 0)
            k = float(el.get(ATTR_K, 0) or 0)
            total = ink_total(c, m, y, k)

            if total > cfg.ink_limit_pct:
                report.add(PreflightIssue(
                    code=PF_INK_OVER_LIMIT, severity="error", element_id=eid,
                    message=f"Total ink {total:.1f}% exceeds limit {cfg.ink_limit_pct:.0f}%.",
                    value=total))

            if cfg.check_rich_black and is_rich_black(c, m, y, k):
                report.add(PreflightIssue(
                    code=PF_RICH_BLACK, severity="info", element_id=eid,
                    message=(f"Rich black (C={c*100:.0f}% M={m*100:.0f}% "
                             f"Y={y*100:.0f}% K={k*100:.0f}%)."),
                    value=total))

            if k > 0.80 and not (k > 0.95 and c < 0.05 and m < 0.05 and y < 0.05):
                if "text" in tag.lower():
                    report.add(PreflightIssue(
                        code=PF_PURE_K_RECOMMENDED, severity="warning",
                        element_id=eid,
                        message="Text uses non-pure-K black — use K=100% for body text."))

            spot_name = el.get(ATTR_SPOT_NAME, "")
            if spot_name:
                cmyk_tuple = (c, m, y, k)
                if spot_name in spot_registry:
                    diff = max(abs(a - b)
                               for a, b in zip(spot_registry[spot_name], cmyk_tuple))
                    if diff > 0.02:
                        report.add(PreflightIssue(
                            code=PF_SPOT_MISMATCH, severity="error",
                            element_id=eid,
                            message=f"Spot '{spot_name}' inconsistent CMYK (diff={diff*100:.1f}%)."))
                else:
                    spot_registry[spot_name] = (c, m, y, k)

            if cfg.check_overprint:
                fp = el.get(ATTR_OVERPRINT_FILL,   "0") == "1"
                sp = el.get(ATTR_OVERPRINT_STROKE, "0") == "1"
                if (fp or sp) and "text" in tag.lower():
                    fsz = style.get("font-size", "")
                    fpt = _parse_length_to_pt(fsz) if fsz else None
                    if fpt and fpt < 14:
                        report.add(PreflightIssue(
                            code=PF_TEXT_OVERPRINT, severity="warning",
                            element_id=eid,
                            message=f"Overprint on small text ({fpt:.1f}pt).",
                            value=fpt))

            if eid:
                cmyk_elements.append({"id": eid, "c": c, "m": m, "y": y, "k": k})

        # --- Pattern ink check (v2.2) ---
        if cfg.check_patterns:
            pat_blob = el.get(ATTR_PATTERN, "")
            if pat_blob:
                pat_total = pattern_ink_total(pat_blob)
                if pat_total > cfg.ink_limit_pct:
                    report.add(PreflightIssue(
                        code=PF_PATTERN_OVER_LIMIT, severity="error",
                        element_id=eid,
                        message=(f"Pattern tile max ink {pat_total:.1f}% "
                                 f"exceeds limit {cfg.ink_limit_pct:.0f}%."),
                        value=pat_total))

        # --- Gradient check ---
        if cfg.check_gradients:
            if fill_val.startswith("url(") and not el.get(ATTR_GRAD_STOPS):
                report.add(PreflightIssue(
                    code=PF_GRADIENT_NONCMYK, severity="warning",
                    element_id=eid,
                    message="Gradient fill has no CMYK stop metadata."))

        # --- Hairline / thin stroke checks ---
        sw_str = style.get("stroke-width", "")
        if sw_str and stroke_val and stroke_val not in ("none", "inherit"):
            sw_pt = _parse_length_to_pt(sw_str)
            if sw_pt is not None:
                if sw_pt < cfg.hairline_pt:
                    report.add(PreflightIssue(
                        code=PF_HAIRLINE, severity="error", element_id=eid,
                        message=f"Stroke {sw_pt:.3f}pt is a hairline (< {cfg.hairline_pt:.2f}pt).",
                        value=sw_pt))
                elif sw_pt < cfg.min_stroke_pt:
                    report.add(PreflightIssue(
                        code=PF_THIN_STROKE, severity="warning", element_id=eid,
                        message=f"Stroke {sw_pt:.3f}pt below minimum {cfg.min_stroke_pt:.2f}pt.",
                        value=sw_pt))

        # --- Image resolution proxy ---
        if cfg.check_images and "image" in tag.lower():
            try:
                iw = float(el.get("width",  "0") or "0")
                ih = float(el.get("height", "0") or "0")
                if 0 < iw < 50 or 0 < ih < 50:
                    report.add(PreflightIssue(
                        code=PF_LOW_RESOLUTION, severity="warning",
                        element_id=eid,
                        message=f"Image very small ({iw:.0f}x{ih:.0f}px SVG units)."))
            except (ValueError, TypeError):
                pass

    # --- Transparency + Overprint conflict check (v2.4) ---
    if cfg.check_transparency:
        for el in elements:
            eid   = el.get("id") or ""
            style: Dict[str, str] = {}
            try:
                st = el.style
                if hasattr(st,"items"): style = dict(st.items())
            except AttributeError:
                pass
            op_str = style.get("opacity", el.get("opacity",""))
            has_op = False
            try:
                has_op = float(op_str) < 0.9999 if op_str else False
            except ValueError:
                pass
            blend  = style.get("mix-blend-mode","")
            has_blend = blend and blend != "normal"
            has_mask  = bool(el.get("mask") or style.get("mask",""))

            # Conflict: overprint is set AND element has transparency
            fp = el.get(ATTR_OVERPRINT_FILL,   "0") == "1"
            sp = el.get(ATTR_OVERPRINT_STROKE, "0") == "1"
            if (fp or sp) and (has_op or has_blend or has_mask):
                report.add(PreflightIssue(
                    code=PF_TRANSPARENCY_OP_CONFLICT, severity="error",
                    element_id=eid,
                    message=(
                        "Overprint is set on an element that also has transparency "
                        "({}).  "
                        "This combination produces undefined output on most RIPs.  "
                        "Either remove the transparency or disable overprint.".format(
                            "opacity" if has_op else
                            "blend-mode:{}".format(blend) if has_blend else "mask"
                        )
                    )
                ))

            # Flag any transparency for PDF/X-1a mode
            if cfg.pdfx_mode == "pdfx1a" and (has_op or has_blend or has_mask):
                report.add(PreflightIssue(
                    code=PF_TRANSPARENCY_X1A, severity="error",
                    element_id=eid,
                    message=(
                        "Transparency present ({}) — incompatible with PDF/X-1a.  "
                        "Flatten transparency before export or use PDF/X-4.".format(
                            "opacity" if has_op else
                            "blend-mode:{}".format(blend) if has_blend else "mask"
                        )
                    )
                ))

    # --- Trapping pass (v2.2, optional — O(n^2) so disabled by default) ---
    if cfg.check_trapping and len(cmyk_elements) <= 200:
        trap_report = find_trap_pairs(cmyk_elements)
        for pair in trap_report.pairs:
            report.add(PreflightIssue(
                code=PF_TRAP_NEEDED, severity="warning",
                element_id=pair.id_a,
                message=(f"No shared ink channel with '{pair.id_b}' — "
                         "misregistration gap possible.  Consider adding a trap stroke.")))

    return report


# ===========================================================================
# SECTION 7 – COMPRESSION
# ===========================================================================

COMPRESSION_SAFE       = 3
COMPRESSION_STANDARD   = 2
COMPRESSION_AGGRESSIVE = 1

_PATH_NUM_RE         = re.compile(r"(-?\d+\.\d+)")
_STYLE_WHITESPACE_RE = re.compile(r"\s*([;:{},])\s*")
_MULTI_SPACE_RE      = re.compile(r"  +")


def round_path_data(d: str, precision: int = 3) -> str:
    def _round(m: re.Match) -> str:
        val = round(float(m.group(1)), precision)
        if precision == 0:
            return str(int(val))
        s = f"{val:.{precision}f}"
        if "." in s:
            s = s.rstrip("0").rstrip(".")
        return s
    result = _PATH_NUM_RE.sub(_round, d)
    return result if result.strip() else "0"


def normalise_style_string(style: str) -> str:
    props: Dict[str, str] = {}
    for part in style.split(";"):
        part = part.strip()
        if ":" in part:
            k, v = part.split(":", 1)
            k, v = k.strip(), v.strip()
            if k and v:
                props[k] = v
    return ";".join(f"{k}:{props[k]}" for k in sorted(props))


def compress_svg_bytes(svg_bytes: bytes, level: int = 9) -> bytes:
    buf = io.BytesIO()
    with gzip.GzipFile(fileobj=buf, mode="wb", compresslevel=level) as gz:
        gz.write(svg_bytes)
    return buf.getvalue()


def decompress_svgz_bytes(svgz_bytes: bytes) -> bytes:
    buf = io.BytesIO(svgz_bytes)
    with gzip.GzipFile(fileobj=buf, mode="rb") as gz:
        return gz.read()


@dataclass
class CompressionStats:
    original_bytes:   int = 0
    compressed_bytes: int = 0
    paths_rounded:    int = 0
    styles_deduped:   int = 0
    chars_saved:      int = 0
    svgz_bytes:       int = 0

    @property
    def ratio(self) -> float:
        return 0.0 if self.original_bytes == 0 else \
               1.0 - self.compressed_bytes / self.original_bytes

    @property
    def svgz_ratio(self) -> float:
        return 0.0 if self.original_bytes == 0 else \
               1.0 - self.svgz_bytes / self.original_bytes

    def summary(self) -> str:
        return (
            f"Compression results:\n"
            f"  Original:      {self.original_bytes:>10,} bytes\n"
            f"  After cleanup: {self.compressed_bytes:>10,} bytes  "
            f"({self.ratio*100:.1f}% saved)\n"
            f"  SVGZ output:   {self.svgz_bytes:>10,} bytes  "
            f"({self.svgz_ratio*100:.1f}% saved total)\n"
            f"  Paths rounded: {self.paths_rounded}\n"
            f"  Styles deduped:{self.styles_deduped}\n"
            f"  Chars removed: {self.chars_saved:,}"
        )


def compress_svg_document(svg_text: str, path_precision: int = 3,
                          dedup_styles: bool = True,
                          produce_svgz: bool = False,
                          compression_level: int = 9) -> Tuple[str, CompressionStats]:
    stats = CompressionStats()
    stats.original_bytes = len(svg_text.encode("utf-8"))

    def _round_path_attr(m: re.Match) -> str:
        stats.paths_rounded += 1
        return m.group(1) + round_path_data(m.group(2), path_precision) + m.group(3)

    path_re  = re.compile(r'(\bd\s*=\s*["\'])([^"\']+)(["\'])', re.DOTALL)
    svg_text = path_re.sub(_round_path_attr, svg_text)
    style_re = re.compile(r'(\bstyle\s*=\s*["\'])([^"\']+)(["\'])')

    def _norm_style(m: re.Match) -> str:
        return m.group(1) + normalise_style_string(m.group(2)) + m.group(3)
    svg_text = style_re.sub(_norm_style, svg_text)

    def _strip_ws(m: re.Match) -> str:
        inner = _STYLE_WHITESPACE_RE.sub(r"\1", m.group(2))
        inner = _MULTI_SPACE_RE.sub(" ", inner)
        return m.group(1) + inner + m.group(3)
    svg_text = style_re.sub(_strip_ws, svg_text)

    final_bytes            = len(svg_text.encode("utf-8"))
    stats.compressed_bytes = final_bytes
    stats.chars_saved      = stats.original_bytes - final_bytes
    if produce_svgz:
        stats.svgz_bytes = len(compress_svg_bytes(svg_text.encode("utf-8"),
                                                   compression_level))
    return svg_text, stats


def estimate_element_bytes(el_repr: str) -> int:
    return len(el_repr.encode("utf-8"))


def build_gs_compression_args(image_downsample_dpi: int = 300,
                               embed_fonts: bool = True,
                               pdf_version: str = "1.4") -> List[str]:
    args = [
        f"-dCompatibilityLevel={pdf_version}",
        "-dNOPAUSE", "-dBATCH", "-dSAFER",
        "-sDEVICE=pdfwrite",
        "-sColorConversionStrategy=CMYK",
        "-dProcessColorModel=/DeviceCMYK",
        "-dCompressFonts=true", "-dSubsetFonts=true",
        f"-dColorImageResolution={image_downsample_dpi}",
        f"-dGrayImageResolution={image_downsample_dpi}",
        f"-dMonoImageResolution={image_downsample_dpi}",
        "-dColorImageDownsampleType=/Bicubic",
        "-dGrayImageDownsampleType=/Bicubic",
        "-dCompressPages=true", "-dUseFlateCompression=true",
        "-dOptimize=true",
    ]
    if embed_fonts:
        args += ["-dEmbedAllFonts=true"]
    return args


# ===========================================================================
# SECTION 8 – SEPARATIONS PREVIEW  (new in v2.2)
# ===========================================================================

SEPARATION_LAYER_PREFIX = "cmyk-sep-preview"
SEPARATION_CHANNELS     = ("c", "m", "y", "k")
SEPARATION_LABELS       = {
    "c": "Cyan", "m": "Magenta", "y": "Yellow", "k": "Black"
}
SEPARATION_TINT_COLORS  = {
    "c": "#00b4d8",   # cyan tint for tinted preview mode
    "m": "#e63946",   # magenta tint
    "y": "#f4d03f",   # yellow tint
    "k": "#1a1a1a",   # key black
}


def channel_to_greyscale(c: float, m: float, y: float, k: float,
                         channel: str) -> int:
    """
    Return 0-255 greyscale representing ink density on one separation plate.

    Plate convention: 0 = no ink (white on plate), 255 = full ink (black).

    Args:
        c, m, y, k : CMYK (0-1)
        channel    : 'c', 'm', 'y', or 'k'

    Returns:
        int in [0, 255]
    """
    value = {"c": c, "m": m, "y": y, "k": k}.get(channel.lower(), 0.0)
    return int(round(_clamp01(float(value)) * 255))


def channel_to_hex(c: float, m: float, y: float, k: float,
                   channel: str) -> str:
    """Hex grey for a channel's plate density."""
    g = channel_to_greyscale(c, m, y, k, channel)
    return rgb_to_hex(g, g, g)


def separation_style(c: float, m: float, y: float, k: float,
                     channel: str,
                     tinted: bool = False,
                     existing_style: str = "") -> str:
    """
    Build an SVG inline style for an element rendered on a separation plate.

    Standard (greyscale) mode: fill = grey proportional to channel density.
        Full ink  -> #000000 (black)
        No ink    -> #ffffff (white)

    Tinted mode: fill = channel identity colour at proportional opacity.
        Makes it easier to identify which plate you are looking at.

    Args:
        c, m, y, k    : CMYK (0-1) of the element
        channel       : 'c' | 'm' | 'y' | 'k'
        tinted        : use channel colour instead of grey
        existing_style: preserve other CSS properties

    Returns:
        CSS style string with fill/stroke set for the separation view.
    """
    density = _clamp01({"c": c, "m": m, "y": y, "k": k}.get(channel.lower(), 0.0))

    if tinted:
        fill_color   = SEPARATION_TINT_COLORS.get(channel.lower(), "#000000")
        fill_opacity = f"{density:.4f}"
    else:
        # Invert: 0 ink = white background, full ink = black plate
        g            = int(round((1.0 - density) * 255))
        fill_color   = rgb_to_hex(g, g, g)
        fill_opacity = "1"

    props: Dict[str, str] = {}
    for part in existing_style.split(";"):
        part = part.strip()
        if ":" in part:
            pk, pv = part.split(":", 1)
            props[pk.strip()] = pv.strip()

    props["fill"]           = fill_color
    props["fill-opacity"]   = fill_opacity
    props["stroke"]         = fill_color
    props["stroke-opacity"] = f"{density * 0.5:.4f}" if density > 0 else "0"
    # Strip overprint blend modes — not needed on separation view
    props.pop("mix-blend-mode", None)
    props.pop("isolation",      None)

    return ";".join(f"{k}:{v}" for k, v in props.items() if v)


def spot_coverage_style(c: float, m: float, y: float, k: float,
                        spot_name: str, target_spot: str,
                        existing_style: str = "") -> str:
    """
    Build a style for a named spot colour plate.

    Elements carrying the target spot name are shown at density.
    All other elements render white (not on this plate).
    """
    on_plate = spot_name.strip().lower() == target_spot.strip().lower()

    if on_plate:
        density = _clamp01(ink_total(c, m, y, k) / 100.0)
        g       = int(round((1.0 - density) * 255))
        color   = rgb_to_hex(g, g, g)
        opacity = "1"
    else:
        color   = "#ffffff"
        opacity = "0"

    props: Dict[str, str] = {}
    for part in existing_style.split(";"):
        part = part.strip()
        if ":" in part:
            pk, pv = part.split(":", 1)
            props[pk.strip()] = pv.strip()

    props["fill"]           = color
    props["fill-opacity"]   = opacity
    props["stroke"]         = color
    props["stroke-opacity"] = opacity
    props.pop("mix-blend-mode", None)

    return ";".join(f"{k}:{v}" for k, v in props.items() if v)


@dataclass
class SeparationPlate:
    """Describes a single ink plate in the separations preview."""
    channel:       str         # 'c' | 'm' | 'y' | 'k' | spot name
    label:         str         # Human label, e.g. "Cyan", "PANTONE 485 C"
    is_spot:       bool = False
    tinted:        bool = False
    layer_id:      str  = ""   # SVG layer element id once created
    element_count: int  = 0    # Elements with non-zero ink on this plate

    @property
    def layer_name(self) -> str:
        return build_separation_layer_name(self.channel)


def build_separation_layer_name(channel: str) -> str:
    """Return the Inkscape layer label for a separation preview layer."""
    label = SEPARATION_LABELS.get(channel.lower(),
                                   channel.upper().replace(" ", "-"))
    return f"{SEPARATION_LAYER_PREFIX}:{label}"


def four_up_transforms(page_width: float, page_height: float,
                       gap: float = 20.0) -> List[str]:
    """
    Return four SVG transform strings for the 2x2 four-up plate grid.

    Each quadrant is scaled to 50% so all four plates fit on one canvas.

    Args:
        page_width, page_height : document size in user units (px)
        gap                     : spacing between quadrants in user units

    Returns:
        [top-left, top-right, bottom-left, bottom-right] transform strings
    """
    hw  = page_width  / 2.0
    hh  = page_height / 2.0
    g2  = gap / 2.0
    positions = [
        (0,       0    ),
        (hw + g2, 0    ),
        (0,       hh+g2),
        (hw + g2, hh+g2),
    ]
    return [f"translate({x:.2f},{y:.2f}) scale(0.5)" for x, y in positions]


def separation_plates_for_document(iter_elements) -> List[SeparationPlate]:
    """
    Scan annotated elements and return the full set of plates present.
    Always includes C, M, Y, K.  Adds one plate per unique spot found.
    """
    plates: List[SeparationPlate] = [
        SeparationPlate(channel="c", label="Cyan"),
        SeparationPlate(channel="m", label="Magenta"),
        SeparationPlate(channel="y", label="Yellow"),
        SeparationPlate(channel="k", label="Black"),
    ]
    seen_spots: set = set()
    elements = list(iter_elements)   # materialise so we can iterate twice

    for el in elements:
        spot = el.get(ATTR_SPOT_NAME, "")
        if spot and spot not in seen_spots:
            seen_spots.add(spot)
            plates.append(SeparationPlate(channel=spot, label=spot, is_spot=True))

    plate_map = {p.channel: p for p in plates}
    for el in elements:
        if el.get(ATTR_C) is None:
            continue
        c = float(el.get(ATTR_C, 0) or 0)
        m = float(el.get(ATTR_M, 0) or 0)
        y = float(el.get(ATTR_Y, 0) or 0)
        k = float(el.get(ATTR_K, 0) or 0)
        spot = el.get(ATTR_SPOT_NAME, "")
        if c > 0.001 and "c" in plate_map: plate_map["c"].element_count += 1
        if m > 0.001 and "m" in plate_map: plate_map["m"].element_count += 1
        if y > 0.001 and "y" in plate_map: plate_map["y"].element_count += 1
        if k > 0.001 and "k" in plate_map: plate_map["k"].element_count += 1
        if spot and spot in plate_map:      plate_map[spot].element_count += 1

    return plates


# ===========================================================================
# SECTION 9 – TRAPPING
# ===========================================================================

DEFAULT_TRAP_WIDTH_PT  = 0.25
DEFAULT_TRAP_THRESHOLD = 0.02


def shares_ink_channel(cmyk1: CMYK, cmyk2: CMYK,
                       threshold: float = DEFAULT_TRAP_THRESHOLD) -> bool:
    """
    Return True if two CMYK colours share at least one ink channel above
    `threshold`.  Shared channels prevent misregistration gaps.
    """
    return any(a > threshold and b > threshold for a, b in zip(cmyk1, cmyk2))


def trap_needed(cmyk1: CMYK, cmyk2: CMYK,
                threshold: float = DEFAULT_TRAP_THRESHOLD) -> bool:
    """Return True when no shared channel -> trapping is required."""
    return not shares_ink_channel(cmyk1, cmyk2, threshold)


def lighter_cmyk(cmyk1: CMYK, cmyk2: CMYK) -> CMYK:
    """Return the lighter (lower total ink) of two CMYK values."""
    return cmyk1 if ink_total(*cmyk1) <= ink_total(*cmyk2) else cmyk2


def darker_cmyk(cmyk1: CMYK, cmyk2: CMYK) -> CMYK:
    """Return the darker (higher total ink) of two CMYK values."""
    return cmyk2 if ink_total(*cmyk1) <= ink_total(*cmyk2) else cmyk1


def trap_stroke_style(trap_cmyk: CMYK,
                      width_pt: float = DEFAULT_TRAP_WIDTH_PT,
                      existing_style: str = "") -> str:
    """
    Build CSS for a trap stroke (lighter colour, spread trap).

    The stroke is set to overprint so it sits on top of both adjacent
    inks without knocking either out.
    """
    hex_c = cmyk_to_hex(*trap_cmyk)
    props: Dict[str, str] = {}
    for part in existing_style.split(";"):
        part = part.strip()
        if ":" in part:
            pk, pv = part.split(":", 1)
            props[pk.strip()] = pv.strip()
    props["stroke"]         = hex_c
    props["stroke-width"]   = f"{width_pt:.3f}pt"
    props["stroke-opacity"] = "1"
    props["mix-blend-mode"] = "multiply"   # trap must overprint
    return ";".join(f"{k}:{v}" for k, v in props.items() if v)


@dataclass
class TrapPair:
    """A pair of adjacent elements that need a trap stroke."""
    id_a:          str
    id_b:          str
    cmyk_a:        CMYK
    cmyk_b:        CMYK
    trap_color:    CMYK
    trap_width_pt: float = DEFAULT_TRAP_WIDTH_PT

    @property
    def needs_trap(self) -> bool:
        return trap_needed(self.cmyk_a, self.cmyk_b)

    def trap_style(self) -> str:
        return trap_stroke_style(self.trap_color, self.trap_width_pt)

    def to_dict(self) -> Dict:
        return {
            "id_a":       self.id_a,
            "id_b":       self.id_b,
            "trap_color": list(self.trap_color),
            "trap_width": self.trap_width_pt,
        }


@dataclass
class TrapReport:
    pairs:     List[TrapPair] = field(default_factory=list)
    n_checked: int            = 0

    def summary(self) -> str:
        return (f"Trap analysis: {len(self.pairs)} pair(s) need trapping "
                f"of {self.n_checked} element(s) checked.")

    def to_json(self) -> str:
        return json.dumps({"summary": self.summary(),
                           "pairs":   [p.to_dict() for p in self.pairs]},
                          indent=2)


def find_trap_pairs(elements_with_cmyk: List[Dict],
                   threshold: float = DEFAULT_TRAP_THRESHOLD) -> TrapReport:
    """
    Scan element dicts for pairs needing trapping.

    Each dict requires keys: 'id', 'c', 'm', 'y', 'k'.
    Checks all pairs in document order; caller is responsible for spatial
    filtering (only adjacent elements need trapping in practice).
    """
    report = TrapReport(n_checked=len(elements_with_cmyk))
    n = len(elements_with_cmyk)
    for i in range(n):
        for j in range(i + 1, n):
            a  = elements_with_cmyk[i]
            b  = elements_with_cmyk[j]
            ca = (a.get("c",0.0), a.get("m",0.0), a.get("y",0.0), a.get("k",0.0))
            cb = (b.get("c",0.0), b.get("m",0.0), b.get("y",0.0), b.get("k",0.0))
            if trap_needed(ca, cb, threshold):
                report.pairs.append(TrapPair(
                    id_a=a.get("id", f"el{i}"),
                    id_b=b.get("id", f"el{j}"),
                    cmyk_a=ca, cmyk_b=cb,
                    trap_color=lighter_cmyk(ca, cb),
                ))
    return report


# ===========================================================================
# SECTION 10 – PATTERN FILL CMYK
# ===========================================================================
#
# JSON schema for cmyk:pattern-colors:
# [{"i": child_index, "prop": "fill"|"stroke",
#   "c": float, "m": float, "y": float, "k": float, "hex": str}, ...]

def build_pattern_cmyk_metadata(child_colors: List[Dict]) -> str:
    """Serialise pattern tile child colours to cmyk:pattern-colors JSON blob."""
    records = []
    for item in child_colors:
        c = _clamp01(float(item.get("c", 0)))
        m = _clamp01(float(item.get("m", 0)))
        y = _clamp01(float(item.get("y", 0)))
        k = _clamp01(float(item.get("k", 0)))
        records.append({
            "i":    int(item.get("child_index", 0)),
            "prop": item.get("prop", "fill"),
            "c":    round(c,6), "m": round(m,6),
            "y":    round(y,6), "k": round(k,6),
            "hex":  cmyk_to_hex(c, m, y, k),
        })
    return json.dumps(records, separators=(",",":"))


def parse_pattern_cmyk_metadata(blob: str) -> List[Dict]:
    """Decode a cmyk:pattern-colors JSON blob."""
    try:
        return json.loads(blob)
    except Exception:
        return []


def pattern_ink_total(blob: str) -> float:
    """Return maximum total ink across all tiles in a pattern blob."""
    records = parse_pattern_cmyk_metadata(blob)
    if not records:
        return 0.0
    return max(
        ink_total(r.get("c",0), r.get("m",0), r.get("y",0), r.get("k",0))
        for r in records
    )


# ===========================================================================
# SECTION 11 – INK HEATMAP
# ===========================================================================
#
# Maps total ink % to a green -> amber -> red RGB ramp:
#   0%   -> #22c55e  green  (safe)
#   250% -> #f59e0b  amber  (caution)
#   300+ -> #ef4444  red    (over limit)

HEATMAP_COLOR_SAFE    = (34,  197, 94 )   # #22c55e
HEATMAP_COLOR_CAUTION = (245, 158, 11 )   # #f59e0b
HEATMAP_COLOR_DANGER  = (239, 68,  68 )   # #ef4444
HEATMAP_CAUTION_PCT   = 250.0
HEATMAP_DANGER_PCT    = 300.0
HEATMAP_LAYER_NAME    = f"{SEPARATION_LAYER_PREFIX}:heatmap"


def _lerp(a: float, b: float, t: float) -> float:
    return a + (b - a) * t


def ink_heatmap_color(ink_pct: float) -> RGB:
    """
    Map total ink % (0-400) to a heatmap RGB colour.

    0-250%  : green  -> amber  (linear)
    250-400%: amber  -> red    (linear)
    """
    pct = max(0.0, min(400.0, float(ink_pct)))
    if pct <= HEATMAP_CAUTION_PCT:
        t = pct / HEATMAP_CAUTION_PCT
        r = int(round(_lerp(HEATMAP_COLOR_SAFE[0], HEATMAP_COLOR_CAUTION[0], t)))
        g = int(round(_lerp(HEATMAP_COLOR_SAFE[1], HEATMAP_COLOR_CAUTION[1], t)))
        b = int(round(_lerp(HEATMAP_COLOR_SAFE[2], HEATMAP_COLOR_CAUTION[2], t)))
    else:
        t = (pct - HEATMAP_CAUTION_PCT) / (400.0 - HEATMAP_CAUTION_PCT)
        r = int(round(_lerp(HEATMAP_COLOR_CAUTION[0], HEATMAP_COLOR_DANGER[0], t)))
        g = int(round(_lerp(HEATMAP_COLOR_CAUTION[1], HEATMAP_COLOR_DANGER[1], t)))
        b = int(round(_lerp(HEATMAP_COLOR_CAUTION[2], HEATMAP_COLOR_DANGER[2], t)))
    return (r, g, b)


def ink_heatmap_hex(ink_pct: float) -> str:
    """Hex colour for a total ink percentage."""
    return rgb_to_hex(*ink_heatmap_color(ink_pct))


def ink_heatmap_style(c: float, m: float, y: float, k: float,
                      opacity: float = 0.75) -> str:
    """
    CSS style for a heatmap overlay element.
    fill = heatmap colour, mix-blend-mode:multiply so it composites.
    """
    total = ink_total(c, m, y, k)
    hex_c = ink_heatmap_hex(total)
    return (f"fill:{hex_c};fill-opacity:{opacity:.4f};"
            f"stroke:none;mix-blend-mode:multiply")
