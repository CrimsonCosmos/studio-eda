# studioforge

**Pair Claude Code with deterministic EDA pipelines.** Say *"build me a humanoid robot; budget is $50,000"* and get a
system architecture, cost-vs-budget, fab-ready PCB packages (Gerber, drill, BOM, pick-and-place, KiCad), a wiring harness,
and a shopping list with a link for every part.

Claude does what only a language model can (decompose the goal, research parts on the web, make buy-vs-build calls).
Everything numeric or geometric is **deterministic code** - cheap, reproducible, and independently re-checked - so the LLM never has to
hold a netlist or a component coordinate in its context.

```
request ──► supervisor LLM (Claude Code) ──► SystemPlan (boards, purchased items, links - no netlists, no coordinates)
                │  web research: parts, prices, COTS modules, datasheets
                ▼
          INTERFACE RESOLVER (deterministic)          link constraints decide each block's exact inputs/outputs:
            power demand ↑ (qty × duty), voltage windows ↓, MCU pin-mux, connectors + wire gauge, kind/level/I²C checks
                ▼
          BLOCK BUILDERS (deterministic)              buck/LDO/battery/MCU/CAN/3-phase motor driver/connector/custom_netlist
            datasheet equations → circuit + placement constraints (decoupling, hot loops, EMI, thermal)
                ▼
          PCB ENGINE (deterministic)                  simulated-annealing placer (thermal/EMI/decoupling) → A* multi-layer autorouter
            planes + pours + stitching → independent geometry DRC → thermal map → EMI checks
                ▼
          EXPORT                                      Gerber RS-274X, Excellon, BOM (+JLCPCB), CPL, KiCad .kicad_pcb + netlist, SVG, REPORT.md
                ▼
          PURCHASE                                    aggregated BOM × quantities + spares, supplier choice, cart CSVs, shopping list,
                                                      optional agent cart-fill (stops before payment)
```

## Architecture decision: integrate KiCad, don't re-implement it
Our value is the **LLM supervisor, interface resolver, circuit generators ("blocks"), parts research and purchasing**. Commodity EDA
(libraries, DRC, Gerber/drill/position export, schematic/PCB file formats, autorouting) is *bought*, not built:
KiCad 7 (via `kicad-cli`, the `pcbnew` Python module and its file formats; libraries read from `/usr/share/kicad`) and Freerouting.
We do not fork KiCad: its public interfaces are enough, and a fork would carry a GPL C++ code base for no gain.
The earlier hand-written exporters, footprint generators and router remain only as offline fallbacks and are being retired as the
KiCad-backed paths land (see `git log`). Our own placer (thermal/EMI/decoupling-aware) stays because KiCad has none.

## Quick start

```bash
pip install -e '.[dev]'                       # numpy, shapely (+ pytest, gerbonara, kiutils for tests)
forge doctor
forge blocks                                  # the deterministic subassembly builders
forge board buck_converter --params '{"vin_min":39,"vin_max":54.6,"vout":12,"iout":3}' --out out/buck   # one block → fab-ready board
forge build "build me a humanoid robot" --budget 50000 --out out/humanoid    # needs `claude` CLI, or falls back to built-in templates
forge build-plan plan.json --no-pcb           # resolve + cost only (seconds)
```

### Claude Code as the supervisor (MCP)
```bash
claude mcp add studioforge -- python -m studioforge.mcp_server      # or use .mcp.json in this directory
# then, inside Claude Code:  "build me a humanoid robot, budget $50,000"
```
`CLAUDE.md` contains the supervisor instructions (same text as `planner.SUPERVISOR_PROMPT`). Tools: `forge_list_blocks`,
`forge_search_parts`, `forge_get_part`, `forge_research_part`, `forge_validate_plan`, `forge_build_system`, `forge_build_block_board`.

## BUY vs BUILD
The supervisor is instructed **not to build every subassembly**: unless you ask for it, or the budget forces it, or no product fits,
off-the-shelf power supplies, DC-DC modules, batteries/BMS, motor drivers, compute modules, sensors, cameras and cables are planned as
*purchased items* (spec + cost + source URL). For blocks that could be bought, the plan carries a `buy_alternative`; the pipeline then
compares the **actually computed** build cost (BOM + PCB + assembly at the real quantity) with the module price and picks one
(`prefer: "build" | "buy"` overrides). Decisions and reasoning are written to `SYSTEM.md`.

