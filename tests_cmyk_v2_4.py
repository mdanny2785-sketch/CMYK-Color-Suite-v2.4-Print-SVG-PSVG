#!/usr/bin/env python3
"""
tests_cmyk_v2_4.py  -  Test suite for CMYK plugin v2.4
=======================================================
Section A - PDFXMode (GS args, flattening requirement)
Section B - Transparency detection
Section C - Spot separation mapping
Section D - PSVG schema validation
Section E - Namespace migration (cmyk:* -> psvg:*)
Section F - Soft proof colour math (Fogra39 matrix)
Section G - Regression: v2.3 IO still works

Run:  python3 tests_cmyk_v2_4.py -v
"""

import io
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "extension"))

from cmyk_psvg import (
    PDFXMode, PSVG_NS, PSVG_PREFIX, PSVG_VERSION,
    PSVG_C, PSVG_M, PSVG_Y, PSVG_K, PSVG_TARGET,
    PSVG_SPOT_NAME, PSVG_UUID, PSVG_OP_FILL,
    PSVG_TRANS_PRESENT, PSVG_DOC_VERSION,
    LEGACY_ATTR_MAP, CMYK_NS_LEGACY,
    TransparencyInfo, detect_transparency,
    SpotSeparation, SeparationMap,
    build_separation_map_from_elements,
    PSVGValidationReport, PSVGValidationIssue,
    validate_psvg_document,
    migrate_cmyk_to_psvg,
    build_gs_flatten_args,
    PSVG_ERR_TRANSPARENCY_X1A,
    PSVG_WARN_LEGACY_NAMESPACE,
    PSVG_WARN_UNRESOLVED_SPOT,
    PSVG_ERR_MISSING_UUID,
)
from cmyk_core import (
    PLUGIN_VERSION, SPOT_COLORS,
    cmyk_to_rgb, rgb_to_cmyk,
    PF_TRANSPARENCY_X1A, PF_SPOT_NOT_SEPARATED, PF_CMYK_RGB_DESYNC,
)
from cmyk_io import (
    soft_proof_cmyk_to_srgb,
    ElementCmykData, CmykDocument,
)

try:
    from lxml import etree as ET
    LXML = True
except ImportError:
    LXML = False

SKIP_LXML = unittest.skipUnless(LXML, "lxml required")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class FakeEl:
    def __init__(self, tag="{http://www.w3.org/2000/svg}rect",
                 attribs=None, style=""):
        self.tag    = tag
        self._a     = attribs or {}
        self._style = style
    def get(self, k, d=None): return self._a.get(k, d)
    @property
    def attrib(self): return self._a

def _cmyk_el(c,m,y,k,eid="e1",spot="",style="fill:#ff0000"):
    ns = CMYK_NS_LEGACY
    a  = {
        "id": eid,
        f"{{{ns}}}c": str(c), f"{{{ns}}}m": str(m),
        f"{{{ns}}}y": str(y), f"{{{ns}}}k": str(k),
    }
    if spot: a[f"{{{ns}}}spot-name"] = spot
    return FakeEl(attribs=a, style=style)


# ===========================================================================
# SECTION A - PDFXMode
# ===========================================================================

class TestPDFXMode(unittest.TestCase):

    def test_x1a_args_contain_flatten(self):
        args = PDFXMode.ghostscript_args(PDFXMode.X1A)
        self.assertTrue(any("Flatten" in a or "flatten" in a for a in args))

    def test_x1a_uses_pdf_13(self):
        args = PDFXMode.ghostscript_args(PDFXMode.X1A)
        self.assertTrue(any("1.3" in a for a in args))

    def test_x4_uses_pdf_16(self):
        args = PDFXMode.ghostscript_args(PDFXMode.X4)
        self.assertTrue(any("1.6" in a for a in args))

    def test_requires_flattening_x1a(self):
        self.assertTrue(PDFXMode.requires_flattening(PDFXMode.X1A))

    def test_requires_flattening_x4_false(self):
        self.assertFalse(PDFXMode.requires_flattening(PDFXMode.X4))

    def test_requires_flattening_x3_false(self):
        self.assertFalse(PDFXMode.requires_flattening(PDFXMode.X3))

    def test_all_modes_have_descriptions(self):
        for mode in (PDFXMode.X1A, PDFXMode.X3, PDFXMode.X4):
            self.assertIn(mode, PDFXMode.DESCRIPTIONS)
            self.assertTrue(len(PDFXMode.DESCRIPTIONS[mode]) > 10)

    def test_none_mode_returns_basic_args(self):
        args = PDFXMode.ghostscript_args(PDFXMode.NONE)
        self.assertTrue(any("CMYK" in a for a in args))

    def test_all_modes_contain_cmyk(self):
        for mode in (PDFXMode.X1A, PDFXMode.X3, PDFXMode.X4, PDFXMode.NONE):
            args = PDFXMode.ghostscript_args(mode)
            self.assertTrue(any("CMYK" in a for a in args),
                            msg=f"Mode {mode} missing CMYK arg")


