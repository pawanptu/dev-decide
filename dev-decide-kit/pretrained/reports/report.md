# dev-decide-kit benchmark

Trained 2026-10-23 13:07:56. Teacher: haiku writes, sonnet labels. Bootstrap: 89 model calls, about $1.22 of usage.

Accuracy means agreement with the teacher's labels on examples the model never saw, unless a decision says its test set is human gold labels. *CV* is 5-fold cross-validation on the development set; *test* is a separate held-out split that no choice ever looked at.

## Summary

| Decision | Model | CV accuracy | Test accuracy (95% CI) | Majority baseline | ECE raw → calibrated (noise floor) | Auto-accept at 90% target (test) | Model p50 / p99 | Size | Status |
|---|---|---|---|---|---|---|---|---|---|
| route | f4, C=16 | 84.9% | 90.6% (80%–96%) | 11% | 0.287 → 0.064 (0.086) | 91% answered, 94% right | 973 µs / 1.89 ms | 718 KB | **enabled** |
| risk | f2, C=64 | 46.8% | 68.2% (47%–84%) | 30% | 0.267 → 0.196 (0.219) | 9% answered, 100% right | 573 µs / 1.85 ms | 272 KB | not enabled |
| breaking_change | f1, C=1, balanced | 73.4% | 63.2% (41%–81%) | 68% | 0.068 → 0.191 (0.198) | 37% answered, 71% right | 152 µs / 408 µs | 65 KB | not enabled |
| issue_type | f1, C=64 | 90.7% | 95.5% (85%–99%) | 19% | 0.185 → 0.052 (0.066) | 98% answered, 98% right | 96 µs / 168 µs | 186 KB | **enabled** |
| severity | f1, C=4, balanced | 75.6% | 78.6% (64%–88%) | 50% | 0.286 → 0.133 (0.114) | 69% answered, 83% right | 96 µs / 243 µs | 156 KB | not enabled |
| domain | f3, C=16, balanced | 67.7% | 68.4% (53%–81%) | 4% | 0.516 → 0.150 (0.162) | 47% answered, 89% right | 1.36 ms / 2.99 ms | 1847 KB | not enabled |

Ordered labels: risk is within one level 82% of the time on test (MAE 0.59 levels); severity is within one level 95% of the time on test (MAE 0.26 levels).

## Speed against Jev and the LLM tier

| Path | Latency for one decision | Relative to the analyzers + model |
|---|---|---|
| This kit, model only (featurize + predict) | 0.36 ms median (0.10–1.36 across decisions) | - |
| This kit, dev-decide analyzers + model (in-process) | 1.00 ms median (0.54–2.77), worst p99 10.1 ms | 1× |
| Jev (TypeSafe AI, reported, over the network) | 70–500 ms | 70–501× slower |
| dev-decide's LLM tier (one Claude call, measured) | 2.5–4.5 s | 2,503–4,505× slower |

Latencies are pure-Python inference on one CPU core, in a running process, with no network, and cover the analyzers and the model only. A whole dev-decide request also parses the request, checks the cache and writes the decision log: for dev-decide 3.1 on a 2-core cloud VM, a routing or triage request took about 2 ms in a warm process and about 3 ms through the local HTTP API, and a full pull-request bundle read from git about 11 ms. A new process per call (the command line, the router hook) also pays Python start-up and first-use pattern compilation, about 55–80 ms, which is roughly Jev's best case. Jev is a general model and these are narrow models trained for one decision each, so compare accuracy only within this report.

## How each winner was chosen

20 candidates per decision: featurizers f1 and f2, five L2 strengths, with and without class balancing. A held-out test split is set aside first. On the rest, every candidate is scored by 5-fold cross-validation that keeps near-duplicates in the same fold, and calibrated by temperature scaling on its out-of-fold predictions. Then:
1. Calibration as a constraint: out-of-fold ECE may exceed the noise floor (what a perfectly calibrated model shows at this sample size, by simulation) by at most 0.05.
2. Accuracy as the objective: the highest cross-validated accuracy; candidates within one standard error are tied.
3. Latency as the tie-break: among tied candidates, the fastest featurizer, then the lowest log-loss.
The winner is refit on the whole development set. Every test number comes from the exported model (pruned float32 weights) running through dev-decide's own predict code. The auto-accept threshold is the lowest confidence at which cross-validated accepted answers reach the target, but never below 0.60, where dev-decide would abstain anyway; those inputs go to the LLM tier instead. A model is enabled only if, on test, at least 10 answers clear the threshold, their accuracy is within 5 points of the target, and the 95% lower bound is within 15 points.

## route

Data: 351 usable of 360 examples in the route family (360 near-duplicate groups). Writer and blind labeler agreed on 97.5% of 360. Development set 298, test set 53 (held-out teacher labels).

Winner: f4, C=16, class weight none, temperature 0.45, 18391 weights, 718 KB. Selection: 40 of 40 candidates passed calibration; 13 tied within one standard error (2.0 points).

