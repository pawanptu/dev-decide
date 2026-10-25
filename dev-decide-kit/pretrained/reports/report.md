# dev-decide-kit benchmark

Trained 2026-10-25 07:36:07. Teacher: haiku writes, sonnet labels. Bootstrap: 244 model calls, about $3.81 of usage.

Accuracy means agreement with the teacher's labels on examples the model never saw, unless a decision says its test set is human gold labels. *CV* is 5-fold cross-validation on the development set; *test* is a separate held-out split that no choice ever looked at.

## Summary

| Decision | Model | CV accuracy | Test accuracy (95% CI) | Majority baseline | ECE raw → calibrated (noise floor) | Auto-accept at 90% target (test) | Model p50 / p99 | Size | Status |
|---|---|---|---|---|---|---|---|---|---|
| route | f4, C=16 | 84.9% | 90.6% (80%–96%) | 11% | 0.287 → 0.064 (0.086) | 91% answered, 94% right | 973 µs / 1.89 ms | 718 KB | **enabled** |
| risk | f2, C=64 | 46.8% | 68.2% (47%–84%) | 30% | 0.267 → 0.196 (0.219) | 9% answered, 100% right | 573 µs / 1.85 ms | 272 KB | not enabled |
| breaking_change | f1, C=1, balanced | 73.4% | 63.2% (41%–81%) | 68% | 0.068 → 0.191 (0.198) | 37% answered, 71% right | 152 µs / 408 µs | 65 KB | not enabled |
| issue_type | f2, C=64, balanced | 90.6% | 95.3% (91%–98%) | 20% | 0.106 → 0.041 (0.047) | 93% answered, 96% right | 242 µs / 481 µs | 633 KB | **enabled** |
| severity | f1, C=64 | 70.9% | 73.2% (65%–80%) | 49% | 0.108 → 0.086 (0.089) | 46% answered, 98% right | 67 µs / 164 µs | 308 KB | **enabled** |
| domain | f4, C=64, balanced | 78.0% | 80.2% (72%–87%) | 4% | 0.198 → 0.058 (0.070) | 70% answered, 92% right | 1.42 ms / 2.15 ms | 2936 KB | **enabled** |

Ordered labels: risk is within one level 82% of the time on test (MAE 0.59 levels); severity is within one level 92% of the time on test (MAE 0.37 levels).

## Speed against Jev and the LLM tier

| Path | Latency for one decision | Relative to the analyzers + model |
|---|---|---|
| This kit, model only (featurize + predict) | 0.41 ms median (0.07–1.42 across decisions) | - |
| This kit, dev-decide analyzers + model (in-process) | 1.00 ms median (0.30–1.70), worst p99 10.1 ms | 1× |
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

Data: 976 usable of 1232 examples in the triage family (1228 near-duplicate groups). Writer and blind labeler agreed on 88.4% of 936. Development set 828, test set 148 (held-out teacher labels).

Winner: f2, C=64, class weight balanced, temperature 0.66, 23159 weights, 633 KB. Selection: 40 of 40 candidates passed calibration; 8 tied within one standard error (1.0 points).

Cross-validated: accuracy 90.6%, macro-F1 0.905, ECE 0.042 (noise floor 0.023), log-loss 0.288, AURC 0.017.

Test: accuracy 95.3% (95% CI 90.6%–97.7%) against a majority baseline of 19.6%, macro-F1 0.957, ECE 0.041 (uncalibrated 0.106, noise floor 0.047), Brier 0.092, AURC 0.009.

Auto-accept: confidence ≥ 0.60. In cross-validation that answered 91.8% of inputs at 93.2% accuracy; on test it answered 92.6% (137 inputs) at 96.4% accuracy (95% CI 91.7%–98.4%). Everything below the threshold falls through to the LLM tier.

Status: enabled. Accepted answers reach the target on held-out data.

| Featurizer | Best CV accuracy | Its CV ECE | Featurize p50 | Features |
|---|---|---|---|---|
| f1 | 89.1% | 0.023 | 117 µs | 13480 |
| f2 | 90.6% | 0.042 | 427 µs | 23159 |
| f3 | 90.9% | 0.023 | 2.77 ms | 23415 |
| f4 | 89.0% | 0.018 | 2.65 ms | 23415 |