class TestGsFlattenArgs(unittest.TestCase):

    def test_returns_list(self):
        args = build_gs_flatten_args()
        self.assertIsInstance(args, list)
        self.assertGreater(len(args), 0)

    def test_includes_flatten_flag(self):
        args = build_gs_flatten_args()
        self.assertTrue(any("Flatten" in a for a in args))

    def test_pdf_13_required(self):
        args = build_gs_flatten_args()
        self.assertTrue(any("1.3" in a for a in args))

    def test_icc_path_included_when_given(self):
        args = build_gs_flatten_args(icc_path="/path/to/profile.icc")
        self.assertTrue(any("profile.icc" in a for a in args))

    def test_icc_omitted_when_none(self):
        args = build_gs_flatten_args(icc_path=None)
        self.assertFalse(any("icc" in a.lower() for a in args))


# ===========================================================================
# SECTION B - Transparency detection
# ===========================================================================

class TestTransparencyInfo(unittest.TestCase):

    def _info(self, **kw):
        return TransparencyInfo(element_id="e1", **kw)

    def test_has_transparency_when_opacity(self):
        info = self._info(has_opacity=True, opacity_value=0.5)
        self.assertTrue(info.has_transparency)

    def test_has_transparency_when_blend(self):
        info = self._info(has_blend=True, blend_mode="multiply")
        self.assertTrue(info.has_transparency)

    def test_no_transparency_default(self):
        info = TransparencyInfo(element_id="e1")
        self.assertFalse(info.has_transparency)

    def test_severity_error_for_opacity(self):
        info = self._info(has_opacity=True, opacity_value=0.5)
        self.assertEqual(info.severity, "error")

    def test_severity_error_for_blend(self):
        info = self._info(has_blend=True, blend_mode="multiply")
        self.assertEqual(info.severity, "error")

    def test_flattening_advice_not_empty(self):
        info = self._info(has_opacity=True, opacity_value=0.5)
        advice = info.flattening_advice()
        self.assertIn("opacity", advice)

    def test_flattening_advice_empty_when_clean(self):
        info = TransparencyInfo(element_id="e1")
        self.assertEqual(info.flattening_advice(), "no action needed")


class TestDetectTransparency(unittest.TestCase):

    def _el(self, style="", **attrs):
        el = FakeEl(attribs={"id":"e1", **attrs}, style=style)
        # Make style a real string on the object
        el._style = style
        # lxml-style: el.get("style") returns style string
        el._a["style"] = style
        return el

    def test_detects_opacity(self):
        el      = self._el(style="fill:#ff0000;opacity:0.5")
        results = list(detect_transparency([el]))
        self.assertEqual(len(results), 1)
        self.assertTrue(results[0].has_opacity)
        self.assertAlmostEqual(results[0].opacity_value, 0.5)

    def test_ignores_full_opacity(self):
        el      = self._el(style="fill:#ff0000;opacity:1.0")
        results = list(detect_transparency([el]))
        self.assertEqual(len(results), 0)

    def test_detects_blend_mode(self):
        el      = self._el(style="fill:#ff0000;mix-blend-mode:multiply")
        results = list(detect_transparency([el]))
        self.assertEqual(len(results), 1)
        self.assertTrue(results[0].has_blend)
        self.assertEqual(results[0].blend_mode, "multiply")

    def test_normal_blend_not_flagged(self):
        el      = self._el(style="fill:#ff0000;mix-blend-mode:normal")
        results = list(detect_transparency([el]))
        self.assertEqual(len(results), 0)

    def test_detects_mask(self):
        el      = self._el(style="fill:#ff0000;mask:url(#m1)")
        results = list(detect_transparency([el]))
        self.assertEqual(len(results), 1)
        self.assertTrue(results[0].has_mask)

    def test_clean_element_not_flagged(self):
        el      = self._el(style="fill:#ff0000;stroke:none")
        results = list(detect_transparency([el]))
        self.assertEqual(len(results), 0)

    def test_multiple_elements(self):
        els = [
            self._el(style="opacity:0.5"),
            self._el(style="fill:#000"),
            self._el(style="mix-blend-mode:screen"),
        ]
        els[1]._a["id"] = "e2"
        els[2]._a["id"] = "e3"
        results = list(detect_transparency(els))
        self.assertEqual(len(results), 2)


