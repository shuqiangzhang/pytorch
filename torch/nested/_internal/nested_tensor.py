from typing import Tuple

import torch
from torch._C import DispatchKey, DispatchKeySet
from torch._prims_common import is_expandable_to
from torch.utils.weak import WeakTensorKeyDictionary
from typing import *  # noqa: F403
import weakref

def _get_nested_int(equiv_set, vec):
    return torch._C._get_nested_int(equiv_set, coeff=1, vec=vec)

# Only to be used in NestedTensorState
# union find needs to exist in both python and cpp.
class UnionFind:
    _vecs = WeakTensorKeyDictionary() # vec -> vec used for union find

    # Union find in python
    def merge(self, src, tgt):
        # grab the canonical vec for vec1, and the canonical vec for vec2
        # what do we do if they are not in the set?
        if src not in self._vecs:
            self._vecs[src] = src
        if tgt not in self._vecs:
            self._vecs[tgt] = tgt
        # Arbitrarily choose vec1's canonical as the canonical for both
        self._vecs[self._vecs[src]] = self._vecs[self._vecs[tgt]]

    def get_canonical_vec(self, vec):
        if vec not in self._vecs:
            self._vecs[vec] = vec
            return vec
        orig = vec
        prev = vec
        curr = self._vecs[vec]
        while prev is not curr:
            prev = curr
            curr = self._vecs[curr]
        self._vecs[orig] = curr
        return curr

class DefaultWeakTensorKeyDictionary():
    # If getitem is called on a key that is not in the dictionary, the dictionary
    # will create a new entry for the key and return the default value.
    def __init__(self, default_cls):
        self._data = WeakTensorKeyDictionary()
        self._default_cls = default_cls

    def __getitem__(self, key):
        if key not in self._data:
            self._data[key] = self._default_cls()
        return self._data[key]

    def __setitem__(self, key, value):
        self._data[key] = value

    def items(self):
        return self._data.items()

# Only to be used in NestedTensorState
class TensorCounter:
    # This class is NOT equiv set aware, simply assigns a unique id to each tensor
    # object. This is all the state you need for nested int creation.
    _incrementing_id = 0
    # TODO: id to vec would belong here, but not sure why we need it?
    _global_nested_int_ids = WeakTensorKeyDictionary()

    def get_id(self, vec):
        if vec not in self._global_nested_int_ids:
            self._global_nested_int_ids[vec] = self._incrementing_id
            self._incrementing_id += 1
        return self._global_nested_int_ids[vec]

    def contains_vec(self, vec):
        return vec in self._global_nested_int_ids

