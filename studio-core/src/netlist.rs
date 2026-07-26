//! Devices, the netlist that holds them, and how each one stamps into MNA.
//!
//! # Modified nodal analysis in one paragraph
//!
//! Plain nodal analysis writes KCL at every node and solves for node voltages,
//! which works until something in the circuit has no conductance to express —
//! an ideal voltage source has infinite conductance, and an inductor has zero
//! resistance at DC. MNA fixes that by adding the *current through* each such
//! element as an extra unknown, with an extra equation constraining the voltage
//! across it. So the unknown vector is `[node voltages…, branch currents…]`, and
//! the matrix picks up a border of ±1 entries. That border is also why partial
//! pivoting is mandatory: those rows have a zero on the diagonal.
//!
//! # Nonlinear devices
//!
//! Diodes and MOSFETs are stamped by *linearising about the present operating
//! point* — a conductance plus a current source that together reproduce the
//! device's current and slope at that point. Newton's method then iterates the
//! operating point until it stops moving. Every nonlinear device therefore
//! implements the same shape: given terminal voltages, return current and
//! partial derivatives.

use serde::{Deserialize, Serialize};

use crate::matrix::{LinearSystem, Scalar};

/// Thermal voltage kT/q at 300 K, V.
pub const VT_THERMAL: f64 = 0.025_85;
/// Conductance added from every node to ground, S.
///
/// SPICE calls this GMIN. Without it a node that is only connected through
/// cut-off transistors has no DC path to ground and the matrix is singular —
/// which is a numerical artefact, not a real circuit fault. 1e-12 S is far
/// below any current anyone is measuring and makes the matrix non-singular.
pub const GMIN: f64 = 1e-12;

/// Ground is always node 0 and is not an unknown.
pub const GROUND: usize = 0;

// ---------------------------------------------------------------------------
// Devices
// ---------------------------------------------------------------------------

/// Channel type.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
pub enum MosKind {
    /// n-channel.
    Nmos,
    /// p-channel.
    Pmos,
}

/// Level-1 (Shichman–Hodges) MOSFET parameters.
#[derive(Debug, Clone, Copy, PartialEq, Serialize, Deserialize)]
pub struct MosParams {
    /// Channel type.
    pub kind: MosKind,
    /// Channel width, µm.
    pub w_um: f64,
    /// Channel length, µm.
    pub l_um: f64,
    /// Threshold voltage, V. Positive for both types; the sign convention is
    /// handled inside the model.
    pub vth: f64,
    /// Transconductance parameter µ·Cox, A/V².
    pub k_prime: f64,
    /// Channel-length modulation, 1/V.
    pub lambda: f64,
}

impl MosParams {
    /// Build a transistor from what the fab actually produced.
    ///
    /// This is the design↔fab loop made literal: `gate_oxide_a` and
    /// `channel_dose_e12` are the numbers `fab-control` reports off the line, so
    /// a furnace running cold shows up here as a shifted threshold and a lower
    /// gain — the schematic inherits the process rather than assuming a
    /// datasheet.
    pub fn from_process(
        kind: MosKind,
        gate_oxide_a: f64,
        channel_dose_e12: f64,
        w_um: f64,
        l_um: f64,
    ) -> Self {
        // Same oxide capacitance the threshold model uses: C_ox = ε_ox / t_ox.
        const EPS_OX: f64 = 3.9 * 8.854_187_8e-14; // F/cm
        let t_ox_cm = gate_oxide_a.max(1.0) * 1.0e-8;
        let c_ox = EPS_OX / t_ox_cm;
        // Surface mobility, cm²/V·s — well below the bulk figure because
        // carriers in an inversion layer scatter off the oxide interface. Holes
        // run at roughly a third of electrons.
        let mobility = match kind {
            MosKind::Nmos => 600.0,
            MosKind::Pmos => 220.0,
        };
        let vth = fab_control::wafer::threshold_voltage(gate_oxide_a, channel_dose_e12);
        MosParams {
            kind,
            w_um,
            l_um,
            // A p-channel threshold is negative; the model works with the
            // magnitude and applies the sign itself.
            vth: vth.abs(),
            k_prime: mobility * c_ox,
            lambda: 0.02,
        }
    }

