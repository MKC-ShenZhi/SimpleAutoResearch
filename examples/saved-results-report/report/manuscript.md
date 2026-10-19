---
title: "Is Monthly Recalibration Worth Doing? Evidence from Saved Calibration Results"
---

# Summary

Saved calibration results let us separate three questions that are often conflated: does the **model form** matter, does the **update policy** matter, and how much of the answer depends on **which rows survive the eligibility filter**? On the 3,493 eligible scored cases recorded between 2004-11 and 2005-04, model form dominates by a wide margin. A quadratic specification fitted once and then frozen attains an RMSE of 0.146, against 2.526 for a frozen linear specification and 1.701 for a monthly-updated linear one. Refitting monthly coincides with lower error *within* a given form — pooled RMSE falls by roughly a third — but it does not close the gap between forms. The conclusion these files support is therefore conditional: monthly updating is worth doing if the deployed form is already adequate, and its recorded compute cost is negligible within the measured scope; choosing the form is the far larger decision.

Two scope notes matter for reading everything below. First, all numbers are computed from the saved result files and their reference tables; no model was refitted and no new experiment was run for this note. Second, the saved files name a calibration exercise with a linear and a quadratic specification but do not record the application domain, the units or valid range of the target, or any admissibility rule for negative predicted values. We therefore report quantities as recorded and avoid physical interpretation; negative predicted values are counted, not judged.

# What the saved results cover

The evaluation window contains 3,711 scored rows, of which 3,493 pass the eligibility filter (94.1%). The 218 excluded rows are concentrated in three of the six months: December (80 of 744), January (61 of 744) and February (76 of 672), with essentially none in November, March and April. November, March and April therefore contribute near-complete monthly samples, whereas the three winter months contribute 88.7–91.8%. April is a partial month: the test file ends on 4 April, so that cohort holds only 87 rows, 2.5% of the test set, and any monthly statistic for it is correspondingly imprecise.

The fit window expands from 5,498 rows in November to 8,904 rows in April as each month's data is appended. The frozen baseline is a single fit on the training-plus-validation window (5,498 rows); the "updated" series refits at the start of each month on everything available up to that point. The first cohort is therefore identical under both policies by construction: in November the frozen fit and the first refit are the same model, so the exact zero difference there (Table 2) is a bookkeeping identity, not evidence that updating fails to help.

# Model form versus update policy

The primary comparison is pooled across all 3,493 eligible cases and is summarised in Table 1. Read it in two steps. First compare the two update policies *within* a form: RMSE drops from 2.526 to 1.701 for the linear specification and from 0.1465 to 0.0926 for the quadratic, i.e. by 32.7% and 36.8%. Then compare forms at a fixed policy: the frozen quadratic has 17.2 times lower RMSE than the frozen linear, and even the monthly-updated linear (1.701) is 11.6 times worse than the frozen quadratic (0.1465). The form gap is an order of magnitude larger than the policy gap, which is the central result of this comparison.

Table 1. Pooled accuracy on the 3,493 eligible scored cases (identical cases for all four series).

| Form | Policy | n | RMSE | MAE | \|bias\| | P95 \|err\| | R^2 | Pred. < 0 |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| linear | frozen | 3493 | 2.5261 | 2.1287 | 1.8965 | 4.4509 | 0.8923 | 644 (18.4%) |
| linear | updated | 3493 | 1.7010 | 1.1461 | 0.7675 | 3.1826 | 0.9511 | 391 (11.2%) |
| quadratic | frozen | 3493 | 0.1465 | 0.1089 | 0.0664 | 0.3044 | 0.9996 | 9 (0.3%) |
| quadratic | updated | 3493 | 0.0926 | 0.0573 | 0.0247 | 0.1777 | 0.9999 | 6 (0.2%) |

Within a form, the updated series also has lower MAE (46.2% for linear, 47.4% for quadratic) and lower 95th-percentile absolute error (28.5% and 41.6%). It roughly halves the count of negative predicted values for the linear specification (644 to 391 cases, 18.4% to 11.2% of the scored set); the quadratic's residual count is 6–9 cases. Because the saved files record no admissibility rule, no post-processing step and no valid range for the target, these counts are reported as a descriptive property of the outputs, and any downstream treatment of them is outside what the files can establish.