Cross-validated: accuracy 84.9%, macro-F1 0.846, ECE 0.076 (noise floor 0.045), log-loss 0.427, AURC 0.033.

Test: accuracy 90.6% (95% CI 79.7%–95.9%) against a majority baseline of 11.4%, macro-F1 0.901, ECE 0.064 (uncalibrated 0.287, noise floor 0.086), Brier 0.161, AURC 0.015.

Auto-accept: confidence ≥ 0.60. In cross-validation that answered 87.2% of inputs at 92.7% accuracy; on test it answered 90.6% (48 inputs) at 93.8% accuracy (95% CI 83.2%–97.9%). Everything below the threshold falls through to the LLM tier.

Status: enabled. Accepted answers reach the target on held-out data.

| Featurizer | Best CV accuracy | Its CV ECE | Featurize p50 | Features |
|---|---|---|---|---|
| f1 | 80.5% | 0.064 | 132 µs | 9732 |
| f2 | 83.9% | 0.080 | 237 µs | 18135 |
| f3 | 85.9% | 0.049 | 446 µs | 18391 |
| f4 | 84.9% | 0.076 | 421 µs | 18391 |

Most common test confusions: backend-reviewer → frontend-dev (1); backend-reviewer → debugger (1); security-auditor → test-writer (1); devops-deploy → debugger (1); docs-writer → devops-deploy (1).

<details><summary>All 40 candidates (cross-validated)</summary>

| Featurizer | C | Class weight | Accuracy | ECE | Noise floor | Log-loss |
|---|---|---|---|---|---|---|
| f3 | 4 | none | 86.6% | 0.032 | 0.043 | 0.418 |
| f3 | 1 | balanced | 86.6% | 0.037 | 0.044 | 0.434 |
| f3 | 4 | balanced | 86.2% | 0.039 | 0.043 | 0.418 |
| f3 | 16 | balanced | 86.2% | 0.035 | 0.043 | 0.418 |
| f3 | 64 | balanced | 86.2% | 0.035 | 0.044 | 0.421 |
| f3 | 16 | none | 85.9% | 0.049 | 0.044 | 0.417 |
| f3 | 64 | none | 85.9% | 0.048 | 0.043 | 0.419 |
| f4 | 64 | none | 85.9% | 0.059 | 0.046 | 0.429 |
| f4 | 64 | balanced | 85.9% | 0.055 | 0.045 | 0.429 |
| f4 | 16 | balanced | 85.6% | 0.057 | 0.044 | 0.427 |
| f3 | 0.25 | balanced | 85.6% | 0.053 | 0.048 | 0.475 |
| f4 | 16 | none | 84.9% | 0.076 | 0.045 | 0.427 |
| f4 | 4 | balanced | 84.9% | 0.042 | 0.045 | 0.431 |
| f4 | 4 | none | 84.6% | 0.040 | 0.044 | 0.431 |
| f2 | 16 | none | 84.6% | 0.061 | 0.051 | 0.583 |
| f3 | 1 | none | 84.2% | 0.049 | 0.047 | 0.464 |
| f2 | 4 | balanced | 84.2% | 0.068 | 0.052 | 0.590 |
| f4 | 1 | balanced | 83.9% | 0.042 | 0.046 | 0.454 |
| f2 | 64 | balanced | 83.9% | 0.080 | 0.050 | 0.581 |
| f2 | 16 | balanced | 83.9% | 0.070 | 0.051 | 0.582 |
| f2 | 64 | none | 83.9% | 0.069 | 0.050 | 0.583 |
| f4 | 1 | none | 83.6% | 0.055 | 0.048 | 0.464 |
| f2 | 4 | none | 83.2% | 0.058 | 0.050 | 0.597 |
| f4 | 0.25 | balanced | 82.6% | 0.047 | 0.049 | 0.522 |
| f2 | 1 | balanced | 81.9% | 0.078 | 0.053 | 0.622 |
| f2 | 0.25 | balanced | 80.9% | 0.112 | 0.062 | 0.698 |
| f1 | 64 | balanced | 80.5% | 0.064 | 0.053 | 0.656 |
| f1 | 64 | none | 80.2% | 0.068 | 0.054 | 0.657 |
| f1 | 16 | balanced | 79.9% | 0.056 | 0.055 | 0.660 |
| f1 | 16 | none | 79.2% | 0.053 | 0.053 | 0.662 |
| f1 | 4 | balanced | 79.2% | 0.047 | 0.055 | 0.674 |
| f4 | 0.25 | none | 78.5% | 0.053 | 0.051 | 0.651 |
| f2 | 1 | none | 78.2% | 0.041 | 0.055 | 0.698 |
| f1 | 4 | none | 77.2% | 0.041 | 0.055 | 0.685 |
| f1 | 1 | balanced | 76.8% | 0.047 | 0.057 | 0.721 |
| f1 | 0.25 | balanced | 76.2% | 0.082 | 0.063 | 0.795 |
| f3 | 0.25 | none | 73.8% | 0.070 | 0.059 | 0.860 |
| f1 | 1 | none | 71.5% | 0.040 | 0.060 | 0.808 |
| f2 | 0.25 | none | 63.4% | 0.075 | 0.066 | 1.241 |
| f1 | 0.25 | none | 58.1% | 0.061 | 0.069 | 1.326 |

