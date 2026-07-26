//! DC, transient and AC analyses.
//!
//! # Streaming, not batch
//!
//! [`Transient`] is an iterator, not a function that returns a `Vec`. That is a
//! deliberate shape: the point of "simulation you can feel" is that waveforms
//! appear as the solver produces them, so the UI pulls one timestep at a time
//! and draws it. A batch API would force the caller to wait for the whole run
//! before showing anything, and would make a long sweep look like a hang.
//!
//! # Companion models
//!
//! Reactive elements are converted, each timestep, into a resistor and a source
//! that together reproduce their behaviour over that step. With backward Euler
//! a capacitor's `i = C·dv/dt` becomes `i ≈ (C/h)·v − (C/h)·v_prev`: a
//! conductance `C/h` in parallel with a current source carrying the history.
//! Backward Euler is chosen over the trapezoidal rule because it is
//! unconditionally stable and does not ring on a step input — trapezoidal is
//! more accurate per step but oscillates on stiff switching circuits, which is
//! most of what gets drawn here.

use serde::{Deserialize, Serialize};

use crate::matrix::{Complex, LinearSystem, SolveError};
use crate::netlist::{
    eval_diode, eval_mosfet, limit_junction_voltage, stamp_branch, stamp_conductance,
    stamp_current, stamp_vccs, Device, Netlist, GMIN, GROUND, VT_THERMAL,
};

/// Absolute voltage tolerance for Newton convergence, V.
pub const VNTOL: f64 = 1e-6;
/// Relative tolerance for Newton convergence.
pub const RELTOL: f64 = 1e-3;
/// Newton iterations before giving up.
pub const MAX_NEWTON_ITERATIONS: usize = 200;

/// Why an analysis failed.
#[derive(Debug, Clone, PartialEq)]
pub enum SimError {
    /// The matrix was singular.
    Singular(SolveError),
    /// Newton did not converge.
    ///
    /// Reports the worst-offending unknown, because "did not converge" on its
    /// own is not actionable and the node that is still moving usually is.
    NoConvergence {
        /// Iterations attempted.
        iterations: usize,
        /// Largest remaining change, V.
        worst_delta: f64,
        /// Unknown index it occurred at.
        at_unknown: usize,
    },
}

impl std::fmt::Display for SimError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            SimError::Singular(e) => write!(f, "{e}"),
            SimError::NoConvergence {
                iterations,
                worst_delta,
                at_unknown,
            } => write!(
                f,
                "no convergence after {iterations} Newton iterations — unknown {at_unknown} still \
                 moving by {worst_delta:.3e} V"
            ),
        }
    }
}

impl From<SolveError> for SimError {
    fn from(e: SolveError) -> Self {
        SimError::Singular(e)
    }
}

/// A solved operating point.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct OperatingPoint {
    /// Node voltages, indexed by node number. Index 0 is ground and is always 0.
    pub voltages: Vec<f64>,
    /// Branch currents, in [`Netlist::branch_components`] order.
    pub branch_currents: Vec<f64>,
    /// Newton iterations taken. 1 for a linear circuit.
    pub iterations: usize,
}

impl OperatingPoint {
    /// Voltage at a node index.
    pub fn v(&self, node: usize) -> f64 {
        self.voltages.get(node).copied().unwrap_or(0.0)
    }
}

/// Read the unknown vector back into node voltages and branch currents.
fn unpack(net: &Netlist, x: &[f64], iterations: usize) -> OperatingPoint {
    let n = net.node_count();
    let mut voltages = vec![0.0; n + 1];
    voltages[1..].copy_from_slice(&x[..n]);
    OperatingPoint {
        voltages,
        branch_currents: x[n..].to_vec(),
        iterations,
    }
}

/// Node voltage from an unknown vector, treating ground as exactly zero.
fn node_v(x: &[f64], node: usize) -> f64 {
    if node == GROUND {
        0.0
    } else {
        x.get(node - 1).copied().unwrap_or(0.0)
    }
}

