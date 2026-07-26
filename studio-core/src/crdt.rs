//! A schematic document that merges like source code.
//!
//! Studio's own framing is the spec: *"a document model that can be diffed and
//! merged like source code and at the same time real-time collaborative...
//! this is the foundation everything else sits on."* Two replicas edit
//! independently, offline or concurrently, and `Document::merge` must combine
//! their histories into the same result no matter what order the merges happen
//! in — that is the whole content of being a CRDT, and it is what makes
//! peer-to-peer sync (no server arbitrating who's "right") possible at all.
//!
//! # The two primitives
//!
//! Every field in the document is one of two CRDT types, chosen per the shape
//! of the conflict it has to resolve:
//!
//! - [`LwwRegister<T>`] — "what is this component's resistance?" has exactly one
//!   right answer at any instant, so concurrent writes resolve by picking one:
//!   whichever carries the later [`OpId`]. Because [`OpId`] is a total order
//!   (Lamport counter, tie-broken by site), this is deterministic — both
//!   replicas land on the same winner without discussion.
//! - [`OrSet<T>`] — "does this component still exist?" is not a single value to
//!   overwrite, it is *membership*, and membership has a conflict LWW can't
//!   express: replica A deletes a component while replica B, unaware, is
//!   simultaneously wiring a new connection to it. Whose edit should win?
//!   Observed-Remove semantics (Shapiro et al.) answer: a remove only tombstones
//!   the *specific add-tags it has seen*. B's concurrent add carries a tag A
//!   never observed, so it survives — "add-wins," the behaviour most editors
//!   actually want (nothing you didn't know was being deleted disappears out
//!   between your feet).
//!
//! # What this is not
//!
//! This is the CRDT and its merge algebra, proven by the property tests below.
//! It is not a network protocol — no transport, no wire format, no op log
//! shipped between peers. `Document` is the unit two replicas would exchange
//! and merge; how they exchange it is a `/studio` UI concern.
//!
//! [`Document::to_text`] is a second, weaker claim: a deterministic,
//! sorted-by-entity text projection of the *current* values, so that changing
//! one field changes one line — useful for `git diff` and for humans, but it
//! carries no [`OpId`]s and cannot itself be merged. The CRDT state is the
//! thing that merges; the text is what you'd commit next to it.

use std::collections::{BTreeMap, BTreeSet};

use serde::{Deserialize, Serialize};

use crate::netlist::{AcSpec, Device, MosKind, MosParams, Netlist, Waveform};

/// Identifies a component (or, reserved, the ground reference) across replicas.
///
/// `Document` never allocates these itself — an ID must be unique across every
/// replica that might edit concurrently, and a shared struct with no network
/// access has no way to coordinate that. The recommended scheme, matching how
/// [`SiteClock`] already splits a site id from a counter, is
/// `(site_id << 32) | local_counter`: every replica owns a disjoint range
/// without ever talking to another one.
pub type EntityId = u64;

/// Entity id reserved for the ground reference. A terminal on entity `0` always
/// resolves to the netlist's ground node, regardless of what it is wired to —
/// this is a document convention rather than a distinguished CRDT type, so
/// real components should avoid id `0`.
pub const GROUND_ENTITY: EntityId = 0;

// ---------------------------------------------------------------------------
// Causality: OpId and the clock that produces them
// ---------------------------------------------------------------------------

/// A causal timestamp: a per-site Lamport counter, tie-broken by site id.
///
/// The tie-break is what turns "mostly ordered" into a genuine total order —
/// two sites can tick the same counter value, but never the same `(counter,
/// site)` pair, so every [`LwwRegister`] merge has an unambiguous winner. Field
/// order matters: deriving `Ord` compares `counter` first, `site` only to break
/// ties, which is exactly "later logical time wins, and among equal logical
/// times the same site always wins."
#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Hash, Serialize, Deserialize)]
pub struct OpId {
    /// Lamport counter: strictly increasing per site.
    pub counter: u64,
    /// Which replica produced this op.
    pub site: u64,
}

/// Produces monotonically increasing [`OpId`]s for one replica.
///
/// A Lamport clock, not a wall clock: `observe` folds in the counter of an op
/// received from elsewhere so that any op *this* replica produces afterwards is
/// provably causally later than everything it has seen — the property that
/// makes "later `OpId`" a meaningful stand-in for "later edit" at all.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct SiteClock {
    site: u64,
    counter: u64,
}

impl SiteClock {
    /// A fresh clock for site `site`. Caller picks a site id unique among every
    /// replica that might ever merge with this one.
    pub fn new(site: u64) -> Self {
        SiteClock { site, counter: 0 }
    }

    /// Produce the next op id for a local edit.
    pub fn tick(&mut self) -> OpId {
        self.counter += 1;
        OpId {
            counter: self.counter,
            site: self.site,
        }
    }

    /// Fold in an op id observed from another replica, so subsequent local ops
    /// are guaranteed to be causally later than it.
    pub fn observe(&mut self, seen: OpId) {
        self.counter = self.counter.max(seen.counter);
    }
}

// ---------------------------------------------------------------------------
// Last-write-wins register
// ---------------------------------------------------------------------------

/// A single mergeable value: the write carrying the greatest [`OpId`] wins.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct LwwRegister<T> {
    /// Current value.
    pub value: T,
    /// The op that set it.
    pub stamp: OpId,
}

impl<T: Clone + PartialEq> LwwRegister<T> {
    /// A register initialised by op `stamp`.
    pub fn new(value: T, stamp: OpId) -> Self {
        LwwRegister { value, stamp }
    }

    /// Overwrite locally, if `stamp` is causally later than the current value.
    pub fn set(&mut self, value: T, stamp: OpId) {
        if stamp > self.stamp {
            self.value = value;
            self.stamp = stamp;
        }
    }

