import wgpu  # only for flags/enums

from . import register_wgpu_render_function
from ._shadercomposer import Binding, WorldObjectShader
from ...objects import Text
from ...materials import TextMaterial
from ...utils.text import atlas

# from ...resources import Texture, TextureView


@register_wgpu_render_function(Text, TextMaterial)
def text_renderer(render_info):
    """Render function capable of rendering text."""

    wobject = render_info.wobject
    geometry = wobject.geometry
    material = wobject.material
    shader = TextShader(
        render_info,
        screen_space=material.screen_space,
    )
    n = geometry.positions.nitems * 6

    bindings = [
        Binding("u_stdinfo", "buffer/uniform", render_info.stdinfo_uniform),
        Binding("u_wobject", "buffer/uniform", wobject.uniform_buffer),
        Binding("u_material", "buffer/uniform", material.uniform_buffer),
        Binding("s_indices", "buffer/read_only_storage", geometry.indices, "VERTEX"),
        Binding(
            "s_coverages", "buffer/read_only_storage", geometry.coverages, "VERTEX"
        ),
        Binding(
            "s_positions", "buffer/read_only_storage", geometry.positions, "VERTEX"
        ),
    ]

    view = atlas.texture_view
    bindings.append(Binding("s_atlas", "sampler/filtering", view, "FRAGMENT"))
    bindings.append(Binding("t_atlas", "texture/auto", view, "FRAGMENT"))

    # Let the shader generate code for our bindings
    for i, binding in enumerate(bindings):
        shader.define_binding(0, i, binding)

    # Determine in what render passes this objects must be rendered
    suggested_render_mask = 3  # todo: needs work

    # Put it together!
    return [
        {
            "suggested_render_mask": suggested_render_mask,
            "render_shader": shader,
            "primitive_topology": wgpu.PrimitiveTopology.triangle_list,
            "indices": (range(n), range(1)),
            "vertex_buffers": {},
            "bindings0": bindings,
        }
    ]


class TextShader(WorldObjectShader):
    def get_code(self):
        return (
            self.get_definitions()
            + self.common_functions()
            + self.vertex_shader()
            + self.fragment_shader()
        )

    def vertex_shader(self):
        return """

        struct VertexInput {
            @builtin(vertex_index) vertex_index : u32,
        };


        @stage(vertex)
        fn vs_main(in: VertexInput) -> Varyings {

            let size = 22.0;  // todo: where to get the size?

            let raw_index = i32(in.vertex_index);
            let index = raw_index / 6;
            let sub_index = raw_index % 6;

            let glyph_pos = load_s_positions(index) * size;
            let coverage = load_s_coverages(index);

            let screen_factor = u_stdinfo.logical_size.xy / 2.0;

            var deltas = array<vec2<f32>, 6>(
                vec2<f32>(0.0, 0.0),
                vec2<f32>(0.0, coverage.y),
                vec2<f32>( coverage.x, 0.0),
                vec2<f32>(0.0,  coverage.y),
                vec2<f32>( coverage.x, 0.0),
                vec2<f32>( coverage.x,  coverage.y),
                //vec2<f32>(-1.0, -1.0),
                //vec2<f32>(-1.0,  1.0),
                //vec2<f32>( 1.0, -1.0),
                //vec2<f32>(-1.0,  1.0),
                //vec2<f32>( 1.0, -1.0),
                //vec2<f32>( 1.0,  1.0),
            );
            let point_coord = deltas[sub_index] * size;

            $$ if screen_space

                // We take the object's pos (model pos is origin), move to NDC, and apply the
                // glyph-positioning in logical screen coords.

                let raw_pos = vec3<f32>(0.0, 0.0, 0.0);
                let world_pos = u_wobject.world_transform * vec4<f32>(raw_pos, 1.0);
                let ndc_pos = u_stdinfo.projection_transform * u_stdinfo.cam_transform * world_pos;
                let delta_ndc = (glyph_pos.xy + vec2<f32>(1.0, -1.0) * point_coord) / screen_factor;

            $$ else

                // We take the glyph positions as model pos, move to world and then NDC.

                let raw_pos = glyph_pos + point_coord;
                let world_pos = u_wobject.world_transform * vec4<f32>(raw_pos, 0.0, 1.0);
                let ndc_pos = u_stdinfo.projection_transform * u_stdinfo.cam_transform * world_pos;
                let delta_ndc = vec2<f32>(0.0, 0.0);

            $$ endif

            var varyings: Varyings;
            varyings.position = vec4<f32>(ndc_pos.xy + delta_ndc * ndc_pos.w, ndc_pos.zw);
            varyings.world_pos = vec3<f32>(world_pos.xyz / world_pos.w);
            varyings.pointcoord = vec2<f32>(point_coord);
            varyings.size = f32(size);
            varyings.atlas_index = i32(load_s_indices(index));

            // Picking
            varyings.pick_idx = u32(index);

            return varyings;
        }
        """

    def fragment_shader(self):
        return """

        @stage(fragment)
        fn fs_main(varyings: Varyings) -> FragmentOutput {

            let max_d = 0.5 * varyings.size;

            // TODO: the below is likely not fully correct yet :)
            //
            // The glyph is represented in the texture as a square region
            // of fixed size, though only a subrectangle is actually used:
            // the coverage.
            //
            // o-------   →  pointcoord.x in logical pizels
            // |       |
            // |       |  ↓  pointcoord.y in logical pixels
            // |       |
            //  -------

            let atlas_size = textureDimensions(t_atlas);
            let glyph_size = GLYPH_SIZE;
            let index = varyings.atlas_index;
            let ncols = atlas_size.x / glyph_size;
            let col_row = vec2<i32>(index % ncols, index / ncols);
            let left_top = col_row * glyph_size;

            // pointcoord is in logical pixels (and so is size)
            let localcoord = f32(glyph_size) * varyings.pointcoord / varyings.size;
            let texcoord_f = (vec2<f32>(left_top) + localcoord) / vec2<f32>(atlas_size);
            let texcoord_i = left_top + vec2<i32>(localcoord + 0.5);

            // Sample value
            // TODO: This is an SDF
            // TODO: gamma correction
            let value = textureSample(t_atlas, s_atlas, texcoord_f).r;
            // textureLoad(t_atlas, let_texcoord_i, 0);

            var color: vec4<f32> = u_material.color;
            color.a = color.a * u_material.opacity * value;

            if color.a <= 0.0 { discard; }

            // Debug
            color = vec4<f32>(mix(vec3<f32>(0.2, 0.0, 0.0), color.rgb, value), 1.0);
            //color.g = varyings.pointcoord.y / varyings.size;

            // Wrap up
            apply_clipping_planes(varyings.world_pos);
            var out = get_fragment_output(varyings.position.z, color);

            $$ if write_pick
            // The wobject-id must be 20 bits. In total it must not exceed 64 bits.
            out.pick = (
                pick_pack(u32(u_wobject.id), 20) +
                pick_pack(varyings.pick_idx, 26) +
                pick_pack(u32(varyings.pointcoord.x + 256.0), 9) +
                pick_pack(u32(varyings.pointcoord.y + 256.0), 9)
            );
            $$ endif

            return out;
        }
        """.replace(
            "GLYPH_SIZE", str(atlas.glyph_size)
        )
