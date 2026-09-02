# Technical report: closed-book agentic search for reduced *E. coli* genomes

**Status:** interim in-silico methods report

**Evidence cutoff:** 2 September 2026

**Application revisions:** first five
`d87debf67379ff4826937e7e0b26918e2235d438`; second five
`8fea728af685ef0b036893e57e528a474028e203`; Minesweeper scaling
`2032c5aba3df47dca71ff8736c8ea424ceb66f6b`

**Yggdrisil revision:**
`67983c5c0821c57e6b0f60449b3e608b981455e2`

## Executive summary

This study asks whether a closed-book language-model policy can search a large
combinatorial gene-deletion space more effectively than a strong structural
baseline when both receive the same maximum number of candidate evaluations.
The
benchmark restricts deletion proposals to 1,216 canonical *Escherichia coli*
K-12 protein-coding genes that map into the 1,219-gene whole-cell-model (WCM)
universe released by Gherman et al. The agent sees blinded identifiers and
categorical evidence rather than canonical locus tags.

At a common budget of at most 193 evaluated states per run and across ten
paired seed blocks, the maximum deletion count among states passing both the
iML1515 flux-balance-analysis (FBA) gate and a fixed-growth
resource-balance-analysis (RBA) gate was:

| Method | Mean deletions | 95% bootstrap interval for mean | Range | Mean fraction of eligible set deleted |
| --- | ---: | ---: | ---: | ---: |
| Matched-cap Minesweeper | 139.9 | 120.1--161.0 | 95--207 | 11.5% |
| Closed-book Sol | 238.6 | 227.7--250.1 | 211--268 | 19.6% |
| Closed-book Qwen | 275.8 | 259.2--291.7 | 239--305 | 22.7% |

Sol exceeded matched-cap Minesweeper in all ten paired seeds, by a mean of
98.7 deletions (paired run-level bootstrap 95% interval 69.9--125.6). Qwen
also won all ten pairs, by a mean of 135.9 deletions (102.9--165.6). Each exact
two-sided paired sign-flip test gives `p=0.001953125`; correcting the two
declared primary comparisons by Holm's method gives `p=0.00390625` for each.
The comparison family was declared before seeds 606--1010 were run, but after
the first-five checkpoint had been observed; the analysis is therefore a
locked replication extension, not a wholly preregistered confirmatory study.
The experimental unit is one complete search run, not an evaluated DAG state
or gene.

