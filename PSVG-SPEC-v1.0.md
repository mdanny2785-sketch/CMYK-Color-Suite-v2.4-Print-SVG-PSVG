# Print-SVG Specification v1.0

**Namespace:** `http://printsvg.org/spec/1.0`  
**Prefix:** `psvg`  
**Status:** Draft — Reference Implementation  
**Implementation:** CMYK Color Suite v2.4 for Inkscape  

---

## 1. Purpose

Print-SVG (PSVG) is an extension layer on top of SVG 1.1 that carries
CMYK colour data, spot colour separations, overprint intent, transparency
status, and PDF/X conformance metadata through the SVG format without
conflicting with existing SVG renderers.

An SVG file carrying PSVG metadata renders correctly in any SVG viewer
using its RGB fallback colours. PSVG-aware tools additionally read the
exact CMYK values, spot definitions, and press intent.

### Design principles

1. **Additive.** PSVG adds attributes; it never removes or redefines
   existing SVG semantics.

2. **Redundant.** CMYK data is stored in three places simultaneously so
   it survives every known export path:
   - `psvg:*` attributes on elements (lossless, Inkscape native SVG)
   - `<metadata>` JSON blob (survives plain-SVG export)
   - `icc-color()` paint values (SVG 1.1 §11.2, ICC-aware tools)

3. **Versioned.** `psvg:version` on the root `<svg>` enables future
   migration without breaking existing files.

4. **Tool-agnostic.** The namespace is not tied to Inkscape. Any SVG
   editor, web app, or converter can read and write PSVG data.

5. **Backward-compatible.** Files using the legacy `cmyk:*` namespace
   (CMYK Color Suite v2.3 and earlier) are read transparently. Migration
   to `psvg:*` is a one-time upgrade operation.

---

## 2. Namespace declaration

```xml
<svg xmlns="http://www.w3.org/2000/svg"
     xmlns:psvg="http://printsvg.org/spec/1.0"
     xmlns:xlink="http://www.w3.org/1999/xlink"
     psvg:version="1.0"
     psvg:pdfx-mode="pdfx4"
     psvg:profile="fogra39"
     psvg:rendering-intent="relative-colorimetric"
     psvg:bleed-mm="3.0">
```

---

## 3. Document-level attributes (on root `<svg>`)

| Attribute | Type | Values | Description |
|---|---|---|---|
| `psvg:version` | string | `"1.0"` | Spec version. Required for full compliance. |
| `psvg:profile` | string | `fogra39` \| `srgb` \| `custom` | ICC profile used for CMYK interpretation. |
| `psvg:rendering-intent` | string | `perceptual` \| `relative-colorimetric` \| `saturation` \| `absolute-colorimetric` | ICC rendering intent. Default: `relative-colorimetric`. |
| `psvg:pdfx-mode` | string | `none` \| `pdfx1a` \| `pdfx3` \| `pdfx4` | Target PDF/X conformance level. |
| `psvg:bleed-mm` | float | e.g. `3.0` | Bleed size in millimetres. Press standard is 3mm. |

---

## 4. Element-level attributes

These attributes MAY appear on any SVG element that carries colour.

### 4.1 CMYK channels

| Attribute | Type | Range | Description |
|---|---|---|---|
| `psvg:c` | float | 0.0–1.0 | Cyan channel |
| `psvg:m` | float | 0.0–1.0 | Magenta channel |
| `psvg:y` | float | 0.0–1.0 | Yellow channel |
| `psvg:k` | float | 0.0–1.0 | Key (Black) channel |
| `psvg:target` | string | `fill` \| `stroke` \| `both` | Which paint the CMYK values apply to |
| `psvg:ink-total` | float | 0–400 | Cached sum: `(C+M+Y+K) × 100`. Not authoritative. |
| `psvg:alpha` | float | 0.0–1.0 | Opacity of the CMYK colour. Default: 1.0 |

### 4.2 Identity

| Attribute | Type | Description |
|---|---|---|
| `psvg:uuid` | string | UUID4. Stable identity across ID rewrites (paste, clone, duplicate). Required for lossless round-trips. |