## Parts: large catalog, cached docs, research on a miss
* SQLite catalog (`~/.studioforge`): seed ICs/connectors with pinouts + unbounded **synthesised generics** (any R/C/L/LED/header/fuse by value and
  package, parametric MLCC derating) + bulk importer (`forge parts import-jlcpcb`).
* Datasheets are downloaded and cached (`forge parts verify`); pin names/numbers are cross-checked against the PDF text.
  *Verified against real TI/Diodes/AMS datasheets during development - and it caught a naming mismatch.*
* **Miss → research agent.** If no catalog part satisfies a requirement, headless Claude Code searches the web, downloads the official
  datasheet, extracts pinout/footprint/ratings and distributor links into strict JSON. Deterministic code validates it (pad coverage, overlaps,
  duplicate pins, footprint sanity, datasheet cross-check), retries with the validation errors on failure, caches the part, and records where to buy it.
* Every part carries its `verified` flag, provenance, and datasheet path. Unverified parts are listed in each board's report.

## What is verified, and how
| Layer | Evidence |
|--|--|
| Footprint generators | pad geometry compared with KiCad's official library (found and fixed a swapped-dimension bug) |
| Datasheet equations | buck converter formulas follow TI's published design procedure; UVLO/compensation/inductor-range land next to the datasheet examples |
| Router | every result re-checked by an *independent* shapely DRC + copper-connectivity analysis (it caught real router bugs during development) |
| Gerber/drill | parsed by gerbonara; pad/via/track/drill coordinates compared 1:1 with the board model |
| KiCad output | parsed by kiutils; counts of footprints/vias/nets match |
| Resolver | unit tests for demand propagation, windows, address clashes, connector/wire sizing, buy-vs-build |
| Research agent | live run: found TPS5430DDA on the web, downloaded TI's official datasheet + an app note (SHA-256 recorded), extracted the pinout, corrected an overlapping land pattern after our validator rejected it, `verified=True`; the part then drove a block builder |
| Supervisor LLM | live run: headless Claude Code wrote a plan with sourced product URLs; the resolver rejected it once (3.28 V rail vs 3.3 V minimum), Claude repaired it, the buy-vs-build step applied the policy (`examples/live_supervisor_24v_joint/`) |
| MCP server | connects, lists tools and builds a DRC-clean board through the **official `mcp` Python client** |
| Final boards | humanoid example: both board types DRC-clean; Excellon hits identical to the hole set recomputed from the exported `.kicad_pcb` (462 / 351) |

## Example outputs
`examples/humanoid/` (offline archetype, $50k budget → ~$28.7k, two board types ready for fab review) and
`examples/live_supervisor_24v_joint/` (plan written by the live supervisor). See `examples/README.md`.

## Honest limitations
* The built-in router is a Python grid router: dense boards may need several automatic layout attempts (the 15 A joint board ended at 125 × 92 mm after 5 attempts / ~8 min).
* Nothing here has been sent to a fab. Run the fab's DFM check and open the Gerbers in KiCad/Gerbv first. `REPORT.md` ends with a checklist.
* Seed pinouts are `verified=False` until `forge parts verify` matches them to a cached datasheet (STM32 datasheet host blocks automated downloads → use the research agent or download manually).
* Land patterns are IPC-nominal generators; connectors/modules must be compared with manufacturer drawings.
* Electrical design is equation-based, **not simulated** (loop stability, EMC, enclosure thermals). Thermal is a 2-D steady-state estimate.
* Autorouter is a grid router (Python): small/medium boards route in seconds-minutes; very dense/large boards need the time budget raised or a different tool (Specctra DSN export for Freerouting is a planned adapter).
* Single-sided SMT assembly, no differential-pair/length-matching, no BGA fanout yet.
* "Anything an EE can build" is bounded by the block library + `custom_netlist` escape hatch + part research; mechanical, firmware and software are out of scope (planned as purchased items with estimated cost).
* Agent purchasing (browser/computer use) is implemented safely (cart-fill only; checkout requires an exact confirmation phrase) but not exercised in CI. Prices are estimates until refreshed from suppliers (`purchase/mouser.py` API adapter needs a key).

## Layout
`src/studioforge/` — `model.py` data model · `footprints.py`/`kicad_mod.py` · `parts/` catalog, research, docs · `blocks/` builders ·
`system/` plan, resolver, planner, cost, build · `pcb/` placement, router, pours, planes, DRC, analysis, flow · `export/` Gerber, KiCad, BOM, SVG, report ·
`purchase/` plan, agent, API · `mcp_server.py` · `cli.py` · `tests/`
