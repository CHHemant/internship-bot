"""
Security hardening checks — run before any production deployment.

Usage:
  python scripts/security_check.py

Checks:
  1. No secrets committed to git
  2. .env not tracked
  3. vault/ not tracked
  4. JWT_SECRET is set and strong enough
  5. DRY_RUN status
  6. Dependency CVE scan (pip-audit)
  7. .gitignore covers all sensitive paths
  8. VAULT_MASTER_PASSWORD strength

Exit code 0 = all good, 1 = warnings, 2 = critical issues found.
"""

from __future__ import annotations
import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent

CRITICAL = []
WARNINGS = []
INFO = []


def check(label: str, passed: bool, message: str, critical: bool = False):
    if passed:
        INFO.append(f"  ✓  {label}")
    else:
        (CRITICAL if critical else WARNINGS).append(f"  {'✗' if critical else '!'} {label}: {message}")


# ── 1. .env not committed ─────────────────────────────────────────────────────
try:
    result = subprocess.run(
        ["git", "ls-files", ".env"],
        capture_output=True, text=True, cwd=ROOT
    )
    check(".env not tracked by git", result.stdout.strip() == "",
          ".env is tracked — run: git rm --cached .env", critical=True)
except FileNotFoundError:
    WARNINGS.append("  ! git not found — skipping git checks")

# ── 2. .vault/ not committed ──────────────────────────────────────────────────
try:
    result = subprocess.run(
        ["git", "ls-files", ".vault/"],
        capture_output=True, text=True, cwd=ROOT
    )
    check(".vault/ not tracked", result.stdout.strip() == "",
          ".vault/ is tracked — contains encrypted credentials!", critical=True)
except FileNotFoundError:
    pass

# ── 3. No hardcoded secrets in source ─────────────────────────────────────────
SECRET_PATTERNS = [
    (r"sk-ant-[A-Za-z0-9]{20,}", "Anthropic API key"),
    (r"(?i)password\s*=\s*['\"][^'\"]{16,}", "hardcoded password"),
    (r"(?i)secret\s*=\s*['\"][^'\"]{8,}", "hardcoded secret"),
    (r"(?i)api_key\s*=\s*['\"][A-Za-z0-9]{16,}", "hardcoded API key"),
]
found_secrets = []
for py_file in ROOT.rglob("*.py"):
    if any(skip in str(py_file) for skip in [".venv", "__pycache__", "security_check.py"]):
        continue
    content = py_file.read_text(errors="ignore")
    for pattern, label in SECRET_PATTERNS:
        if re.search(pattern, content):
            found_secrets.append(f"{py_file.relative_to(ROOT)}: {label}")

check("No hardcoded secrets in .py files",
      len(found_secrets) == 0,
      "Found: " + ", ".join(found_secrets[:3]),
      critical=True)

# ── 4. JWT_SECRET set and strong ──────────────────────────────────────────────
jwt_secret = os.environ.get("JWT_SECRET", "")
check("JWT_SECRET set", bool(jwt_secret), "JWT_SECRET not set — auth will fail", critical=True)
if jwt_secret:
    check("JWT_SECRET strong (≥32 chars)", len(jwt_secret) >= 32,
          f"Only {len(jwt_secret)} chars — use 32+ for security", critical=True)

# ── 5. DRY_RUN status ─────────────────────────────────────────────────────────
dry_run = os.environ.get("DRY_RUN", "true").lower()
check("DRY_RUN acknowledged",
      True,  # always pass — just inform
      "")
if dry_run == "false":
    WARNINGS.append("  ! DRY_RUN=false — pipeline will submit REAL applications")
else:
    INFO.append(f"  ✓  DRY_RUN=true (safe mode)")

# ── 6. VAULT_MASTER_PASSWORD strength ────────────────────────────────────────
vault_pwd = os.environ.get("VAULT_MASTER_PASSWORD", "")
check("VAULT_MASTER_PASSWORD set", bool(vault_pwd),
      "Not set — vault cannot encrypt credentials", critical=True)
if vault_pwd:
    check("VAULT_MASTER_PASSWORD strong (≥16 chars)", len(vault_pwd) >= 16,
          f"Only {len(vault_pwd)} chars — use 16+ for security")

# ── 7. .gitignore covers key paths ────────────────────────────────────────────
gitignore = (ROOT / ".gitignore").read_text() if (ROOT / ".gitignore").exists() else ""
for path in [".env", ".vault/", "*.enc", "screenshots/"]:
    check(f".gitignore covers {path}",
          path.rstrip("/") in gitignore or path in gitignore,
          f"{path} not in .gitignore — could be committed accidentally")

# ── 8. pip-audit CVE scan ─────────────────────────────────────────────────────
try:
    result = subprocess.run(
        ["pip-audit", "--format=json", "-q"],
        capture_output=True, text=True, cwd=ROOT, timeout=60
    )
    import json
    if result.stdout:
        vulns = json.loads(result.stdout)
        critical_vulns = [v for v in vulns if any(
            fix.get("fix_versions") for dep in v.get("dependencies", [])
            for fix in dep.get("vulns", [])
        )]
        check("No CVEs in dependencies",
              len(vulns) == 0,
              f"{len(vulns)} vulnerabilities found — run: pip-audit for details")
    else:
        INFO.append("  ✓  pip-audit: no vulnerabilities found")
except FileNotFoundError:
    WARNINGS.append("  ! pip-audit not installed — run: pip install pip-audit")
except subprocess.TimeoutExpired:
    WARNINGS.append("  ! pip-audit timed out")


# ── Report ────────────────────────────────────────────────────────────────────
print("\n=== Security Check Report ===\n")

if CRITICAL:
    print("CRITICAL (fix before deploy):")
    for c in CRITICAL: print(c)
    print()

if WARNINGS:
    print("Warnings:")
    for w in WARNINGS: print(w)
    print()

if INFO:
    print("Passed:")
    for i in INFO: print(i)

print()
print(f"Result: {len(CRITICAL)} critical, {len(WARNINGS)} warnings, {len(INFO)} passed")

if CRITICAL:
    sys.exit(2)
elif WARNINGS:
    sys.exit(1)
else:
    print("All clear — safe to deploy.")
    sys.exit(0)
