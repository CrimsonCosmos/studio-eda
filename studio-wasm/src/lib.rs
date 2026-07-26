//! `wasm-bindgen` boundary for Studio: the CRDT document, the circuit solver,
//! and a live GPU canvas over `studio-renderer`'s indexed/culled/LOD path.
//!
//! Two halves, following the same split as `fab-control-wasm` next door:
//! everything through [`Schematic`] and [`TransientHandle`] forwards straight
//! into `studio-core` — no computation happens here that the native test suite
//! doesn't already cover. [`GpuCanvas`] is different in kind: it is the actual
//! wgpu surface, built exactly to the shape the spike proved out (see
//! `docs/wgpu-wasm-findings.md` — wgpu 30, the WebGL2 fallback, `queue.present`
//! rather than `frame.present`), because unlike the CRDT or the solver there is
//! no native equivalent of "draws a canvas" to test against; the browser is the
//! only place this code path exists at all.

use fab_control::wafer::threshold_voltage;
use serde::{Deserialize, Serialize};
use studio_core::analysis::{log_sweep, solve_ac, solve_dc};
use studio_core::crdt::{ComponentKind, Document, OpId, SiteClock};
use studio_core::netlist::MosKind;
use studio_core::Transient;
use wasm_bindgen::prelude::*;

mod render;
pub use render::{create_gpu_canvas, GpuCanvas};

#[wasm_bindgen(start)]
pub fn init_panic_hook() {
    console_error_panic_hook::set_once();
}

fn to_js<T: Serialize + ?Sized>(value: &T) -> Result<JsValue, JsValue> {
    serde_wasm_bindgen::to_value(value).map_err(|e| JsValue::from_str(&e.to_string()))
}

fn from_js<T: for<'de> Deserialize<'de>>(value: JsValue) -> Result<T, JsValue> {
    serde_wasm_bindgen::from_value(value).map_err(|e| JsValue::from_str(&e.to_string()))
}

fn parse_kind(s: &str) -> Result<ComponentKind, JsValue> {
    Ok(match s {
        "R" => ComponentKind::Resistor,
        "C" => ComponentKind::Capacitor,
        "L" => ComponentKind::Inductor,
        "V" => ComponentKind::VoltageSource,
        "I" => ComponentKind::CurrentSource,
        "D" => ComponentKind::Diode,
        "NMOS" => ComponentKind::Nmos,
        "PMOS" => ComponentKind::Pmos,
        other => return Err(JsValue::from_str(&format!("unknown component kind '{other}'"))),
    })
}

// ---------------------------------------------------------------------------
// Schematic (Document + SiteClock)
// ---------------------------------------------------------------------------

/// One replica's view of a schematic: the CRDT document plus the clock that
/// stamps this replica's own edits.
#[wasm_bindgen]
pub struct Schematic {
    doc: Document,
    clock: SiteClock,
}

#[wasm_bindgen]
impl Schematic {
    /// A new, empty schematic. `site` must be unique among every replica this
    /// one might ever merge with — see [`studio_core::crdt::EntityId`]'s docs
    /// for the recommended id scheme, which uses the same site id.
    #[wasm_bindgen(constructor)]
    pub fn new(site: u64) -> Schematic {
        Schematic {
            doc: Document::new(),
            clock: SiteClock::new(site),
        }
    }

    /// Place a component. `kind` is one of `"R" "C" "L" "V" "I" "D" "NMOS"
    /// "PMOS"`; `params` meaning is documented on [`ComponentKind`].
    #[wasm_bindgen(js_name = addComponent)]
    pub fn add_component(
        &mut self,
        id: u64,
        kind: &str,
        name: &str,
        x: f64,
        y: f64,
        params: Vec<f64>,
    ) -> Result<(), JsValue> {
        let kind = parse_kind(kind)?;
        self.doc.add_component(id, kind, name, x, y, params, &mut self.clock);
        Ok(())
    }

    /// Remove a component.
    #[wasm_bindgen(js_name = removeComponent)]
    pub fn remove_component(&mut self, id: u64) {
        self.doc.remove_component(id, &mut self.clock);
    }

    /// Move a component.
    #[wasm_bindgen(js_name = moveComponent)]
    pub fn move_component(&mut self, id: u64, x: f64, y: f64) {
        self.doc.move_component(id, x, y, &mut self.clock);
    }

    /// Replace a component's device parameters.
    #[wasm_bindgen(js_name = setParams)]
    pub fn set_params(&mut self, id: u64, params: Vec<f64>) {
        self.doc.set_params(id, params, &mut self.clock);
    }

