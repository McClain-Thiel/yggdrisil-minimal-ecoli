# Experiment execution and storage

- Run genomic searches, vEcoli simulations, sweeps, and their controllers only on remote Linux compute. Never launch these workloads on the Mac, including through notebooks or frozen historical scripts. Local editing, inspection, and lightweight tests are allowed.
- Use private S3 `s3://phd-research-storage-1758274488/yggdrisil/` for durable scientific data and W&B for experiment tracking. Never upload credentials or source-code archives as data.
- Keep exact experiment IDs, S3 URIs, checksums, verification status, and remote execution locations in `experiments.json`. Verify remote contents before removing local scientific data.
- Preserve frozen scientific inputs and failed attempts. Do not resume historical Mac workflows locally. Prepare remote runs as separate, traceable executions.
- Obtain a cost estimate and approval before new paid compute or model runs. Leave pre-existing shared instances running.