# ===========================================================================
# SECTION C - Spot separation mapping
# ===========================================================================

class TestSpotSeparation(unittest.TestCase):

    def _sep(self, **kw):
        return SpotSeparation(
            spot_name="PANTONE 485 C",
            cmyk=(0, 0.95, 1.0, 0),
            **kw
        )

    def test_to_dict_keys(self):
        d = self._sep().to_dict()
        for key in ("name","cmyk","mode","plate","density_pct"):
            self.assertIn(key, d)

    def test_default_mode_separated(self):
        self.assertEqual(self._sep().mode, "separated")

    def test_gs_arg_is_name(self):
        sep = self._sep(plate_name="PMS485")
        self.assertEqual(sep.ghostscript_sep_arg(), "PMS485")

    def test_gs_arg_fallback_to_spot_name(self):
        sep = self._sep()
        self.assertIn("PANTONE", sep.ghostscript_sep_arg())


class TestSeparationMap(unittest.TestCase):

    def _map(self):
        m = SeparationMap()
        m.spots = [
            SpotSeparation("PANTONE 485 C",  (0,0.95,1,0),  "separated"),
            SpotSeparation("PANTONE 286 C",  (1,0.75,0,0.02),"separated"),
        ]
        return m

    def test_all_plate_names_includes_process(self):
        names = self._map().all_plate_names()
        for plate in ("Cyan","Magenta","Yellow","Black"):
            self.assertIn(plate, names)

    def test_all_plate_names_includes_spots(self):
        names = self._map().all_plate_names()
        self.assertIn("PANTONE 485 C", names)

    def test_gs_separation_args_format(self):
        args = self._map().ghostscript_separation_args()
        self.assertIn("-sDEVICE=tiffsep", args)

    def test_scribus_color_defs_xml(self):
        xml = self._map().to_scribus_color_defs()
        self.assertIn("PANTONE 485 C", xml)
        self.assertIn('Spot="1"', xml)

    def test_validation_no_issues_on_clean_map(self):
        issues = self._map().validation_report()
        self.assertEqual(issues, [])

    def test_validation_detects_mismatch(self):
        m = SeparationMap()
        m.spots = [
            SpotSeparation("PANTONE 485 C", (0, 0.95, 1, 0), "separated"),
            SpotSeparation("PANTONE 485 C", (0, 0.50, 1, 0), "separated"),
        ]
        issues = m.validation_report()
        self.assertTrue(any("MISMATCH" in i for i in issues))


class TestBuildSeparationMap(unittest.TestCase):

    def test_builds_from_elements(self):
        el = _cmyk_el(0, 0.95, 1.0, 0, eid="r1", spot="PANTONE 485 C")
        sep_map = build_separation_map_from_elements([el], SPOT_COLORS)
        self.assertEqual(len(sep_map.spots), 1)
        self.assertEqual(sep_map.spots[0].spot_name, "PANTONE 485 C")
        self.assertEqual(sep_map.spots[0].mode, "separated")

    def test_unknown_spot_mode_process(self):
        el = _cmyk_el(0.1, 0.2, 0.3, 0.4, eid="r1", spot="CUSTOM ORANGE")
        sep_map = build_separation_map_from_elements([el], SPOT_COLORS)
        self.assertEqual(sep_map.spots[0].mode, "process")

    def test_no_spots_empty_list(self):
        el = _cmyk_el(0, 0, 0, 1, eid="r1")
        sep_map = build_separation_map_from_elements([el], SPOT_COLORS)
        self.assertEqual(len(sep_map.spots), 0)


# ===========================================================================
# SECTION D - PSVG schema validation
# ===========================================================================