These results are strong evidence that, under this particular action space,
evaluation stack, and 193-state budget, the two agent policies find more
extensively deleted **model-feasible** candidates than the matched structural
baseline. They do not establish that the candidates are biologically viable,
minimal, smaller than the published EMine-737 design, or superior to the
native published Minesweeper workflow. The deepest current endpoint deletes
305 of 1,216 eligible genes, leaving 911 in that subset. EMine-737 retained
737 of the 1,219 WCM genes after 39,086 one-generation surrogate-assisted and
1,525 six-generation WCM simulations; it was then tested directly in the WCM
across repeated lineages. Those are different computational budgets and
validation standards, so the designs must not be ranked as if they came from
one experiment [Gherman et al., 2025](https://pure.hw.ac.uk/ws/portalfiles/portal/160646316/PIIS240547122500225X.pdf).

The present evidence supports a computational methods paper when framed as a
sample-efficiency result under a fixed scientific-evaluation budget. The
highest-priority work before a strong submission is to measure the matched-cap
Minesweeper scaling curve, predeclare current finalists and run them in
vEcoli, and replicate the gate and pure tool-access ablations. Extending the
scheduler intervention and the remaining action/evidence experiments would
strengthen mechanism attribution but is secondary to those tests.

## 1. Scientific context

Genome reduction is a contextual optimization problem: the effect of deleting
a gene depends on the medium and on the other deletions already present.
Whole-cell-model design algorithms such as Minesweeper therefore use repeated
design-simulate-test cycles rather than a static list of individually
nonessential genes. The published Minesweeper workflow screens deletion
segments, combines successful segments, subdivides unsuccessful ones, and
eventually tests individual genes [Rees-Garbutt et al.,
2020](https://www.nature.com/articles/s41467-020-14545-0).

The current application replaces the expensive online WCM with several
cheaper, explicit evidence layers. iML1515 is a genome-scale metabolic
reconstruction containing 1,515 open reading frames and 2,719 reactions and
supports FBA-based gene-knockout prediction [Monk et al.,
2017](https://pmc.ncbi.nlm.nih.gov/articles/PMC6521705/). RBA extends metabolic
constraints with molecular-machine and proteome-allocation constraints,
including protein synthesis, folding, transport, catalytic capacity, and
compartment density [Bulovic et al.,
2019](https://www.sciencedirect.com/science/article/pii/S1096717619300710);
[Bodeit et al.,
2023](https://academic.oup.com/bioinformaticsadvances/article/3/1/vbad056/7136629).
Neither method is a whole-cell simulation. The E. coli WCM integrates multiple
cellular processes and thousands of experimentally curated parameters, but its
functional gene coverage is also incomplete [Macklin et al.,
2020](https://www.science.org/doi/10.1126/science.aav3751); [Sun et al.,
2021](https://pmc.ncbi.nlm.nih.gov/articles/PMC11163835/).

Gherman et al. used a random-forest surrogate to accelerate later generations
of WCM simulation. Importantly, that surrogate is not a direct
deletion-vector-to-viability model: it consumes macromolecular features from
the end of the first WCM generation and predicts whether later generations
divide. This is why it was not used as the online evaluator here. The released
1,219-gene list was used only to define a common comparison universe [Gherman
et al.,
2025](https://pure.hw.ac.uk/ws/portalfiles/portal/160646316/PIIS240547122500225X.pdf).

## 2. Methods

### 2.1 Candidate universe and information boundary

The canonical registry contains 4,290 protein-coding genes from [NCBI RefSeq
assembly `GCF_000005845.2`](https://www.ncbi.nlm.nih.gov/bioproject?LinkName=nuccore_bioproject&from_uid=556503834).
The benchmark intersects that registry with the
1,219-gene WCM source list, yielding 1,216 eligible deletion targets. Genes
outside this set remain present and cannot be proposed for deletion. Reported
deletion counts therefore measure reduction of the eligible protein-coding
subset, not total chromosome length or total remaining genes.

The policy is closed-book. Each run creates a seed-specific bijection from
canonical gene identifiers to blinded public identifiers. Model prompts,
candidate previews, tool arguments, and model outputs are checked against the
per-invocation exposure set. Canonical identifiers are translated only inside
the application. The sole scientific tool available in this condition returns
aggregate evidence for a proposed bundle and enforces the same 20-gene action
ceiling. Frozen graph audits found no canonical identifiers in model I/O for
the analyzed runs.

The held-out reduced-genome labels and vEcoli results are not loaded by the
search policy. MDS42 and MS56 are nevertheless not untouched validation sets:
they were used before the paid runs to check that the RBA floor was permissive.
They may support agent-blinded rediscovery analyses, but not independent
evaluator validation.

### 2.2 Search policy

Two OpenRouter model routes were evaluated:

- `openai/gpt-5.6-sol`
- `qwen/qwen3.6-35b-a3b`

Each selected parent can produce up to four alternative deletion actions, and
each action may contain from one to twenty genes at the model's discretion.
The recoverable scheduler maintains an active set of viable states, mixes
exploitation with deletion-set diversity, and can reopen viable non-leaf
states after a descendant is lethal or locally exhausted. It does not
permanently prune a viable state because a fixed number of model calls has
been attempted. Decisions, proposals, created/reused transitions, evaluation
records, errors, and usage metadata are persisted in the Yggdrisil SQLite DAG.
The framework and its exact revision are available from the [Yggdrisil source
repository](https://github.com/McClain-Thiel/yggdrisil).

The locked per-run limits were:

- 193 total states, 48 policy steps, and 7,200 seconds;
- active-set width 16 and up to four selected parents per step;
- four proposals per parent;
- at most six model requests and 16 scientific tool calls per explorer
  invocation;
- at most 8,000 output tokens per model request; and
- USD 0.02 provider-estimated cost per explorer invocation, plus a separate
  authenticated account-level batch guard.

The model seed fixes application RNG, blinded mapping, and candidate order. It
does not guarantee deterministic sampling by the remote provider.

### 2.3 Evaluation stack

Every state receives five immutable evaluation records:

1. number of deleted and remaining canonical protein-coding genes;
2. condition-specific experimental essentiality evidence derived from the
   Choe et al. transposon-insertion study [Choe et al.,
   2023](https://journals.asm.org/doi/10.1128/msystems.00896-22);
3. [KEGG MODULE](https://www.genome.jp/kegg/module.html) retention evidence;
4. iML1515 FBA growth; and
5. feasibility of the pinned E. coli K-12 RBA model at a fixed growth rate of
   0.1 h^-1.

Only positive FBA growth and RBA feasibility at 0.1 h^-1 are hard expansion
requirements. Experimental essentiality categories, unknown coverage, and
broken modules are soft ranking evidence. This is deliberate: experimental
single-gene calls can change with condition and genetic background, whereas a
hard exclusion would prevent the search from investigating compensatory
contexts.

The RBA artifact is pinned to RBAgroup model commit
`973f00e0618493e6df6af52bdde55686168fda62`; each of its 16 source files and
the generated structure are checked by SHA-256. The production evaluator uses
SciPy's interface to the [HiGHS linear optimization
software](https://highs.info/index.html), with presolve and `1e-7` primal/dual
feasibility tolerances. The
0.1 h^-1 value is a predeclared operational floor, not a published viability
threshold or a per-state maximum-growth estimate. The model maps 1,441 of the
4,290 canonical genes; candidate-specific modeled and unmodeled deletions are
reported explicitly. The source model is available from the [official
RBA-models repository](https://github.com/RBAgroup/RBA-models).

### 2.4 Comparator

The primary non-agent comparator is `minesweeper-matched20`, a clean-room,
equal-state-budget adaptation of the published Minesweeper strategy. It:

- shuffles the eligible set by the paired seed;
- omits genes classified essential in both Choe conditions, evidence also
  available to the agent;
- screens non-overlapping segments of at most 20 genes;
- combines successful segments;
- bisects lethal bundles; and
- finishes with singleton cleanup.

It uses the same 193-state cap and the same five evaluators. This is a strong
structural baseline, but it is not the native Minesweeper implementation. Its
prefilter has global access to the experimental-essentiality classification,
whereas the closed-book agent sees that category only for identifiers exposed
in its current invocation; this asymmetry favors the baseline. The
published method used a WCM, larger stages, and thousands of simulations
[Rees-Garbutt et al.,
2020](https://www.nature.com/articles/s41467-020-14545-0). Accordingly, this
report says "matched-cap Minesweeper," not "published Minesweeper."

Random-uniform and evolutionary-uniform controls were also run. Across ten
seeds, the evolutionary baseline averaged 65.9 jointly feasible deletions,
while matched-cap Minesweeper averaged 139.9 and won all ten paired seeds.
This establishes Minesweeper as the stronger tested non-agent comparator.

### 2.5 Experimental design and statistics

The analyzed seed blocks were `101, 202, 303, 404, 505, 606, 707, 808, 909,
1010`. The second five were locked as a symmetric replication block before
their model calls, after the first-five checkpoint had been observed. Each
method/seed arm used a fresh graph. Model order alternated between seed blocks
and paid calls ran serially. The primary endpoint was chosen without WCM or
reduced-genome truth: maximum deletion count among states passing the common
FBA+RBA gate, breaking ties by higher FBA growth and then state ID.

One complete run is the experimental unit. The analysis reports every seed,
group summaries, a 10,000-resample paired run-level percentile bootstrap for
the mean difference, and an exact two-sided sign-flip test. The two
model-versus-Minesweeper primary p-values form one family and are adjusted by
Holm's method. Qwen versus Sol was not a predeclared primary comparison and is
reported descriptively.

### 2.6 Minesweeper scientific-evaluation scaling

The matched-cap Minesweeper policy was subsequently run once per locked seed
to a maximum of 5,000 states. Endpoints at 193, 500, 1,000, and 5,000 states
are nested prefixes of each deterministic trajectory, not independent runs;
as in the primary comparison, each state count includes the wild-type root.
The candidate universe, 20-gene maximum action, four proposals per step,
scientific evaluators, joint FBA+RBA endpoint, seeds, and tie-breaking rule are
unchanged. The first 193 state IDs of every scaling graph must exactly match
the corresponding frozen 193-state graph before the scaling result is
accepted.

This design measures how deletion depth changes with calls to the scientific
evaluator suite. It does not equalize dollars, model tokens, wall-clock time,
or total computation: Minesweeper makes no language-model calls, while its
larger trajectories perform many more FBA and RBA evaluations. Comparisons
between a larger Minesweeper prefix and a 193-state agent endpoint are
therefore descriptive crossing points, not equal-compute hypothesis tests.

Long trajectories were resumed under the same run ID at progressively larger
1,000-state process boundaries to release native solver memory. Run limits are
not policy inputs. The restart ledger retains commands, timestamps, state and
evaluation counts, interruption recovery, and stderr; final analysis accepts
only integrity-checked read-only backups with exactly five active evaluator
records per state. The policy's version-2 implementation bounds temporary
proposal generation but preserves candidate order, as tested by the exact
193-state continuity requirement.

## 3. Results

### 3.1 Per-seed primary endpoints

| Seed | Matched-cap Minesweeper | Sol | Qwen |
| ---: | ---: | ---: | ---: |
| 101 | 173 | 233 | 239 |
| 202 | 111 | 233 | 287 |
| 303 | 207 | 211 | 246 |
| 404 | 174 | 239 | 293 |
| 505 | 140 | 268 | 252 |
| 606 | 117 | 241 | 302 |
| 707 | 133 | 213 | 295 |
| 808 | 95 | 268 | 300 |
| 909 | 141 | 237 | 305 |
| 1010 | 108 | 243 | 239 |

All 30 selected endpoints had positive iML1515 growth and passed the final
HiGHS-RBA fixed-growth test. Sol's smallest paired advantage over Minesweeper
was four deletions; Qwen's was 39. Therefore, the conclusion is not driven by
one extreme seed.

| Primary contrast | Mean paired difference | 95% bootstrap interval | Median difference | Wins | Exact p | Holm-adjusted p |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| Sol minus Minesweeper | 98.7 | 69.9--125.6 | 109.0 | 10/10 | 0.001953125 | 0.00390625 |
| Qwen minus Minesweeper | 135.9 | 102.9--165.6 | 146.5 | 10/10 | 0.001953125 | 0.00390625 |

Qwen exceeded Sol in eight of ten seed blocks. The descriptive mean paired
difference was 37.2 deletions (bootstrap interval 17.4--55.4; unadjusted exact
`p=0.01171875`). Because that contrast was not predeclared as primary and the
two routes differ in provider implementation as well as model, it should be
treated as a hypothesis for replication rather than the paper's central
claim.

The authenticated OpenRouter account-usage delta for the twenty paid model
runs was USD 48.56144675. Graph-recorded provider cost is incomplete for Sol
and must not be substituted for this account-level total.

### 3.2 Scheduler ablation

The first five Sol seeds were rerun with only the scheduler changed from
recoverable to frontier-only. Frontier-only terminated after two to four
states and found `20, 40, 8, 0, 0` deletions, whereas recoverable search found
`233, 233, 211, 239, 268`. The mean paired improvement was 223.2 deletions
(bootstrap interval 201.0--249.2), with five wins in five seeds. At `n=5`, the
smallest possible two-sided sign-flip p-value is 0.0625, which is the observed
value. This is large mechanistic evidence for state reopening, but remains a
five-seed ablation until seeds 606--1010 are completed.

### 3.3 Relation to EMine-737 and whole-cell evidence

The deepest current candidate removes 305 of the 1,216 eligible genes (25.1%)
and leaves 911 genes in that subset. It does not delete genes outside the
eligible set. EMine-737 contains 737 of the WCM's 1,219 modeled genes (a 39.5%
reduction) and was validated by repeated six- and ten-generation WCM
simulations [Gherman et al.,
2025](https://pure.hw.ac.uk/ws/portalfiles/portal/160646316/PIIS240547122500225X.pdf).
Consequently, the current candidates are neither smaller within the WCM set
nor as directly validated as EMine-737.

A separate older experiment provides encouraging but non-transferable WCM
evidence. Five Sol finalists from a full 4,290-gene search, each with
1,470--1,597 deleted canonical genes, were simulated with pinned vEcoli at
lineage seeds 101, 202, and 303. All 15 lineages recorded division through 20
of 20 generations, for 300 recorded divisions. vEcoli is the maintained
Vivarium port of the Covert Lab E. coli WCM ([official
repository](https://github.com/CovertLab/vEcoli)). RBA mapped only 4--11 of the
1,470--1,597 deletions in each of those old designs. In contrast, the vEcoli
adapter mapped every requested canonical deletion one-to-one to a unique
cistron and RNA, set all audited expression parameters to zero, and rejected
ambiguous mappings. This establishes complete knockout execution in that
simulation, not complete mechanistic representation of every deleted gene's
function. These genotypes are not the current 1,216-gene benchmark finalists,
and the result remains model evidence, not wet-lab viability.

## 4. Interpretation

### What the experiment supports

1. **Search-performance claim.** Under the same maximum 193-state scientific-
   evaluation budget, common candidate universe, and common FBA+RBA endpoint,
   both closed-book agent policies outperform the strongest structural
   baseline tested across all ten paired seeds.
2. **Recovery mechanism.** The five-seed scheduler intervention shows that
   reopening viable non-leaf states is necessary for deep search in this
   setup; a frontier-only policy collapses after lethal descendants.
3. **Information-boundary claim.** The result was obtained without exposing
   canonical identifiers, reduced-genome targets, or WCM outcomes to the
   policy. The agent did receive blinded categorical evidence and a bounded
   aggregate bundle-analysis tool; "closed-book" does not mean
   "no scientific evidence."
4. **Reproducibility claim.** Every run retains graph structure, evaluator
   records, policy decisions, proposal events, model/tool traces, usage,
   revisions, artifact identities, and checksums. The uploaded archives were
   downloaded and hash-verified after transfer.

### What the experiment does not support

1. **Biological viability.** FBA and RBA are constrained mechanistic models,
   not observations of a cell. RBA feasibility at 0.1 h^-1 is an operational
   gate, not proof of division.
2. **A new minimal genome.** The search stops at a fixed search cap, not at a
   proof of minimality. Genes outside the 1,216-gene action space are always
   retained.
3. **Superiority to EMine-737.** EMine-737 is smaller within the WCM universe
   and has direct multi-generation WCM validation. The present comparison is
   about equal-budget search depth under cheaper evaluators.
4. **Superiority to native Minesweeper at native compute.** The comparator is
   an action-capped adaptation evaluated 193 times, not a reproduction of the
   paper's WCM-scale pipeline.
5. **Wet-lab function, robustness, or safety.** No current finalist has been
   constructed, and no claim is made outside the pinned in-silico medium and
   model assumptions.

## 5. Important limitations and threats to validity

- **Evaluator coverage.** RBA covers 1,441 canonical genes and iML1515 is
  metabolism-centered. An unmodeled deletion is reported but cannot be judged
  mechanistically by that evaluator. Passing both gates is therefore necessary
  under the protocol, not sufficient for life.
- **Numerical revision.** The production RBA gate was changed from GLPK to
  HiGHS after GLPK false positives and pathological runtimes were found. The
  first five model trajectories used the preceding HiGHS evaluator identity.
  The final revision only added retries for indeterminate numerical status 4;
  every stored first-five RBA result was directly classified `optimal` or
  `infeasible` by the unchanged primary solve, and every selected endpoint was
  independently rescored feasible under the final identity. The policy-visible
  feasibility decisions are therefore equivalent for the stored trajectories.
  A free full-state rescore can make this equivalence explicit, but repeating
  the paid model calls is not required.
- **Comparator fidelity.** Equalizing states makes the algorithmic comparison
  interpretable but intentionally departs from the published Minesweeper
  compute regime.
- **Compute metric.** The 193-state cap equalizes calls to the scientific
  evaluator suite, not dollars, tokens, or wall-clock compute. The structural
  baseline is far cheaper than an LLM policy. A state-budget scaling curve and
  cost-normalized analysis are required before claiming general computational
  efficiency.
- **Adaptive replication design.** The first-five results were already known
  when matched-cap Minesweeper was selected as the primary baseline and the
  second-five block was locked. The new five pairs replicate the direction and
  magnitude, but the full ten-pair p-values must not be described as arising
  from a wholly prospective preregistration.
- **Remote-model reproducibility.** Application seeds do not freeze model
  weights, serving stack, or provider sampling. Exact prompts and outputs are
  preserved, but future reruns may differ.
- **Calibration reuse.** MDS42 and MS56 informed the RBA-floor decision and
  cannot be described as untouched viability tests.
- **Multiple unfinished ablations.** Gate, action-size, evidence-exposure, and
  pure no-tool ablations are still pending. Without them, the contribution of
  individual agent components is not fully identified.
- **Current-finalist WCM gap.** The existing 300/300 division result belongs to
  an older cohort. The present Sol/Qwen endpoints require predeclared vEcoli
  testing with biological-unit-level analysis.

## 6. Publication-readiness assessment

The current result is more than a promising anecdote: it is a complete
ten-pair comparison with a predeclared run-level endpoint, a strong equal-state-
budget baseline, large effects, exact paired inference, provenance-complete graphs,
and off-machine preservation. That is a credible central result for a
computational genome-design methods manuscript.

It is not yet a complete paper package. The minimum high-value additions are:

1. run a predeclared 500/1,000/5,000-state matched-cap Minesweeper scaling
   curve and report both evaluator-budget and compute/cost-normalized results;
2. predeclare one finalist per independent seed and method before viewing new
   vEcoli outcomes, then simulate multiple lineage seeds with matched WT,
   benign-deletion, essential-deletion, and workflow controls;
3. replicate the one-factor FBA-only gate experiment and implement the same-
   evidence closed-book/no-tool comparison before assigning causal importance
   to RBA feedback or tool use;
4. report overlap/diversity and functional enrichment across independent
   finalists, rather than presenting only the deepest genotype; and
5. reserve biological viability claims for wet-lab construction and growth
   measurements.

Extending the five-seed scheduler intervention to ten seeds and running the
fixed-action-size and broader tool-rich conditions remain useful secondary
ablations, but they are not prerequisites for the central fixed-evaluation-
budget search claim.

If those items hold, the strongest defensible central claim is:

> At a fixed candidate-evaluation budget, closed-book agent-guided recoverable
> search finds more extensively reduced *E. coli* genotypes satisfying pinned
> metabolic and resource-allocation constraints than a matched-cap structural
> Minesweeper baseline; scheduler and information/action ablations explain the
> gain, and predeclared finalists are evaluated independently in a pinned
> whole-cell model.

## 7. Reproducibility and data availability

The application source is available at the [project
repository](https://github.com/McClain-Thiel/yggdrisil-minimal-ecoli). Large
immutable evidence is stored in the [McClain/Yggdrisil-ecoli-min Hugging Face
bucket](https://huggingface.co/buckets/McClain/Yggdrisil-ecoli-min), organized
by evidence class with README files.

Key preserved bundles are:

| Evidence | Bucket object | SHA-256 |
| --- | --- | --- |
| Sol/Qwen seeds 101--505 | `01-search-evidence/wcm1216-sol-qwen-highs-seeds101-505-20260830.tar.gz` | `852805895077ab937b2af3edd16e671db56858bf89830d4839b8420437c50f9e` |
| Sol/Qwen seeds 606--1010 and formal ten-seed analysis | `01-search-evidence/wcm1216-sol-qwen-highs-seeds606-1010-20260901.tar.gz` | `789513783a138e55dfd6f747c57f48c04c7c9301b5aa3349d4fdd14e9cb28302` |
| Strong baselines seeds 101--505 | `01-search-evidence/wcm1216-strong-baselines-highs-seeds101-505-20260831.tar.gz` | `fb51760f9cabf632787c331ff3e9e4bf2a85fabec7cb243d54340711e884f762` |
| Strong baselines seeds 606--1010 | `01-search-evidence/wcm1216-strong-baselines-highs-seeds606-1010-20260901.tar.gz` | `c03657c8eb5039c7be7f7ce5b0cec9e8eb69628a9d5b4e9d19919b1a3bbb31c2` |
| Scheduler ablation seeds 101--505 | `01-search-evidence/wcm1216-sol-scheduler-ablation-highs-seeds101-505-20260831.tar.gz` | `6526366194d9eb24ccca23248d15643a62abc8f711d196aa407584b6be7daf29` |

The second-five model archive contains ten new mutable and read-only frozen
graphs, per-arm logs, budget snapshots, exact runner/analyzer/finalizer code,
environment and artifact snapshots, `integrity.json`, and a checksum manifest
covering 109 durable files. Its uploaded archive was downloaded after transfer
and reproduced the local SHA-256 exactly. Third-party source snapshots with
redistribution restrictions are excluded; acquisition URLs, commits, and
source hashes are retained instead.

## References

1. Rees-Garbutt J, Chalkley O, Landon S, et al. Designing minimal genomes using
   whole-cell models. *Nature Communications*. 2020;11:836.
   [doi:10.1038/s41467-020-14545-0](https://www.nature.com/articles/s41467-020-14545-0).
2. Monk JM, Lloyd CJ, Brunk E, et al. iML1515, a knowledgebase that computes
   *Escherichia coli* traits. *Nature Biotechnology*. 2017;35:904--908.
   [doi:10.1038/nbt.3956](https://pmc.ncbi.nlm.nih.gov/articles/PMC6521705/).
3. Bulovic A, Fischer S, Dinh M, et al. Automated generation of bacterial
   resource allocation models. *Metabolic Engineering*. 2019;55:12--22.
   [doi:10.1016/j.ymben.2019.06.001](https://www.sciencedirect.com/science/article/pii/S1096717619300710).
4. Bodeit O, Ben Samir I, Karr JR, Goelzer A, Liebermeister W. RBAtools: a
   programming interface for Resource Balance Analysis models.
   *Bioinformatics Advances*. 2023;3:vbad056.
   [doi:10.1093/bioadv/vbad056](https://academic.oup.com/bioinformaticsadvances/article/3/1/vbad056/7136629).
5. Choe D, Kim U, Hwang S, et al. Revealing causes for false-positive and
   false-negative calling of gene essentiality in *Escherichia coli* using
   transposon insertion sequencing. *mSystems*. 2023;8:e00896-22.
   [doi:10.1128/msystems.00896-22](https://journals.asm.org/doi/10.1128/msystems.00896-22).
6. Macklin DN, Ahn-Horst TA, Choi H, et al. Simultaneous cross-evaluation of
   heterogeneous *E. coli* datasets via mechanistic simulation. *Science*.
   2020;369:eaav3751.
   [doi:10.1126/science.aav3751](https://www.science.org/doi/10.1126/science.aav3751).
7. Sun G, Ahn-Horst TA, Covert MW. The *E. coli* Whole-Cell Modeling Project.
   *EcoSal Plus*. 2021;9:eESP-0001-2020.
   [doi:10.1128/ecosalplus.ESP-0001-2020](https://pmc.ncbi.nlm.nih.gov/articles/PMC11163835/).
8. Gherman IM, Sharma K, Rees-Garbutt J, et al. Accelerated design of
   *Escherichia coli* reduced genomes using a whole-cell model and machine
   learning. *Cell Systems*. 2025;16:101392.
   [doi:10.1016/j.cels.2025.101392](https://pure.hw.ac.uk/ws/portalfiles/portal/160646316/PIIS240547122500225X.pdf).
9. Huangfu Q, Hall JAJ. Parallelizing the dual revised simplex method.
   *Mathematical Programming Computation*. 2018;10:119--142.
   [doi:10.1007/s12532-017-0130-5](https://doi.org/10.1007/s12532-017-0130-5).