class NestedTensorState:
    # Class that encapsulates all the state needed for NestedTensor
    def __init__(self):
        self._tensor_counter = TensorCounter()
        # The union find data structure in python ensures that the canonical vec
        # stays alive as long as any vec in its equiv set is alive.
        self._union_find = UnionFind()
        # the following data structures only allow canonical vec as keys
        # once a merge happens, one of the keys becomes invalid,
        # the value of the non-canonical key is invalidated
        self._INVALID = object()
        self._metadata = DefaultWeakTensorKeyDictionary(dict)
        self._weak_tensors = DefaultWeakTensorKeyDictionary(set)

    def merge(self, src, tgt):
        print("merge", id(src), id(tgt))
        if src is tgt:
            return
        # merge union find in python and cpp (TODO: can we avoid having both?)
        canonical_src = self._union_find.get_canonical_vec(src)
        canonical_tgt = self._union_find.get_canonical_vec(tgt)

        self._union_find.merge(src, tgt)
        src_id = self._tensor_counter.get_id(src)
        tgt_id = self._tensor_counter.get_id(tgt)
        torch._C._get_nested_int_union_find().merge(src_id, tgt_id)

        self._metadata[canonical_src].update(self._metadata[canonical_tgt])
        self._metadata[canonical_tgt] = self._metadata[canonical_src]
        print("marking as invalid", id(canonical_src))
        self._metadata[canonical_src] = self._INVALID

        self._weak_tensors[canonical_tgt].update(self._weak_tensors[canonical_src])
        self._weak_tensors[canonical_src] = self._INVALID

    def get_metadata(self, vec):
        print("get_metadata", id(vec))
        canonical_vec = self._union_find.get_canonical_vec(vec)
        print("canonical_vec", id(canonical_vec))
        ret = self._metadata[canonical_vec]
        # I am asking for the metadata a vec that is no longer canonical
        assert ret is not self._INVALID
        return ret

    def get_equivalent_vecs(self, vec):
        print("get_equivalent_vecs", id(vec))
        canonical_vec = self._union_find.get_canonical_vec(vec)
        # Returns all vecs that are alive in the same equiv set as vec
        # check that canonical vec is actually in the set?
        for weak_vec in self._weak_tensors[canonical_vec]:
            vec = weak_vec()
            if vec is not None:
                yield vec

    def create_nested_int(self, vec, ctor_fn=None):
        print("create_nested_int", id(vec))
        # the issue is that if I have a FakeTensor that doesn't necessarily imply
        # that I need symbolic nested int, it is fine if I use the same obj anyway ig
        if (isinstance(vec, torch._subclasses.fake_tensor.FakeTensor) or
            isinstance(vec, torch._subclasses.functional_tensor.FunctionalTensor)) and ctor_fn is None:
            print("fake but ctor is None")
            return vec.create_nested_int(_get_nested_int, use_cache=True)
        # Parameters:
        #     ctor_fn (Callable[[int, Tensor], SymInt]): If not None, use a custom
        #        constructor to create the nested int.
        _ctor_fn = ctor_fn if ctor_fn is not None else _get_nested_int
        return _ctor_fn(self._tensor_counter.get_id(vec), vec)

    def validate_invariants(self):
        # for testing only
        for vec, val in self._metadata.items():
            assert (self._union_find.get_canonical_vec(vec) is vec) == (val is self._INVALID)
        for vec, val in self._weak_tensors.items():
            assert (self._union_find.get_canonical_vec(vec) is vec) == (val is self._INVALID)

    def print_metadata(self):
        for vec, val in self._metadata.items():
            print(f"vec: {id(vec)}, val: {val}")


_nt_state: Optional[NestedTensorState] = None

def get_nt_state() -> NestedTensorState:
    global _nt_state
    if _nt_state is None:
        _nt_state = NestedTensorState()
    return _nt_state


def trust_me_assert_equal(vec1, vec2, _nt_state=None):
    nt_state = get_nt_state() if _nt_state is None else _nt_state
    nt_state.merge(vec1, vec2)


# SDPA metadata; max / min seqlens are needed for e.g. flash
def _get_sdpa_extreme_seqlen(func, tensor):
    return int(func(tensor).item())


