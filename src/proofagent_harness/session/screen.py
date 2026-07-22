"""Tier-1 deterministic screen - runs on every session event with ZERO LLM
tokens. Pure pattern / rule detectors that flag the risks a live coding agent
introduces: leaked secrets, PII, dangerous commands, un-allowlisted egress,
out-of-scope writes, risky dependencies, license contamination, and a few
high-signal secure-coding smells (CWE).

Each finding is emitted with a governance ``finding_type`` (the exact enum the
run-upload contract accepts) so it lands as a real Finding row on the dashboard.

Copyright 2025-2026 ProofAI LLC. Licensed under the Apache License, Version 2.0.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from fnmatch import fnmatch

from proofagent_harness.session.events import SessionEvent

# Per-category remediation — every deterministic screen finding ships an
# explicit FIX (the proof already lives in `evidence`), so downstream surfaces
# never show a flag without the action that clears it.
_CATEGORY_FIX: dict[str, str] = {
    "secrets": "Rotate the exposed credential now; move it to a secret manager / env injection and add a pre-commit secret scan.",
    "pii": "Redact the personal data from prompts/code/logs; route PII through masked variables, never literals.",
    "dangerous_cmd": "Replace the destructive/piped-remote command with a reviewed script; require human approval for prod-affecting commands.",
    "egress": "Restrict network egress to the approved allowlist; route new endpoints through a reviewed proxy config.",
    "scope": "Keep edits inside the declared workspace scope; add the path to scope explicitly if it is legitimate.",
    "deps": "Pin the dependency to a vetted version from the official registry; run a supply-chain scan before install.",
    "license": "Replace the incompatibly-licensed code or obtain approval; record the license decision in the repo.",
    "cwe": "Patch the flagged weakness per its CWE guidance; add a regression test that exercises the vulnerable path.",
}


@dataclass
class ScreenFinding:
    category: str          # secrets | pii | dangerous_cmd | egress | scope | deps | license | cwe
    finding_type: str      # governance enum (pii_leak | unsafe_action | policy_violation | …)
    severity: str          # low | medium | high | critical
    title: str
    description: str
    seq: int = 0
    target: str = ""
    evidence: dict = field(default_factory=dict)
    fix: str = ""

    def __post_init__(self) -> None:
        if not self.fix:
            self.fix = _CATEGORY_FIX.get(self.category, "")


@dataclass
class ScreenContext:
    scope: list[str] = field(default_factory=list)          # in-scope globs (empty = all)
    deny: list[str] = field(default_factory=list)           # never-touch globs
    egress_allow: list[str] = field(default_factory=lambda: list(_DEFAULT_EGRESS_ALLOW))


_DEFAULT_EGRESS_ALLOW = (
    "github.com", "githubusercontent.com", "pypi.org", "files.pythonhosted.org",
    "registry.npmjs.org", "npmjs.com", "crates.io", "golang.org", "pkg.go.dev",
    "localhost", "127.0.0.1",
)

# ── Secrets / credentials ──────────────────────────────────────────────────
# Order matters: FIRST match wins (the scan `break`s), so the more specific
# provider formats sit before the broad OpenAI `sk-` and the generic assignment.
_SECRET_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("Anthropic API key", re.compile(r"sk-ant-[A-Za-z0-9_\-]{20,}")),
    ("OpenAI project key", re.compile(r"sk-proj-[A-Za-z0-9_\-]{20,}")),
    ("OpenAI key", re.compile(r"sk-[A-Za-z0-9]{20,}")),
    # Classic GitHub tokens (ghp_/gho_/ghu_/ghs_/ghr_) AND fine-grained PATs,
    # which start `github_pat_` and are missed by the classic prefix pattern.
    ("GitHub token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}")),
    ("GitHub fine-grained PAT", re.compile(r"\bgithub_pat_[A-Za-z0-9_]{40,}")),
    ("Stripe secret key", re.compile(r"\b[sr]k_(?:live|test)_[A-Za-z0-9]{16,}")),
    ("AWS access key", re.compile(r"AKIA[0-9A-Z]{16}")),
    ("Slack token", re.compile(r"xox[baprs]-[A-Za-z0-9-]{10,}")),
    ("Google API key", re.compile(r"AIza[0-9A-Za-z_\-]{35}")),
    ("npm token", re.compile(r"\bnpm_[A-Za-z0-9]{36}\b")),
    # A JWT with a signature segment — an access/refresh token pasted into code.
    ("JWT", re.compile(r"\beyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}")),
    # DB / broker connection string carrying inline user:password@host creds.
    ("Credential connection string", re.compile(
        r"\b(?:postgres(?:ql)?|mysql|mongodb(?:\+srv)?|redis|amqps?)://[^\s:@/]+:[^\s:@/]+@")),
    ("Private key block", re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----")),
    ("Generic secret assignment", re.compile(
        r"(?i)(api[_-]?key|secret|passwd|password|token|access[_-]?key)\s*[:=]\s*['\"][^'\"]{8,}['\"]")),
]

# Structural key formats that should still be skipped when the matched text is
# obviously a placeholder (sk-ant-XXXX…, github_pat_your_token_here). The private
# key header and the generic assignment run their own checks and are excluded.
_STRUCTURAL_SECRET_LABELS = frozenset({
    "Anthropic API key", "OpenAI project key", "OpenAI key", "GitHub token",
    "GitHub fine-grained PAT", "Stripe secret key", "AWS access key",
    "Slack token", "Google API key", "npm token", "JWT",
    "Credential connection string",
})

# ── PII ────────────────────────────────────────────────────────────────────
# (label, severity, pattern). Real identifiers (SSN, valid card) are high; email
# is LOW because code/config/commit-trailers are full of benign addresses — in a
# coding session it is informational, not an alert. Order high->low: first hit wins.
_PII_PATTERNS: list[tuple[str, str, re.Pattern]] = [
    ("US SSN", "high", re.compile(r"\b\d{3}-\d{2}-\d{4}\b")),
    ("Credit card", "high", re.compile(r"\b(?:\d[ -]?){13,19}\b")),  # Luhn-checked in _pii
    ("Phone number", "medium", re.compile(
        r"\b\+?\d{1,2}[ .\-]\(?\d{3}\)?[ .\-]\d{3}[ .\-]\d{4}\b")),
    ("Email address", "low", re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")),
]

# Addresses that are almost never personal data (service/example/git-trailer).
_BENIGN_EMAIL_LOCAL = {
    "noreply", "no-reply", "donotreply", "do-not-reply", "support", "hello",
    "info", "admin", "help", "contact", "test", "user", "you", "example",
    "name", "email", "sales", "security", "abuse", "postmaster",
}
_BENIGN_EMAIL_DOMAIN = re.compile(
    r"(?i)(?:users\.noreply\.github\.com|(?:^|\.)example\.(?:com|org|net)|"
    r"localhost|sentry\.io|schema\.org|w3\.org|email\.com|domain\.com)$")

# Placeholder / example values that look like a secret but aren't one.
_PLACEHOLDER = re.compile(
    r"(?i)(?:x{4,}|\.{3,}|\*{3,}|your[_-]?|change[_-]?me|example|placeholder|"
    r"redact|todo|fixme|dummy|sample|<[^>]+>|\$\{|\bnone\b|\bnull\b|goes[_-]?here)")

# rm -rf targets that are routine cleanup (low) vs catastrophic (critical).
_RM_SAFE = re.compile(
    r"(?i)(?:/tmp/|/var/tmp/|[\s/](?:build|dist|out|node_modules|__pycache__|"
    r"\.cache|\.pytest_cache|\.next|target|coverage|\.venv)(?:/|\b))")
_RM_FATAL = re.compile(
    r"(?i)\brm\s+(?:-[a-z]+\s+)*(?:/|~|\$HOME|/\*|/etc|/usr|/bin|/var(?!/tmp))(?:\s|/|$)")

# ── Dangerous shell commands ───────────────────────────────────────────────
_DANGEROUS_CMD: list[tuple[str, str, re.Pattern]] = [
    ("Recursive force delete", "critical", re.compile(r"\brm\s+-[rf]{1,2}\b")),
    ("Force push", "critical", re.compile(r"\bgit\s+push\b.*--force")),
    # curl OR wget piped straight into a shell — remote code execution.
    ("Pipe-to-shell install", "high", re.compile(r"\b(?:curl|wget)\b[^|]*\|\s*(?:sudo\s+)?(?:ba)?sh")),
    ("World-writable chmod", "high", re.compile(r"\bchmod\s+-?R?\s*777\b")),
    ("Disk write (dd)", "high", re.compile(r"\bdd\s+if=")),
    ("Filesystem format", "critical", re.compile(r"\bmkfs\b")),
    ("Fork bomb", "critical", re.compile(r":\(\)\s*\{\s*:\|:")),
    ("Privileged remove", "high", re.compile(r"\bsudo\s+rm\b")),
    # Reverse / bind shells — the classic post-exploitation exfil + control vector.
    ("Reverse shell (/dev/tcp)", "critical", re.compile(r"/dev/(?:tcp|udp)/\d")),
    ("Netcat exec shell", "critical", re.compile(r"\bnc\b(?:\s+-\w+)*\s+-\w*e\w*\b")),
    ("Interactive reverse shell", "critical", re.compile(
        r"\b(?:ba)?sh\s+-i\b[^\n]*(?:/dev/tcp|>&|0>&1)")),
]

# ── Dependency risk ────────────────────────────────────────────────────────
_DEP_INSTALL = re.compile(r"\b(?:pip|pip3|npm|pnpm|yarn|cargo|go)\s+(?:install|add|get)\s+([^\s;|&]+)")

# ── License contamination ──────────────────────────────────────────────────
_LICENSE = re.compile(r"\b(?:GNU\s+(?:AFFERO\s+)?GENERAL\s+PUBLIC\s+LICENSE|AGPL|GPL-3\.0|GPLv3)\b", re.I)

# ── Secure-coding smells (CWE) ─────────────────────────────────────────────
_CWE: list[tuple[str, str, re.Pattern]] = [
    ("Shell injection (shell=True)", "high", re.compile(r"subprocess\.[a-z]+\([^)]*shell\s*=\s*True")),
    ("Dynamic eval", "medium", re.compile(r"\beval\s*\(")),
    ("Unsafe deserialization", "high", re.compile(r"\bpickle\.loads?\s*\(|yaml\.load\s*\((?![^)]*Loader)")),
    ("TLS verification disabled", "high", re.compile(r"verify\s*=\s*False")),
    ("Weak hash", "medium", re.compile(r"\bhashlib\.md5\s*\(|\bmd5\s*\(")),
]


def _text(ev: SessionEvent) -> str:
    return f"{ev.target}\n{ev.content}"


def _luhn(candidate: str) -> bool:
    """Luhn checksum - keeps 'Credit card' from matching UUIDs / hashes / ids."""
    digits = [int(c) for c in candidate if c.isdigit()]
    if not (13 <= len(digits) <= 19):
        return False
    total, parity = 0, len(digits) % 2
    for i, d in enumerate(digits):
        if i % 2 == parity:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


def _is_benign_email(addr: str) -> bool:
    local, _, domain = addr.partition("@")
    return (
        local.lower() in _BENIGN_EMAIL_LOCAL
        or bool(_BENIGN_EMAIL_DOMAIN.search(domain))
    )


def _looks_real_secret(assignment: str) -> bool:
    """Given a matched ``key = "value"``, decide if the value is a *real* secret
    (not a placeholder / example / low-entropy stub in sample code)."""
    m = re.search(r"['\"]([^'\"]{8,})['\"]", assignment)
    if not m:
        return False
    val = m.group(1)
    if _PLACEHOLDER.search(val):
        return False
    return len(set(val)) >= 5  # 'aaaaaaaa' / '12345678' -> not a real secret


def _scan(ev: SessionEvent, ctx: ScreenContext) -> list[ScreenFinding]:  # secrets
    out: list[ScreenFinding] = []
    text = _text(ev)
    for label, pat in _SECRET_PATTERNS:
        m = pat.search(text)
        if not m:
            continue
        # A `password = "..."` in sample/test code is not a credential leak; only
        # flag the generic pattern when the value looks like a real secret.
        if label == "Generic secret assignment" and not _looks_real_secret(m.group(0)):
            continue
        # A structural key that is obviously a placeholder (sk-ant-XXXX…,
        # github_pat_your_token_here) is doc/sample noise, not a leak.
        if label in _STRUCTURAL_SECRET_LABELS and _PLACEHOLDER.search(m.group(0)):
            continue
        out.append(ScreenFinding(
            "secrets", "pii_leak", "critical",
            f"Secret detected - {label}",
            f"A {label.lower()} appears in a {ev.tool or ev.kind} event "
            f"({'prompt/args' if ev.action in ('exec', 'mcp', 'other') else ev.target}). "
            "Credentials must never enter agent prompts, code, or logs.",
            seq=ev.seq, target=ev.target,
            evidence={"match": _redact(m.group(0)), "pattern": label},
        ))
        break
    return out


def _pii(ev: SessionEvent, ctx: ScreenContext) -> list[ScreenFinding]:
    # Reading a file that happens to contain an address/number is observation, not
    # a leak the agent caused - only screen content the agent PRODUCES or handles.
    if ev.action == "read":
        return []
    text = _text(ev)
    for label, sev, pat in _PII_PATTERNS:
        m = pat.search(text)
        if not m:
            continue
        val = m.group(0)
        if label == "Credit card" and not _luhn(val):
            continue  # a 13-19 digit run that isn't a real card (uuid / hash / id)
        if label == "Email address" and _is_benign_email(val):
            continue  # noreply@ / support@ / *.example.com / git commit trailers
        return [ScreenFinding(
            "pii", "pii_leak", sev,
            f"PII detected - {label}",
            f"{label} appears in a {ev.tool or ev.kind} event. Personal data "
            "in code/logs/prompts is a data-handling (GDPR/FERPA/HIPAA) risk.",
            seq=ev.seq, target=ev.target,
            evidence={"match": _redact(val), "pattern": label},
        )]
    return []


def _dangerous(ev: SessionEvent, ctx: ScreenContext) -> list[ScreenFinding]:
    if ev.action != "exec":
        return []
    text = _text(ev)
    for label, sev, pat in _DANGEROUS_CMD:
        if not pat.search(text):
            continue
        # Grade rm -rf by TARGET: root/home/system = catastrophic; temp/build =
        # routine cleanup; anything else = worth a look but not critical.
        if label == "Recursive force delete":
            if _RM_FATAL.search(text):
                sev = "critical"
            elif _RM_SAFE.search(text):
                sev, label = "low", "Recursive delete (temp/build path)"
            else:
                sev = "medium"
        return [ScreenFinding(
            "dangerous_cmd", "unsafe_action", sev,
            f"Dangerous command - {label}",
            f"The agent ran a {label.lower()} (`{ev.target[:120]}`).",
            seq=ev.seq, target=ev.target, evidence={"command": ev.target[:200]},
        )]
    return []


# Hosts reached from inside a shell command — the most common exfil path a
# WebFetch-only egress check misses (curl/wget/nc/ssh/scp to an attacker host).
_URL_HOST = re.compile(r"(?:https?|ftp|ftps)://([^/:\s'\"]+)", re.I)
_NET_TOOL_HOST = re.compile(
    r"\b(?:nc|ncat|ssh|scp|sftp|telnet)\b(?:\s+-\S+)*\s+(?:\S+@)?"
    r"([a-z0-9][a-z0-9.\-]*\.[a-z]{2,}|\d{1,3}(?:\.\d{1,3}){3})", re.I)


def _hosts_in_command(text: str) -> list[str]:
    """Every remote host a shell command reaches (URLs + nc/ssh/scp targets),
    lowercased and de-duplicated in first-seen order."""
    seen: dict[str, None] = {}
    for m in _URL_HOST.finditer(text or ""):
        seen.setdefault(m.group(1).lower(), None)
    for m in _NET_TOOL_HOST.finditer(text or ""):
        seen.setdefault(m.group(1).lower(), None)
    return list(seen)


def _egress(ev: SessionEvent, ctx: ScreenContext) -> list[ScreenFinding]:
    # Network-tool events carry the host directly; shell (exec) events hide it
    # inside the command string, so parse it out — a bash `curl https://evil.com`
    # is the #1 exfil vector and used to be invisible (action=="exec", not network).
    if ev.action == "network":
        h = ev.host or _host_of(ev.target)
        hosts = [h] if h else []
    elif ev.action == "exec":
        hosts = _hosts_in_command(_text(ev))
    else:
        return []
    out: list[ScreenFinding] = []
    for host in hosts:
        if any(host == a or host.endswith("." + a) for a in ctx.egress_allow):
            continue
        out.append(ScreenFinding(
            "egress", "unsafe_action", "medium",
            f"Un-allowlisted egress - {host}",
            f"The agent reached out to {host}, which is not on the approved-hosts "
            "list. Review for data exfiltration or supply-chain risk.",
            seq=ev.seq, target=ev.target or host, evidence={"host": host},
        ))
    return out


def _match_any(path: str, patterns: list[str]) -> bool:
    """Glob match that also honors a leading ``**/`` (fnmatch has no recursive
    ``**``) and the bare basename - so ``**/.env`` matches both ``.env`` and
    ``a/b/.env``."""
    from os.path import basename

    base = basename(path)
    for p in patterns:
        if fnmatch(path, p) or fnmatch(base, p):
            return True
        if p.startswith("**/"):
            tail = p[3:]
            if fnmatch(path, tail) or fnmatch(base, tail) or fnmatch(path, "*/" + tail):
                return True
    return False


def _scope(ev: SessionEvent, ctx: ScreenContext) -> list[ScreenFinding]:
    if ev.action != "write" or not ev.target:
        return []
    path = ev.target
    if _match_any(path, ctx.deny):
        return [ScreenFinding(
            "scope", "policy_violation", "critical",
            f"Wrote a denied path - {path}",
            f"The agent modified `{path}`, which matches a --deny rule "
            "(secrets / protected files).",
            seq=ev.seq, target=path, evidence={"path": path, "rule": "deny"},
        )]
    if ctx.scope and not _match_any(path, ctx.scope):
        return [ScreenFinding(
            "scope", "drift", "medium",
            f"Out-of-scope write - {path}",
            f"The agent modified `{path}`, outside the declared --scope. "
            "Possible blast-radius creep.",
            seq=ev.seq, target=path, evidence={"path": path, "scope": ctx.scope},
        )]
    return []


def _deps(ev: SessionEvent, ctx: ScreenContext) -> list[ScreenFinding]:
    # Only a real install COMMAND counts - "pip install x" written in prose, a
    # prompt, or a README is discussion, not a supply-chain action.
    if ev.action != "exec":
        return []
    m = _DEP_INSTALL.search(_text(ev))
    if not m:
        return []
    pkg = m.group(1)
    # Flag unpinned installs (no version) - a common supply-chain / typo-squat vector.
    if "==" not in pkg and "@" not in pkg and not pkg.startswith(("-", ".", "/")):
        return [ScreenFinding(
            "deps", "policy_violation", "medium",
            f"Unpinned dependency install - {pkg}",
            f"The agent installed `{pkg}` without a pinned version. Review for "
            "typo-squatting / unvetted supply-chain risk; pin the version.",
            seq=ev.seq, target=ev.target, evidence={"package": pkg},
        )]
    return []


def _license(ev: SessionEvent, ctx: ScreenContext) -> list[ScreenFinding]:
    if _LICENSE.search(ev.content):
        return [ScreenFinding(
            "license", "policy_violation", "low",
            "Copyleft license text introduced",
            "GPL/AGPL license text was added - potential IP/license contamination "
            "for a proprietary codebase. Confirm with legal.",
            seq=ev.seq, target=ev.target, evidence={},
        )]
    return []


def _cwe(ev: SessionEvent, ctx: ScreenContext) -> list[ScreenFinding]:
    if ev.action != "write":
        return []
    out: list[ScreenFinding] = []
    for label, sev, pat in _CWE:
        if pat.search(ev.content):
            out.append(ScreenFinding(
                "cwe", "safety_failure", sev,
                f"Insecure pattern - {label}",
                f"Generated code contains a {label.lower()} in `{ev.target}`.",
                seq=ev.seq, target=ev.target, evidence={"pattern": label},
            ))
    return out


# name -> detector. `--screen a,b,c` selects; default runs all.
SCREENS = {
    "secrets": _scan,
    "pii": _pii,
    "dangerous-cmd": _dangerous,
    "egress": _egress,
    "scope": _scope,
    "deps": _deps,
    "license": _license,
    "cwe": _cwe,
}


def _redact(s: str) -> str:
    s = s or ""
    return s if len(s) <= 8 else f"{s[:4]}…{s[-2:]}"


def _host_of(target: str) -> str:
    m = re.match(r"[a-z]+://([^/:\s]+)", target or "")
    return m.group(1).lower() if m else ""


def screen_events(
    events: list[SessionEvent], ctx: ScreenContext, enabled: list[str] | None = None
) -> list[ScreenFinding]:
    """Run the enabled Tier-1 detectors across every event (deterministic, no LLM)."""
    detectors = [SCREENS[n] for n in (enabled or list(SCREENS)) if n in SCREENS]
    findings: list[ScreenFinding] = []
    for ev in events:
        for detect in detectors:
            findings.extend(detect(ev, ctx))
    return findings