class TestPSVGValidation(unittest.TestCase):

    def _validate(self, elements, doc_attrs=None, pdfx=PDFXMode.NONE):
        da = doc_attrs or {}
        return validate_psvg_document(
            lambda a, d="": da.get(a, d),
            elements,
            pdfx_mode=pdfx,
            spot_colors_table=SPOT_COLORS,
        )

    def test_clean_doc_passes(self):
        el = _cmyk_el(0, 0.95, 1, 0, eid="r1")
        el._a[PSVG_UUID] = "test-uuid"
        report = self._validate([el])
        # May have warnings (no bleed, legacy ns) but should not error
        self.assertEqual(len(report.errors()), 0)

    def test_transparency_x1a_flagged(self):
        el = FakeEl(
            attribs={"id":"e1"},
            style="fill:#ff0000;opacity:0.5"
        )
        el._a["style"] = "fill:#ff0000;opacity:0.5"
        report = self._validate([el], pdfx=PDFXMode.X1A)
        codes  = [i.code for i in report.errors()]
        self.assertIn(PSVG_ERR_TRANSPARENCY_X1A, codes)

    def test_transparency_x4_not_flagged(self):
        el = FakeEl(
            attribs={"id":"e1"},
            style="fill:#ff0000;opacity:0.5"
        )
        el._a["style"] = "fill:#ff0000;opacity:0.5"
        report = self._validate([el], pdfx=PDFXMode.X4)
        codes  = [i.code for i in report.errors()]
        self.assertNotIn(PSVG_ERR_TRANSPARENCY_X1A, codes)

    def test_legacy_namespace_warned(self):
        el = _cmyk_el(0, 0, 0, 1, eid="r1")  # uses cmyk:* attrs
        report = self._validate([el])
        codes  = [i.code for i in report.warnings()]
        self.assertIn(PSVG_WARN_LEGACY_NAMESPACE, codes)

    def test_unresolved_spot_warned(self):
        el = _cmyk_el(0, 0.95, 1, 0, eid="r1", spot="PANTONE IMAGINARY C")
        report = self._validate([el])
        codes  = [i.code for i in report.warnings()]
        self.assertIn(PSVG_WARN_UNRESOLVED_SPOT, codes)

    def test_known_spot_not_warned(self):
        el = _cmyk_el(0, 0.95, 1, 0, eid="r1", spot="PANTONE 485 C")
        report = self._validate([el])
        codes  = [i.code for i in report.warnings()]
        self.assertNotIn(PSVG_WARN_UNRESOLVED_SPOT, codes)

    def test_missing_uuid_warned(self):
        el = _cmyk_el(0, 0, 0, 1, eid="r1")  # no uuid
        report = self._validate([el])
        codes  = [i.code for i in report.issues]
        self.assertIn(PSVG_ERR_MISSING_UUID, codes)

    def test_validation_report_to_text(self):
        el = _cmyk_el(0, 0.95, 1, 0, eid="r1")
        report = self._validate([el])
        text   = report.to_text()
        self.assertIn("PSVG Schema Validation", text)

    def test_passed_false_on_x1a_error(self):
        el = FakeEl(attribs={"id":"e1"})
        el._a["style"] = "fill:#ff0000;opacity:0.5"
        report = self._validate([el], pdfx=PDFXMode.X1A)
        self.assertFalse(report.passed)


# ===========================================================================
# SECTION E - Namespace migration
# ===========================================================================