</details>

## risk

Data: 146 usable of 150 examples in the pr family (150 near-duplicate groups). Writer and blind labeler agreed on 97.3% of 150. Development set 124, test set 22 (held-out teacher labels).

Winner: f2, C=64, class weight none, temperature 1.37, 11598 weights, 272 KB. Selection: 35 of 40 candidates passed calibration; 14 tied within one standard error (4.5 points).

Cross-validated: accuracy 46.8%, macro-F1 0.349, ECE 0.091 (noise floor 0.103), log-loss 1.311, AURC 0.325.

Test: accuracy 68.2% (95% CI 47.3%–83.6%) against a majority baseline of 29.8%, macro-F1 0.628, ECE 0.196 (uncalibrated 0.267, noise floor 0.219), Brier 0.539, AURC 0.225, within one level 81.8% (MAE 0.59).

Auto-accept: confidence ≥ 0.73. In cross-validation that answered 10.5% of inputs at 92.3% accuracy; on test it answered 9.1% (2 inputs) at 100.0% accuracy (95% CI 34.2%–100.0%). Everything below the threshold falls through to the LLM tier.

Status: not enabled. Only 2 confident test answers, too few to verify the threshold (need 10).

| Featurizer | Best CV accuracy | Its CV ECE | Featurize p50 | Features |
|---|---|---|---|---|
| f1 | 44.4% | 0.090 | 115 µs | 6102 |
| f2 | 44.4% | 0.101 | 299 µs | 11598 |
| f3 | 47.6% | 0.090 | 834 µs | 11854 |
| f4 | 47.6% | 0.081 | 475 µs | 11854 |

Most common test confusions: 4 → 1 (2); 2 → 1 (2); 1 → 3 (1); 3 → 1 (1); 4 → 5 (1).

<details><summary>All 40 candidates (cross-validated)</summary>

| Featurizer | C | Class weight | Accuracy | ECE | Noise floor | Log-loss |
|---|---|---|---|---|---|---|
| f3 | 16 | balanced | 50.0% | 0.097 | 0.102 | 1.276 |
| f4 | 64 | balanced | 49.2% | 0.068 | 0.102 | 1.263 |
| f3 | 64 | balanced | 49.2% | 0.102 | 0.101 | 1.279 |
| f4 | 16 | balanced | 48.4% | 0.072 | 0.103 | 1.259 |
| f3 | 4 | balanced | 48.4% | 0.052 | 0.104 | 1.273 |
| f4 | 16 | none | 47.6% | 0.081 | 0.105 | 1.246 |
| f4 | 64 | none | 47.6% | 0.087 | 0.101 | 1.251 |
| f4 | 4 | none | 47.6% | 0.101 | 0.102 | 1.251 |
| f4 | 4 | balanced | 47.6% | 0.078 | 0.104 | 1.256 |
| f3 | 16 | none | 47.6% | 0.090 | 0.105 | 1.270 |
| f3 | 64 | none | 47.6% | 0.097 | 0.104 | 1.271 |
| f4 | 0.25 | balanced | 47.6% | 0.080 | 0.102 | 1.276 |
| f4 | 1 | balanced | 46.8% | 0.084 | 0.103 | 1.262 |
| f2 | 64 | none | 46.8% | 0.091 | 0.103 | 1.311 |
| f3 | 4 | none | 45.2% | 0.104 | 0.105 | 1.289 |
| f2 | 64 | balanced | 45.2% | 0.084 | 0.101 | 1.318 |
| f4 | 1 | none | 45.2% | 0.123 | 0.100 | 1.324 |
| f1 | 64 | balanced | 45.2% | 0.099 | 0.098 | 1.332 |
| f3 | 1 | balanced | 44.4% | 0.064 | 0.104 | 1.284 |
| f2 | 16 | none | 44.4% | 0.101 | 0.105 | 1.309 |
| f2 | 16 | balanced | 44.4% | 0.091 | 0.100 | 1.315 |
| f1 | 16 | none | 44.4% | 0.090 | 0.101 | 1.325 |
| f1 | 16 | balanced | 44.4% | 0.095 | 0.098 | 1.331 |
| f3 | 1 | none | 44.4% | 0.106 | 0.092 | 1.394 |
| f3 | 0.25 | balanced | 43.5% | 0.095 | 0.101 | 1.301 |
| f2 | 4 | balanced | 43.5% | 0.094 | 0.100 | 1.314 |
| f1 | 64 | none | 43.5% | 0.102 | 0.101 | 1.326 |
| f2 | 1 | balanced | 43.5% | 0.149 | 0.100 | 1.329 |
| f1 | 4 | balanced | 43.5% | 0.132 | 0.099 | 1.333 |
| f2 | 4 | none | 41.9% | 0.136 | 0.096 | 1.333 |
| f1 | 4 | none | 41.9% | 0.119 | 0.099 | 1.347 |
| f2 | 1 | none | 41.9% | 0.124 | 0.086 | 1.436 |
| f1 | 1 | none | 41.9% | 0.101 | 0.083 | 1.442 |
| f2 | 0.25 | balanced | 41.1% | 0.170 | 0.099 | 1.346 |
| f1 | 1 | balanced | 41.1% | 0.112 | 0.102 | 1.351 |
| f1 | 0.25 | balanced | 40.3% | 0.133 | 0.099 | 1.370 |
| f4 | 0.25 | none | 37.9% | 0.145 | 0.072 | 1.472 |
| f3 | 0.25 | none | 33.1% | 0.153 | 0.058 | 1.514 |
| f2 | 0.25 | none | 31.5% | 0.168 | 0.054 | 1.530 |
| f1 | 0.25 | none | 31.5% | 0.177 | 0.054 | 1.530 |

