# M1 verification — oneM2M pipeline end to end

**Criterion (interim report, slide 12):** virtual node → MN → inference → /clean → IN works end to end.
**Result: met** (2026-11-14).

## Setup under test

| Part | Version |
|---|---|
| Cloud IN-CSE | Mobius4 v4.24.5 (`3da4d85`), PostgreSQL 16.15 + PostGIS 3.4, Node 22 |
| Edge MN-CSE | tinyIoT `c140495` + `patches/01..04`, SQLite, built by `scripts/build_tinyiot.sh` |
| AEs | Python 3.13, scikit-learn 1.9.1, numpy 2.5.3, pandas 3.0.6 |
| Input | `sim/data/stream_demo_labeled.csv` — synthetic, 543 rows: node1 179, node2 183, node3 181 (15-minute steps, 2 days) |
| Host | one Ubuntu 24.04 host, all components on 127.0.0.1 |

## What `scripts/e2e_check.py` checks

For each node: rows written to `EdgeMN/icing/nodeK/raw` (sent) = results in `EdgeMN/icing/nodeK/clean` on the MN
(clean) = results whose state equals the **same model run offline on the same rows** (match) = copies in
`Mobius/icing/history/nodeK` on the IN (IN hist). It also reports rows the edge discarded as late.

## Runs

| Run | Replay pace | sent | clean | match | IN hist | late | Result |
|---|---|---|---|---|---|---|---|
| A — fresh MN database, fresh resource trees | 0.02 s / row | 543 | 543 | 543 | 543 | 0 | PASS |
| B — fresh resource trees, after six burst runs | 0 (burst) | 543 | 543 | 543 | 543 | 0 | PASS |

Label agreement with the synthetic labels was 533/543 in both runs. It is printed for transparency only: the
labels come from the same simulator the model was trained on, so it says nothing about real roads.

## Model lifecycle (cloud → edge)

| Check | Observed |
|---|---|
| `<mlModel>` registered by URL (`mmu`) with SHA-256 label | 201; edge downloaded 6,151,681 B, hash matched |
| `<modelDeployment>` ×3 with `inr`/`our` on the MN (`/EdgeMN/EdgeMN/icing/nodeK/raw` → `.../clean`) | 201; edge subscribed to each `inr` |
| `mcmd=1` (run) | `mds` 0→1, `nrm`=3, edge log `mds 0 -> 1 (running)` |
| `mcmd=0` (stop), then one input row | `mds`=2; edge skipped the row (`skipped_not_running` 1) |
| `mcmd=1` again, then one input row | `mds`=1; row inferred and written; readable from the IN via `~/EdgeMN/EdgeMN/icing/node1/clean/la` |

## Stability under burst load (tinyIoT)

With patches 01–04 applied (see `patches/README.md`), six back-to-back burst runs (543 rows each, no delay)
completed with no MN restart. Real nodes report every 15 minutes; the burst runs are a stress condition, not the
operating point.

## Findings (Mobius4 v4.24.5 ↔ tinyIoT c140495)

| # | Finding | Handling |
|---|---|---|
| 1 | tinyIoT `<remoteCSE>` create carries `dcse`/`ast`/`spi`; Mobius4 v4.24.5 refuses `dcse` | patch 01 |
| 2 | Discovery through MN→IN forwarding fails: tinyIoT drops `ty`, appends `fo=1`; Mobius4 refuses `fo` | edge AE looks deployments up by name; new ones arrive by notification |
| 3 | tinyIoT caps one discovery at 150 results (+1 "more" row) | `e2e_check.py` pages with `ofst` |
| 4 | Notification `con`: string (tinyIoT) vs object (Mobius4); `sur` is a resource ID on tinyIoT | `parse_con()`; `node_id` inside every result |
| 5 | tinyIoT needs `cnf` `application/json:0` and a string `con` (an object is refused: `invalid attribute : con`) | `create_cin()` always serialises |
| 6 | Notifications arrive out of order under load | per-node reorder buffer (0.6 s hold, ordered by measurement time): 0 late rows in the burst runs |
| 7 | `<modelDeployment>` `inr`/`our` can name another CSE's resources | used to deploy the cloud's model onto the edge's containers |
| 8 | Stability under long-running and burst operation | stability patches 02–04 |