The monthly breakdown (Table 2, Figure 1) shows where the pooled difference comes from and where it does not. Two features are worth reporting to a collaborator. First, the difference is absent in the first month by construction, appears only weakly in December (linear −4.7%, quadratic +3.2%), and then becomes large from January onward: 38.3–55.1% for the linear specification and 45.8–63.1% for the quadratic. Second, the largest percentage differences land in February and March, which have near-complete coverage (88.7% and 99.9%) and 596–743 scored cases, so they are not artifacts of tiny samples. December is the one genuine counter-example: the monthly refit is followed by a slightly higher RMSE for the better-specified quadratic while the same policy coincides with a lower RMSE for the linear specification.

Table 2. RMSE and MAE by month and form, frozen versus monthly-updated.

| Month | n | Form | RMSE frozen | RMSE updated | Δ RMSE | MAE frozen | MAE updated |
| --- | ---: | --- | ---: | ---: | ---: | ---: | ---: |
| 2004-11 | 720 | linear | 1.8974 | 1.8974 | 0.0% | 1.3265 | 1.3265 |
| 2004-11 | 720 | quadratic | 0.0989 | 0.0989 | 0.0% | 0.0612 | 0.0612 |
| 2004-12 | 664 | linear | 2.3825 | 2.2712 | −4.7% | 1.7290 | 1.3802 |
| 2004-12 | 664 | quadratic | 0.1263 | 0.1304 | +3.2% | 0.0832 | 0.0677 |
| 2005-01 | 683 | linear | 2.4427 | 1.5072 | −38.3% | 2.1872 | 1.0556 |
| 2005-01 | 683 | quadratic | 0.1410 | 0.0764 | −45.8% | 0.1106 | 0.0516 |
| 2005-02 | 596 | linear | 2.8479 | 1.2785 | −55.1% | 2.6518 | 0.9211 |
| 2005-02 | 596 | quadratic | 0.1704 | 0.0629 | −63.1% | 0.1391 | 0.0448 |
| 2005-03 | 743 | linear | 2.7791 | 1.3089 | −52.9% | 2.6044 | 0.9736 |
| 2005-03 | 743 | quadratic | 0.1686 | 0.0777 | −53.9% | 0.1389 | 0.0578 |
| 2005-04 | 87 | linear | 3.8365 | 1.8287 | −52.3% | 3.7127 | 1.5915 |
| 2005-04 | 87 | quadratic | 0.2465 | 0.0955 | −61.3% | 0.2239 | 0.0732 |

![Monthly effect of the update policy and its sample coverage. (a) Pooled RMSE by form and policy on a log scale, with an arrow from the frozen to the monthly-updated bar for each form and the pooled percentage change annotated. (b) Monthly RMSE for both forms under both policies; the partial 2005-04 cohort, shaded, holds only 87 scored cases. (c) Per-month percentage change in RMSE from updating, separately for each form; bars below zero mean the updated fit had the lower RMSE, and 2004-11 is exactly zero because both policies use the same first fit. (d) Rows in the monthly fit window (bars, left axis) against the share of available test cases that pass the eligibility filter (line, right axis). (e) Recorded median fit time (bars) and one prediction pass (diamonds) per month on a log scale, with six-month sums, annualised twelve-refit sums and amortised cost annotated. Legends are placed outside the plotting areas.](figures/monthly_update_panel.png){width=84%}

# Sample coverage

Coverage is a property of the eligibility filter, not of the model, and the same 3,493 cases are scored for all four series, so the pooled comparison is not distorted by different evaluation sets. What coverage does affect is how much each monthly number can be trusted, and how the results would transport to a period with a different exclusion rate.

Table 3. Test-case coverage and fit-window size by month.

| Month | Fit rows | Test rows avail. | Eligible | Excluded | Coverage | Share of test |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| 2004-11 | 5,498 | 720 | 720 | 0 | 100.0% | 20.6% |
| 2004-12 | 6,218 | 744 | 664 | 80 | 89.2% | 19.0% |
| 2005-01 | 6,882 | 744 | 683 | 61 | 91.8% | 19.6% |
| 2005-02 | 7,565 | 672 | 596 | 76 | 88.7% | 17.1% |
| 2005-03 | 8,161 | 744 | 743 | 1 | 99.9% | 21.3% |
| 2005-04 | 8,904 | 87 | 87 | 0 | 100.0% | 2.5% |
| test total | — | 3,711 | 3,493 | 218 | 94.1% | 100.0% |

