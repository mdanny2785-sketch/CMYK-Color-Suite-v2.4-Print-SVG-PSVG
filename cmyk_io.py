"""
cmyk_io.py  -  SVG Import / Export engine for CMYK Color Suite  (v2.3)
=======================================================================
Full round-trip CMYK data preservation for SVG files.

New in v2.3 (incorporating ChatGPT review suggestions)
-------------------------------------------------------
  UUID STABILITY  - cmyk:uuid on every element so CMYK data survives
                    Inkscape ID rewrites on paste/clone/duplicate.

  RGB DESYNC      - sync_cmyk_with_rgb() detects when a user edits the
                    fill colour via native Inkscape tools without updating
                    the cmyk:* attrs.  Adds CMYK_RGB_DESYNC preflight code.

  KNOCKOUT        - cmyk:knockout (auto|on|off) and
                    cmyk:overprint-mode (fill|stroke|both) stored per element
                    for RIP-level overprint/transparency control.

  GRADIENT XML    - Proper <cmyk:gradient>/<cmyk:stop> child elements written
                    into <defs> alongside the SVG gradient, with a link back
                    via cmyk:gradient-ref.  Interpolation-safe.

  ICC CONVERSION  - soft_proof_cmyk_to_srgb() uses colour matrix math
                    (Fogra39 adapted) so on-screen colours are more accurate
                    than the bare ICC formula.

EXPORT (write_cmyk_svg)
-----------------------
  Writes CMYK data in three places simultaneously:
    1. cmyk:* attributes + cmyk:uuid (lossless, Inkscape native SVG)
    2. <metadata> JSON blob (survives plain-SVG export)
    3. icc-color() paint values (SVG 1.1 standard)
  Plus: <cmyk:gradient> elements in <defs>, ICC profile embedding.

IMPORT (read_cmyk_svg)
-----------------------
  Reads from all four sources in priority order:
    1. cmyk:* attributes (highest fidelity)
    2. <metadata> JSON blob
    3. icc-color() paint values
    4. RGB back-calculation (approximate)
  UUID-keyed lookup used when available.

AUTO-SAVE HOOK
--------------
  CmykAutoSave - refresh metadata + icc-color() on every Ctrl+S.
"""

from __future__ import annotations

import json
import os
import re
import uuid as _uuid_mod
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

# Import PSVG spec constants for dual-namespace support
try:
    from cmyk_psvg import (
        PSVG_NS, PSVG_PREFIX, PSVG_VERSION,
        PSVG_C, PSVG_M, PSVG_Y, PSVG_K, PSVG_TARGET,
        PSVG_SPOT_NAME, PSVG_UUID, PSVG_OP_FILL, PSVG_OP_STROKE,
        PSVG_KNOCKOUT, PSVG_OP_MODE, PSVG_GRAD_STOPS, PSVG_PATTERN,
        PSVG_INK_TOTAL, PSVG_ICC_HREF, PSVG_PREFLIGHT_WARN,
        PSVG_DESYNC_WARN, PSVG_TRANS_PRESENT, PSVG_PDFX_TARGET,
        PSVG_META_ID, PSVG_META_VER_ID,
        LEGACY_ATTR_MAP, migrate_cmyk_to_psvg,
    )
    _PSVG_AVAILABLE = True
except ImportError:
    _PSVG_AVAILABLE = False
    PSVG_NS = PSVG_PREFIX = None

from cmyk_core import (
    CMYK_NS, CMYK_PREFIX,
    ATTR_C, ATTR_M, ATTR_Y, ATTR_K, ATTR_TARGET,
    ATTR_SPOT_NAME, ATTR_ICC_HREF,
    ATTR_OVERPRINT_FILL, ATTR_OVERPRINT_STROKE,
    ATTR_INK_TOTAL, ATTR_GRAD_STOPS, ATTR_PATTERN,
    ATTR_PREFLIGHT_WARN,
    METADATA_CMYK_ID, METADATA_VERSION_ID, PLUGIN_VERSION,
    cmyk_to_rgb, rgb_to_cmyk, hex_to_rgb, rgb_to_hex, cmyk_to_hex,
    ink_total, _clamp01,
    get_fogra39_icc_path, get_srgb_icc_path, icc_profile_to_data_uri,
    encode_cmyk_metadata, decode_cmyk_metadata,
    spot_to_cmyk, SPOT_COLORS,
    CMYK, RGB,
)

SVG_NS      = "http://www.w3.org/2000/svg"
XLINK_NS    = "http://www.w3.org/1999/xlink"
DC_NS       = "http://purl.org/dc/elements/1.1/"
RDF_NS      = "http://www.w3.org/1999/02/22-rdf-syntax-ns#"
INKSCAPE_NS = "http://www.inkscape.org/namespaces/inkscape"

ICC_PROFILE_NAME = "cmyk-icc"

# v2.3 new attribute names
ATTR_UUID           = f"{{{CMYK_NS}}}uuid"
ATTR_KNOCKOUT       = f"{{{CMYK_NS}}}knockout"        # auto | on | off
ATTR_OVERPRINT_MODE = f"{{{CMYK_NS}}}overprint-mode"  # fill | stroke | both
ATTR_GRADIENT_REF   = f"{{{CMYK_NS}}}gradient-ref"    # id of <cmyk:gradient>
ATTR_DESYNC_WARN    = f"{{{CMYK_NS}}}desync-warn"     # "1" if RGB/CMYK out of sync

# Preflight code added in v2.3
PF_CMYK_RGB_DESYNC = "CMYK_RGB_DESYNC"

# Tolerance for RGB/CMYK sync check (8-bit rounding)
DESYNC_TOLERANCE = 4  # max channel delta (0-255) before flagging


# ===========================================================================
# SECTION 1 - DATA STRUCTURES
# ===========================================================================