class NestedTensor(torch.Tensor):
    _values: torch.Tensor  # type: ignore[assignment]
    _offsets: torch.Tensor
    _lengths: Optional[torch.Tensor]
    # NOTE [ Nested ints for ragged sizes and strides ]
    #
    # Jagged layout tensors are tensors that represent a n-dim tensor with a
    # ragged dimension, but are backed by an (n-1)-dim tensor underneath, e.g.,
    # a jagged tensor with outer shape [B, x, D] is represented internally by a
    # tensor with shape [sum(x), D] where we introduce what we call a nested int
    # denoted as "x" here (but sometimes denoted with "*" to
    # represent the ragged dimension, and sum(x) represents the dim of the inner
    # tensor or equivalently the sum of all the sizes of the constituent
    # tensors' varying lengths.
    #
    # We also use nested ints to represent the strides of this tensor.
    # For example, a jagged tensor with shape [B, x, D] can be strided in two
    # ways: [xD, D, 1] and [x, 1, sum(x)], where xD represents x multiplied by D
    _size: Tuple[int, ...]
    _stride: Tuple[int, ...]
    # Indicates that the nth dimension is ragged
    _ragged_idx: int
    _metadata_cache: Dict[str, Any]

    @staticmethod
    def __new__(
        cls,
        values,
        offsets,
        *,
        lengths=None,
        nested_int=None,
        **kwargs,
    ):
        ks = DispatchKeySet(DispatchKey.NestedTensor)
        ks = ks.add(DispatchKey.AutogradNestedTensor)
        r = torch.Tensor._make_wrapper_subclass(  # type: ignore[attr-defined]
            cls,
            (0,),
            (0,),
            0,
            torch.contiguous_format,
            values.dtype,
            torch.jagged,
            values.device,
            False,
            kwargs.get("requires_grad", False),
            "sizes",
            False,
            True,  # dispatch_layout
            ks,
        )
        return r

    def __init__(self, values, offsets, *, lengths=None, **kwargs):
        super().__init__()
        # Only support jagged for now.
        assert offsets is not None
        assert offsets.ndim == 1
        assert not isinstance(values, NestedTensor)

        # Query cache for the symint associated with offsets or lengths
        # (create a new one if needed).
        ragged_source = offsets if lengths is None else lengths

        nt_state = get_nt_state()
        ragged_size = nt_state.create_nested_int(ragged_source)
        # print(ragged_size, ragged_source)
        nt_state.print_metadata()
        # nt_state.validate_invariants()
        metadata = nt_state.get_metadata(ragged_source)
        metadata["sum_vec"] = values.shape[0]

        self._ragged_idx = kwargs.get("_ragged_idx", 1)
        B = offsets.shape[0] - 1
        Ds = values.shape[: self._ragged_idx - 1] + values.shape[self._ragged_idx :]

        nested_size = [B]
        nested_size.extend(Ds[: self._ragged_idx - 1])
        nested_size.append(ragged_size)
        nested_size.extend(Ds[self._ragged_idx - 1 :])
        self._size = tuple(nested_size)

        stride = values.stride()
        self._strides = (ragged_size * stride[self._ragged_idx - 1], *stride)

        if values.requires_grad:
            raise ValueError(
                "NestedTensor values cannot require grad, please "
                "detach before passing to NestedTensor constructor"
            )
        self._values = values
        self._offsets = offsets
        self._lengths = lengths

        # holds properties that are computed lazily
        self._metadata_cache = kwargs.get("_metadata_cache") or {}

        # collapsed ragged dim must always be dynamic
        torch._dynamo.mark_dynamic(self, self._ragged_idx)
        torch._dynamo.mark_dynamic(self._values, self._ragged_idx - 1)

    def values(self):
        return self._values

    def offsets(self):
        return self._offsets

    def lengths(self):
        return self._lengths

    @property
    def _max_seqlen(self):
        if "max_seqlen" not in self._metadata_cache:
            # compute & cache
            self._metadata_cache["max_seqlen"] = _get_sdpa_extreme_seqlen(
                torch.max,
                self._offsets.diff() if self._lengths is None else self._lengths,
            )
        return self._metadata_cache["max_seqlen"]

    @property
    def _min_seqlen(self):
        if "min_seqlen" not in self._metadata_cache:
            # compute & cache
            self._metadata_cache["min_seqlen"] = _get_sdpa_extreme_seqlen(
                torch.min,
                self._offsets.diff() if self._lengths is None else self._lengths,
            )
        return self._metadata_cache["min_seqlen"]

    def __repr__(self):
        # We should implement this in torch/_tensor_str.py instead
        grad_fn_str = (
            f", requires_grad={self.requires_grad}" if self.requires_grad else ""
        )
        if self.grad_fn:
            grad_fn_str = f", grad_fn={self.grad_fn}"
        return f"NestedTensor(size={self._size}, offsets={self._offsets}{grad_fn_str}, contiguous={self._lengths is None})"

    def __reduce_ex__(self, proto):
        state = torch._utils._get_obj_state(self)

        # SymNodes are not serializable
        assert "_size" in state and "_strides" in state
        state = dict(state)
        del state["_size"]
        del state["_strides"]

        func = NestedTensor
        args = (self._values, self._offsets)
        return (torch._tensor._rebuild_from_type_v2, (func, type(self), args, state))

    def __tensor_flatten__(self):
        ctx = {
            "requires_grad": self.requires_grad,
            # TODO: Don't guard on this!
            "metadata_cache": self._metadata_cache,
            "ragged_idx": self._ragged_idx,
        }
        inner_tensors = ["_values", "_offsets"]
        if self._lengths is not None:
            inner_tensors.append("_lengths")
        return inner_tensors, ctx

    @staticmethod
    def __tensor_unflatten__(inner_tensors: Dict, meta, outer_size, outer_stride):
        assert len(inner_tensors) >= 2 and len(inner_tensors) <= 3
        values = inner_tensors["_values"]
        offsets = inner_tensors["_offsets"]
        lengths = inner_tensors.get("_lengths", None)
        ragged_idx = meta["ragged_idx"]

        vec = offsets if lengths is None else lengths
        nested_int = None

        # Note [Nested ints handling in __tensor_unflatten__]
        #
        # First, read "When you have tracing subclass tensors as vec".
        #
        # __tensor_unflatten__ is generally responsible for creating a new
        # instance of the subclass given (1) some metadata (2) the inner tensors.
        # and ordinarily, you would be able to use those inputs as-is to
        # construct the new instance.
        #
        # This is not possible in the case of NT, however, because the NT's
        # metadata is associated with one of the inner tensors. In particular,
        # for every NT, its nested int is associated with some offsets or
        # lengths (WLOG, let's say offsets from now on.) with the invariant that
        # the offsets on the NT and the NT's nested int must be in the same
        # equiv set. Naively using the metadata/inner tensors as-is would
        # violate the invariant for example in the case when we are in
        # AOTAutograd's runtime wrapper, constructing a new NT using traced
        # metadata and real dense outputs.
        #
        # What you kind of want to do is to use offsets as the source of truth
        # and rederive the nested int, and this is easy to do in the case
        # where we have already seen and registered that offsets before, as it
        # is already associated with a nested int. The harder case is when
        # you don't actually know what the equiv set of offsets is. Unlike
        # ordinary subclasses, NT's __tensor_unflatten__ has a second
        # responsibility, which is to register the new vec via maybe_create if
        # it is not already registered. This is because the caller of
        # maybe_create is responsible for telling the registry what the equiv
        # set of the new vec is, i.e., (1) either our offset is in the same
        # equiv set as the vec associated with the metadata, or it is not.
        #
        # In this function we decide between the two by making the following
        # assumption:
        #
        #   If the new offsets is the same type of tensor as the offsets
        #   associated with the metadata, then we assume that they belong to the
        #   same equiv set.
        #
        # Today it seems that this assumption holds for the below known cases:
        # (TODO: expand on this part)
        # - functional -> fake
        # - runtime wrapper
        # - fakification
        # - grad_output aliasing
        nt_state = get_nt_state()

        if (isinstance(vec, torch._subclasses.fake_tensor.FakeTensor) or
                isinstance(vec, torch._subclasses.functional_tensor.FunctionalTensor)):
            old_nested_int = outer_size[ragged_idx]

            def ctor_fn(i, v):
                # TODO: functional tensor unwraps?
                def creation_fn():
                    return torch.SymInt(
                        # TODO: update clone to no longer take id
                        old_nested_int.node.clone_nested_int_with_new_vec(i, v)
                    )
                ret = v.create_nested_int(
                    creation_fn,
                    use_cache=old_nested_int.node._hint.node.nested_int_coeff() == 1,
                )
                print("ctor_fn return: ", ret, v)
                return ret

            old_vec = old_nested_int.node.nested_int_vec()
            nt_state.create_nested_int(vec, ctor_fn=ctor_fn)

            if type(vec) == type(old_vec):
                trust_me_assert_equal(vec, old_vec)

        return NestedTensor(
            values,
            offsets=offsets,
            lengths=lengths,
            requires_grad=meta["requires_grad"],
            _ragged_idx=ragged_idx,
            _metadata_cache=meta["metadata_cache"],
        )

    @classmethod
    def __torch_dispatch__(cls, func, types, args=(), kwargs=None):
        kwargs = {} if kwargs is None else kwargs

        # Lazy import to avoid circular dependency
        from .ops import lookup_jagged

        fn = lookup_jagged(func, *args, **kwargs)
        if fn is not None:
            return fn(*args, **kwargs)

        raise NotImplementedError(func)

    @classmethod
    def __torch_function__(cls, func, types, args=(), kwargs=None):
        if kwargs is None:
            kwargs = {}

        from .ops import jagged_torch_function

        try:
            return jagged_torch_function(func, *args, **kwargs)
        except NotImplementedError:
            pass
        with torch._C.DisableTorchFunctionSubclass():
            return func(*args, **kwargs)


