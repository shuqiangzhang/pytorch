# Owner(s): ["module: inductor"]

import sys
import unittest
import weakref
from contextlib import ExitStack

from copy import deepcopy
from typing import NamedTuple

import torch

import torch._inductor
import torch._inductor.cudagraph_trees
from torch._inductor import config

from torch.optim import (
    Adadelta,
    Adagrad,
    Adam,
    Adamax,
    AdamW,
    ASGD,
    LBFGS,
    NAdam,
    RAdam,
    RMSprop,
    Rprop,
    SGD,
    SparseAdam,
)
from torch.testing._internal.common_device_type import instantiate_device_type_tests

from torch.testing._internal.common_optimizers import (
    _get_optim_inputs_including_global_cliquey_kwargs,
    optim_db,
    optims,
)

from torch.testing._internal.common_utils import TestCase

from torch.testing._internal.inductor_utils import HAS_CPU, HAS_CUDA

from torch.testing._internal.triton_utils import requires_cuda


class KernelCounts(NamedTuple):
    multitensor: int
    singletensor: int


# With different settings for certain
# tests you can get different kernel counts
# This maps the test name to the
# expected kernel count
KERNEL_COUNT_OVERRIDES = {
    "test_rmsprop_foreach_weight_decay_cpu": 12,
    "test_nadam_foreach_weight_decay_momentum_decay_cpu": 20,
    "test_adamw_amsgrad_capturable_foreach_cuda": 3,
    "test_adamw_amsgrad_capturable_cuda": 6,
    "test_adam_amsgrad_capturable_cuda": 6,
    "test_adadelta_foreach_weight_decay_maximize_cpu": 12,
    "test_adadelta_foreach_rho_weight_decay_cpu": 12,
    "test_adadelta_foreach_weight_decay_cpu": 12,
    "test_sgd_foreach_momentum_weight_decay_cpu": 16,
    "test_sgd_foreach_momentum_nesterov_weight_decay_cpu": 16,
    "test_sgd_momentum_dampening_foreach_cuda": 5,
    "test_sgd_momentum_foreach_cuda": 5,
}

# also tracks currently supported optimizers
KERNEL_COUNTS = {
    Adam: KernelCounts(multitensor=2, singletensor=8),
    AdamW: KernelCounts(multitensor=2, singletensor=8),
    NAdam: KernelCounts(multitensor=2, singletensor=12),
    Rprop: KernelCounts(multitensor=1, singletensor=4),
    RMSprop: KernelCounts(multitensor=1, singletensor=4),
    Adadelta: KernelCounts(multitensor=1, singletensor=4),
    Adagrad: KernelCounts(multitensor=5, singletensor=8),
    ASGD: KernelCounts(multitensor=2, singletensor=12),
    SGD: KernelCounts(multitensor=2, singletensor=8),
    RAdam: KernelCounts(
        multitensor=2, singletensor=None
    ),  # Single tensor eager needs to be refactored to enable tracing (#118230)
    Adamax: KernelCounts(
        multitensor=2, singletensor=None
    ),  # Single tensor eager needs to be refactored to enable tracing (#117836)
}


def build_opt_kwarg_db():
    compiled_opt_db = []
    for optim_info in optim_db:
        if optim_info.optim_cls not in KERNEL_COUNTS:
            continue

        for device in ["cpu", "cuda"]:
            for optim_inputs in _get_optim_inputs_including_global_cliquey_kwargs(
                device, None, optim_info, skip=("differentiable",)
            ):
                kwargs = dict(optim_inputs.kwargs)
                name = f"test_{optim_info.optim_cls.__name__.lower()}"

                for key, val in kwargs.items():
                    if not key == "lr" and (
                        not isinstance(val, bool) or (isinstance(val, bool) and val)
                    ):
                        name += "_" + key

                name += f"_{device}"

                # Eager for-loop impl doesn't support capturable ASGD
                if name in [
                    "test_asgd_capturable_cuda",
                    "test_asgd_maximize_capturable_cuda",
                    "test_asgd_weight_decay_capturable_cuda",
                    "test_asgd_weight_decay_maximize_capturable_cuda",
                ]:
                    continue

                kwargs["device"] = device
                if name in KERNEL_COUNT_OVERRIDES:
                    kwargs["kernel_count"] = KERNEL_COUNT_OVERRIDES[name]
                else:
                    kwargs["kernel_count"] = (
                        KERNEL_COUNTS[optim_info.optim_cls].multitensor
                        if kwargs.get("foreach", False) and device == "cuda"
                        else KERNEL_COUNTS[optim_info.optim_cls].singletensor
                    )

                if kwargs["kernel_count"] is None:
                    continue

                # fused optimizers are disabled
                if kwargs.get("fused", False):
                    kwargs["kernel_count"] = 0

                compiled_opt_db.append((optim_info.optim_cls, name, kwargs))

    return compiled_opt_db


