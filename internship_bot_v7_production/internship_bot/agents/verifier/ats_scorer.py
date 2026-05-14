"""
ATS scorer. Simulates what Taleo/Greenhouse/Workday do when they parse your resume.

Quick summary of how real ATS actually works (spent way too long figuring this out):
  - They strip formatting and read plain text. Fancy PDFs = death.
  - They exact-match keywords first, then stem, then give up.
  - Required keywords in the JD title/requirements count 3x more than body text.
  - Tables and columns completely break the parser. Single column only.
  - Page count matters for USA (1 page) but not Germany (2 pages fine).

Scoring:
  required_coverage * 60  +  preferred_coverage * 25  +  format_score * 15
  Pass = 70+. Below 40% required coverage = skip the listing entirely.

Known issues:
  - Fuzzy matching sometimes hits false positives on short words. "R" matches "research"
    which is wrong but whatever, it's rare enough to not fix yet.
  - German resumes with Umlauts (ä/ö/ü) sometimes fail the clean_text step.
    TODO: add proper unicode normalization before matching.
  - We use Porter stemmer which is English-only. Bad for French/Dutch JDs.
    FIXME: detect JD language and skip stemming if non-English.
"""

import re
from dataclasses import dataclass, field

from rapidfuzz import fuzz
from nltk.stem import PorterStemmer

from models.schemas import ATSScoreReport, CountryFormatRules, JDAnalysis, KeywordWeight

PASS_THRESHOLD = 70.0
SKIP_THRESHOLD = 0.40  # below this required coverage = don't bother applying
FUZZY_MIN = 75  # 85 was too strict for hyphenated/split phrases like "natural-language processing"         # rapidfuzz ratio threshold. 85 feels right, 80 is too loose.

# hand-curated synonym map. add more as you encounter them.
# format: canonical_term -> [aliases]
# TODO: eventually replace this with an embeddings lookup but that's overkill for now
SYNONYMS = {
    "machine learning":    ["ml", "statistical learning", "predictive modeling"],
    "deep learning":       ["dl", "neural networks", "ann", "dnn"],
    "natural language processing": ["nlp", "text mining", "computational linguistics", "natural-language processing"],
    "computer vision":     ["cv", "image recognition", "visual ai"],
    "python":              ["python3", "py"],
    "pytorch":             ["torch"],
    "tensorflow":          ["tf", "keras"],
    "large language models": ["llm", "llms", "foundation models"],
    "reinforcement learning": ["rl"],
    "sql":                 ["postgresql", "mysql", "sqlite"],
    "git":                 ["github", "gitlab", "version control"],
    "docker":              ["containerization", "containers"],
    "kubernetes":          ["k8s"],
    "research":            ["r&d", "research and development"],
}

# reverse lookup built once at import time
_ALIAS_MAP = {
    alias.lower(): canonical.lower()
    for canonical, aliases in SYNONYMS.items()
    for alias in aliases
}

_stemmer = PorterStemmer()


@dataclass
class _Match:
    keyword: str
    matched: bool
    how: str        # exact / synonym / stem / fuzzy / none
    score: float    # 0-1, used for logging but not scoring
    required: bool


@dataclass
class _FormatResult:
    score: float    # 0-100
    issues: list[str] = field(default_factory=list)


