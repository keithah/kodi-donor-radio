# Offline source-clock candidate v2

V2 is the current offline research tool. V1 is historical frozen evidence and remains unmodified. V2 changes delay input validation and strict JSON output only; it does not retune the v1 algorithm or its parameters. Nothing here enables a production clock or establishes physical-speaker timing.

Null, missing, nonnumeric and nonfinite delay observations are retained as uncertain rows with null raw position. The previous estimated position is held, the anchor is cleared, and the existing nine-observation acquisition rule applies to subsequent finite values. The analyzer records `invalid_clock_delay`, leaves the candidate and channel error null, and cannot qualify that point. Serialized nonfinite numeric evidence becomes null; JSON output forbids NaN/Infinity tokens.

## Frozen identity and validation

The estimator and finite-input algorithm were frozen at 2026-10-28T07:33:18.969494Z, before the sixth capture. `freeze.json` retains those original hashes. The capture analyzer later gained capture-integrity checks, endpoint-anchor selection, and held-out-row safeguards; its current hash and validation are recorded separately as `post_freeze_analyzer_sha256` and `post_freeze_analyzer_validation`. Estimator SHA256: `7002aa99fb5a24159b25e19783a177b3c97a73d3777d6350eb2957d5bfa46f87`.

Twelve targeted tests passed with the bundled Python runtime (NumPy is required for analyzer import). Tests first reproduced the null/missing crashes and nonstandard nonfinite output, then verified uncertainty, hold/reacquisition, retained CLI rows, strict JSON and the unchanged finite analyzer formula.

The post-freeze analyzer hardening was rechecked with the same 12 targeted tests
and exact finite-input equivalence across the five archived ledgers (47,605 rows).
This validates analyzer behavior and unchanged estimator output; it adds no new
captured-audio acceptance claim.

`finite-input-equivalence.json` proves exact equality of every v1/v2 output dictionary over 47,605 native clock rows across five archived ledgers: first capture, independent reader, independent pauses, host silence and its debug run. This comparison includes every timeline field, not only qualified points, and uses no waveform input. Ledger and resulting timeline hashes are recorded. This is equivalence evidence, not a new capture-validation claim.

## Use

```sh
python3 estimator.py --ledger /absolute/path/to/ledger.jsonl \
  --comparison-report /absolute/path/to/independent-waveform-report.json \
  --output /absolute/path/to/candidate-v2-result.json
```

The comparison report is optional and used only after all estimates have been computed. All original qualified comparison points remain accounted for; invalid estimates are reported separately and are not passing observations. The companion `.timeline.jsonl` retains every native clock sample.

Run tests from this directory using a Python environment containing NumPy:

```sh
python3 -m unittest -q test_estimator.py test_analyzer.py
```

`verify_equivalence.py --v1 ../clock-candidate-v1/estimator.py --ledger <archive> --output <proof.json>` reproduces the exact timeline comparison; repeat `--ledger` for additional archives. It intentionally rejects nonfinite input, for which v1 was unsafe.

Startup, pause tails, underruns, resume acquisition and drain remain uncertain. No waveform-fit offset was introduced. Native statistics age, sink latency and the capture process-tap timing domain remain limitations; see algorithm.md.