Most common test confusions: feature_request → question (2); question → docs (2); feature_request → bug (1); bug → question (1); bug → docs (1).

<details><summary>All 40 candidates (cross-validated)</summary>

| Featurizer | C | Class weight | Accuracy | ECE | Noise floor | Log-loss |
|---|---|---|---|---|---|---|
| f3 | 64 | balanced | 90.9% | 0.023 | 0.021 | 0.250 |
| f3 | 64 | none | 90.9% | 0.022 | 0.021 | 0.254 |
| f3 | 16 | balanced | 90.6% | 0.019 | 0.021 | 0.256 |
| f2 | 64 | balanced | 90.6% | 0.042 | 0.023 | 0.288 |
| f2 | 16 | balanced | 90.5% | 0.028 | 0.024 | 0.295 |
| f3 | 16 | none | 90.3% | 0.023 | 0.022 | 0.260 |
| f3 | 4 | balanced | 90.2% | 0.027 | 0.022 | 0.273 |
| f2 | 64 | none | 90.2% | 0.027 | 0.022 | 0.291 |
| f3 | 4 | none | 89.7% | 0.027 | 0.022 | 0.282 |
| f2 | 16 | none | 89.7% | 0.018 | 0.023 | 0.300 |
| f4 | 16 | balanced | 89.4% | 0.013 | 0.023 | 0.295 |
| f2 | 4 | balanced | 89.4% | 0.023 | 0.024 | 0.318 |
| f1 | 16 | balanced | 89.3% | 0.031 | 0.024 | 0.316 |
| f1 | 64 | balanced | 89.1% | 0.023 | 0.023 | 0.307 |
| f4 | 64 | balanced | 89.0% | 0.018 | 0.023 | 0.295 |
| f3 | 1 | balanced | 89.0% | 0.021 | 0.024 | 0.323 |
| f4 | 4 | balanced | 88.8% | 0.028 | 0.023 | 0.306 |
| f1 | 64 | none | 88.8% | 0.022 | 0.023 | 0.309 |
| f1 | 4 | balanced | 88.8% | 0.023 | 0.025 | 0.346 |
| f1 | 16 | none | 88.5% | 0.021 | 0.024 | 0.323 |
| f4 | 64 | none | 88.4% | 0.020 | 0.023 | 0.297 |
| f4 | 16 | none | 88.4% | 0.018 | 0.024 | 0.301 |
| f2 | 4 | none | 88.4% | 0.017 | 0.024 | 0.330 |
| f4 | 4 | none | 88.2% | 0.027 | 0.023 | 0.312 |
| f3 | 1 | none | 88.2% | 0.033 | 0.025 | 0.346 |
| f1 | 4 | none | 88.2% | 0.022 | 0.025 | 0.358 |
| f4 | 1 | balanced | 87.8% | 0.029 | 0.024 | 0.343 |
| f4 | 1 | none | 87.6% | 0.031 | 0.025 | 0.353 |
| f2 | 1 | balanced | 86.7% | 0.031 | 0.027 | 0.383 |
| f1 | 1 | balanced | 86.5% | 0.031 | 0.027 | 0.423 |
| f4 | 0.25 | balanced | 85.6% | 0.026 | 0.027 | 0.418 |
| f3 | 0.25 | balanced | 85.5% | 0.021 | 0.028 | 0.419 |
| f2 | 1 | none | 85.4% | 0.028 | 0.027 | 0.420 |
| f4 | 0.25 | none | 84.8% | 0.038 | 0.028 | 0.452 |
| f2 | 0.25 | balanced | 84.7% | 0.063 | 0.029 | 0.505 |
| f1 | 1 | none | 83.9% | 0.029 | 0.029 | 0.463 |
| f3 | 0.25 | none | 83.2% | 0.043 | 0.032 | 0.522 |
| f1 | 0.25 | balanced | 82.5% | 0.056 | 0.032 | 0.557 |
| f2 | 0.25 | none | 78.3% | 0.035 | 0.034 | 0.681 |
| f1 | 0.25 | none | 76.4% | 0.046 | 0.035 | 0.740 |

</details>

## severity

