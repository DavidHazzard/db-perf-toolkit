# Findings

`drop-unused-indexes` applies an 8MB floor, borrowed from Ola Hallengren's `@MinNumberOfPages = 1000`. Running all three scenarios showed where that borrow breaks down.

| | Small | Realistic | Horror |
|---|---|---|---|
| Above floor — would drop | 1 → **58 MB** | 35 → **882 MB** | 33 → **414 MB** |
| Below floor — refused | 1 → 2 MB | 572 → 201 MB | 1,772 → 841 MB |

On the realistic database the floor works exactly as intended: what it surfaces holds 4.4x more than everything it dismisses.

**On the horror database it inverts.** The 1,772 indexes dismissed as too small to bother with hold 2.0x *more* than the 33 it would act on.

## Bytes were the lesser cost

| | Small | Realistic | Horror |
|---|---|---|---|
| Worst table | `orders` | `whale_1` | `orders_2020` |
| Unused / total indexes | 5/6 | 8/9 | 11/12 |
| Redundant index writes | 2,480,000 | 134,382,804 | 142,353,282 |

`orders_2020` carries 11/12 indexes unread. Every `INSERT` pays that many B-tree writes serving no query, whether those indexes are 16kB or 16MB. A per-index size floor is structurally unable to see it.

That is what `index-burden` exists for: it ranks tables by `unused indexes × row modifications` rather than by size. The floor was a sound borrow for *maintenance* — rebuilding a tiny index really is pointless — and the wrong instrument for *dropping*.

## The second blind spot

`bloat` counts dead tuples. After a vacuum those read zero while the file stays exactly as large, because plain `VACUUM` marks space reusable rather than returning it to the OS.

Measured on the horror database **after** the remediation pass:

| Check | Result |
|---|---|
| `bloat` | **0 tables** |
| `free-space` | **31 tables, 1.03 GB reclaimable** |
| Dead tuples on those tables | **0%** |

`free-space` was added to close this. It uses `pgstattuple`, or `pgstattuple_approx` above a size threshold, and reports which was used.
