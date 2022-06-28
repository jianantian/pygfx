from ._shaderbase import BaseShader
from ...resources import Buffer, Texture, TextureView


class WorldObjectShader(BaseShader):
    """A base shader for world objects. Must be subclassed to implement
    a shader for a specific material. This class also implements common
    functions that can be used in all material-specific renderers.
    """

    type = "unspecified"  # must be "compute" or "render"

    def __init__(self, wobject, **kwargs):
        super().__init__(**kwargs)

        # Init values that get set when generate_wgsl() is called, using blender.get_shader_kwargs()
        self.kwargs.setdefault("write_pick", True)
        self.kwargs.setdefault("blending_code", "")

        # Init colormap values
        self.kwargs.setdefault("colormap_dim", "")
        self.kwargs.setdefault("colormap_nchannels", 1)
        self.kwargs.setdefault("colormap_format", "f32")

        # Apply_clip_planes
        self["n_clipping_planes"] = len(wobject.material.clipping_planes)
        self["clipping_mode"] = wobject.material.clipping_mode

    # ----- What subclasses must implement

    def get_resources(self, wobject, shared):
        """Subclasses must return a dict describing the buffers and
        textures used by this shader.

        Fields for a compute shader:
          * "bindings": a dict of dicts with binding objects
            (group_slot -> binding_slot -> binding)

        Fields for a render shader:
          * "index_buffer": None or a Buffer object.
          * "vertex_buffer": a dict of buffer objects.
          * "bindings": a dict of dicts with binding objects
            (group_slot -> binding_slot -> binding)
        """
        return {
            "index_buffer": None,
            "vertex_buffers": {},
            "bindings": {
                0: {},
            },
        }

    def get_pipeline_info(self, wobject, shared):
        """Subclasses must return a dict describing pipeline details.

        Fields for a compute shader: empty

        Fields for a render shader:
          * "cull_mode"
          * "primitive_topology"
        """
        raise NotImplementedError()
        return {
            "primitive_topology": 0,
            "cull_mode": 0,
        }

    def get_render_info(self, wobject, shared):
        """Subclasses must return a dict describing render details.

        Fields for a compute shader:
          * "indices" (3 ints)

        Fields for a render shader:
          * "render_mask"
          * "indices" (list of 2 or 4 ints).
        """
        return {
            "indices": (1, 1),
            "render_mask": 0,
        }

    # ----- Colormap stuff

    def define_vertex_colormap(self, texture_view, texcoords):
        """Define the given texture view as the colormap to be used to
        lookup the final color from the per- vertex texcoords.
        In the WGSL the colormap can be sampled using ``sample_colormap()``.
        Returns a list of bindings.
        """
        from ._pipeline import Binding  # avoid recursive import

        if isinstance(texture_view, Texture):
            raise TypeError("texture_view is a Texture, but must be a TextureView")
        elif not isinstance(texture_view, TextureView):
            raise TypeError("texture_view must be a TextureView")
        elif not isinstance(texcoords, Buffer):
            raise ValueError("texture_view is present, but texcoords must be a buffer")
        # Dimensionality
        self["colormap_dim"] = view_dim = texture_view.view_dim
        if view_dim not in ("1d", "2d", "3d"):
            raise ValueError("Unexpected texture dimension")
        # Texture dim matches texcoords
        vert_fmt = to_vertex_format(texcoords.format)
        if view_dim == "1d" and "x" not in vert_fmt:
            pass
        elif not vert_fmt.endswith("x" + view_dim[0]):
            raise ValueError(
                f"texcoords {texcoords.format} does not match texture_view {view_dim}"
            )
        # Sampling type
        fmt = to_texture_format(texture_view.format)
        if "norm" in fmt or "float" in fmt:
            self["colormap_format"] = "f32"
        elif "uint" in fmt:
            self["colormap_format"] = "u32"
        else:
            self["colormap_format"] = "i32"
        # Channels
        self["colormap_nchannels"] = len(fmt) - len(fmt.lstrip("rgba"))
        # Return bindings
        return [
            Binding("s_colormap", "sampler/filtering", texture_view, "FRAGMENT"),
            Binding("t_colormap", "texture/auto", texture_view, "FRAGMENT"),
            Binding("s_texcoords", "buffer/read_only_storage", texcoords, "VERTEX"),
        ]

    def define_img_colormap(self, texture_view):
        """Define the given texture view as the colormap to be used to
        lookup the final color from the image date.
        In the WGSL the colormap can be sampled using ``sample_colormap()``.
        Returns a list of bindings.
        """
        from ._pipeline import Binding  # avoid recursive import

        if isinstance(texture_view, Texture):
            raise TypeError("texture_view is a Texture, but must be a TextureView")
        elif not isinstance(texture_view, TextureView):
            raise TypeError("texture_view must be a TextureView")
        # Dimensionality
        self["colormap_dim"] = view_dim = texture_view.view_dim
        if texture_view.view_dim not in ("1d", "2d", "3d"):
            raise ValueError("Unexpected colormap texture dimension")
        # Texture dim matches image channels
        if int(view_dim[0]) != self["img_nchannels"]:
            raise ValueError(
                f"Image channels {self['img_nchannels']} does not match texture_view {view_dim}"
            )
        # Sampling type
        fmt = to_texture_format(texture_view.format)
        if "norm" in fmt or "float" in fmt:
            self["colormap_format"] = "f32"
        elif "uint" in fmt:
            self["colormap_format"] = "u32"
        else:
            self["colormap_format"] = "i32"
        # Channels
        self["colormap_nchannels"] = len(fmt) - len(fmt.lstrip("rgba"))
        # Return bindings
        return [
            Binding("s_colormap", "sampler/filtering", texture_view, "FRAGMENT"),
            Binding("t_colormap", "texture/auto", texture_view, "FRAGMENT"),
        ]

    def _code_colormap(self):
        typemap = {"1d": "f32", "2d": "vec2<f32>", "3d": "vec3<f32>"}
        self["colormap_coord_type"] = typemap.get(self["colormap_dim"], "f32")

        return """
        fn sample_colormap(texcoord: {{ colormap_coord_type }}) -> vec4<f32> {
            // Sample in the colormap. We get a vec4 color, but not all channels may be used.
            $$ if not colormap_dim
                let color_value = vec4<f32>(0.0);
            $$ elif colormap_dim == '1d'
                $$ if colormap_format == 'f32'
                    let color_value = textureSample(t_colormap, s_colormap, texcoord);
                $$ else
                    let texcoords_dim = f32(textureDimensions(t_colormap));
                    let texcoords_u = i32(texcoord * texcoords_dim % texcoords_dim);
                    let color_value = vec4<f32>(textureLoad(t_colormap, texcoords_u, 0));
                $$ endif
            $$ elif colormap_dim == '2d'
                $$ if colormap_format == 'f32'
                    let color_value = textureSample(t_colormap, s_colormap, texcoord.xy);
                $$ else
                    let texcoords_dim = vec2<f32>(textureDimensions(t_colormap));
                    let texcoords_u = vec2<i32>(texcoord.xy * texcoords_dim % texcoords_dim);
                    let color_value = vec4<f32>(textureLoad(t_colormap, texcoords_u, 0));
                $$ endif
            $$ elif colormap_dim == '3d'
                $$ if colormap_format == 'f32'
                    let color_value = textureSample(t_colormap, s_colormap, texcoord.xyz);
                $$ else
                    let texcoords_dim = vec3<f32>(textureDimensions(t_colormap));
                    let texcoords_u = vec3<i32>(texcoord.xyz * texcoords_dim % texcoords_dim);
                    let color_value = vec4<f32>(textureLoad(t_colormap, texcoords_u, 0));
                $$ endif
            $$ endif
            // Depending on the number of channels we make grayscale, rgb, etc.
            $$ if colormap_nchannels == 1
                let color = vec4<f32>(color_value.rrr, 1.0);
            $$ elif colormap_nchannels == 2
                let color = vec4<f32>(color_value.rrr, color_value.g);
            $$ elif colormap_nchannels == 3
                let color = vec4<f32>(color_value.rgb, 1.0);
            $$ else
                let color = vec4<f32>(color_value.rgb, color_value.a);
            $$ endif
            return color;
        }
        """

    # ----- WGSL lib

    def code_common(self):
        """Get the WGSL functions builtin by PyGfx."""

        # Just a placeholder
        blending_code = """
        let alpha_compare_epsilon : f32 = 1e-6;
        {{ blending_code }}
        """

        return (
            self._code_colormap()
            + self._code_lighting()
            + self._code_clipping_planes()
            + self._code_picking()
            + self._code_misc()
            + blending_code
        )

    def _code_clipping_planes(self):
        if not self["n_clipping_planes"]:
            return """
            fn check_clipping_planes(world_pos: vec3<f32>) -> bool { return true; }
            fn apply_clipping_planes(world_pos: vec3<f32>) { }
            """

        return """
        fn check_clipping_planes(world_pos: vec3<f32>) -> bool {
            var clipped: bool = {{ 'false' if clipping_mode == 'ANY' else 'true' }};
            for (var i=0; i<{{ n_clipping_planes }}; i=i+1) {
                let plane = u_material.clipping_planes[i];
                let plane_clipped = dot( world_pos, plane.xyz ) < plane.w;
                clipped = clipped {{ '||' if clipping_mode == 'ANY' else '&&' }} plane_clipped;
            }
            return !clipped;
        }
        fn apply_clipping_planes(world_pos: vec3<f32>) {
            if (!(check_clipping_planes(world_pos))) { discard; }
        }
        """

    def _code_picking(self):
        return """
        var<private> p_pick_bits_used : i32 = 0;

        fn pick_pack(value: u32, bits: i32) -> vec4<u32> {
            // Utility to pack multiple values into a rgba16uint (64 bits available).
            // Note that we store in a vec4<u32> but this gets written to a 4xu16.
            // See #212 for details.
            //
            // Clip the given value
            let v = min(value, u32(exp2(f32(bits))));
            // Determine bit-shift for each component
            let shift = vec4<i32>(
                p_pick_bits_used, p_pick_bits_used - 16, p_pick_bits_used - 32, p_pick_bits_used - 48,
            );
            // Prepare for next pack
            p_pick_bits_used = p_pick_bits_used + bits;
            // Apply the shift for each component
            let vv = vec4<u32>(v);
            let selector1 = vec4<bool>(shift[0] < 0, shift[1] < 0, shift[2] < 0, shift[3] < 0);
            let pick_new = select( vv << vec4<u32>(shift) , vv >> vec4<u32>(-shift) , selector1 );
            // Mask the components
            let mask = vec4<u32>(65535u);
            let selector2 = vec4<bool>( abs(shift[0]) < 32, abs(shift[1]) < 32, abs(shift[2]) < 32, abs(shift[3]) < 32 );
            return select( vec4<u32>(0u) , pick_new & mask , selector2 );
        }
        """

    def _code_lighting(self):
        return """
        """

    def _code_misc(self):
        # Small functions
        return """

        fn ndc_to_world_pos(ndc_pos: vec4<f32>) -> vec3<f32> {
            let ndc_to_world = u_stdinfo.cam_transform_inv * u_stdinfo.projection_transform_inv;
            let world_pos = ndc_to_world * ndc_pos;
            return world_pos.xyz / world_pos.w;
        }

        """