# Not actually a view!
class ViewBufferFromNested(torch.autograd.Function):
    @staticmethod
    def forward(ctx, x: NestedTensor):  # type: ignore[override]
        ctx.save_for_backward(x.offsets())
        ctx.metadata_cache = x._metadata_cache
        ctx.ragged_idx = x._ragged_idx
        return x.values()

    @staticmethod
    def backward(ctx, gO: torch.Tensor):  # type: ignore[override]
        (offsets,) = ctx.saved_tensors
        return NestedTensor(
            gO,
            offsets=offsets,
            _metadata_cache=ctx.metadata_cache,
            _ragged_idx=ctx.ragged_idx,
        )


# Not actually a view!
class ViewNestedFromBuffer(torch.autograd.Function):
    @staticmethod
    def forward(
        ctx,
        values: torch.Tensor,
        offsets: torch.Tensor,
        metadata_cache: Optional[Dict[str, Any]] = None,
    ):  # type: ignore[override]
        return NestedTensor(
            values.detach(),
            offsets=offsets,
            _metadata_cache=metadata_cache,
        )

    @staticmethod
    def backward(ctx, gO: NestedTensor):  # type: ignore[override]
        return gO.values(), None, None


# Not actually a view!
# NOTE: @jbschlosser is working on making it a view
class ViewNonContiguousNestedFromBuffer(torch.autograd.Function):
    @staticmethod
    def forward(ctx, values: torch.Tensor, offsets: torch.Tensor, lengths: torch.Tensor):  # type: ignore[override]
        return NestedTensor(
            values.detach(),
            offsets=offsets,
            lengths=lengths,
        )

    @staticmethod
    def backward(ctx, gO: NestedTensor):  # type: ignore[override]
        return gO.values(), None, None