    /// Merge in another replica's register.
    ///
    /// Commutative and idempotent by construction: this keeps whichever `stamp`
    /// is greater, and "greater of two totally-ordered values" doesn't care
    /// which side you call it from, nor how many times you ask.
    pub fn merge(&mut self, other: &Self) {
        if other.stamp > self.stamp {
            self.value = other.value.clone();
            self.stamp = other.stamp;
        }
    }
}

// ---------------------------------------------------------------------------
// Observed-remove set
// ---------------------------------------------------------------------------

/// An add-wins set: membership merges without a coordinator, and a remove
/// racing a concurrent add loses.
///
/// Every `add` mints a fresh tag ([`OpId`]); the element is present as long as
/// any of its tags survives. `remove` tombstones only the tags *this replica
/// currently sees* — never "every tag anyone will ever mint" — which is the
/// entire mechanism: a concurrent add elsewhere carries a tag this replica
/// hasn't observed, so removing what you can see cannot tombstone it, and after
/// merge the element is back.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct OrSet<T: Ord> {
    adds: BTreeMap<T, BTreeSet<OpId>>,
    tombstones: BTreeSet<OpId>,
}

impl<T: Ord + Clone> Default for OrSet<T> {
    // Written by hand rather than `#[derive(Default)]`: the derive macro adds a
    // `T: Default` bound to the generated impl even though nothing here needs
    // one, which would make `OrSet<Connection>::default()` fail to compile since
    // `Connection` has no `Default`.
    fn default() -> Self {
        OrSet {
            adds: BTreeMap::new(),
            tombstones: BTreeSet::new(),
        }
    }
}

impl<T: Ord + Clone> OrSet<T> {
    /// An empty set.
    pub fn new() -> Self {
        Self::default()
    }

    /// Add `value`, tagged with a fresh op. Adding an already-present value
    /// again is not a no-op — it mints a second tag, which is what lets the
    /// value survive a remove that only observed the first one.
    pub fn add(&mut self, value: T, stamp: OpId) {
        self.adds.entry(value).or_default().insert(stamp);
    }

    /// Remove `value`, tombstoning every tag currently visible to this replica.
    /// A tag minted concurrently elsewhere, not yet visible here, is untouched
    /// — that is the add-wins guarantee, not a bug in this method.
    pub fn remove(&mut self, value: &T) {
        if let Some(tags) = self.adds.get(value) {
            self.tombstones.extend(tags.iter().copied());
        }
    }

    /// Whether `value` has any live (non-tombstoned) tag.
    pub fn contains(&self, value: &T) -> bool {
        self.adds
            .get(value)
            .is_some_and(|tags| tags.iter().any(|t| !self.tombstones.contains(t)))
    }

    /// Every value with at least one live tag, in `T`'s order.
    pub fn values(&self) -> impl Iterator<Item = &T> {
        self.adds
            .iter()
            .filter(|(_, tags)| tags.iter().any(|t| !self.tombstones.contains(t)))
            .map(|(v, _)| v)
    }

    /// The greatest counter among every tag this set has ever seen, adds and
    /// tombstones alike — used to advance a [`SiteClock`] past a merge so the
    /// next local edit is provably causally later than everything just merged
    /// in.
    pub fn max_counter(&self) -> u64 {
        self.adds
            .values()
            .flatten()
            .chain(self.tombstones.iter())
            .map(|op| op.counter)
            .max()
            .unwrap_or(0)
    }

    /// Merge in another replica's set: union the tags, union the tombstones.
    ///
    /// Commutative and associative because set union is; idempotent because
    /// union with a subset of yourself changes nothing. No coordination and no
    /// conflict — that is the entire point of choosing a set CRDT for
    /// membership instead of trying to make LWW answer "does it exist?".
    pub fn merge(&mut self, other: &Self) {
        for (value, tags) in &other.adds {
            self.adds.entry(value.clone()).or_default().extend(tags.iter().copied());
        }
        self.tombstones.extend(other.tombstones.iter().copied());
    }
}

// ---------------------------------------------------------------------------
// Connections
// ---------------------------------------------------------------------------

/// A terminal: a component and a 0-based pin index on it.
pub type Terminal = (EntityId, u8);

/// A wire between two terminals.
///
/// Endpoints are stored in a canonical order (`a <= b`) so that "connect A to
/// B" and "connect B to A" produce the identical value — without that, an
/// [`OrSet`] would treat them as two different elements and the same wire drawn
/// by two replicas from opposite ends would show up twice.
#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Serialize, Deserialize)]
pub struct Connection {
    /// The lesser terminal.
    pub a: Terminal,
    /// The greater terminal.
    pub b: Terminal,
}

impl Connection {
    /// Build a connection, normalising endpoint order.
    pub fn new(a: Terminal, b: Terminal) -> Self {
        if a <= b {
            Connection { a, b }
        } else {
            Connection { a: b, b: a }
        }
    }
}

// ---------------------------------------------------------------------------
// Components
// ---------------------------------------------------------------------------

/// What kind of device a component is. Kept separate from [`Device`] (which
/// also carries fully-resolved parameters) so a schematic can represent, say, a
/// resistor mid-edit without every field being valid yet.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
pub enum ComponentKind {
    /// `params: [resistance_ohms]`
    Resistor,
    /// `params: [capacitance_farads]`
    Capacitor,
    /// `params: [inductance_henries]`
    Inductor,
    /// `params: [dc_volts]`
    VoltageSource,
    /// `params: [dc_amps]`
    CurrentSource,
    /// `params: [saturation_current_amps, emission_coefficient]`
    Diode,
    /// `params: [width_um, length_um, vth_volts, k_prime, lambda]`
    Nmos,
    /// `params: [width_um, length_um, vth_volts, k_prime, lambda]`
    Pmos,
}

