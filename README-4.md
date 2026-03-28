# CMYK Color Suite v2.4
### Print-SVG (PSVG) framework for Inkscape

> You didn't just fix CMYK in Inkscape — you created a foundation for
> print-safe SVG as a format.

---

## What this is

CMYK Color Suite v2.4 is a prepress pipeline inside Inkscape. It adds:

- Full CMYK authoring with per-element values
- Separation preview (C/M/Y/K plates, spot plates, 4-up grid)
- Trapping detection and application
- Ink heatmap overlay
- Round-trip SVG import/export (three-layer redundancy)
- Press preflight with 16 rules
- Transparency detection and PDF/X-1a enforcement
- PDF/X export (X-1a / X-3 / X-4) via Ghostscript
- A formal specification: **Print-SVG v1.0** (`psvg:*` namespace)
- A standalone CLI: `psvg validate/convert/inspect/preflight/migrate/...`

---

## Install

Copy all files from `extension/` to your Inkscape extensions directory:

| OS | Path |
|---|---|
| Linux / macOS | `~/.config/inkscape/extensions/` |
| Windows | `%APPDATA%\inkscape\extensions\` |

**Required files:**

```
cmyk_core.py        colour math, preflight, separations, heatmap
cmyk_io.py          SVG import/export engine
cmyk_psvg.py        Print-SVG spec v1.0 implementation
cmyk_color.py       main Inkscape extension (14 tabs)
cmyk_color.inx      main UI definition
cmyk_import.py      import extension
cmyk_import.inx
cmyk_export_svg.py  export extension + auto-save hook
cmyk_export_svg.inx
psvg_cli.py         standalone CLI tool
```

Restart Inkscape. The extension appears as **Extensions → Color → CMYK Color Suite**.

**Install the CLI:**

```bash
# Linux / macOS
chmod +x /path/to/psvg_cli.py
ln -s /path/to/psvg_cli.py /usr/local/bin/psvg

# Verify
psvg --version
```

---

## The PSVG namespace

Version 2.4 introduces the formal **Print-SVG** namespace:

```
xmlns:psvg="http://printsvg.org/spec/1.0"
```

This decouples the format from Inkscape. Any SVG editor, web app, or
command-line tool can now read and write PSVG data using a stable,
versioned specification.

The legacy `cmyk:*` namespace (v2.3 and earlier) continues to work —
it is read transparently, and `psvg migrate` upgrades files in place.

---

## Data architecture

CMYK values are written to **three places simultaneously** on every export:

```
Layer 1  Display        RGB colour in fill/stroke style     renders everywhere
Layer 2  Compatibility  icc-color() in fill/stroke style    ICC-aware tools
Layer 3  Persistence    <metadata> JSON blob                survives plain-SVG export
Layer 4  Authoritative  psvg:* XML attributes               lossless in Inkscape SVG
```

**Import priority** (highest to lowest):

1. `psvg:*` attributes
2. Legacy `cmyk:*` attributes
3. `<metadata>` JSON (`psvg-data` then `cmyk-plugin-data`)
4. `icc-color()` paint values
5. RGB back-calculation

**A fully annotated element:**

```xml
<rect id="logo-bg"
  psvg:c="0" psvg:m="0.95" psvg:y="1" psvg:k="0"
  psvg:target="fill" psvg:ink-total="195.00"
  psvg:uuid="550e8400-e29b-41d4-a716-446655440000"
  psvg:spot-name="PANTONE 485 C" psvg:spot-separation="separated"
  psvg:overprint-fill="0" psvg:knockout="auto"
  psvg:icc-profile="cmyk-icc" psvg:pdfx-target="pdfx4"
  style="fill:#ff0d00 icc-color(cmyk-icc, 0, 0.950000, 1.000000, 0)"
/>
```

---

## CLI reference

```bash
# Validate a file against the PSVG spec
psvg validate artwork.svg --pdfx pdfx4

# Export press-ready PDF/X-4
psvg convert artwork.svg --to pdfx4 --out artwork_pdfx4.pdf

# Export PDF/X-1a (blocks if live transparency found)
psvg convert artwork.svg --to pdfx1a --out artwork_pdfx1a.pdf

# Full inspection report
psvg inspect artwork.svg

# Inspection as JSON (for scripting)
psvg inspect artwork.svg --format json

# Run preflight with Fogra39 ink limit
psvg preflight artwork.svg --ink-limit 330

# Migrate legacy cmyk:* attributes to psvg:*
psvg migrate artwork.svg --out artwork_psvg.svg

# Back-calculate CMYK from RGB and annotate
psvg annotate artwork.svg --out artwork_annotated.svg

# Remove all CMYK/PSVG metadata (clean handoff)
psvg strip artwork.svg --out artwork_clean.svg

