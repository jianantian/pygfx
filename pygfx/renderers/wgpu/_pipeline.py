"""
This module implements the PipelineContainer (for compute and render pipelines).
This object is responsible for creating the native wgpu objects and doing the
actual dispatching / drawing.
"""

import wgpu

from ...resources import Buffer, TextureView
from ...utils import logger
from ._shader import BaseShader
from ._utils import to_vertex_format, to_texture_format
from ._update import update_resource, ALTTEXFORMAT
from . import registry


visibility_render = wgpu.ShaderStage.VERTEX | wgpu.ShaderStage.FRAGMENT
visibility_all = (
    wgpu.ShaderStage.VERTEX | wgpu.ShaderStage.FRAGMENT | wgpu.ShaderStage.COMPUTE
)


def get_pipeline_container_group(wobject, environment, shared):
    """Update the GPU objects associated with the given wobject. Returns
    quickly if no changes are needed. Only this function is used by the
    renderer.
    """

    # Get pipeline_container
    try:
        pipeline_container_group = wobject._wgpu_pipeline_container_group
    except AttributeError:
        pipeline_container_group = PipelineContainerGroup()
        wobject._wgpu_pipeline_container_group = pipeline_container_group
        changed_labels = {"create"}
    else:
        # Get whether the object has changes
        changed_labels = wobject.tracker.pop_changed()

    # Update if necessary - this part is defined to be fast if there are no changes
    pipeline_container_group.update(wobject, environment, shared, changed_labels)

    # Check if we need to update any resources. The number of resources
    # should typically be small.
    # todo: (in another PR). Keep track of resources that need an update globally, and let the renderer flush that on each draw
    flat_resources = pipeline_container_group.get_flat_resources()
    for kind, resource in flat_resources:
        our_version = getattr(resource, "_wgpu_" + kind, (-1, None))[0]
        if resource.rev > our_version:
            update_resource(shared.device, resource, kind)

    # Return the pipeline container group
    return pipeline_container_group


class Binding:
    """Simple object to hold together some information about a binding, for internal use.

    * name: the name in wgsl
    * type: "buffer/subtype", "sampler/subtype", "texture/subtype", "storage_texture/subtype".
      The subtype depends on the type:
      BufferBindingType, SamplerBindingType, TextureSampleType, or StorageTextureAccess.
    * resource: the Buffer, Texture or TextureView object.
    * visibility: wgpu.ShaderStage flag
    """

    def __init__(self, name, type, resource, visibility=visibility_render):
        if isinstance(visibility, str):
            visibility = getattr(wgpu.ShaderStage, visibility)
        self.name = name
        self.type = type
        self.resource = resource
        self.visibility = visibility

    def get_bind_group_descriptors(self, slot):
        resource = self.resource
        subtype = self.type.partition("/")[2]

        if self.type.startswith("buffer/"):
            assert isinstance(resource, Buffer)
            binding = {
                "binding": slot,
                "resource": {
                    "buffer": resource._wgpu_buffer[1],
                    "offset": 0,
                    "size": resource.nbytes,
                },
            }
            binding_layout = {
                "binding": slot,
                "visibility": self.visibility,
                "buffer": {
                    "type": getattr(wgpu.BufferBindingType, subtype),
                    "has_dynamic_offset": False,
                    "min_binding_size": 0,
                },
            }
        elif self.type.startswith("sampler/"):
            assert isinstance(resource, TextureView)
            binding = {"binding": slot, "resource": resource._wgpu_sampler[1]}
            binding_layout = {
                "binding": slot,
                "visibility": self.visibility,
                "sampler": {
                    "type": getattr(wgpu.SamplerBindingType, subtype),
                },
            }
        elif self.type.startswith("texture/"):
            assert isinstance(resource, TextureView)
            binding = {"binding": slot, "resource": resource._wgpu_texture_view[1]}
            dim = resource.view_dim
            dim = getattr(wgpu.TextureViewDimension, dim, dim)
            sample_type = getattr(wgpu.TextureSampleType, subtype, subtype)
            # Derive sample type from texture
            if sample_type == "auto":
                fmt = to_texture_format(resource.format)
                fmt = ALTTEXFORMAT.get(fmt, [fmt])[0]
                if "float" in fmt or "norm" in fmt:
                    sample_type = wgpu.TextureSampleType.float
                    # For float32 wgpu does not allow the sampler to be filterable,
                    # except when the native-only feature
                    # TEXTURE_ADAPTER_SPECIFIC_FORMAT_FEATURES is set,
                    # which wgpu-py does by default.
                    # if "32float" in fmt:
                    #     sample_type = wgpu.TextureSampleType.unfilterable_float
                elif "uint" in fmt:
                    sample_type = wgpu.TextureSampleType.uint
                elif "sint" in fmt:
                    sample_type = wgpu.TextureSampleType.sint
                elif "depth" in fmt:
                    sample_type = wgpu.TextureSampleType.depth
                else:
                    raise ValueError("Could not determine texture sample type.")
            binding_layout = {
                "binding": slot,
                "visibility": self.visibility,
                "texture": {
                    "sample_type": sample_type,
                    "view_dimension": dim,
                    "multisampled": False,
                },
            }
        elif self.type.startswith("storage_texture/"):
            assert isinstance(resource, TextureView)
            binding = {"binding": slot, "resource": resource._wgpu_texture_view[1]}
            dim = resource.view_dim
            dim = getattr(wgpu.TextureViewDimension, dim, dim)
            fmt = to_texture_format(resource.format)
            fmt = ALTTEXFORMAT.get(fmt, [fmt])[0]
            binding_layout = {
                "binding": slot,
                "visibility": self.visibility,
                "storage_texture": {
                    "access": getattr(wgpu.StorageTextureAccess, subtype),
                    "format": fmt,
                    "view_dimension": dim,
                },
            }

        return binding, binding_layout


