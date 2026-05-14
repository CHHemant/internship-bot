"""
Research Internship Platform Registry — NO LinkedIn.

Why each platform beats LinkedIn for research internships:
  - Less bot detection
  - Structured research-specific metadata (field, lab, PI name)
  - Less competition per listing
  - Higher signal-to-noise ratio for academic roles

Platforms by region:
  GLOBAL     : Nature Careers, Science Careers, ResearchGate, Academia.edu Jobs,
                IEEE Jobs, ACM Jobs, FindAPhD
  USA        : NSF REU Sites, Handshake, Pathways (Federal), Wayup, GoinGlobal
  CANADA     : MITACS Globalink, Université portals
  GERMANY    : DAAD, Stepstone, Academics.de, Uni-assist
  EU (broad) : Euraxess, Marie Curie/MSCA, PhDportal, EMBL Jobs
  FRANCE     : ABG-Asso (CIFRE), Welcome to the Jungle, Emploi-Recherche
  NETHERLANDS: Academic Transfer, VSNU
  SWEDEN/DK  : Varbi (Nordic research portal)
  ASIA-PAC   : OIST (Japan), NTU Career, NUS Talent Connect, A*STAR (Singapore)
"""

from __future__ import annotations
from dataclasses import dataclass, field
from models.schemas import Country


@dataclass
class PlatformConfig:
    id: str
    name: str
    base_url: str
    countries: list[Country]
    domains: list[str]          # ["CS", "Biology", "Physics", "All"]
    search_url_template: str    # {query}, {country}, {page} placeholders
    result_selector: str        # CSS selector for listing cards
    title_selector: str
    company_selector: str
    description_selector: str
    link_selector: str
    deadline_selector: str | None
    requires_auth: bool = False
    rate_limit_sec: float = 2.0
    max_pages: int = 5
    notes: str = ""


