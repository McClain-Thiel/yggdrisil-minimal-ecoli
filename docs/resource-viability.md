# Resource-allocation viability gate

## Decision

The next online search adds the official E. coli K-12 RBA model snapshot, loaded with
RBApy/RBAtools, as an independent resource-allocation evaluator. A state is
eligible for further expansion only when iML1515 FBA predicts positive growth
and the RBA model is feasible at the predeclared 0.1 h^-1 growth floor.
Experimental essentiality and KEGG module retention remain soft, separately
reported evidence.

The University of Bristol whole-cell-model surrogate is not used as an online
evaluator. Its random forest does not accept a deletion vector: each candidate
first requires a one-generation whole-cell simulation to produce eleven
macromolecular features. It remains a later, selective validation path for a
predeclared shortlist in the WCM's 1,219-gene universe.

## Alternatives evaluated

- [Gherman et al. 2025](https://doi.org/10.1016/j.cels.2025.101392) and its
  [released surrogate](https://github.com/ioanagherman/surrogateMinesweeperEcoli)
  accelerate generations two through six after a mechanistic WCM has already
  simulated generation one. They are therefore a finalist validator, not a
  deletion-vector scorer. The repository and weights have no clear root
  license, and the bundled legacy WCM has restrictive academic terms.
- [coralME](https://github.com/jdtibochab/coralme) is a stronger ME-model path
  for selected candidates, but its numerically difficult E. coli model needs a
  commercial macOS solver or an amd64 container on this Apple Silicon host. The
  code is MIT; the separate prebuilt model repository does not state a root
  license.
- The [RBAgroup model](https://github.com/RBAgroup/RBA-models/tree/master/Escherichia-coli-K12-WT)
  provides direct multi-gene knockout semantics, a free GLPK solve, and enough
  cellular-process coverage to reject the saved FBA-only candidate. RBApy and
  RBAtools are GPL-3.0; the model is CC-BY-NC-4.0 and remains a local artifact.

## Scientific contract

- Source of record: the RBAgroup E. coli K-12 WT model at Git commit
  `973f00e0618493e6df6af52bdde55686168fda62`, with a pinned SHA-256 for every
  downloaded model file.
- Environment: the model's published medium and constraints, SciPy's HiGHS LP
  solver, RBApy 3.0.3, RBAtools 2.0.1, and swiglpk 5.0.13 as RBAtools' matrix
  construction backend. HiGHS avoids pathological runtimes and unreliable
  fallback statuses observed with GLPK on random multi-gene deletions. Presolve
  and the 1e-7 primal/dual feasibility tolerance are explicit. A numerical
  status-not-set result is retried with HiGHS interior-point and then with
  presolve disabled; every attempt is recorded, and unresolved results still
  fail rather than being assigned a biological class. The solver, ordered
  fallback methods, matrix backend, presolve, and tolerance participate in
  evaluator identity.
- Deletion semantics: canonical MG1655 `b`-number knockouts disable every RBA
  enzyme and process machine that requires the encoded protein. Unknown genes
  are rejected by the mapper and reported as uncovered, never inferred safe.
- Output: feasibility at 0.1 h^-1, solver status, mapped/unmapped deletion
  counts, the repository wild-type maximum-growth reference
  (0.5986785888671875 h^-1), and exact model/configuration provenance. The
  evaluator does not perform a per-state maximum-growth search.
- Search eligibility: positive iML1515 growth **and** RBA feasibility at the
  fixed 0.1 h^-1 floor. Neither result is called experimental viability.
- Search ranking among RBA-feasible states: deletion count first, then FBA
  growth; experimental essentiality, unknown evidence, and broken modules are
  independent Pareto-style penalties.
- Reproducibility: changing the resource model, mapping, numerical stack, or
  environment changes the evaluator identity and makes an old run ineligible
  for resume.

## Acceptance checks

1. The builder refuses a changed source digest, missing source file, malformed
   model, or unexpected model dimensions.
2. Wild type is feasible at the fixed 0.1 h^-1 growth floor.
3. A known translation/process-machinery deletion that ordinary FBA does not
   model is rejected by RBA.
4. A neutral modeled deletion remains feasible.
5. Unmodeled deleted genes are reported, not treated as safe.
6. Repeated and concurrent evaluations are deterministic and do not mutate the
   frozen base model.
7. Open-set, heuristic, summarization, and environment validation use the exact
   active resource evaluator identity and require positive resource growth.
8. A changed resource artifact is rejected on resume.
9. A fresh closed-book graph passes the fixed biological panel before paid
   model calls. The prior FBA-only run remains archived and immutable.

## Known limitation

RBA predicts balanced exponential growth with enzyme, translation, chaperone,
secretion, compartment, and proteome-allocation constraints. It is not a
whole-cell model. The frozen artifact maps 1,441 of the 4,290 canonical genes to
at least one enzyme or process-machine LP variable. Its
value is a stronger mechanistic gate—not proof that a designed cell will divide.
Final claims require held-out comparison and selective vEcoli/WCM simulation or
wet-lab validation.

MDS42 and MS56 are agent-invisible positive calibration controls: they were used
to check that the 0.1 h^-1 floor is permissive before this run, so they are not an
untouched evaluator-level test. Their later overlap scores remain useful as
closed-book rediscovery measurements, but not as independent viability
validation. A separate, predeclared vEcoli/WCM or experimental benchmark is still
required.