@unittest.skipUnless(LXML, "lxml required")
class TestMigrateCmykToPsvg(unittest.TestCase):

    def _make_tree(self):
        ns = CMYK_NS_LEGACY
        xml = (
            f'<?xml version="1.0"?>'
            f'<svg xmlns="http://www.w3.org/2000/svg" '
            f'    xmlns:cmyk="{ns}">'
            f'  <rect id="r1" cmyk:c="0" cmyk:m="0.95" cmyk:y="1" cmyk:k="0" '
            f'       cmyk:target="fill" style="fill:#ff0000"/>'
            f'  <rect id="r2" cmyk:c="0" cmyk:m="0" cmyk:y="0" cmyk:k="1" '
            f'       cmyk:target="fill" style="fill:#000000"/>'
            f'</svg>'
        )
        from lxml import etree
        return etree.parse(io.BytesIO(xml.encode()))

    def test_migrates_attrs(self):
        tree = self._make_tree()
        root = tree.getroot()
        n    = migrate_cmyk_to_psvg(root)
        self.assertEqual(n, 2)
        r1 = root.find('.//*[@id="r1"]')
        # psvg:c should now be set
        self.assertIsNotNone(r1.get(PSVG_C))
        self.assertAlmostEqual(float(r1.get(PSVG_M)), 0.95, places=2)

    def test_removes_legacy_attrs(self):
        tree = self._make_tree()
        root = tree.getroot()
        migrate_cmyk_to_psvg(root)
        r1 = root.find('.//*[@id="r1"]')
        ns = CMYK_NS_LEGACY
        self.assertIsNone(r1.get(f"{{{ns}}}c"))

    def test_all_known_attrs_migrated(self):
        # Every attr in LEGACY_ATTR_MAP should be migrated
        ns = CMYK_NS_LEGACY
        from lxml import etree
        xml = (
            f'<svg xmlns="http://www.w3.org/2000/svg" xmlns:cmyk="{ns}">'
            f'  <rect id="r1"'
        )
        for old_attr in LEGACY_ATTR_MAP:
            local = old_attr.split("}")[-1]
            xml += f' cmyk:{local}="test"'
        xml += ' style="fill:#ff0000"/></svg>'
        tree = etree.parse(io.BytesIO(xml.encode()))
        root = tree.getroot()
        migrate_cmyk_to_psvg(root)
        r1   = root.find('.//*[@id="r1"]')
        for old_attr in LEGACY_ATTR_MAP:
            self.assertIsNone(r1.get(old_attr),
                             msg=f"Legacy attr {old_attr} not removed")


# ===========================================================================
# SECTION F - Soft proof colour math
# ===========================================================================

class TestSoftProofCmykToSrgb(unittest.TestCase):

    def test_black_is_dark(self):
        r, g, b = soft_proof_cmyk_to_srgb(0, 0, 0, 1)
        self.assertLess(r, 30)
        self.assertLess(g, 30)
        self.assertLess(b, 30)

    def test_white_is_white(self):
        r, g, b = soft_proof_cmyk_to_srgb(0, 0, 0, 0)
        self.assertGreater(r, 240)
        self.assertGreater(g, 240)
        self.assertGreater(b, 240)

    def test_output_in_range(self):
        for c in (0, 0.5, 1.0):
            for k in (0, 0.5, 1.0):
                r, g, b = soft_proof_cmyk_to_srgb(c, 0, 0, k)
                for ch in (r, g, b):
                    self.assertGreaterEqual(ch, 0)
                    self.assertLessEqual(ch, 255)

    def test_cyan_shifts_green_blue(self):
        # Cyan ink: predominantly shifts the cyan channel
        r0, g0, b0 = soft_proof_cmyk_to_srgb(0,   0, 0, 0)  # white
        r1, g1, b1 = soft_proof_cmyk_to_srgb(1.0, 0, 0, 0)  # full cyan
        # Cyan should reduce red significantly
        self.assertLess(r1, r0 - 100)

    def test_more_accurate_than_bare_formula(self):
        # Pantone 485 C: C=0 M=0.95 Y=1.0 K=0
        # Soft proof should give a different (more accurate) result than bare formula
        r_bare, g_bare, b_bare = cmyk_to_rgb(0, 0.95, 1.0, 0)
        r_soft, g_soft, b_soft = soft_proof_cmyk_to_srgb(0, 0.95, 1.0, 0)
        # They should differ — the whole point of soft proofing
        diffs = (abs(r_bare-r_soft) + abs(g_bare-g_soft) + abs(b_bare-b_soft))
        self.assertGreater(diffs, 0)


# ===========================================================================
# SECTION G - Regression
# ===========================================================================

