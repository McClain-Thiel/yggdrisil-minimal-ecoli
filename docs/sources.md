# Scientific source ledger

The repository stores acquisition code and validation rules, not redistributable
source snapshots. Local manifests are the run-specific provenance record.

| Evidence | Source of record | Frozen check | Use in this application |
| --- | --- | --- | --- |
| Canonical genes | NCBI RefSeq GFF3 for `GCF_000005845.2` | SHA-256 `7aa71ffaef2caa51e5cb00da96d567c8001c19f029a173d3df3b273331a587b2`, plus assembly, chromosome, organism, and feature-type checks | Defines the only v1 search universe and core metadata. |
| Gene/KO crosswalk and modules | KEGG REST API, organism `eco` | Access time, database info, response hashes, parser semantics | Adds annotations and evaluates modules; never adds canonical genes. |
| Metabolic model | Monk et al. 2017 Supplementary Data Set 1, DOI `10.1038/nbt.3956` | Archive SHA-256 `e799bb0e266224f3f79a63ccffad98d4bec9a9aa29b4884de86be177138770a1`; JSON member SHA-256 `832e706681b60eeefce844348dd7ded1520b7f8d2c2d72d423e0b77cc473dc45` | Supplies exact iML1515 GPRs, reactions, metabolites, objective, and model membership. |
| Experimental essentiality | Choe et al. 2023 Table S1, DOI `10.1128/msystems.00896-22` | Workbook SHA-256 `b1b27667bb9671e0cf031c46bb91e99077e759f4ccd5f75642c809e4d8b9595e` | Supplies condition-specific LB/M9 classifications; canonical genes without a measurement remain unknown. |

NCBI data are fetched from NCBI. KEGG access is optional, rate-limited below
three requests per second, gated by `--accept-kegg-terms`, and its response
snapshots are not committed. The Springer Nature iML1515 supplement is retained
only as a local artifact. Choe supplementary data are CC BY 4.0, but are also
kept out of Git so every generated build follows the same provenance path.

Published whole-cell-model and current vEcoli crosswalks are intentionally
deferred. Their empty registry columns mean "not integrated," not "not modeled."

The v1 numerical environment is locked to COBRApy 0.32.1 and swiglpk 5.0.13
with the transitive Optlang version in `uv.lock`. All three runtime versions are
reported in FBA provenance and participate in its cache fingerprint.
