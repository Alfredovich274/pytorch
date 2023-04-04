from __future__ import annotations

import warnings

from typing import Callable, Dict, List, Sequence, Tuple, Union

import numpy as np

import onnxscript  # type: ignore[import]
from onnxscript import evaluator  # type: ignore[import]

import torch
import torch.fx

from torch.onnx import _constants, _type_utils
from torch.onnx._internal import _beartype, onnx_proto_utils
from torch.onnx._internal.fx import diagnostics
from torch.onnx._internal.fx.passes import fx_to_onnxscript
from torch.utils import _pytree


@_beartype.beartype
def validate_op_between_ort_torch(
    node: torch.fx.Node,
    symbolic_fn: Union[onnxscript.OnnxFunction, Callable],
    torch_args: tuple,
    torch_kwargs: dict,
):
    """Validate the op between ONNX Runtime and PyTorch."""
    # op-level validation
    # Symbolic_fn should have the same output as node.target (torch ops)
    # trace_only function is regular python function
    function_name = (
        symbolic_fn.name
        if isinstance(symbolic_fn, onnxscript.OnnxFunction)
        else symbolic_fn.__name__
    )

    with evaluator.default_as(evaluator.ort_evaluator):
        try:
            expected_outputs = node.target(*torch_args, **torch_kwargs)  # type: ignore[operator]
        except IndexError as index_error:
            # TODO(titaiwang): How to bound indices/dim: INT64
            warnings.warn(
                f"\nBypass the test of running on PyTorch Op {node.target} with "
                f"IndexError: \n{index_error}.\n This is possibly raised by "
                f"unsupported input args of randomnized dim/indices(INT64).\n"
            )
            diagnostic = diagnostics.export_context().inflight_diagnostic()
            diagnostic.with_additional_message(
                f"### Op level debug is bypassed\n"
                f"{diagnostics.decorator.format_exception_in_markdown(index_error)}"
            )
            diagnostic.level = diagnostics.levels.WARNING
            return
        except RuntimeError as runtime_error:
            warnings.warn(
                f"\nFail the test of running on PyTorch Op {node.target} with "
                f"RuntimeError: \n{runtime_error}.\n"
            )
            diagnostic = diagnostics.export_context().inflight_diagnostic()
            diagnostic.with_additional_message(
                f"### Op level debug fails on PyTorch\n"
                f"{diagnostics.decorator.format_exception_in_markdown(runtime_error)}"
            )
            diagnostic.level = diagnostics.levels.ERROR
            return

        # TODO(titaiwang): Need Opschema from ONNX function to better split args/kwargs
        # Currently, we only support torch.Tensor to numpy array. Potentially, we
        # could fail on INT64. However, we don't support dims/indices INT64 validation.
        input_onnx = [
            onnx_proto_utils._convert_tensor_to_numpy(x)
            if isinstance(x, (torch.Tensor, torch.dtype, list, tuple))
            else x
            for x in torch_args
        ]
        kwargs_onnx = fx_to_onnxscript.filter_incompatible_and_dtype_convert_kwargs(
            torch_kwargs
        )
        try:
            ort_outputs = symbolic_fn(*input_onnx, **kwargs_onnx)
        except ValueError as value_error:
            # FIXME(titaiwang): This is caused by wronly split args/kwargs.
            # When Opschema is ready, we should follow Opschema to split args/kwargs.
            warnings.warn(
                f"\nBypass the test of running on ONNX Op {function_name} with "
                f"ValueError: \n{value_error}.\n This is possibly raised by "
                f"unsupported input args due to lack of Opschema.\n"
            )
            diagnostic = diagnostics.export_context().inflight_diagnostic()
            diagnostic.with_additional_message(
                f"### Op level debug is bypassed\n"
                f"{diagnostics.decorator.format_exception_in_markdown(value_error)}"
            )
            diagnostic.level = diagnostics.levels.WARNING
            return
        except RuntimeError as runtime_error:
            warnings.warn(
                f"\nFail the test of running on ONNX Op {function_name} with "
                f"RuntimeError: \n{runtime_error}.\n"
            )
            diagnostic = diagnostics.export_context().inflight_diagnostic()
            diagnostic.with_additional_message(
                f"### Op level debug fails on ONNXRUNTIME:\n"
                f"{diagnostics.decorator.format_exception_in_markdown(runtime_error)}"
            )
            diagnostic.level = diagnostics.levels.ERROR
            return

        flattened_torch_outputs, _ = _pytree.tree_flatten(expected_outputs)
        flattened_function_outputs, _ = _pytree.tree_flatten(ort_outputs)

        assert flattened_torch_outputs
        assert len(flattened_torch_outputs) == len(flattened_function_outputs)

        for torch_output, function_output in zip(
            flattened_torch_outputs, flattened_function_outputs
        ):
            try:
                if not isinstance(function_output, np.ndarray):
                    # An onnxscript tensor
                    function_output = function_output.value

                # Use torch.testing as opposed to np.testing to ensure dtypes and shapes match
                torch.testing.assert_close(
                    torch.tensor(function_output).cpu(),
                    torch_output.cpu()
                    if isinstance(torch_output, torch.Tensor)
                    else torch.tensor(torch_output).cpu(),
                    rtol=1e-4,
                    atol=1e-3,
                )
            except AssertionError as e:
                warnings.warn(
                    f"\nSuppressed AssertionError:\n{e}.\n"
                    f"Op {node.target} has mismatch outputs. "
                    f"Please check the implementation of {function_name}.\n"
                )
                diagnostic = diagnostics.export_context().inflight_diagnostic()
                diagnostic.with_additional_message(
                    f"### Validation failed\n"
                    f"{diagnostics.decorator.format_exception_in_markdown(e)}"
                )
                diagnostic.level = diagnostics.levels.ERROR