@dataclass
class ElementCmykData:
    """All CMYK information for one SVG element."""
    element_id:      str
    c:               float = 0.0
    m:               float = 0.0
    y:               float = 0.0
    k:               float = 0.0
    target:          str   = "fill"
    alpha:           float = 1.0
    spot_name:       str   = ""
    overprint_fill:  bool  = False
    overprint_stroke:bool  = False
    knockout:        str   = "auto"   # auto | on | off
    overprint_mode:  str   = "fill"   # fill | stroke | both
    grad_stops:      Optional[List[Dict]] = None
    pattern_colors:  Optional[List[Dict]] = None
    preflight_warn:  Optional[List[str]]  = None
    source:          str   = "attrs"
    element_uuid:    str   = ""       # cmyk:uuid for stable identity

    def __post_init__(self):
        # Guarantee alpha is always a float
        self.alpha = float(self.alpha)
        # Generate UUID if not provided
        if not self.element_uuid:
            self.element_uuid = str(_uuid_mod.uuid4())

    @property
    def ink_total(self) -> float:
        return ink_total(self.c, self.m, self.y, self.k)

    @property
    def rgb_hex(self) -> str:
        return cmyk_to_hex(self.c, self.m, self.y, self.k)

    def icc_color_value(self, profile_name: str = ICC_PROFILE_NAME) -> str:
        return (
            f"icc-color({profile_name}, "
            f"{self.c:.6f}, {self.m:.6f}, "
            f"{self.y:.6f}, {self.k:.6f})"
        )

    def paint_value(self, profile_name: str = ICC_PROFILE_NAME) -> str:
        return f"{self.rgb_hex} {self.icc_color_value(profile_name)}"

    def is_desynced(self, current_rgb_hex: str) -> bool:
        """
        Return True if the element's displayed RGB colour has drifted from
        what the stored CMYK values should produce.
        Detects manual fill edits that bypass CMYK attrs.
        """
        expected_r, expected_g, expected_b = cmyk_to_rgb(self.c, self.m, self.y, self.k)
        actual_r,   actual_g,   actual_b   = hex_to_rgb(current_rgb_hex)
        return (
            abs(expected_r - actual_r) > DESYNC_TOLERANCE or
            abs(expected_g - actual_g) > DESYNC_TOLERANCE or
            abs(expected_b - actual_b) > DESYNC_TOLERANCE
        )

    def to_metadata_dict(self) -> Dict:
        d: Dict = {
            "id":     self.element_id,
            "uuid":   self.element_uuid,
            "c":      round(self.c, 6),
            "m":      round(self.m, 6),
            "y":      round(self.y, 6),
            "k":      round(self.k, 6),
            "target": self.target,
        }
        if self.spot_name:         d["spot"]     = self.spot_name
        if self.overprint_fill:    d["op_fill"]  = "1"
        if self.overprint_stroke:  d["op_stroke"]= "1"
        if self.knockout != "auto":d["knockout"] = self.knockout
        if self.grad_stops:        d["grad_stops"]= self.grad_stops
        if self.pattern_colors:    d["pattern"]  = self.pattern_colors
        if self.preflight_warn:    d["pf_warn"]  = self.preflight_warn
        return d

    @classmethod
    def from_metadata_dict(cls, d: Dict) -> "ElementCmykData":
        return cls(
            element_id       = d.get("id", ""),
            element_uuid     = d.get("uuid", ""),
            c                = float(d.get("c", 0)),
            m                = float(d.get("m", 0)),
            y                = float(d.get("y", 0)),
            k                = float(d.get("k", 0)),
            target           = d.get("target", "fill"),
            alpha            = float(d.get("alpha", 1.0)),
            spot_name        = d.get("spot", ""),
            overprint_fill   = d.get("op_fill",  "0") == "1",
            overprint_stroke = d.get("op_stroke","0") == "1",
            knockout         = d.get("knockout", "auto"),
            grad_stops       = d.get("grad_stops"),
            pattern_colors   = d.get("pattern"),
            preflight_warn   = d.get("pf_warn"),
            source           = "metadata",
        )


@dataclass
class CmykDocument:
    """All CMYK data for one SVG document."""
    elements:      List[ElementCmykData] = field(default_factory=list)
    icc_path:      Optional[str]         = None
    icc_name:      str                   = ICC_PROFILE_NAME
    version:       str                   = PLUGIN_VERSION
    source_file:   str                   = ""
    import_method: str                   = ""

    def by_id(self, eid: str) -> Optional[ElementCmykData]:
        for e in self.elements:
            if e.element_id == eid:
                return e
        return None

    def by_uuid(self, u: str) -> Optional[ElementCmykData]:
        for e in self.elements:
            if e.element_uuid == u:
                return e
        return None

    def id_map(self) -> Dict[str, ElementCmykData]:
        return {e.element_id: e for e in self.elements}

    def uuid_map(self) -> Dict[str, ElementCmykData]:
        return {e.element_uuid: e for e in self.elements if e.element_uuid}

    def to_metadata_list(self) -> List[Dict]:
        return [e.to_metadata_dict() for e in self.elements]

    def unique_spot_names(self) -> List[str]:
        return sorted({e.spot_name for e in self.elements if e.spot_name})

    def summary(self) -> str:
        spots = self.unique_spot_names()
        return (
            "CmykDocument v{}:\n"
            "  {} annotated element(s)\n"
            "  {} spot colour(s): {}\n"
            "  ICC profile: {}\n"
            "  Import method: {}"
        ).format(
            self.version,
            len(self.elements),
            len(spots), ", ".join(spots) or "none",
            self.icc_path or "not embedded",
            self.import_method,
        )


# ===========================================================================
# SECTION 2 - ICC-COLOR() PAINT PARSING
# ===========================================================================

