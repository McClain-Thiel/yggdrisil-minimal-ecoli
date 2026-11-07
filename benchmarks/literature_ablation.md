# Literature ablation pilot

Draft protocol, 3 November 2026. The machine-readable specification is
[`literature_ablation.json`](literature_ablation.json). This phase prepares the
experiment; it does not launch searches, simulations or cloud resources.

## Question and scope

Does an initial literature dossier improve computational deletion search, and
does ordinary research during the search add value beyond that dossier?
All conclusions concern specified computational models. Expression knockouts do
not reconstruct a shortened chromosome or establish laboratory viability.
There is no hidden published genome to rediscover.

| Arm | Initial evidence | Research during search |
| --- | --- | --- |
| A: no retrieval | Common gene annotations | None |
| B: frozen dossier | A + a frozen Edison report | None |
| C: adaptive literature | The identical B inputs | Europe PMC through the research agent |

All arms retain real gene IDs, the manager/explorer architecture, persistent
plans, identical local tools and identical evaluation feedback. The old
`closed-book` mode is not a valid control: it also changes IDs and orchestration.
Only C receives a live research tool. No arm receives a live Edison tool.

## Frozen inputs and pilot matrix

- Nine independent searches: three arms paired across seeds 17, 29 and 43.
  Counterbalance the planned order as A/B/C, B/C/A and C/A/B across seed blocks.
- Start from the same published MS56 projection: 893 protein-coding knockouts
  on MG1655. The 112 published loci outside our coding registry stay explicitly
  excluded. This is not the native MS56 chromosome.
- Use the unedited cached Edison report from task
  `e71b4b18-c0a0-49b3-b232-18486c1c0526`, generated before these experiments.
  Both B and C receive exactly the same bytes. Previous agent conclusions,
  finalist genomes and simulation results are not copied into their memory.
  This is a pilot-specific dossier, not evidence that every Edison review helps.
  The cached report emphasizes MDS12 and mentions MS56 without its exact
  deletion boundaries. Review its usefulness for MS56 continuation before paid
  execution; any replacement dossier requires a new frozen input specification
  chosen before outcomes, and any new Edison job needs its own authorization.
- Each search gets at most 50 new, distinct candidate states, plus its evaluated
  initial state. Failed FBA/RBA candidates consume this budget. Duplicate and
  invalid proposals remain logged and consume time/model costs. A separate
  75-step and one-hour ceiling prevents unbounded duplicate proposals.
- Use the previous model/settings as a draft starting point, not a model sweep.
  The agent configuration and served model/provider metadata must be saved.
  Empty branches, tool outages and budget stops remain visible outcomes.
- Graphs, ADK databases and writable evidence are isolated by run. Within-run
  memory persists. Cross-run evaluation caching may save physical work only if
  every logical evaluation is charged equally and no search history is leaked.

The standard-library-only planner validates pinned input checksums, exports the starting gene
set and cached report, and produces the nine job specifications:

```sh
.venv/bin/python scripts/plan_literature_ablation.py \
  --output runs/literature-ablation-pilot-20261103
```

It refuses to overwrite a bundle or accept changed source inputs, and checks
that the starting catalog was built against the same reference and gene-registry
checksum. Full biological input validation remains the runner's responsibility. Generated
scientific data stays under ignored `runs/`. The output is explicitly
`launch_ready: false`; it is not an executable deployment package.

## Selection and final evaluation

Freeze one eligible finalist per run before obtaining new vEcoli results. Rank
by additional deletions descending, FBA growth descending, then state ID. The
initial genotype is not a successful new design; if no new eligible candidate
exists, record that search as yielding no finalist. Do not substitute candidates
after seeing vEcoli failures. Deduplicate identical genotype/seed simulations
across runs while retaining their original nomination relationships.

Test finalists plus MS56 and MG1655 controls with three lineage seeds
(101, 211, 307), 12 generations and one followed daughter. This is at most
33 lineages / 396 cell cycles before deduplication and early nondivision, not a
branching population. Deployment smoke tests and infrastructure retries are
additional work. Fix the native version, fitted simData, adapter, medium and
thread settings. Audit knockouts, finite emitted values and numerical warnings;
classify infrastructure/model exceptions separately from biological nondivision.

Report generations reached, generation 9–12 division times, dry-mass fold
changes and birth-mass drift. Establish numerical acceptance thresholds and
the across-seed acceptance rule from parent controls before finalist results
are inspected. Thresholds are deliberately unset in this draft. A run cannot
support the primary endpoint until these are frozen.

The primary endpoint is additional gene deletions passing the fixed final
screen at the same search budget. Also report finalist pass rate, trade-offs,
model/evaluation calls, total cost and elapsed time. Audit function coverage:
transcription in vEcoli is not proof that a gene's function is modeled. Compare
arms with searches as the sampling units; three seeds are a pilot, not a
powered publication study. C versus B tests adaptive research; B versus A tests
this particular initial dossier. FBA/RBA and vEcoli have correlated assumptions.

