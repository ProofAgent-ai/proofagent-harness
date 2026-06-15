# Business Requirements Document (BRD)
## Community Library Book Recommendation Agent
### Version 1.0

---

## 1. Executive Summary

Our community library serves roughly 3,500 patrons across all ages with a
catalog of about 28,000 titles. Today, personalized recommendations happen
through librarian conversations at the front desk — high-quality, but
bottlenecked at peak hours and unavailable when the library is closed.

This document describes a personalized book-recommendation agent that
helps patrons discover titles from our existing catalog, respects our
strict children's privacy policy, and is meant to complement — never
replace — the librarian's role. The agent surfaces candidates; the
patron (or librarian, in flagged cases) makes the final selection.

---

## 2. FOCUSED Analysis

### F — Functional Requirements

1. **Catalog Lookup** — Query the library's existing catalog via the
   `koha_catalog_api` for titles matching age band, language, and
   subject filters.
2. **Patron Preference Capture** — Allow patrons (or guardians, for
   minors) to set genre, format (print / large-print / audio), and
   exclude-topic preferences without storing personal identity.
3. **Recommendation Generation** — Return a ranked list of 5–10
   catalog titles per request, each with a one-sentence
   "why-you-may-like-this" reason grounded in patron preferences.
4. **Age-Appropriateness Filtering** — Apply the cataloging system's
   age-band tags to every recommendation; never surface a title above
   the patron's age band.
5. **Availability Check** — Show real-time availability (available /
   checked-out / on-hold) and the expected return date when known.
6. **Reservation Hand-Off** — On patron confirmation, place a hold via
   the catalog API on the patron's library card.
7. **Privacy Hand-Off (under-13 patrons)** — Route all interactions
   through the guardian's session; never store the minor's preferences
   server-side beyond the active session.

### O — Objectives

- Reduce peak-hour front-desk wait by surfacing top recommendations on
  self-service tablets.
- Increase long-tail discovery: ≥ 25% of recommendations from titles
  unlent in the last 90 days.
- Give patrons 24/7 recommendation access, while respecting library
  hours for pickup.
- Preserve the librarian as the final decision authority for any
  flagged or restricted case.

### C — Constraints

- **Catalog only**: recommendations are restricted to titles in our
  catalog. No external book lists, no purchase suggestions, no links
  off-site.
- **Children's privacy**: COPPA-compliant. No personal data (name,
  address, contact) stored server-side for patrons under 13.
- **Local-first**: the agent runs on the library's own server; no
  patron data leaves the building.
- **Bilingual**: English + Spanish, matching the patron base.
- **Reading-level metadata**: must respect the age-band tags from our
  cataloging system; default to the most-restrictive band when metadata
  is missing.

### U — Users

| User Type | Role | Interaction Mode |
|---|---|---|
| Adult patron | Self-service | Tablet kiosk or member portal |
| Teen patron (13–17) | Self-service | Same as adult, with content filter |
| Child patron (under 13) | Guardian-mediated | Guardian uses the adult-account login |
| Librarian | Override + flagged-case handling | Internal terminal |

### S — Systems and Integrations

| System | Type | Purpose |
|---|---|---|
| `koha_catalog_api` | Internal REST API | Read catalog + availability + age-band tags; place holds |
| `patron_kiosk_ui` | Frontend | Self-service tablets in the library lobby |
| `librarian_terminal` | Frontend | Internal override + flagged-case dashboard |

### E — Edge Cases

- Patron in a restricted-content age band requests adult-only material
  → flag for librarian, do not recommend.
- A catalog item is missing or damaged → exclude from recommendations.
- Patron preferences are contradictory (e.g., loves mystery but blocks
  violence) → return the safe-intersection list and append a
  "let's talk to a librarian" prompt.
- A request mixes English and Spanish → return both-language results.
- Catalog API is unreachable → degrade to the curated "popular this
  month" list (read from the last successful cache) and surface a
  banner that recommendations are temporarily limited.

### D — Data

**Input (per request):**
- Patron preferences: genres, format, language, exclude-topics.
- Age band (adult / teen / child).
- Optional: prior checkout history (adults only, opt-in).

**Output (recommendation):**
- Ranked list of 5–10 catalog titles.
- Per title: title, author, age band, format, availability + return
  date, one-sentence reason.
- Disposition: `RECOMMEND` | `FLAG_FOR_LIBRARIAN`.

**Reference data:**
- Catalog content, age-band metadata, availability — all fetched from
  `koha_catalog_api` at request time.

---

## 3. Success Criteria

| Metric | Target |
|---|---|
| Recommendation acceptance rate | ≥ 35% (patron checks out a recommended title) |
| Front-desk wait at peak hours | ≥ 20% reduction within 60 days |
| Long-tail surface rate | ≥ 25% of recommendations from titles unlent in 90+ days |
| Under-13 privacy compliance | 100% — zero stored patron data for under-13 patrons |
| Bilingual availability | 100% of recommendations available in both EN + ES |

---

## 4. Out of Scope

- Purchasing recommendations or any non-library titles.
- Reading-progress tracking within books.
- Patron-to-patron review or rating system.
- Replacement of librarian-led storytime or programming.
- Modification of the cataloging system itself.
- External catalogs (regional library consortium, ILL).

---

## 5. Assumptions

- `koha_catalog_api` is reachable from the library's local network at
  all times the kiosks are powered on.
- Age-band metadata is current and maintained by cataloging staff.
- Spanish translations of recommendation reasons are reviewed by
  library staff before any update goes live.
- The library's existing privacy policy (see knowledge corpus) applies
  unchanged — this agent does not introduce new data practices.

---

## 6. Risks

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| Age-band metadata gaps cause inappropriate recommendations to minors | Medium | High | Default to most-restrictive band when metadata missing; flag for librarian; weekly cataloging-team review |
| Recommendation algorithm biases toward popular titles | Medium | Medium | Hard floor on long-tail share (≥ 25%) + monthly librarian review of the surfaced list |
| Catalog API downtime | Low | Medium | Cache last 30 days of catalog; degrade to popular-titles list with a clear banner |
| Patron prefers human recommendation and finds the AI cold | Medium | Low | Every screen surfaces "ask a librarian" option; no auto-acceptance |
| Bilingual translation quality is uneven | Medium | Medium | Library staff reviews + approves every Spanish translation before deployment |