Data: 818 usable of 1232 examples in the triage family (1228 near-duplicate groups). Writer and blind labeler agreed on 87.4% of 936. Development set 695, test set 123 (held-out teacher labels).

Winner: f1, C=64, class weight none, temperature 1.19, 13141 weights, 308 KB. Selection: 33 of 40 candidates passed calibration; 17 tied within one standard error (1.7 points).

Cross-validated: accuracy 70.9%, macro-F1 0.518, ECE 0.051 (noise floor 0.039), log-loss 0.741, AURC 0.107.

Test: accuracy 73.2% (95% CI 64.7%–80.2%) against a majority baseline of 49.2%, macro-F1 0.561, ECE 0.086 (uncalibrated 0.108, noise floor 0.089), Brier 0.342, AURC 0.097, within one level 91.9% (MAE 0.37).

Auto-accept: confidence ≥ 0.72. In cross-validation that answered 53.8% of inputs at 90.1% accuracy; on test it answered 45.5% (56 inputs) at 98.2% accuracy (95% CI 90.6%–99.7%). Everything below the threshold falls through to the LLM tier.

Status: enabled. Accepted answers reach the target on held-out data.

| Featurizer | Best CV accuracy | Its CV ECE | Featurize p50 | Features |
|---|---|---|---|---|
| f1 | 70.9% | 0.051 | 41 µs | 13141 |
| f2 | 72.4% | 0.025 | 136 µs | 22683 |
| f3 | 71.5% | 0.045 | 498 µs | 22939 |
| f4 | 70.1% | 0.054 | 568 µs | 22939 |

Most common test confusions: sev3_minor → sev2_major (6); sev3_minor → not_a_problem (5); sev4_cosmetic → sev3_minor (5); sev1_outage → sev2_major (4); sev3_minor → sev4_cosmetic (3).

<details><summary>All 40 candidates (cross-validated)</summary>

| Featurizer | C | Class weight | Accuracy | ECE | Noise floor | Log-loss |
|---|---|---|---|---|---|---|
| f3 | 64 | none | 72.4% | 0.063 | 0.038 | 0.705 |
| f2 | 4 | balanced | 72.4% | 0.025 | 0.038 | 0.720 |
| f3 | 16 | none | 72.2% | 0.063 | 0.037 | 0.700 |
| f3 | 16 | balanced | 72.1% | 0.039 | 0.038 | 0.701 |
| f2 | 16 | none | 72.1% | 0.054 | 0.038 | 0.726 |
| f3 | 1 | balanced | 71.9% | 0.030 | 0.038 | 0.715 |
| f1 | 4 | balanced | 71.9% | 0.050 | 0.038 | 0.746 |
| f3 | 4 | balanced | 71.5% | 0.045 | 0.038 | 0.693 |
| f3 | 64 | balanced | 71.5% | 0.040 | 0.039 | 0.714 |
| f2 | 16 | balanced | 71.5% | 0.036 | 0.036 | 0.721 |
| f2 | 64 | balanced | 71.4% | 0.034 | 0.038 | 0.729 |
| f1 | 16 | balanced | 71.4% | 0.037 | 0.039 | 0.741 |
| f1 | 64 | balanced | 71.4% | 0.043 | 0.039 | 0.746 |
| f2 | 1 | balanced | 71.4% | 0.037 | 0.039 | 0.758 |
| f2 | 64 | none | 71.2% | 0.043 | 0.038 | 0.724 |
| f1 | 64 | none | 70.9% | 0.051 | 0.039 | 0.741 |
| f1 | 16 | none | 70.9% | 0.058 | 0.039 | 0.744 |
| f4 | 16 | balanced | 70.5% | 0.057 | 0.039 | 0.731 |
| f4 | 64 | none | 70.4% | 0.050 | 0.039 | 0.728 |
| f4 | 64 | balanced | 70.4% | 0.037 | 0.039 | 0.743 |
| f4 | 4 | balanced | 70.2% | 0.036 | 0.039 | 0.721 |
| f4 | 16 | none | 70.1% | 0.054 | 0.038 | 0.716 |
| f1 | 1 | balanced | 69.9% | 0.036 | 0.039 | 0.791 |
| f3 | 0.25 | balanced | 69.8% | 0.045 | 0.039 | 0.795 |
| f3 | 4 | none | 69.5% | 0.058 | 0.038 | 0.724 |
| f4 | 1 | balanced | 69.4% | 0.057 | 0.038 | 0.735 |
| f2 | 4 | none | 69.4% | 0.053 | 0.039 | 0.766 |
| f4 | 0.25 | balanced | 69.4% | 0.051 | 0.039 | 0.804 |
| f4 | 4 | none | 69.1% | 0.064 | 0.038 | 0.718 |
| f1 | 4 | none | 68.1% | 0.058 | 0.039 | 0.787 |
| f2 | 0.25 | balanced | 68.1% | 0.057 | 0.042 | 0.846 |
| f1 | 0.25 | balanced | 65.8% | 0.050 | 0.041 | 0.890 |
| f4 | 1 | none | 65.2% | 0.078 | 0.040 | 0.793 |
| f3 | 1 | none | 61.4% | 0.117 | 0.040 | 0.858 |
| f2 | 1 | none | 59.7% | 0.137 | 0.040 | 0.927 |
| f1 | 1 | none | 58.8% | 0.144 | 0.041 | 0.945 |
| f4 | 0.25 | none | 56.0% | 0.188 | 0.042 | 1.000 |
| f3 | 0.25 | none | 49.5% | 0.291 | 0.041 | 1.096 |
| f2 | 0.25 | none | 49.2% | 0.300 | 0.038 | 1.148 |
| f1 | 0.25 | none | 49.2% | 0.302 | 0.038 | 1.157 |

