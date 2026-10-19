Editable publication converted with the explicitly supplied Pandoc LaTeX template.
Canonical Markdown, bibliography and figure inputs were not edited. source.md uses portable figure paths.
Keep template.latex, companion files, code/ and figures/ with main.tex/body.tex/references.bib.
Supported contract: $body$, $title$, $abstract$, the standard $for(header-includes)$$header-includes$$endfor$ hook (required in preamble), $bibliography$, $biblio-style$; no Pandoc partials, scripts, filters, downloads or arbitrary metadata.
header-includes contains renderer-owned support only; source YAML metadata is excluded.
Templates own class, packages, title/abstract placement and BibTeX style. Only pdflatex or xelatex with BibTeX is supported; no Biber or external build recipes.
Body hierarchy uses the declared top_level_division when supplied; no class-name inference.
pdfLaTeX support covers ordinary Latin text and the bounded scientific Greek/math helper, not CJK.
Broader Unicode requires optional xelatex and suitable installed fonts/packages selected by the template; no fonts are provisioned and CJK rendering is unverified.
Fenced code remains literal in code/block-*.txt; fvextra and seqsplit are required.
Trusted TeX can execute TeX primitives and installed packages. No shell escape is enabled; file restrictions and resource limits are not a security sandbox. Use only trusted templates/companions.
No official requirements, author anonymity, page limit or visual compliance is certified. Inspect export.json, build.log and any requested full preview independently.
Missing authors/dates remain missing. External evidence paths are listed in export.json, not copied.
Rebuild here: pdflatex -no-shell-escape -halt-on-error main.tex; bibtex main (if cited); repeat the TeX command twice.