    /// Gain factor β = k′·W/L, A/V².
    pub fn beta(&self) -> f64 {
        self.k_prime * (self.w_um / self.l_um.max(1e-9))
    }
}

/// Small-signal source specification for AC analysis.
#[derive(Debug, Clone, Copy, PartialEq, Serialize, Deserialize, Default)]
pub struct AcSpec {
    /// Magnitude, V or A.
    pub magnitude: f64,
    /// Phase, degrees.
    pub phase_deg: f64,
}

/// Time-domain stimulus for transient analysis.
#[derive(Debug, Clone, Copy, PartialEq, Serialize, Deserialize)]
pub enum Waveform {
    /// Constant.
    Dc,
    /// `offset + amplitude·sin(2πft + φ)`.
    Sine {
        /// Offset, V or A.
        offset: f64,
        /// Amplitude, V or A.
        amplitude: f64,
        /// Frequency, Hz.
        freq_hz: f64,
        /// Phase, degrees.
        phase_deg: f64,
    },
    /// A single step from `from` to `to` at `t_edge`, with a finite rise so the
    /// integrator is not asked to follow a discontinuity.
    Step {
        /// Level before the edge.
        from: f64,
        /// Level after the edge.
        to: f64,
        /// Edge time, s.
        t_edge: f64,
        /// Rise time, s.
        rise: f64,
    },
    /// Repeating pulse.
    Pulse {
        /// Low level.
        low: f64,
        /// High level.
        high: f64,
        /// Period, s.
        period: f64,
        /// Time spent high per period, s.
        width: f64,
        /// Rise and fall time, s.
        rise: f64,
    },
}

impl Waveform {
    /// Value at time `t`, given the source's DC level for [`Waveform::Dc`].
    pub fn value(&self, t: f64, dc: f64) -> f64 {
        match *self {
            Waveform::Dc => dc,
            Waveform::Sine {
                offset,
                amplitude,
                freq_hz,
                phase_deg,
            } => {
                offset
                    + amplitude
                        * (std::f64::consts::TAU * freq_hz * t
                            + phase_deg.to_radians())
                        .sin()
            }
            Waveform::Step {
                from,
                to,
                t_edge,
                rise,
            } => {
                if t <= t_edge {
                    from
                } else if rise > 0.0 && t < t_edge + rise {
                    from + (to - from) * (t - t_edge) / rise
                } else {
                    to
                }
            }
            Waveform::Pulse {
                low,
                high,
                period,
                width,
                rise,
            } => {
                if period <= 0.0 {
                    return low;
                }
                let phase = t.rem_euclid(period);
                if phase < rise && rise > 0.0 {
                    low + (high - low) * phase / rise
                } else if phase < width {
                    high
                } else if rise > 0.0 && phase < width + rise {
                    high - (high - low) * (phase - width) / rise
                } else {
                    low
                }
            }
        }
    }
}

/// What a component is.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub enum Device {
    /// Resistance, Ω.
    Resistor(f64),
    /// Capacitance, F.
    Capacitor(f64),
    /// Inductance, H.
    Inductor(f64),
    /// Independent voltage source.
    VoltageSource {
        /// DC level, V.
        dc: f64,
        /// Transient stimulus.
        waveform: Waveform,
        /// Small-signal drive for AC analysis.
        ac: AcSpec,
    },
    /// Independent current source.
    CurrentSource {
        /// DC level, A.
        dc: f64,
        /// Transient stimulus.
        waveform: Waveform,
        /// Small-signal drive for AC analysis.
        ac: AcSpec,
    },
    /// pn junction diode.
    Diode {
        /// Saturation current, A.
        is: f64,
        /// Emission coefficient.
        n: f64,
    },
    /// MOSFET.
    Mosfet(MosParams),
}