</details>

## breaking_change

Data: 128 usable of 150 examples in the pr family (150 near-duplicate groups). Writer and blind labeler agreed on 85.3% of 150. Development set 109, test set 19 (held-out teacher labels).

Winner: f1, C=1, class weight balanced, temperature 0.28, 5540 weights, 65 KB. Selection: 37 of 40 candidates passed calibration; 8 tied within one standard error (4.1 points).

Cross-validated: accuracy 73.4%, macro-F1 0.693, ECE 0.088 (noise floor 0.087), log-loss 0.525, AURC 0.133.

Test: accuracy 63.2% (95% CI 41.0%–80.9%) against a majority baseline of 67.9%, macro-F1 0.591, ECE 0.191 (uncalibrated 0.068, noise floor 0.198), Brier 0.469, AURC 0.252, AUROC 0.641.

Auto-accept: confidence ≥ 0.78. In cross-validation that answered 45.9% of inputs at 90.0% accuracy; on test it answered 36.8% (7 inputs) at 71.4% accuracy (95% CI 35.9%–91.8%). Everything below the threshold falls through to the LLM tier.

Status: not enabled. Only 7 confident test answers, too few to verify the threshold (need 10).

| Featurizer | Best CV accuracy | Its CV ECE | Featurize p50 | Features |
|---|---|---|---|---|
| f1 | 73.4% | 0.088 | 117 µs | 5540 |
| f2 | 74.3% | 0.103 | 309 µs | 10839 |
| f3 | 73.4% | 0.108 | 641 µs | 11095 |
| f4 | 73.4% | 0.100 | 822 µs | 11095 |

Most common test confusions: false → true (4); true → false (3).

<details><summary>All 40 candidates (cross-validated)</summary>

| Featurizer | C | Class weight | Accuracy | ECE | Noise floor | Log-loss |
|---|---|---|---|---|---|---|
| f2 | 0.25 | balanced | 76.1% | 0.108 | 0.089 | 0.518 |
| f2 | 1 | balanced | 74.3% | 0.103 | 0.089 | 0.518 |
| f1 | 0.25 | balanced | 74.3% | 0.097 | 0.086 | 0.527 |
| f3 | 0.25 | balanced | 73.4% | 0.108 | 0.088 | 0.523 |
| f1 | 1 | balanced | 73.4% | 0.088 | 0.087 | 0.525 |
| f4 | 4 | none | 73.4% | 0.100 | 0.086 | 0.548 |
| f2 | 16 | none | 72.5% | 0.085 | 0.087 | 0.532 |
| f1 | 4 | none | 72.5% | 0.097 | 0.085 | 0.544 |
| f3 | 1 | balanced | 71.6% | 0.114 | 0.088 | 0.525 |
| f1 | 16 | none | 71.6% | 0.095 | 0.084 | 0.534 |
| f3 | 16 | none | 71.6% | 0.094 | 0.084 | 0.537 |
| f1 | 64 | none | 71.6% | 0.094 | 0.087 | 0.538 |
| f3 | 64 | none | 71.6% | 0.108 | 0.088 | 0.543 |
| f2 | 4 | none | 71.6% | 0.101 | 0.084 | 0.543 |
| f3 | 4 | none | 71.6% | 0.085 | 0.084 | 0.543 |
| f2 | 64 | none | 70.6% | 0.097 | 0.086 | 0.536 |
| f4 | 16 | none | 70.6% | 0.061 | 0.089 | 0.549 |
| f2 | 4 | balanced | 69.7% | 0.079 | 0.086 | 0.523 |
| f3 | 4 | balanced | 69.7% | 0.097 | 0.088 | 0.531 |
| f4 | 1 | none | 69.7% | 0.077 | 0.076 | 0.571 |
| f1 | 4 | balanced | 68.8% | 0.094 | 0.085 | 0.528 |
| f4 | 64 | none | 67.9% | 0.100 | 0.090 | 0.556 |
| f3 | 1 | none | 67.9% | 0.113 | 0.072 | 0.578 |
| f1 | 1 | none | 67.9% | 0.102 | 0.069 | 0.580 |
| f2 | 1 | none | 67.9% | 0.140 | 0.066 | 0.581 |
| f4 | 0.25 | none | 67.9% | 0.096 | 0.054 | 0.605 |
| f3 | 0.25 | none | 67.9% | 0.040 | 0.042 | 0.610 |
| f1 | 0.25 | none | 67.9% | 0.029 | 0.043 | 0.612 |
| f2 | 0.25 | none | 67.9% | 0.042 | 0.042 | 0.612 |
| f2 | 16 | balanced | 67.0% | 0.114 | 0.086 | 0.532 |
| f1 | 16 | balanced | 67.0% | 0.112 | 0.085 | 0.535 |
| f4 | 1 | balanced | 67.0% | 0.133 | 0.089 | 0.547 |
| f3 | 64 | balanced | 67.0% | 0.147 | 0.089 | 0.548 |
| f4 | 4 | balanced | 67.0% | 0.117 | 0.091 | 0.552 |
| f4 | 16 | balanced | 67.0% | 0.084 | 0.089 | 0.560 |
| f3 | 16 | balanced | 66.1% | 0.119 | 0.090 | 0.540 |
| f2 | 64 | balanced | 66.1% | 0.139 | 0.086 | 0.541 |
| f1 | 64 | balanced | 66.1% | 0.114 | 0.089 | 0.542 |
| f4 | 0.25 | balanced | 66.1% | 0.115 | 0.089 | 0.545 |
| f4 | 64 | balanced | 66.1% | 0.079 | 0.090 | 0.565 |