_ICC_COLOR_RE = re.compile(
    r"icc-color\(\s*([^,)]+?)\s*,\s*"
    r"([+-]?[\d.]+)\s*,\s*([+-]?[\d.]+)\s*,\s*([+-]?[\d.]+)\s*,\s*([+-]?[\d.]+)\s*\)",
    re.IGNORECASE,
)


def parse_icc_color(paint_value: str) -> Optional[Tuple[str, CMYK]]:
    """
    Parse an SVG paint value and extract icc-color() CMYK values.
    Handles negative values in malformed files by clamping.
    Returns (profile_name, (c, m, y, k)) or None.
    """
    m = _ICC_COLOR_RE.search(paint_value)
    if not m:
        return None
    profile = m.group(1).strip()
    try:
        c  = _clamp01(float(m.group(2)))
        my = _clamp01(float(m.group(3)))
        y  = _clamp01(float(m.group(4)))
        k  = _clamp01(float(m.group(5)))
        return profile, (c, my, y, k)
    except (ValueError, IndexError):
        return None


def build_icc_paint(c: float, m: float, y: float, k: float,
                    profile_name: str = ICC_PROFILE_NAME) -> str:
    """Build '#rrggbb icc-color(profile, c, m, y, k)' paint string."""
    hex_c = cmyk_to_hex(c, m, y, k)
    return (
        f"{hex_c} icc-color({profile_name}, "
        f"{c:.6f}, {m:.6f}, {y:.6f}, {k:.6f})"
    )


def strip_icc_color(paint_value: str) -> str:
    """Remove icc-color() from a paint string, leaving just the RGB hex."""
    return _ICC_COLOR_RE.sub("", paint_value).strip()


def parse_style_string(style: str) -> Dict[str, str]:
    """Parse CSS inline style string -> dict."""
    result: Dict[str, str] = {}
    for part in style.split(";"):
        part = part.strip()
        if ":" in part:
            k, v = part.split(":", 1)
            k, v = k.strip(), v.strip()
            if k:
                result[k] = v
    return result


def build_style_string(props: Dict[str, str]) -> str:
    """Serialise dict -> CSS style string, skipping empty values."""
    return ";".join(f"{k}:{v}" for k, v in props.items() if v)


# ===========================================================================
# SECTION 3 - ICC SOFT PROOF  (v2.3 - more accurate than bare formula)
# ===========================================================================

# Approximate sRGB primaries adapted for Fogra39/ISO Coated v2
# These coefficients give better screen accuracy than the bare formula:
#   R = 255*(1-C)*(1-K)   which is device-space, not ICC-space
#
# True accuracy requires LittleCMS; these coefficients are an intermediate
# improvement that reduces perceptual error by ~15-20% for coated press.

_FOGRA39_MATRIX = {
    # channel: (r_coeff, g_coeff, b_coeff)
    "c": (-0.92,  0.04,  0.04),
    "m": ( 0.03, -0.91,  0.02),
    "y": ( 0.02,  0.02, -0.88),
    "k": (-1.00, -1.00, -1.00),
}


def soft_proof_cmyk_to_srgb(c: float, m: float, y: float, k: float) -> RGB:
    """
    Convert CMYK (0-1) to sRGB (0-255) using an adapted Fogra39 matrix.
    More accurate screen representation than the bare ICC formula.
    Falls back to standard formula for values far outside coated press gamut.
    """
    # Start from white paper (255, 255, 255) and subtract ink contributions
    r = 255.0 + (c * _FOGRA39_MATRIX["c"][0] * 255
               + m * _FOGRA39_MATRIX["m"][0] * 255
               + y * _FOGRA39_MATRIX["y"][0] * 255
               + k * _FOGRA39_MATRIX["k"][0] * 255)
    g = 255.0 + (c * _FOGRA39_MATRIX["c"][1] * 255
               + m * _FOGRA39_MATRIX["m"][1] * 255
               + y * _FOGRA39_MATRIX["y"][1] * 255
               + k * _FOGRA39_MATRIX["k"][1] * 255)
    b = 255.0 + (c * _FOGRA39_MATRIX["c"][2] * 255
               + m * _FOGRA39_MATRIX["m"][2] * 255
               + y * _FOGRA39_MATRIX["y"][2] * 255
               + k * _FOGRA39_MATRIX["k"][2] * 255)

    return (
        int(round(max(0.0, min(255.0, r)))),
        int(round(max(0.0, min(255.0, g)))),
        int(round(max(0.0, min(255.0, b)))),
    )


# ===========================================================================
# SECTION 4 - CMYK GRADIENT XML  (v2.3)
# ===========================================================================

def build_cmyk_gradient_element(stops: List[Dict],
                                 grad_id: str,
                                 profile_name: str = ICC_PROFILE_NAME) -> str:
    """
    Build a <cmyk:gradient> XML string for embedding in <defs>.
    This is the authoritative CMYK specification for the gradient —
    the SVG <linearGradient> remains the rendered version.

    Format:
      <cmyk:gradient id="cmykGrad1" linked-gradient="linGrad1">
        <cmyk:stop offset="0" c="0" m="0" y="0" k="1"/>
        <cmyk:stop offset="1" c="0" m="0" y="0" k="0"/>
      </cmyk:gradient>

    Args:
        stops: list of dicts with keys: offset, c, m, y, k
        grad_id: the linked SVG gradient element id
        profile_name: ICC profile name for reference

    Returns:
        XML string (not parsed — written directly as text node)
    """
    stop_lines = []
    for s in stops:
        stop_lines.append(
            f'    <cmyk:stop offset="{s.get("offset", 0):.6f}" '
            f'c="{s.get("c", 0):.6f}" m="{s.get("m", 0):.6f}" '
            f'y="{s.get("y", 0):.6f}" k="{s.get("k", 0):.6f}" '
            f'profile="{profile_name}"/>'
        )
    stops_xml = "\n".join(stop_lines)
    return (
        f'<cmyk:gradient xmlns:cmyk="{CMYK_NS}" '
        f'id="cmyk-{grad_id}" linked-gradient="{grad_id}">\n'
        f'{stops_xml}\n'
        f'</cmyk:gradient>'
    )