    /// Rename a component.
    pub fn rename(&mut self, id: u64, name: &str) {
        self.doc.rename(id, name, &mut self.clock);
    }

    /// Wire two terminals.
    pub fn connect(&mut self, a_id: u64, a_pin: u8, b_id: u64, b_pin: u8) {
        self.doc.connect((a_id, a_pin), (b_id, b_pin), &mut self.clock);
    }

    /// Remove a wire.
    pub fn disconnect(&mut self, a_id: u64, a_pin: u8, b_id: u64, b_pin: u8) {
        self.doc.disconnect((a_id, a_pin), (b_id, b_pin), &mut self.clock);
    }

    /// The full CRDT state, for merging into another replica or persisting.
    pub fn snapshot(&self) -> Result<JsValue, JsValue> {
        to_js(&self.doc)
    }

    /// Deterministic text projection — see [`Document::to_text`].
    #[wasm_bindgen(js_name = toText)]
    pub fn to_text(&self) -> String {
        self.doc.to_text()
    }

    /// Merge another replica's [`Schematic::snapshot`] into this one.
    ///
    /// Advances this replica's clock past everything just merged in, so the
    /// next local edit is provably causally later than anything the other
    /// replica had — the same guarantee [`SiteClock::observe`] gives a single
    /// received op, applied to a whole merged document at once.
    #[wasm_bindgen(js_name = mergeFrom)]
    pub fn merge_from(&mut self, snapshot: JsValue) -> Result<(), JsValue> {
        let other: Document = from_js(snapshot)?;
        self.doc.merge(&other);
        self.clock.observe(OpId {
            counter: self.doc.max_counter(),
            site: 0,
        });
        Ok(())
    }

    /// Every live component, in a plain shape — not the raw CRDT internals.
    ///
    /// `snapshot()`/`merge_from` are deliberately the only place this crate
    /// exposes the CRDT's own representation, and even there it stays opaque:
    /// JS only ever hands one `Schematic`'s snapshot to another's
    /// `mergeFrom`, never inspects it. `serde-wasm-bindgen` represents the
    /// underlying `BTreeMap`/`BTreeSet` internals as JS `Map`/`Set` (and a
    /// struct used as a map key comes through as a plain object *value*,
    /// which cannot be looked up by `.get()` the way a primitive key can) —
    /// exactly the kind of representation detail a UI has no business
    /// depending on. This method and [`Schematic::wires`] are the real read
    /// path: plain arrays of plain records.
    pub fn components(&self) -> Result<JsValue, JsValue> {
        #[derive(Serialize)]
        struct ComponentSummary {
            id: u64,
            kind: String,
            name: String,
            x: f64,
            y: f64,
            params: Vec<f64>,
        }
        let out: Vec<ComponentSummary> = self
            .doc
            .live_components()
            .map(|(id, f)| ComponentSummary {
                id,
                kind: format!("{:?}", f.kind.value),
                name: f.name.value.clone(),
                x: f.x.value,
                y: f.y.value,
                params: f.params.value.clone(),
            })
            .collect();
        to_js(&out)
    }

    /// Every wire, in a plain shape. See [`Schematic::components`].
    pub fn wires(&self) -> Result<JsValue, JsValue> {
        #[derive(Serialize)]
        struct WireSummary {
            a_id: u64,
            a_pin: u8,
            b_id: u64,
            b_pin: u8,
        }
        let out: Vec<WireSummary> = self
            .doc
            .connections
            .values()
            .map(|c| WireSummary {
                a_id: c.a.0,
                a_pin: c.a.1,
                b_id: c.b.0,
                b_pin: c.b.1,
            })
            .collect();
        to_js(&out)
    }

    /// Solve the DC operating point. Returns node voltages, names, and branch
    /// currents.
    #[wasm_bindgen(js_name = solveDc)]
    pub fn solve_dc(&self) -> Result<JsValue, JsValue> {
        let net = self.doc.to_netlist().map_err(|e| JsValue::from_str(&e))?;
        let op = solve_dc(&net).map_err(|e| JsValue::from_str(&e.to_string()))?;
        #[derive(Serialize)]
        struct DcResult {
            voltages: Vec<f64>,
            node_names: Vec<String>,
            branch_currents: Vec<f64>,
        }
        to_js(&DcResult {
            voltages: op.voltages,
            node_names: net.node_names,
            branch_currents: op.branch_currents,
        })
    }