</details>

## issue_type

Data: 290 usable of 311 examples in the triage family (311 near-duplicate groups). Writer and blind labeler agreed on 93.2% of 311. Development set 246, test set 44 (held-out teacher labels).

Winner: f1, C=64, class weight none, temperature 0.47, 6813 weights, 186 KB. Selection: 40 of 40 candidates passed calibration; 19 tied within one standard error (1.7 points).

Cross-validated: accuracy 90.7%, macro-F1 0.908, ECE 0.053 (noise floor 0.041), log-loss 0.276, AURC 0.016.

Test: accuracy 95.5% (95% CI 84.9%–98.7%) against a majority baseline of 18.7%, macro-F1 0.951, ECE 0.052 (uncalibrated 0.185, noise floor 0.066), Brier 0.052, AURC 0.002.

Auto-accept: confidence ≥ 0.60. In cross-validation that answered 92.7% of inputs at 95.2% accuracy; on test it answered 97.7% (43 inputs) at 97.7% accuracy (95% CI 87.9%–99.6%). Everything below the threshold falls through to the LLM tier.

Status: enabled. Accepted answers reach the target on held-out data.

| Featurizer | Best CV accuracy | Its CV ECE | Featurize p50 | Features |
|---|---|---|---|---|
| f1 | 90.2% | 0.047 | 60 µs | 6813 |
| f2 | 91.5% | 0.048 | 208 µs | 13988 |
| f3 | 91.9% | 0.021 | 376 µs | 14244 |
| f4 | 90.2% | 0.030 | 418 µs | 14244 |

Most common test confusions: bug → question (1); support → bug (1).

<details><summary>All 40 candidates (cross-validated)</summary>