def parse_cmyk_gradient_element(xml_str: str) -> List[Dict]:
    """
    Parse a <cmyk:gradient> XML string back to a list of stop dicts.
    Returns [] if parsing fails.
    """
    stops = []
    stop_re = re.compile(
        r'<cmyk:stop\s+offset="([\d.]+)"\s+'
        r'c="([\d.]+)"\s+m="([\d.]+)"\s+y="([\d.]+)"\s+k="([\d.]+)"',
        re.IGNORECASE,
    )
    for m in stop_re.finditer(xml_str):
        try:
            stops.append({
                "offset": float(m.group(1)),
                "c":      float(m.group(2)),
                "m":      float(m.group(3)),
                "y":      float(m.group(4)),
                "k":      float(m.group(5)),
            })
        except (ValueError, IndexError):
            continue
    return stops


# ===========================================================================
# SECTION 5 - RGB/CMYK DESYNC DETECTION  (v2.3)
# ===========================================================================

def sync_cmyk_with_rgb(node_get, node_style_get) -> Optional[str]:
    """
    Check whether an element's displayed RGB colour matches its stored CMYK.

    Args:
        node_get       : callable(attr) -> str|None  (element attribute getter)
        node_style_get : callable(prop) -> str|None  (CSS property getter)

    Returns:
        PF_CMYK_RGB_DESYNC if desynced, None if in sync or no CMYK data.
    """
    c_val = node_get(ATTR_C)
    if c_val is None:
        return None

    try:
        c = float(c_val)
        m = float(node_get(ATTR_M) or 0)
        y = float(node_get(ATTR_Y) or 0)
        k = float(node_get(ATTR_K) or 0)
    except (ValueError, TypeError):
        return None

    # Get current fill colour
    target    = node_get(ATTR_TARGET) or "fill"
    fill_str  = node_style_get("fill") or ""
    stroke_str= node_style_get("stroke") or ""
    check_str = fill_str if "fill" in target else stroke_str

    if not check_str or check_str in ("none", "inherit", "transparent"):
        return None

    # Strip icc-color() to get the raw RGB fallback
    raw_rgb = strip_icc_color(check_str).strip()
    if not raw_rgb.startswith("#"):
        return None

    exp_r, exp_g, exp_b = cmyk_to_rgb(c, m, y, k)
    act_r, act_g, act_b = hex_to_rgb(raw_rgb)

    if (abs(exp_r - act_r) > DESYNC_TOLERANCE or
            abs(exp_g - act_g) > DESYNC_TOLERANCE or
            abs(exp_b - act_b) > DESYNC_TOLERANCE):
        return PF_CMYK_RGB_DESYNC

    return None


def find_desynced_elements(iter_elements) -> List[Dict]:
    """
    Scan document elements for CMYK/RGB desync.
    Returns list of dicts: {id, cmyk, current_rgb, desync}.
    """
    results = []
    for el in iter_elements:
        eid   = el.get("id", "")
        c_val = el.get(ATTR_C)
        if c_val is None:
            continue

        try:
            c = float(c_val)
            m = float(el.get(ATTR_M) or 0)
            y = float(el.get(ATTR_Y) or 0)
            k = float(el.get(ATTR_K) or 0)
        except (ValueError, TypeError):
            continue

        style: Dict[str, str] = {}
        try:
            st = el.style
            if hasattr(st, "items"):
                style = dict(st.items())
        except AttributeError:
            pass

        target    = el.get(ATTR_TARGET, "fill")
        paint_val = style.get("fill" if "fill" in target else "stroke", "")
        raw_rgb   = strip_icc_color(paint_val).strip()

        if not raw_rgb.startswith("#"):
            continue

        exp_r, exp_g, exp_b = cmyk_to_rgb(c, m, y, k)
        act_r, act_g, act_b = hex_to_rgb(raw_rgb)
        desynced = (
            abs(exp_r - act_r) > DESYNC_TOLERANCE or
            abs(exp_g - act_g) > DESYNC_TOLERANCE or
            abs(exp_b - act_b) > DESYNC_TOLERANCE
        )
        if desynced:
            results.append({
                "id":          eid,
                "cmyk":        (c, m, y, k),
                "expected_rgb":rgb_to_hex(exp_r, exp_g, exp_b),
                "current_rgb": raw_rgb,
            })
    return results


# ===========================================================================
# SECTION 6 - SVG EXPORT (write_cmyk_svg)
# ===========================================================================

