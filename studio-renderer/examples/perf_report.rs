//! Prints the naive-vs-indexed comparison this crate's perf claim is based on.
//!
//! `cargo run --release --example perf_report -p studio-renderer`
//!
//! Run in release: the whole point is measuring CPU cost, and an unoptimised
//! debug build's constant factors would swamp the algorithmic difference this
//! is trying to show.

use std::time::{Duration, Instant};

use studio_renderer::{indexed_build, naive_build, synthetic_extent, synthetic_layout, SpatialGrid};

fn fmt(d: Duration) -> String {
    if d.as_millis() > 0 {
        format!("{:.2} ms", d.as_secs_f64() * 1e3)
    } else {
        format!("{} µs", d.as_micros())
    }
}

fn main() {
    println!(
        "{:>10} | {:>12} {:>10} {:>10} | {:>12} {:>10} {:>10} | {:>8}",
        "N", "naive cons.", "emitted", "time", "indexed cons.", "emitted", "time", "speedup"
    );
    println!("{}", "-".repeat(96));

    for &n in &[10_000usize, 100_000, 1_000_000, 5_000_000] {
        let rects = synthetic_layout(n, 42);
        let (world_w, world_h) = synthetic_extent(n);
        let grid = SpatialGrid::build(&rects, 40.0);

        // A viewport covering ~2% of the world's area -- "the user has zoomed
        // into one corner of a very large layout," which is the scenario this
        // exists for.
        let side = (world_w.min(world_h)) * 0.02f32.sqrt();
        let viewport = studio_renderer::Rect::new(0.0, 0.0, side, side);

        let t0 = Instant::now();
        let (_, naive) = naive_build(&rects, viewport);
        let naive_time = t0.elapsed();

        let t1 = Instant::now();
        let (_, indexed) = indexed_build(&grid, &rects, viewport, 1.0, 0.0);
        let indexed_time = t1.elapsed();

        let speedup = naive_time.as_secs_f64() / indexed_time.as_secs_f64().max(1e-12);
        println!(
            "{n:>10} | {:>12} {:>10} {:>10} | {:>12} {:>10} {:>10} | {:>7.1}x",
            naive.considered,
            naive.emitted,
            fmt(naive_time),
            indexed.considered,
            indexed.emitted,
            fmt(indexed_time),
            speedup,
        );
    }

    println!();
    println!("With LOD collapsing a distant view of a dense layout:");
    let n = 2_000_000;
    let rects = synthetic_layout(n, 42);
    let grid = SpatialGrid::build(&rects, 40.0);
    let (world_w, world_h) = synthetic_extent(n);
    let whole_world = studio_renderer::Rect::new(0.0, 0.0, world_w, world_h);
    let t2 = Instant::now();
    let (out, _stats) = indexed_build(&grid, &rects, whole_world, 0.02, 3.0);
    let lod_time = t2.elapsed();
    println!(
        "  N={n}  cells={}  emitted={} (vs {n} rects, {:.1}x fewer)  time={}",
        grid.cell_count(),
        out.len(),
        n as f64 / out.len().max(1) as f64,
        fmt(lod_time)
    );
}