COMPILED_OPT_KWARG_DB = build_opt_kwarg_db()

aten = torch.ops.aten


try:
    try:
        from .test_torchinductor import check_model, check_model_cuda
    except ImportError:
        from test_torchinductor import check_model, check_model_cuda
except (unittest.SkipTest, ImportError) as e:
    sys.stderr.write(f"{type(e)}: {e}\n")
    if __name__ == "__main__":
        sys.exit(0)
    raise


def compile_opt(opt_compiled, closure=None, fullgraph=True):
    # run the patcher so that step has the expected structure
    torch._dynamo.eval_frame.TorchPatcher.patch()

    # unwrap step TWICE to avoid a deliberate graph break due to
    # a limitation of functionalization/no_grad detection
    # see the [Note on graph break] in optimizer.py
    # This ignores the outer _use_grad_if_differentiable wrapper
    # and instead manually disables grad before calling step, which is fine
    # for now as dynamo does not support differentiable optimizers anyway
    step_fn = opt_compiled.step.__wrapped__.__wrapped__
    if closure is not None:

        def fn():
            step_fn(opt_compiled, closure)

    else:

        def fn():
            step_fn(opt_compiled)

    return torch.compile(fn, backend="inductor", fullgraph=fullgraph)


def check_optim(
    self,
    optim_cls,
    params_eager,
    params_compiled,
    state_eager,
    state_compiled,
    atol=None,
    rtol=None,
):
    params_eager = list(params_eager)
    params_compiled = list(params_compiled)
    # Note on tolerances:
    # test_correctness_Adadelta_cuda_float32
    # Mismatched elements: 10 / 100 (10.0%)
    # Greatest absolute difference: 4.838220775127411e-05 at index (7, 4) (up to 1e-05 allowed)
    # Greatest relative difference: 0.007270356640219688 at index (7, 2) (up to 1e-05 allowed)
    # This is due to floating point ordering error + usage of sqrt
    rtol = None
    atol = None
    if optim_cls is Adadelta:
        rtol = 5.5e-4
        atol = 5e-5

    self.assertEqual(list(params_eager), list(params_compiled), atol=atol, rtol=rtol)

    # currently we don't mutate step properly until we resolve
    # https://github.com/pytorch/pytorch/issues/115679
    if optim_cls not in (Rprop, RMSprop, Adadelta):
        for p_eager, p_compiled in zip(params_eager, params_compiled):
            self.assertEqual(
                state_eager[p_eager],
                state_compiled[p_compiled],
                atol=atol,
                rtol=rtol,
            )