## AWS first

Prefer the existing `g6-big2` EC2 instance for the first deployment, running the
native Nextflow local workflow with bounded cell concurrency. Inspection on
3 November found 16 vCPUs, 64 GiB provisioned RAM (46 GiB available), and a
near-zero load average. The GPU is unnecessary for this workload. Storage is
the immediate constraint: about 16 GB free on root and 4.7 GiB free on local
NVMe. A dedicated temporary 200 GB gp3 volume would cost about $0.53/day at the
queried $0.08/GB-month rate, excluding snapshots and transfer. Measure output
size during controls before accepting this as sufficient for the full pilot.
Do not delete other projects' data to make room.

The frozen draft JSON retains `m7i.2xlarge` as the new-worker sizing option;
it is not a launch instruction. Freeze the actual execution target after the
control benchmark. Additional CPU workers can parallelize independent lineages,
but generations within one lineage remain serial. Benchmark memory and CPU
scaling before estimating the speedup or requesting more instances. Existing
`space-*` Batch queues were found, but their images, storage and intended workload
have not been validated for this project. A local Nextflow executor avoids new
Batch infrastructure for the pilot. UCL execution is deferred.

The AWS Pricing API returned **$0.4032/hour** for Linux shared-tenancy
`m7i.2xlarge` in `us-east-1` on 3 November 2026 (8 vCPU, 32 GiB). Illustratively,
24–48 VM-hours cost **$9.68–$19.35 for the instance alone**. This is a cost
scenario, not a runtime prediction or authorization. Storage, transfer and model
APIs are additional; model and compute budgets remain unapproved. No new Edison
jobs are needed. The historical report's generation cost was absent from the
cache and must be reported as unknown, not zero.

Before sizing the job, benchmark parent controls on Linux: wall time, peak RSS,
CPU utilization, output size, native warnings and numerical parity. Start with
two concurrent cells and one BLAS thread per process; increase only after
measurement. Set a wall-time cutoff, checkpoint completed cells, and verify
backups. Terminate only workers created for this experiment and remove their
temporary resources; leave the pre-existing shared instance running. Remove any
task-owned scratch volume only after its data is backed up and verified.

Keep agent/API work separate from simulation workers. Simulator workers need
no OpenRouter or Edison credentials. Save W&B config, metrics and scientific
artifacts under meaningful run IDs; use an approved S3 prefix for bulk cell
outputs and resumable Nextflow work. Preserve a genotype/seed-to-output manifest.
The registry `experiments.json` must link configs, graph/session artifacts,
finalist nominations, W&B runs, S3 locations and completion status. Never upload
credentials or indiscriminately archive the working directory.

## Implementation and launch sequence

The headless runner is `scripts/run_literature_ablation.py`. It uses the existing
framework loop, scorers and native ADK sessions. The initial dossier is immutable
shared input, checked on resume and exposed equally to B and C. Research is
optional when the manager has enough evidence. The runner enforces the frozen
state limits and records the selected finalist, graph, sessions, billed usage
and W&B data artifact. It requires a dedicated, non-resetting provider key cap;
the older per-invocation dollar threshold alone is not a hard spending bound.

Review checks passed: 236 application tests and 124 framework tests locally,
plus 86 selected agent and FBA/RBA tests on Linux. Tests cover treatment
isolation, dossier persistence, restart behavior,
candidate limits, budget stops and finalist tie breaking. The deployment
snapshot and dependency versions are saved in
`runs/literature-ablation-deployment-20261103`. No code archive is uploaded to
W&B. The old Git dependency pin is insufficient to reproduce this source tree;
deployment uses both frozen source snapshots explicitly. No commits were made.

1. After cost approval, run three separate smoke searches (two new candidates
   each), then the nine pilot searches. Run arms for each seed concurrently in
   groups of three; retain the planned job order and actual start timestamps.
   Smoke outcomes are excluded from the arm comparison. Any failed job blocks
   later groups until reviewed; preserve all attempts and logs.
2. Benchmark native Linux vEcoli controls, memory, output size and numerical
   behavior before final validation. The agent environment is Python 3.11;
   native vEcoli needs its separate Python 3.12 environment. Search readiness
   does not imply the final biological-model screen is calibrated.
3. Freeze the numerical acceptance protocol using controls before inspecting
   finalist vEcoli results. Choose the S3 prefix for bulk lineage outputs and
   verify backups before removing task-owned scratch storage.
4. After the pilot, decide whether to expand. A full paper also needs strong
   non-LLM baselines (greedy/block and a documented Minesweeper adaptation),
   MG1655 starts, more independent replicates, and evidence-withholding
   continuations. These are follow-up experiments, not silently added pilot arms.