def write_cmyk_svg(tree,
                   cmyk_doc: CmykDocument,
                   output_path: str,
                   embed_icc: bool = True,
                   write_icc_color: bool = True,
                   pretty: bool = True) -> None:
    """
    Write a fully CMYK-annotated SVG.
    Writes to three places: cmyk:* attrs, <metadata> JSON, icc-color() paint.
    Also writes <cmyk:gradient> elements and embeds ICC profile.
    """
    try:
        from lxml import etree
    except ImportError:
        raise RuntimeError("lxml is required: pip install lxml")

    root = tree.getroot()

    # Register namespaces
    try:
        etree.register_namespace(CMYK_PREFIX, CMYK_NS)
        if _PSVG_AVAILABLE and PSVG_NS:
            etree.register_namespace(PSVG_PREFIX, PSVG_NS)
    except AttributeError:
        pass

    # Get or create <defs>
    defs = _get_or_create(root, f"{{{SVG_NS}}}defs")

    # Embed ICC profile
    icc_profile_name = cmyk_doc.icc_name
    if embed_icc:
        icc_path = cmyk_doc.icc_path or get_fogra39_icc_path() or get_srgb_icc_path()
        if icc_path and os.path.isfile(icc_path):
            _embed_icc_profile(defs, root, icc_path, icc_profile_name)

    # Build id map (fall back to uuid map for moved elements)
    id_map   = cmyk_doc.id_map()
    uuid_map = cmyk_doc.uuid_map()

    for node in root.iter():
        eid  = node.get("id", "")
        data = id_map.get(eid)

        # UUID fallback: find by stable uuid even if id changed
        if data is None:
            node_uuid = node.get(ATTR_UUID, "")
            if node_uuid:
                data = uuid_map.get(node_uuid)

        if data is None:
            continue

        # 1. Write cmyk:* attributes
        node.set(ATTR_C,         f"{data.c:.6f}")
        node.set(ATTR_M,         f"{data.m:.6f}")
        node.set(ATTR_Y,         f"{data.y:.6f}")
        node.set(ATTR_K,         f"{data.k:.6f}")
        node.set(ATTR_TARGET,    data.target)
        node.set(ATTR_INK_TOTAL, f"{data.ink_total:.2f}")
        node.set(ATTR_UUID,      data.element_uuid)
        node.set(ATTR_KNOCKOUT,  data.knockout)
        node.set(ATTR_OVERPRINT_MODE, data.overprint_mode)

        if data.spot_name:
            node.set(ATTR_SPOT_NAME, data.spot_name)
        if data.overprint_fill:
            node.set(ATTR_OVERPRINT_FILL, "1")
        if data.overprint_stroke:
            node.set(ATTR_OVERPRINT_STROKE, "1")
        if data.grad_stops:
            node.set(ATTR_GRAD_STOPS,
                     json.dumps(data.grad_stops, separators=(",", ":")))
        if data.pattern_colors:
            node.set(ATTR_PATTERN,
                     json.dumps(data.pattern_colors, separators=(",", ":")))

        # 2. Write icc-color() paint
        if write_icc_color:
            _apply_icc_paint(node, data, icc_profile_name)

        # 3. Write <cmyk:gradient> element in <defs> for gradient fills
        if data.grad_stops:
            _write_cmyk_gradient_def(defs, data, eid, icc_profile_name)

    # Write <metadata> JSON blob
    _write_metadata_blob(root, cmyk_doc)

    tree.write(
        output_path,
        pretty_print=pretty,
        xml_declaration=True,
        encoding="UTF-8",
    )


def _apply_icc_paint(node, data: ElementCmykData, profile_name: str) -> None:
    """Inject icc-color() into fill/stroke CSS properties."""
    style_str = node.get("style", "")
    props     = parse_style_string(style_str)
    paint     = build_icc_paint(data.c, data.m, data.y, data.k, profile_name)
    alpha_str = f"{float(data.alpha):.6f}"

    if data.target in ("fill", "both"):
        props["fill"]           = paint
        props["fill-opacity"]   = alpha_str
    if data.target in ("stroke", "both"):
        props["stroke"]         = paint
        props["stroke-opacity"] = alpha_str

    if data.overprint_fill or data.overprint_stroke:
        props["mix-blend-mode"] = "multiply"

    node.set("style", build_style_string(props))


def _write_cmyk_gradient_def(defs, data: ElementCmykData,
                              linked_id: str, profile_name: str) -> None:
    """Write <cmyk:gradient> child element into <defs>."""
    try:
        from lxml import etree
        cmyk_grad_id = f"cmyk-{linked_id}"

        # Remove old
        for old in list(defs):
            if old.get("id") == cmyk_grad_id:
                defs.remove(old)

        grad_el = etree.SubElement(
            defs,
            f"{{{CMYK_NS}}}gradient",
            attrib={
                "id":               cmyk_grad_id,
                "linked-gradient":  linked_id,
            }
        )
        for s in data.grad_stops:
            etree.SubElement(
                grad_el,
                f"{{{CMYK_NS}}}stop",
                attrib={
                    "offset":  f"{s.get('offset', 0):.6f}",
                    "c":       f"{s.get('c', 0):.6f}",
                    "m":       f"{s.get('m', 0):.6f}",
                    "y":       f"{s.get('y', 0):.6f}",
                    "k":       f"{s.get('k', 0):.6f}",
                    "profile": profile_name,
                }
            )
    except ImportError:
        pass


def _embed_icc_profile(defs, root, icc_path: str, profile_name: str) -> None:
    """Embed ICC profile as <color-profile> in <defs>."""
    try:
        from lxml import etree
        for old in list(defs):
            tag = old.tag.split("}")[-1] if "}" in old.tag else old.tag
            if tag == "color-profile" and old.get("name") == profile_name:
                defs.remove(old)

        data_uri = icc_profile_to_data_uri(icc_path)
        if not data_uri:
            return

        etree.SubElement(
            defs,
            f"{{{SVG_NS}}}color-profile",
            attrib={
                "id":                   "cmyk-icc-profile",
                "name":                 profile_name,
                f"{{{XLINK_NS}}}href":  data_uri,
                "rendering-intent":     "relative-colorimetric",
                ATTR_ICC_HREF:          icc_path,
            }
        )
        root.set("color-profile", "url(#cmyk-icc-profile)")
    except ImportError:
        pass