# Need to make it obvious that users should be passing in offsets
def jagged_from_list(
    tensors: List[torch.Tensor],
    offsets: Optional[torch.Tensor],
    dtype=None,
    device=None,
) -> Tuple[NestedTensor, torch.Tensor]:
    """Constructs a NestedTensor backed by jagged layout from a list of tensors"""

    if not len(set(t.dtype for t in tensors)) == 1:  # noqa: C401
        raise RuntimeError(
            "When constructing a nested tensor, all tensors in list must have the same dtype"
        )
    if not len(set(t.device for t in tensors)) == 1:  # noqa: C401
        raise RuntimeError(
            "When constructing a nested tensor, all tensors in list must be on the same device"
        )

    # Check that the NT is representable by the jagged layout.
    # Jagged layout represents (B, *, D_0, D_1, ..., D_N), where the only
    # raggedness allowed is for the single dim immediately adjacent to the batch dim.
    sizes = [t.shape for t in tensors]
    non_first_sizes = [s[1:] for s in sizes]
    at_most_first_ragged = all(s == non_first_sizes[0] for s in non_first_sizes)
    if not at_most_first_ragged:
        raise RuntimeError(
            "Cannot represent given tensor list as a nested tensor with the jagged layout. "
            "Note that the jagged layout only represents shapes of the form "
            "(B, *, D_0, D_1, ..., D_N), with only * allowed to be ragged."
        )

    # Set properties appropriately.
    values = torch.cat(tensors, dim=0)
    to_kwargs = {}
    if device is not None:
        to_kwargs["device"] = device
    if dtype is not None:
        to_kwargs["dtype"] = dtype
    values = values.to(**to_kwargs)

    # Calculate jagged offsets if not provided.
    if offsets is None:
        # Jagged layout specifies that offsets are stored as int64 on the same device as values.
        offsets = torch.cat(
            [
                torch.zeros(1, dtype=torch.int64, device=values.device),
                torch.tensor([s[0] for s in sizes], device=values.device).cumsum(dim=0),
            ]
        )

    ret_nt = ViewNestedFromBuffer.apply(values, offsets)
    ret_nt._metadata_cache = {
        # compute this now since it's easy
        "max_seqlen": max([t.shape[0] for t in tensors]),
        "min_seqlen": min([t.shape[0] for t in tensors]),
    }
    return (ret_nt, offsets)  # type: ignore[return-value]


