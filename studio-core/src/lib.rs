//! Studio: schematic capture and circuit simulation for the browser.
//!
//! The design half of the design→fab loop. `fab-control` next door simulates
//! running the factory; this simulates the circuit that gets sent to it, and the
//! two meet at [`netlist::MosParams::from_process`] — a transistor drawn here
//! takes its threshold voltage and gain from the gate oxide the line actually
//! grew, so a furnace running cold shows up as a shifted operating point rather
//! than as a datasheet number.
pub mod analysis;
pub mod crdt;
pub mod matrix;
pub mod netlist;

pub use analysis::{solve_ac, solve_dc, AcSample, OperatingPoint, SimError, Transient, TransientSample};
pub use crdt::{ComponentFields, ComponentKind, Connection, Document, EntityId, LwwRegister, OpId, OrSet, SiteClock, GROUND_ENTITY};
pub use netlist::{Device, MosKind, MosParams, Netlist, Waveform};