| Featurizer | C | Class weight | Accuracy | ECE | Noise floor | Log-loss |
|---|---|---|---|---|---|---|
| f3 | 4 | balanced | 92.3% | 0.008 | 0.034 | 0.234 |
| f3 | 16 | none | 92.3% | 0.023 | 0.037 | 0.237 |
| f3 | 1 | balanced | 92.3% | 0.029 | 0.038 | 0.241 |
| f2 | 64 | none | 92.3% | 0.052 | 0.040 | 0.267 |
| f2 | 4 | none | 92.3% | 0.039 | 0.041 | 0.286 |
| f3 | 64 | balanced | 91.9% | 0.021 | 0.035 | 0.231 |
| f3 | 16 | balanced | 91.9% | 0.023 | 0.037 | 0.232 |
| f3 | 64 | none | 91.9% | 0.024 | 0.036 | 0.236 |
| f3 | 0.25 | balanced | 91.9% | 0.062 | 0.041 | 0.261 |
| f2 | 16 | none | 91.9% | 0.050 | 0.041 | 0.271 |
| f2 | 4 | balanced | 91.9% | 0.051 | 0.041 | 0.271 |
| f3 | 4 | none | 91.5% | 0.038 | 0.039 | 0.245 |
| f2 | 64 | balanced | 91.5% | 0.048 | 0.039 | 0.262 |
| f2 | 16 | balanced | 91.5% | 0.045 | 0.040 | 0.265 |
| f2 | 1 | balanced | 91.5% | 0.048 | 0.042 | 0.286 |
| f1 | 16 | none | 91.1% | 0.042 | 0.043 | 0.284 |
| f1 | 64 | none | 90.7% | 0.053 | 0.041 | 0.276 |
| f3 | 1 | none | 90.7% | 0.036 | 0.040 | 0.287 |
| f2 | 0.25 | balanced | 90.7% | 0.053 | 0.047 | 0.314 |
| f1 | 64 | balanced | 90.2% | 0.047 | 0.041 | 0.273 |
| f4 | 16 | balanced | 90.2% | 0.030 | 0.042 | 0.277 |
| f1 | 16 | balanced | 90.2% | 0.033 | 0.041 | 0.278 |
| f1 | 4 | none | 90.2% | 0.038 | 0.043 | 0.305 |
| f1 | 4 | balanced | 89.8% | 0.044 | 0.045 | 0.290 |
| f4 | 64 | balanced | 89.4% | 0.038 | 0.043 | 0.278 |
| f2 | 1 | none | 89.4% | 0.043 | 0.047 | 0.371 |
| f4 | 4 | balanced | 89.0% | 0.050 | 0.042 | 0.279 |
| f4 | 1 | balanced | 89.0% | 0.048 | 0.043 | 0.286 |
| f4 | 64 | none | 88.6% | 0.044 | 0.042 | 0.280 |
| f4 | 16 | none | 88.6% | 0.041 | 0.041 | 0.283 |
| f1 | 1 | balanced | 88.6% | 0.038 | 0.044 | 0.315 |
| f4 | 0.25 | balanced | 88.6% | 0.041 | 0.045 | 0.316 |
| f4 | 4 | none | 88.2% | 0.052 | 0.041 | 0.289 |
| f4 | 1 | none | 87.8% | 0.041 | 0.045 | 0.312 |
| f1 | 0.25 | balanced | 87.8% | 0.045 | 0.048 | 0.355 |
| f1 | 1 | none | 86.6% | 0.044 | 0.049 | 0.401 |
| f4 | 0.25 | none | 85.8% | 0.059 | 0.055 | 0.460 |
| f3 | 0.25 | none | 81.7% | 0.062 | 0.059 | 0.632 |
| f2 | 0.25 | none | 76.8% | 0.074 | 0.067 | 0.830 |
| f1 | 0.25 | none | 76.0% | 0.083 | 0.067 | 0.850 |

</details>

## severity

Data: 276 usable of 311 examples in the triage family (311 near-duplicate groups). Writer and blind labeler agreed on 88.7% of 311. Development set 234, test set 42 (held-out teacher labels).

Winner: f1, C=4, class weight balanced, temperature 0.31, 6676 weights, 156 KB. Selection: 30 of 40 candidates passed calibration; 11 tied within one standard error (2.7 points).

Cross-validated: accuracy 75.6%, macro-F1 0.679, ECE 0.044 (noise floor 0.063), log-loss 0.627, AURC 0.089.

Test: accuracy 78.6% (95% CI 64.1%–88.3%) against a majority baseline of 49.6%, macro-F1 0.693, ECE 0.133 (uncalibrated 0.286, noise floor 0.114), Brier 0.374, AURC 0.151, within one level 95.2% (MAE 0.26).

Auto-accept: confidence ≥ 0.70. In cross-validation that answered 62.0% of inputs at 90.3% accuracy; on test it answered 69.0% (29 inputs) at 82.8% accuracy (95% CI 65.5%–92.4%). Everything below the threshold falls through to the LLM tier.

Status: not enabled. Accepted test accuracy 83% is below the 90% target.

| Featurizer | Best CV accuracy | Its CV ECE | Featurize p50 | Features |
|---|---|---|---|---|
| f1 | 74.8% | 0.035 | 105 µs | 6676 |
| f2 | 77.8% | 0.066 | 183 µs | 13836 |
| f3 | 76.9% | 0.050 | 410 µs | 14092 |
| f4 | 75.6% | 0.075 | 446 µs | 14092 |

Most common test confusions: sev3_minor → sev4_cosmetic (3); sev3_minor → not_a_problem (2); sev1_outage → sev2_major (1); sev2_major → sev3_minor (1); sev4_cosmetic → not_a_problem (1).

<details><summary>All 40 candidates (cross-validated)</summary>