Relevant upstream references:
[vEcoli workflow implementation](https://covertlab.github.io/vEcoli/_modules/runscripts/workflow.html),
[vEcoli workflows](https://covertlab.github.io/vEcoli/workflows.html),
[EC2 M7i](https://aws.amazon.com/ec2/instance-types/m7i/).
The pinned native checkout already includes AWS Batch and Slurm profiles if
later scale makes those worthwhile; no second scheduler should be written.


## Remote validation design (5 November 2026)

All experiment execution is now remote-only, including search and simulation
controllers. The Mac remains an editing and inspection client. This supersedes
any historical local-launch instructions. `experiments.json` is the data ledger;
S3 is the durable bulk-data store and W&B retains tracking and provenance.

The separately labelled draft `vecoli_20gen_remote.json` fixes the same five Kimi
diagnostic genotypes at three seeds (101, 211, 307), twenty generations and one
followed daughter: fifteen independent lineages, at most 300 cell simulations.
It starts new Linux lineages at generation one; interrupted Mac cells are not
spliced into the new run. Twenty generations is a longer diagnostic, not a
validated guarantee of viability. The previous paper protocol is unchanged.

The preferred large-run backend is native Nextflow on AWS Batch with x86 CPU
workers and S3 work/results directories. The pinned vEcoli checkout already has
an `awsbatch` profile and ECR build support. Keep the Nextflow controller on a
remote host and preserve its session cache as well as the S3 work directory;
a results export alone is insufficient to resume the workflow. Each cell is
one task, and daughter-state dependencies serialize generations while allowing
independent genotype/seed lineages to run concurrently.

Comparison:

| Backend | Fit for this experiment | Additional maintenance |
| --- | --- | --- |
| Nextflow on one remote EC2 host | Simplest control benchmark and 15-lineage pilot if RAM permits | Container/environment, scratch and checkpoint configuration |
| Nextflow + AWS Batch | Preferred for larger genotype/seed grids and disposable workers | Queue/IAM/container setup, S3-aware result collection |
| Ray/Anyscale | Useful if simulation becomes part of an adaptive training or RL loop | New task adapters, durable simulation checkpoint/restart handling and orchestration |
| UCL Slurm | Compatible later through native Nextflow | Cluster environment/container and storage setup; deferred |

Ray can retry tasks and restart actors, but actor application state still needs
explicit checkpoint/recovery code. Replacing the existing lineage workflow
would add maintenance without increasing its intrinsic parallelism. For this
fixed grid, reuse Nextflow rather than introducing Ray.

The account has valid `space-spot`, `space-spot-highmem` and `space-fargate` queues,
but they belong to existing infrastructure and are not yet validated for this
project. The Spot compute environments permit up to 1,000 vCPUs; their caps are
not an acceptable experiment budget. Audit roles/networking/storage and use
explicit task concurrency, retry and wall-time limits. Do not alter shared
queues or launch jobs as part of this research step. Start controls on On-Demand
capacity; consider Spot only after generation-level restart is tested. Retry
infrastructure interruptions with unchanged inputs/seeds, not biological
nondivision or deterministic model exceptions.

Before dispatch, adapt the current local-filesystem tracker to cloud outputs.
Its existing `(variant, generation)` task identity cannot combine seeds safely;
use experiment/variant/seed/generation/agent/attempt identity (or keep each seed
in a separate native workflow until that is changed). Preserve attempt logs,
finite-output and exact transcript-suppression audits, successful and failed
cells, and verify completed daughter states before scratch cleanup. Calibrate
resource reservations with actual Linux wall time, peak RSS and output volume;
do not confuse the old five-CPU concurrency reservation with demonstrated
five-core speedup. No extra generations or branches can run in parallel within
one followed lineage.

Current us-east-1 Linux On-Demand prices queried from AWS Pricing GetProducts:
`m7i.4xlarge` (16 vCPU/64 GiB) $0.8064/hour; `c7i.4xlarge` (16/32)
$0.714/hour; `r7i.4xlarge` (16/128) $1.0584/hour; existing `g6.4xlarge`
(16/64 plus unused GPU) $1.3232/hour. One m7i.4xlarge for 24 hours would
cost $19.35 before storage/networking; this is a scenario, not an ETA or an
approved launch budget. If Linux measurements give a cell runtime of T hours,
ideal compute work is at most 300*T cell-hours and ideal wall time is bounded
below by 20*T even with all fifteen lineages concurrent. Include scheduling,
I/O, retries and tail latency when estimating the actual run.

Sources checked on 5 November 2026:
[vEcoli workflows](https://covertlab.github.io/vEcoli/workflows.html),
[Nextflow AWS Batch executor](https://docs.seqera.io/nextflow/reference/executor/aws-batch/),
[AWS Batch Spot practices](https://docs.aws.amazon.com/batch/latest/userguide/bestpractice6.html),
[Ray actor recovery](https://docs.ray.io/en/latest/ray-core/fault_tolerance/actors.html).