/// Build the real-valued MNA system at a given operating point and time.
///
/// `x` is the present Newton iterate — linear devices ignore it, nonlinear ones
/// linearise about it. `history` supplies companion-model state for transient;
/// pass `None` for DC, where capacitors are open and inductors are shorts.
fn build_system(
    net: &Netlist,
    x: &[f64],
    t: f64,
    history: Option<&History>,
    junctions: &mut Junctions,
) -> LinearSystem<f64> {
    let n = net.node_count();
    let mut sys = LinearSystem::new(net.unknown_count());
    let branches = net.branch_components();

    // GMIN to ground everywhere keeps the matrix non-singular when a node is
    // isolated by cut-off devices.
    for i in 0..n {
        sys.add(i, i, GMIN);
    }

    for (ci, comp) in net.components.iter().enumerate() {
        let branch_row = branches.iter().position(|&b| b == ci).map(|k| n + k);
        match &comp.device {
            Device::Resistor(r) => {
                stamp_conductance(&mut sys, comp.nodes[0], comp.nodes[1], 1.0 / r.max(1e-12));
            }
            Device::Capacitor(c) => {
                if let Some(h) = history {
                    // Backward Euler companion: conductance C/h plus a source
                    // carrying the previous voltage.
                    let geq = c / h.dt;
                    let v_prev = h.cap_voltage[ci];
                    stamp_conductance(&mut sys, comp.nodes[0], comp.nodes[1], geq);
                    stamp_current(&mut sys, comp.nodes[1], comp.nodes[0], geq * v_prev);
                }
                // At DC a capacitor is an open circuit: stamp nothing.
            }
            Device::Inductor(l) => {
                let row = branch_row.expect("inductor has a branch unknown");
                stamp_branch(&mut sys, comp.nodes[0], comp.nodes[1], row);
                if let Some(h) = history {
                    // v = (L/h)(i − i_prev)
                    let req = l / h.dt;
                    sys.add(row, row, -req);
                    sys.add_rhs(row, -req * h.ind_current[ci]);
                }
                // At DC an inductor is a short: the row is just v_p − v_n = 0.
            }
            Device::VoltageSource { dc, waveform, .. } => {
                let row = branch_row.expect("voltage source has a branch unknown");
                stamp_branch(&mut sys, comp.nodes[0], comp.nodes[1], row);
                let v = if history.is_some() {
                    waveform.value(t, *dc)
                } else {
                    *dc
                };
                sys.add_rhs(row, v);
            }
            Device::CurrentSource { dc, waveform, .. } => {
                let i = if history.is_some() {
                    waveform.value(t, *dc)
                } else {
                    *dc
                };
                stamp_current(&mut sys, comp.nodes[0], comp.nodes[1], i);
            }
            Device::Diode { is, n: emission } => {
                let (p, m) = (comp.nodes[0], comp.nodes[1]);
                // Limit here, against this diode's own previous voltage, rather
                // than by patching the solution vector afterwards. Patching the
                // vector is wrong when two junctions share a node — correcting
                // one silently moves the other — and it also loses the per-device
                // history that makes the limit meaningful.
                let vte = emission * VT_THERMAL;
                let v_crit = vte * (vte / (std::f64::consts::SQRT_2 * is)).ln();
                let raw = node_v(x, p) - node_v(x, m);
                let vd = limit_junction_voltage(raw, junctions.voltages[ci], vte, v_crit);
                if vd != raw {
                    junctions.limited = true;
                }
                junctions.voltages[ci] = vd;
                let lin = eval_diode(*is, *emission, vd);
                stamp_conductance(&mut sys, p, m, lin.gds);
                // Companion source. Linearising gives I = g·V + I_eq with
                // I_eq = I(v0) − g·v0; the tangent line's Norton equivalent.
                // `stamp_current(p, m, i)` already applies KCL's sign (current
                // leaving p subtracts from p's row), so I_eq goes in as-is —
                // negating it here would cancel that and the Newton step would
                // climb the exponential in the wrong direction.
                let ieq = lin.current - lin.gds * vd;
                stamp_current(&mut sys, p, m, ieq);
            }
            Device::Mosfet(params) => {
                let (d, g, s) = (comp.nodes[0], comp.nodes[1], comp.nodes[2]);
                let vgs = node_v(x, g) - node_v(x, s);
                let vds = node_v(x, d) - node_v(x, s);
                let lin = eval_mosfet(params, vgs, vds);
                // Output conductance across drain–source.
                stamp_conductance(&mut sys, d, s, lin.gds);
                // Transconductance: drain current controlled by gate–source.
                stamp_vccs(&mut sys, d, s, g, s, lin.gm);
                let ieq = lin.current - lin.gm * vgs - lin.gds * vds;
                stamp_current(&mut sys, d, s, ieq);
            }
        }
    }
    sys
}

/// Whether every unknown has stopped moving.
fn converged(new: &[f64], old: &[f64]) -> Option<(f64, usize)> {
    let mut worst = 0.0;
    let mut at = 0;
    for (i, (a, b)) in new.iter().zip(old).enumerate() {
        let delta = (a - b).abs();
        let tol = VNTOL + RELTOL * a.abs().max(b.abs());
        if delta > tol && delta > worst {
            worst = delta;
            at = i;
        }
    }
    (worst > 0.0).then_some((worst, at))
}