impl Device {
    /// How many terminals the device has.
    pub fn terminal_count(&self) -> usize {
        match self {
            Device::Mosfet(_) => 3,
            _ => 2,
        }
    }

    /// Whether MNA needs an extra current unknown for this device.
    ///
    /// True for anything whose branch current cannot be written as a
    /// conductance times a node-voltage difference: ideal voltage sources
    /// always, and inductors at DC (where they are a short).
    pub fn needs_current_unknown(&self) -> bool {
        matches!(self, Device::VoltageSource { .. } | Device::Inductor(_))
    }

    /// Whether the device must be linearised and iterated.
    pub fn is_nonlinear(&self) -> bool {
        matches!(self, Device::Diode { .. } | Device::Mosfet(_))
    }

    /// Short label for the UI, e.g. `"R"`, `"M1"`.
    pub fn symbol(&self) -> &'static str {
        match self {
            Device::Resistor(_) => "R",
            Device::Capacitor(_) => "C",
            Device::Inductor(_) => "L",
            Device::VoltageSource { .. } => "V",
            Device::CurrentSource { .. } => "I",
            Device::Diode { .. } => "D",
            Device::Mosfet(_) => "M",
        }
    }
}

/// One placed component.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct Component {
    /// Reference designator, e.g. `"R1"`. Unique within a netlist.
    pub name: String,
    /// What it is.
    pub device: Device,
    /// Node indices, in device order. Two-terminal devices are `[plus, minus]`;
    /// a MOSFET is `[drain, gate, source]`.
    pub nodes: Vec<usize>,
}

// ---------------------------------------------------------------------------
// Nonlinear device evaluation
// ---------------------------------------------------------------------------

/// A nonlinear device linearised about an operating point.
#[derive(Debug, Clone, Copy, PartialEq)]
pub struct Linearised {
    /// Current through the device at this operating point, A.
    pub current: f64,
    /// ∂I/∂V across the controlling terminals, S.
    pub gm: f64,
    /// ∂I/∂V across the output terminals, S.
    pub gds: f64,
}

/// Limit the step in a pn-junction voltage between Newton iterations.
///
/// A diode's current is exponential in its voltage, so an unconstrained Newton
/// step routinely proposes a voltage tens of volts too high, `exp()` overflows,
/// and the solve diverges or produces NaN. This is SPICE's `pnjlim`: it lets the
/// voltage grow only logarithmically once past the critical voltage, which
/// converges reliably without changing the solution it converges *to*.
pub fn limit_junction_voltage(v_new: f64, v_old: f64, vt: f64, v_crit: f64) -> f64 {
    if v_new > v_crit && (v_new - v_old).abs() > 2.0 * vt {
        if v_old > 0.0 {
            let arg = 1.0 + (v_new - v_old) / vt;
            if arg > 0.0 {
                v_old + vt * arg.ln()
            } else {
                v_crit
            }
        } else {
            vt * (v_new / vt).max(1e-9).ln()
        }
    } else {
        v_new
    }
}

/// Evaluate a diode at bias `vd`.
pub fn eval_diode(is: f64, n: f64, vd: f64) -> Linearised {
    let vte = n * VT_THERMAL;
    // Clamp the exponent so a wild intermediate iterate cannot produce inf.
    let x = (vd / vte).min(80.0);
    let e = x.exp();
    Linearised {
        current: is * (e - 1.0),
        gm: 0.0,
        gds: (is / vte) * e + GMIN,
    }
}