| Featurizer | C | Class weight | Accuracy | ECE | Noise floor | Log-loss |
|---|---|---|---|---|---|---|
| f2 | 4 | balanced | 77.8% | 0.066 | 0.063 | 0.612 |
| f3 | 1 | balanced | 77.4% | 0.075 | 0.062 | 0.625 |
| f3 | 4 | balanced | 76.9% | 0.050 | 0.060 | 0.601 |
| f2 | 16 | balanced | 76.9% | 0.082 | 0.063 | 0.616 |
| f2 | 1 | balanced | 76.5% | 0.050 | 0.064 | 0.647 |
| f2 | 64 | balanced | 76.1% | 0.065 | 0.063 | 0.624 |
| f3 | 16 | balanced | 75.6% | 0.063 | 0.061 | 0.610 |
| f3 | 64 | balanced | 75.6% | 0.061 | 0.063 | 0.620 |
| f1 | 4 | balanced | 75.6% | 0.044 | 0.063 | 0.627 |
| f4 | 1 | balanced | 75.6% | 0.075 | 0.065 | 0.679 |
| f1 | 1 | balanced | 75.2% | 0.059 | 0.066 | 0.669 |
| f1 | 16 | balanced | 74.8% | 0.035 | 0.062 | 0.626 |
| f1 | 64 | balanced | 73.9% | 0.044 | 0.064 | 0.634 |
| f4 | 0.25 | balanced | 73.9% | 0.090 | 0.068 | 0.757 |
| f3 | 64 | none | 73.1% | 0.049 | 0.063 | 0.665 |
| f3 | 0.25 | balanced | 73.1% | 0.054 | 0.066 | 0.718 |
| f4 | 4 | balanced | 72.6% | 0.094 | 0.064 | 0.666 |
| f4 | 16 | balanced | 72.6% | 0.079 | 0.065 | 0.676 |
| f2 | 0.25 | balanced | 72.6% | 0.046 | 0.066 | 0.742 |
| f4 | 64 | balanced | 72.2% | 0.058 | 0.066 | 0.685 |
| f2 | 64 | none | 70.5% | 0.048 | 0.064 | 0.685 |
| f3 | 16 | none | 70.5% | 0.073 | 0.064 | 0.690 |
| f2 | 16 | none | 69.7% | 0.072 | 0.067 | 0.722 |
| f1 | 0.25 | balanced | 69.7% | 0.046 | 0.069 | 0.766 |
| f4 | 64 | none | 69.2% | 0.093 | 0.068 | 0.704 |
| f1 | 64 | none | 68.4% | 0.107 | 0.064 | 0.691 |
| f4 | 16 | none | 67.5% | 0.078 | 0.067 | 0.711 |
| f1 | 16 | none | 67.1% | 0.111 | 0.066 | 0.725 |
| f4 | 4 | none | 65.8% | 0.073 | 0.066 | 0.756 |
| f3 | 4 | none | 63.2% | 0.115 | 0.067 | 0.784 |
| f2 | 4 | none | 61.1% | 0.144 | 0.070 | 0.836 |
| f1 | 4 | none | 60.7% | 0.153 | 0.068 | 0.836 |
| f4 | 1 | none | 59.8% | 0.141 | 0.072 | 0.918 |
| f3 | 1 | none | 50.9% | 0.274 | 0.070 | 1.004 |
| f2 | 1 | none | 50.4% | 0.284 | 0.071 | 1.055 |
| f1 | 1 | none | 50.0% | 0.288 | 0.070 | 1.051 |
| f4 | 0.25 | none | 49.6% | 0.300 | 0.063 | 1.141 |
| f3 | 0.25 | none | 49.6% | 0.301 | 0.054 | 1.198 |
| f1 | 0.25 | none | 49.6% | 0.283 | 0.052 | 1.217 |
| f2 | 0.25 | none | 49.6% | 0.296 | 0.049 | 1.220 |

</details>

## domain

Data: 261 usable of 311 examples in the triage family (311 near-duplicate groups). Writer and blind labeler agreed on 83.9% of 311. Development set 223, test set 38 (held-out teacher labels).

Winner: f3, C=16, class weight balanced, temperature 0.28, 14328 weights, 1847 KB. Selection: 30 of 40 candidates passed calibration; 8 tied within one standard error (3.1 points).

Cross-validated: accuracy 67.7%, macro-F1 0.663, ECE 0.067 (noise floor 0.076), log-loss 1.368, AURC 0.131.

Test: accuracy 68.4% (95% CI 52.5%–80.9%) against a majority baseline of 3.6%, macro-F1 0.561, ECE 0.150 (uncalibrated 0.516, noise floor 0.162), Brier 0.442, AURC 0.116.

Auto-accept: confidence ≥ 0.69. In cross-validation that answered 47.5% of inputs at 90.6% accuracy; on test it answered 47.4% (18 inputs) at 88.9% accuracy (95% CI 67.2%–96.9%). Everything below the threshold falls through to the LLM tier.

Status: not enabled. Accepted test accuracy 89% is not yet proven: its 95% lower bound is 67%.

| Featurizer | Best CV accuracy | Its CV ECE | Featurize p50 | Features |
|---|---|---|---|---|
| f1 | 53.4% | 0.107 | 110 µs | 6880 |
| f2 | 56.5% | 0.068 | 190 µs | 14072 |
| f3 | 67.3% | 0.063 | 377 µs | 14328 |
| f4 | 68.6% | 0.057 | 386 µs | 14328 |

Most common test confusions: tooling_cli → quantum (1); desktop → ar_vr (1); cloud → networking (1); hpc_scientific → devops_infra (1); os_systems → docs (1).

<details><summary>All 40 candidates (cross-validated)</summary>