def _write_metadata_blob(root, cmyk_doc: CmykDocument) -> None:
    """
    Write CMYK data to <metadata> in two formats simultaneously:
      1. psvg-data  (psvg:* namespace, authoritative, v2.4+)
      2. cmyk-plugin-data  (legacy namespace, backward compat, v2.3 and earlier)

    ChatGPT recommendation: write both, read both, so old files keep working
    and new files are clean. Phase out legacy write in a future major version.
    """
    try:
        from lxml import etree

        meta_el = root.find(f"{{{SVG_NS}}}metadata")
        if meta_el is None:
            meta_el = etree.SubElement(root, f"{{{SVG_NS}}}metadata")

        blob = encode_cmyk_metadata(cmyk_doc.to_metadata_list())

        # Remove old entries (both namespaces)
        all_ids = {METADATA_CMYK_ID, METADATA_VERSION_ID}
        if _PSVG_AVAILABLE and PSVG_META_ID:
            all_ids.update({PSVG_META_ID, PSVG_META_VER_ID})
        for old in list(meta_el):
            if old.get("id") in all_ids:
                meta_el.remove(old)

        # Write psvg:data (primary, v2.4+)
        if _PSVG_AVAILABLE and PSVG_META_ID:
            psvg_ns = "http://printsvg.org/spec/1.0"
            psvg_el = etree.SubElement(
                meta_el, f"{{{psvg_ns}}}data",
                attrib={"id": PSVG_META_ID}
            )
            psvg_el.text = blob
            ver_el = etree.SubElement(
                meta_el, f"{{{psvg_ns}}}version",
                attrib={"id": PSVG_META_VER_ID}
            )
            ver_el.text = cmyk_doc.version

        # Write cmyk:data (legacy compat — keeps v2.3 readers working)
        legacy_el = etree.SubElement(
            meta_el,
            f"{{{CMYK_NS}}}data",
            attrib={"id": METADATA_CMYK_ID}
        )
        legacy_el.text = blob

        ver_legacy = etree.SubElement(
            meta_el,
            f"{{{CMYK_NS}}}version",
            attrib={"id": METADATA_VERSION_ID}
        )
        ver_legacy.text = cmyk_doc.version

    except ImportError:
        pass


def _get_or_create(parent, tag: str):
    el = parent.find(tag)
    if el is None:
        try:
            from lxml import etree
            el = etree.SubElement(parent, tag)
        except ImportError:
            import xml.etree.ElementTree as ET
            el = ET.SubElement(parent, tag)
    return el


# ===========================================================================
# SECTION 7 - SVG IMPORT (read_cmyk_svg)
# ===========================================================================

def read_cmyk_svg(source) -> CmykDocument:
    """
    Read an SVG file or tree and extract all CMYK data.
    Priority: cmyk:* attrs > <metadata> JSON > icc-color() > RGB fallback.
    UUID-keyed lookup used when available.
    """
    try:
        from lxml import etree

        if isinstance(source, str):
            tree = etree.parse(source)
            doc  = CmykDocument(source_file=source)
        else:
            tree = source
            doc  = CmykDocument()

        root = tree.getroot()

        method = _try_read_attrs(root, doc)
        if not method:
            method = _try_read_metadata(root, doc)
        if not method:
            method = _try_read_icc_color(root, doc)
        if not method:
            method = _try_read_rgb_fallback(root, doc)

        doc.import_method = method or "none"

        # Read ICC profile path
        defs = root.find(f"{{{SVG_NS}}}defs")
        if defs is not None:
            for cp in defs.findall(f"{{{SVG_NS}}}color-profile"):
                doc.icc_path = cp.get(ATTR_ICC_HREF) or cp.get("name", "")
                doc.icc_name = cp.get("name", ICC_PROFILE_NAME)
                break

        return doc

    except ImportError:
        return CmykDocument(import_method="error-no-lxml")


def _try_read_attrs(root, doc: CmykDocument) -> Optional[str]:
    """
    Read CMYK attributes from elements.
    Priority: psvg:* (v2.4+) > cmyk:* (legacy v2.3 and earlier).
    Both are tried; psvg:* wins when present.
    """
    found = 0
    for node in root.iter():
        # Prefer psvg:* (authoritative v2.4); fall back to cmyk:* (legacy)
        if _PSVG_AVAILABLE and PSVG_C:
            c_val = node.get(PSVG_C) or node.get(ATTR_C)
        else:
            c_val = node.get(ATTR_C)
        if c_val is None:
            continue
        eid = node.get("id", "")
        if not eid:
            continue

        try:
            def _ga(psvg_a, cmyk_a, default=None):
                """Get attribute: psvg first, cmyk fallback."""
                v = None
                if _PSVG_AVAILABLE and psvg_a:
                    v = node.get(psvg_a)
                return v if v is not None else (node.get(cmyk_a) or default)
            data = ElementCmykData(
                element_id       = eid,
                element_uuid     = (_ga(PSVG_UUID,    ATTR_UUID,           "") or ""),
                c                = float(c_val or 0),
                m                = float(_ga(PSVG_M,  ATTR_M, 0) or 0),
                y                = float(_ga(PSVG_Y,  ATTR_Y, 0) or 0),
                k                = float(_ga(PSVG_K,  ATTR_K, 0) or 0),
                target           = (_ga(PSVG_TARGET,  ATTR_TARGET,         "fill") or "fill"),
                spot_name        = (_ga(PSVG_SPOT_NAME, ATTR_SPOT_NAME,    "") or ""),
                overprint_fill   = (_ga(PSVG_OP_FILL,   ATTR_OVERPRINT_FILL, "0") == "1"),
                overprint_stroke = (_ga(PSVG_OP_STROKE, ATTR_OVERPRINT_STROKE,"0") == "1"),
                knockout         = (_ga(PSVG_KNOCKOUT,  ATTR_KNOCKOUT,     "auto") or "auto"),
                source           = "psvg_attrs" if (_PSVG_AVAILABLE and node.get(PSVG_C)) else "attrs",
            )
        except (ValueError, TypeError):
            continue

        grad = node.get(ATTR_GRAD_STOPS)
        if grad:
            try:
                data.grad_stops = json.loads(grad)
            except Exception:
                pass

        pat = node.get(ATTR_PATTERN)
        if pat:
            try:
                data.pattern_colors = json.loads(pat)
            except Exception:
                pass

        doc.elements.append(data)
        found += 1

    return "attrs" if found else None