/// Evaluate a level-1 MOSFET.
///
/// Returns drain current and its derivatives with respect to `vgs` and `vds`.
/// For a p-channel device the caller's voltages are negated on the way in and
/// the current on the way out, so the same equations serve both.
pub fn eval_mosfet(p: &MosParams, vgs_in: f64, vds_in: f64) -> Linearised {
    let sign = match p.kind {
        MosKind::Nmos => 1.0,
        MosKind::Pmos => -1.0,
    };
    let vgs = sign * vgs_in;
    let vds = sign * vds_in;
    let beta = p.beta();
    let vov = vgs - p.vth;

    let (id, gm, gds) = if vov <= 0.0 {
        // Cut-off. GMIN keeps the node connected for the matrix's sake.
        (0.0, 0.0, GMIN)
    } else if vds < vov {
        // Triode.
        let lam = 1.0 + p.lambda * vds;
        let id = beta * (vov * vds - 0.5 * vds * vds) * lam;
        let gm = beta * vds * lam;
        let gds = beta * (vov - vds) * lam + beta * (vov * vds - 0.5 * vds * vds) * p.lambda;
        (id, gm, gds + GMIN)
    } else {
        // Saturation.
        let lam = 1.0 + p.lambda * vds;
        let id = 0.5 * beta * vov * vov * lam;
        let gm = beta * vov * lam;
        let gds = 0.5 * beta * vov * vov * p.lambda;
        (id, gm, gds + GMIN)
    };

    Linearised {
        // Derivatives are even under the sign flip; the current is not.
        current: sign * id,
        gm,
        gds,
    }
}

// ---------------------------------------------------------------------------
// Stamping helpers
// ---------------------------------------------------------------------------

/// Map a circuit node to its matrix index, or `None` for ground.
fn idx(node: usize) -> Option<usize> {
    (node != GROUND).then(|| node - 1)
}

/// Stamp a conductance `g` between two nodes.
pub fn stamp_conductance<T: Scalar>(sys: &mut LinearSystem<T>, a: usize, b: usize, g: T) {
    match (idx(a), idx(b)) {
        (Some(i), Some(j)) => {
            sys.add(i, i, g);
            sys.add(j, j, g);
            sys.add(i, j, -g);
            sys.add(j, i, -g);
        }
        (Some(i), None) | (None, Some(i)) => sys.add(i, i, g),
        (None, None) => {}
    }
}

/// Stamp a current `i` flowing from node `from` to node `to` through the device
/// — i.e. leaving `from` and entering `to`.
pub fn stamp_current<T: Scalar>(sys: &mut LinearSystem<T>, from: usize, to: usize, i: T) {
    if let Some(r) = idx(from) {
        sys.add_rhs(r, -i);
    }
    if let Some(r) = idx(to) {
        sys.add_rhs(r, i);
    }
}

/// Stamp a voltage-controlled current source: current `gm·(v_cp − v_cn)`
/// flowing from `p` to `n`.
pub fn stamp_vccs<T: Scalar>(
    sys: &mut LinearSystem<T>,
    p: usize,
    n: usize,
    cp: usize,
    cn: usize,
    gm: T,
) {
    for (out, s) in [(p, T::one()), (n, -T::one())] {
        let Some(r) = idx(out) else { continue };
        if let Some(c) = idx(cp) {
            sys.add(r, c, s * gm);
        }
        if let Some(c) = idx(cn) {
            sys.add(r, c, -(s * gm));
        }
    }
}

/// Stamp the ±1 border that ties a branch current unknown to the voltage across
/// two nodes.
pub fn stamp_branch<T: Scalar>(sys: &mut LinearSystem<T>, p: usize, n: usize, row: usize) {
    if let Some(i) = idx(p) {
        sys.add(i, row, T::one());
        sys.add(row, i, T::one());
    }
    if let Some(i) = idx(n) {
        sys.add(i, row, -T::one());
        sys.add(row, i, -T::one());
    }
}

// ---------------------------------------------------------------------------
// Netlist
// ---------------------------------------------------------------------------

/// A circuit.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize, Default)]
pub struct Netlist {
    /// Components, in stamping order.
    pub components: Vec<Component>,
    /// Human-readable name per node index; index 0 is ground.
    pub node_names: Vec<String>,
}

impl Netlist {
    /// An empty circuit containing only ground.
    pub fn new() -> Self {
        Netlist {
            components: Vec::new(),
            node_names: vec!["0".to_string()],
        }
    }

