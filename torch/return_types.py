import inspect

import torch
from torch.utils._pytree import register_pytree_node, SequenceKey

__all__ = ["pytree_register_structseq"]

# error: Module has no attribute "_return_types"
return_types = torch._C._return_types  # type: ignore[attr-defined]


def pytree_register_structseq(cls):
    if not torch.utils._pytree.is_structseq_class(cls):
        raise TypeError(f"Class {cls!r} is not a PyStructSequence class.")

    def structseq_flatten(structseq):
        return list(structseq), type(structseq)

    def structseq_flatten_with_keys(structseq):
        values, context = structseq_flatten(structseq)
        return [(SequenceKey(i), v) for i, v in enumerate(values)], context

    def structseq_unflatten(values, context):
        return context(values)

    register_pytree_node(
        cls,
        structseq_flatten,
        structseq_unflatten,
        flatten_with_keys_fn=structseq_flatten_with_keys,
    )


for name in dir(return_types):
    if name.startswith("__"):
        continue

    _attr = getattr(return_types, name)
    globals()[name] = _attr

    if not name.startswith("_"):
        __all__.append(name)

    # Today everything in torch.return_types is a structseq, aka a "namedtuple"-like
    # thing defined by the Python C-API. We're going to need to modify this when that
    # is no longer the case.
    # NB: I don't know how to check that something is a "structseq" so we do a fuzzy
    # check for tuple
    if inspect.isclass(_attr) and issubclass(_attr, tuple):
        pytree_register_structseq(_attr)