class TestV23Regression(unittest.TestCase):

    def test_version_is_2_4(self):
        self.assertEqual(PLUGIN_VERSION, "2.4")

    def test_psvg_version_is_1_0(self):
        self.assertEqual(PSVG_VERSION, "1.0")

    def test_new_preflight_codes_in_core(self):
        self.assertEqual(PF_TRANSPARENCY_X1A, "TRANSPARENCY_X1A")
        self.assertEqual(PF_SPOT_NOT_SEPARATED, "SPOT_NOT_SEPARATED")
        self.assertEqual(PF_CMYK_RGB_DESYNC, "CMYK_RGB_DESYNC")

    def test_element_cmyk_data_still_works(self):
        d = ElementCmykData("r1", 0, 0.95, 1.0, 0,
                            target="fill", spot_name="PANTONE 485 C")
        self.assertAlmostEqual(d.ink_total, 195.0, delta=0.5)
        self.assertEqual(d.spot_name, "PANTONE 485 C")
        self.assertAlmostEqual(float(d.alpha), 1.0)

    def test_psvg_namespace_correct(self):
        self.assertEqual(PSVG_NS, "http://printsvg.org/spec/1.0")

    def test_legacy_attr_map_complete(self):
        # All core cmyk:* attrs should be in the migration map
        ns = CMYK_NS_LEGACY
        required = ["c","m","y","k","target","spot-name",
                    "overprint-fill","overprint-stroke"]
        for attr in required:
            key = f"{{{ns}}}{attr}"
            self.assertIn(key, LEGACY_ATTR_MAP,
                         msg=f"Missing migration for cmyk:{attr}")

    def test_io_still_imports(self):
        from cmyk_io import read_cmyk_svg, write_cmyk_svg, CmykAutoSave
        self.assertTrue(callable(read_cmyk_svg))

    def test_all_pdf_x_modes_defined(self):
        for mode in (PDFXMode.NONE, PDFXMode.X1A, PDFXMode.X3, PDFXMode.X4):
            self.assertIsNotNone(mode)



# ===========================================================================
# SECTION H - Dual-write metadata (both psvg-data and cmyk-plugin-data)
# ===========================================================================

@unittest.skipUnless(LXML, "lxml required")
class TestDualWriteMetadata(unittest.TestCase):

    def _make_doc_and_tree(self):
        from cmyk_io import CmykDocument, ElementCmykData, write_cmyk_svg
        xml = (
            b'<?xml version="1.0"?>'
            b'<svg xmlns="http://www.w3.org/2000/svg" '
            b'    xmlns:cmyk="https://inkscape.org/extensions/cmyk">'
            b'  <rect id="r1" style="fill:#ff0000"/>'
            b'</svg>'
        )
        tree = ET.parse(io.BytesIO(xml))
        doc  = CmykDocument()
        doc.elements = [
            ElementCmykData("r1", 0, 0.95, 1.0, 0, target="fill",
                            spot_name="PANTONE 485 C")
        ]
        import tempfile, os
        with tempfile.NamedTemporaryFile(suffix=".svg", delete=False) as tmp:
            tmp_path = tmp.name
        try:
            write_cmyk_svg(tree, doc, tmp_path, embed_icc=False)
            result_tree = ET.parse(tmp_path)
            return result_tree
        finally:
            try: os.unlink(tmp_path)
            except OSError: pass

    def test_both_ids_written(self):
        tree    = self._make_doc_and_tree()
        root    = tree.getroot()
        SVG_NS_L= "http://www.w3.org/2000/svg"
        meta    = root.find(f"{{{SVG_NS_L}}}metadata")
        self.assertIsNotNone(meta)
        # Both IDs must be present
        psvg_el = meta.find(".//*[@id='psvg-data']")
        cmyk_el = meta.find(".//*[@id='cmyk-plugin-data']")
        self.assertIsNotNone(psvg_el, "psvg-data not written")
        self.assertIsNotNone(cmyk_el, "cmyk-plugin-data not written")

    def test_both_contain_same_data(self):
        tree    = self._make_doc_and_tree()
        root    = tree.getroot()
        SVG_NS_L= "http://www.w3.org/2000/svg"
        meta    = root.find(f"{{{SVG_NS_L}}}metadata")
        psvg_el = meta.find(".//*[@id='psvg-data']")
        cmyk_el = meta.find(".//*[@id='cmyk-plugin-data']")
        import json as _json
        p_records = _json.loads(psvg_el.text)
        c_records = _json.loads(cmyk_el.text)
        self.assertEqual(len(p_records), len(c_records))
        self.assertAlmostEqual(p_records[0]["m"], c_records[0]["m"], places=4)


# ===========================================================================
# SECTION I - Transparency + overprint conflict
# ===========================================================================

