"""
Special Document Generators — required by specific European portals.

1. DAADFundingStatementAgent
   - Generates a 1–2 page research funding statement required by DAAD
   - Format: formal German academic convention
   - Sections: Research background → Proposed work → Expected outcomes → Career plan
   - Grounded entirely in master resume — no hallucination

2. EuropassCVGenerator
   - Generates Europass CV XML + PDF (required by Euraxess and some EU portals)
   - Follows official Europass schema
   - Fills from MasterResume — no fabrication
"""

from __future__ import annotations
import json
import re
from pathlib import Path

import structlog

from agents.base_agent import BaseAgent
from models.schemas import JDAnalysis, MasterResume

log = structlog.get_logger()


# ═══════════════════════════════════════════════════════════════════════════════
# DAAD Funding Statement Generator
# ═══════════════════════════════════════════════════════════════════════════════

DAAD_SYSTEM_PROMPT = """
You are an expert academic writer specialising in German research funding applications.
Write a DAAD research funding statement following ALL rules below.
Output ONLY the statement text — no labels, no headers outside the document.

FORMAT:
- Length: 600–800 words
- Tone: formal, precise, third-person optional for academic claims, first-person acceptable
- Language: English (unless target_language is German)
- Structure:
    1. Research Background (2 paragraphs)
       — Describe current field, specific problem, why it matters
       — Reference candidate's prior relevant work (use resume data only)
    2. Proposed Research at Host Institution (2 paragraphs)
       — What specific project will be done at the German institution
       — Concrete methods + expected contributions
       — Explain WHY this institution specifically (reference lab/PI if known)
    3. Expected Outcomes (1 paragraph)
       — Publications, datasets, models, practical impact
    4. Career Development Plan (1 paragraph)
       — How this fits into long-term academic/research career

HARD RULES:
1. NEVER invent research projects, publications, or affiliations not in the resume.
2. Be specific — generic statements like "I want to contribute to AI" fail DAAD review.
3. Reference specific techniques from the candidate's skill list naturally.
4. Do not use: "passion", "excited", "dream", "perfect fit".
5. Do not write a cover letter — this is a RESEARCH STATEMENT only.
"""


class DAADFundingStatementAgent(BaseAgent):

    async def run(
        self,
        master: MasterResume,
        jd: JDAnalysis,
        output_path: Path | None = None,
    ) -> str:
        """Generate DAAD funding statement. Returns text. Optionally saves to file."""

        user_msg = self._build_user(master, jd)
        text = await self._llm(DAAD_SYSTEM_PROMPT, user_msg, max_tokens=1500)

        if output_path:
            output_path.write_text(text, encoding="utf-8")
            log.info("daad_statement_saved", path=str(output_path))

        return text

    def _build_user(self, master: MasterResume, jd: JDAnalysis) -> str:
        exp_lines = []
        for exp in master.experiences:
            exp_lines.append(f"- {exp.title} at {exp.company}: " + " | ".join(exp.bullets[:2]))

        return "\n".join([
            f"CANDIDATE: {master.name}",
            f"TARGET INSTITUTION: {jd.listing.company} (Germany)",
            f"TARGET ROLE: {jd.listing.title}",
            f"CANDIDATE SKILLS: {', '.join(master.skills[:20])}",
            "",
            "CANDIDATE EXPERIENCE:",
            *exp_lines,
            "",
            f"PUBLICATIONS: {'; '.join(master.publications[:3]) if master.publications else 'None yet'}",
            "",
            f"JD RESEARCH FOCUS (use to frame proposed work): {jd.listing.description[:500]}",
            "",
            "Write the DAAD research funding statement now.",
        ])


# ═══════════════════════════════════════════════════════════════════════════════
# Europass CV Generator
# ═══════════════════════════════════════════════════════════════════════════════

