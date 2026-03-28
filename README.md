🎨 CMYK Color Suite v2.4 — Print-SVG (PSVG)

A full prepress workflow for SVG inside Inkscape — with a formal Print-SVG (PSVG) specification, round-trip CMYK support, PDF/X export, and a complete CLI toolchain.


---

🚀 What this project is

CMYK Color Suite extends Inkscape with a print-safe SVG workflow, designed to help bring Inkscape closer to real-world printing standards used in the industry.

It introduces Print-SVG (PSVG) v1.0, a specification that allows SVG files to carry reliable CMYK, spot color, and print production data — while remaining compatible with standard SVG tools.


---

💡 Why this exists

SVG is powerful, but it’s traditionally limited to RGB workflows and not suited for professional print production.

This project bridges that gap by enabling:

Accurate CMYK color definition

Reliable round-trip editing (no data loss)

Preflight checks aligned with print industry expectations

PDF/X export for professional printing workflows

CLI automation for production pipelines


The goal is to make Inkscape a viable option in professional print environments where tools like Adobe Illustrator, CorelDRAW, Affinity Designer, and even Adobe InDesign are commonly used.


---

🧠 About the project

This project is my original idea and design.

It was built with the assistance of AI tools:

Implementation support from Claude AI

Architectural suggestions and refinement from ChatGPT


All core concepts, structure, and direction were designed and guided by me.


---

🧪 I need testers

This project has grown into a full workflow system, and real-world testing is the next step.

I’m looking for:

🎨 Designers working with print

🖨️ People familiar with prepress workflows

💻 Developers interested in SVG, color systems, or tooling


What to test:

CMYK round-trip (open → edit → save → reopen)

PDF/X export (X-1a, X-3, X-4)

Preflight warnings and errors

CLI commands (psvg validate, convert, preflight, etc.)

Opening files in other tools and re-importing



---

🐛 Found a bug or issue?

Please open an issue and include:

The SVG file (if possible)

Steps to reproduce

Expected vs actual result



---

🙌 Goal

Make SVG a viable format for real print production workflows and help bring Inkscape closer to industry-standard printing capabilities.


---

📬 Feedback

All feedback is welcome — bugs, ideas, criticism, or suggestions.

This project is evolving, and your input helps shape where it goes next.
