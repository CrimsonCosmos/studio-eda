//! The GPU canvas: instanced-quad rendering of `studio-renderer`'s output.
//!
//! Built exactly to the shape `docs/wgpu-wasm-findings.md` proved out: wgpu 30
//! (0.20 sends a WebGPU limit Chrome no longer recognises), `Backends::
//! BROWSER_WEBGPU | Backends::GL` so a browser without WebGPU still renders,
//! and `queue.present(frame)` rather than the pre-30 `frame.present()` — a
//! `SurfaceTexture` dropped without that call is silently discarded, which
//! paints nothing while every part of the call chain reports success.
//!
//! # Why one draw call
//!
//! Every rectangle [`studio_renderer::indexed_build`] emits becomes one
//! instance in a single vertex buffer, drawn with one `draw()` call
//! (`TriangleStrip`, 4 vertices × N instances) rather than one draw call per
//! rectangle. Instancing is what actually lets "millions of polygons" reach the
//! GPU at all — a few million individual draw calls would be dominated by
//! per-call overhead regardless of how little each one draws.
//!
//! # A simplification worth naming
//!
//! The instance buffer is recreated fresh every frame, sized exactly to that
//! frame's instance count, rather than a persistent buffer grown and reused
//! across frames. That costs an allocation per frame; a shipped renderer would
//! keep a buffer sized to the largest frame seen and only grow it on demand.
//! For a demo whose point is the CPU-side culling/LOD win (see
//! `docs/studio-renderer-perf.md`), the simpler version is the honest scope.

use studio_renderer::{indexed_build, naive_build, synthetic_layout, Instance, Rect, SpatialGrid};
use wasm_bindgen::prelude::*;
use wasm_bindgen::JsCast;
use wgpu::util::DeviceExt;

const SHADER: &str = r#"
struct Viewport {
    x: f32,
    y: f32,
    w: f32,
    h: f32,
};
@group(0) @binding(0) var<uniform> viewport: Viewport;

struct VOut {
    @builtin(position) pos: vec4<f32>,
    @location(0) color: vec3<f32>,
};

@vertex
fn vs(
    @location(0) corner: vec2<f32>,
    @location(1) inst_pos: vec2<f32>,
    @location(2) inst_size: vec2<f32>,
    @location(3) inst_color: vec3<f32>,
) -> VOut {
    let world = inst_pos + corner * inst_size;
    let ndc_x = ((world.x - viewport.x) / viewport.w) * 2.0 - 1.0;
    let ndc_y = ((world.y - viewport.y) / viewport.h) * 2.0 - 1.0;
    var out: VOut;
    out.pos = vec4<f32>(ndc_x, ndc_y, 0.0, 1.0);
    out.color = inst_color;
    return out;
}

@fragment
fn fs(in: VOut) -> @location(0) vec4<f32> {
    return vec4<f32>(in.color, 1.0);
}
"#;

/// Unit-quad corners as a triangle strip: (0,0) (1,0) (0,1) (1,1).
const QUAD_CORNERS: [f32; 8] = [0.0, 0.0, 1.0, 0.0, 0.0, 1.0, 1.0, 1.0];

fn f32s_to_le_bytes(v: &[f32]) -> Vec<u8> {
    let mut out = Vec::with_capacity(v.len() * 4);
    for x in v {
        out.extend_from_slice(&x.to_le_bytes());
    }
    out
}

/// A live wgpu surface drawing `studio-renderer` instance lists.
#[wasm_bindgen]
pub struct GpuCanvas {
    device: wgpu::Device,
    queue: wgpu::Queue,
    surface: wgpu::Surface<'static>,
    pipeline: wgpu::RenderPipeline,
    viewport_buffer: wgpu::Buffer,
    viewport_bind_group: wgpu::BindGroup,
    quad_vbo: wgpu::Buffer,
    /// Reported once, at creation, so the UI can show which rendering path is
    /// actually active — proving the fallback rather than assuming it.
    backend_report: String,
}

