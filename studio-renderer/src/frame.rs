//! Building one frame's worth of draw instances: the naive path and the
//! indexed, culled, level-of-detail path it is measured against.
//!
//! # What "fast" means here, and what it deliberately does not
//!
//! The posting's ask is *"you've taken something slow and made it fast, and you
//! can tell us about why it was slow."* Frame time on an actual GPU is real, but
//! it is also noisy, hardware-dependent, and different again in a headless CI
//! run versus a laptop with a display attached — a claim built on it is hard for
//! anyone else to reproduce or trust. What is deterministic, and is the thing
//! spatial indexing, culling and LOD collapsing exist to reduce in the first
//! place, is **how much geometry has to be prepared for the GPU at all**: how
//! many rectangles get walked, and how many instances get emitted into the
//! buffer that's uploaded. That is what [`naive_build`] and [`indexed_build`]
//! measure, on the CPU, with [`std::time::Instant`] — and it is the honest
//! bottleneck: at a few million polygons, building and uploading the instance
//! buffer dominates long before the GPU's own fill rate does.
//!
//! # LOD granularity: per cell, not per rectangle
//!
//! Collapsing detail below a pixel-size threshold could be judged rectangle by
//! rectangle, but then the *emitted* count is still bounded only by how many
//! rectangles are visible — exactly the number LOD is meant to cap. Judging it
//! per grid cell instead means the worst case is bounded by cell count, however
//! densely packed a cell's contents are, which is what actually keeps a frame
//! cheap when a million-transistor block is zoomed out to a few pixels.

use crate::geometry::Rect;
use crate::grid::SpatialGrid;

/// One thing to draw: a rectangle plus a marker for what produced it.
#[derive(Debug, Clone, Copy, PartialEq)]
pub struct Instance {
    /// World-space rectangle.
    pub rect: Rect,
    /// `false` for a real rectangle, `true` for an aggregate standing in for an
    /// entire below-threshold grid cell.
    pub is_aggregate: bool,
}

/// What a frame build cost, independent of what it produced.
///
/// Deliberately carries no timing of its own. `std::time::Instant` panics on
/// `wasm32-unknown-unknown` without an extra shim crate, and this crate's whole
/// premise is being plain, portable CPU geometry — pulling in a wasm-specific
/// timer just so the library could time itself would be exactly the kind of
/// platform dependency it exists to avoid. Measuring wall time around a call to
/// [`naive_build`] or [`indexed_build`] is the caller's job: a native benchmark
/// uses `std::time::Instant` (see `examples/perf_report.rs`), a browser caller
/// uses `performance.now()`.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct FrameStats {
    /// Rectangles (or cell buckets) the algorithm had to look at at all.
    pub considered: usize,
    /// Instances actually emitted for drawing.
    pub emitted: usize,
}

/// The simplest thing anyone would write with no spatial structure: walk every
/// rectangle, keep the ones inside the viewport.
///
/// This is the baseline, not a strawman — it is exactly what `/fab`'s existing
/// `MaskEditor.tsx` does with SVG, and what a first draft of this renderer would
/// do too. `considered` is always `rects.len()`, by definition: nothing here
/// has a way to skip a rectangle without first looking at it.
pub fn naive_build(rects: &[Rect], viewport: Rect) -> (Vec<Instance>, FrameStats) {
    let mut out = Vec::new();
    for r in rects {
        if r.overlaps(&viewport) {
            out.push(Instance {
                rect: *r,
                is_aggregate: false,
            });
        }
    }
    let stats = FrameStats {
        considered: rects.len(),
        emitted: out.len(),
    };
    (out, stats)
}

