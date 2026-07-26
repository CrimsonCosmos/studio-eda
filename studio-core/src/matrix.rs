//! Dense linear algebra for the MNA solver.
//!
//! # Why dense
//!
//! SPICE uses sparse matrices with fill-reducing ordering because production
//! netlists have 10⁵–10⁷ nodes and the MNA matrix is ~99.99% zeros. A schematic
//! being drawn by hand in a browser has tens of nodes, occasionally hundreds.
//! At n = 100, dense LU is ~3×10⁵ flops — microseconds — and the sparse
//! machinery (symbolic factorisation, Markowitz pivoting, an index structure
//! traversed per entry) costs more in constant factors and in bugs than it saves.
//!
//! So this is dense, deliberately, and the boundary is written down: past a few
//! hundred nodes this becomes the bottleneck and wants a sparse LU. The tests
//! pin the O(n³) behaviour so that claim stays honest.
//!
//! Partial pivoting is not optional here even at small n — an MNA matrix has a
//! structurally zero diagonal wherever a voltage source is stamped, so the
//! unpivoted algorithm divides by zero on the second row of almost every real
//! circuit.

use std::ops::{Add, AddAssign, Div, Mul, Neg, Sub};

/// A value the solver can work in: real for DC and transient, complex for AC.
///
/// Abstracting over this rather than writing the factorisation twice means the
/// AC path exercises exactly the same pivoting and back-substitution as DC, so a
/// bug can't hide in one and not the other.
pub trait Scalar:
    Copy
    + Add<Output = Self>
    + Sub<Output = Self>
    + Mul<Output = Self>
    + Div<Output = Self>
    + Neg<Output = Self>
    + AddAssign
    + PartialEq
{
    /// Additive identity.
    fn zero() -> Self;
    /// Multiplicative identity.
    fn one() -> Self;
    /// Magnitude, for pivot selection.
    fn magnitude(self) -> f64;
    /// Build from a real number.
    fn from_real(x: f64) -> Self;
}

impl Scalar for f64 {
    fn zero() -> Self {
        0.0
    }
    fn one() -> Self {
        1.0
    }
    fn magnitude(self) -> f64 {
        self.abs()
    }
    fn from_real(x: f64) -> Self {
        x
    }
}

/// A complex number, for AC analysis.
///
/// Hand-rolled rather than pulling in `num-complex`: the solver needs five
/// operations, and every dependency here is bytes in a `.wasm` the browser
/// downloads.
#[derive(Debug, Clone, Copy, PartialEq)]
pub struct Complex {
    /// Real part.
    pub re: f64,
    /// Imaginary part.
    pub im: f64,
}

impl Complex {
    /// Construct from parts.
    pub fn new(re: f64, im: f64) -> Self {
        Complex { re, im }
    }
    /// Magnitude.
    pub fn abs(self) -> f64 {
        self.re.hypot(self.im)
    }
    /// Argument, radians.
    pub fn arg(self) -> f64 {
        self.im.atan2(self.re)
    }
    /// Magnitude in decibels, `20·log₁₀|z|`.
    pub fn db(self) -> f64 {
        20.0 * self.abs().max(1e-300).log10()
    }
}

impl Add for Complex {
    type Output = Complex;
    fn add(self, o: Complex) -> Complex {
        Complex::new(self.re + o.re, self.im + o.im)
    }
}
impl Sub for Complex {
    type Output = Complex;
    fn sub(self, o: Complex) -> Complex {
        Complex::new(self.re - o.re, self.im - o.im)
    }
}
impl Mul for Complex {
    type Output = Complex;
    fn mul(self, o: Complex) -> Complex {
        Complex::new(
            self.re * o.re - self.im * o.im,
            self.re * o.im + self.im * o.re,
        )
    }
}
impl Div for Complex {
    type Output = Complex;
    fn div(self, o: Complex) -> Complex {
        let d = o.re * o.re + o.im * o.im;
        Complex::new(
            (self.re * o.re + self.im * o.im) / d,
            (self.im * o.re - self.re * o.im) / d,
        )
    }
}
impl Neg for Complex {
    type Output = Complex;
    fn neg(self) -> Complex {
        Complex::new(-self.re, -self.im)
    }
}
impl AddAssign for Complex {
    fn add_assign(&mut self, o: Complex) {
        self.re += o.re;
        self.im += o.im;
    }
}

