# Copyright 2023 The TensorFlow Authors. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ==============================================================================

from tensorflow.compiler.tests import xla_test
from tensorflow.python.eager.polymorphic_function import compiler_ir
from tensorflow.python.eager.polymorphic_function import polymorphic_function
from tensorflow.python.framework import constant_op
from tensorflow.python.framework import dtypes
from tensorflow.python.framework import ops
from tensorflow.python.framework import tensor_spec
from tensorflow.python.framework import test_util
from tensorflow.python.ops import array_ops
from tensorflow.python.ops import gen_array_ops
from tensorflow.python.ops import math_ops
from tensorflow.python.ops import variables
from tensorflow.python.platform import test
from tensorflow.python.util import nest


class CompilerIrTest(xla_test.XLATestCase):

  def _compareTwoMethodsCompilerIROutput(self, f, args, kwargs):
    flat_args = list(args) + list(kwargs.values())
    if not all([isinstance(x, ops.Tensor) for x in flat_args]):
      self.skipTest('It only support args and kwargs are all tf.Tensor types.')

    args_spec = nest.map_structure(tensor_spec.TensorSpec.from_tensor, args)
    kwargs_spec = nest.map_structure(tensor_spec.TensorSpec.from_tensor, kwargs)

    hlo_1 = f.experimental_get_compiler_ir(*args, **kwargs)()
    hlo_2 = f.experimental_get_compiler_ir(*args_spec, **kwargs_spec)()

    if hlo_1 != hlo_2:
      self.fail(
          'The tensor_spec way experimental_get_compiler_ir give diff result to'
          f' normal experimental_get_compiler_ir. \nhlo_1:\n{hlo_1}'
          f'\nhlo_2:\n{hlo_2}\n'
      )

  def test_zero_input(self):
    with ops.device('device:{}:0'.format(self.device)):

      @polymorphic_function.function(jit_compile=True, autograph=False)
      def fun_tf():
        return array_ops.zeros((10), dtype=dtypes.int32)

      self._compareTwoMethodsCompilerIROutput(fun_tf, [], {})

  def test_constant_slice(self):
    with ops.device('device:{}:0'.format(self.device)):
      # Constant slice. This is the common case.
      x = array_ops.zeros((10,), dtype=dtypes.int32)

      @polymorphic_function.function(jit_compile=True, autograph=False)
      def fun_tf(x):
        begin = 0
        return x[begin:5]

      self._compareTwoMethodsCompilerIROutput(fun_tf, [x], {})

  def test_compile_time_constant(self):
    with ops.device('device:{}:0'.format(self.device)):
      # Non-constant slice, but compile-time constant depending only on shapes.
      x = array_ops.zeros((10,), dtype=dtypes.int32)

      @polymorphic_function.function(jit_compile=True, autograph=False)
      def fun_tf(x):
        # begin is a compile-time constant, even if x is not
        begin = array_ops.shape_v2(x)[0] - 2
        return x[begin:]

      self._compareTwoMethodsCompilerIROutput(fun_tf, [x], {})

  def test_capture_constant(self):
    with ops.device('device:{}:0'.format(self.device)):
      # Capture a constant
      outer_ct = [3.0]
      x = ops.convert_to_tensor([2.0, 3.0, 4.0], dtype=dtypes.float32)

      @polymorphic_function.function(jit_compile=True, autograph=False)
      def fun_tf(x):
        return x * gen_array_ops.broadcast_to(outer_ct, x.shape) + 1.0

      self._compareTwoMethodsCompilerIROutput(fun_tf, [x], {})

  def test_unsupported_dynamic_input(self):
    with ops.device('device:{}:0'.format(self.device)):

      @polymorphic_function.function(jit_compile=True)
      def f(x):
        return x

      with self.assertRaisesRegex(
          ValueError, 'Only support static input shape but got'
      ):
        args_spec = [tensor_spec.TensorSpec((None), dtype=dtypes.float32)]
        concrete_fn = f.get_concrete_function(*args_spec)
        _ = compiler_ir.from_concrete_function(concrete_fn)(stage='hlo')

  def test_unsupported_shape_depend_input(self):
    with ops.device('device:{}:0'.format(self.device)):
      # Those cases output shapes are dynamic.
      @polymorphic_function.function(jit_compile=True)
      def f2(x):
        return x[x[0] : 0]

      args = [ops.convert_to_tensor([1, 2, 3, 4])]
      args_spec = nest.map_structure(tensor_spec.TensorSpec.from_tensor, args)
      concrete_fn = f2.get_concrete_function(*args_spec)
      if test_util.is_mlir_bridge_enabled():
        with self.assertRaisesRegex(
            ValueError, 'TF to XLA legalization failed'
        ):
          _ = compiler_ir.from_concrete_function(concrete_fn)(stage='hlo')
      else:
        _ = compiler_ir.from_concrete_function(concrete_fn)(stage='hlo')

      # Those cases both input and output shapes are static but tf graph
      # contains constant args inside.
      @polymorphic_function.function(jit_compile=True)
      def f3(a, b):
        c = array_ops.slice(a, [b], [-1])
        return math_ops.reduce_sum(c)

      args = [ops.convert_to_tensor([2, 3]), ops.convert_to_tensor(1)]
      args_spec = nest.map_structure(tensor_spec.TensorSpec.from_tensor, args)
      concrete_fn = f3.get_concrete_function(*args_spec)
      if test_util.is_mlir_bridge_enabled():
        with self.assertRaisesRegex(
            ValueError, 'TF to XLA legalization failed'
        ):
          _ = compiler_ir.from_concrete_function(concrete_fn)(stage='hlo')
      else:
        _ = compiler_ir.from_concrete_function(concrete_fn)(stage='hlo')

  def test_unsupported_capture_outside_variable(self):
    """Those cases define tf.Variable outside function body."""
    with ops.device('device:{}:0'.format(self.device)):
      error_msg = (
          'The function to be lowered uses some tf.Variable defined outside_'
          '_the_function body.'
      )

      v = variables.Variable([0.1, 0.1])

      @polymorphic_function.function(jit_compile=True)
      def f4(a, b):
        return (a + b) * v

      a = constant_op.constant([1.1, 1.1])
      b = constant_op.constant([2.2, 2.2])

      kwargs = {'b': a, 'a': b}
      with self.assertRaisesRegex(ValueError, error_msg):
        kwargs_spec = nest.map_structure(
            tensor_spec.TensorSpec.from_tensor, kwargs
        )
        concrete_fn = f4.get_concrete_function(**kwargs_spec)
        _ = compiler_ir.from_concrete_function(concrete_fn)(stage='hlo')


if __name__ == '__main__':
  ops.enable_eager_execution()
  test.main()