class PipelineContainerGroup:
    """This is a thin wrapper for a list of compute pipeline containers,
    and render pipeline containers. The purpose of this object is to
    obtain the appropiate shader objects and store them.
    """

    def __init__(self):
        self.compute_containers = None
        self.render_containers = None

    def update(self, wobject, environment, shared, changed):
        """Update the pipeline containers that are wrapped. Creates (and re-creates)
        the containers if necessary.
        """

        if "create" in changed:
            self.compute_containers = []
            self.render_containers = []

            # Get render function for this world object,
            # and use it to get a high-level description of pipelines.
            renderfunc = registry.get_render_function(wobject)
            if renderfunc is None:
                raise ValueError(
                    f"Could not get a render function for {wobject.__class__.__name__} "
                    f"with {wobject.material.__class__.__name__}"
                )

            # Call render function
            with wobject.tracker.track_usage("create"):
                shaders = renderfunc(wobject)
                if isinstance(shaders, BaseShader):
                    shaders = [shaders]

            # Divide result over two bins, one for compute, and one for render
            for shader in shaders:
                assert isinstance(shader, BaseShader)
                if shader.type == "compute":
                    self.compute_containers.append(ComputePipelineContainer(shader))
                elif shader.type == "render":
                    self.render_containers.append(RenderPipelineContainer(shader))
                else:
                    raise ValueError(f"Shader type {shader.type} is unknown.")

        for container in self.compute_containers:
            container.update(wobject, environment, shared, changed)
        for container in self.render_containers:
            container.update(wobject, environment, shared, changed)

    def get_flat_resources(self):
        """Get a set of the combined resources of all pipeline containers."""
        flat_resources = set()
        for container in self.compute_containers:
            flat_resources.update(container.flat_resources)
        for container in self.render_containers:
            flat_resources.update(container.flat_resources)
        return flat_resources


