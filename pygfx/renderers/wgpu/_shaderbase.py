"""
Implements the base shader class. The shader is responsible for
providing the WGSL code, as well as providing the information to connect
it to the resources (buffers and textures) and some details on the
pipeline and rendering.
"""

import re
import hashlib

import jinja2
import numpy as np

from ...utils import array_from_shadertype
from ...resources import Buffer
from ._utils import to_vertex_format, to_texture_format


jinja_env = jinja2.Environment(
    block_start_string="{$",
    block_end_string="$}",
    variable_start_string="{{",
    variable_end_string="}}",
    line_statement_prefix="$$",
    undefined=jinja2.StrictUndefined,
)


varying_types = ["f32", "vec2<f32>", "vec3<f32>", "vec4<f32>"]
varying_types = (
    varying_types
    + [t.replace("f", "i") for t in varying_types]
    + [t.replace("f", "u") for t in varying_types]
)


re_varying_getter = re.compile(r"[\s,\(\[]varyings\.(\w+)", re.UNICODE)
re_varying_setter = re.compile(r"\A\s*?varyings\.(\w+)(\.\w+)?\s*?\=")
builtin_varyings = {"position": "vec4<f32>"}


def resolve_varyings(wgsl):
    """Resolve varyings in the given wgsl:
    * Detect varyings being used.
    * Check that these are also set.
    * Remove assignments of varyings that are not used.
    * Include the Varyings struct.
    """
    assert isinstance(wgsl, str)

    # Split into lines, which is easier to process. Ensure it ends with newline in the end.
    lines = wgsl.splitlines()
    while lines and not lines[0].strip():
        lines.pop(0)
    while lines and not lines[-1].strip():
        lines.pop(-1)
    lines.append("")

    # Prepare dicts that map name to list-of-linenr. And a tupe dict.
    assigned_varyings = {}
    used_varyings = {}
    types = {}  # varying types

    # We try to find the function that first uses the Varyings struct.
    struct_insert_pos = None

    # Go over all lines to:
    # - find the lines where a varying is set
    # - collect the types of these varyings
    for linenr, line in enumerate(lines):
        match = re_varying_setter.match(line)
        if match:
            # Get parts
            name = match.group(1)
            attr = match.group(2)
            # Handle builtin
            if name in builtin_varyings:
                used_varyings[name] = []
                types[name] = builtin_varyings[name]
            # Find type
            type = line[match.end() :].split("(")[0].strip().replace(" ", "")
            if type not in varying_types:
                type = ""
            # Triage
            if attr:
                pass  # Not actually a type but an attribute access
            elif not type:
                raise TypeError(
                    f"Varying {name!r} assignment needs an explicit cast (of a correct type), e.g. `varying.{name} = f32(3.0);`:\n{line}"
                )
            elif name in types and type != types[name]:
                raise TypeError(
                    f"Varying {name!r} assignment does not match expected type {types[name]}:\n{line}"
                )
            else:
                types[name] = type
            # Store position
            assigned_varyings.setdefault(name, []).append(linenr)

    # Go over all lines to:
    # - collect all used varyings
    # - find where the vertex-shader starts
    in_vertex_shader = False
    current_func_linenr = 0
    for linenr, line in enumerate(lines):
        line = line.strip()
        # Detect when we enter a new function
        if line.startswith("fn "):
            current_func_linenr = linenr
            if line.startswith("fn vs_main"):
                in_vertex_shader = True
            else:
                in_vertex_shader = False
        # Remove comments (shader code has no strings that can contain slashes)
        line = line.split("//")[0]
        if "Varyings" in line and struct_insert_pos is None:
            struct_insert_pos = current_func_linenr
        # Everything we find here is a match (prepend a space to allow an easier regexp)
        for match in re_varying_getter.finditer(" " + line):
            name = match.group(1)
            this_varying_is_set_on_this_line = linenr in assigned_varyings.get(name, [])
            if this_varying_is_set_on_this_line:
                pass
            elif in_vertex_shader:
                # If varyings are used in another way than setting, in the vertex shader,
                # we should either consider them "used", or possibly break the shader if
                # the used varying is disabled. So let's just not allow it.
                raise TypeError(
                    f"Varying {name!r} is read in the vertex shader, but only writing is allowed:\n{line}"
                )
            else:
                used_varyings.setdefault(name, []).append(linenr)

    # Check if all used varyings are assigned
    for name in used_varyings:
        if name not in assigned_varyings:
            line = lines[used_varyings[name][0]]
            raise TypeError(f"Varying {name!r} is read, but not assigned:\n{line}")

    # Comment-out the varying setter if its unused elsewhere in the shader
    for name, linenrs in assigned_varyings.items():
        if name not in used_varyings:
            for linenr in linenrs:
                line = lines[linenr]
                indent = line[: len(line) - len(line.lstrip())]
                lines[linenr] = indent + "// unused: " + line[len(indent) :]
                # Deal with multiple-line assignments
                line_s = line.strip()
                while not line_s.endswith(";"):
                    linenr += 1
                    line_s = lines[linenr].strip()
                    unexpected = "fn ", "struct ", "var ", "let ", "}"
                    if line_s.startswith(unexpected) or linenr == len(lines) - 1:
                        raise TypeError(
                            f"Varying {name!r} assignment seems to be missing a semicolon:\n{line}"
                        )
                    lines[linenr] = indent + "// " + line_s

    # Build and insert the struct
    if struct_insert_pos is not None:
        # Maybe we should move up a bit
        if struct_insert_pos > 0:
            if lines[struct_insert_pos - 1].lstrip().startswith("@"):
                struct_insert_pos -= 1
        # First divide into slot-based and builtins
        used_varyings = set(used_varyings)
        used_builtins = used_varyings.intersection(builtin_varyings)
        used_slots = used_varyings.difference(used_builtins)
        used_slots = list(sorted(used_slots))
        # Build struct
        struct_lines = ["struct Varyings {"]
        for slotnr, name in enumerate(used_slots):
            struct_lines.append(f"    @location({slotnr}) {name} : {types[name]},")
        for name in sorted(used_builtins):
            struct_lines.append(f"    @builtin({name}) {name} : {types[name]},")
        struct_lines.append("};\n")
        # Apply indentation and insert
        line = lines[struct_insert_pos]
        indent = line[: len(line) - len(line.lstrip())]
        struct_lines = [indent + line for line in struct_lines]
        lines.insert(struct_insert_pos, "\n".join(struct_lines))
    else:
        assert not used_varyings, "woops, did not expect used_varyings here"

    # Return modified code
    return "\n".join(lines)


