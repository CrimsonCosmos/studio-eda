//! A deterministic, procedurally-generated layout for benchmarking.
//!
//! This has no relationship to any real chip design — it is `count` rectangles
//! tiled across a roughly-square world at a fixed pitch with a little jittered
//! size, which is enough structure to be a fair stand-in for "a very large pile
//! of similarly-sized shapes" without needing (or being allowed to depend on)
//! real GDSII geometry. Deterministic so a benchmark run is reproducible: same
//! `seed`, same layout, byte for byte.

/// SplitMix64 finaliser, used here purely as a hash — not cryptographic, just
/// enough avalanche that adjacent indices don't produce visibly correlated
/// jitter.
fn mix64(mut z: u64) -> u64 {
    z = z.wrapping_add(0x9E37_79B9_7F4A_7C15);
    z = (z ^ (z >> 30)).wrapping_mul(0xBF58_476D_1CE4_E5B9);
    z = (z ^ (z >> 27)).wrapping_mul(0x94D0_49BB_1331_11EB);
    z ^ (z >> 31)
}

use crate::geometry::Rect;

/// Generate `count` rectangles on a roughly square tiling, deterministically
/// jittered by `seed`.
pub fn synthetic_layout(count: usize, seed: u64) -> Vec<Rect> {
    if count == 0 {
        return Vec::new();
    }
    const PITCH: f32 = 10.0;
    let cols = (count as f64).sqrt().ceil() as usize;

    (0..count)
        .map(|i| {
            let col = (i % cols) as f32;
            let row = (i / cols) as f32;
            let h1 = mix64(seed ^ (i as u64));
            let w = 4.0 + ((h1 & 0xff) as f32 / 255.0) * 3.0;
            let h = 4.0 + (((h1 >> 8) & 0xff) as f32 / 255.0) * 3.0;
            Rect::new(col * PITCH, row * PITCH, w, h)
        })
        .collect()
}

/// World-space bounds `count` rectangles from [`synthetic_layout`] occupy.
pub fn synthetic_extent(count: usize) -> (f32, f32) {
    if count == 0 {
        return (0.0, 0.0);
    }
    let cols = (count as f64).sqrt().ceil() as f32;
    let rows = (count as f64 / cols as f64).ceil() as f32;
    (cols * 10.0, rows * 10.0)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn same_seed_produces_byte_identical_layouts() {
        let a = synthetic_layout(1_000, 42);
        let b = synthetic_layout(1_000, 42);
        assert_eq!(a, b);
    }

    #[test]
    fn different_seeds_produce_different_jitter() {
        let a = synthetic_layout(1_000, 1);
        let b = synthetic_layout(1_000, 2);
        assert_ne!(a, b);
    }

    #[test]
    fn produces_exactly_the_requested_count() {
        assert_eq!(synthetic_layout(12_345, 7).len(), 12_345);
        assert_eq!(synthetic_layout(0, 7).len(), 0);
    }

    #[test]
    fn every_rectangle_has_a_sane_positive_size() {
        for r in synthetic_layout(5_000, 11) {
            assert!(r.w > 0.0 && r.w < 10.0);
            assert!(r.h > 0.0 && r.h < 10.0);
        }
    }

    #[test]
    fn the_layout_stays_within_its_reported_extent() {
        let count = 8_000;
        let rects = synthetic_layout(count, 3);
        let (w, h) = synthetic_extent(count);
        for r in &rects {
            assert!(r.right() <= w + 1.0, "{r:?} exceeds width {w}");
            assert!(r.top() <= h + 1.0, "{r:?} exceeds height {h}");
        }
    }
}
