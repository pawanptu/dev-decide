# dev-decide benchmark

Data: `pretrained`. Trained 2026-10-25 07:36:07. Held-out test splits are never used for any choice; accuracy is on them.

| Decision | Features | Test accuracy | ECE (calibrated, was) | Answered locally | Right when answered | Status |
|---|---|---|---|---|---|---|
| `route` | f3 | 87.7% | 0.059 (0.377) | 84.6% | 94.5% | **enabled** |
| `risk` | f2 | 68.2% | 0.196 (0.267) | 9.1% | 100.0% | not yet (only 2 confident test answers, too few to verify the threshold (need 10)) |
| `breaking_change` | f1 | 76.0% | 0.138 (0.133) | 57.3% | 90.7% | **enabled** |
| `issue_type` | f2 | 95.3% | 0.041 (0.106) | 92.6% | 96.4% | **enabled** |
| `severity` | f1 | 73.2% | 0.086 (0.108) | 45.5% | 98.2% | **enabled** |
| `domain` | f4 | 80.2% | 0.058 (0.198) | 70.3% | 92.3% | **enabled** |

**Routing hint without a trained model** (held-out half of 185 labeled requests): accuracy 69.7%, hint precision 88.1% at 45.4% coverage; with one unrelated skill, false hints 3.2%.

**Short issue reports never trained on** (60): accuracy 88.3%; answered locally 83.3%, right when answered 96.0%.

**Speed:** one local triage decision takes 2.4 ms (median of 30, in-process, starter models).