/// Build a [`GpuCanvas`] on the `<canvas>` with the given element id.
///
/// Async because acquiring a wgpu adapter and device is: the browser may need
/// to negotiate with the platform's real GPU driver before either exists.
#[wasm_bindgen(js_name = createGpuCanvas)]
pub async fn create_gpu_canvas(canvas_id: String) -> Result<GpuCanvas, JsValue> {
    let window = web_sys::window().ok_or_else(|| JsValue::from_str("no window"))?;
    let document = window.document().ok_or_else(|| JsValue::from_str("no document"))?;
    let canvas: web_sys::HtmlCanvasElement = document
        .get_element_by_id(&canvas_id)
        .ok_or_else(|| JsValue::from_str("canvas not found"))?
        .dyn_into()
        .map_err(|_| JsValue::from_str("element is not a canvas"))?;
    let width = canvas.width().max(1);
    let height = canvas.height().max(1);

    let instance = wgpu::Instance::new(wgpu::InstanceDescriptor {
        backends: wgpu::Backends::BROWSER_WEBGPU | wgpu::Backends::GL,
        flags: wgpu::InstanceFlags::default(),
        memory_budget_thresholds: Default::default(),
        backend_options: Default::default(),
        display: None,
    });
    let surface = instance
        .create_surface(wgpu::SurfaceTarget::Canvas(canvas))
        .map_err(|e| JsValue::from_str(&format!("create_surface: {e}")))?;
    let adapter = instance
        .request_adapter(&wgpu::RequestAdapterOptions {
            power_preference: wgpu::PowerPreference::HighPerformance,
            compatible_surface: Some(&surface),
            force_fallback_adapter: false,
            apply_limit_buckets: false,
        })
        .await
        .map_err(|e| JsValue::from_str(&format!("no adapter: {e}")))?;
    let backend_report = format!("{:?} / {}", adapter.get_info().backend, adapter.get_info().name);

    let (device, queue) = adapter
        .request_device(&wgpu::DeviceDescriptor {
            label: Some("studio-gpu-canvas"),
            required_limits: wgpu::Limits::downlevel_webgl2_defaults().using_resolution(adapter.limits()),
            ..Default::default()
        })
        .await
        .map_err(|e| JsValue::from_str(&format!("request_device: {e}")))?;

    let caps = surface.get_capabilities(&adapter);
    let format = caps.formats[0];
    surface.configure(
        &device,
        &wgpu::SurfaceConfiguration {
            usage: wgpu::TextureUsages::RENDER_ATTACHMENT,
            format,
            width,
            height,
            present_mode: wgpu::PresentMode::Fifo,
            alpha_mode: caps.alpha_modes[0],
            view_formats: vec![],
            color_space: wgpu::SurfaceColorSpace::Srgb,
            desired_maximum_frame_latency: 2,
        },
    );

    let viewport_buffer = device.create_buffer(&wgpu::BufferDescriptor {
        label: Some("viewport"),
        size: 16,
        usage: wgpu::BufferUsages::UNIFORM | wgpu::BufferUsages::COPY_DST,
        mapped_at_creation: false,
    });
    let bind_group_layout = device.create_bind_group_layout(&wgpu::BindGroupLayoutDescriptor {
        label: Some("viewport layout"),
        entries: &[wgpu::BindGroupLayoutEntry {
            binding: 0,
            visibility: wgpu::ShaderStages::VERTEX,
            ty: wgpu::BindingType::Buffer {
                ty: wgpu::BufferBindingType::Uniform,
                has_dynamic_offset: false,
                min_binding_size: None,
            },
            count: None,
        }],
    });
    let viewport_bind_group = device.create_bind_group(&wgpu::BindGroupDescriptor {
        label: Some("viewport"),
        layout: &bind_group_layout,
        entries: &[wgpu::BindGroupEntry {
            binding: 0,
            resource: viewport_buffer.as_entire_binding(),
        }],
    });

    let shader = device.create_shader_module(wgpu::ShaderModuleDescriptor {
        label: Some("instanced quads"),
        source: wgpu::ShaderSource::Wgsl(SHADER.into()),
    });
    let pipeline_layout = device.create_pipeline_layout(&wgpu::PipelineLayoutDescriptor {
        label: Some("studio pipeline layout"),
        bind_group_layouts: &[Some(&bind_group_layout)],
        ..Default::default()
    });

    let quad_vbo = device.create_buffer_init(&wgpu::util::BufferInitDescriptor {
        label: Some("unit quad"),
        contents: &f32s_to_le_bytes(&QUAD_CORNERS),
        usage: wgpu::BufferUsages::VERTEX,
    });

    let pipeline = device.create_render_pipeline(&wgpu::RenderPipelineDescriptor {
        label: Some("instanced quads"),
        layout: Some(&pipeline_layout),
        vertex: wgpu::VertexState {
            module: &shader,
            entry_point: Some("vs"),
            buffers: &[
                Some(wgpu::VertexBufferLayout {
                    array_stride: 8,
                    step_mode: wgpu::VertexStepMode::Vertex,
                    attributes: &[wgpu::VertexAttribute {
                        format: wgpu::VertexFormat::Float32x2,
                        offset: 0,
                        shader_location: 0,
                    }],
                }),
                Some(wgpu::VertexBufferLayout {
                    array_stride: 28,
                    step_mode: wgpu::VertexStepMode::Instance,
                    attributes: &[
                        wgpu::VertexAttribute {
                            format: wgpu::VertexFormat::Float32x2,
                            offset: 0,
                            shader_location: 1,
                        },
                        wgpu::VertexAttribute {
                            format: wgpu::VertexFormat::Float32x2,
                            offset: 8,
                            shader_location: 2,
                        },
                        wgpu::VertexAttribute {
                            format: wgpu::VertexFormat::Float32x3,
                            offset: 16,
                            shader_location: 3,
                        },
                    ],
                }),
            ],
            compilation_options: Default::default(),
        },
        fragment: Some(wgpu::FragmentState {
            module: &shader,
            entry_point: Some("fs"),
            targets: &[Some(format.into())],
            compilation_options: Default::default(),
        }),
        primitive: wgpu::PrimitiveState {
            topology: wgpu::PrimitiveTopology::TriangleStrip,
            ..Default::default()
        },
        depth_stencil: None,
        multisample: wgpu::MultisampleState::default(),
        multiview_mask: None,
        cache: None,
    });

    Ok(GpuCanvas {
        device,
        queue,
        surface,
        pipeline,
        viewport_buffer,
        viewport_bind_group,
        quad_vbo,
        backend_report,
    })
}