def jagged_from_tensor_and_lengths(
    tensor: torch.Tensor, starts: torch.Tensor, lengths: torch.Tensor
) -> Tuple[NestedTensor, torch.Tensor, Optional[torch.Tensor]]:
    """Constructs a NestedTensor backed by jagged layout from a tensor, starts of sequences, and sequence lengths"""
    batch_size = tensor.shape[0]
    if is_expandable_to(starts.shape, (batch_size,)) and is_expandable_to(
        lengths.shape, (batch_size,)
    ):
        start_list = starts.expand(batch_size)
        length_list = lengths.expand(batch_size)
    else:
        raise RuntimeError(
            "When constructing a jagged nested tensor using narrow(), "
            "your start and length must be Tensors that broadcast to input.shape[0]"
        )

    # Calculate jagged offsets
    assert (
        len(tensor.shape) >= 2
    ), "tensor must at least be 2D for the nested narrow op to work"
    max_seq_len = tensor.shape[1]
    offset_lengths = max_seq_len * torch.arange(
        0, batch_size, dtype=torch.int64, device=tensor.device
    )
    # Jagged layout specifies that offsets are stored as int64 on the same device as values.
    offsets = torch.cat(
        [
            start_list + offset_lengths,
            (start_list[-1] + offset_lengths[-1] + length_list[-1]).unsqueeze(0),
        ]
    )

    # Reshape buffer to flatten the 1st and 2nd dimension (view used to enforce non-copy)
    if len(tensor.shape) > 2:
        values = tensor.view(-1, *tensor.shape[2:])
    else:
        values = tensor.view(-1)

    # Check if offsets and lengths make it possibly contiguous and return a regular NT
    is_contiguous = True
    orig_dim = tensor.shape[1]
    if torch.any(length_list[1:-1].ne(orig_dim)):
        is_contiguous = False
    if torch.any(offsets[1:-2].diff().ne(orig_dim)):
        is_contiguous = False
    if offsets[0] + length_list[0] != orig_dim:
        is_contiguous = False

    actual_max_seqlen = int(torch.max(lengths).item())
    min_seqlen = int(torch.min(lengths).item())

    if is_contiguous:
        ret_nt = ViewNestedFromBuffer.apply(
            values[offsets[0] : offsets[-1]],
            offsets - offsets[0],
        )
    else:
        ret_nt = ViewNonContiguousNestedFromBuffer.apply(values, offsets, length_list)

    # populate metadata cache with computed seqlen extremes
    ret_nt._metadata_cache = {
        "max_seqlen": actual_max_seqlen,
        "min_seqlen": min_seqlen,
    }

    return (ret_nt, offsets, None if is_contiguous else length_list)


def buffer_from_jagged(jagged):
    return ViewBufferFromNested.apply(jagged)