class PipelineContainer:
    """The pipeline container stores the wgpu pipeline object as well as intermediate
    steps needed to create it. When an dependency of a certain step changes (which we track)
    then only the steps below it need to be re-run.
    """

    def __init__(self, shader):
        self.shader = shader

        # The info that the shader generates
        self.shader_hash = b""
        self.resources = None
        self.pipeline_info = None
        self.render_info = None

        # The wgpu objects that we generate
        # These map blend_mode to a list of objects (one for each pass).
        # For compute shaders the blend_mode is always "" and there is
        # one object in each.
        self.wgpu_shaders = {}
        self.wgpu_pipelines = {}

        # The bindings map group_index -> group
        self.bind_group_layout_descriptors = []
        self.wgpu_bind_group_layouts = []
        self.wgpu_bind_groups = []

        # A flat list of all buffers, textures, samplers etc. in use.
        # This is to allow fast iteration for updating the resources
        # (uploading new data to buffers and textures).
        self.flat_resources = []

        # A flag to indicate that an error occured and we cannot dispatch
        self.broken = False

    def remove_env_hash(self, env_hash):
        """Called from the environment when it becomes inactive.
        This allows this object to remove all references to wgpu objects
        that won't be used, reclaiming memory on both CPU and GPU.
        """
        self.wgpu_shaders.pop(env_hash, None)
        self.wgpu_pipelines.pop(env_hash, None)

    def update(self, wobject, environment, shared, changed):
        """Make sure that the pipeline is up-to-date."""

        if isinstance(self, RenderPipelineContainer):
            env_hash = environment.hash
        else:
            env_hash = ""

        # Ensure that the information provided by the shader is up-to-date
        if changed:
            try:
                self.update_shader_data(wobject, shared, changed)
            except Exception as err:
                self.broken = 1
                raise err
            else:
                self.broken = 0

        # Ensure that the (environment specific) wgpu objects are up-to-date
        if not self.broken:
            try:
                self.update_wgpu_data(wobject, environment, shared, env_hash, changed)
            except Exception as err:
                self.broken = 2
                raise err
            else:
                self.broken = False

        if changed:
            logger.info(f"{wobject} shader update: {', '.join(sorted(changed))}.")

    def update_shader_data(self, wobject, shared, changed):
        """Update the info that applies to all passes and environments."""

        if "create" in changed:
            changed.update(("resources", "pipeline_info", "render_info"))

        if "resources" in changed:
            with wobject.tracker.track_usage("!resources"):
                self.resources = self.shader.get_resources(wobject, shared)
            self.flat_resources = self.collect_flat_resources()
            for kind, resource in self.flat_resources:
                update_resource(shared.device, resource, kind)
            self._check_resources()
            self.update_shader_hash()
            self.update_bind_groups(shared.device)

        if "pipeline_info" in changed:
            with wobject.tracker.track_usage("pipeline_info"):
                self.pipeline_info = self.shader.get_pipeline_info(wobject, shared)
            self._check_pipeline_info()
            changed.add("render_info")
            self.wgpu_pipelines = {}

        if "render_info" in changed:
            with wobject.tracker.track_usage("render_info"):
                self.render_info = self.shader.get_render_info(wobject, shared)
            self._check_render_info()

    def update_wgpu_data(self, wobject, environment, shared, env_hash, changed):
        """Update the actual wgpu objects."""

        if self.wgpu_shaders.get(env_hash, None) is None:
            environment.register_pipeline_container(self)  # allows us to clean up
            changed.add("compile_shader")
            self.wgpu_shaders[env_hash] = self._compile_shaders(
                shared.device, environment.blender
            )

        if self.wgpu_pipelines.get(env_hash, None) is None:
            changed.add("compose_pipeline")
            shader_modules = self.wgpu_shaders[env_hash]
            self.wgpu_pipelines[env_hash] = self._compose_pipelines(
                shared.device, environment.blender, shader_modules
            )

    def update_shader_hash(self):
        """Update the shader hash, invalidating the wgpu shaders if it changed."""
        if self.shader.hash() != self.shader_hash:
            self.shader_hash = self.shader.hash()
            self.wgpu_shaders = {}

    def update_bind_groups(self, device):
        """
        - Calculate new bind_group_layout_descriptors (simple dicts).
        - When this has changed from our last version, we also update
          wgpu_bind_group_layouts and reset self.wgpu_pipelines
        - Calculate new wgpu_bind_groups.
        """
        binding_groups = self.resources["bindings"]

        # Check the bindings structure
        for i, group in binding_groups.items():
            assert isinstance(i, int)
            assert isinstance(group, dict)
            for j, b in group.items():
                assert isinstance(j, int)
                assert isinstance(b, Binding)

        # Create two new dicts that correspond closely to the bindings
        # in the resources. Except this turns the dicts into lists.
        # These are the descriptors to create the wgpu bind groups and
        # bind group layouts.
        bg_descriptors = []
        bg_layout_descriptors = []
        for group_id in sorted(binding_groups.keys()):
            bindings_dict = binding_groups[group_id]
            while len(bg_descriptors) <= group_id:
                bg_descriptors.append([])
                bg_layout_descriptors.append([])
            bg_descriptor = bg_descriptors[group_id]
            bg_layout_descriptor = bg_layout_descriptors[group_id]
            for slot in sorted(bindings_dict.keys()):
                binding = bindings_dict[slot]
                binding_des, binding_layout_des = binding.get_bind_group_descriptors(
                    slot
                )
                bg_descriptor.append(binding_des)
                bg_layout_descriptor.append(binding_layout_des)

        # Clean up any trailing empty descriptors
        while bg_descriptors and not bg_descriptors[-1]:
            bg_descriptors.pop(-1)
            bg_layout_descriptors.pop(-1)

        # If the layout has changed, we need a new pipeline
        if self.bind_group_layout_descriptors != bg_layout_descriptors:
            self.bind_group_layout_descriptors = bg_layout_descriptors
            # Invalidate the pipeline
            self.wgpu_pipelines = {}
            # Create wgpu objects for the bind group layouts
            self.wgpu_bind_group_layouts = []
            for bg_layout_descriptor in bg_layout_descriptors:
                bind_group_layout = device.create_bind_group_layout(
                    entries=bg_layout_descriptor
                )
                self.wgpu_bind_group_layouts.append(bind_group_layout)

        # Always create wgpu objects for the bind groups. Note that this dict
        # includes the buffer texture objects.
        self.wgpu_bind_groups = []
        for bg_descriptor, layout in zip(bg_descriptors, self.wgpu_bind_group_layouts):
            bind_group = device.create_bind_group(layout=layout, entries=bg_descriptor)
            self.wgpu_bind_groups.append(bind_group)

    def collect_flat_resources(self):
        """Collect a list of all used resources, and also set their usage."""
        resources = self.resources
        pipeline_resources = []  # List, because order matters

        buffer = resources.get("index_buffer", None)
        if buffer is not None:
            buffer._wgpu_usage |= wgpu.BufferUsage.INDEX | wgpu.BufferUsage.STORAGE
            pipeline_resources.append(("buffer", buffer))

        for buffer in resources.get("vertex_buffers", {}).values():
            buffer._wgpu_usage |= wgpu.BufferUsage.VERTEX | wgpu.BufferUsage.STORAGE
            pipeline_resources.append(("buffer", buffer))

        for group_dict in resources.get("bindings", {}).values():
            for binding in group_dict.values():
                resource = binding.resource
                if binding.type.startswith("buffer/"):
                    assert isinstance(resource, Buffer)
                    pipeline_resources.append(("buffer", resource))
                    if "uniform" in binding.type:
                        resource._wgpu_usage |= wgpu.BufferUsage.UNIFORM
                    elif "storage" in binding.type:
                        resource._wgpu_usage |= wgpu.BufferUsage.STORAGE
                        if "indices" in binding.name:
                            resource._wgpu_usage |= wgpu.BufferUsage.INDEX
                        else:
                            resource._wgpu_usage |= wgpu.BufferUsage.VERTEX
                elif binding.type.startswith("sampler/"):
                    assert isinstance(resource, TextureView)
                    pipeline_resources.append(("sampler", resource))
                elif binding.type.startswith("texture/"):
                    assert isinstance(resource, TextureView)
                    resource.texture._wgpu_usage |= wgpu.TextureUsage.TEXTURE_BINDING
                    pipeline_resources.append(("texture", resource.texture))
                    pipeline_resources.append(("texture_view", resource))
                elif binding.type.startswith("storage_texture/"):
                    assert isinstance(resource, TextureView)
                    resource.texture._wgpu_usage |= wgpu.TextureUsage.STORAGE_BINDING
                    pipeline_resources.append(("texture", resource.texture))
                    pipeline_resources.append(("texture_view", resource))
                else:
                    raise RuntimeError(
                        f"Unknown resource binding {binding.name} of type {binding.type}"
                    )

        return pipeline_resources


