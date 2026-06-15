"""Domain glossary packs — inject industry-specific terminology into
juror prompts when `AgentArtifact.metadata["domain"]` is set.

Why these exist: artifacts in regulated industries use jargon the
juror may not recognize without context. An airline BRD mentions PNR,
fare class, IATA codes; a healthcare runbook mentions HIPAA, PHI, ICD
codes. Without a glossary, jurors may either miss the meaning OR flag
real terms as hallucinations.

Add a domain by:
  1. Add a `_pack_<domain>()` function returning a markdown glossary.
  2. Register it in `_REGISTRY`.

Unknown domains return empty (juror uses base prompt unchanged).
"""

from __future__ import annotations

from typing import Callable

CANONICAL_DOMAINS: list[str] = [
    "airline", "healthcare", "fintech", "retail", "logistics", "gov",
]


def get_domain_pack(domain: str | None) -> str:
    """Return a markdown glossary block for the named domain.

    Returns empty string for unknown / None domains.
    """
    if not domain:
        return ""
    fn = _REGISTRY.get(domain.lower().replace(" ", "_").replace("-", "_"))
    if fn is None:
        return ""
    return fn()


def _pack_airline() -> str:
    return """## Domain glossary — airline / travel

Recognized terms (don't flag as hallucinations if used correctly):
  - **PNR**: Passenger Name Record — the 6-character booking reference.
  - **FFP**: Frequent Flyer Program. **FFP number**: the customer's loyalty ID.
  - **Fare class / fare bucket**: single-letter inventory code (Y, B, M, etc.).
  - **IATA / ICAO codes**: 3-letter (airport) and 4-letter (airline) standards.
  - **MCO**: Miscellaneous Charges Order — a payment instrument.
  - **EMD**: Electronic Miscellaneous Document — for ancillary services.
  - **GDS**: Global Distribution System (Sabre, Amadeus, Travelport).
  - **NDC**: New Distribution Capability — IATA's modern booking standard.
  - **Codeshare**: a flight marketed by airline A but operated by airline B.
  - **Refund vs Reissue**: refund returns funds; reissue produces a new ticket.

Common upstream / downstream systems: Sabre PSS, Amadeus, Navitaire,
SITA, ARC, IATA BSP, Stripe / Adyen for payment processing.
"""


def _pack_healthcare() -> str:
    return """## Domain glossary — healthcare

Recognized terms:
  - **PHI**: Protected Health Information (HIPAA-regulated).
  - **HIPAA**: US privacy regulation. **HITECH**: enforcement amendment.
  - **EHR / EMR**: Electronic Health / Medical Record.
  - **HL7 / FHIR**: clinical-data interoperability standards.
  - **ICD-10 / ICD-11**: diagnosis classification.
  - **CPT codes**: procedure billing codes.
  - **NPI**: National Provider Identifier.
  - **PA**: Prior Authorization. **EOB**: Explanation of Benefits.
  - **BAA**: Business Associate Agreement (HIPAA contract).
  - **De-identification**: Safe Harbor (18-element) vs Expert Determination.

Compliance posture flags: any handling of PHI requires BAA, encryption at
rest + in transit, audit logging, minimum-necessary access controls.
"""


def _pack_fintech() -> str:
    return """## Domain glossary — fintech / financial services

Recognized terms:
  - **KYC / KYB / AML / CDD / EDD**: identity + due-diligence frameworks.
  - **SOC 2 (Type I / Type II)**: trust services audit.
  - **PCI-DSS**: Payment Card Industry Data Security Standard.
  - **ACH / SWIFT / SEPA / Wire**: payment rails.
  - **FedNow / RTP**: US instant-payment rails.
  - **PII / PCI / NPI (financial)**: regulated data categories.
  - **GDPR / CCPA / GLBA / Reg DD / Reg E / Reg Z**: privacy + consumer protection regs.
  - **OFAC / sanctions screening**: required pre-transaction checks.
  - **3DS / 3-D Secure**: cardholder authentication for online payments.
  - **Tokenization** vs **encryption**: PCI-compliant card-data handling.

Common platforms: Stripe, Plaid, Marqeta, Galileo, Synapse, Currents, Modern Treasury.
"""


def _pack_retail() -> str:
    return """## Domain glossary — retail / e-commerce

Recognized terms:
  - **SKU / GTIN / UPC**: product identifiers.
  - **OMS / WMS / ERP**: Order / Warehouse / Enterprise Resource systems.
  - **PIM**: Product Information Management.
  - **MAP**: Minimum Advertised Price.
  - **Drop-ship**: supplier ships direct to customer; retailer holds no inventory.
  - **3PL / 4PL**: third-/fourth-party logistics provider.
  - **CDP**: Customer Data Platform.
  - **AOV / CAC / LTV**: average order value, customer acquisition cost, lifetime value.
  - **Click-and-collect / BOPIS**: buy online, pick up in store.

Compliance: PCI-DSS for card storage, CCPA / state privacy laws,
ADA / WCAG for ecommerce sites.
"""


def _pack_logistics() -> str:
    return """## Domain glossary — logistics / supply chain

Recognized terms:
  - **BOL / BL**: Bill of Lading. **POD**: Proof of Delivery.
  - **HS code / Schedule B**: customs classification.
  - **Incoterms 2020**: FCA, EXW, CIP, DDP, etc. — risk/cost transfer points.
  - **ASN**: Advance Shipping Notice (EDI 856).
  - **EDI 850 / 855 / 856 / 810 / 940 / 945**: order / acknowledgement / ASN /
    invoice / warehouse-shipping documents.
  - **TMS / YMS / WMS**: Transportation / Yard / Warehouse Management System.
  - **LTL / FTL / Intermodal**: trucking modes.
  - **HAZMAT / DG**: hazardous materials / dangerous goods.

Compliance: C-TPAT for US imports, IMDG for ocean DG, IATA DGR for air DG.
"""


def _pack_gov() -> str:
    return """## Domain glossary — government / public sector

Recognized terms:
  - **FedRAMP (Low / Moderate / High)**: federal cloud-security baseline.
  - **FISMA**: federal info-security mgmt act.
  - **CMMC**: Cybersecurity Maturity Model Certification (DoD contractors).
  - **NIST SP 800-53 / 800-171**: security control catalogs.
  - **CUI**: Controlled Unclassified Information.
  - **ATO**: Authority To Operate.
  - **FOIA**: Freedom of Information Act.
  - **Section 508**: federal accessibility requirement.

Procurement: SBIR/STTR, GSA Schedule, OASIS, CIO-SP3, GWAC vehicles.
Acquisition regs: FAR / DFARS / CFR.
"""


_REGISTRY: dict[str, Callable[[], str]] = {
    "airline": _pack_airline,
    "travel": _pack_airline,
    "aviation": _pack_airline,
    "healthcare": _pack_healthcare,
    "health": _pack_healthcare,
    "medical": _pack_healthcare,
    "fintech": _pack_fintech,
    "finance": _pack_fintech,
    "banking": _pack_fintech,
    "payments": _pack_fintech,
    "retail": _pack_retail,
    "ecommerce": _pack_retail,
    "commerce": _pack_retail,
    "logistics": _pack_logistics,
    "supply_chain": _pack_logistics,
    "shipping": _pack_logistics,
    "gov": _pack_gov,
    "government": _pack_gov,
    "public_sector": _pack_gov,
    "federal": _pack_gov,
}