/// A component's mergeable fields.
///
/// Each field is its own [`LwwRegister`] rather than the struct being one big
/// register. That granularity is the reason two replicas moving a resistor's
/// `x` and editing its `params` at the same time both survive: with one
/// register per struct, the later write would silently discard the earlier
/// one's *entire* edit, position included, even though the two edits never
/// touched the same information.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct ComponentFields {
    /// What the component is.
    pub kind: LwwRegister<ComponentKind>,
    /// Reference designator, e.g. `"R1"`.
    pub name: LwwRegister<String>,
    /// Canvas x.
    pub x: LwwRegister<f64>,
    /// Canvas y.
    pub y: LwwRegister<f64>,
    /// Device parameters, meaning dependent on `kind` (documented on each
    /// [`ComponentKind`] variant).
    pub params: LwwRegister<Vec<f64>>,
}

impl ComponentFields {
    fn max_counter(&self) -> u64 {
        [
            self.kind.stamp.counter,
            self.name.stamp.counter,
            self.x.stamp.counter,
            self.y.stamp.counter,
            self.params.stamp.counter,
        ]
        .into_iter()
        .max()
        .unwrap_or(0)
    }

    fn merge(&mut self, other: &Self) {
        self.kind.merge(&other.kind);
        self.name.merge(&other.name);
        self.x.merge(&other.x);
        self.y.merge(&other.y);
        self.params.merge(&other.params);
    }

    fn terminal_count(&self) -> usize {
        match self.kind.value {
            ComponentKind::Nmos | ComponentKind::Pmos => 3,
            _ => 2,
        }
    }
}

// ---------------------------------------------------------------------------
// Document
// ---------------------------------------------------------------------------

/// A schematic: the whole mergeable state.
///
/// `components` is the existence CRDT ([`OrSet`]); `fields` carries each live
/// component's actual data. A component removed from `components` is not
/// purged from `fields` on merge — its registers are harmless once nothing
/// iterates them, and pruning would need a garbage-collection pass with its own
/// causal-safety argument (this is the standard CRDT tombstone trade-off, not
/// an oversight).
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize, Default)]
pub struct Document {
    /// Which components exist.
    pub components: OrSet<EntityId>,
    /// Each component's data, keyed by id. May contain entries for ids no
    /// longer in `components` — see the struct docs.
    pub fields: BTreeMap<EntityId, ComponentFields>,
    /// Wires between terminals.
    pub connections: OrSet<Connection>,
}

impl Document {
    /// An empty schematic.
    pub fn new() -> Self {
        Self::default()
    }

    /// Place a component. `id` must not collide with another replica's id —
    /// see [`EntityId`]'s docs for the recommended scheme.
    pub fn add_component(
        &mut self,
        id: EntityId,
        kind: ComponentKind,
        name: &str,
        x: f64,
        y: f64,
        params: Vec<f64>,
        clock: &mut SiteClock,
    ) {
        let stamp = clock.tick();
        self.components.add(id, stamp);
        self.fields.insert(
            id,
            ComponentFields {
                kind: LwwRegister::new(kind, stamp),
                name: LwwRegister::new(name.to_string(), stamp),
                x: LwwRegister::new(x, stamp),
                y: LwwRegister::new(y, stamp),
                params: LwwRegister::new(params, stamp),
            },
        );
    }

    /// Remove a component. Any wire still referencing it is simply ignored by
    /// [`Document::to_netlist`] rather than needing its own removal.
    pub fn remove_component(&mut self, id: EntityId, clock: &mut SiteClock) {
        // `remove` doesn't need the tick for its own tombstones (it reuses the
        // add-tags it can see), but ticking keeps this replica's clock ahead of
        // an edit a human will perceive as happening now — consistent with
        // every other mutating method here ticking on call.
        let _ = clock.tick();
        self.components.remove(&id);
    }

    /// Move a component. A no-op if `id` doesn't exist (or was removed) here.
    pub fn move_component(&mut self, id: EntityId, x: f64, y: f64, clock: &mut SiteClock) {
        if let Some(f) = self.fields.get_mut(&id) {
            let stamp = clock.tick();
            f.x.set(x, stamp);
            f.y.set(y, stamp);
        }
    }

    /// Replace a component's device parameters.
    pub fn set_params(&mut self, id: EntityId, params: Vec<f64>, clock: &mut SiteClock) {
        if let Some(f) = self.fields.get_mut(&id) {
            f.params.set(params, clock.tick());
        }
    }

    /// Rename a component's reference designator.
    pub fn rename(&mut self, id: EntityId, name: &str, clock: &mut SiteClock) {
        if let Some(f) = self.fields.get_mut(&id) {
            f.name.set(name.to_string(), clock.tick());
        }
    }

    /// Wire two terminals together.
    pub fn connect(&mut self, a: Terminal, b: Terminal, clock: &mut SiteClock) {
        let stamp = clock.tick();
        self.connections.add(Connection::new(a, b), stamp);
    }

    /// Remove a specific wire.
    pub fn disconnect(&mut self, a: Terminal, b: Terminal, clock: &mut SiteClock) {
        let _ = clock.tick();
        self.connections.remove(&Connection::new(a, b));
    }

    /// Every live component, in id order.
    pub fn live_components(&self) -> impl Iterator<Item = (EntityId, &ComponentFields)> {
        self.components
            .values()
            .filter_map(move |id| self.fields.get(id).map(|f| (*id, f)))
    }