re_depth_setter = re.compile(r"\A\s*?out\.depth\s*?\=")


def resolve_depth_output(wgsl):
    """When out.depth is set (in the fragment shader), adjust the FragmentOutput
    to accept depth.
    """
    assert isinstance(wgsl, str)

    # Split into lines, which is easier to process. Ensure it ends with newline in the end.
    lines = wgsl.splitlines()
    while lines and not lines[0].strip():
        lines.pop(0)
    while lines and not lines[-1].strip():
        lines.pop(-1)
    lines.append("")

    # Detect whether the depth is set in the shader. We're going to assume
    # this is in the fragment shader. We check for "out.depth =".
    # Background: by default the depth is based on the geometry (set
    # by vertex shader and interpolated). It is possible for a fragment
    # shader to write the depth instead. If this is done, the GPU cannot
    # do early depth testing; the fragment shader must be run for the
    # depth to be known.
    depth_is_set = False
    struct_linrnr = -1
    for linenr, line in enumerate(lines):
        if line.lstrip().startswith("struct FragmentOutput {"):
            struct_linrnr = linenr
        elif re_depth_setter.match(line):
            depth_is_set = True
            if struct_linrnr >= 0:
                break

    if depth_is_set:
        if struct_linrnr < 0:
            raise TypeError("FragmentOutput definition not found.")
        depth_field = "    @builtin(frag_depth) depth : f32,"
        line = lines[struct_linrnr]
        indent = line[: len(line) - len(line.lstrip())]
        lines.insert(struct_linrnr + 1, indent + depth_field)

    return "\n".join(lines)