PLATFORMS: list[PlatformConfig] = [

    # ── GLOBAL ────────────────────────────────────────────────────────────────

    PlatformConfig(
        id="nature_careers",
        name="Nature Careers",
        base_url="https://www.nature.com/naturecareers",
        countries=list(Country),
        domains=["Biology", "CS", "Physics", "Chemistry", "Neuroscience", "All"],
        search_url_template="https://www.nature.com/naturecareers/jobs?text={query}&type=internship&page={page}",
        result_selector="article.results-list__item",
        title_selector="h2.title a",
        company_selector=".affiliation",
        description_selector=".description",
        link_selector="h2.title a",
        deadline_selector=".deadline",
        rate_limit_sec=3.0,
        notes="High-quality research listings. No login required for search.",
    ),

    PlatformConfig(
        id="science_careers",
        name="Science Careers (AAAS)",
        base_url="https://jobs.sciencecareers.org",
        countries=list(Country),
        domains=["Biology", "Chemistry", "Physics", "Neuroscience", "CS", "All"],
        search_url_template="https://jobs.sciencecareers.org/jobs/?keywords={query}&job_type=internship&pg={page}",
        result_selector=".search-result-item",
        title_selector=".job-title a",
        company_selector=".company-name",
        description_selector=".job-description",
        link_selector=".job-title a",
        deadline_selector=".closing-date",
        rate_limit_sec=2.5,
        notes="AAAS journal jobs board. Strong for life/physical sciences.",
    ),

    PlatformConfig(
        id="ieee_jobs",
        name="IEEE Job Site",
        base_url="https://jobs.ieee.org",
        countries=list(Country),
        domains=["CS", "Engineering", "Physics", "AI", "All"],
        search_url_template="https://jobs.ieee.org/jobs/?keywords={query}&job-type=Internship&pg={page}",
        result_selector=".job-listing",
        title_selector=".job-title a",
        company_selector=".company",
        description_selector=".job-description",
        link_selector=".job-title a",
        deadline_selector=".close-date",
        rate_limit_sec=2.0,
        notes="Best for EE, CS, signal processing, hardware research internships.",
    ),

    PlatformConfig(
        id="acm_jobs",
        name="ACM Career & Job Center",
        base_url="https://jobs.acm.org",
        countries=list(Country),
        domains=["CS", "AI", "HCI", "Systems", "All"],
        search_url_template="https://jobs.acm.org/jobs/?keywords={query}&job-type=Internship&page={page}",
        result_selector=".job-result",
        title_selector=".job-title a",
        company_selector=".employer",
        description_selector=".job-excerpt",
        link_selector=".job-title a",
        deadline_selector=".job-expiry",
        rate_limit_sec=2.0,
        notes="CS-focused. Strong for research labs and universities.",
    ),

    PlatformConfig(
        id="findaphd",
        name="FindAPhD (Research Positions)",
        base_url="https://www.findaphd.com",
        countries=list(Country),
        domains=["All"],
        search_url_template="https://www.findaphd.com/phds/non-eu-students/?Keywords={query}&PG={page}&MResearch=1",
        result_selector=".phd-result",
        title_selector=".phd-result__title a",
        company_selector=".phd-result__dept",
        description_selector=".phd-result__desc",
        link_selector=".phd-result__title a",
        deadline_selector=".phd-result__deadline",
        rate_limit_sec=2.0,
        notes="Covers research internships and visiting researcher positions globally.",
    ),

    # ── USA ───────────────────────────────────────────────────────────────────

    PlatformConfig(
        id="nsf_reu",
        name="NSF REU Sites",
        base_url="https://www.nsf.gov/crssprgm/reu/reu_search.jsp",
        countries=[Country.USA],
        domains=["CS", "Biology", "Physics", "Chemistry", "Engineering", "All"],
        search_url_template="https://www.nsf.gov/crssprgm/reu/reu_search.jsp?searchType=keyword&keyword={query}&action=Search",
        result_selector="table.search-results tr",
        title_selector="td:first-child a",
        company_selector="td:nth-child(2)",
        description_selector="td:nth-child(3)",
        link_selector="td:first-child a",
        deadline_selector="td:nth-child(4)",
        rate_limit_sec=3.0,
        notes="NSF-funded REU sites. Paid stipend + housing. Highly competitive but legitimate.",
    ),

    PlatformConfig(
        id="handshake",
        name="Handshake",
        base_url="https://app.joinhandshake.com",
        countries=[Country.USA, Country.CANADA],
        domains=["All"],
        search_url_template="https://app.joinhandshake.com/jobs?page={page}&per_page=25&sort_direction=desc&query={query}&job_type_names[]=Internship",
        result_selector=".job-card",
        title_selector=".job-card__title",
        company_selector=".job-card__company",
        description_selector=".job-card__description",
        link_selector="a.job-card__link",
        deadline_selector=".job-card__expiration",
        requires_auth=True,
        rate_limit_sec=2.0,
        notes="University-verified employers. Requires university email to access full listings.",
    ),

    # ── CANADA ────────────────────────────────────────────────────────────────

    PlatformConfig(
        id="mitacs",
        name="MITACS Globalink",
        base_url="https://www.mitacs.ca/en/programs/globalink",
        countries=[Country.CANADA],
        domains=["CS", "Biology", "Engineering", "Physics", "All"],
        search_url_template="https://www.mitacs.ca/en/programs/globalink/globalink-research-internship",
        result_selector=".project-item",
        title_selector=".project-title a",
        company_selector=".university-name",
        description_selector=".project-description",
        link_selector=".project-title a",
        deadline_selector=".application-deadline",
        rate_limit_sec=3.0,
        notes="Fully funded 12-week research internships at Canadian universities. Highly reputable.",
    ),

    # ── GERMANY ───────────────────────────────────────────────────────────────

    PlatformConfig(
        id="daad",
        name="DAAD Scholarship Database",
        base_url="https://www.daad.de/en",
        countries=[Country.GERMANY],
        domains=["All"],
        search_url_template="https://www.daad.de/en/study-and-research-in-germany/scholarships/daad-scholarship-database/?origin=&target=57&subjectGrpId=&langAbroad=&q={query}&page={page}",
        result_selector=".scholarship-item",
        title_selector=".scholarship-title a",
        company_selector=".scholarship-provider",
        description_selector=".scholarship-desc",
        link_selector=".scholarship-title a",
        deadline_selector=".scholarship-deadline",
        rate_limit_sec=3.0,
        notes="Official German Academic Exchange — funded research stays.",
    ),

    PlatformConfig(
        id="academics_de",
        name="Academics.de",
        base_url="https://www.academics.de",
        countries=[Country.GERMANY, Country.OTHER],  # Austria/Switzerland fall under OTHER
        domains=["All"],
        search_url_template="https://www.academics.de/stellenmarkt/suche?q={query}&employmentType=INTERNSHIP&page={page}",
        result_selector=".job-list-item",
        title_selector=".job-title a",
        company_selector=".job-employer",
        description_selector=".job-description",
        link_selector=".job-title a",
        deadline_selector=".job-deadline",
        rate_limit_sec=2.0,
        notes="Germany/Austria/Switzerland academic jobs. High density of university and institute postings.",
    ),

    # ── EU ────────────────────────────────────────────────────────────────────

    PlatformConfig(
        id="euraxess",
        name="Euraxess",
        base_url="https://euraxess.ec.europa.eu",
        countries=[Country.GERMANY, Country.FRANCE, Country.NETHERLANDS,
                   Country.SWEDEN, Country.OTHER],
        domains=["All"],
        search_url_template="https://euraxess.ec.europa.eu/jobs/search?keywords={query}&type_of_contract=Traineeship&page={page}",
        result_selector=".views-row",
        title_selector=".job-title a",
        company_selector=".organisation",
        description_selector=".field-description",
        link_selector=".job-title a",
        deadline_selector=".application-deadline",
        rate_limit_sec=2.5,
        notes="Official EU researcher mobility portal. Marie Curie / MSCA listings included.",
    ),

    PlatformConfig(
        id="embl_jobs",
        name="EMBL Jobs",
        base_url="https://www.embl.org/jobs",
        countries=[Country.GERMANY, Country.OTHER],
        domains=["Biology", "Bioinformatics", "Chemistry"],
        search_url_template="https://www.embl.org/jobs/position/internship?keywords={query}&page={page}",
        result_selector=".job-item",
        title_selector=".job-title a",
        company_selector=".department",
        description_selector=".job-summary",
        link_selector=".job-title a",
        deadline_selector=".deadline",
        rate_limit_sec=2.0,
        notes="European Molecular Biology Laboratory — premier bio/chem research internships.",
    ),

    # ── ASIA-PACIFIC ──────────────────────────────────────────────────────────

    PlatformConfig(
        id="oist",
        name="OIST Research Internship (Japan)",
        base_url="https://www.oist.jp/research-internship",
        countries=[Country.OTHER],
        domains=["CS", "Biology", "Physics", "Neuroscience", "All"],
        search_url_template="https://www.oist.jp/research-internship/apply",
        result_selector=".unit-item",
        title_selector=".unit-title a",
        company_selector=".unit-pi",
        description_selector=".unit-description",
        link_selector=".unit-title a",
        deadline_selector=".deadline",
        rate_limit_sec=3.0,
        notes="OIST is a world-class graduate university in Japan. English-medium, fully funded.",
    ),

    PlatformConfig(
        id="astar",
        name="A*STAR Research Internship (Singapore)",
        base_url="https://www.a-star.edu.sg",
        countries=[Country.OTHER],
        domains=["Biology", "CS", "Engineering", "Chemistry"],
        search_url_template="https://careers.a-star.edu.sg/search/?q={query}&type=Internship&pg={page}",
        result_selector=".job-listing",
        title_selector=".job-title a",
        company_selector=".institute",
        description_selector=".job-desc",
        link_selector=".job-title a",
        deadline_selector=".closing-date",
        rate_limit_sec=2.0,
        notes="Singapore's Agency for Science, Technology and Research. Stipend included.",
    ),
]


def get_platforms_for(
    countries: list[Country],
    domains: list[str],
) -> list[PlatformConfig]:
    """
    Filter platform registry by target countries and domains.
    Always includes global platforms (list(Country) in their countries field).
    """
    result = []
    for p in PLATFORMS:
        country_match = any(c in p.countries for c in countries) or len(p.countries) >= 8
        domain_match = "All" in p.domains or any(d in p.domains for d in domains) or not domains
        if country_match and domain_match:
            result.append(p)
    return result
