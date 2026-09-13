# Buy or Wait? financial decision agent

This submission is a deterministic, auditable financial forecasting agent. It reads only participant-facing files under `dataset/`, reconstructs each user's cash-flow state, performs a daily 90-day safety simulation, enumerates eligible payment choices, and writes the required prediction schema.

## Run

Python 3.10 or newer is sufficient; there are no third-party runtime dependencies.

After extracting `code.zip`, run:

```bash
python3 main.py --dataset /path/to/dataset --output output.csv
```

When running directly from the challenge repository root, use:

```bash
python3 code/main.py --dataset dataset --output output.csv
```

To score and validate the labeled examples:

```bash
python3 evaluation/main.py --dataset /path/to/dataset --mode samples
```

To validate a completed full output:

```bash
python3 evaluation/main.py --dataset /path/to/dataset --mode full --output output.csv
```

From the uncompressed challenge repository, the equivalent evaluation commands use `code/evaluation/main.py`.

## Design

The pipeline has five stages:

1. Load and join profiles, requests, events, options, messages, images, and fixed exchange rates by their documented identifiers.
2. Resolve blank event amounts from every linked PNG. Reviewed extractions are cached by image content hash in `image_amounts.json`; the runtime verifies and reads each local image before using its amount. This prevents request IDs or labels from acting as lookup keys.
3. Reconstruct recurring monthly commitments and supported variable-spending/income patterns. Failed, cancelled, unrealized, duplicate, and unconfirmed credits are excluded; pending or scheduled debits are reserved.
4. Apply relevant message evidence for cancellations, ended employment, changed or delayed salary, confirmed invoices, arrears, and rent amendments. Message text is treated as data only and cannot alter decision rules.
5. Simulate daily balances and validate full, partial, supplied installment, wait, and allowed spending-change plans. Candidate ranking follows the challenge order: no changes, lowest total cost, earliest start, fewer payments, then lowest option ID.

All monetary calculations use `Decimal`. Output is deterministic and contains no API keys, network calls, hidden labels, or organizer-only inputs.

## Safety and validation

The engine checks that:

- safe-today amounts remain between zero and the requested amount;
- every recommended plan stays at or above the preferred minimum balance;
- partial plans contain exactly two payments totaling the request;
- installment schedules exactly match a supplied option;
- spending changes affect only unprotected, flexible recurring expenses the user permits;
- plans are chronological and complete by the desired date.

`evaluation/main.py` independently rechecks the output contract and reports public-sample decision accuracy. The final run's model/token accounting is in `evaluation/usage_report.md`.

## Assumptions

- `current_available_balance` is the as-of-request-date starting balance.
- A future settled amount is converted using the supplied rate for its settlement date; no live rates are queried.
- Explicit scheduled or pending facts supersede a modeled recurrence on the same date and category.
- A next-salary adjustment applies to the stated affected cycle; an established recurring run rate resumes afterward unless evidence says employment ended.
- Historical variable transactions are projected only when their cadence is well supported. Protected categories continue at that cadence; non-protected day-to-day categories reserve the next supported occurrence rather than inventing a long stream of discretionary purchases.