</details>

## domain

Data: 742 usable of 1232 examples in the triage family (1228 near-duplicate groups). Writer and blind labeler agreed on 79.3% of 936. Development set 631, test set 111 (held-out teacher labels).

Winner: f4, C=64, class weight balanced, temperature 0.52, 22775 weights, 2936 KB. Selection: 34 of 40 candidates passed calibration; 11 tied within one standard error (1.6 points).

Cross-validated: accuracy 78.0%, macro-F1 0.782, ECE 0.042 (noise floor 0.037), log-loss 0.785, AURC 0.059.

Test: accuracy 80.2% (95% CI 71.8%–86.5%) against a majority baseline of 4.1%, macro-F1 0.798, ECE 0.058 (uncalibrated 0.198, noise floor 0.070), Brier 0.291, AURC 0.052.

Auto-accept: confidence ≥ 0.61. In cross-validation that answered 75.1% of inputs at 90.1% accuracy; on test it answered 70.3% (78 inputs) at 92.3% accuracy (95% CI 84.2%–96.4%). Everything below the threshold falls through to the LLM tier.

Status: enabled. Accepted answers reach the target on held-out data.

| Featurizer | Best CV accuracy | Its CV ECE | Featurize p50 | Features |
|---|---|---|---|---|
| f1 | 68.1% | 0.065 | 52 µs | 13021 |
| f2 | 71.6% | 0.043 | 153 µs | 22519 |
| f3 | 79.1% | 0.043 | 1.03 ms | 22775 |
| f4 | 78.0% | 0.042 | 522 µs | 22775 |

Most common test confusions: tooling_cli → blockchain_web3 (1); hpc_scientific → data_science (1); game → robotics (1); tooling_cli → os_systems (1); rag → quantum (1).

<details><summary>All 40 candidates (cross-validated)</summary>