/// Solve the DC operating point.
///
/// Linear circuits converge in one pass. Nonlinear ones iterate Newton with
/// junction limiting until every unknown settles.
pub fn solve_dc(net: &Netlist) -> Result<OperatingPoint, SimError> {
    let unknowns = net.unknown_count();
    let mut x = vec![0.0; unknowns];
    let mut junctions = Junctions::new(net.components.len());

    if !net.is_nonlinear() {
        let x = build_system(net, &x, 0.0, None, &mut junctions).solve()?;
        return Ok(unpack(net, &x, 1));
    }

    for iteration in 1..=MAX_NEWTON_ITERATIONS {
        junctions.limited = false;
        let x_new = build_system(net, &x, 0.0, None, &mut junctions).solve()?;
        let settled = converged(&x_new, &x);
        match settled {
            None if !junctions.limited => return Ok(unpack(net, &x_new, iteration)),
            None => {
                // Vector is still but a junction is being held back; keep going.
                x = x_new;
                continue;
            }
            Some((worst_delta, at_unknown)) => {
                x = x_new;
                if iteration == MAX_NEWTON_ITERATIONS {
                    return Err(SimError::NoConvergence {
                        iterations: iteration,
                        worst_delta,
                        at_unknown,
                    });
                }
            }
        }
    }
    unreachable!("loop returns on the final iteration")
}

// ---------------------------------------------------------------------------
// Transient
// ---------------------------------------------------------------------------

/// Per-junction state for Newton limiting.
///
/// `limited` is not bookkeeping — it is part of the convergence test. Limiting
/// deliberately holds a junction back from the voltage Newton asked for, and
/// while that is happening the *solution vector* can sit still (the diode is
/// still effectively open, so nothing else in the circuit moves) even though the
/// junction is still marching toward its answer. Declaring convergence on the
/// vector alone therefore stops several hundred millivolts early, and reports a
/// diode that never turned on.
#[derive(Debug, Clone, Default)]
struct Junctions {
    voltages: Vec<f64>,
    limited: bool,
}

impl Junctions {
    fn new(len: usize) -> Self {
        Junctions {
            voltages: vec![0.0; len],
            limited: false,
        }
    }
}

/// Companion-model state carried between timesteps.
#[derive(Debug, Clone)]
struct History {
    dt: f64,
    /// Previous voltage across each capacitor, indexed by component.
    cap_voltage: Vec<f64>,
    /// Previous current through each inductor, indexed by component.
    ind_current: Vec<f64>,
}

/// One timestep of a transient run.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct TransientSample {
    /// Time, s.
    pub t: f64,
    /// Node voltages, index 0 being ground.
    pub voltages: Vec<f64>,
    /// Branch currents.
    pub branch_currents: Vec<f64>,
    /// Newton iterations this step needed — a useful convergence diagnostic.
    pub iterations: usize,
}

/// A transient analysis in progress.
///
/// Pull samples with [`Transient::step`] (or via [`Iterator`]) so a UI can plot
/// them as they are produced rather than waiting for the whole run.
#[derive(Debug, Clone)]
pub struct Transient {
    net: Netlist,
    history: History,
    x: Vec<f64>,
    /// Present time, s.
    pub t: f64,
    /// End time, s.
    pub t_stop: f64,
    junctions: Junctions,
    finished: bool,
}

impl Transient {
    /// Start a transient run with every unknown at zero: an unenergized
    /// circuit, capacitors uncharged and inductors carrying no current.
    ///
    /// This deliberately does *not* seed from [`solve_dc`]. `solve_dc` reads
    /// each source's plain `dc` field and never looks at its `waveform` —
    /// correct for what a DC analysis means, but wrong as a transient's t=0⁻
    /// condition whenever a source's `dc` field (its post-step level) differs
    /// from its waveform's actual value just before the run starts (its
    /// pre-step level). A `Waveform::Step` from 0 to 5 V with `dc: 5.0`, for
    /// instance, would seed the whole circuit as if it had already been
    /// sitting at 5 V forever — silently erasing the very edge the transient
    /// exists to show. Zero avoids the mismatch entirely and is the ordinary
    /// convention for "not yet turned on"; it costs nothing in reliability,
    /// since [`solve_dc`]'s own Newton iteration already starts every
    /// nonlinear circuit in this crate's test suite from the same all-zero
    /// guess.
    ///
    /// Takes `net` by reference and clones it internally — a `Transient` does
    /// not borrow, so a caller across an FFI boundary (a wasm handle, in
    /// particular) can hold one without also having to keep the original
    /// `Netlist` alive and immutable for the run's whole lifetime.
    pub fn start(net: &Netlist, dt: f64, t_stop: f64) -> Result<Self, SimError> {
        let x = vec![0.0; net.unknown_count()];
        let count = net.components.len();
        let mut history = History {
            dt,
            cap_voltage: vec![0.0; count],
            ind_current: vec![0.0; count],
        };
        record_history(net, &x, &mut history);

        Ok(Transient {
            net: net.clone(),
            history,
            x,
            t: 0.0,
            t_stop,
            junctions: Junctions::new(count),
            finished: false,
        })
    }