class BaseShader:
    """Base shader object to compose and template shaders using jinja2.

    Templating variables can be provided as kwargs, set (and get) as attributes,
    or passed as kwargs to ``generate_wgsl()``.

    The idea is that this class is subclassed, and that methods are
    implemented that return (templated) shader code. The purpose of
    using methods for this is to easier navigate/structure parts of the
    shader. Subclasses should also implement ``get_code()`` that simply
    composes the different parts of the total shader.
    """

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self._typedefs = {}
        self._binding_codes = {}

    def __setitem__(self, key, value):
        if hasattr(self.__class__, key):
            msg = f"Templating variable {key} causes name clash with class attribute."
            raise KeyError(msg)
        self.kwargs[key] = value

    def __getitem__(self, key):
        return self.kwargs[key]

    def hash(self):
        """A hash of the current state of the shader. If the hash changed,
        it's likely that the shader changed.
        """
        h = hashlib.sha1()
        h.update(repr(self.kwargs).encode())
        h.update(self.code_definitions().encode())
        return h.hexdigest()

    def code_definitions(self):
        """Get the WGSL definitions of types and bindings (uniforms, storage
        buffers, samplers, and textures).
        """
        code = (
            "\n".join(self._typedefs.values())
            + "\n"
            + "\n".join(self._binding_codes.values())
        )
        return code

    def get_code(self):
        """Implement this to compose the total (but still templated)
        shader. This method is called by ``generate_wgsl()``.
        """
        return self.get_definitions()

    def generate_wgsl(self, **kwargs):
        """Generate the final WGSL. Calls get_code() and then resolves
        the templating variables, varyings, and depth output.
        """

        old_kwargs = self.kwargs
        self.kwargs = old_kwargs.copy()
        self.kwargs.update(kwargs)

        try:

            code1 = self.get_code()
            t = jinja_env.from_string(code1)

            err_msg = None
            try:
                code2 = t.render(**self.kwargs)
            except jinja2.UndefinedError as err:
                err_msg = f"Canot compose shader: {err.args[0]}"

            if err_msg:
                # Don't raise within handler to avoid recursive tb
                raise ValueError(err_msg)
            else:
                code2 = resolve_varyings(code2)
                code2 = resolve_depth_output(code2)
                return code2

        finally:
            self.kwargs = old_kwargs

    def define_bindings(self, bindgroup, bindings_dict):
        """Define a collection of bindings organized in a dict."""
        for index, binding in bindings_dict.items():
            self.define_binding(bindgroup, index, binding)

    def define_binding(self, bindgroup, index, binding):
        """Define a uniform, buffer, sampler, or texture. The produced wgsl
        will be part of the code returned by ``get_definitions()``. The binding
        must be a Binding object.
        """
        if binding.type == "buffer/uniform":
            self._define_uniform(bindgroup, index, binding)
        elif binding.type.startswith("buffer"):
            self._define_buffer(bindgroup, index, binding)
        elif binding.type.startswith("sampler"):
            self._define_sampler(bindgroup, index, binding)
        elif binding.type.startswith("texture"):
            self._define_texture(bindgroup, index, binding)
        else:
            raise RuntimeError(
                f"Unknown binding {binding.name} with type {binding.type}"
            )

    def _define_uniform(self, bindgroup, index, binding):

        structname = "Struct_" + binding.name
        code = f"""
        struct {structname} {{
        """.rstrip()

        resource = binding.resource
        if isinstance(resource, dict):
            dtype_struct = array_from_shadertype(resource).dtype
        elif isinstance(resource, Buffer):
            if resource.data.dtype.fields is None:
                raise TypeError(f"define_uniform() needs a structured dtype")
            dtype_struct = resource.data.dtype
        elif isinstance(resource, np.dtype):
            if resource.fields is None:
                raise TypeError(f"define_uniform() needs a structured dtype")
            dtype_struct = resource
        else:
            raise TypeError(f"Unsupported struct type {resource.__class__.__name__}")

        # Obtain names of fields that are arrays. This is encoded as an empty field with a
        # name that has the array-fields-names separated with double underscores.
        array_names = []
        for fieldname in dtype_struct.fields.keys():
            if fieldname.startswith("__") and fieldname.endswith("__"):
                array_names.extend(fieldname.replace("__", " ").split())

        # Process fields
        for fieldname, (dtype, offset) in dtype_struct.fields.items():
            if fieldname.startswith("__"):
                continue
            # Resolve primitive type
            primitive_type = dtype.base.name
            primitive_type = primitive_type.replace("float", "f")
            primitive_type = primitive_type.replace("uint", "u")
            primitive_type = primitive_type.replace("int", "i")
            # Resolve actual type (only scalar, vec, mat)
            shape = dtype.shape
            # Detect array
            length = -1
            if fieldname in array_names:
                length = shape[0]
                shape = shape[1:]
            # Obtain base type
            if shape == () or shape == (1,):
                # A scalar
                wgsl_type = align_type = primitive_type
            elif len(shape) == 1:
                # A vector
                n = shape[0]
                if n < 2 or n > 4:
                    raise TypeError(f"Type {dtype} looks like an unsupported vec{n}.")
                wgsl_type = align_type = f"vec{n}<{primitive_type}>"
            elif len(shape) == 2:
                # A matNxM is Matrix of N columns and M rows
                n, m = shape[1], shape[0]
                if n < 2 or n > 4 or m < 2 or m > 4:
                    raise TypeError(
                        f"Type {dtype} looks like an unsupported mat{n}x{m}."
                    )
                align_type = f"vec{m}<primitive_type>"
                wgsl_type = f"mat{n}x{m}<{primitive_type}>"
            else:
                raise TypeError(f"Unsupported type {dtype}")
            # If an array, wrap it
            if length == 0:
                wgsl_type = align_type = None  # zero-length; dont use
            elif length > 0:
                wgsl_type = f"array<{wgsl_type},{length}>"
            else:
                pass  # not an array

            # Check alignment (https://www.w3.org/TR/WGSL/#alignment-and-size)
            if not wgsl_type:
                continue
            elif align_type == primitive_type:
                alignment = 4
            elif align_type.startswith("vec"):
                c = int(align_type.split("<")[0][-1])
                alignment = 8 if c < 3 else 16
            else:
                raise TypeError(f"Cannot establish alignment of wgsl type: {wgsl_type}")
            if offset % alignment != 0:
                # If this happens, our array_from_shadertype() has failed.
                raise TypeError(
                    f"Struct alignment error: {binding.name}.{fieldname} alignment must be {alignment}"
                )

            code += f"\n            {fieldname}: {wgsl_type},"

        code += "\n        };"
        self._typedefs[structname] = code

        code = f"""
        @group({bindgroup}) @binding({index})
        var<uniform> {binding.name}: {structname};
        """.rstrip()
        self._binding_codes[binding.name] = code

    def _define_buffer(self, bindgroup, index, binding):

        # Get format, and split in the scalar part and the number of channels
        fmt = to_vertex_format(binding.resource.format)
        if "x" in fmt:
            fmt_scalar, _, nchannels = fmt.partition("x")
            nchannels = int(nchannels)
        else:
            fmt_scalar = fmt
            nchannels = 1

        # Define scalar type: i32, u32 or f32
        # Since the stride must be a multiple of 4 for storage buffers,
        # the supported types is limited until we support structured numpy arrays.
        scalar_type = (
            fmt_scalar.replace("float", "f").replace("uint", "u").replace("sint", "i")
        )
        if not scalar_type.endswith("32"):
            raise ValueError(
                f"Buffer format {format} not supported, format must have a stride of 4 bytes: i4, u4 of f4."
            )

        # Define the element types. The element_type2 is the actual type.
        # Because for storage buffers a vec3 has an alignment of 16, we have to
        # be creative for vec3: we bind the buffer as if it was 1D, and convert
        # in the accessor function.
        if nchannels == 1:
            element_type1 = element_type2 = scalar_type
            stride = 4
        elif nchannels == 3:
            element_type1 = scalar_type
            element_type2 = f"vec{nchannels}<{scalar_type}>"
            stride = 4
        else:
            element_type1 = element_type2 = f"vec{nchannels}<{scalar_type}>"
            stride = 4 * nchannels

        stride  # not actually used anymore in wgsl?

        # Produce the binding code and accessor function
        type_modifier = "read" if "read_only" in binding.type else "read_write"
        code = f"""
        @group({bindgroup}) @binding({index})
        var<storage, {type_modifier}> {binding.name}: array<{element_type1}>;
        fn load_{binding.name} (i: i32) -> {element_type2} {{
        """.rstrip()
        if element_type1 == element_type2:
            code += f" return {binding.name}[i];"
        elif nchannels == 2:
            code += f" return {element_type2}( {binding.name}[i * 2], {binding.name}[i * 2 + 1] );"
        elif nchannels == 3:
            code += f" return {element_type2}( {binding.name}[i * 3], {binding.name}[i * 3 + 1], {binding.name}[i * 3 + 2] );"
        else:  # nchannels == 4
            code += f" return {element_type2}( {binding.name}[i * 4], {binding.name}[i * 4 + 1], {binding.name}[i * 4 + 2], {binding.name}[i * 4 + 3] );"
        code += " }"
        self._binding_codes[binding.name] = code

    def _define_sampler(self, bindgroup, index, binding):
        code = f"""
        @group({bindgroup}) @binding({index})
        var {binding.name}: sampler;
        """.rstrip()
        self._binding_codes[binding.name] = code

    def _define_texture(self, bindgroup, index, binding):
        texture = binding.resource  # or view
        format = to_texture_format(texture.format)
        if "norm" in format or "float" in format:
            format = "f32"
        elif "uint" in format:
            format = "u32"
        else:
            format = "i32"
        code = f"""
        @group({bindgroup}) @binding({index})
        var {binding.name}: texture_{texture.view_dim}<{format}>;
        """.rstrip()
        self._binding_codes[binding.name] = code
