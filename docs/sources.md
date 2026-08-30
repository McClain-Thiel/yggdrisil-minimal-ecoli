# Scientific source ledger

The repository stores acquisition code and validation rules, not redistributable
source snapshots. Local manifests are the run-specific provenance record.

| Evidence | Source of record | Frozen check | Use in this application |
| --- | --- | --- | --- |
| Canonical genes | NCBI RefSeq GFF3 for `GCF_000005845.2` | SHA-256 `7aa71ffaef2caa51e5cb00da96d567c8001c19f029a173d3df3b273331a587b2`, plus assembly, chromosome, organism, and feature-type checks | Defines the only v1 search universe and core metadata. |
| Gene/KO crosswalk and modules | KEGG REST API, organism `eco` | Access time, database info, response hashes, parser semantics | Adds annotations and evaluates modules; never adds canonical genes. |
| Metabolic model | Monk et al. 2017 Supplementary Data Set 1, DOI `10.1038/nbt.3956` | Archive SHA-256 `e799bb0e266224f3f79a63ccffad98d4bec9a9aa29b4884de86be177138770a1`; JSON member SHA-256 `832e706681b60eeefce844348dd7ded1520b7f8d2c2d72d423e0b77cc473dc45` | Supplies exact iML1515 GPRs, reactions, metabolites, objective, and model membership. |
| Resource-allocation model | RBAgroup E. coli K-12 WT model, commit `973f00e0618493e6df6af52bdde55686168fda62`; Bulović et al. 2019, DOI `10.1016/j.ymben.2019.06.001` | SHA-256 for each of the 16 source files, including the repository WT growth output, plus generated-artifact manifest; RBApy/RBAtools/solver versions | Supplies enzyme, translation, chaperone, secretion, compartment, and proteome-allocation constraints for fixed-growth feasibility. |
| Experimental essentiality | Choe et al. 2023 Table S1, DOI `10.1128/msystems.00896-22` | Workbook SHA-256 `b1b27667bb9671e0cf031c46bb91e99077e759f4ccd5f75642c809e4d8b9595e` | Supplies condition-specific LB/M9 classifications; canonical genes without a measurement remain unknown. |
| MDS42 agent-invisible calibration/rediscovery control | NCBI nucleotide records `AP012306` and `NC_000913.3` | Wrapper record length and SHA-256, minimap2 version/parameters, reference registry SHA-256 | Calibrates permissiveness of the RBA gate and later measures closed-book rediscovery; never loaded by an agent policy, but no longer an untouched evaluator-level validation set. |
| MS56 agent-invisible calibration/rediscovery control | Park et al. 2014 Supplementary Table S3, DOI `10.1007/s00253-014-5739-y` | Supplement PDF SHA-256 and exact locus-tag extraction | Calibrates permissiveness of the RBA gate and later measures closed-book rediscovery after intersection with the canonical universe; never loaded by an agent policy, but no longer an untouched evaluator-level validation set. |
| WCM comparison universe | Gherman et al. 2025 code release at commit `47e3997bac61d481fa0e2fa0d48c1b6ca98762b0`, DOI `10.1016/j.cels.2025.101392` | Source SHA-256 `c1c64097b4f6bf2a969af5705efdec833900698d3f4fbcacbae2101d17df96fb`, exact 1,219 rows, explicit three-ID mapping gap, registry and output hashes | Optionally restricts every policy to the 1,216 canonical protein-coding genes shared with the EMine WCM universe. Closed-book identifiers remain blinded. |

NCBI data are fetched from NCBI. KEGG access is optional, rate-limited below
three requests per second, gated by `--accept-kegg-terms`, and its response
snapshots are not committed. The Springer Nature iML1515 supplement is retained
only as a local artifact. Choe supplementary data are CC BY 4.0, but are also
kept out of Git so every generated build follows the same provenance path.

Reduced-genome source files and generated labels remain under the gitignored
`data/validation/` directory. The repository records the builder and source
identifiers, while each local validation artifact records the exact source,
registry, aligner, and output inputs used for a comparison.

WCM comparison membership is a separate, pinned action-space artifact; it does
not modify the canonical registry or expose canonical identifiers in
closed-book runs. A current-vEcoli downstream functional-coverage audit remains
deferred.

The numerical environment pins COBRApy 0.32.1, Optlang 1.9.1, RBApy 3.0.3,
RBAtools 2.0.1, and swiglpk 5.0.13 directly in `pyproject.toml`. Runtime versions
are reported in the corresponding evaluator provenance and participate in cache
fingerprints. The RBA code is GPL-3.0 and the frozen E. coli model is
CC-BY-NC-4.0; generated model artifacts remain gitignored.