/// Build a frame using the grid for culling and per-cell collapsing for LOD.
///
/// `scale` is world-units-per-pixel⁻¹ (screen pixels per world unit) at the
/// current zoom; `lod_pixel_threshold` is the on-screen cell size, in pixels,
/// below which a whole cell's contents collapse into one aggregate instance
/// instead of being emitted individually.
pub fn indexed_build(
    grid: &SpatialGrid,
    rects: &[Rect],
    viewport: Rect,
    scale: f32,
    lod_pixel_threshold: f32,
) -> (Vec<Instance>, FrameStats) {
    let mut out = Vec::new();
    let mut considered = 0usize;
    // A rect can be bucketed into more than one cell; without this, one on a
    // cell boundary within the viewport would be emitted twice.
    let mut emitted_ids: Vec<bool> = vec![false; rects.len()];

    for cell_idx in grid.cells_overlapping(viewport) {
        let cell_rect = grid.cell_rect(cell_idx);
        let ids = grid.cell_contents(cell_idx);
        let screen_w = cell_rect.w * scale;

        if screen_w < lod_pixel_threshold {
            // Below threshold: the whole cell becomes at most one instance,
            // however many rectangles it actually holds — that bound is the
            // entire point of judging LOD per cell rather than per rectangle.
            considered += ids.len();
            if !ids.is_empty() {
                out.push(Instance {
                    rect: cell_rect,
                    is_aggregate: true,
                });
            }
            continue;
        }

        for &id in ids {
            considered += 1;
            if emitted_ids[id as usize] {
                continue;
            }
            let r = rects[id as usize];
            if r.overlaps(&viewport) {
                emitted_ids[id as usize] = true;
                out.push(Instance {
                    rect: r,
                    is_aggregate: false,
                });
            }
        }
    }

    let stats = FrameStats {
        considered,
        emitted: out.len(),
    };
    (out, stats)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::synthetic::synthetic_layout;

    #[test]
    fn naive_considers_every_rectangle_no_matter_the_viewport() {
        let rects = synthetic_layout(500, 1);
        let (_, stats) = naive_build(&rects, Rect::new(0.0, 0.0, 5.0, 5.0));
        assert_eq!(stats.considered, 500);
    }

    #[test]
    fn with_lod_disabled_indexed_and_naive_emit_the_same_rectangles() {
        // A threshold of 0 never collapses anything, so the two paths should
        // agree exactly — proof indexed_build isn't silently dropping or
        // duplicating geometry, before any LOD reduction is layered on.
        let rects = synthetic_layout(2_000, 7);
        let grid = SpatialGrid::build(&rects, 40.0);
        let viewport = Rect::new(50.0, 50.0, 80.0, 80.0);

        let (naive, _) = naive_build(&rects, viewport);
        let (indexed, _) = indexed_build(&grid, &rects, viewport, 1.0, 0.0);

        let mut naive_sorted: Vec<Rect> = naive.iter().map(|i| i.rect).collect();
        let mut indexed_sorted: Vec<Rect> = indexed.iter().map(|i| i.rect).collect();
        let key = |r: &Rect| (r.x.to_bits(), r.y.to_bits());
        naive_sorted.sort_by_key(key);
        indexed_sorted.sort_by_key(key);
        assert_eq!(naive_sorted.len(), indexed_sorted.len());
        assert_eq!(naive_sorted, indexed_sorted);
        assert!(indexed.iter().all(|i| !i.is_aggregate));
    }

    #[test]
    fn zoomed_out_far_enough_lod_collapses_a_dense_region_to_near_cell_count() {
        // 50,000 rects packed into a small area, viewed at a scale where each
        // grid cell is under a pixel wide: LOD must cap emission near the cell
        // count, not the rectangle count.
        let rects = synthetic_layout(50_000, 3);
        let grid = SpatialGrid::build(&rects, 40.0);
        let world = Rect::new(0.0, 0.0, 100_000.0, 100_000.0);
        let (out, stats) = indexed_build(&grid, &rects, world, 0.01, 4.0);

        assert!(
            out.len() < grid.cell_count() * 2,
            "emitted {} instances against {} cells",
            out.len(),
            grid.cell_count()
        );
        assert!(out.len() < rects.len() / 10, "LOD should collapse the vast majority");
        assert!(out.iter().all(|i| i.is_aggregate), "everything should be below threshold");
        assert!(stats.considered <= rects.len());
    }

    #[test]
    fn zoomed_in_close_lod_never_engages() {
        let rects = synthetic_layout(500, 4);
        let grid = SpatialGrid::build(&rects, 40.0);
        let viewport = Rect::new(0.0, 0.0, 60.0, 60.0);
        // A huge scale means every cell is far wider than the threshold on
        // screen, so nothing should collapse.
        let (out, _) = indexed_build(&grid, &rects, viewport, 1000.0, 4.0);
        assert!(!out.is_empty());
        assert!(out.iter().all(|i| !i.is_aggregate));
    }

    #[test]
    fn indexed_considers_far_fewer_rectangles_than_naive_at_scale() {
        // The actual algorithmic claim, as a deterministic count rather than a
        // timing measurement: looking only at cells the viewport touches must
        // examine far fewer candidates than a full scan, once the layout is
        // large and the viewport is a small fraction of it.
        let rects = synthetic_layout(200_000, 5);
        let grid = SpatialGrid::build(&rects, 40.0);
        // A viewport covering roughly 1% of the layout's extent.
        let viewport = Rect::new(0.0, 0.0, 450.0, 450.0);

        let (_, naive_stats) = naive_build(&rects, viewport);
        let (_, indexed_stats) = indexed_build(&grid, &rects, viewport, 1.0, 0.0);

        assert_eq!(naive_stats.considered, 200_000);
        assert!(
            indexed_stats.considered < naive_stats.considered / 50,
            "indexed considered {} of {}",
            indexed_stats.considered,
            naive_stats.considered
        );
        assert_eq!(
            indexed_stats.emitted, naive_stats.emitted,
            "restricting to nearby cells must not change what's actually visible"
        );
    }

    #[test]
    fn indexed_build_is_meaningfully_faster_at_scale() {
        // A generous sanity floor, not the headline number — see
        // docs/studio-renderer-perf.md for the real measured numbers from
        // `cargo run --release --example perf_report`. Timed here, around the
        // library calls, rather than inside FrameStats — see that struct's docs
        // for why the library itself stays timer-free. Wall-clock assertions
        // need enough margin to survive a loaded machine; 3x is comfortably
        // inside what the count-based test above guarantees algorithmically.
        use std::time::Instant;
        let rects = synthetic_layout(1_000_000, 9);
        let grid = SpatialGrid::build(&rects, 40.0);
        let viewport = Rect::new(0.0, 0.0, 1000.0, 1000.0);

        let naive_start = Instant::now();
        naive_build(&rects, viewport);
        let naive_time = naive_start.elapsed();

        let indexed_start = Instant::now();
        indexed_build(&grid, &rects, viewport, 1.0, 0.0);
        let indexed_time = indexed_start.elapsed();

        assert!(
            indexed_time.as_secs_f64() * 3.0 < naive_time.as_secs_f64(),
            "naive {naive_time:?} vs indexed {indexed_time:?}"
        );
    }
}
