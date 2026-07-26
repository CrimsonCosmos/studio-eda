//! Axis-aligned rectangles: the primitive everything else in this crate
//! operates on.
//!
//! A schematic or a chip layout is, at the geometry level, an enormous pile of
//! axis-aligned boxes (traces, gates, wires, vias). Nothing here needs a general
//! polygon representation, and using one would cost every downstream structure
//! (the grid, the instance buffer) real complexity for a case this crate never
//! has.

use serde::{Deserialize, Serialize};

/// An axis-aligned rectangle in world units, `(x, y)` at the lower-left corner.
#[derive(Debug, Clone, Copy, PartialEq, Serialize, Deserialize)]
pub struct Rect {
    /// Lower-left x.
    pub x: f32,
    /// Lower-left y.
    pub y: f32,
    /// Width. Non-negative.
    pub w: f32,
    /// Height. Non-negative.
    pub h: f32,
}

impl Rect {
    /// Build a rectangle.
    pub fn new(x: f32, y: f32, w: f32, h: f32) -> Self {
        Rect { x, y, w, h }
    }

    /// Whether two rectangles' interiors intersect.
    ///
    /// Strict inequalities: two rectangles that merely touch at an edge do not
    /// overlap. That matters at the grid-cell scale — adjacent cells share a
    /// boundary, and treating "touching" as "overlapping" would make every rect
    /// exactly on a cell edge claim membership in both, doubling query cost for
    /// no benefit.
    pub fn overlaps(&self, other: &Rect) -> bool {
        self.x < other.x + other.w
            && other.x < self.x + self.w
            && self.y < other.y + other.h
            && other.y < self.y + self.h
    }

    /// Right edge.
    pub fn right(&self) -> f32 {
        self.x + self.w
    }
    /// Top edge.
    pub fn top(&self) -> f32 {
        self.y + self.h
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn identical_rects_overlap() {
        let a = Rect::new(0.0, 0.0, 10.0, 10.0);
        assert!(a.overlaps(&a));
    }

    #[test]
    fn disjoint_rects_do_not_overlap() {
        let a = Rect::new(0.0, 0.0, 10.0, 10.0);
        let b = Rect::new(20.0, 20.0, 5.0, 5.0);
        assert!(!a.overlaps(&b));
        assert!(!b.overlaps(&a));
    }

    #[test]
    fn touching_edges_do_not_count_as_overlapping() {
        let a = Rect::new(0.0, 0.0, 10.0, 10.0);
        let b = Rect::new(10.0, 0.0, 10.0, 10.0); // shares the x=10 edge exactly
        assert!(!a.overlaps(&b));
    }

    #[test]
    fn a_rect_that_contains_another_overlaps() {
        let big = Rect::new(0.0, 0.0, 100.0, 100.0);
        let small = Rect::new(40.0, 40.0, 5.0, 5.0);
        assert!(big.overlaps(&small));
        assert!(small.overlaps(&big));
    }

    #[test]
    fn overlap_is_symmetric() {
        let a = Rect::new(3.0, 3.0, 10.0, 10.0);
        let b = Rect::new(5.0, 5.0, 2.0, 2.0);
        assert_eq!(a.overlaps(&b), b.overlaps(&a));
    }
}