@_beartype.beartype
def generate_random_tensors(shape: torch.Size, dtype: torch.dtype):
    if dtype == torch.uint8:
        return torch.randint(
            low=_constants.UINT8_MIN, high=_constants.UINT8_MAX, size=shape, dtype=dtype
        )
    if dtype == torch.int8:
        return torch.randint(
            low=_constants.INT8_MIN, high=_constants.INT8_MAX, size=shape, dtype=dtype
        )
    if dtype == torch.int16:
        return torch.randint(
            low=_constants.INT16_MIN, high=_constants.INT16_MAX, size=shape, dtype=dtype
        )
    if dtype == torch.int32:
        return torch.randint(
            low=_constants.INT32_MIN, high=_constants.INT32_MAX, size=shape, dtype=dtype
        )
    if dtype == torch.int64:
        return torch.randint(
            low=_constants.INT64_MIN, high=_constants.INT64_MAX, size=shape, dtype=dtype
        )
    if dtype == torch.bool:
        random_numbers = torch.rand(shape)
        return torch.where(
            random_numbers > 0.5, torch.tensor(True), torch.tensor(False)
        )
    return torch.randn(shape, dtype=dtype)


@_beartype.beartype
def _recursive_wrap_args(
    complete_args: List[_type_utils.Argument],
) -> List[_type_utils.Argument]:
    """Wrap args in torch.fx.Node to FakeTensor"""
    wrapped_args: List[_type_utils.Argument] = []
    for arg in complete_args:
        if isinstance(arg, torch.fx.Node):
            fake_tensor = arg.meta["val"]
            if isinstance(fake_tensor, torch.Tensor):
                real_tensor = generate_random_tensors(
                    fake_tensor.shape, fake_tensor.dtype
                )
                wrapped_args.append(real_tensor)
            elif isinstance(fake_tensor, (int, float)):
                # TODO(titaiwang): Could dtype be inside a fx.Node?
                wrapped_args.append(fake_tensor)
            else:
                warnings.warn(
                    f"Unexpected argument type found inside fx.Node. arg: {arg}; "
                    f"arg.meta['val']: {fake_tensor}; type(arg.meta['val']): "
                    f"{type(fake_tensor)}. This might lead to an error when running on Ops."
                )
        elif isinstance(arg, Sequence):
            wrapped_args.append(_recursive_wrap_args(arg))
        elif isinstance(arg, (int, float, torch.dtype)):
            wrapped_args.append(arg)
        else:
            warnings.warn(
                f"Unexpected argument type found. arg: {arg}; type(arg): {type(arg)}. "
                "This might lead to an error when running on Ops"
            )

    return wrapped_args


@_beartype.beartype
def wrap_fx_args_as_torch_args(
    complete_args: List[_type_utils.Argument],
    complete_kwargs: Dict[str, _type_utils.Argument],
) -> Tuple[tuple, dict]:
    """Prepare torch format args and kwargs for op-level validation by using fake tensor to create real tensor to feed in ops"""

    # NOTE: This function only supports FakeTensor with concrete shapes
    torch_args: List[_type_utils.Argument] = _recursive_wrap_args(complete_args)
    torch_kwargs = complete_kwargs
    return (tuple(torch_args), torch_kwargs)
