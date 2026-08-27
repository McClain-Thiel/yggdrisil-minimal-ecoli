# Scientific source ledger

The repository stores acquisition code and validation rules, not redistributable
source snapshots. Local manifests are the run-specific provenance record.

| Evidence | Source of record | Frozen check | Use in this application |
| --- | --- | --- | --- |
| Canonical genes | NCBI RefSeq GFF3 for `GCF_000005845.2` | SHA-256 `7aa71ffaef2caa51e5cb00da96d567c8001c19f029a173d3df3b273331a587b2`, plus assembly, chromosome, organism, and feature-type checks | Defines the only v1 search universe and core metadata. |
| Gene/KO crosswalk and modules | KEGG REST API, organism `eco` | Access time, database info, response hashes, parser semantics | Adds annotations and evaluates modules; never adds canonical genes. |
| Metabolic model | Monk et al. 2017 Supplementary Data Set 1, DOI `10.1038/nbt.3956` | Archive SHA-256 `e799bb0e266224f3f79a63ccffad98d4bec9a9aa29b4884de86be177138770a1`; JSON member SHA-256 `832e706681b60eeefce844348dd7ded1520b7f8d2c2d72d423e0b77cc473dc45` | Supplies exact iML1515 GPRs, reactions, metabolites, objective, and model membership. |
| Experimental essentiality | Choe et al. 2023 Table S1, DOI `10.1128/msystems.00896-22` | Workbook SHA-256 `b1b27667bb9671e0cf031c46bb91e99077e759f4ccd5f75642c809e4d8b9595e` | Supplies condition-specific LB/M9 classifications; canonical genes without a measurement remain unknown. |
| MDS42 held-out validation | NCBI nucleotide records `AP012306` and `NC_000913.3` | Wrapper record length and SHA-256, minimap2 version/parameters, reference registry SHA-256 | Derives large reference deletion intervals and their canonical protein-coding genes after search; never loaded by an agent policy. |
| MS56 held-out validation | Park et al. 2014 Supplementary Table S3, DOI `10.1007/s00253-014-5739-y` | Supplement PDF SHA-256 and exact locus-tag extraction | Supplies the published MS56 deleted-gene set after intersection with the canonical search universe; never loaded by an agent policy. |

NCBI data are fetched from NCBI. KEGG access is optional, rate-limited below
three requests per second, gated by `--accept-kegg-terms`, and its response
snapshots are not committed. The Springer Nature iML1515 supplement is retained
only as a local artifact. Choe supplementary data are CC BY 4.0, but are also
kept out of Git so every generated build follows the same provenance path.

Reduced-genome source files and generated labels remain under the gitignored
`data/validation/` directory. The repository records the builder and source
identifiers, while each local validation artifact records the exact source,
registry, aligner, and output inputs used for a comparison.

Published whole-cell-model and current vEcoli crosswalks are intentionally
deferred and are not exposed in the search registry.

The v1 numerical environment pins COBRApy 0.32.1, Optlang 1.9.1, and swiglpk
5.0.13 directly in `pyproject.toml`. All three runtime versions are reported in
FBA provenance and participate in its cache fingerprint.
