from __future__ import annotations

from simple_ar.literature.models import Paper, bibliographic_details


def papers_to_bibtex(papers: list[Paper]) -> str:
    """Render paper metadata into BibTeX entries.

    Args:
        papers: Papers previously stored in ``papers.jsonl``.

    Returns:
        BibTeX text using each paper id as the citation key.
    """
    return "\n\n".join(_paper_to_bibtex(paper) for paper in papers).strip() + "\n"


def _paper_to_bibtex(paper: Paper) -> str:
    metadata = bibliographic_details(paper)
    fields = {
        "title": metadata["title"],
        "author": " and ".join(metadata["authors"]),
        "url": metadata["url"],
    }
    if not metadata["authors"]:
        # BibTeX styles use key for an authorless label/sort fallback. This is
        # the existing citation identity, not a manufactured author or title.
        fields["key"] = paper.id
    if metadata["year"]:
        fields["year"] = metadata["year"]
    if metadata["doi"]:
        fields["doi"] = metadata["doi"]
    if metadata["notes"]:
        fields["note"] = " ".join(metadata["notes"])
    if paper.source == "arxiv" and paper.source_id:
        fields["eprint"] = paper.source_id
        fields["archivePrefix"] = "arXiv"

    lines = [f"@misc{{{paper.id},"]
    for key, value in fields.items():
        if value:
            lines.append(f"  {key} = {{{_escape_bibtex(value, literal_url=key in {'url', 'doi', 'eprint'})}}},")
    lines[-1] = lines[-1].rstrip(",")
    lines.append("}")
    return "\n".join(lines)


def _escape_bibtex(value: str, *, literal_url: bool = False) -> str:
    """Keep recorded text literal in TeX; locator fields are read by \\url."""
    escapes = {"\\": r"\textbackslash{}", "{": r"\{", "}": r"\}",
               "&": r"\&", "%": r"\%", "$": r"\$", "#": r"\#", "_": r"\_",
               "~": r"\textasciitilde{}", "^": r"\textasciicircum{}"}
    if literal_url:
        escapes = {"\\": r"\\", "{": r"\{", "}": r"\}"}
    return "".join(escapes.get(char, char) for char in value)