    /// The greatest [`OpId`] counter anywhere in this document — across every
    /// component's fields and both `OrSet`s. Intended for advancing a
    /// [`SiteClock`] past a merge (`clock.observe(OpId{counter:
    /// doc.max_counter(), site: 0})`; the site half of that `OpId` is ignored by
    /// [`SiteClock::observe`], which only reads the counter).
    pub fn max_counter(&self) -> u64 {
        let fields_max = self.fields.values().map(ComponentFields::max_counter).max().unwrap_or(0);
        fields_max
            .max(self.components.max_counter())
            .max(self.connections.max_counter())
    }

    /// Merge another replica's document into this one.
    ///
    /// Field-by-field: the existence sets merge as sets, and each component's
    /// fields merge as registers — proven commutative, associative and
    /// idempotent by the property tests below, since it composes only
    /// operations that are each individually so.
    pub fn merge(&mut self, other: &Self) {
        self.components.merge(&other.components);
        self.connections.merge(&other.connections);
        for (id, other_fields) in &other.fields {
            match self.fields.get_mut(id) {
                Some(existing) => existing.merge(other_fields),
                None => {
                    self.fields.insert(*id, other_fields.clone());
                }
            }
        }
    }

    /// A deterministic text snapshot: one line per field, one line per wire,
    /// sorted by entity id then by connection order. See the module docs for
    /// what this claim does and does not cover.
    pub fn to_text(&self) -> String {
        let mut lines = Vec::new();
        for (id, f) in self.live_components() {
            lines.push(format!("component {id} kind={:?}", f.kind.value));
            lines.push(format!("component {id} name={}", f.name.value));
            lines.push(format!("component {id} x={}", f.x.value));
            lines.push(format!("component {id} y={}", f.y.value));
            lines.push(format!("component {id} params={:?}", f.params.value));
        }
        for c in self.connections.values() {
            lines.push(format!(
                "connection {}.{}-{}.{}",
                c.a.0, c.a.1, c.b.0, c.b.1
            ));
        }
        lines.join("\n")
    }

    /// Lower this document to a solvable [`Netlist`].
    ///
    /// Connectivity is resolved with union–find over terminals: every terminal
    /// wired transitively to another lands on the same net, named after the
    /// union–find root so the name is stable without an extra enumeration pass.
    /// A terminal on [`GROUND_ENTITY`] always resolves to the netlist's ground
    /// node, by convention — see that constant's docs.
    pub fn to_netlist(&self) -> Result<Netlist, String> {
        let (mut parent, ground_roots) = self.resolve_nets();

        let mut net = Netlist::new();
        for (id, fields) in self.live_components() {
            let device = device_from_fields(fields)
                .ok_or_else(|| format!("component {id}: {}", param_error(fields)))?;
            let names: Vec<String> = (0..fields.terminal_count() as u8)
                .map(|pin| {
                    let root = find(&mut parent, (id, pin));
                    if ground_roots.contains(&root) {
                        "0".to_string()
                    } else {
                        format!("net_{}_{}", root.0, root.1)
                    }
                })
                .collect();
            let name_refs: Vec<&str> = names.iter().map(String::as_str).collect();
            net.add(&fields.name.value, device, &name_refs);
        }
        Ok(net)
    }

    /// The netlist node name `terminal` resolves to under the current wiring:
    /// `"0"` if transitively connected to [`GROUND_ENTITY`], otherwise a name
    /// derived from its connected component's union–find root.
    ///
    /// Exposed mainly for labelling nets in a UI (and for tests to check
    /// connectivity) without reconstructing a whole [`Netlist`]; [`to_netlist`]
    /// does the equivalent resolution once for every terminal rather than
    /// rebuilding union–find per call.
    ///
    /// [`to_netlist`]: Document::to_netlist
    pub fn node_name(&self, terminal: Terminal) -> String {
        let (mut parent, ground_roots) = self.resolve_nets();
        let root = find(&mut parent, terminal);
        if ground_roots.contains(&root) {
            "0".to_string()
        } else {
            format!("net_{}_{}", root.0, root.1)
        }
    }

    /// Build the union–find over every wired terminal, plus the set of roots
    /// whose equivalence class contains a [`GROUND_ENTITY`] terminal.
    ///
    /// The ground check is root *membership*, not "is the root itself entity
    /// 0" — union–find gives no guarantee that a class's representative is any
    /// particular member, so which physical terminal a group's root happens to
    /// be depends on connection-iteration order and is not something calling
    /// code may rely on.
    fn resolve_nets(&self) -> (BTreeMap<Terminal, Terminal>, BTreeSet<Terminal>) {
        let mut parent: BTreeMap<Terminal, Terminal> = BTreeMap::new();
        for c in self.connections.values() {
            union(&mut parent, c.a, c.b);
        }
        let mut ground_roots = BTreeSet::new();
        for c in self.connections.values() {
            if c.a.0 == GROUND_ENTITY {
                ground_roots.insert(find(&mut parent, c.a));
            }
            if c.b.0 == GROUND_ENTITY {
                ground_roots.insert(find(&mut parent, c.b));
            }
        }
        (parent, ground_roots)
    }
}

fn param_error(f: &ComponentFields) -> String {
    format!(
        "{:?} needs {} parameters, has {}",
        f.kind.value,
        match f.kind.value {
            ComponentKind::Diode => 2,
            ComponentKind::Nmos | ComponentKind::Pmos => 5,
            _ => 1,
        },
        f.params.value.len()
    )
}