    /// Fraction of the run completed, 0..1.
    pub fn progress(&self) -> f64 {
        if self.t_stop <= 0.0 {
            1.0
        } else {
            (self.t / self.t_stop).clamp(0.0, 1.0)
        }
    }

    /// Advance one timestep.
    pub fn step(&mut self) -> Option<Result<TransientSample, SimError>> {
        if self.finished || self.t >= self.t_stop {
            return None;
        }
        self.t += self.history.dt;
        if self.t > self.t_stop {
            self.t = self.t_stop;
        }

        let mut iterations = 1;
        let result = if self.net.is_nonlinear() {
            let mut ok = None;
            let mut err = None;
            let mut guess = self.x.clone();
            for iteration in 1..=MAX_NEWTON_ITERATIONS {
                self.junctions.limited = false;
                match build_system(
                    &self.net,
                    &guess,
                    self.t,
                    Some(&self.history),
                    &mut self.junctions,
                )
                .solve()
                {
                    Err(e) => {
                        err = Some(SimError::Singular(e));
                        break;
                    }
                    Ok(x_new) => {
                        match converged(&x_new, &guess) {
                            None if !self.junctions.limited => {
                                iterations = iteration;
                                ok = Some(x_new);
                                break;
                            }
                            None => {
                                guess = x_new;
                            }
                            Some((worst_delta, at_unknown)) => {
                                guess = x_new;
                                if iteration == MAX_NEWTON_ITERATIONS {
                                    err = Some(SimError::NoConvergence {
                                        iterations: iteration,
                                        worst_delta,
                                        at_unknown,
                                    });
                                }
                            }
                        }
                    }
                }
            }
            match (ok, err) {
                (Some(x), _) => Ok(x),
                (None, Some(e)) => Err(e),
                (None, None) => unreachable!(),
            }
        } else {
            build_system(
                &self.net,
                &self.x,
                self.t,
                Some(&self.history),
                &mut self.junctions,
            )
            .solve()
            .map_err(SimError::from)
        };

        match result {
            Err(e) => {
                self.finished = true;
                Some(Err(e))
            }
            Ok(x) => {
                self.x = x;
                record_history(&self.net, &self.x, &mut self.history);
                let op = unpack(&self.net, &self.x, iterations);
                Some(Ok(TransientSample {
                    t: self.t,
                    voltages: op.voltages,
                    branch_currents: op.branch_currents,
                    iterations,
                }))
            }
        }
    }
}

impl Iterator for Transient {
    type Item = Result<TransientSample, SimError>;
    fn next(&mut self) -> Option<Self::Item> {
        self.step()
    }
}

/// Capture reactive-element state for the next timestep's companion models.
fn record_history(net: &Netlist, x: &[f64], history: &mut History) {
    let n = net.node_count();
    let branches = net.branch_components();
    for (ci, comp) in net.components.iter().enumerate() {
        match comp.device {
            Device::Capacitor(_) => {
                history.cap_voltage[ci] = node_v(x, comp.nodes[0]) - node_v(x, comp.nodes[1]);
            }
            Device::Inductor(_) => {
                if let Some(k) = branches.iter().position(|&b| b == ci) {
                    history.ind_current[ci] = x[n + k];
                }
            }
            _ => {}
        }
    }
}

// ---------------------------------------------------------------------------
// AC
// ---------------------------------------------------------------------------

/// Small-signal response at one frequency.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct AcSample {
    /// Frequency, Hz.
    pub freq_hz: f64,
    /// Complex node voltages as (real, imaginary) pairs, index 0 ground.
    pub voltages: Vec<(f64, f64)>,
}

impl AcSample {
    /// Magnitude in dB at a node.
    pub fn db(&self, node: usize) -> f64 {
        let (re, im) = self.voltages.get(node).copied().unwrap_or((0.0, 0.0));
        Complex::new(re, im).db()
    }
    /// Phase in degrees at a node.
    pub fn phase_deg(&self, node: usize) -> f64 {
        let (re, im) = self.voltages.get(node).copied().unwrap_or((0.0, 0.0));
        Complex::new(re, im).arg().to_degrees()
    }
}