#[wasm_bindgen]
impl GpuCanvas {
    /// Which wgpu backend this canvas actually acquired — `"BrowserWebGpu / ..."`
    /// or `"Gl / ANGLE ..."` on the WebGL2 fallback.
    #[wasm_bindgen(js_name = backendReport)]
    pub fn backend_report(&self) -> String {
        self.backend_report.clone()
    }

    /// Build one frame of a `count`-rectangle synthetic layout and draw it.
    ///
    /// `viewport` is world-space `[x, y, w, h]`; `scale` is screen pixels per
    /// world unit at the current zoom; `lod_px` is the on-screen cell size
    /// below which a cell's contents collapse to one aggregate instance;
    /// `indexed` toggles the accelerated path against the naive one, so the
    /// perf claim in `docs/studio-renderer-perf.md` can be shown live rather
    /// than only read from an offline benchmark table.
    pub fn render(
        &mut self,
        count: usize,
        seed: u64,
        viewport: Vec<f32>,
        scale: f32,
        lod_px: f32,
        indexed: bool,
    ) -> Result<JsValue, JsValue> {
        if viewport.len() != 4 {
            return Err(JsValue::from_str("viewport must be [x, y, w, h]"));
        }
        let vp = Rect::new(viewport[0], viewport[1], viewport[2], viewport[3]);

        let rects = synthetic_layout(count, seed);
        let (instances, stats) = if indexed {
            let grid = SpatialGrid::build(&rects, 40.0);
            indexed_build(&grid, &rects, vp, scale, lod_px)
        } else {
            naive_build(&rects, vp)
        };

        self.queue.write_buffer(&self.viewport_buffer, 0, &f32s_to_le_bytes(&viewport));

        let instance_bytes = f32s_to_le_bytes(&instance_floats(&instances));
        let instance_buffer = (!instances.is_empty()).then(|| {
            self.device.create_buffer_init(&wgpu::util::BufferInitDescriptor {
                label: Some("instances"),
                contents: &instance_bytes,
                usage: wgpu::BufferUsages::VERTEX,
            })
        });

        let frame = match self.surface.get_current_texture() {
            wgpu::CurrentSurfaceTexture::Success(t) | wgpu::CurrentSurfaceTexture::Suboptimal(t) => t,
            other => return Err(JsValue::from_str(&format!("no surface texture: {other:?}"))),
        };
        let view = frame.texture.create_view(&Default::default());
        let mut encoder = self.device.create_command_encoder(&Default::default());
        {
            let mut pass = encoder.begin_render_pass(&wgpu::RenderPassDescriptor {
                label: Some("studio frame"),
                color_attachments: &[Some(wgpu::RenderPassColorAttachment {
                    view: &view,
                    resolve_target: None,
                    depth_slice: None,
                    ops: wgpu::Operations {
                        load: wgpu::LoadOp::Clear(wgpu::Color {
                            r: 0.05,
                            g: 0.06,
                            b: 0.09,
                            a: 1.0,
                        }),
                        store: wgpu::StoreOp::Store,
                    },
                })],
                depth_stencil_attachment: None,
                timestamp_writes: None,
                occlusion_query_set: None,
                multiview_mask: None,
            });
            if let Some(ib) = &instance_buffer {
                pass.set_pipeline(&self.pipeline);
                pass.set_bind_group(0, &self.viewport_bind_group, &[]);
                pass.set_vertex_buffer(0, self.quad_vbo.slice(..));
                pass.set_vertex_buffer(1, ib.slice(..));
                pass.draw(0..4, 0..instances.len() as u32);
            }
        }
        self.queue.submit(Some(encoder.finish()));
        self.queue.present(frame);

        #[derive(serde::Serialize)]
        struct Stats {
            considered: usize,
            emitted: usize,
        }
        serde_wasm_bindgen::to_value(&Stats {
            considered: stats.considered,
            emitted: stats.emitted,
        })
        .map_err(|e| JsValue::from_str(&e.to_string()))
    }
}

/// Flatten instances into `[x, y, w, h, r, g, b]` per instance, matching the
/// vertex buffer layout the pipeline declares.
fn instance_floats(instances: &[Instance]) -> Vec<f32> {
    let mut out = Vec::with_capacity(instances.len() * 7);
    for inst in instances {
        let (r, g, b) = if inst.is_aggregate {
            (0.93, 0.69, 0.38) // sand -- a collapsed LOD cell
        } else {
            (0.49, 0.78, 0.85) // cyan -- an individual rectangle
        };
        out.extend_from_slice(&[inst.rect.x, inst.rect.y, inst.rect.w, inst.rect.h, r, g, b]);
    }
    out
}