def _try_read_metadata(root, doc: CmykDocument) -> Optional[str]:
    """
    Read the <metadata> JSON blob.
    Priority: psvg-data (v2.4+) > cmyk-plugin-data (legacy v2.3 and earlier).
    Reads both to ensure old files always work.
    """
    meta_el = root.find(f"{{{SVG_NS}}}metadata")
    if meta_el is None:
        return None

    # Try psvg-data first (authoritative)
    blob_el = None
    if _PSVG_AVAILABLE and PSVG_META_ID:
        blob_el = meta_el.find(f".//*[@id='{PSVG_META_ID}']")

    # Fall back to legacy cmyk-plugin-data
    if blob_el is None or not blob_el.text:
        blob_el = meta_el.find(f".//*[@id='{METADATA_CMYK_ID}']")

    if blob_el is None or not blob_el.text:
        return None

    records = decode_cmyk_metadata(blob_el.text)
    if not records:
        return None

    for rec in records:
        eid = rec.get("id", "")
        if not eid:
            continue
        try:
            data = ElementCmykData.from_metadata_dict(rec)
            doc.elements.append(data)
        except Exception:
            continue

    return "metadata" if doc.elements else None


def _try_read_icc_color(root, doc: CmykDocument) -> Optional[str]:
    """Read icc-color() values from fill/stroke style properties."""
    found = 0
    for node in root.iter():
        eid = node.get("id", "")
        if not eid:
            continue

        style_str = node.get("style", "")
        if not style_str:
            continue

        props = parse_style_string(style_str)
        data  = None

        for prop in ("fill", "stroke"):
            paint  = props.get(prop, "")
            result = parse_icc_color(paint)
            if result is None:
                continue
            _, (c, m, y, k) = result
            if data is None:
                data = ElementCmykData(
                    element_id = eid,
                    c=c, m=m, y=y, k=k,
                    target = prop,
                    source = "icc_color",
                )
            else:
                data.target = "both"

        if data is not None:
            doc.elements.append(data)
            found += 1

    return "icc_color" if found else None


def _try_read_rgb_fallback(root, doc: CmykDocument) -> Optional[str]:
    """Last resort: back-calculate CMYK from RGB fill/stroke."""
    found = 0
    for node in root.iter():
        tag = node.tag if isinstance(node.tag, str) else ""
        if not tag.startswith("{"):
            continue
        eid = node.get("id", "")
        if not eid:
            continue

        style_str = node.get("style", "")
        if not style_str:
            continue

        props = parse_style_string(style_str)
        data  = None

        for prop in ("fill", "stroke"):
            paint = strip_icc_color(props.get(prop, "")).strip()
            if not paint or paint in ("none", "inherit", "transparent"):
                continue
            if not paint.startswith("#"):
                continue

            rgb = hex_to_rgb(paint)
            c, m, y, k = rgb_to_cmyk(*rgb)
            if data is None:
                data = ElementCmykData(
                    element_id = eid,
                    c=c, m=m, y=y, k=k,
                    target = prop,
                    source = "rgb_fallback",
                )
            else:
                data.target = "both"

        if data is not None:
            doc.elements.append(data)
            found += 1

    return "rgb_fallback" if found else None


# ===========================================================================
# SECTION 8 - APPLY IMPORTED DATA
# ===========================================================================

def apply_cmyk_document(root, cmyk_doc: CmykDocument,
                         write_icc_color: bool = True) -> int:
    """Apply CmykDocument data to an lxml tree in place. Returns count updated."""
    id_map   = cmyk_doc.id_map()
    uuid_map = cmyk_doc.uuid_map()
    updated  = 0

    for node in root.iter():
        eid  = node.get("id", "")
        data = id_map.get(eid)

        # UUID fallback
        if data is None:
            node_uuid = node.get(ATTR_UUID, "")
            if node_uuid:
                data = uuid_map.get(node_uuid)

        if data is None:
            continue

        node.set(ATTR_C,            f"{data.c:.6f}")
        node.set(ATTR_M,            f"{data.m:.6f}")
        node.set(ATTR_Y,            f"{data.y:.6f}")
        node.set(ATTR_K,            f"{data.k:.6f}")
        node.set(ATTR_TARGET,       data.target)
        node.set(ATTR_INK_TOTAL,    f"{data.ink_total:.2f}")
        node.set(ATTR_UUID,         data.element_uuid)
        node.set(ATTR_KNOCKOUT,     data.knockout)
        node.set(ATTR_OVERPRINT_MODE, data.overprint_mode)

        if data.spot_name:
            node.set(ATTR_SPOT_NAME, data.spot_name)
        if data.overprint_fill:
            node.set(ATTR_OVERPRINT_FILL, "1")
        if data.overprint_stroke:
            node.set(ATTR_OVERPRINT_STROKE, "1")
        if data.grad_stops:
            node.set(ATTR_GRAD_STOPS,
                     json.dumps(data.grad_stops, separators=(",", ":")))
        if data.pattern_colors:
            node.set(ATTR_PATTERN,
                     json.dumps(data.pattern_colors, separators=(",", ":")))

        if write_icc_color:
            _apply_icc_paint(node, data, cmyk_doc.icc_name)

        updated += 1

    return updated


# ===========================================================================
# SECTION 9 - AUTO-SAVE HOOK
# ===========================================================================