/// Solve the small-signal response at one frequency, linearised about `op`.
pub fn solve_ac(net: &Netlist, op: &OperatingPoint, freq_hz: f64) -> Result<AcSample, SimError> {
    let n = net.node_count();
    let branches = net.branch_components();
    let mut sys = LinearSystem::<Complex>::new(net.unknown_count());
    let omega = std::f64::consts::TAU * freq_hz;

    for i in 0..n {
        sys.add(i, i, Complex::new(GMIN, 0.0));
    }

    // Reconstruct the DC unknown vector so nonlinear devices linearise about the
    // same operating point the DC analysis found.
    let mut x = vec![0.0; net.unknown_count()];
    x[..n].copy_from_slice(&op.voltages[1..]);
    x[n..].copy_from_slice(&op.branch_currents);

    for (ci, comp) in net.components.iter().enumerate() {
        let branch_row = branches.iter().position(|&b| b == ci).map(|k| n + k);
        match &comp.device {
            Device::Resistor(r) => stamp_conductance(
                &mut sys,
                comp.nodes[0],
                comp.nodes[1],
                Complex::new(1.0 / r.max(1e-12), 0.0),
            ),
            Device::Capacitor(c) => stamp_conductance(
                &mut sys,
                comp.nodes[0],
                comp.nodes[1],
                // Y = jωC
                Complex::new(0.0, omega * c),
            ),
            Device::Inductor(l) => {
                let row = branch_row.expect("inductor branch");
                stamp_branch(&mut sys, comp.nodes[0], comp.nodes[1], row);
                // v − jωL·i = 0
                sys.add(row, row, Complex::new(0.0, -omega * l));
            }
            Device::VoltageSource { ac, .. } => {
                let row = branch_row.expect("source branch");
                stamp_branch(&mut sys, comp.nodes[0], comp.nodes[1], row);
                let phase = ac.phase_deg.to_radians();
                sys.add_rhs(
                    row,
                    Complex::new(ac.magnitude * phase.cos(), ac.magnitude * phase.sin()),
                );
            }
            Device::CurrentSource { ac, .. } => {
                let phase = ac.phase_deg.to_radians();
                stamp_current(
                    &mut sys,
                    comp.nodes[0],
                    comp.nodes[1],
                    Complex::new(ac.magnitude * phase.cos(), ac.magnitude * phase.sin()),
                );
            }
            Device::Diode { is, n: emission } => {
                let vd = node_v(&x, comp.nodes[0]) - node_v(&x, comp.nodes[1]);
                let lin = eval_diode(*is, *emission, vd);
                stamp_conductance(
                    &mut sys,
                    comp.nodes[0],
                    comp.nodes[1],
                    Complex::new(lin.gds, 0.0),
                );
            }
            Device::Mosfet(params) => {
                let (d, g, s) = (comp.nodes[0], comp.nodes[1], comp.nodes[2]);
                let vgs = node_v(&x, g) - node_v(&x, s);
                let vds = node_v(&x, d) - node_v(&x, s);
                let lin = eval_mosfet(params, vgs, vds);
                stamp_conductance(&mut sys, d, s, Complex::new(lin.gds, 0.0));
                stamp_vccs(&mut sys, d, s, g, s, Complex::new(lin.gm, 0.0));
            }
        }
    }

    let x = sys.solve()?;
    let mut voltages = vec![(0.0, 0.0); n + 1];
    for (i, z) in x.iter().take(n).enumerate() {
        voltages[i + 1] = (z.re, z.im);
    }
    Ok(AcSample { freq_hz, voltages })
}