### 4.3 Spot colours

| Attribute | Type | Description |
|---|---|---|
| `psvg:spot-name` | string | Canonical spot colour name, e.g. `PANTONE 485 C`. |
| `psvg:spot-separation` | string | `separated` \| `process` \| `unknown`. Whether this spot maps to a dedicated plate (`separated`) or is simulated by process inks (`process`). |

### 4.4 Overprint and knockout

| Attribute | Type | Values | Description |
|---|---|---|---|
| `psvg:overprint-fill` | string | `1` \| `0` | Fill overprint flag. |
| `psvg:overprint-stroke` | string | `1` \| `0` | Stroke overprint flag. |
| `psvg:knockout` | string | `auto` \| `on` \| `off` | Transparency group knockout mode. |
| `psvg:overprint-mode` | string | `fill` \| `stroke` \| `both` | Which targets overprint is active for. |

### 4.5 Transparency

| Attribute | Type | Description |
|---|---|---|
| `psvg:transparency-group` | string | `isolated` \| `knockout` \| `passthrough`. Transparency group type. |
| `psvg:transparency` | string | `1` if the element uses opacity, blend mode, mask, or filter. Set by the transparency detection pass. |

### 4.6 Gradients

| Attribute | Type | Description |
|---|---|---|
| `psvg:gradient-stops` | JSON | Array of stop objects: `[{"offset":0,"c":0,"m":0,"y":0,"k":1}, ...]` |
| `psvg:gradient-ref` | string | `id` of the linked `<psvg:gradient>` element in `<defs>`. |

### 4.7 Patterns

| Attribute | Type | Description |
|---|---|---|
| `psvg:pattern-colors` | JSON | Array of tile colour objects: `[{"i":0,"prop":"fill","c":0,"m":0,"y":0,"k":1,"hex":"#000000"}, ...]` |

### 4.8 ICC

| Attribute | Type | Description |
|---|---|---|
| `psvg:icc-profile` | string | Profile name reference (matches `<color-profile name="...">` in `<defs>`). |
| `psvg:icc-href` | string | File system path to the source ICC profile. |

### 4.9 PDF/X targeting

| Attribute | Type | Values | Description |
|---|---|---|---|
| `psvg:pdfx-target` | string | `pdfx1a` \| `pdfx3` \| `pdfx4` \| `none` | PDF/X target for this element. |

### 4.10 Preflight status

| Attribute | Type | Description |
|---|---|---|
| `psvg:preflight-warn` | JSON | Array of preflight warning codes, e.g. `["INK_OVER_LIMIT","HAIRLINE"]`. |
| `psvg:desync-warn` | string | `1` if the element's displayed RGB has drifted from its stored CMYK values. |

---

## 5. Child elements in `<defs>`

### 5.1 `<psvg:gradient>`

Carries exact CMYK stop values for a gradient. Linked to the SVG gradient
by `linked-gradient`.

```xml
<defs>
  <linearGradient id="grad1">
    <!-- normal SVG gradient stops with RGB fallback -->
  </linearGradient>

  <psvg:gradient id="psvg-grad1" linked-gradient="grad1">
    <psvg:stop offset="0.000000" c="0" m="0" y="0" k="1.0"
               profile="cmyk-icc"/>
    <psvg:stop offset="1.000000" c="0" m="0" y="0" k="0.0"
               profile="cmyk-icc"/>
  </psvg:gradient>

  <color-profile id="cmyk-icc-profile" name="cmyk-icc"
                 xlink:href="data:application/vnd.iccprofile;base64,..."
                 rendering-intent="relative-colorimetric"/>
</defs>
```

### 5.2 `<color-profile>`

Standard SVG 1.1 ICC colour profile element. PSVG requires the Fogra39
(ISO Coated v2) profile to be embedded when `psvg:profile="fogra39"`.

---

## 6. `<metadata>` JSON schema

PSVG data is written to `<metadata>` as a JSON blob so it survives
plain-SVG export (which strips namespace attributes but preserves
`<metadata>`).