class ComputePipelineContainer(PipelineContainer):
    """Container for compute pipelines."""

    # These checks are here to validate the output of the shader
    # methods, not for user code. So its ok to use assertions here.

    def _check_pipeline_info(self):
        pipeline_info = self.pipeline_info
        assert isinstance(pipeline_info, dict)

        assert set(pipeline_info.keys()) == set(), f"{pipeline_info.keys()}"

    def _check_render_info(self):
        render_info = self.render_info
        assert isinstance(render_info, dict)

        assert set(render_info.keys()) == {"indices"}, f"{render_info.keys()}"

        indices = render_info["indices"]
        assert isinstance(indices, (tuple, list))
        assert all(isinstance(i, int) for i in indices)
        assert len(indices) == 3

    def _check_resources(self):
        resources = self.resources
        assert isinstance(resources, dict)
        assert set(resources.keys()) == {"bindings"}, f"{resources.keys()}"

    def _compile_shaders(self, device, blender):
        """Compile the templateds wgsl shader to a wgpu shader module."""
        wgsl = self.shader.generate_wgsl()
        shader_module = cache.get_shader_module(device, wgsl)
        return {0: shader_module}

    def _compose_pipelines(self, device, blender, shader_modules):
        """Create the wgpu pipeline object from the shader and bind group layouts."""

        # Create pipeline layout object from list of layouts
        pipeline_layout = device.create_pipeline_layout(
            bind_group_layouts=self.wgpu_bind_group_layouts
        )

        # Create pipeline object
        pipeline = device.create_compute_pipeline(
            layout=pipeline_layout,
            compute={"module": shader_modules[0], "entry_point": "main"},
        )

        return {0: pipeline}

    def dispatch(self, compute_pass):
        """Dispatch the pipeline, doing the actual compute job."""
        if self.broken:
            return

        # Collect what's needed
        pipeline = self.wgpu_pipeline
        indices = self.render_info["indices"]
        bind_groups = self.wgpu_bind_groups

        # Set pipeline and resources
        compute_pass.set_pipeline(pipeline)
        for bind_group_id, bind_group in enumerate(bind_groups):
            compute_pass.set_bind_group(bind_group_id, bind_group, [], 0, 999999)

        # Compute!
        compute_pass.dispatch_workgroups(*indices)