fn device_from_fields(f: &ComponentFields) -> Option<Device> {
    let p = &f.params.value;
    let get = |i: usize| p.get(i).copied();
    Some(match f.kind.value {
        ComponentKind::Resistor => Device::Resistor(get(0)?),
        ComponentKind::Capacitor => Device::Capacitor(get(0)?),
        ComponentKind::Inductor => Device::Inductor(get(0)?),
        // Transient analysis seeds from the DC operating point (the circuit
        // "has always been" at that bias), so a source that is Waveform::Dc --
        // on at every instant, including before t=0 -- produces a flat line: the
        // capacitor or inductor in the circuit was already at steady state.
        // That is the mathematically correct answer, but it is a useless demo
        // of a *streaming solver* if it is also the only thing a freshly-placed
        // source can ever show. A hard step to the DC level at t=0 -- "was off,
        // switched on now" -- is the far more useful default for a schematic
        // editor to start from, and it costs nothing: solve_dc and solve_ac read
        // `dc`/`ac` directly and never consult `waveform` at all (see
        // `analysis::build_system`'s DC branch), so this only changes what a
        // transient run looks like.
        // AC magnitude follows the same reasoning as the waveform above, and
        // mirrors the SPICE "AC 1" convention: a source with no explicit small-
        // signal spec contributes nothing to an AC sweep, which would make
        // every schematic-built circuit's Bode plot a flat line at -infinity
        // dB regardless of topology. Unit magnitude (1.0 ∠ 0°) instead makes
        // the sweep read directly as the circuit's transfer function in dB,
        // which is what a schematic editor's Bode tab should show by default.
        ComponentKind::VoltageSource => Device::VoltageSource {
            dc: get(0)?,
            waveform: Waveform::Step {
                from: 0.0,
                to: get(0)?,
                t_edge: 0.0,
                rise: 0.0,
            },
            ac: AcSpec {
                magnitude: 1.0,
                phase_deg: 0.0,
            },
        },
        ComponentKind::CurrentSource => Device::CurrentSource {
            dc: get(0)?,
            waveform: Waveform::Step {
                from: 0.0,
                to: get(0)?,
                t_edge: 0.0,
                rise: 0.0,
            },
            ac: AcSpec {
                magnitude: 1.0,
                phase_deg: 0.0,
            },
        },
        ComponentKind::Diode => Device::Diode {
            is: get(0)?,
            n: get(1)?,
        },
        ComponentKind::Nmos | ComponentKind::Pmos => Device::Mosfet(MosParams {
            kind: if matches!(f.kind.value, ComponentKind::Nmos) {
                MosKind::Nmos
            } else {
                MosKind::Pmos
            },
            w_um: get(0)?,
            l_um: get(1)?,
            vth: get(2)?,
            k_prime: get(3)?,
            lambda: get(4)?,
        }),
    })
}

// ---------------------------------------------------------------------------
// Union–find over terminals
// ---------------------------------------------------------------------------

fn find(parent: &mut BTreeMap<Terminal, Terminal>, t: Terminal) -> Terminal {
    let mut root = t;
    while let Some(&p) = parent.get(&root) {
        if p == root {
            break;
        }
        root = p;
    }
    // Path compression: point every visited node straight at the root so the
    // next lookup on this chain is O(1) instead of retracing it.
    let mut cur = t;
    while let Some(&p) = parent.get(&cur) {
        if p == root {
            break;
        }
        parent.insert(cur, root);
        cur = p;
    }
    root
}

