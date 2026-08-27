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
  identifiers, EcoCyc cross-references, protein accessions, and descriptions.
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
- Published-WCM and current-vEcoli membership remain absent until their
  independent discovery milestones are complete.

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
| `gene_type` | `protein_coding` in v1. |
| `reference_accession` | Must be `NC_000913.3`. |
| `start`, `end`, `strand` | One-based inclusive reference coordinates. |
| `protein_id` | First NCBI translated-product accession for compact display. |
| `product_descriptions`, `protein_ids` | Complete ordered CDS-product lists; these preserve genes such as `b0149` that have more than one annotated translation. |
| `ncbi_gene_id`, `ecocyc_id` | External identifiers from NCBI GFF3. |
| `kegg_gene_id`, `ko_ids` | Optional KEGG snapshot mappings. |
| `*_gene_id`, `in_*` | Model-universe membership and identifier. |

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

- `essentiality_observations.parquet` preserves the source rows and conditions.
- `essentiality_summary.parquet` reports each canonical gene as `essential`,
  `conditionally_essential`, `nonessential`, `ambiguous`, or `unknown`.
- `kegg_modules.json` contains definitions for modules KEGG reports complete in
  wild-type MG1655, plus referenced dependencies. KO assignments belonging to
  non-protein-coding MG1655 genes remain fixed background evidence because those
  genes are outside the deletion universe. The builder requires the exact
  gene-to-KO snapshot recorded by the registry manifest and copies its lineage
  and hash into the module manifest.
- `iML1515.json` is a local, hash-validated publication artifact. FBA reports
  model coverage separately for every deletion set.

All derived scorer results retain artifact/configuration fingerprints. A result
is invalidated when its state, scorer version, source artifact, crosswalk,
medium, solver configuration, or model changes.