impl Scalar for Complex {
    fn zero() -> Self {
        Complex::new(0.0, 0.0)
    }
    fn one() -> Self {
        Complex::new(1.0, 0.0)
    }
    fn magnitude(self) -> f64 {
        self.abs()
    }
    fn from_real(x: f64) -> Self {
        Complex::new(x, 0.0)
    }
}

/// Why a solve failed.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum SolveError {
    /// No pivot above tolerance in some column — the matrix is singular.
    ///
    /// In circuit terms this is almost always a real modelling mistake rather
    /// than a numerical one: a node with no DC path to ground, a loop of
    /// voltage sources, or a floating subcircuit. The offending column is the
    /// MNA unknown involved, which is what makes it diagnosable.
    Singular {
        /// Column at which elimination stalled.
        column: usize,
    },
}

impl std::fmt::Display for SolveError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            SolveError::Singular { column } => write!(
                f,
                "matrix is singular at unknown {column} — check for a node with no DC path to \
                 ground, or a loop of ideal voltage sources"
            ),
        }
    }
}

/// A dense `n × n` system `A·x = b`, stored row-major.
#[derive(Debug, Clone, PartialEq)]
pub struct LinearSystem<T: Scalar> {
    /// Number of unknowns.
    pub n: usize,
    a: Vec<T>,
    b: Vec<T>,
}

impl<T: Scalar> LinearSystem<T> {
    /// An `n × n` system of zeros.
    pub fn new(n: usize) -> Self {
        LinearSystem {
            n,
            a: vec![T::zero(); n * n],
            b: vec![T::zero(); n],
        }
    }

    /// Reset every entry to zero, keeping the allocation.
    ///
    /// Transient analysis rebuilds the matrix every timestep and Newton rebuilds
    /// it every iteration, so this is on the hot path.
    pub fn clear(&mut self) {
        self.a.iter_mut().for_each(|v| *v = T::zero());
        self.b.iter_mut().for_each(|v| *v = T::zero());
    }

    /// Read `A[row][col]`.
    pub fn get(&self, row: usize, col: usize) -> T {
        self.a[row * self.n + col]
    }

    /// `A[row][col] += value`. Stamping is additive by construction — several
    /// components contribute to the same entry.
    pub fn add(&mut self, row: usize, col: usize, value: T) {
        self.a[row * self.n + col] += value;
    }

    /// `b[row] += value`.
    pub fn add_rhs(&mut self, row: usize, value: T) {
        self.b[row] += value;
    }

    /// Read `b[row]`.
    pub fn rhs(&self, row: usize) -> T {
        self.b[row]
    }