**Element:**

```xml
<metadata>
  <psvg:data id="psvg-data">
    [{"id":"rect1","uuid":"550e8400-...","c":0,"m":0.95,"y":1,"k":0,
      "target":"fill","spot":"PANTONE 485 C","op_fill":"0","op_stroke":"0",
      "knockout":"auto"}, ...]
  </psvg:data>
  <psvg:version id="psvg-version">1.0</psvg:version>
</metadata>
```

**Record fields:**

| Field | Type | Description |
|---|---|---|
| `id` | string | Element SVG id |
| `uuid` | string | UUID4 stable identity |
| `c`, `m`, `y`, `k` | float | CMYK channels 0–1 |
| `target` | string | `fill` \| `stroke` \| `both` |
| `spot` | string | Spot colour name (optional) |
| `op_fill` | string | `"1"` if fill overprints |
| `op_stroke` | string | `"1"` if stroke overprints |
| `knockout` | string | `auto` \| `on` \| `off` |
| `grad_stops` | array | Gradient stop objects (optional) |
| `pattern` | array | Pattern tile colour objects (optional) |
| `pf_warn` | array | Preflight warning codes (optional) |

**Backward compatibility:** The legacy `cmyk-plugin-data` element is
also written alongside `psvg-data` for compatibility with CMYK Color
Suite v2.3 and earlier. Readers MUST read `psvg-data` first.

---

## 7. Paint value format

Every CMYK-annotated element carries its colour as an SVG paint value
combining an RGB fallback with `icc-color()`:

```
fill: <rgb-fallback> icc-color(<profile-name>, c, m, y, k)
```

Example:

```css
fill: #ff0d00 icc-color(cmyk-icc, 0, 0.950000, 1.000000, 0)
```

The RGB fallback is computed from the CMYK values using an adapted
Fogra39 matrix for improved screen accuracy. The `icc-color()` value
is authoritative for ICC-aware renderers (Scribus, Acrobat, RIPs).

---

## 8. Import priority

When reading a PSVG file, implementations MUST follow this priority order:

1. **`psvg:*` attributes** — authoritative, lossless
2. **Legacy `cmyk:*` attributes** — backward compat (v2.3 and earlier)
3. **`<metadata>` JSON** — `psvg-data` first, then `cmyk-plugin-data`
4. **`icc-color()` paint values** — SVG 1.1 standard
5. **RGB back-calculation** — approximate, last resort only

When multiple sources disagree, the highest-priority source wins.

---

## 9. PDF/X conformance levels

| Mode | PDF version | Transparency | Colour spaces | ICC required |
|---|---|---|---|---|
| `pdfx1a` | 1.3 | Must be flattened | CMYK + spot only | Yes (output intent) |
| `pdfx3` | 1.4 | Live allowed | Device-independent | Yes |
| `pdfx4` | 1.6 | Live allowed | Any ICC | Yes |

PSVG validators MUST report `TRANSPARENCY_X1A` errors when
`psvg:pdfx-mode="pdfx1a"` and live transparency is detected.

---

## 10. Preflight error codes

| Code | Severity | Description |
|---|---|---|
| `INK_OVER_LIMIT` | Error | Total ink exceeds configured limit |
| `HAIRLINE` | Error | Stroke < 0.25pt |
| `SPOT_MISMATCH` | Error | Same spot name, different CMYK values |
| `CMYK_RGB_DESYNC` | Error | Display RGB has drifted from stored CMYK |
| `TRANSPARENCY_X1A` | Error | Transparency incompatible with PDF/X-1a |
| `TRANSPARENCY_OVERPRINT_CONFLICT` | Error | Overprint set on transparent element |
| `SPOT_NOT_SEPARATED` | Error | Spot colour not mapped to a plate |
| `THIN_STROKE` | Warning | Stroke below minimum |
| `NO_CMYK_ANNOT` | Warning | Colour with no CMYK metadata |
| `MISSING_BLEED` | Warning | No bleed area defined |
| `LOW_RESOLUTION` | Warning | Image element very small |
| `TEXT_OVERPRINT` | Warning | Overprint on text < 14pt |
| `GRADIENT_NONCMYK` | Warning | Gradient without CMYK stops |
| `PATTERN_INK_OVER_LIMIT` | Error | Pattern tile ink exceeds limit |
| `PDFX_COMPLIANCE` | Error | Document not compliant with target PDF/X mode |
| `RICH_BLACK` | Info | Rich black detected |

