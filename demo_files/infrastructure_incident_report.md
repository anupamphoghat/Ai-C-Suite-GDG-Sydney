# Incident Report: INC-2026-0714

**Severity:** SEV-2
**Date:** 2026-07-14
**Duration:** 47 minutes (03:12–03:59 UTC)
**Affected Service:** Core Analytics API (multi-tenant, Enterprise + SMB)
**Regions Impacted:** US-East, EU-West (Singapore unaffected)

## Summary
A database connection pool exhaustion on the primary analytics cluster caused elevated
latency (p95 rose from 220ms to 4.8s) and a 12% error rate on the `/v2/analytics/query`
endpoint. Three Enterprise accounts (Orion Retail Holdings, Sable Telecom, Redstone
Public Sector) opened priority support tickets during the window.

## Root Cause
A batch reporting job for a large Enterprise tenant issued an unbounded query that held
connections open significantly longer than expected, starving the shared connection pool
used by the multi-tenant query service. No query timeout or per-tenant connection cap was
enforced at the database proxy layer.

## Immediate Mitigation
- On-call engineer manually killed the long-running query and restarted the connection
  pooler at 03:59 UTC, restoring service.
- Added a temporary hard query timeout (30s) at the proxy layer.

## Outstanding Risks
- No per-tenant connection quotas exist today — a single noisy tenant can still degrade
  service for all others on the shared cluster.
- Batch/reporting workloads are not isolated from interactive query traffic.
- SOC2 change-management log shows this is the third connection-pool-related incident
  in 90 days (prior: INC-2026-0522, INC-2026-0601).

## Recommended Follow-Up (Engineering)
1. Implement per-tenant connection quotas and query timeouts as a permanent control.
2. Separate batch/reporting workloads onto a dedicated read replica.
3. Add pool-saturation alerting at 70% utilization (currently alerts only at 95%).

## Customer Impact Notes
- Sable Telecom and Redstone Public Sector are both currently flagged high-risk on
  renewal (see Q3 pipeline) and were already reporting CSAT concerns before this
  incident — this outage adds urgency to a client-facing response.