    /// Find or create a node by name. `"0"` and `"gnd"` are ground.
    pub fn node(&mut self, name: &str) -> usize {
        if name == "0" || name.eq_ignore_ascii_case("gnd") {
            return GROUND;
        }
        if let Some(i) = self.node_names.iter().position(|n| n == name) {
            return i;
        }
        self.node_names.push(name.to_string());
        self.node_names.len() - 1
    }

    /// Add a component, resolving node names.
    pub fn add(&mut self, name: &str, device: Device, nodes: &[&str]) -> &mut Self {
        let resolved: Vec<usize> = nodes.iter().map(|n| self.node(n)).collect();
        debug_assert_eq!(
            resolved.len(),
            device.terminal_count(),
            "{name}: wrong terminal count"
        );
        self.components.push(Component {
            name: name.to_string(),
            device,
            nodes: resolved,
        });
        self
    }

    /// Number of non-ground nodes.
    pub fn node_count(&self) -> usize {
        self.node_names.len().saturating_sub(1)
    }

    /// Components needing a branch-current unknown, in order. The returned
    /// index is the offset past the node unknowns.
    pub fn branch_components(&self) -> Vec<usize> {
        self.components
            .iter()
            .enumerate()
            .filter(|(_, c)| c.device.needs_current_unknown())
            .map(|(i, _)| i)
            .collect()
    }

    /// Total MNA unknowns: node voltages plus branch currents.
    pub fn unknown_count(&self) -> usize {
        self.node_count() + self.branch_components().len()
    }

    /// Whether any device needs Newton iteration.
    pub fn is_nonlinear(&self) -> bool {
        self.components.iter().any(|c| c.device.is_nonlinear())
    }

