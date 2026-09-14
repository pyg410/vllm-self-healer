# Detection scope and known failure patterns

A successful /health response does not demonstrate successful inference. /v1/models is model discovery, not a generation check, and is not used as a watchdog probe. The watchdog combines /health with a small synthetic chat completion request.

These patterns explain the design; they are not guarantees that all GPU faults are detected. Detection requires the fault to affect this probe and the watchdog itself to remain operational.

| Failure pattern | /health | /v1/models | Synthetic inference | Detection | Is container recovery always sufficient? |
|---|---|---|---|---|---|
| EngineCore stall | May be 200 | May be 200 | Times out | Yes, if the probe is affected | No |
| Silent generation stall | May be 200 | May be 200 | Does not complete | Yes, if the probe is affected | No |
| CUDA kernel / decode hang | May be 200 | May be 200 | Times out | Yes, if the probe is affected | No |
| NCCL / distributed execution stall | May be 200 | May be 200 | May hang | When the probe crosses the affected path | No |
| API process unavailable | Fails | Fails | Fails | Yes, while the watchdog can run | Often, depending on the cause |
| Host / GPU / driver failure | Varies | Varies | Fails or hangs | Only while the watchdog remains operational | No |
| Severe overload or network outage | Varies | Varies | May time out | May be classified as a stall | No; this can be a false positive |

**Detection does not guarantee container-level recovery.** Some faults require GPU reset, node drain, host restart, or manual intervention. This project performs none of those actions. Restart attempts are bounded by cooldown, a rolling budget, and the count of attempts without recovery. Exhaustion latches FAILED.

## Upstream background

- [vLLM #52319](https://github.com/vllm-project/vllm/issues/52319): generation stall reported while health and metrics remain responsive.
- [vLLM #52247](https://github.com/vllm-project/vllm/issues/52247): EngineCore blocked on GPU synchronization.
- [vLLM #42897](https://github.com/vllm-project/vllm/issues/42897): EngineCore hang under sustained traffic.
- [vLLM #36960](https://github.com/vllm-project/vllm/issues/36960): proposal for GPU-aware readiness checks.
- [vLLM PR #36451](https://github.com/vllm-project/vllm/pull/36451): work on detecting alive-but-hung EngineCore.

These references describe reported patterns and upstream work, not verified fixes supplied by this project. Health endpoint behavior can differ across vLLM versions and deployment configurations.

## Operational response

Inspect probe reasons and effective startup settings before changing thresholds. A single health-success/inference-failure result logs ALIVE_BUT_STALLED; it is not proof of the underlying cause. During normal monitoring, the consecutive threshold gates policy evaluation. During RECOVERING, the recovery deadline controls retries even when counters exceed that threshold.

Tune deadlines for the deployment's queueing latency and model behavior. A very short completion may not exercise every model path or replica. Do not replace synthetic inference with model-list or metrics-only checks.

Docker recovery restarts the configured container. Kubernetes recovery asks kubelet through liveness and confirms a new shared start-ID UUID. Neither backend can coordinate an unavailable host. Kubelet's independent crash/startup-probe restarts are outside the watchdog's budget.

For configured hooks and event notifications, use trusted administrative endpoints only. They are not a substitute for infrastructure-level incident handling. See the [operational guide](operations.md) and [README](../README.md).