    /// Sweep AC response from `start_hz` to `stop_hz`, `points_per_decade`
    /// points per decade, linearised about the DC operating point.
    #[wasm_bindgen(js_name = acSweep)]
    pub fn ac_sweep(
        &self,
        start_hz: f64,
        stop_hz: f64,
        points_per_decade: usize,
    ) -> Result<JsValue, JsValue> {
        let net = self.doc.to_netlist().map_err(|e| JsValue::from_str(&e))?;
        let op = solve_dc(&net).map_err(|e| JsValue::from_str(&e.to_string()))?;
        let mut out = Vec::new();
        for f in log_sweep(start_hz, stop_hz, points_per_decade) {
            out.push(solve_ac(&net, &op, f).map_err(|e| JsValue::from_str(&e.to_string()))?);
        }
        #[derive(Serialize)]
        struct AcResult {
            node_names: Vec<String>,
            samples: Vec<studio_core::analysis::AcSample>,
        }
        to_js(&AcResult {
            node_names: net.node_names,
            samples: out,
        })
    }

    /// Start a streaming transient run over this schematic's netlist.
    #[wasm_bindgen(js_name = startTransient)]
    pub fn start_transient(&self, dt: f64, t_stop: f64) -> Result<TransientHandle, JsValue> {
        let net = self.doc.to_netlist().map_err(|e| JsValue::from_str(&e))?;
        let inner =
            Transient::start(&net, dt, t_stop).map_err(|e| JsValue::from_str(&e.to_string()))?;
        Ok(TransientHandle {
            inner,
            node_names: net.node_names,
        })
    }
}

impl Default for Schematic {
    fn default() -> Self {
        Self::new(0)
    }
}

// ---------------------------------------------------------------------------
// Transient
// ---------------------------------------------------------------------------

/// A running transient analysis, pulled one timestep at a time so the UI can
/// draw a waveform as it is produced rather than waiting for the whole run —
/// see [`Transient`]'s own docs for why that shape exists at all.
#[wasm_bindgen]
pub struct TransientHandle {
    inner: Transient,
    node_names: Vec<String>,
}

#[wasm_bindgen]
impl TransientHandle {
    /// Advance one timestep. Returns `null` once the run has reached
    /// `t_stop`.
    pub fn step(&mut self) -> Result<JsValue, JsValue> {
        match self.inner.step() {
            None => Ok(JsValue::NULL),
            Some(Ok(sample)) => to_js(&sample),
            Some(Err(e)) => Err(JsValue::from_str(&e.to_string())),
        }
    }

    /// Fraction of the run completed, 0..1.
    #[wasm_bindgen(getter)]
    pub fn progress(&self) -> f64 {
        self.inner.progress()
    }

    /// Node names, in the same order as each sample's `voltages`.
    #[wasm_bindgen(js_name = nodeNames)]
    pub fn node_names(&self) -> Vec<JsValue> {
        self.node_names.iter().map(|s| JsValue::from_str(s)).collect()
    }
}

// ---------------------------------------------------------------------------
// Closing the loop: a transistor's parameters, from the fab
// ---------------------------------------------------------------------------

/// Computed MOSFET parameters for a schematic component, derived from process
/// data exactly as `fab-control` reports it.
#[derive(Serialize)]
pub struct ProcessDerivedParams {
    /// Threshold voltage, V.
    pub vth: f64,
    /// Transconductance parameter, A/V².
    pub k_prime: f64,
}

/// The design↔fab loop, made callable: given the gate oxide thickness and
/// channel dose `/fab2`'s furnace and implanter actually produced, compute the
/// same threshold voltage and gain [`studio_core::netlist::MosParams::from_process`]
/// would — the exact function the solver uses, so a value pulled from here and
/// dropped into a schematic's params is not an approximation of the process,
/// it *is* the process.
#[wasm_bindgen(js_name = mosfetFromProcess)]
pub fn mosfet_from_process(
    kind: &str,
    gate_oxide_a: f64,
    channel_dose_e12: f64,
    w_um: f64,
    l_um: f64,
) -> Result<JsValue, JsValue> {
    let kind = if kind.eq_ignore_ascii_case("pmos") {
        MosKind::Pmos
    } else {
        MosKind::Nmos
    };
    let params = studio_core::netlist::MosParams::from_process(
        kind,
        gate_oxide_a,
        channel_dose_e12,
        w_um,
        l_um,
    );
    to_js(&ProcessDerivedParams {
        vth: params.vth,
        k_prime: params.k_prime,
    })
}

/// Threshold voltage alone, for a quick "what would Vt be" readout without
/// committing to full MOSFET params.
#[wasm_bindgen(js_name = thresholdVoltage)]
pub fn threshold_voltage_js(gate_oxide_a: f64, channel_dose_e12: f64) -> f64 {
    threshold_voltage(gate_oxide_a, channel_dose_e12)
}
