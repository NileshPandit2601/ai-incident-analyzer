# Evaluation results (2026-10-23 23:49)

Model `qwen3.5:9b`, num_ctx 8192, temperature 0.0, runs 6

| Metric | Value |
|---|---|
| json_valid_rate | 1.0 |
| severity_acceptable_rate | 0.83 |
| severity_exact_rate | 0.33 |
| rule_severity_acceptable_rate | 1.0 |
| mean_cause_concepts | 1.0 |
| mean_grounding | 0.9 |
| mean_llm_time_s | 341.4 |

| Sample | Run | Expected | AI severity | Rule severity | Cause concepts | Grounding | Unsupported numbers | LLM time (s) |
|---|---|---|---|---|---|---|---|---|
| payment_db_timeout.log | 1 | Critical | Critical | Critical | 1.0 | 0.75 | - | 421.3 |
| memory_leak_oom.log | 1 | High | Critical | Critical | 1.0 | 1.0 | - | 318.3 |
| auth_token_expiry.log | 1 | High | High | High | 1.0 | 1.0 | - | 426.2 |
| mixed_json_format.log | 1 | High | Critical ✗ | High | 1.0 | 1.0 | - | 296.7 |
| disk_space_warning.log | 1 | Medium | High | Medium | 1.0 | 0.67 | - | 302.1 |
| minor_retry_noise.log | 1 | Low | Medium | Low | 1.0 | 1.0 | - | 283.8 |

## Likely causes produced

- **payment_db_timeout.log** (run 1): The nightly batch-reconciler job (rc-20261020) started a heavy scan of the transactions table, holding connections open and exhausting the pool.
- **memory_leak_oom.log** (run 1): The ReportTemplateCache grew unbounded because no eviction policy is configured, eventually exhausting the 2GB heap and triggering an OutOfMemoryError.
- **auth_token_expiry.log** (run 1): The immediate removal of the old signing key (grace_period=0s) caused a window where tokens signed with the old key were rejected by profile-service and tokens signed with the new key were rejected by orders-service due to stale local caches.
- **mixed_json_format.log** (run 1): The deployment of inventory-api v2.14.0 likely changed the data access logic to require 'warehouse_id', but legacy data imported by the job in L16 lacks this field, causing KeyError exceptions.
- **disk_space_warning.log** (run 1): The log-shipper service's AWS (or cloud) credentials expired, causing upload failures. The system cannot delete local rotated files because the upload fails, leading to disk space exhaustion on web-03.
- **minor_retry_noise.log** (run 1): The primary issue is a transient network instability causing SMTP resets; the secondary issue is a missing user attribute ('first_name') in the data pipeline feeding the newsletter template.