---

## 11. Legacy namespace

Files using `xmlns:cmyk="https://inkscape.org/extensions/cmyk"` (CMYK
Color Suite v2.3 and earlier) MUST be treated as valid PSVG input.
The migration mapping from `cmyk:*` to `psvg:*` is defined in
`cmyk_psvg.py:LEGACY_ATTR_MAP`.

Migration is performed by the `psvg migrate` CLI command or the
Print-SVG tab > Migrate action in Inkscape.

---

## 12. Minimal compliant element

A PSVG-compliant colour element MUST carry at minimum:

```xml
<rect id="logo-bg"
  psvg:c="0.000000" psvg:m="0.950000" psvg:y="1.000000" psvg:k="0.000000"
  psvg:target="fill"
  psvg:uuid="550e8400-e29b-41d4-a716-446655440000"
  style="fill:#ff0d00 icc-color(cmyk-icc, 0, 0.950000, 1.000000, 0)"
/>
```

A **fully annotated** element additionally carries:

```xml
<rect id="logo-bg"
  psvg:c="0" psvg:m="0.95" psvg:y="1" psvg:k="0"
  psvg:target="fill"
  psvg:ink-total="195.00"
  psvg:uuid="550e8400-e29b-41d4-a716-446655440000"
  psvg:spot-name="PANTONE 485 C"
  psvg:spot-separation="separated"
  psvg:overprint-fill="0"
  psvg:overprint-stroke="0"
  psvg:knockout="auto"
  psvg:overprint-mode="fill"
  psvg:icc-profile="cmyk-icc"
  psvg:pdfx-target="pdfx4"
  style="fill:#ff0d00 icc-color(cmyk-icc, 0, 0.950000, 1.000000, 0)"
/>
```

---

## 13. Conformance

An SVG file is **PSVG-Basic compliant** when:
- All colour-bearing elements carry `psvg:c/m/y/k` and `psvg:uuid`
- The root `<svg>` carries `psvg:version="1.0"`

An SVG file is **PSVG-Standard compliant** when it is Basic-compliant and:
- `icc-color()` is present on all fill/stroke paint values
- `<metadata>` contains a valid `psvg-data` JSON blob
- An ICC profile is embedded in `<defs>`

An SVG file is **PSVG-Full compliant** when it is Standard-compliant and:
- All spot colours carry `psvg:spot-name` and `psvg:spot-separation`
- `psvg:pdfx-mode` is set on the root element
- The document passes PSVG schema validation with no errors

---

## Appendix A: Reference implementation

The reference implementation is the CMYK Color Suite v2.4 for Inkscape:

- `cmyk_psvg.py` — spec constants, validation, migration, transparency detection
- `cmyk_io.py` — import/export engine with dual-namespace support
- `cmyk_core.py` — colour math, preflight, separations, heatmap
- `cmyk_color.py` — Inkscape extension (14 tabs)
- `psvg_cli.py` — CLI toolchain

---

## Appendix B: CLI quick reference

```
psvg validate  artwork.svg [--pdfx pdfx4]
psvg convert   artwork.svg --to pdfx4 --out artwork.pdf
psvg strip     artwork.svg [--out clean.svg]
psvg inspect   artwork.svg [--format json]
psvg migrate   artwork.svg [--out migrated.svg]
psvg preflight artwork.svg [--ink-limit 300] [--format json]
psvg annotate  artwork.svg [--out annotated.svg]
psvg spec      [--out PSVG-SPEC-v1.0.txt]
```

---

*Print-SVG Specification v1.0 — Reference implementation: CMYK Color Suite v2.4*
