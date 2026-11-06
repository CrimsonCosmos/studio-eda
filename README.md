# studio-eda

Rust→WASM electronic design automation (EDA) core: schematic capture over a
CRDT document model, a modified-nodal-analysis (MNA) circuit solver, and a
wgpu-based canvas renderer.

Extracted from [ramandu.com](https://www.ramandu.com)'s `/studio` page
(source repo: [wintern-next](https://github.com/CrimsonCosmos/wintern-next)).

- `studio-core` — schematic document model (CRDT), MNA solver, spatial index.
- `studio-renderer` — layout/rendering support, synthetic layout generation.
- `studio-wasm` — `wasm-bindgen` boundary with a wgpu canvas backend
  (webgl/webgpu). wasm32-only — a plain native `cargo build` will fail on
  this crate; build/test it for `wasm32-unknown-unknown` or via `wasm-pack`.

Depends on [`fab-control`](https://github.com/CrimsonCosmos/fab2-control-plane)
(a transistor drawn here inherits the gate oxide the fab simulator's process
output actually grew), pulled in as a git dependency rather than vendored —
see `Cargo.toml`.

## Building the WASM bundle

```
cd studio-wasm
wasm-pack build --target web --release --out-dir ../pkg
```

## Tests

```
cargo test -p studio-core -p studio-renderer   # native
cargo test --target wasm32-unknown-unknown     # full workspace
```


## `forge/` — studioforge: LLM-supervised, deterministic PCB/system design

A Python package that pairs Claude Code with deterministic EDA pipelines: *"build me a humanoid robot, budget $50,000"* →
recursive decomposition (with a buy-vs-build policy) → interface resolver → parametric circuit blocks → placement/autorouting/DRC →
Gerber/drill/BOM/CPL/KiCad files → purchasing lists. See [`forge/README.md`](forge/README.md).
It is independent of the Rust crates above (they could later supply the MNA solver for simulation-in-the-loop checks).