class TestTransparencyOverprintConflict(unittest.TestCase):

    def _run_preflight(self, style, overprint_fill="0"):
        from cmyk_core import (
            run_preflight, PreflightConfig,
            ATTR_C, ATTR_M, ATTR_Y, ATTR_K, ATTR_OVERPRINT_FILL,
            PF_TRANSPARENCY_OP_CONFLICT,
        )
        CMYK_NS_L = "https://inkscape.org/extensions/cmyk"

        class El:
            def __init__(self):
                self.tag    = "{http://www.w3.org/2000/svg}rect"
                self._a     = {
                    "id": "e1",
                    f"{{{CMYK_NS_L}}}c": "0",
                    f"{{{CMYK_NS_L}}}m": "0",
                    f"{{{CMYK_NS_L}}}y": "0",
                    f"{{{CMYK_NS_L}}}k": "1",
                    f"{{{CMYK_NS_L}}}overprint-fill": overprint_fill,
                }
                self._style = style
                self._a["style"] = style

            def get(self, k, d=None): return self._a.get(k, d)

            class _Style(dict):
                pass

            @property
            def style(self):
                from cmyk_core import _parse_length_to_pt
                s = self._Style()
                for p in self._style.split(";"):
                    p = p.strip()
                    if ":" in p:
                        k,v = p.split(":",1)
                        s[k.strip()] = v.strip()
                return s

        cfg = PreflightConfig(
            check_transparency=True,
            check_bleed=False,
        )
        el  = El()
        return run_preflight([el], lambda a,d="": d, cfg)

    def test_overprint_plus_opacity_flagged(self):
        from cmyk_core import PF_TRANSPARENCY_OP_CONFLICT
        report = self._run_preflight("fill:#ff0000;opacity:0.5", overprint_fill="1")
        codes  = [i.code for i in report.issues]
        self.assertIn(PF_TRANSPARENCY_OP_CONFLICT, codes)

    def test_overprint_plus_blend_flagged(self):
        from cmyk_core import PF_TRANSPARENCY_OP_CONFLICT
        report = self._run_preflight("fill:#ff0000;mix-blend-mode:multiply",
                                     overprint_fill="1")
        codes  = [i.code for i in report.issues]
        self.assertIn(PF_TRANSPARENCY_OP_CONFLICT, codes)

    def test_overprint_no_transparency_ok(self):
        from cmyk_core import PF_TRANSPARENCY_OP_CONFLICT
        report = self._run_preflight("fill:#ff0000", overprint_fill="1")
        codes  = [i.code for i in report.issues]
        self.assertNotIn(PF_TRANSPARENCY_OP_CONFLICT, codes)

    def test_transparency_no_overprint_ok(self):
        from cmyk_core import PF_TRANSPARENCY_OP_CONFLICT
        report = self._run_preflight("fill:#ff0000;opacity:0.5", overprint_fill="0")
        codes  = [i.code for i in report.issues]
        self.assertNotIn(PF_TRANSPARENCY_OP_CONFLICT, codes)

    def test_conflict_is_error_severity(self):
        from cmyk_core import PF_TRANSPARENCY_OP_CONFLICT
        report = self._run_preflight("fill:#ff0000;opacity:0.5", overprint_fill="1")
        conflicts = [i for i in report.issues if i.code == PF_TRANSPARENCY_OP_CONFLICT]
        if conflicts:
            self.assertEqual(conflicts[0].severity, "error")


# ===========================================================================
# SECTION J - CLI tool import check
# ===========================================================================

class TestCLIImports(unittest.TestCase):

    def test_cli_imports_cleanly(self):
        import importlib.util, sys
        spec = importlib.util.spec_from_file_location(
            "psvg_cli",
            str(Path(__file__).parent.parent / "extension" / "psvg_cli.py")
        )
        mod = importlib.util.module_from_spec(spec)
        # Should not raise on import
        try:
            spec.loader.exec_module(mod)
            imported = True
        except SystemExit:
            imported = True  # argparse calls sys.exit on --help etc
        except Exception as e:
            self.fail(f"CLI import failed: {e}")
        self.assertTrue(imported)

    def test_cli_has_all_commands(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "psvg_cli",
            str(Path(__file__).parent.parent / "extension" / "psvg_cli.py")
        )
        mod = importlib.util.module_from_spec(spec)
        try:
            spec.loader.exec_module(mod)
        except SystemExit:
            pass
        for cmd in ("cmd_validate","cmd_convert","cmd_strip",
                    "cmd_inspect","cmd_migrate","cmd_preflight",
                    "cmd_annotate","cmd_spec"):
            self.assertTrue(hasattr(mod, cmd), f"Missing: {cmd}")




# ===========================================================================

if __name__ == "__main__":
    unittest.main(verbosity=2)