def make_test(
    optim_cls,
    closure=None,
    kernel_count=2,
    device="cuda",
    atol=None,
    rtol=None,
    **kwargs,
):
    def test_fn(self):
        stack = ExitStack()
        try:
            # we fallback to eager on the fused implementation
            is_fused = kwargs.get("fused", False)

            # https://github.com/pytorch/pytorch/issues/118715 for capturable Adagrad support
            # https://github.com/pytorch/pytorch/issues/118018 for capturable SGD support
            run_cudagraphs = (
                device == "cuda" and not is_fused and optim_cls not in (Adagrad, SGD)
            )
            if run_cudagraphs:
                stack.enter_context(config.patch({"triton.cudagraphs": True}))

            if isinstance(kwargs.get("lr", None), torch.Tensor):
                kwargs["lr"] = kwargs["lr"].to(device)

            torch._dynamo.reset()
            torch._inductor.metrics.reset()
            input = torch.ones([10, 10], device=device)
            model_eager = torch.nn.Sequential(
                *[torch.nn.Linear(10, 10, device=device) for _ in range(2)]
            )
            model_eager(input).sum().backward()

            input = torch.ones([10, 10], device=device)
            model_compiled = deepcopy(model_eager)
            model_compiled(input).sum().backward()

            opt_eager = optim_cls(model_eager.parameters(), **kwargs)
            opt_compiled = optim_cls(model_compiled.parameters(), **kwargs)
            compiled_step = compile_opt(
                opt_compiled, closure=closure, fullgraph=(not is_fused)
            )

            with torch.set_grad_enabled(False):
                compiled_step()
                compiled_step()
                opt_eager.step()
                opt_eager.step()

            check_optim(
                self,
                optim_cls,
                model_eager.parameters(),
                model_compiled.parameters(),
                opt_eager.state,
                opt_compiled.state,
            )

            if run_cudagraphs:
                self.check_cudagraphs_ran()

            if self.check_kernel_count:
                # currently, we compile the step and the rest of the computation
                # separately because the step is a single element tensor
                # hence, the usual kernel count is 2
                self.assertEqual(
                    torch._inductor.metrics.generated_kernel_count, kernel_count
                )
        finally:
            stack.close()

    if device == "cuda":
        test_fn = requires_cuda(test_fn)

    return test_fn


def make_recompile_test(optim_cls, closure=None, kernel_count=2, **kwargs):
    @requires_cuda
    def test_fn(self):
        torch._dynamo.reset()
        torch._inductor.metrics.reset()
        input = torch.ones([10, 10], device="cuda")
        model = torch.nn.Sequential(
            *[torch.nn.Linear(10, 10, device="cuda") for _ in range(2)]
        )
        model(input).sum().backward()

        opt_compiled = optim_cls(model.parameters(), **kwargs)
        compiled_step = compile_opt(opt_compiled)

        # check no recompile here
        with torch.set_grad_enabled(False):
            for _ in range(4):
                compiled_step()

            # perturb state to force recompile
            # Adagrad doesn't reinitialize state on each step
            if optim_cls is Adagrad:
                opt_compiled.param_groups[0]["lr"] = 0.02
            else:
                opt_compiled.state.clear()

            compiled_step()

        if self.check_kernel_count:
            if optim_cls is SGD:
                # SGD triggers an additional recompile
                # because of momentum buffer list mutation in step()
                multiplier = 3
            else:
                # currently, we compile the step and the rest of the computation
                # separately because the step is a single element tensor
                # hence, the usual kernel count is 2
                # multiply by 2 to account for the recompile
                multiplier = 2

            self.assertEqual(
                torch._inductor.metrics.generated_kernel_count,
                multiplier * kernel_count,
            )

    return test_fn


class CompiledOptimizerParityTests(TestCase):
    @optims(optim_db, dtypes=[torch.float32])
    def test_correctness(self, device, dtype, optim_info):
        optim_cls = optim_info.optim_cls
        all_optim_inputs = _get_optim_inputs_including_global_cliquey_kwargs(
            device, dtype, optim_info, skip=("differentiable",)
        )
        for optim_input in all_optim_inputs:
            kwargs = dict(optim_input.kwargs)

            # RAdam #117836 and Adamax #118230 and ASGD #116052
            # Single tensor eager needs to be refactored to enable tracing
            if optim_info.only_supports_capturable_on_foreach and not kwargs.get(
                "foreach", False
            ):
                kwargs["foreach"] = True

            torch._dynamo.reset()
            torch._inductor.metrics.reset()
            input = torch.ones([10, 10], device=device)
            model_eager = torch.nn.Sequential(
                *[torch.nn.Linear(10, 10, device=device) for _ in range(2)]
            )
            model_eager(input).sum().backward()
            model_compiled = deepcopy(model_eager)
            model_compiled(input).sum().backward()

            if optim_cls is SparseAdam:
                for param in model_eager.parameters():
                    param.grad = param.grad.to_sparse()
                for param in model_compiled.parameters():
                    param.grad = param.grad.to_sparse()

            opt_compiled = optim_cls(model_compiled.parameters(), **kwargs)
            opt_eager = optim_cls(model_eager.parameters(), **kwargs)

            if optim_cls is LBFGS:

                @torch.compile()
                def fn():
                    def closure():
                        loss = model_compiled(input).sum()
                        loss.backward()
                        return loss

                    opt_compiled.step(closure)

                def closure_eager():
                    loss = model_eager(input).sum()
                    loss.backward()
                    return loss

                opt_eager.step(closure_eager)
                opt_eager.step(closure_eager)
            else:

                @torch.compile()
                def fn():
                    opt_compiled.step()

                opt_eager.step()
                opt_eager.step()

            fn()
            fn()

            check_optim(
                self,
                optim_cls,
                model_eager.parameters(),
                model_compiled.parameters(),
                opt_eager.state,
                opt_compiled.state,
            )