class RenderPipelineContainer(PipelineContainer):
    """Container for render pipelines."""

    def __init__(self, shader):
        super().__init__(shader)
        self.strip_index_format = 0
        self.vertex_buffer_descriptors = []

    def _check_pipeline_info(self):
        pipeline_info = self.pipeline_info
        assert isinstance(pipeline_info, dict)

        expected = {"cull_mode", "primitive_topology"}
        assert set(pipeline_info.keys()) == expected, f"{pipeline_info.keys()}"

        self.update_index_buffer_format()

    def _check_render_info(self):
        render_info = self.render_info
        assert isinstance(render_info, dict)

        expected = {"indices", "render_mask"}
        assert set(render_info.keys()) == expected, f"{render_info.keys()}"

        indices = render_info["indices"]
        assert isinstance(indices, (tuple, list))
        assert all(isinstance(i, int) for i in indices)
        assert len(indices) in (2, 4, 5)
        if len(indices) == 2:
            render_info["indices"] = indices[0], indices[1], 0, 0

        render_mask = render_info["render_mask"]
        assert isinstance(render_mask, int) and render_mask in (1, 2, 3)

    def _check_resources(self):
        resources = self.resources
        assert isinstance(resources, dict)

        expected = {"index_buffer", "vertex_buffers", "bindings"}
        assert set(resources.keys()) == expected, f"{resources.keys()}"

        assert isinstance(resources["index_buffer"], (None.__class__, Buffer))
        assert isinstance(resources["vertex_buffers"], dict)
        assert all(isinstance(slot, int) for slot in resources["vertex_buffers"].keys())
        assert all(isinstance(b, Buffer) for b in resources["vertex_buffers"].values())

        self.update_index_buffer_format()
        self.update_vertex_buffer_descriptors()

    def update_index_buffer_format(self):
        if not self.resources or not self.pipeline_info:
            return
        # Set strip_index_format
        index_format = wgpu.IndexFormat.uint32
        index_buffer = self.resources["index_buffer"]
        if index_buffer is not None:
            index_format = to_vertex_format(index_buffer.format)
            index_format = index_format.split("x")[0].replace("s", "u")
        strip_index_format = 0
        if "strip" in self.pipeline_info["primitive_topology"]:
            strip_index_format = index_format
        # Trigger a pipeline rebuild?
        if self.strip_index_format != strip_index_format:
            self.strip_index_format = strip_index_format
            self.wgpu_pipelines = {}

    def update_vertex_buffer_descriptors(self):
        # todo: we can probably expose multiple attributes per buffer using a BufferView
        vertex_buffer_descriptors = []
        for slot, buffer in self.resources["vertex_buffers"].items():
            vbo_des = {
                "array_stride": buffer.nbytes // buffer.nitems,
                "step_mode": wgpu.VertexStepMode.vertex,  # vertex or instance
                "attributes": [
                    {
                        "format": to_vertex_format(buffer.format),
                        "offset": 0,
                        "shader_location": slot,
                    }
                ],
            }
            vertex_buffer_descriptors.append(vbo_des)
        # Trigger a pipeline rebuild?
        if vertex_buffer_descriptors != self.vertex_buffer_descriptors:
            self.vertex_buffer_descriptors = vertex_buffer_descriptors
            self.wgpu_pipelines = {}

    def _compile_shaders(self, device, blender):
        """Compile the templated shader to a list of wgpu shader modules
        (one for each pass of the blender).
        """
        shader_modules = {}
        for pass_index in range(blender.get_pass_count()):
            color_descriptors = blender.get_color_descriptors(pass_index)
            if not color_descriptors:
                continue

            wgsl = self.shader.generate_wgsl(**blender.get_shader_kwargs(pass_index))
            shader_modules[pass_index] = cache.get_shader_module(device, wgsl)

        return shader_modules

    def _compose_pipelines(self, device, blender, shader_modules):
        """Create a list of wgpu pipeline objects from the shader, bind group
        layouts and other pipeline info (one for each pass of the blender).
        """

        # todo: cache the pipeline with a lot of things as the hash
        # todo: cache vertex descriptors

        strip_index_format = self.strip_index_format
        vertex_buffer_descriptors = self.vertex_buffer_descriptors
        primitive_topology = self.pipeline_info["primitive_topology"]
        cull_mode = self.pipeline_info["cull_mode"]

        # Create pipeline layout object from list of layouts
        pipeline_layout = device.create_pipeline_layout(
            bind_group_layouts=self.wgpu_bind_group_layouts
        )

        # Instantiate the pipeline objects
        pipelines = {}
        for pass_index in range(blender.get_pass_count()):
            color_descriptors = blender.get_color_descriptors(pass_index)
            depth_descriptor = blender.get_depth_descriptor(pass_index)
            if not color_descriptors:
                continue

            shader_module = shader_modules[pass_index]

            pipelines[pass_index] = device.create_render_pipeline(
                layout=pipeline_layout,
                vertex={
                    "module": shader_module,
                    "entry_point": "vs_main",
                    "buffers": vertex_buffer_descriptors,
                },
                primitive={
                    "topology": primitive_topology,
                    "strip_index_format": strip_index_format,
                    "front_face": wgpu.FrontFace.ccw,
                    "cull_mode": cull_mode,
                },
                depth_stencil={
                    **depth_descriptor,
                    "stencil_front": {},  # use defaults
                    "stencil_back": {},  # use defaults
                },
                multisample={
                    "count": 1,
                    "mask": 0xFFFFFFFF,
                    "alpha_to_coverage_enabled": False,
                },
                fragment={
                    "module": shader_module,
                    "entry_point": "fs_main",
                    "targets": color_descriptors,
                },
            )

        return pipelines

    def draw(self, render_pass, environment, pass_index, render_mask):
        """Draw the pipeline, doing the actual rendering job."""
        if self.broken:
            return

        if not (render_mask & self.render_info["render_mask"]):
            return
        env_hash = environment.hash

        # Collect what's needed
        pipeline = self.wgpu_pipelines[env_hash][pass_index]
        indices = self.render_info["indices"]
        index_buffer = self.resources["index_buffer"]
        vertex_buffers = self.resources["vertex_buffers"]
        bind_groups = self.wgpu_bind_groups

        # Set pipeline and resources
        render_pass.set_pipeline(pipeline)
        for slot, vbuffer in vertex_buffers.items():
            render_pass.set_vertex_buffer(
                slot,
                vbuffer._wgpu_buffer[1],
                vbuffer.vertex_byte_range[0],
                vbuffer.vertex_byte_range[1],
            )
        for bind_group_id, bind_group in enumerate(bind_groups):
            render_pass.set_bind_group(bind_group_id, bind_group, [], 0, 99)

        # Draw!
        # draw_indexed(count_v, count_i, first_vertex, base_vertex, first_instance)
        # draw(count_vertex, count_instance, first_vertex, first_instance)
        if index_buffer is not None:
            render_pass.set_index_buffer(index_buffer, 0, index_buffer.size)
            if len(indices) == 4:
                base_vertex = 0  # A value added to each index before reading [...]
                indices = list(indices)
                indices.insert(-1, base_vertex)
            render_pass.draw_indexed(*indices)
        else:
            render_pass.draw(*indices)


class Cache:
    def __init__(self):
        self._d = {}

    def get(self, key):
        return self._d.get(key, None)

    def set(self, key, value):
        self._d[key] = value

    def get_shader_module(self, device, source):
        """Compile a shader module object, or re-use it from the cache."""
        # todo: also release shader modules that are no longer used
        # todo: cache more objects, like pipelines once we figure out how to clean things up

        assert isinstance(source, str)
        key = source  # or hash(code)

        m = self.get(key)
        if m is None:
            m = device.create_shader_module(code=source)
            self.set(key, m)

        return m


cache = Cache()
