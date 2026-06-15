# Catalog Overview

## Size and structure

- **Total titles:** ~28,000 across all formats.
- **Format mix:** ~75% print, ~15% audiobook, ~10% large-print.
- **Acquisition pace:** ~40 new titles per month, reviewed by the
  cataloging team within 5 business days of arrival.

## Age-band metadata

Every item in the catalog is tagged with exactly one age band by the
cataloging team:

| Tag | Description |
|---|---|
| `child` | Picture books + early readers; approx. age 0–8 |
| `child` | Middle-grade fiction + non-fiction; approx. age 8–12 |
| `teen` | Young-adult fiction + reference; approx. age 13–17 |
| `adult` | General collection |

Missing or ambiguous tags are treated as `adult` for safety. The
cataloging team reviews and corrects gaps weekly.

## Subject distribution

- Fiction (general): ~38% of catalog
- Children's + young adult: ~24%
- Non-fiction (history, biography, science): ~18%
- Reference + local history: ~9%
- Spanish-language collection (any subject): ~11%

## Long-tail problem

About 52% of the catalog has not been checked out in the last 12 months.
Many of these titles are good, just under-discovered — they sit on
shelves while the same popular titles cycle through. A core goal of the
recommendation agent is to surface more of this long tail.

## API: `koha_catalog_api`

Internal REST API. Endpoints relevant to recommendations:

- `GET /catalog/search` — search by subject, format, language, age band
- `GET /catalog/title/{id}` — full record incl. age-band + availability
- `GET /catalog/availability/{id}` — current status + expected return date
- `POST /holds` — place a hold on behalf of an authenticated patron

All API calls require the kiosk's service-account token (managed by IT).