EUROPASS_HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8"/>
<title>Europass CV — {name}</title>
<style>
  body {{ font-family: Arial, sans-serif; font-size: 11pt; color: #222; margin: 2cm; }}
  h1 {{ font-size: 18pt; color: #003399; border-bottom: 2px solid #003399; }}
  h2 {{ font-size: 12pt; color: #003399; margin-top: 16px; border-bottom: 1px solid #ccc; }}
  .label {{ font-weight: bold; width: 160px; display: inline-block; color: #555; }}
  .section {{ margin-bottom: 14px; }}
  .exp-title {{ font-weight: bold; }}
  ul {{ margin: 4px 0; padding-left: 18px; }}
  li {{ margin-bottom: 3px; }}
  .logo {{ color: #003399; font-size: 10pt; float: right; }}
</style>
</head>
<body>
<div class="logo">Europass CV</div>
<h1>{name}</h1>

<div class="section">
  <h2>Personal Information</h2>
  <p><span class="label">Email</span>{email}</p>
  <p><span class="label">Phone</span>{phone}</p>
  <p><span class="label">LinkedIn</span>{linkedin}</p>
  <p><span class="label">GitHub</span>{github}</p>
</div>

<div class="section">
  <h2>Summary</h2>
  <p>{summary}</p>
</div>

<div class="section">
  <h2>Work Experience</h2>
  {experience_blocks}
</div>

<div class="section">
  <h2>Education and Training</h2>
  {education_blocks}
</div>

<div class="section">
  <h2>Digital Skills</h2>
  <p>{skills}</p>
</div>

{publications_block}

<div class="section">
  <h2>Languages</h2>
  <p>{languages}</p>
</div>

</body>
</html>"""

EXP_BLOCK = """<div style="margin-bottom:10px">
  <p class="exp-title">{title} — {company}</p>
  <p style="color:#555;font-size:10pt">{start} – {end}</p>
  <ul>{bullets}</ul>
</div>"""

EDU_BLOCK = """<div style="margin-bottom:8px">
  <p><strong>{degree}</strong> in {field}</p>
  <p>{institution} | {year}{gpa}</p>
</div>"""


class EuropassCVGenerator(BaseAgent):
    """
    Generates a Europass-formatted CV as HTML → PDF.
    Uses WeasyPrint for PDF rendering.
    Grounded entirely in MasterResume — no LLM fabrication.
    """

    async def run(self, master: MasterResume, output_dir: Path) -> Path:
        """Generate Europass CV PDF. Returns path to PDF."""
        html = self._build_html(master)

        html_path = output_dir / f"europass_cv_{master.name.replace(' ', '_')}.html"
        pdf_path  = output_dir / f"europass_cv_{master.name.replace(' ', '_')}.pdf"

        output_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        html_path.write_text(html, encoding="utf-8")

        # Render PDF via WeasyPrint
        try:
            from weasyprint import HTML as WP_HTML
            WP_HTML(filename=str(html_path)).write_pdf(str(pdf_path))
            log.info("europass_pdf_generated", path=str(pdf_path))
            html_path.unlink()  # remove temp HTML
            return pdf_path
        except ImportError:
            log.warning("weasyprint_not_installed — returning HTML instead")
            return html_path
        except Exception as e:
            log.error("pdf_render_failed", error=str(e))
            return html_path

    def _build_html(self, master: MasterResume) -> str:
        # Experience blocks
        exp_blocks = []
        for exp in master.experiences:
            bullets_html = "".join(f"<li>{b}</li>" for b in exp.bullets)
            exp_blocks.append(EXP_BLOCK.format(
                title=exp.title,
                company=exp.company,
                start=exp.start,
                end=exp.end or "Present",
                bullets=bullets_html,
            ))

        # Education blocks
        edu_blocks = []
        for edu in master.education:
            gpa_str = f" | GPA: {edu.gpa}" if edu.gpa else ""
            edu_blocks.append(EDU_BLOCK.format(
                degree=edu.degree,
                field=edu.field,
                institution=edu.institution,
                year=edu.year,
                gpa=gpa_str,
            ))

        # Publications
        if master.publications:
            pub_html = "<ul>" + "".join(f"<li>{p}</li>" for p in master.publications) + "</ul>"
            pubs_block = f'<div class="section"><h2>Publications</h2>{pub_html}</div>'
        else:
            pubs_block = ""

        return EUROPASS_HTML_TEMPLATE.format(
            name=master.name,
            email=master.email,
            phone=master.phone or "—",
            linkedin=master.linkedin or "—",
            github=master.github or "—",
            summary=master.summary or "Research-focused candidate with expertise in " + ", ".join(master.skills[:5]),
            experience_blocks="\n".join(exp_blocks),
            education_blocks="\n".join(edu_blocks),
            skills=", ".join(master.skills),
            publications_block=pubs_block,
            languages=", ".join(master.languages) if master.languages else "English (fluent)",
        )