The excluded rows are not analysed here: the saved results record how many were dropped but not how the models would have performed on them. Since the dropped rows are the ones the filter judged unusable, the reported errors are best read as performance on eligible cases. If the exclusion rule removes exactly the hard cases, the operational error would be higher than 0.093 (quadratic, updated); the saved files cannot settle this either way.

# Cost of updating

The recorded timings are medians of repeated in-process measurements, and they are small (Table 4). A linear refit takes 3.0–5.0 ms, a quadratic refit 19.8–39.6 ms; a prediction pass over one month's cohort takes 0.36–0.46 ms and 0.83–1.15 ms respectively. Six refits in total cost 20.9 ms (linear) and 164.0 ms (quadratic). Three different normalisations are easy to confuse, so the table keeps them in separate columns: per-month medians, sums of those six medians, and an annualised scenario of twelve refits. Dividing the six-month measured totals by the 3,493 scored cases gives about 6.0 µs and 47.0 µs per case, whereas annualising the refit cost to twelve refits (41.8 ms and 328.0 ms) and dividing by the same cases gives about 12.0 µs and 93.9 µs. The six-month figures are *not* annualised costs, and the totals are sums of the six monthly medians, not per-row costs.

Table 4. Recorded refit and prediction time: monthly medians, their six-month sums, and a separate annualised scenario.

| Form | Six refits (ms) | Six predict passes (ms) | Annualised refits (ms) | µs/scored case, six-month basis | µs/scored case, annualised basis |
| --- | ---: | ---: | ---: | ---: | ---: |
| linear | 20.88 | 2.37 | 41.8 | 6.0 | 12.0 |
| quadratic | 164.02 | 5.43 | 328.0 | 47.0 | 93.9 |

The per-row figures recorded per month are 0.49–0.64 µs (linear) and 1.23–1.54 µs (quadratic) for the five full months, but 5.23 and 9.78 µs for April. Dividing the recorded prediction time by the batch size reproduces the eligible-case counts exactly, so the April spike is fixed per-call overhead spread over an 87-row batch, not a real increase in marginal cost. Reporting the April figure as "the" per-row cost would overstate prediction cost by roughly an order of magnitude.

These numbers cover only the fit and predict calls. Data ingestion, feature construction, evaluation and plotting are outside the recorded scope, and on a real system they plausibly dominate. The defensible claim is narrow: *within the recorded scope*, a monthly refit is negligible (0.04 s and 0.33 s of refit compute per year under the annualised scenario), so recorded compute is not a reason to prefer a frozen model. It is not evidence about the total cost of running a monthly pipeline in deployment.

# What this does and does not establish

Supported by the saved results: on the 3,493 eligible cases, model form dominates update policy; within each form, the monthly-updated series has roughly a third lower RMSE and about half lower MAE; the difference grows as the fit window lengthens and is absent by construction in the first month; coverage varies from 88.7% to 100% by month; recorded fit and prediction times are on the order of milliseconds; and the pooled metrics are reproducible from the monthly rows (the accompanying script re-derives pooled RMSE, MAE and bias from Table 2 to within 1e-9).

Not established: whether a monthly schedule would still help under a *different* form or after hyperparameter retuning — no retuning was run, so "quadratic" here means the specific saved specification, and nothing in these results shows how a retuned linear model would behave (a 17-fold gap makes full closure unlikely, but that is an extrapolation, not a measurement). Nor is anything established about models with dependence structure across rows, about performance on the 218 excluded rows, about periods outside 2004-11 to 2005-04, or about deployment cost including data engineering. The comparison is descriptive: the two policies differ in fit window and in the data each sees, so the recorded differences are associations in these files, not an isolated causal effect of "updating". No uncertainty intervals were computed: the monthly percentages are point estimates on 87–743 cases, and April in particular should be read as indicative only. Timing values come from one machine and one process, and the last cohort's per-row cost is a small-batch artifact.

# Recommendation

Treat the two decisions separately. First choose the form, using pooled RMSE, MAE, tail error and the count of negative predictions; on this evidence the quadratic specification is the one worth operating. Then, given that form, refit monthly: the error difference is consistent from January onward, refit compute is a fraction of a second per year within the measured scope, and the only counter-example is December, early in the window when the fit history is shortest. Keep the frozen benchmark in reporting so the update's contribution stays visible, and report coverage alongside accuracy so readers can see which cases the comparison actually covers. If the form is later retuned or replaced, the monthly-updating question should be re-answered from new evidence rather than carried over from this note.
