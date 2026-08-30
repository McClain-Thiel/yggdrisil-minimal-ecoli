# Reference-data contract

## Canonical scope

The initial search universe is the set of `gene` features annotated by NCBI as
`gene_biotype=protein_coding` on `NC_000913.3` in assembly
`GCF_000005845.2` (`ASM584v2`). A canonical identifier must match
`^b[0-9]{4}$`.

Gene symbols are display metadata. They are never accepted as persistent
identity or silently translated outside the registry layer.

## Source ownership

- NCBI RefSeq GFF3 owns the canonical gene set, coordinates, symbols, NCBI Gene
  identifiers, EcoCyc cross-references, and descriptions.
- KEGG contributes only `eco` gene identifiers and KO cross-references. Its API
  is optional, build-time only, rate-limited, and requires explicit acceptance
  of KEGG's academic-use terms.
- The exact iML1515 JSON from Monk et al. 2017 Supplementary Data Set 1
  contributes model membership. The publication archive and member hashes are
  pinned and validated before use; the mutable BiGG download is not the source
  of record.
- Choe et al. 2023 Table S1 contributes experimental essentiality observations
  in LB and aerobic M9 with 0.2% glucose at 37 °C. It never changes registry
  membership.
- The RBAgroup E. coli K-12 WT model contributes enzyme and cellular-process
  capacity constraints. It is frozen at one Git commit and never changes the
  canonical search universe.
- The Gherman et al. 2025 WCM gene list may define a separate candidate
  universe for controlled EMine comparisons. It never changes registry rows.
- Current-vEcoli downstream functional membership remains absent pending a
  dedicated coverage audit.

Crosswalk sources may annotate canonical genes, but they may not create new
members of the search universe.

## Registry columns

The Parquet registry contains the requested identity/crosswalk fields plus
reference coordinates needed later to derive canonical gene sets from
published MG1655 deletion intervals:

| Column | Meaning |
| --- | --- |
| `b_number` | Unique canonical locus tag. |
| `symbol` | Current NCBI display symbol, nullable. |
| `name` | Longer gene name when separately available, nullable. |
| `description` | NCBI product description, nullable. |
| `start`, `end`, `strand` | One-based inclusive reference coordinates. |
| `ncbi_gene_id`, `ecocyc_id` | External identifiers from NCBI GFF3. |
| `kegg_gene_id`, `ko_ids` | Optional KEGG snapshot mappings. |
| `iml1515_gene_id` | Optional model-universe mapping; membership is derived from its presence. |

Protein-coding type and reference accession are validated once at the GFF3
boundary and recorded in the source manifest rather than repeated on every row.
WCM candidate membership and current-vEcoli data are not registry columns.

## Build failures

The build fails before writing the final registry when it detects:

- the wrong assembly, chromosome accession, strain, or substrain;
- malformed or duplicate canonical `b` numbers;
- invalid coordinates;
- incompatible duplicate external identifiers;
- malformed source records.

Unmapped optional crosswalk entries are not silently dropped. They are recorded
as unresolved identifiers in the audit and manifest. Missing optional coverage
is not a build failure and must never be interpreted as evidence that deletion
is safe.

## Derived evidence artifacts

- `essentiality.parquet` contains exactly one row per canonical gene, with its
  LB/M9 source calls, ecIPKM values, derived classification, and explicit
  measured/unknown coverage. Study, assay, medium, and source-provenance
  constants are stored once in Parquet metadata rather than repeated per gene.
- `kegg_modules.json` contains definitions for modules KEGG reports complete in
  wild-type MG1655, plus referenced dependencies. Each keyed definition stores
  only its name, expression, and class; referenced identifiers and complete
  result lists are derived at evaluation time. KO assignments belonging to
  non-protein-coding MG1655 genes remain fixed background evidence because those
  genes are outside the deletion universe. The builder requires the exact
  gene-to-KO snapshot recorded by the registry manifest. Module results retain
  the catalog, registry, gene-to-KO, and fixed-background source hashes.
- `iML1515.json` is a local, hash-validated publication artifact. FBA reports
  model coverage separately for every deletion set.
- `rba_ecoli_k12_wt/` contains the locally acquired, hash-validated RBA model
  and generated model structure. Resource-allocation evaluation reports modeled
  and unmodeled deleted genes separately and tests feasibility at the fixed
  0.1 h^-1 growth floor; absence from the RBA model is not evidence of safety.
- `wcm_1219_candidate_universe.json` contains the canonical intersection with
  the exact 1,219-gene WCM list used for EMine-737. Its 1,216 genes constrain
  deletion eligibility only; all five evaluators still score every state.

All derived scorer results retain artifact/configuration fingerprints. A result
is invalidated when its state, scorer version, source artifact, crosswalk,
medium, solver configuration, or model changes.