class ATSScorer:
    """
    Pure NLP, no LLM. Fast and deterministic.
    Call score() and you get back an ATSScoreReport.
    """

    def score(self, resume_text: str, jd: JDAnalysis,
              page_count: int = 1, resume_page_count: int = None) -> ATSScoreReport:
        if resume_page_count is not None:
            page_count = resume_page_count
        clean = _clean(resume_text)
        tokens = re.findall(r"\b\w+\b", clean)
        matches = [self._match(kw, clean, tokens) for kw in jd.keywords]
        required = [m for m in matches if m.required]
        preferred = [m for m in matches if not m.required]
        if not required and not preferred:
            fmt = self._check_format(resume_text, page_count, jd.format_rules)
            final = round((fmt.score / 100) * 100, 1)
            return ATSScoreReport(
                score=final, keyword_hits={}, required_coverage=1.0,
                preferred_coverage=1.0, format_issues=fmt.issues,
                improvement_notes=fmt.issues, passed=final >= PASS_THRESHOLD,
            )
        req_cov = sum(1 for m in required if m.matched) / max(len(required), 1)
        pref_cov = sum(1 for m in preferred if m.matched) / max(len(preferred), 1)
        fmt = self._check_format(resume_text, page_count, jd.format_rules)
        final = round(req_cov * 60 + pref_cov * 25 + (fmt.score / 100) * 15, 1)
        notes = self._build_notes(matches, fmt, req_cov)
        return ATSScoreReport(
            score=final,
            keyword_hits={m.keyword: m.matched for m in matches},
            required_coverage=round(req_cov, 3),
            preferred_coverage=round(pref_cov, 3),
            format_issues=fmt.issues,
            improvement_notes=notes,
            passed=final >= PASS_THRESHOLD,
        )

    def _match(self, kw: KeywordWeight, resume: str, tokens: list[str]) -> _Match:
        k = kw.keyword.lower().strip()

        # 1. exact phrase
        if k in resume:
            return _Match(k, True, "exact", 1.0, kw.required)

        # 2. synonym check — is k an alias for something? or does k have aliases in resume?
        canonical = _ALIAS_MAP.get(k)
        if canonical and canonical in resume:
            return _Match(k, True, "synonym", 0.85, kw.required)
        if k in SYNONYMS:
            for alias in SYNONYMS[k]:
                if alias.lower() in resume:
                    return _Match(k, True, "synonym", 0.85, kw.required)

        # 3. stemmed match
        k_stem = _stemmer.stem(k)
        stemmed_tokens = [_stemmer.stem(t) for t in tokens]
        if k_stem in stemmed_tokens:
            return _Match(k, True, "stem", 0.9, kw.required)

        # 4. fuzzy — runs against whole resume in chunks, slow but catches typos
        # this is the most expensive step, runs last
        best = 0
        for chunk in re.split(r"[.\n;]", resume):
            r = fuzz.token_set_ratio(k, chunk)
            if r > best:
                best = r
        if best >= FUZZY_MIN:
            return _Match(k, True, "fuzzy", best / 100, kw.required)

        return _Match(k, False, "none", 0.0, kw.required)

    def _check_format(self, raw: str, pages: int, rules: CountryFormatRules) -> _FormatResult:
        issues = []
        deduct = 0.0

        if pages > rules.max_pages:
            issues.append(f"Too long: {pages} pages (max {rules.max_pages} for this country)")
            deduct += 20

        # tabs = table/column layout = ATS killer
        if raw.count("\t") / max(len(raw), 1) > 0.01:
            issues.append("Looks like you have tables or columns — ATS will mangle these, go single column")
            deduct += 15

        # lots of short lines = probably a two-column layout
        short = [l for l in raw.splitlines() if 5 < len(l.strip()) < 30]
        if len(short) > 20:
            issues.append("Possible two-column layout detected — convert to single column")
            deduct += 10

        for section in ["experience", "education", "skills"]:
            if section not in raw.lower():
                issues.append(f"Missing '{section}' section header — ATS needs this to parse correctly")
                deduct += 5

        if not re.search(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}", raw):
            issues.append("No email found in resume — add contact info at top")
            deduct += 10

        if rules.europass_format and "europass" not in raw.lower():
            issues.append("This portal wants Europass format — plain resume may be rejected")
            deduct += 15

        return _FormatResult(max(0.0, 100.0 - deduct), issues)

    def _build_notes(self, matches, fmt, req_cov) -> list[str]:
        notes = []

        if req_cov < SKIP_THRESHOLD:
            # put this first so it's obvious
            notes.append(
                f"CRITICAL: only {req_cov:.0%} required keyword coverage — "
                "probably not worth applying, consider skipping this one"
            )

        missed_req = [m.keyword for m in matches if m.required and not m.matched]
        if missed_req:
            notes.append(f"Missing required keywords (add these naturally): {', '.join(missed_req[:8])}")

        missed_pref = [m.keyword for m in matches if not m.required and not m.matched]
        if missed_pref:
            notes.append(f"Missing preferred keywords: {', '.join(missed_pref[:5])}")

        # weak matches on required keywords — synonym/fuzzy isn't as good as exact
        weak = [m for m in matches if m.required and m.matched and m.how in ("fuzzy", "synonym")]
        if weak:
            notes.append(
                f"Use exact phrasing for: {', '.join(m.keyword for m in weak[:4])} "
                "(synonym matches are less reliable)"
            )

        notes.extend(fmt.issues)
        return notes


# ── helpers ───────────────────────────────────────────────────────────────────

def _clean(text: str) -> str:
    """lowercase + strip punctuation noise. keeps letters, numbers, spaces, hyphens, dots."""
    text = text.lower()
    text = re.sub(r"[^\w\s.\-+#/]", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text
