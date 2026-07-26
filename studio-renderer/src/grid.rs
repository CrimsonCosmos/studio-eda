//! A uniform spatial grid over a fixed set of rectangles.
//!
//! A uniform grid rather than an R-tree or a quadtree: the layouts this crate
//! draws (a schematic, a chip's mask geometry) are close to uniformly dense —
//! standard cells and traces at a roughly constant pitch — so a grid gets an
//! R-tree's query performance without its rebalancing logic, and it is
//! trivially easy to get *correct*, which matters more than a marginal speedup
//! on skewed data this crate doesn't expect to see. Written down as the
//! trade-off it is: pathologically clustered geometry (everything in one
//! corner of a huge empty world) would degrade a uniform grid toward a single
//! overloaded cell, where a quadtree would adapt. That case does not arise from
//! `synthetic_layout` or from real chip geometry.
//!
//! The grid is built once per layout and queried once per frame — geometry is
//! static within a frame; only the viewport moves.

use std::collections::BTreeSet;

use crate::geometry::Rect;

/// A grid of buckets over a fixed rectangle set.
#[derive(Debug, Clone)]
pub struct SpatialGrid {
    origin_x: f32,
    origin_y: f32,
    cell_size: f32,
    cols: usize,
    rows: usize,
    /// One bucket of rect indices per cell, row-major.
    cells: Vec<Vec<u32>>,
}

impl SpatialGrid {
    /// Build a grid over `rects` with the given cell size (world units).
    ///
    /// Cell size is the one real tuning knob: too small and a rect spanning
    /// several cells gets duplicated into each of them (more bookkeeping, not
    /// wrong, just wasteful); too large and a query pulls in a bucket far
    /// bigger than the viewport that asked for it. A cell holding on the order
    /// of ten rects is the usual sweet spot, which is why callers size it off
    /// the layout's own pitch rather than a fixed constant.
    pub fn build(rects: &[Rect], cell_size: f32) -> Self {
        assert!(cell_size > 0.0, "cell_size must be positive");
        if rects.is_empty() {
            return SpatialGrid {
                origin_x: 0.0,
                origin_y: 0.0,
                cell_size,
                cols: 0,
                rows: 0,
                cells: Vec::new(),
            };
        }

        let min_x = rects.iter().map(|r| r.x).fold(f32::INFINITY, f32::min);
        let min_y = rects.iter().map(|r| r.y).fold(f32::INFINITY, f32::min);
        let max_x = rects.iter().map(|r| r.right()).fold(f32::NEG_INFINITY, f32::max);
        let max_y = rects.iter().map(|r| r.top()).fold(f32::NEG_INFINITY, f32::max);

        let cols = (((max_x - min_x) / cell_size).ceil() as usize).max(1);
        let rows = (((max_y - min_y) / cell_size).ceil() as usize).max(1);

        let mut cells = vec![Vec::new(); cols * rows];
        let mut grid = SpatialGrid {
            origin_x: min_x,
            origin_y: min_y,
            cell_size,
            cols,
            rows,
            cells: Vec::new(),
        };
        for (i, rect) in rects.iter().enumerate() {
            let (c0, r0, c1, r1) = grid.cell_range(*rect);
            for row in r0..=r1 {
                for col in c0..=c1 {
                    cells[row * cols + col].push(i as u32);
                }
            }
        }
        grid.cells = cells;
        grid
    }

    /// Total cells.
    pub fn cell_count(&self) -> usize {
        self.cells.len()
    }

    /// World-space bounds of one cell, by row-major index.
    pub fn cell_rect(&self, index: usize) -> Rect {
        let col = (index % self.cols.max(1)) as f32;
        let row = (index / self.cols.max(1)) as f32;
        Rect::new(
            self.origin_x + col * self.cell_size,
            self.origin_y + row * self.cell_size,
            self.cell_size,
            self.cell_size,
        )
    }

    /// Rect indices bucketed into cell `index`. Empty for an out-of-range index.
    pub fn cell_contents(&self, index: usize) -> &[u32] {
        self.cells.get(index).map(Vec::as_slice).unwrap_or(&[])
    }

    /// Column/row range a rectangle's bounding box spans, clamped to the grid.
    fn cell_range(&self, r: Rect) -> (usize, usize, usize, usize) {
        let to_col = |x: f32| {
            (((x - self.origin_x) / self.cell_size).floor().max(0.0) as usize).min(self.cols - 1)
        };
        let to_row = |y: f32| {
            (((y - self.origin_y) / self.cell_size).floor().max(0.0) as usize).min(self.rows - 1)
        };
        (to_col(r.x), to_row(r.y), to_col(r.right()), to_row(r.top()))
    }

    /// Row-major indices of every cell whose bounds overlap `viewport`.
    ///
    /// This is the whole benefit of the grid: it answers "which regions might
    /// matter" from the grid's own geometry, in time proportional to the
    /// viewport's size, without touching the rect list — the O(N) part of a
    /// naive scan never happens.
    pub fn cells_overlapping(&self, viewport: Rect) -> Vec<usize> {
        if self.cols == 0 || self.rows == 0 {
            return Vec::new();
        }
        let to_col = |x: f32| {
            (((x - self.origin_x) / self.cell_size).floor() as isize)
                .clamp(0, self.cols as isize - 1) as usize
        };
        let to_row = |y: f32| {
            (((y - self.origin_y) / self.cell_size).floor() as isize)
                .clamp(0, self.rows as isize - 1) as usize
        };
        // A viewport entirely outside the grid still clamps into range; guard
        // with an explicit overlap test on each candidate cell below rather
        // than trusting the clamped range alone.
        let c0 = to_col(viewport.x);
        let c1 = to_col(viewport.right());
        let r0 = to_row(viewport.y);
        let r1 = to_row(viewport.top());

        let mut out = Vec::new();
        for row in r0..=r1 {
            for col in c0..=c1 {
                let idx = row * self.cols + col;
                if self.cell_rect(idx).overlaps(&viewport) {
                    out.push(idx);
                }
            }
        }
        out
    }

