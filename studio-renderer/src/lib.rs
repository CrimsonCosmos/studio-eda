//! Spatial indexing, viewport culling and level-of-detail for drawing very
//! large layouts — the algorithmic core of Studio's canvas.
//!
//! This crate is deliberately GPU-free. Everything here — the grid, culling,
//! LOD collapsing, the [`frame::Instance`] list a frame produces — is plain
//! CPU geometry, testable with `cargo test` and no browser. The wgpu surface
//! that actually uploads an [`frame::Instance`] buffer and draws it lives with
//! the `/studio` page, once there is a real canvas for it to own; see
//! `docs/wgpu-wasm-findings.md` for what that migration already cost.
pub mod frame;
pub mod geometry;
pub mod grid;
pub mod synthetic;

pub use frame::{indexed_build, naive_build, FrameStats, Instance};
pub use geometry::Rect;
pub use grid::SpatialGrid;
pub use synthetic::{synthetic_extent, synthetic_layout};
