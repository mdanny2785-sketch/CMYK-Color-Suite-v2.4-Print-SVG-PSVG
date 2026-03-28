# Changelog

## [2.4] — 2026-03-27  Print-SVG specification + full toolchain

### New files
- `cmyk_psvg.py` — Print-SVG spec v1.0 implementation
- `psvg_cli.py` — standalone CLI toolchain (8 commands)
- `spec/PSVG-SPEC-v1.0.md` — publishable formal specification

### Added — Print-SVG (PSVG) Specification v1.0

- Formal namespace: `xmlns:psvg="http://printsvg.org/spec/1.0"`
- Complete attribute schema (see spec for full list)
- Three conformance levels: Basic, Standard, Full
- `PSVGDocument` dataclass — top-level document model
- `validate_psvg_document()` — schema validation with error codes
- `write_spec_document()` — export the spec as a text file

### Added — Namespace migration

- `migrate_cmyk_to_psvg()` — upgrades legacy `cmyk:*` attrs to `psvg:*` in place
- `LEGACY_ATTR_MAP` — complete mapping of every `cmyk:*` → `psvg:*` attribute
- Migration action in the Print-SVG tab
- `psvg migrate` CLI command

### Added — Dual-write metadata (ChatGPT recommendation)

- `_write_metadata_blob()` now writes BOTH `psvg-data` AND `cmyk-plugin-data`
  on every export, so files work with both v2.4+ and v2.3 readers
- `_try_read_metadata()` reads `psvg-data` first, falls back to `cmyk-plugin-data`
- Import priority strictly enforced:
  1. `psvg:*` attributes (authoritative)
  2. Legacy `cmyk:*` attributes
  3. `<metadata>` JSON (`psvg-data` > `cmyk-plugin-data`)
  4. `icc-color()` paint values
  5. RGB back-calculation

### Added — Transparency detection and enforcement

- `detect_transparency()` — scans elements for opacity, blend modes, masks, filters
- `TransparencyInfo` dataclass with `flattening_advice()` per element
- `build_gs_flatten_args()` — Ghostscript args for transparency flattening pass
- Transparency tab in Inkscape: detect, mark on canvas, show GS commands
- **Hard block on PDF/X-1a export** when live transparency is present

### Added — `TRANSPARENCY_OVERPRINT_CONFLICT` preflight rule

- New `PF_TRANSPARENCY_OP_CONFLICT` (Error severity)
- Fires when an element has both overprint set AND uses opacity/blend/mask
- This combination produces undefined output on most RIPs
- Real production failure case: was previously undetected

### Added — PDF/X export modes

- `PDFXMode` class: `NONE`, `X1A`, `X3`, `X4` with correct GS args per mode
- PDF/X-1a: forces PDF 1.3, flat transparency, CMYK/spot only
- PDF/X-3: PDF 1.4, device-independent colour
- PDF/X-4: PDF 1.6, live transparency, recommended for modern workflows
- PDF/X Export tab in Inkscape with enforcement and validation
- `psvg convert --to pdfx4` CLI command

### Added — Spot colour separation mapping

- `SpotSeparation` dataclass — one named plate definition
- `SeparationMap` — complete document plate map
- `build_separation_map_from_elements()` — derives map from annotated elements
- `SeparationMap.ghostscript_separation_args()` — GS tiffsep args
- `SeparationMap.to_scribus_color_defs()` — Scribus SLA colour block
- Separation map view in Print-SVG tab and `psvg inspect`

### Added — CLI toolchain (`psvg`)

Eight commands, zero dependencies beyond the extension files and lxml:

```
psvg validate  <file.svg>  [--pdfx pdfx4]         spec validation, exit 1 on errors
psvg convert   <file.svg>  --to pdfx4 --out f.pdf  drives Inkscape + Ghostscript
psvg strip     <file.svg>  [--out clean.svg]        remove all PSVG/CMYK metadata
psvg inspect   <file.svg>  [--format json]          full report: colours, plates, issues
psvg migrate   <file.svg>  [--out migrated.svg]     cmyk:* → psvg:* namespace upgrade
psvg preflight <file.svg>  [--ink-limit 300]        16-rule press preflight
psvg annotate  <file.svg>  [--out annotated.svg]    back-calculate CMYK from RGB
psvg spec                  [--out PSVG-SPEC-v1.0.txt]  print or export the spec
```

### Added — Print-SVG tab in Inkscape

- Validate document against PSVG spec
- Migrate `cmyk:*` → `psvg:*` namespace
- View separation plate map
- Mark document with `psvg:version`, `psvg:pdfx-mode`, `psvg:bleed-mm`
- Export PSVG specification as text file

### Fixed — All regression test failures from previous session

- v2.3 tests: both `test_metadata_blob_written` and `test_syncs_metadata` now
  accept either `psvg-data` or `cmyk-plugin-data` (dual-write)
- v2.2 tests: version assertions updated to current version
- All 9 new test classes (sections H/I/J) placed before `__main__` block
  so they are discovered by the test runner (was: 66 found, now: 75 found)

### Tests

- 75 tests in `tests_cmyk_v2_4.py` — PSVG spec, transparency, PDF/X,
  separation mapping, dual-write metadata, overprint conflict, CLI imports
- **310 tests total across all suites — all passing**

---

## [2.3] — 2026-03-27  Full SVG round-trip

- `cmyk_io.py` — SVG import/export engine
- Three-layer redundancy: attrs + metadata + icc-color()
- UUID stability, RGB desync detection
- Auto-save hook, round-trip validation
- 58 new tests

---

## [2.2] — 2026-03-20

- Separations preview (single, spot, 4-up)
- Trapping (0.25pt spread trap)
- Pattern fill CMYK annotation
- Ink heatmap overlay
- 94 new tests

---

## [2.1] — 2026-03-18

- Overprint tab (fill/stroke, OPM 1, screen preview)
- Preflight (10 rules, mark mode, JSON output)
- Compression (path rounding, SVGZ)
- 83 new tests

---

## [2.0] — 2026-02-01

- Gradients, Spot Colors, ICC Profile, SVG Preserve, Export PDF tabs
- `cmyk_export.py` standalone CLI

---

## [1.0] — 2025-12-01

- Initial: CMYK fill/stroke, document annotation, basic GS export