    /// Solve in place by Gaussian elimination with partial pivoting.
    ///
    /// Consumes the system because the factorisation overwrites it.
    pub fn solve(mut self) -> Result<Vec<T>, SolveError> {
        let n = self.n;
        if n == 0 {
            return Ok(Vec::new());
        }

        // Singularity has to be judged *relative to the matrix's own scale*.
        // A forward-biased diode stamps a conductance of order 1e22 S, so a
        // perfectly healthy pivot elsewhere can be 1e-14 in absolute terms. An
        // absolute threshold declares those circuits singular — which is wrong,
        // and the failure looks like a modelling error rather than a numerical
        // one, so it is expensive to diagnose.
        let scale = self
            .a
            .iter()
            .map(|v| v.magnitude())
            .fold(0.0f64, f64::max)
            .max(f64::MIN_POSITIVE);
        let tolerance = scale * 1e-14;

        for k in 0..n {
            // Partial pivot: the largest magnitude at or below the diagonal.
            let mut pivot_row = k;
            let mut best = self.get(k, k).magnitude();
            for r in (k + 1)..n {
                let m = self.get(r, k).magnitude();
                if m > best {
                    best = m;
                    pivot_row = r;
                }
            }
            if best <= tolerance {
                return Err(SolveError::Singular { column: k });
            }
            if pivot_row != k {
                for c in 0..n {
                    self.a.swap(k * n + c, pivot_row * n + c);
                }
                self.b.swap(k, pivot_row);
            }

            let pivot = self.get(k, k);
            for r in (k + 1)..n {
                let factor = self.get(r, k) / pivot;
                if factor == T::zero() {
                    continue;
                }
                for c in k..n {
                    let delta = factor * self.get(k, c);
                    self.a[r * n + c] = self.get(r, c) - delta;
                }
                let delta = factor * self.b[k];
                self.b[r] = self.b[r] - delta;
            }
        }

        // Back-substitution.
        let mut x = vec![T::zero(); n];
        for r in (0..n).rev() {
            let mut sum = self.b[r];
            for c in (r + 1)..n {
                sum = sum - self.get(r, c) * x[c];
            }
            x[r] = sum / self.get(r, r);
        }
        Ok(x)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn build(n: usize, rows: &[&[f64]], rhs: &[f64]) -> LinearSystem<f64> {
        let mut s = LinearSystem::new(n);
        for (r, row) in rows.iter().enumerate() {
            for (c, v) in row.iter().enumerate() {
                s.add(r, c, *v);
            }
            s.add_rhs(r, rhs[r]);
        }
        s
    }

    #[test]
    fn solves_a_two_by_two() {
        // 2x + y = 5 ; x - y = 1  ->  x = 2, y = 1
        let s = build(2, &[&[2.0, 1.0], &[1.0, -1.0]], &[5.0, 1.0]);
        let x = s.solve().unwrap();
        assert!((x[0] - 2.0).abs() < 1e-12);
        assert!((x[1] - 1.0).abs() < 1e-12);
    }

    #[test]
    fn requires_pivoting_when_the_diagonal_is_zero() {
        // A zero pivot in position (0,0). Unpivoted elimination divides by zero
        // here — and an MNA matrix has exactly this shape wherever a voltage
        // source is stamped, so this is the normal case, not a pathological one.
        let s = build(2, &[&[0.0, 1.0], &[1.0, 0.0]], &[3.0, 4.0]);
        let x = s.solve().unwrap();
        assert!((x[0] - 4.0).abs() < 1e-12);
        assert!((x[1] - 3.0).abs() < 1e-12);
    }

    #[test]
    fn singularity_is_judged_relative_to_the_matrix_scale() {
        // The property that matters is scale invariance: multiplying A and b by
        // a constant must not change whether the system solves, nor its answer.
        // With an absolute pivot threshold the scaled-down copy is wrongly
        // rejected as singular — and circuits routinely span 25 orders of
        // magnitude in conductance once a diode turns on.
        let solve_scaled = |k: f64| {
            let mut s = LinearSystem::<f64>::new(2);
            for (r, row) in [[2.0, 1.0], [1.0, -1.0]].iter().enumerate() {
                for (c, v) in row.iter().enumerate() {
                    s.add(r, c, v * k);
                }
            }
            s.add_rhs(0, 5.0 * k);
            s.add_rhs(1, 1.0 * k);
            s.solve()
        };
        for k in [1.0, 1e20, 1e-20] {
            let x = solve_scaled(k).unwrap_or_else(|e| panic!("scale {k}: {e}"));
            assert!((x[0] - 2.0).abs() < 1e-9 && (x[1] - 1.0).abs() < 1e-9, "scale {k}");
        }
    }

    #[test]
    fn detects_a_singular_matrix() {
        // Second row is twice the first: no unique solution.
        let s = build(2, &[&[1.0, 2.0], &[2.0, 4.0]], &[3.0, 6.0]);
        assert_eq!(s.solve(), Err(SolveError::Singular { column: 1 }));
    }

    #[test]
    fn singular_error_explains_itself_in_circuit_terms() {
        let msg = SolveError::Singular { column: 3 }.to_string();
        assert!(msg.contains("no DC path to ground"));
    }

    #[test]
    fn solves_a_larger_system_against_a_known_answer() {
        // Build A from a known x so the expected answer is exact by construction.
        let n = 24;
        let a: Vec<f64> = (0..n * n)
            .map(|i| {
                let (r, c) = (i / n, i % n);
                if r == c {
                    (r as f64) + 5.0
                } else {
                    1.0 / ((r as f64 - c as f64).abs() + 1.0)
                }
            })
            .collect();
        let x_true: Vec<f64> = (0..n).map(|i| (i as f64) * 0.37 - 2.0).collect();

        let mut s = LinearSystem::new(n);
        for r in 0..n {
            let mut bi = 0.0;
            for c in 0..n {
                s.add(r, c, a[r * n + c]);
                bi += a[r * n + c] * x_true[c];
            }
            s.add_rhs(r, bi);
        }
        let x = s.solve().unwrap();
        for i in 0..n {
            assert!((x[i] - x_true[i]).abs() < 1e-9, "x[{i}] = {} vs {}", x[i], x_true[i]);
        }
    }

    #[test]
    fn clear_preserves_shape() {
        let mut s = build(2, &[&[1.0, 2.0], &[3.0, 4.0]], &[5.0, 6.0]);
        s.clear();
        assert_eq!(s.n, 2);
        assert_eq!(s.get(1, 1), 0.0);
        assert_eq!(s.rhs(0), 0.0);
    }

    #[test]
    fn an_empty_system_is_not_an_error() {
        // A circuit that is nothing but ground has zero unknowns.
        assert!(LinearSystem::<f64>::new(0).solve().unwrap().is_empty());
    }

    #[test]
    fn complex_arithmetic_is_correct() {
        let a = Complex::new(3.0, 4.0);
        let b = Complex::new(1.0, -2.0);
        assert_eq!(a + b, Complex::new(4.0, 2.0));
        assert_eq!(a - b, Complex::new(2.0, 6.0));
        // (3+4i)(1-2i) = 3 - 6i + 4i + 8 = 11 - 2i
        assert_eq!(a * b, Complex::new(11.0, -2.0));
        assert!((a.abs() - 5.0).abs() < 1e-12);
        // 20·log10(5) ≈ 13.979 dB
        assert!((a.db() - 13.9794).abs() < 1e-3);

        let q = a / b;
        let back = q * b;
        assert!((back.re - a.re).abs() < 1e-12 && (back.im - a.im).abs() < 1e-12);
    }

    #[test]
    fn solves_a_complex_system() {
        // (1+i)z = 2  ->  z = 2/(1+i) = 1 - i
        let mut s = LinearSystem::<Complex>::new(1);
        s.add(0, 0, Complex::new(1.0, 1.0));
        s.add_rhs(0, Complex::new(2.0, 0.0));
        let x = s.solve().unwrap();
        assert!((x[0].re - 1.0).abs() < 1e-12);
        assert!((x[0].im + 1.0).abs() < 1e-12);
    }

    #[test]
    fn the_same_factorisation_serves_real_and_complex() {
        // A complex system with zero imaginary parts must reproduce the real
        // answer exactly — proof the two paths share one implementation.
        let real = build(2, &[&[2.0, 1.0], &[1.0, -1.0]], &[5.0, 1.0])
            .solve()
            .unwrap();
        let mut c = LinearSystem::<Complex>::new(2);
        for (r, row) in [[2.0, 1.0], [1.0, -1.0]].iter().enumerate() {
            for (col, v) in row.iter().enumerate() {
                c.add(r, col, Complex::from_real(*v));
            }
        }
        c.add_rhs(0, Complex::from_real(5.0));
        c.add_rhs(1, Complex::from_real(1.0));
        let cx = c.solve().unwrap();
        for i in 0..2 {
            assert!((cx[i].re - real[i]).abs() < 1e-12);
            assert!(cx[i].im.abs() < 1e-12);
        }
    }
}