class CompiledOptimizerTests(TestCase):
    check_model_cuda = check_model_cuda
    check_model_cpu = check_model
    check_kernel_count = True

    def setUp(self):
        super().setUp()
        torch._dynamo.reset()
        torch._inductor.metrics.reset()

    def tearDown(self):
        super().tearDown()
        torch._dynamo.reset()
        torch._inductor.metrics.reset()

    def check_cudagraphs_ran(self):
        # We run the zeroth device currently
        manager = torch._inductor.cudagraph_trees.get_container(0).tree_manager
        self.assertIsNotNone(manager)
        self.assertEqual(manager.new_graph_id().id, 1)

    test_adam_recompile = make_recompile_test(Adam, lr=0.01)
    test_adamw_recompile = make_recompile_test(AdamW, lr=0.01)
    test_adamax_recompile = make_recompile_test(Adamax, lr=0.01)
    test_nadam_recompile = make_recompile_test(NAdam, lr=0.01)
    test_rprop_recompile = make_recompile_test(Rprop, kernel_count=1, lr=0.01)
    test_rmsprop_recompile = make_recompile_test(RMSprop, kernel_count=1, lr=0.01)
    test_adadelta_recompile = make_recompile_test(Adadelta, kernel_count=1, lr=0.01)
    test_adagrad_recompile = make_recompile_test(Adagrad, kernel_count=5, lr=0.01)
    test_asgd_recompile_default = make_recompile_test(ASGD, kernel_count=2, lr=0.01)
    test_asgd_recompile_single = make_recompile_test(
        ASGD, kernel_count=12, lr=0.01, foreach=False
    )
    test_asgd_recompile_foreach = make_recompile_test(
        ASGD, kernel_count=2, lr=0.01, foreach=True
    )
    test_sgd_recompile_single = make_recompile_test(
        SGD, kernel_count=4, lr=0.01, foreach=False
    )
    test_sgd_recompile_foreach = make_recompile_test(
        SGD, kernel_count=1, lr=0.01, foreach=True
    )

    @requires_cuda
    def test_static_address_finalizer(self):
        import gc

        gc.disable()
        p_ref = None

        def fn():
            nonlocal p_ref
            mod = torch.nn.Linear(10, 10, device="cuda:0", bias=False)
            for p in mod.parameters():
                p.grad = torch.rand_like(p)

            opt = torch.optim.Adam(mod.parameters(), lr=0.1)

            def fn():
                opt.step()

            with torch.set_grad_enabled(False):
                step_fn_compiled = torch.compile(fn)
                step_fn_compiled()
            p_ref = weakref.ref(p)
            self.assertTrue(p_ref() is not None)

        fn()

        self.assertTrue(p_ref() is None)
        gc.enable()


for optim_cls, name, kwargs in COMPILED_OPT_KWARG_DB:
    setattr(CompiledOptimizerTests, name, make_test(optim_cls, **kwargs))

instantiate_device_type_tests(CompiledOptimizerParityTests, globals())

if __name__ == "__main__":
    from torch._dynamo.test_case import run_tests

    if HAS_CPU or HAS_CUDA:
        run_tests(needs="filelock")
