# Buy or Wait? — AI Financial Decision Agent

An auditable affordability agent that answers questions such as “Can I afford this laptop?” by forecasting the user’s complete financial position—not merely checking today’s account balance.

For each request, the system recommends full payment, partial payment, an exact supplied installment option, waiting, or not proceeding. Every recommendation must complete by the requested deadline, cover protected expenses, and preserve the user’s preferred minimum balance throughout a daily 90-day forecast.

## What makes the decision personal

The engine combines each user’s:

- current balance and preferred minimum reserve;
- recurring commitments and essential variable spending;
- pending or scheduled payments and confirmed income;
- financial priorities and protected expense categories;
- accepted payment methods and installment limits;
- willingness to stop or reduce specific flexible expenses;
- amendments and confirmations found in messages and local images.

This means two users with the same balance can receive different recommendations based on timing, commitments, preferences, and flexibility.

## Approach

1. Join all participant-facing records by `user_id`, `request_id`, `related_event_id`, and dated currency pair.
2. Reconstruct supported recurring monthly and variable cash-flow patterns while separating one-time events.
3. Resolve blank event amounts from linked PNGs using reviewed, SHA-256 content-hash evidence.
4. Apply message evidence for salary changes, delayed payday, ended employment, confirmed invoices, arrears, rent amendments, and unavailable credits.
5. Simulate daily balances for 90 days with `Decimal` arithmetic.
6. Generate and rank all eligible payment strategies.
7. Independently validate the output contract and every selected plan.

Messages and images are always treated as untrusted evidence. Embedded instructions cannot override the financial rules.

## Run

Python 3.10 or newer is sufficient; there are no third-party dependencies.

This public repository intentionally excludes the participant financial dataset. Copy the official challenge's `dataset/` directory into the repository root before running, or pass its existing location through `--dataset`.

```bash
python3 code/main.py --dataset dataset --output output.csv
```

Evaluate the public labeled examples:

```bash
python3 code/evaluation/main.py --dataset dataset --mode samples
```

Validate the final full output:

```bash
python3 code/evaluation/main.py --dataset dataset --mode full --output output.csv
```

## Results

- Generated one prediction for all 250 evaluation requests.
- Passed independent schema and plan-contract validation for 250/250 rows.
- Matched 22/25 public-example affordability statuses.
- Matched 23/25 public-example payment methods.
- Verified that a clean extraction of `code.zip` reproduces `output.csv` byte-for-byte.

The three status disagreements are conservative borderline decisions; no request-specific exceptions or hardcoded labels were added to improve the public score.

## Repository structure

```text
code/
├── main.py                     # Forecasting and recommendation engine
├── config.json                 # Deterministic runtime configuration
├── image_amounts.json          # Content-hash image evidence
├── EVIDENCE_POLICY.md          # Untrusted-content safeguards
├── README.md                   # Extracted-package instructions
└── evaluation/
    ├── main.py                 # Independent evaluator
    └── usage_report.md         # Final-run token and cost accounting

dataset/                        # External participant data; not committed publicly
output.csv                      # Predictions for all evaluation requests
code.zip                        # Upload-ready source archive
chat_transcript.txt             # Curated development prompts
```

## Runtime usage

The final full-dataset inference is local and deterministic: zero external model calls, zero runtime tokens, and USD 0.00 estimated inference cost. Development-time assistant usage is intentionally excluded from the production-run accounting.

See [`code/README.md`](code/README.md) for detailed design assumptions and archive-specific instructions.