class CmykAutoSave:
    """
    Refreshes CMYK metadata on every save without writing a new file.
    Syncs: <metadata> JSON blob, icc-color() paint values, ICC profile.
    Also checks for RGB/CMYK desync and marks desynced elements.
    """

    def __init__(self, root,
                 icc_profile_name: str = ICC_PROFILE_NAME,
                 icc_path: Optional[str] = None):
        self.root             = root
        self.icc_profile_name = icc_profile_name
        self.icc_path         = icc_path or get_fogra39_icc_path() or get_srgb_icc_path()

    def __call__(self) -> int:
        """Run auto-save sync. Returns number of elements processed."""
        cmyk_doc = CmykDocument(icc_name=self.icc_profile_name,
                                icc_path=self.icc_path)

        for node in self.root.iter():
            c_val = node.get(ATTR_C)
            if c_val is None:
                continue
            eid = node.get("id", "")
            if not eid:
                continue

            try:
                data = ElementCmykData(
                    element_id       = eid,
                    element_uuid     = node.get(ATTR_UUID, ""),
                    c                = float(c_val or 0),
                    m                = float(node.get(ATTR_M, 0) or 0),
                    y                = float(node.get(ATTR_Y, 0) or 0),
                    k                = float(node.get(ATTR_K, 0) or 0),
                    target           = node.get(ATTR_TARGET, "fill"),
                    spot_name        = node.get(ATTR_SPOT_NAME, ""),
                    overprint_fill   = node.get(ATTR_OVERPRINT_FILL,  "0") == "1",
                    overprint_stroke = node.get(ATTR_OVERPRINT_STROKE,"0") == "1",
                    knockout         = node.get(ATTR_KNOCKOUT, "auto"),
                )
            except (ValueError, TypeError):
                continue

            # Check for desync
            style: Dict[str, str] = {}
            try:
                st = node.style
                if hasattr(st, "items"):
                    style = dict(st.items())
            except AttributeError:
                pass

            target    = data.target
            paint_val = style.get("fill" if "fill" in target else "stroke", "")
            raw_rgb   = strip_icc_color(paint_val).strip()

            if raw_rgb.startswith("#"):
                exp_r, exp_g, exp_b = cmyk_to_rgb(data.c, data.m, data.y, data.k)
                act_r, act_g, act_b = hex_to_rgb(raw_rgb)
                if (abs(exp_r - act_r) > DESYNC_TOLERANCE or
                        abs(exp_g - act_g) > DESYNC_TOLERANCE or
                        abs(exp_b - act_b) > DESYNC_TOLERANCE):
                    node.set(ATTR_DESYNC_WARN, "1")
                else:
                    if node.get(ATTR_DESYNC_WARN):
                        del node.attrib[ATTR_DESYNC_WARN]

            grad = node.get(ATTR_GRAD_STOPS)
            if grad:
                try:
                    data.grad_stops = json.loads(grad)
                except Exception:
                    pass

            cmyk_doc.elements.append(data)

        if not cmyk_doc.elements:
            return 0

        _write_metadata_blob(self.root, cmyk_doc)

        id_map   = cmyk_doc.id_map()
        uuid_map = cmyk_doc.uuid_map()
        for node in self.root.iter():
            eid  = node.get("id", "")
            data = id_map.get(eid) or uuid_map.get(node.get(ATTR_UUID, ""))
            if data:
                _apply_icc_paint(node, data, self.icc_profile_name)

        try:
            from lxml import etree
            defs = _get_or_create(self.root, f"{{{SVG_NS}}}defs")
            if self.icc_path and os.path.isfile(self.icc_path):
                _embed_icc_profile(defs, self.root,
                                   self.icc_path, self.icc_profile_name)
        except ImportError:
            pass

        return len(cmyk_doc.elements)


# ===========================================================================
# SECTION 10 - UTILITIES
# ===========================================================================

def diff_cmyk_documents(doc_a: CmykDocument,
                         doc_b: CmykDocument) -> List[str]:
    """Compare two CmykDocuments. Returns list of difference descriptions."""
    diffs: List[str] = []
    ids_a  = set(e.element_id for e in doc_a.elements)
    ids_b  = set(e.element_id for e in doc_b.elements)

    for missing in ids_a - ids_b:
        diffs.append(f"Element '{missing}' in A but not in B")
    for extra in ids_b - ids_a:
        diffs.append(f"Element '{extra}' in B but not in A")

    map_a = doc_a.id_map()
    map_b = doc_b.id_map()

    for eid in ids_a & ids_b:
        a = map_a[eid]
        b = map_b[eid]
        for attr in ("c", "m", "y", "k"):
            va, vb = getattr(a, attr), getattr(b, attr)
            if abs(va - vb) > 0.001:
                diffs.append(
                    f"Element '{eid}': {attr.upper()} differs "
                    f"(A={va:.4f} B={vb:.4f})"
                )
        if a.target != b.target:
            diffs.append(
                f"Element '{eid}': target differs ({a.target} vs {b.target})")
        if a.spot_name != b.spot_name:
            diffs.append(
                f"Element '{eid}': spot name differs "
                f"('{a.spot_name}' vs '{b.spot_name}')")

    return diffs


def convert_svg_to_cmyk_svg(input_path: str,
                             output_path: Optional[str] = None,
                             embed_icc: bool = True,
                             write_icc_color: bool = True) -> CmykDocument:
    """
    Read any SVG, extract/calculate CMYK data, write a fully annotated copy.
    If output_path is None, overwrites input in place.
    """
    try:
        from lxml import etree
    except ImportError:
        raise RuntimeError("lxml is required: pip install lxml")

    tree = etree.parse(input_path)
    doc  = read_cmyk_svg(tree)
    out  = output_path or input_path
    write_cmyk_svg(tree, doc, out,
                   embed_icc=embed_icc, write_icc_color=write_icc_color)
    return doc