    /// Every rect index that actually overlaps `viewport`, exactly.
    ///
    /// `cells_overlapping` narrows the search to a handful of buckets, but a
    /// bucket's contents are only candidates — a rect bucketed into a cell the
    /// viewport touches can still sit in the part of that cell the viewport
    /// doesn't reach, and a rect spanning several cells is bucketed into all of
    /// them, so a naive union would also report it once per cell it happens to
    /// share with the query. This does the final exact check and dedup, so the
    /// result is provably the same *set* a full linear scan would produce —
    /// which is the whole value of this method: it exists to let that
    /// equivalence be tested, before any LOD collapsing (which trades exactness
    /// for speed on purpose) enters the picture.
    pub fn query(&self, rects: &[Rect], viewport: Rect) -> BTreeSet<u32> {
        let mut out = BTreeSet::new();
        for idx in self.cells_overlapping(viewport) {
            for &id in self.cell_contents(idx) {
                if rects[id as usize].overlaps(&viewport) {
                    out.insert(id);
                }
            }
        }
        out
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn brute_force(rects: &[Rect], viewport: Rect) -> BTreeSet<u32> {
        rects
            .iter()
            .enumerate()
            .filter(|(_, r)| r.overlaps(&viewport))
            .map(|(i, _)| i as u32)
            .collect()
    }

    #[test]
    fn query_matches_a_brute_force_scan() {
        let rects: Vec<Rect> = (0..200)
            .map(|i| Rect::new((i % 20) as f32 * 5.0, (i / 20) as f32 * 5.0, 3.0, 3.0))
            .collect();
        let grid = SpatialGrid::build(&rects, 15.0);
        for viewport in [
            Rect::new(0.0, 0.0, 20.0, 20.0),
            Rect::new(30.0, 10.0, 15.0, 15.0),
            Rect::new(-100.0, -100.0, 5.0, 5.0), // entirely outside
            Rect::new(0.0, 0.0, 1000.0, 1000.0), // covers everything
        ] {
            assert_eq!(grid.query(&rects, viewport), brute_force(&rects, viewport), "{viewport:?}");
        }
    }

    #[test]
    fn a_rect_spanning_several_cells_is_found_from_any_of_them_without_duplication() {
        let rects = vec![Rect::new(0.0, 0.0, 40.0, 5.0)]; // spans ~4 cells at size 10
        let grid = SpatialGrid::build(&rects, 10.0);
        assert!(grid.cell_count() >= 4);
        // A viewport touching only the far end must still find it, exactly once.
        let far = grid.query(&rects, Rect::new(35.0, 0.0, 2.0, 2.0));
        assert_eq!(far, BTreeSet::from([0]));
        let near = grid.query(&rects, Rect::new(0.0, 0.0, 2.0, 2.0));
        assert_eq!(near, BTreeSet::from([0]));
    }

    #[test]
    fn a_query_outside_every_cell_finds_nothing() {
        let rects = vec![Rect::new(0.0, 0.0, 5.0, 5.0)];
        let grid = SpatialGrid::build(&rects, 10.0);
        assert!(grid.query(&rects, Rect::new(1000.0, 1000.0, 1.0, 1.0)).is_empty());
    }

    #[test]
    fn an_empty_layout_builds_an_empty_grid_without_panicking() {
        let grid = SpatialGrid::build(&[], 10.0);
        assert_eq!(grid.cell_count(), 0);
        assert!(grid.query(&[], Rect::new(0.0, 0.0, 10.0, 10.0)).is_empty());
    }

    #[test]
    fn cells_overlapping_only_returns_cells_that_actually_overlap() {
        let rects: Vec<Rect> = (0..25)
            .map(|i| Rect::new((i % 5) as f32 * 10.0, (i / 5) as f32 * 10.0, 8.0, 8.0))
            .collect();
        let grid = SpatialGrid::build(&rects, 10.0);
        for idx in grid.cells_overlapping(Rect::new(0.0, 0.0, 22.0, 22.0)) {
            assert!(grid.cell_rect(idx).overlaps(&Rect::new(0.0, 0.0, 22.0, 22.0)));
        }
    }

    #[test]
    fn a_cell_candidate_that_does_not_actually_overlap_is_excluded() {
        // The bug this guards: a rect bucketed into a cell the viewport merely
        // touches is only a *candidate* — cell_contents can include it even
        // when the rect itself sits outside the viewport, and query() must
        // filter that back out.
        let rects = vec![
            Rect::new(0.0, 0.0, 3.0, 3.0),  // near the query
            Rect::new(20.0, 0.0, 3.0, 3.0), // shares a cell with the above at
                                             // cell_size 15, but is far outside
        ];
        let grid = SpatialGrid::build(&rects, 15.0);
        let hit = grid.query(&rects, Rect::new(0.0, 0.0, 4.0, 4.0));
        assert_eq!(hit, BTreeSet::from([0]), "the far rect must not appear");
    }

    #[test]
    #[should_panic(expected = "cell_size must be positive")]
    fn zero_cell_size_is_rejected() {
        SpatialGrid::build(&[Rect::new(0.0, 0.0, 1.0, 1.0)], 0.0);
    }
}