/// Frequency points for a logarithmic sweep.
pub fn log_sweep(start_hz: f64, stop_hz: f64, points_per_decade: usize) -> Vec<f64> {
    if start_hz <= 0.0 || stop_hz <= start_hz || points_per_decade == 0 {
        return vec![start_hz.max(1e-9)];
    }
    let decades = (stop_hz / start_hz).log10();
    let total = (decades * points_per_decade as f64).round().max(1.0) as usize;
    (0..=total)
        .map(|i| start_hz * 10f64.powf(decades * i as f64 / total as f64))
        .collect()
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::netlist::{AcSpec, Device, MosKind, MosParams, Waveform};

    fn dc_source(v: f64) -> Device {
        Device::VoltageSource {
            dc: v,
            waveform: Waveform::Dc,
            ac: AcSpec::default(),
        }
    }

    fn ac_source(v: f64) -> Device {
        Device::VoltageSource {
            dc: 0.0,
            waveform: Waveform::Dc,
            ac: AcSpec {
                magnitude: v,
                phase_deg: 0.0,
            },
        }
    }

    #[test]
    fn a_voltage_divider_divides() {
        let mut net = Netlist::new();
        net.add("V1", dc_source(10.0), &["in", "0"]);
        net.add("R1", Device::Resistor(1e3), &["in", "mid"]);
        net.add("R2", Device::Resistor(1e3), &["mid", "0"]);

        let op = solve_dc(&net).unwrap();
        let mid = net.node_index("mid").unwrap();
        // GMIN (1e-12 S against 1e-3 S resistors) sets a ~1e-9 relative
        // accuracy floor; asking for tighter than that is asking the solver to
        // beat a deliberate modelling choice.
        assert!((op.v(mid) - 5.0).abs() < 1e-6, "mid = {}", op.v(mid));
        // The source supplies 5 mA, flowing out of its positive terminal.
        assert!((op.branch_currents[0].abs() - 5e-3).abs() < 1e-8);
    }

    #[test]
    fn an_unequal_divider_divides_unequally() {
        let mut net = Netlist::new();
        net.add("V1", dc_source(12.0), &["in", "0"]);
        net.add("R1", Device::Resistor(3e3), &["in", "mid"]);
        net.add("R2", Device::Resistor(1e3), &["mid", "0"]);
        let op = solve_dc(&net).unwrap();
        // 12 × 1k/(3k+1k) = 3 V
        assert!((op.v(net.node_index("mid").unwrap()) - 3.0).abs() < 1e-6);
    }

    #[test]
    fn a_current_source_into_a_resistor_makes_ohms_law() {
        let mut net = Netlist::new();
        net.add(
            "I1",
            Device::CurrentSource {
                dc: 1e-3,
                waveform: Waveform::Dc,
                ac: AcSpec::default(),
            },
            &["0", "out"],
        );
        net.add("R1", Device::Resistor(1e3), &["out", "0"]);
        let op = solve_dc(&net).unwrap();
        assert!((op.v(net.node_index("out").unwrap()) - 1.0).abs() < 1e-6);
    }

    #[test]
    fn a_floating_node_does_not_make_the_matrix_singular() {
        // GMIN's whole job. Without it this circuit has no DC path to ground
        // from `island` and the solve fails.
        let mut net = Netlist::new();
        net.add("V1", dc_source(5.0), &["in", "0"]);
        net.add("R1", Device::Resistor(1e3), &["in", "0"]);
        net.node("island");
        let op = solve_dc(&net);
        assert!(op.is_ok(), "{:?}", op.err());
    }

    #[test]
    fn a_diode_clamps_near_its_forward_drop() {
        let mut net = Netlist::new();
        net.add("V1", dc_source(5.0), &["in", "0"]);
        net.add("R1", Device::Resistor(1e3), &["in", "a"]);
        net.add("D1", Device::Diode { is: 1e-14, n: 1.0 }, &["a", "0"]);

        let op = solve_dc(&net).unwrap();
        let va = op.v(net.node_index("a").unwrap());
        // ~4.3 mA through 1k leaves ~0.7 V across the diode.
        assert!((0.6..0.8).contains(&va), "diode sat at {va} V");
        // KCL: the resistor current must equal the diode current.
        let i_r = (5.0 - va) / 1e3;
        let i_d = eval_diode(1e-14, 1.0, va).current;
        assert!((i_r - i_d).abs() / i_r < 1e-3, "KCL violated: {i_r} vs {i_d}");
        assert!(op.iterations > 1, "a nonlinear circuit should iterate");
    }

    #[test]
    fn a_reverse_biased_diode_blocks() {
        let mut net = Netlist::new();
        net.add("V1", dc_source(-5.0), &["in", "0"]);
        net.add("R1", Device::Resistor(1e3), &["in", "a"]);
        net.add("D1", Device::Diode { is: 1e-14, n: 1.0 }, &["a", "0"]);
        let op = solve_dc(&net).unwrap();
        // Essentially all the voltage is dropped across the diode.
        assert!(op.v(net.node_index("a").unwrap()) < -4.9);
    }

    #[test]
    fn two_diodes_in_series_drop_twice() {
        let mut net = Netlist::new();
        net.add("V1", dc_source(5.0), &["in", "0"]);
        net.add("R1", Device::Resistor(1e3), &["in", "a"]);
        net.add("D1", Device::Diode { is: 1e-14, n: 1.0 }, &["a", "b"]);
        net.add("D2", Device::Diode { is: 1e-14, n: 1.0 }, &["b", "0"]);
        let op = solve_dc(&net).unwrap();
        let va = op.v(net.node_index("a").unwrap());
        assert!((1.2..1.5).contains(&va), "two drops gave {va} V");
    }

    #[test]
    fn a_common_source_amplifier_biases_where_the_square_law_says() {
        let params = MosParams {
            kind: MosKind::Nmos,
            w_um: 20.0,
            l_um: 1.0,
            vth: 0.7,
            k_prime: 46e-6,
            lambda: 0.0,
        };
        let mut net = Netlist::new();
        net.add("VDD", dc_source(5.0), &["vdd", "0"]);
        net.add("VG", dc_source(1.5), &["g", "0"]);
        net.add("RD", Device::Resistor(10e3), &["vdd", "d"]);
        net.add("M1", Device::Mosfet(params), &["d", "g", "0"]);

        let op = solve_dc(&net).unwrap();
        let vd = op.v(net.node_index("d").unwrap());

        // Hand check: Vov = 0.8, β = 46µ × 20 = 920 µA/V²,
        // Id = ½ × 920µ × 0.64 = 294.4 µA, Vd = 5 − 294.4µ × 10k = 2.056 V.
        let expected = 5.0 - 0.5 * 920e-6 * 0.8 * 0.8 * 10e3;
        assert!((vd - expected).abs() < 0.02, "Vd = {vd}, expected {expected}");
        assert!(vd > 1.5 - 0.7, "must still be in saturation");
    }

    #[test]
    fn a_transistor_below_threshold_pulls_no_current() {
        let params = MosParams {
            kind: MosKind::Nmos,
            w_um: 20.0,
            l_um: 1.0,
            vth: 0.7,
            k_prime: 46e-6,
            lambda: 0.0,
        };
        let mut net = Netlist::new();
        net.add("VDD", dc_source(5.0), &["vdd", "0"]);
        net.add("VG", dc_source(0.3), &["g", "0"]);
        net.add("RD", Device::Resistor(10e3), &["vdd", "d"]);
        net.add("M1", Device::Mosfet(params), &["d", "g", "0"]);
        let op = solve_dc(&net).unwrap();
        // Off: the drain sits at the supply.
        assert!((op.v(net.node_index("d").unwrap()) - 5.0).abs() < 1e-3);
    }

    #[test]
    fn a_cmos_inverter_switches() {
        // Both transistors come off the same simulated process.
        let nmos = MosParams::from_process(MosKind::Nmos, 450.0, 1.0, 10.0, 1.0);
        let pmos = MosParams::from_process(MosKind::Pmos, 450.0, 1.0, 25.0, 1.0);
        let build = |vin: f64| {
            let mut net = Netlist::new();
            net.add("VDD", dc_source(5.0), &["vdd", "0"]);
            net.add("VIN", dc_source(vin), &["in", "0"]);
            net.add("MN", Device::Mosfet(nmos), &["out", "in", "0"]);
            net.add("MP", Device::Mosfet(pmos), &["out", "in", "vdd"]);
            net
        };

        let low = build(0.0);
        let op = solve_dc(&low).unwrap();
        let out_low_in = op.v(low.node_index("out").unwrap());
        assert!(out_low_in > 4.5, "input low should give output high, got {out_low_in}");

        let high = build(5.0);
        let op = solve_dc(&high).unwrap();
        let out_high_in = op.v(high.node_index("out").unwrap());
        assert!(out_high_in < 0.5, "input high should give output low, got {out_high_in}");
    }

    #[test]
    fn rc_charging_follows_the_exponential() {
        let (r, c) = (1e3, 1e-6);
        let tau = r * c; // 1 ms
        let mut net = Netlist::new();
        net.add(
            "V1",
            Device::VoltageSource {
                dc: 0.0,
                waveform: Waveform::Step {
                    from: 0.0,
                    to: 1.0,
                    t_edge: 0.0,
                    rise: 0.0,
                },
                ac: AcSpec::default(),
            },
            &["in", "0"],
        );
        net.add("R1", Device::Resistor(r), &["in", "out"]);
        net.add("C1", Device::Capacitor(c), &["out", "0"]);

        let out = net.node_index("out").unwrap();
        // Backward Euler is first-order, so keep h well under tau.
        let dt = tau / 2000.0;
        let mut tr = Transient::start(&net, dt, 5.0 * tau).unwrap();
        let mut at_tau = None;
        let mut last = 0.0;
        while let Some(sample) = tr.step() {
            let s = sample.unwrap();
            last = s.voltages[out];
            if at_tau.is_none() && s.t >= tau {
                at_tau = Some(last);
            }
        }
        // One time constant charges to 1 − 1/e = 63.2%.
        let v_tau = at_tau.expect("should have passed one tau");
        assert!((v_tau - 0.632).abs() < 0.01, "v(tau) = {v_tau}");
        // Five time constants is 99.3%.
        assert!((last - 0.993).abs() < 0.01, "v(5tau) = {last}");
    }

    #[test]
    fn transient_streams_rather_than_batching() {
        // The samples must be available one at a time, in order, as produced.
        let mut net = Netlist::new();
        net.add("V1", dc_source(1.0), &["in", "0"]);
        net.add("R1", Device::Resistor(1e3), &["in", "out"]);
        net.add("C1", Device::Capacitor(1e-9), &["out", "0"]);

        let mut tr = Transient::start(&net, 1e-7, 1e-5).unwrap();
        let first = tr.step().unwrap().unwrap();
        assert!(tr.progress() > 0.0 && tr.progress() < 1.0);
        let second = tr.step().unwrap().unwrap();
        assert!(second.t > first.t, "time must advance");

        let rest = tr.count();
        assert!(rest > 90, "should be ~100 steps total, {rest} remained");
    }

    #[test]
    fn an_lr_circuit_rises_on_its_own_time_constant() {
        let (r, l) = (100.0, 0.1); // tau = L/R = 1 ms
        let tau = l / r;
        let mut net = Netlist::new();
        net.add(
            "V1",
            Device::VoltageSource {
                dc: 0.0,
                waveform: Waveform::Step {
                    from: 0.0,
                    to: 1.0,
                    t_edge: 0.0,
                    rise: 0.0,
                },
                ac: AcSpec::default(),
            },
            &["in", "0"],
        );
        net.add("R1", Device::Resistor(r), &["in", "a"]);
        net.add("L1", Device::Inductor(l), &["a", "0"]);

        let mut tr = Transient::start(&net, tau / 2000.0, tau).unwrap();
        let mut last = None;
        while let Some(s) = tr.step() {
            last = Some(s.unwrap());
        }
        // Inductor current reaches 63.2% of V/R after one tau.
        let i = last.unwrap().branch_currents;
        let i_l = i.iter().map(|v| v.abs()).fold(0.0, f64::max);
        assert!((i_l - 0.00632).abs() < 2e-4, "i(tau) = {i_l}");
    }

    #[test]
    fn an_rc_lowpass_rolls_off_at_its_corner() {
        let (r, c) = (1e3, 1e-6);
        let fc = 1.0 / (std::f64::consts::TAU * r * c); // ~159.15 Hz
        let mut net = Netlist::new();
        net.add("V1", ac_source(1.0), &["in", "0"]);
        net.add("R1", Device::Resistor(r), &["in", "out"]);
        net.add("C1", Device::Capacitor(c), &["out", "0"]);

        let op = solve_dc(&net).unwrap();
        let out = net.node_index("out").unwrap();

        // At the corner: −3.01 dB and −45°.
        let at_fc = solve_ac(&net, &op, fc).unwrap();
        assert!((at_fc.db(out) + 3.01).abs() < 0.05, "{} dB", at_fc.db(out));
        assert!((at_fc.phase_deg(out) + 45.0).abs() < 0.5, "{}°", at_fc.phase_deg(out));

        // A decade up: −20 dB/decade and approaching −90°.
        let decade = solve_ac(&net, &op, fc * 10.0).unwrap();
        assert!((decade.db(out) + 20.04).abs() < 0.2, "{} dB", decade.db(out));
        assert!(decade.phase_deg(out) < -80.0);

        // Well below the corner it passes unattenuated.
        let low = solve_ac(&net, &op, fc / 100.0).unwrap();
        assert!(low.db(out).abs() < 0.01);
    }

    #[test]
    fn an_lc_tank_resonates_where_theory_says() {
        let (l, c): (f64, f64) = (1e-3, 1e-6);
        let f0 = 1.0 / (std::f64::consts::TAU * (l * c).sqrt()); // ~5033 Hz
        let mut net = Netlist::new();
        net.add("V1", ac_source(1.0), &["in", "0"]);
        net.add("R1", Device::Resistor(50.0), &["in", "out"]);
        net.add("L1", Device::Inductor(l), &["out", "0"]);
        net.add("C1", Device::Capacitor(c), &["out", "0"]);

        let op = solve_dc(&net).unwrap();
        let out = net.node_index("out").unwrap();
        let peak = solve_ac(&net, &op, f0).unwrap().db(out);
        let below = solve_ac(&net, &op, f0 / 8.0).unwrap().db(out);
        let above = solve_ac(&net, &op, f0 * 8.0).unwrap().db(out);
        assert!(peak > below + 15.0 && peak > above + 15.0, "no peak at f0");
    }

    #[test]
    fn log_sweep_spans_the_requested_decades() {
        let f = log_sweep(10.0, 100_000.0, 10);
        assert!((f[0] - 10.0).abs() < 1e-9);
        assert!((f[f.len() - 1] - 100_000.0).abs() / 100_000.0 < 1e-9);
        assert_eq!(f.len(), 41, "4 decades at 10/decade, inclusive");
        // Evenly spaced in log.
        let r0 = f[1] / f[0];
        let r1 = f[20] / f[19];
        assert!((r0 - r1).abs() < 1e-9);
    }

    #[test]
    fn a_shorted_source_is_reported_as_singular_not_a_panic() {
        // Two ideal sources in parallel with different voltages is genuinely
        // unsolvable; it must be an error, not a crash or silent nonsense.
        let mut net = Netlist::new();
        net.add("V1", dc_source(5.0), &["a", "0"]);
        net.add("V2", dc_source(3.0), &["a", "0"]);
        assert!(matches!(solve_dc(&net), Err(SimError::Singular(_))));
    }

    #[test]
    fn errors_explain_themselves() {
        let e = SimError::NoConvergence {
            iterations: 200,
            worst_delta: 1.5e-3,
            at_unknown: 4,
        };
        let msg = e.to_string();
        assert!(msg.contains("200") && msg.contains("unknown 4"));
    }
}