| Featurizer | C | Class weight | Accuracy | ECE | Noise floor | Log-loss |
|---|---|---|---|---|---|---|
| f4 | 4 | balanced | 70.4% | 0.069 | 0.075 | 1.161 |
| f4 | 16 | none | 69.5% | 0.057 | 0.073 | 1.186 |
| f4 | 16 | balanced | 69.1% | 0.059 | 0.072 | 1.153 |
| f4 | 1 | balanced | 69.1% | 0.053 | 0.073 | 1.176 |
| f4 | 64 | balanced | 68.6% | 0.057 | 0.073 | 1.150 |
| f4 | 0.25 | balanced | 68.6% | 0.198 | 0.081 | 1.349 |
| f4 | 64 | none | 68.2% | 0.067 | 0.073 | 1.161 |
| f4 | 4 | none | 68.2% | 0.069 | 0.076 | 1.270 |
| f3 | 16 | balanced | 67.7% | 0.067 | 0.076 | 1.368 |
| f3 | 64 | balanced | 67.3% | 0.063 | 0.076 | 1.356 |
| f3 | 4 | balanced | 67.3% | 0.101 | 0.076 | 1.371 |
| f3 | 64 | none | 66.4% | 0.095 | 0.076 | 1.390 |
| f3 | 16 | none | 65.9% | 0.108 | 0.075 | 1.416 |
| f3 | 0.25 | balanced | 65.9% | 0.446 | 0.064 | 2.061 |
| f3 | 1 | balanced | 65.5% | 0.081 | 0.074 | 1.387 |
| f3 | 4 | none | 61.9% | 0.078 | 0.078 | 1.588 |
| f2 | 64 | balanced | 56.5% | 0.068 | 0.079 | 1.819 |
| f2 | 16 | balanced | 56.5% | 0.090 | 0.080 | 1.827 |
| f2 | 4 | balanced | 56.1% | 0.080 | 0.080 | 1.829 |
| f2 | 64 | none | 55.6% | 0.091 | 0.078 | 1.832 |
| f4 | 1 | none | 54.7% | 0.094 | 0.080 | 1.788 |
| f2 | 1 | balanced | 54.7% | 0.087 | 0.079 | 1.868 |
| f2 | 16 | none | 53.8% | 0.094 | 0.078 | 1.873 |
| f1 | 64 | none | 53.8% | 0.105 | 0.079 | 2.053 |
| f2 | 0.25 | balanced | 53.8% | 0.408 | 0.047 | 2.541 |
| f1 | 64 | balanced | 53.4% | 0.107 | 0.078 | 2.043 |
| f1 | 16 | balanced | 53.4% | 0.098 | 0.081 | 2.049 |
| f1 | 4 | balanced | 52.9% | 0.111 | 0.080 | 2.072 |
| f1 | 16 | none | 52.5% | 0.102 | 0.079 | 2.101 |
| f2 | 4 | none | 50.2% | 0.082 | 0.081 | 2.096 |
| f1 | 1 | balanced | 50.2% | 0.098 | 0.080 | 2.141 |
| f1 | 0.25 | balanced | 48.0% | 0.360 | 0.045 | 2.674 |
| f1 | 4 | none | 45.7% | 0.098 | 0.077 | 2.335 |
| f3 | 1 | none | 44.4% | 0.144 | 0.074 | 2.496 |
| f2 | 1 | none | 30.5% | 0.146 | 0.049 | 2.991 |
| f4 | 0.25 | none | 30.5% | 0.170 | 0.037 | 3.066 |
| f1 | 1 | none | 25.1% | 0.123 | 0.036 | 3.113 |
| f3 | 0.25 | none | 15.7% | 0.101 | 0.015 | 3.375 |
| f2 | 0.25 | none | 11.2% | 0.068 | 0.011 | 3.429 |
| f1 | 0.25 | none | 9.4% | 0.052 | 0.011 | 3.438 |

</details>

## Caveats

- route: only 53 test examples, so its test accuracy is uncertain by about ±8 points (the cross-validated figure rests on 298 examples).
- risk: only 22 test examples, so its test accuracy is uncertain by about ±18 points (the cross-validated figure rests on 124 examples).
- risk stays on the LLM tier for now: only 2 confident test answers, too few to verify the threshold (need 10).
- breaking_change: only 19 test examples, so its test accuracy is uncertain by about ±20 points (the cross-validated figure rests on 109 examples).
- breaking_change stays on the LLM tier for now: only 7 confident test answers, too few to verify the threshold (need 10).
- severity: only 42 test examples, so its test accuracy is uncertain by about ±12 points (the cross-validated figure rests on 234 examples).
- severity stays on the LLM tier for now: accepted test accuracy 83% is below the 90% target.
- domain: only 38 test examples, so its test accuracy is uncertain by about ±14 points (the cross-validated figure rests on 223 examples).
- domain stays on the LLM tier for now: accepted test accuracy 89% is not yet proven: its 95% lower bound is 67%.
- All tests are against teacher labels, not people. `kit.py review` builds a human gold set.
- Most training data is synthetic (written by the teacher model). Your real requests, issues and diffs will look different; label some with `generate --from-file` or `--from-git` and retrain to see how the numbers transfer.

_Generated 2026-10-23 13:07:56._