    /// Index of a node by name, if it exists.
    pub fn node_index(&self, name: &str) -> Option<usize> {
        if name == "0" || name.eq_ignore_ascii_case("gnd") {
            return Some(GROUND);
        }
        self.node_names.iter().position(|n| n == name)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn ground_is_never_an_unknown() {
        let mut n = Netlist::new();
        assert_eq!(n.node("0"), GROUND);
        assert_eq!(n.node("gnd"), GROUND);
        assert_eq!(n.node("GND"), GROUND);
        assert_eq!(n.node_count(), 0);
        n.node("out");
        assert_eq!(n.node_count(), 1);
    }

    #[test]
    fn node_names_are_interned() {
        let mut n = Netlist::new();
        let a = n.node("mid");
        let b = n.node("mid");
        assert_eq!(a, b);
        assert_eq!(n.node_count(), 1);
    }

    #[test]
    fn unknown_count_adds_a_row_per_voltage_source_and_inductor() {
        let mut n = Netlist::new();
        n.add("R1", Device::Resistor(1e3), &["a", "0"]);
        assert_eq!(n.unknown_count(), 1);
        n.add(
            "V1",
            Device::VoltageSource {
                dc: 5.0,
                waveform: Waveform::Dc,
                ac: AcSpec::default(),
            },
            &["a", "0"],
        );
        assert_eq!(n.unknown_count(), 2, "voltage source adds a current unknown");
        n.add("L1", Device::Inductor(1e-3), &["a", "b"]);
        assert_eq!(n.unknown_count(), 4, "inductor adds a node and a current");
        n.add("C1", Device::Capacitor(1e-9), &["b", "0"]);
        assert_eq!(n.unknown_count(), 4, "a capacitor adds neither");
    }

    #[test]
    fn conductance_stamp_is_symmetric_and_sums_to_zero() {
        // KCL demands every row of a pure-conductance stamp sum to zero.
        let mut s = LinearSystem::<f64>::new(2);
        stamp_conductance(&mut s, 1, 2, 0.001);
        assert_eq!(s.get(0, 0), 0.001);
        assert_eq!(s.get(1, 1), 0.001);
        assert_eq!(s.get(0, 1), -0.001);
        assert_eq!(s.get(1, 0), -0.001);
        for r in 0..2 {
            assert!((s.get(r, 0) + s.get(r, 1)).abs() < 1e-18);
        }
    }

    #[test]
    fn a_grounded_conductance_only_touches_one_diagonal() {
        let mut s = LinearSystem::<f64>::new(1);
        stamp_conductance(&mut s, 1, GROUND, 0.005);
        assert_eq!(s.get(0, 0), 0.005);
    }

    #[test]
    fn current_stamp_leaves_one_node_and_enters_the_other() {
        let mut s = LinearSystem::<f64>::new(2);
        stamp_current(&mut s, 1, 2, 0.003);
        assert_eq!(s.rhs(0), -0.003);
        assert_eq!(s.rhs(1), 0.003);
    }

    #[test]
    fn diode_matches_the_shockley_equation() {
        let is = 1e-14;
        let d = eval_diode(is, 1.0, 0.6);
        let expected = is * ((0.6 / VT_THERMAL).exp() - 1.0);
        assert!((d.current - expected).abs() / expected < 1e-9);
        // The textbook landmark: a 1e-14 A junction needs ~0.65 V for 1 mA, and
        // carries only ~0.12 mA at 0.6 V — the decade-per-60 mV slope.
        assert!((d.current - 0.120e-3).abs() < 5e-6, "I at 0.6 V = {}", d.current);
        let one_ma = eval_diode(is, 1.0, 0.655);
        assert!((one_ma.current - 1e-3).abs() < 5e-5, "I at 0.655 V = {}", one_ma.current);
        // A decade of current per ~60 mV.
        let decade = eval_diode(is, 1.0, 0.6 + 0.0595);
        assert!((decade.current / d.current - 10.0).abs() < 0.3);
        // Conductance is I/Vt in forward bias.
        assert!((d.gds - (d.current + is) / VT_THERMAL).abs() / d.gds < 1e-6);
    }

    #[test]
    fn a_reverse_biased_diode_carries_minus_is() {
        let d = eval_diode(1e-14, 1.0, -1.0);
        assert!((d.current + 1e-14).abs() < 1e-20);
        assert!(d.gds > 0.0, "must stay non-singular");
    }

    #[test]
    fn diode_evaluation_never_overflows() {
        // An intermediate Newton iterate can propose an absurd voltage; the
        // model must return a finite number rather than inf/NaN.
        let d = eval_diode(1e-14, 1.0, 50.0);
        assert!(d.current.is_finite() && d.gds.is_finite());
    }

    #[test]
    fn junction_limiting_bounds_the_step_but_keeps_the_direction() {
        let vt = VT_THERMAL;
        let v_crit = 0.7;
        // A wild step gets reined in...
        let limited = limit_junction_voltage(5.0, 0.6, vt, v_crit);
        assert!(limited < 5.0 && limited > 0.6, "got {limited}");
        // ...but a small step passes through untouched.
        assert_eq!(limit_junction_voltage(0.61, 0.6, vt, v_crit), 0.61);
        // And below the critical voltage nothing is limited.
        assert_eq!(limit_junction_voltage(0.2, 0.1, vt, v_crit), 0.2);
    }

    #[test]
    fn mosfet_regions_are_continuous_at_the_boundary() {
        // Triode and saturation must agree at Vds = Vov, or Newton will
        // oscillate across the seam and never converge.
        let p = MosParams {
            kind: MosKind::Nmos,
            w_um: 10.0,
            l_um: 1.0,
            vth: 0.7,
            k_prime: 46e-6,
            lambda: 0.02,
        };
        let vgs = 2.0;
        let vov = vgs - p.vth;
        let below = eval_mosfet(&p, vgs, vov - 1e-7);
        let above = eval_mosfet(&p, vgs, vov + 1e-7);
        assert!((below.current - above.current).abs() < 1e-9);
        assert!((below.gm - above.gm).abs() < 1e-9);
    }

    #[test]
    fn a_mosfet_below_threshold_is_off() {
        let p = MosParams {
            kind: MosKind::Nmos,
            w_um: 10.0,
            l_um: 1.0,
            vth: 0.7,
            k_prime: 46e-6,
            lambda: 0.02,
        };
        let off = eval_mosfet(&p, 0.5, 2.0);
        assert_eq!(off.current, 0.0);
        assert_eq!(off.gm, 0.0);
        assert!(off.gds > 0.0, "must stay non-singular");
    }

    #[test]
    fn saturation_current_matches_the_square_law() {
        let p = MosParams {
            kind: MosKind::Nmos,
            w_um: 10.0,
            l_um: 1.0,
            vth: 0.7,
            k_prime: 46e-6,
            lambda: 0.0,
        };
        // Id = (β/2)(Vgs−Vth)², β = 46µ × 10 = 460 µA/V²; Vov = 1.3
        let m = eval_mosfet(&p, 2.0, 3.0);
        let expected = 0.5 * 460e-6 * 1.3 * 1.3;
        assert!((m.current - expected).abs() / expected < 1e-9, "{}", m.current);
        // gm = β·Vov
        assert!((m.gm - 460e-6 * 1.3).abs() / m.gm < 1e-9);
    }

    #[test]
    fn a_pmos_mirrors_an_nmos() {
        let mk = |kind| MosParams {
            kind,
            w_um: 10.0,
            l_um: 1.0,
            vth: 0.7,
            k_prime: 46e-6,
            lambda: 0.02,
        };
        let n = eval_mosfet(&mk(MosKind::Nmos), 2.0, 3.0);
        let p = eval_mosfet(&mk(MosKind::Pmos), -2.0, -3.0);
        assert!((n.current + p.current).abs() < 1e-15, "currents should mirror");
        assert!((n.gm - p.gm).abs() < 1e-15, "derivatives should match");
    }

    #[test]
    fn a_transistor_inherits_the_process_it_was_made_on() {
        // The loop: a furnace running cold grows thinner oxide, which lowers the
        // threshold and raises the gain. Nothing here says so explicitly — it
        // falls out of the same equations the fab simulator uses.
        let nominal = MosParams::from_process(MosKind::Nmos, 450.0, 1.0, 10.0, 1.0);
        let thin = MosParams::from_process(MosKind::Nmos, 400.0, 1.0, 10.0, 1.0);
        assert!(thin.vth < nominal.vth, "thinner oxide lowers Vth");
        assert!(thin.beta() > nominal.beta(), "thinner oxide raises beta");
        // Sanity: the nominal process should land on a textbook device.
        assert!(
            (nominal.vth - 0.65).abs() < 0.06,
            "Vth was {}",
            nominal.vth
        );
        assert!(
            (nominal.k_prime - 46e-6).abs() < 8e-6,
            "k' was {}",
            nominal.k_prime
        );
    }

    #[test]
    fn waveforms_evaluate_correctly() {
        assert_eq!(Waveform::Dc.value(1.0, 3.3), 3.3);

        let s = Waveform::Sine {
            offset: 1.0,
            amplitude: 2.0,
            freq_hz: 1.0,
            phase_deg: 0.0,
        };
        assert!((s.value(0.0, 0.0) - 1.0).abs() < 1e-12);
        assert!((s.value(0.25, 0.0) - 3.0).abs() < 1e-12);

        let st = Waveform::Step {
            from: 0.0,
            to: 5.0,
            t_edge: 1.0,
            rise: 1.0,
        };
        assert_eq!(st.value(0.5, 0.0), 0.0);
        assert!((st.value(1.5, 0.0) - 2.5).abs() < 1e-12, "mid-rise");
        assert_eq!(st.value(9.0, 0.0), 5.0);

        let p = Waveform::Pulse {
            low: 0.0,
            high: 1.0,
            period: 10.0,
            width: 5.0,
            rise: 0.0,
        };
        assert_eq!(p.value(1.0, 0.0), 1.0);
        assert_eq!(p.value(7.0, 0.0), 0.0);
        assert_eq!(p.value(11.0, 0.0), 1.0, "repeats");
    }
}