# Print the PSVG v1.0 specification
psvg spec
psvg spec --out PSVG-SPEC-v1.0.txt
```

---

## Preflight rules

| Code | Severity | Rule |
|---|---|---|
| `INK_OVER_LIMIT` | Error | Total ink exceeds configured limit (default 300%) |
| `HAIRLINE` | Error | Stroke < 0.25pt — will disappear in print |
| `SPOT_MISMATCH` | Error | Same spot name, different CMYK values |
| `CMYK_RGB_DESYNC` | Error | Display RGB has drifted from stored CMYK |
| `TRANSPARENCY_X1A` | Error | Transparency present, incompatible with PDF/X-1a |
| `TRANSPARENCY_OVERPRINT_CONFLICT` | Error | Overprint + transparency = undefined RIP output |
| `SPOT_NOT_SEPARATED` | Error | Spot colour not mapped to a named plate |
| `PDFX_COMPLIANCE` | Error | Document not compliant with its PDF/X target |
| `THIN_STROKE` | Warning | Stroke below press minimum |
| `NO_CMYK_ANNOT` | Warning | Coloured element has no CMYK metadata |
| `MISSING_BLEED` | Warning | No bleed area — standard is 3mm |
| `LOW_RESOLUTION` | Warning | Image element too small for press |
| `TEXT_OVERPRINT` | Warning | Overprint on text < 14pt |
| `GRADIENT_NONCMYK` | Warning | Gradient without CMYK stop data |
| `PATTERN_INK_OVER_LIMIT` | Error | Pattern tile exceeds ink limit |
| `RICH_BLACK` | Info | Rich black detected |

---

## All tabs (14)

| Tab | What it does |
|---|---|
| CMYK Color | Apply CMYK fill/stroke; read back; annotate document |
| Gradients | Multi-stop CMYK gradients with `<psvg:gradient>` in defs |
| Spot Colors | 30 Pantone colours; named plate separations |
| ICC Profile | Embed Fogra39 / sRGB; base64 in `<defs>` |
| SVG Preserve | Save to `<metadata>` before plain-SVG export |
| Export PDF | DeviceCMYK PDF via Ghostscript |
| Overprint | Per-element fill/stroke overprint; knockout; blend preview |
| Preflight | 16-rule press check; JSON output; mark mode |
| Compression | Path rounding; style normalisation; SVGZ |
| Separations | Plate preview: single, spot, 4-up grid; tinted mode |
| Trapping | Misregistration detection; 0.25pt spread trap |
| Patterns | CMYK annotation for `<pattern>` tiles |
| Ink Heatmap | Green/amber/red visual ink density overlay |
| SVG I/O | Export CMYK SVG; import; auto-sync; round-trip validate; desync check; strip |
| Transparency | Detect opacity/blend/mask; PDF/X-1a enforcement; flatten advice |
| PDF/X Export | PDF/X-1a / X-3 / X-4 export via Ghostscript with enforcement |
| Print-SVG | Validate spec; migrate namespace; sep map; mark document; export spec |

---

## Production workflow

```
1.  Apply CMYK colours         CMYK Color tab / Gradients / Spot Colors
2.  Assign spot separations    Spot Colors tab → set plate names
3.  Set overprint intent       Overprint tab (rich black, trapping, knockout)
4.  Annotate patterns          Patterns tab
5.  Check transparency         Transparency tab → Detect
    → Fix conflicts            Overprint + opacity is a press error
6.  Embed ICC profile          ICC Profile tab → Fogra39
7.  Run preflight              Preflight tab → fix all errors
8.  Check desync               IO tab → Desync check
9.  Validate PSVG              Print-SVG tab → Validate
10. Export CMYK SVG            IO tab → Export  (saves all three layers)
11. Export PDF/X               PDF/X Export tab → PDF/X-4 (recommended)
                               or: psvg convert artwork.svg --to pdfx4 --out press.pdf
12. Verify                     Acrobat Pro → Output Preview → Separations
                               or: psvg preflight press.pdf
```

---

## Auto-sync on every save

Map a keyboard shortcut to **IO tab → Auto-sync**:

1. `Edit → Preferences → Keyboard Shortcuts → Extensions`
2. Find "CMYK Color Suite"
3. Assign a shortcut (e.g. `Ctrl+Shift+S`)

On each trigger, the extension refreshes:
- `<metadata>` JSON blob
- `icc-color()` paint values  
- ICC profile in `<defs>`
- Marks any RGB/CMYK desynced elements

---

## What happens when you open in Illustrator?

Illustrator strips `psvg:*` attributes on save. After round-tripping:

- `icc-color()` paint values survive (SVG 1.1 standard)
- `<metadata>` JSON survives
- `psvg:*` attributes are gone

On re-import: run **Import CMYK SVG** (blank source) → the plugin
restores from `<metadata>` JSON. Accuracy: ~100% for solid colours,
~95% for gradients (interpolation may differ).

---

## Running tests

```bash
cd tests/
python3 tests_cmyk_v2_4.py -v   # 75 tests  — PSVG spec, transparency, PDF/X, CLI
python3 tests_cmyk_v2_3.py -v   # 58 tests  — IO module round-trip
python3 tests_cmyk_v2_2.py -v   # 94 tests  — separations, trapping, heatmap
python3 tests_cmyk_v2_1.py -v   # 83 tests  — overprint, preflight, compression
# Total: 310 tests, all passing
```

---

## Files

| File | Role | Size |
|---|---|---|
| `cmyk_core.py` | Colour math, ICC, preflight (16 rules), compression, separations, trapping, heatmap | 51 KB |
| `cmyk_io.py` | SVG import/export, UUID, desync, gradient XML, soft proof, dual-namespace | 44 KB |
| `cmyk_psvg.py` | Print-SVG spec v1.0: validation, migration, transparency, PDF/X, sep mapping | 32 KB |
| `cmyk_color.py` | Inkscape extension, 17 tabs | 88 KB |
| `psvg_cli.py` | CLI: validate/convert/strip/inspect/migrate/preflight/annotate/spec | 21 KB |
| `cmyk_import.py` | Inkscape import extension | 8 KB |
| `cmyk_export_svg.py` | Inkscape export extension + auto-save hook | 15 KB |
| `spec/PSVG-SPEC-v1.0.md` | Formal Print-SVG specification | 12 KB |

---

## License

MIT — free to use, modify, distribute, and build upon.

---

*CMYK Color Suite v2.4 — Print-SVG (PSVG) spec v1.0*