| Featurizer | C | Class weight | Accuracy | ECE | Noise floor | Log-loss |
|---|---|---|---|---|---|---|
| f3 | 64 | balanced | 79.1% | 0.043 | 0.038 | 0.816 |
| f4 | 64 | none | 78.6% | 0.037 | 0.038 | 0.786 |
| f3 | 16 | none | 78.3% | 0.038 | 0.038 | 0.827 |
| f3 | 64 | none | 78.3% | 0.038 | 0.038 | 0.827 |
| f3 | 16 | balanced | 78.1% | 0.048 | 0.039 | 0.832 |
| f4 | 64 | balanced | 78.0% | 0.042 | 0.037 | 0.785 |
| f4 | 16 | balanced | 77.8% | 0.037 | 0.038 | 0.795 |
| f4 | 16 | none | 77.8% | 0.031 | 0.038 | 0.799 |
| f3 | 4 | balanced | 77.8% | 0.061 | 0.038 | 0.861 |
| f4 | 4 | balanced | 77.5% | 0.046 | 0.039 | 0.816 |
| f3 | 4 | none | 77.5% | 0.045 | 0.039 | 0.870 |
| f4 | 4 | none | 77.2% | 0.044 | 0.039 | 0.819 |
| f4 | 1 | balanced | 76.2% | 0.048 | 0.040 | 0.892 |
| f3 | 1 | balanced | 74.0% | 0.048 | 0.040 | 0.978 |
| f4 | 1 | none | 73.9% | 0.035 | 0.041 | 0.949 |
| f4 | 0.25 | balanced | 72.1% | 0.059 | 0.041 | 1.027 |
| f2 | 64 | none | 71.6% | 0.043 | 0.042 | 1.091 |
| f2 | 64 | balanced | 71.6% | 0.046 | 0.042 | 1.103 |
| f2 | 16 | balanced | 71.6% | 0.058 | 0.042 | 1.115 |
| f2 | 16 | none | 71.2% | 0.043 | 0.042 | 1.118 |
| f3 | 0.25 | balanced | 71.0% | 0.173 | 0.048 | 1.219 |
| f2 | 4 | balanced | 70.7% | 0.065 | 0.042 | 1.177 |
| f1 | 16 | balanced | 68.3% | 0.065 | 0.045 | 1.303 |
| f1 | 64 | balanced | 68.1% | 0.065 | 0.045 | 1.272 |
| f1 | 64 | none | 68.0% | 0.060 | 0.044 | 1.272 |
| f2 | 4 | none | 67.0% | 0.022 | 0.044 | 1.208 |
| f1 | 16 | none | 67.0% | 0.048 | 0.045 | 1.312 |
| f3 | 1 | none | 65.9% | 0.060 | 0.044 | 1.217 |
| f2 | 1 | balanced | 64.3% | 0.058 | 0.043 | 1.368 |
| f1 | 4 | balanced | 63.5% | 0.059 | 0.045 | 1.404 |
| f1 | 4 | none | 61.2% | 0.040 | 0.045 | 1.451 |
| f2 | 0.25 | balanced | 60.5% | 0.278 | 0.046 | 1.790 |
| f1 | 1 | balanced | 55.5% | 0.055 | 0.047 | 1.650 |
| f1 | 0.25 | balanced | 50.4% | 0.227 | 0.043 | 2.048 |
| f4 | 0.25 | none | 47.9% | 0.046 | 0.048 | 1.792 |
| f2 | 1 | none | 47.9% | 0.045 | 0.048 | 1.830 |
| f1 | 1 | none | 41.8% | 0.054 | 0.048 | 2.140 |
| f3 | 0.25 | none | 25.0% | 0.102 | 0.037 | 2.715 |
| f2 | 0.25 | none | 13.2% | 0.079 | 0.020 | 3.129 |
| f1 | 0.25 | none | 12.4% | 0.073 | 0.016 | 3.212 |

</details>

## Caveats

- route: only 53 test examples, so its test accuracy is uncertain by about ±8 points (the cross-validated figure rests on 298 examples).
- risk: only 22 test examples, so its test accuracy is uncertain by about ±18 points (the cross-validated figure rests on 124 examples).
- risk stays on the LLM tier for now: only 2 confident test answers, too few to verify the threshold (need 10).
- breaking_change: only 19 test examples, so its test accuracy is uncertain by about ±20 points (the cross-validated figure rests on 109 examples).
- breaking_change stays on the LLM tier for now: only 7 confident test answers, too few to verify the threshold (need 10).
- severity: only 123 test examples, so its test accuracy is uncertain by about ±8 points (the cross-validated figure rests on 695 examples).
- All tests are against teacher labels, not people. `kit.py review` builds a human gold set.
- Most training data is synthetic (written by the teacher model). Your real requests, issues and diffs will look different; label some with `generate --from-file` or `--from-git` and retrain to see how the numbers transfer.

_Generated 2026-10-25 07:38:43._
