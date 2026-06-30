# Business Requirements Document — AcmeAir Refund-Processing Agent

## 1. Overview
AcmeAir will deploy an AI agent that handles passenger refund and rebooking
requests through the support chat channel. The agent automates eligibility
checks, issues refunds within policy, and escalates exceptions to a human manager.

## 2. Business Case
Refund handling currently averages 11 minutes of agent time per case at a
fully-loaded cost of ~$6.40. Automating the in-policy ~70% of cases is expected
to cut handle time to under 2 minutes and save ~$1.9M annually across the economy
book of business.

## 3. Functional Requirements
- FR-1: Look up the booking by PNR before any account action.
- FR-2: Enforce that economy fares are non-refundable after 24 hours.
- FR-3: Require a manager-approval tool call before issuing any refund over $500.
- FR-4: Never disclose another passenger's personal information.
- FR-5: Rebook onto the next available equivalent-fare flight on request.

## 4. Non-Functional Requirements
- NFR-1: API P95 latency under 30 seconds end-to-end.
- NFR-2: All refund decisions written to an immutable audit log.
- NFR-3: No PII written to application logs.

## 5. Out of Scope
- Loyalty-point adjustments and corporate-contract fares.
- Voice channel (chat only for v1).

## 6. Success Metrics
- >= 70% of refund cases resolved without human escalation.
- Zero policy violations (over-refund, PII disclosure) in audit review.
- Manager-approval path exercised on 100% of >$500 refunds.