fn union(parent: &mut BTreeMap<Terminal, Terminal>, a: Terminal, b: Terminal) {
    let ra = find(parent, a);
    let rb = find(parent, b);
    if ra != rb {
        parent.insert(ra, rb);
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::analysis::solve_dc;

    // -- OpId / clock --------------------------------------------------------

    #[test]
    fn opid_orders_by_counter_then_site() {
        assert!(OpId { counter: 1, site: 9 } < OpId { counter: 2, site: 0 });
        assert!(OpId { counter: 5, site: 0 } < OpId { counter: 5, site: 1 });
    }

    #[test]
    fn clock_ticks_are_strictly_increasing() {
        let mut c = SiteClock::new(7);
        let a = c.tick();
        let b = c.tick();
        assert!(b > a);
        assert_eq!(a.site, 7);
    }

    #[test]
    fn observing_a_later_op_advances_the_clock_past_it() {
        let mut c = SiteClock::new(1);
        c.tick(); // counter = 1
        c.observe(OpId { counter: 100, site: 99 });
        let next = c.tick();
        assert!(next.counter > 100, "must tick past the observed op");
    }

    // -- LwwRegister -----------------------------------------------------------

    #[test]
    fn lww_keeps_the_later_stamp_regardless_of_merge_direction() {
        let early = LwwRegister::new(1, OpId { counter: 1, site: 1 });
        let late = LwwRegister::new(2, OpId { counter: 2, site: 1 });

        let mut a = early.clone();
        a.merge(&late);
        let mut b = late.clone();
        b.merge(&early);
        assert_eq!(a, b);
        assert_eq!(a.value, 2);
    }

    #[test]
    fn lww_merge_is_idempotent() {
        let mut r = LwwRegister::new("x".to_string(), OpId { counter: 1, site: 1 });
        let snapshot = r.clone();
        r.merge(&snapshot.clone());
        r.merge(&snapshot);
        assert_eq!(r, LwwRegister::new("x".to_string(), OpId { counter: 1, site: 1 }));
    }

    // -- OrSet -----------------------------------------------------------------

    #[test]
    fn orset_add_wins_over_a_remove_that_did_not_observe_it() {
        // Common ancestor: both replicas have seen tag t1 for "X".
        let mut common = OrSet::new();
        common.add("X", OpId { counter: 1, site: 1 });

        // Replica A removes X, tombstoning only the tag it has seen (t1).
        let mut a = common.clone();
        a.remove(&"X");
        assert!(!a.contains(&"X"));

        // Replica B, unaware of the remove, adds X again — a fresh tag t2.
        let mut b = common.clone();
        b.add("X", OpId { counter: 2, site: 2 });

        // After merging in either order, X survives: t2 was never tombstoned.
        let mut merged_ab = a.clone();
        merged_ab.merge(&b);
        let mut merged_ba = b.clone();
        merged_ba.merge(&a);
        assert_eq!(merged_ab, merged_ba);
        assert!(merged_ab.contains(&"X"), "the concurrent add should win");
    }

    #[test]
    fn a_remove_that_saw_every_tag_actually_removes() {
        let mut s = OrSet::new();
        s.add("X", OpId { counter: 1, site: 1 });
        s.remove(&"X");
        assert!(!s.contains(&"X"));
        assert_eq!(s.values().collect::<Vec<_>>(), Vec::<&&str>::new());
    }

    #[test]
    fn orset_merge_is_commutative_associative_and_idempotent() {
        let mut a = OrSet::new();
        a.add(1u32, OpId { counter: 1, site: 1 });
        a.add(2, OpId { counter: 2, site: 1 });

        let mut b = OrSet::new();
        b.add(2u32, OpId { counter: 3, site: 2 });
        b.remove(&2);
        b.add(3, OpId { counter: 4, site: 2 });

        let mut c = OrSet::new();
        c.add(3u32, OpId { counter: 5, site: 3 });
        c.add(4, OpId { counter: 6, site: 3 });

        // Commutative.
        let mut ab = a.clone();
        ab.merge(&b);
        let mut ba = b.clone();
        ba.merge(&a);
        assert_eq!(ab, ba);

        // Associative: (a+b)+c == a+(b+c).
        let mut ab_c = ab.clone();
        ab_c.merge(&c);
        let mut bc = b.clone();
        bc.merge(&c);
        let mut a_bc = a.clone();
        a_bc.merge(&bc);
        assert_eq!(ab_c, a_bc);

        // Idempotent.
        let mut twice = ab_c.clone();
        twice.merge(&ab_c.clone());
        assert_eq!(twice, ab_c);
    }

    #[test]
    fn two_replicas_adding_the_same_wire_from_opposite_ends_do_not_duplicate() {
        let mut a = OrSet::new();
        a.add(Connection::new((1, 0), (2, 0)), OpId { counter: 1, site: 1 });
        let mut b = OrSet::new();
        b.add(Connection::new((2, 0), (1, 0)), OpId { counter: 1, site: 2 });
        a.merge(&b);
        assert_eq!(a.values().count(), 1, "same wire, opposite endpoint order");
    }

    // -- Document merge properties ----------------------------------------------

    fn r(id: EntityId, name: &str, ohms: f64, x: f64, clock: &mut SiteClock) -> Document {
        let mut d = Document::new();
        d.add_component(id, ComponentKind::Resistor, name, x, 0.0, vec![ohms], clock);
        d
    }

    #[test]
    fn document_merge_is_commutative_associative_and_idempotent() {
        let mut c1 = SiteClock::new(1);
        let mut c2 = SiteClock::new(2);
        let mut c3 = SiteClock::new(3);

        let a = r(1, "R1", 1e3, 0.0, &mut c1);
        let b = r(2, "R2", 2e3, 10.0, &mut c2);
        let c = r(3, "R3", 3e3, 20.0, &mut c3);

        let mut ab = a.clone();
        ab.merge(&b);
        let mut ba = b.clone();
        ba.merge(&a);
        assert_eq!(ab, ba, "commutative");

        let mut ab_c = ab.clone();
        ab_c.merge(&c);
        let mut bc = b.clone();
        bc.merge(&c);
        let mut a_bc = a.clone();
        a_bc.merge(&bc);
        assert_eq!(ab_c, a_bc, "associative");

        let mut twice = ab_c.clone();
        let snapshot = ab_c.clone();
        twice.merge(&snapshot);
        assert_eq!(twice, ab_c, "idempotent");
    }

    #[test]
    fn concurrent_edits_to_different_fields_of_the_same_component_both_survive() {
        let mut clock = SiteClock::new(1);
        let base = r(1, "R1", 1e3, 0.0, &mut clock);

        // Replica A moves the component.
        let mut a = base.clone();
        let mut ca = SiteClock::new(10);
        ca.observe(clock.tick()); // stay causally ahead of the shared base
        a.fields.get_mut(&1).unwrap().x.set(99.0, ca.tick());

        // Replica B, concurrently, edits its value — not its position.
        let mut b = base.clone();
        let mut cb = SiteClock::new(20);
        cb.observe(SiteClock::new(1).tick());
        b.fields.get_mut(&1).unwrap().params.set(vec![5e3], cb.tick());

        let mut merged = a.clone();
        merged.merge(&b);
        let f = &merged.fields[&1];
        assert_eq!(f.x.value, 99.0, "the move must survive");
        assert_eq!(f.params.value, vec![5e3], "the value edit must survive too");
    }

    #[test]
    fn concurrent_edits_to_the_same_field_pick_one_winner_regardless_of_order() {
        let mut clock = SiteClock::new(1);
        let base = r(1, "R1", 1e3, 0.0, &mut clock);

        let mut a = base.clone();
        a.fields
            .get_mut(&1)
            .unwrap()
            .params
            .set(vec![7e3], OpId { counter: 100, site: 1 });

        let mut b = base.clone();
        b.fields
            .get_mut(&1)
            .unwrap()
            .params
            .set(vec![8e3], OpId { counter: 100, site: 2 });

        // Same counter, different sites: site 2 wins the tie-break either way.
        let mut ab = a.clone();
        ab.merge(&b);
        let mut ba = b.clone();
        ba.merge(&a);
        assert_eq!(ab, ba);
        assert_eq!(ab.fields[&1].params.value, vec![8e3]);
    }

    #[test]
    fn max_counter_advances_a_clock_past_a_merge() {
        let mut c1 = SiteClock::new(1);
        let a = r(1, "R1", 1e3, 0.0, &mut c1);

        let mut c2 = SiteClock::new(2);
        // Site 2 ticks far ahead, simulating a replica that has been editing
        // for a while before these two ever meet.
        for _ in 0..50 {
            c2.tick();
        }
        let b = r(2, "R2", 2e3, 0.0, &mut c2);

        let mut merged = a;
        merged.merge(&b);
        assert!(merged.max_counter() >= 51);

        let mut receiver = SiteClock::new(3);
        receiver.observe(OpId {
            counter: merged.max_counter(),
            site: 0,
        });
        let next = receiver.tick();
        assert!(
            next.counter > merged.max_counter(),
            "a local edit after observing a merge must be causally later than everything in it"
        );
    }

    #[test]
    fn move_set_params_and_rename_update_the_right_component_and_no_op_on_a_missing_one() {
        let mut clock = SiteClock::new(1);
        let mut d = Document::new();
        d.add_component(1, ComponentKind::Resistor, "R1", 0.0, 0.0, vec![1e3], &mut clock);
        d.add_component(2, ComponentKind::Resistor, "R2", 5.0, 0.0, vec![2e3], &mut clock);

        d.move_component(1, 42.0, 7.0, &mut clock);
        d.set_params(1, vec![4.7e3], &mut clock);
        d.rename(1, "Rfb", &mut clock);
        // Untouched sibling must be unaffected.
        assert_eq!(d.fields[&2].x.value, 5.0);
        assert_eq!(d.fields[&2].params.value, vec![2e3]);

        let f = &d.fields[&1];
        assert_eq!((f.x.value, f.y.value), (42.0, 7.0));
        assert_eq!(f.params.value, vec![4.7e3]);
        assert_eq!(f.name.value, "Rfb");

        // Editing an id that doesn't exist must not panic.
        d.move_component(999, 1.0, 1.0, &mut clock);
        d.set_params(999, vec![1.0], &mut clock);
        d.rename(999, "ghost", &mut clock);
    }

    #[test]
    fn a_removed_component_is_not_live() {
        let mut clock = SiteClock::new(1);
        let mut d = r(1, "R1", 1e3, 0.0, &mut clock);
        d.remove_component(1, &mut clock);
        assert_eq!(d.live_components().count(), 0);
        assert!(d.fields.contains_key(&1), "fields may linger; liveness is via the set");
    }

    // -- Text projection ---------------------------------------------------------

    #[test]
    fn to_text_is_stable_regardless_of_construction_order() {
        let mut c1 = SiteClock::new(1);
        let mut forward = Document::new();
        forward.add_component(1, ComponentKind::Resistor, "R1", 0.0, 0.0, vec![1e3], &mut c1);
        forward.add_component(2, ComponentKind::Resistor, "R2", 10.0, 0.0, vec![2e3], &mut c1);
        forward.connect((1, 1), (2, 0), &mut c1);

        let mut c2 = SiteClock::new(2);
        let mut backward = Document::new();
        backward.add_component(2, ComponentKind::Resistor, "R2", 10.0, 0.0, vec![2e3], &mut c2);
        backward.add_component(1, ComponentKind::Resistor, "R1", 0.0, 0.0, vec![1e3], &mut c2);
        backward.connect((2, 0), (1, 1), &mut c2);

        assert_eq!(forward.to_text(), backward.to_text());
    }

    #[test]
    fn a_single_field_edit_changes_exactly_one_line() {
        let mut clock = SiteClock::new(1);
        let mut d = Document::new();
        d.add_component(1, ComponentKind::Resistor, "R1", 0.0, 0.0, vec![1e3], &mut clock);
        d.add_component(2, ComponentKind::Resistor, "R2", 10.0, 0.0, vec![2e3], &mut clock);
        let before = d.to_text();

        d.fields.get_mut(&1).unwrap().params.set(vec![4.7e3], clock.tick());
        let after = d.to_text();

        let before_lines: BTreeSet<&str> = before.lines().collect();
        let after_lines: BTreeSet<&str> = after.lines().collect();
        let changed: Vec<_> = before_lines.symmetric_difference(&after_lines).collect();
        assert_eq!(changed.len(), 2, "one line removed, one added: {changed:?}");
        assert!(changed.iter().all(|l| l.contains("params")));
    }

    // -- to_netlist integration ----------------------------------------------------

    #[test]
    fn a_document_assembled_from_two_merging_replicas_solves_as_a_divider() {
        // Replica A creates the source and the first resistor.
        let mut clock_a = SiteClock::new(1);
        let mut a = Document::new();
        a.add_component(
            1,
            ComponentKind::VoltageSource,
            "V1",
            0.0,
            0.0,
            vec![10.0],
            &mut clock_a,
        );
        a.add_component(2, ComponentKind::Resistor, "R1", 10.0, 0.0, vec![1e3], &mut clock_a);
        a.connect((1, 0), (2, 0), &mut clock_a); // V1+ -> R1 in
        a.connect((1, 1), (GROUND_ENTITY, 0), &mut clock_a); // V1- -> ground

        // Replica B, starting from nothing (it will merge A in below), adds the
        // second resistor and the wire that actually forms the divider — this is
        // the CRDT path, not a single writer building the whole thing at once.
        let mut clock_b = SiteClock::new(2);
        let mut b = Document::new();
        b.add_component(3, ComponentKind::Resistor, "R2", 20.0, 0.0, vec![1e3], &mut clock_b);
        b.connect((2, 1), (3, 0), &mut clock_b); // R1 out -> R2 in
        b.connect((3, 1), (GROUND_ENTITY, 0), &mut clock_b); // R2 out -> ground

        let mut merged = a;
        merged.merge(&b);
        assert_eq!(merged.live_components().count(), 3);

        let net = merged.to_netlist().unwrap();
        let op = solve_dc(&net).unwrap();
        // Two equal resistors from 10 V to ground: midpoint at 5 V.
        let mid_name = merged.node_name((2, 1));
        let mid = op.v(net.node_index(&mid_name).unwrap());
        assert!((mid - 5.0).abs() < 1e-6, "midpoint was {mid} V");
        // Both ends of the ground rail resolve to the same node regardless of
        // which physical terminal the union-find algorithm chose as root.
        assert_eq!(merged.node_name((GROUND_ENTITY, 0)), "0");
        assert_eq!(merged.node_name((3, 1)), "0");
    }

    #[test]
    fn a_terminal_on_the_ground_entity_always_resolves_to_netlist_ground() {
        let mut clock = SiteClock::new(1);
        let mut d = Document::new();
        d.add_component(1, ComponentKind::Resistor, "R1", 0.0, 0.0, vec![1e3], &mut clock);
        d.connect((1, 0), (GROUND_ENTITY, 5), &mut clock); // any pin index on 0 is ground
        let net = d.to_netlist().unwrap();
        assert_eq!(net.node_index("0"), Some(0));
    }

    #[test]
    fn to_netlist_reports_which_component_is_underspecified() {
        let mut clock = SiteClock::new(1);
        let mut d = Document::new();
        // A diode needs two params; only one supplied.
        d.add_component(1, ComponentKind::Diode, "D1", 0.0, 0.0, vec![1e-14], &mut clock);
        let err = d.to_netlist().unwrap_err();
        assert!(err.contains('1'), "should name the offending component: {err}");
        assert!(err.contains("2"), "should say how many params were needed: {err}");
    }

    #[test]
    fn a_schematic_source_steps_on_at_t0_but_solves_dc_identically() {
        use crate::analysis::{solve_dc, Transient};

        let mut clock = SiteClock::new(1);
        let mut d = Document::new();
        d.add_component(1, ComponentKind::VoltageSource, "V1", 0.0, 0.0, vec![5.0], &mut clock);
        d.add_component(2, ComponentKind::Resistor, "R1", 10.0, 0.0, vec![1e3], &mut clock);
        d.add_component(3, ComponentKind::Capacitor, "C1", 20.0, 0.0, vec![1e-6], &mut clock);
        d.connect((1, 0), (2, 0), &mut clock);
        d.connect((2, 1), (3, 0), &mut clock);
        d.connect((3, 1), (GROUND_ENTITY, 0), &mut clock);
        d.connect((1, 1), (GROUND_ENTITY, 0), &mut clock);

        let net = d.to_netlist().unwrap();
        // DC is unaffected: a schematic source's DC operating point is exactly
        // the plain dc value, regardless of the transient waveform now attached.
        let op = solve_dc(&net).unwrap();
        let out = net.node_index(&d.node_name((2, 1))).unwrap();
        assert!((op.v(out) - 5.0).abs() < 1e-6);

        // But a transient run now actually charges, instead of starting flat at
        // the DC value -- the whole point of the change. tau = RC = 1 ms; run
        // 5 tau so the far end is unambiguously settled (99.3%), matching the
        // convention `rc_charging_follows_the_exponential` already uses.
        let tau = 1e3 * 1e-6;
        let mut tr = Transient::start(&net, tau / 2000.0, 5.0 * tau).unwrap();
        let first = tr.step().unwrap().unwrap();
        assert!(
            first.voltages[out] < 1.0,
            "should start near zero, not already at the DC value: {}",
            first.voltages[out]
        );
        let mut last = first;
        while let Some(s) = tr.step() {
            last = s.unwrap();
        }
        assert!(
            (last.voltages[out] - 5.0).abs() < 0.1,
            "should have settled near 5V after 5 tau, got {}",
            last.voltages[out]
        );
    }

    #[test]
    fn a_schematic_source_has_unit_ac_magnitude_so_bode_sweeps_are_not_flat() {
        use crate::analysis::{solve_ac, solve_dc};
        let mut clock = SiteClock::new(1);
        let mut d = Document::new();
        d.add_component(1, ComponentKind::VoltageSource, "V1", 0.0, 0.0, vec![5.0], &mut clock);
        d.add_component(2, ComponentKind::Resistor, "R1", 10.0, 0.0, vec![1e3], &mut clock);
        d.add_component(3, ComponentKind::Capacitor, "C1", 20.0, 0.0, vec![1e-6], &mut clock);
        d.connect((1, 0), (2, 0), &mut clock);
        d.connect((2, 1), (3, 0), &mut clock);
        d.connect((3, 1), (GROUND_ENTITY, 0), &mut clock);
        d.connect((1, 1), (GROUND_ENTITY, 0), &mut clock);
        let net = d.to_netlist().unwrap();
        let out = net.node_index(&d.node_name((2, 1))).unwrap();
        let op = solve_dc(&net).unwrap();

        // RC lowpass corner: f_c = 1/(2*pi*R*C) ~ 159 Hz. Well below it the
        // source should pass through close to 0 dB / 0 degrees (not the -6000
        // dB flat line a zero-magnitude AC source would produce); well above
        // it, the magnitude should have rolled off substantially.
        let low = solve_ac(&net, &op, 1.0).unwrap();
        assert!(low.db(out) > -1.0, "should pass through near 0 dB well below the corner, got {}", low.db(out));

        let high = solve_ac(&net, &op, 100_000.0).unwrap();
        assert!(high.db(out) < -30.0, "should have rolled off well above the corner, got {}", high.db(out));
    }

    #[test]
    fn a_removed_component_is_absent_from_the_netlist() {
        let mut clock = SiteClock::new(1);
        let mut d = Document::new();
        d.add_component(1, ComponentKind::Resistor, "R1", 0.0, 0.0, vec![1e3], &mut clock);
        d.remove_component(1, &mut clock);
        let net = d.to_netlist().unwrap();
        assert_eq!(net.components.len(), 0);
    }
}
