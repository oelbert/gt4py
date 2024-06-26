# GT4Py - GridTools Framework
#
# Copyright (c) 2014-2023, ETH Zurich
# All rights reserved.
#
# This file is part of the GT4Py project and the GridTools framework.
# GT4Py is free software: you can redistribute it and/or modify it under
# the terms of the GNU General Public License as published by the
# Free Software Foundation, either version 3 of the License, or any later
# version. See the LICENSE.txt file at the top-level directory of this
# distribution for a copy of the license or check <https://www.gnu.org/licenses/>.
#
# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

import dataclasses
import functools
from collections.abc import Callable, Sequence
from types import ModuleType
from typing import ClassVar, Iterable

import numpy as np
from numpy import typing as npt

from gt4py._core import definitions as core_defs
from gt4py.eve.extended_typing import Never, Optional, ParamSpec, TypeAlias, TypeVar
from gt4py.next import common
from gt4py.next.embedded import (
    common as embedded_common,
    context as embedded_context,
    exceptions as embedded_exceptions,
)
from gt4py.next.ffront import experimental, fbuiltins
from gt4py.next.iterator import embedded as itir_embedded


try:
    import cupy as cp
except ImportError:
    cp: Optional[ModuleType] = None  # type:ignore[no-redef]

try:
    from jax import numpy as jnp
except ImportError:
    jnp: Optional[ModuleType] = None  # type:ignore[no-redef]


def _get_nd_array_class(*fields: common.Field | core_defs.Scalar) -> type[NdArrayField]:
    for f in fields:
        if isinstance(f, NdArrayField):
            return f.__class__
    raise AssertionError("No 'NdArrayField' found in the arguments.")


def _make_builtin(
    builtin_name: str, array_builtin_name: str, reverse: bool = False
) -> Callable[..., NdArrayField]:
    def _builtin_op(*fields: common.Field | core_defs.Scalar) -> NdArrayField:
        cls_ = _get_nd_array_class(*fields)
        xp = cls_.array_ns
        op = getattr(xp, array_builtin_name)

        domain_intersection = embedded_common.domain_intersection(
            *[f.domain for f in fields if isinstance(f, common.Field)]
        )

        transformed: list[core_defs.NDArrayObject | core_defs.Scalar] = []
        for f in fields:
            if isinstance(f, common.Field):
                if f.domain == domain_intersection:
                    transformed.append(xp.asarray(f.ndarray))
                else:
                    f_broadcasted = _broadcast(f, domain_intersection.dims)
                    f_slices = _get_slices_from_domain_slice(
                        f_broadcasted.domain, domain_intersection
                    )
                    transformed.append(xp.asarray(f_broadcasted.ndarray[f_slices]))
            else:
                assert core_defs.is_scalar_type(f)
                transformed.append(f)
        if reverse:
            transformed.reverse()
        new_data = op(*transformed)
        return cls_.from_array(new_data, domain=domain_intersection)

    _builtin_op.__name__ = builtin_name
    return _builtin_op


_Value: TypeAlias = common.Field | core_defs.ScalarT
_P = ParamSpec("_P")
_R = TypeVar("_R", _Value, tuple[_Value, ...])


@dataclasses.dataclass(frozen=True)
class NdArrayField(
    common.MutableField[common.DimsT, core_defs.ScalarT], common.FieldBuiltinFuncRegistry
):
    """
    Shared field implementation for NumPy-like fields.

    Builtin function implementations are registered in a dictionary.
    Note: Currently, all concrete NdArray-implementations share
    the same implementation, dispatching is handled inside of the registered
    function via its namespace.
    """

    _domain: common.Domain
    _ndarray: core_defs.NDArrayObject

    array_ns: ClassVar[ModuleType]  # TODO(havogt) introduce a NDArrayNamespace protocol

    @property
    def domain(self) -> common.Domain:
        return self._domain

    @property
    def shape(self) -> tuple[int, ...]:
        return self._ndarray.shape

    @property
    def __gt_origin__(self) -> tuple[int, ...]:
        assert common.Domain.is_finite(self._domain)
        return tuple(-r.start for r in self._domain.ranges)

    @property
    def ndarray(self) -> core_defs.NDArrayObject:
        return self._ndarray

    def asnumpy(self) -> np.ndarray:
        if self.array_ns == cp:
            return cp.asnumpy(self._ndarray)
        else:
            return np.asarray(self._ndarray)

    def as_scalar(self) -> core_defs.ScalarT:
        if self.domain.ndim != 0:
            raise ValueError(
                f"'as_scalar' is only valid on 0-dimensional 'Field's, got a {self.domain.ndim}-dimensional 'Field'."
            )
        return self.ndarray.item()

    @property
    def codomain(self) -> type[core_defs.ScalarT]:
        return self.dtype.scalar_type

    @property
    def dtype(self) -> core_defs.DType[core_defs.ScalarT]:
        return core_defs.dtype(self._ndarray.dtype.type)

    @classmethod
    def from_array(
        cls,
        data: (
            npt.ArrayLike | core_defs.NDArrayObject
        ),  # TODO: NDArrayObject should be part of ArrayLike
        /,
        *,
        domain: common.DomainLike,
        dtype: Optional[core_defs.DTypeLike] = None,
    ) -> NdArrayField:
        domain = common.domain(domain)
        xp = cls.array_ns

        xp_dtype = None if dtype is None else xp.dtype(core_defs.dtype(dtype).scalar_type)
        array = xp.asarray(data, dtype=xp_dtype)

        if dtype is not None:
            assert array.dtype.type == core_defs.dtype(dtype).scalar_type

        assert issubclass(array.dtype.type, core_defs.SCALAR_TYPES)

        assert all(isinstance(d, common.Dimension) for d in domain.dims), domain
        assert len(domain) == array.ndim
        assert all(s == 1 or len(r) == s for r, s in zip(domain.ranges, array.shape))

        return cls(domain, array)

    def remap(
        self: NdArrayField, connectivity: common.ConnectivityField | fbuiltins.FieldOffset
    ) -> NdArrayField:
        # For neighbor reductions, a FieldOffset is passed instead of an actual ConnectivityField
        if not isinstance(connectivity, common.ConnectivityField):
            assert isinstance(connectivity, fbuiltins.FieldOffset)
            connectivity = connectivity.as_connectivity_field()
        assert isinstance(connectivity, common.ConnectivityField)

        # Current implementation relies on skip_value == -1:
        # if we assume the indexed array has at least one element, we wrap around without out of bounds
        assert connectivity.skip_value is None or connectivity.skip_value == -1

        # Compute the new domain
        dim = connectivity.codomain
        dim_idx = self.domain.dim_index(dim)
        if dim_idx is None:
            raise ValueError(f"Incompatible index field, expected a field with dimension '{dim}'.")

        current_range: common.UnitRange = self.domain[dim_idx].unit_range
        new_ranges = connectivity.inverse_image(current_range)
        new_domain = self.domain.replace(dim_idx, *new_ranges)

        # perform contramap
        if not (connectivity.kind & common.ConnectivityKind.MODIFY_STRUCTURE):
            # shortcut for compact remap: don't change the array, only the domain
            new_buffer = self._ndarray
        else:
            # general case: first restrict the connectivity to the new domain
            restricted_connectivity_domain = common.Domain(*new_ranges)
            restricted_connectivity = (
                connectivity.restrict(restricted_connectivity_domain)
                if restricted_connectivity_domain != connectivity.domain
                else connectivity
            )
            assert isinstance(restricted_connectivity, common.ConnectivityField)

            # then compute the index array
            xp = self.array_ns
            new_idx_array = xp.asarray(restricted_connectivity.ndarray) - current_range.start
            # finally, take the new array
            new_buffer = xp.take(self._ndarray, new_idx_array, axis=dim_idx)

        return self.__class__.from_array(new_buffer, domain=new_domain, dtype=self.dtype)

    __call__ = remap  # type: ignore[assignment]

    def restrict(self, index: common.AnyIndexSpec) -> NdArrayField:
        new_domain, buffer_slice = self._slice(index)
        new_buffer = self.ndarray[buffer_slice]
        new_buffer = self.__class__.array_ns.asarray(new_buffer)
        return self.__class__.from_array(new_buffer, domain=new_domain)

    __getitem__ = restrict

    def __setitem__(
        self: NdArrayField[common.DimsT, core_defs.ScalarT],
        index: common.AnyIndexSpec,
        value: common.Field | core_defs.NDArrayObject | core_defs.ScalarT,
    ) -> None:
        target_domain, target_slice = self._slice(index)

        if isinstance(value, common.Field):
            if not value.domain == target_domain:
                raise ValueError(
                    f"Incompatible 'Domain' in assignment. Source domain = '{value.domain}', target domain = '{target_domain}'."
                )
            value = value.ndarray

        assert hasattr(self.ndarray, "__setitem__")
        self._ndarray[target_slice] = value  # type: ignore[index] # np and cp allow index assignment, jax overrides

    __abs__ = _make_builtin("abs", "abs")

    __neg__ = _make_builtin("neg", "negative")

    __add__ = __radd__ = _make_builtin("add", "add")

    __pos__ = _make_builtin("pos", "positive")

    __sub__ = _make_builtin("sub", "subtract")
    __rsub__ = _make_builtin("sub", "subtract", reverse=True)

    __mul__ = __rmul__ = _make_builtin("mul", "multiply")

    __truediv__ = _make_builtin("div", "divide")
    __rtruediv__ = _make_builtin("div", "divide", reverse=True)

    __floordiv__ = _make_builtin("floordiv", "floor_divide")
    __rfloordiv__ = _make_builtin("floordiv", "floor_divide", reverse=True)

    __pow__ = _make_builtin("pow", "power")

    __mod__ = _make_builtin("mod", "mod")
    __rmod__ = _make_builtin("mod", "mod", reverse=True)

    __ne__ = _make_builtin("not_equal", "not_equal")  # type: ignore # mypy wants return `bool`

    __eq__ = _make_builtin("equal", "equal")  # type: ignore # mypy wants return `bool`

    __gt__ = _make_builtin("greater", "greater")

    __ge__ = _make_builtin("greater_equal", "greater_equal")

    __lt__ = _make_builtin("less", "less")

    __le__ = _make_builtin("less_equal", "less_equal")

    def __and__(self, other: common.Field | core_defs.ScalarT) -> NdArrayField:
        if self.dtype == core_defs.BoolDType():
            return _make_builtin("logical_and", "logical_and")(self, other)
        raise NotImplementedError("'__and__' not implemented for non-'bool' fields.")

    __rand__ = __and__

    def __or__(self, other: common.Field | core_defs.ScalarT) -> NdArrayField:
        if self.dtype == core_defs.BoolDType():
            return _make_builtin("logical_or", "logical_or")(self, other)
        raise NotImplementedError("'__or__' not implemented for non-'bool' fields.")

    __ror__ = __or__

    def __xor__(self, other: common.Field | core_defs.ScalarT) -> NdArrayField:
        if self.dtype == core_defs.BoolDType():
            return _make_builtin("logical_xor", "logical_xor")(self, other)
        raise NotImplementedError("'__xor__' not implemented for non-'bool' fields.")

    __rxor__ = __xor__

    def __invert__(self) -> NdArrayField:
        if self.dtype == core_defs.BoolDType():
            return _make_builtin("invert", "invert")(self)
        raise NotImplementedError("'__invert__' not implemented for non-'bool' fields.")

    def _slice(
        self, index: common.AnyIndexSpec
    ) -> tuple[common.Domain, common.RelativeIndexSequence]:
        index = embedded_common.canonicalize_any_index_sequence(index)
        new_domain = embedded_common.sub_domain(self.domain, index)

        index_sequence = common.as_any_index_sequence(index)
        slice_ = (
            _get_slices_from_domain_slice(self.domain, index_sequence)
            if common.is_absolute_index_sequence(index_sequence)
            else index_sequence
        )
        assert common.is_relative_index_sequence(slice_)
        return new_domain, slice_


@dataclasses.dataclass(frozen=True)
class NdArrayConnectivityField(  # type: ignore[misc] # for __ne__, __eq__
    common.ConnectivityField[common.DimsT, common.DimT],
    NdArrayField[common.DimsT, core_defs.IntegralScalar],
):
    _codomain: common.DimT
    _skip_value: Optional[core_defs.IntegralScalar]

    @functools.cached_property
    def _cache(self) -> dict:
        return {}

    @classmethod
    def __gt_builtin_func__(cls, _: fbuiltins.BuiltInFunction) -> Never:  # type: ignore[override]
        raise NotImplementedError()

    @property
    # type: ignore[override] # TODO(havogt): instead of inheriting from NdArrayField, steal implementation or common base
    def codomain(self) -> common.DimT:
        return self._codomain

    @property
    def skip_value(self) -> Optional[core_defs.IntegralScalar]:
        return self._skip_value

    @functools.cached_property
    def kind(self) -> common.ConnectivityKind:
        kind = common.ConnectivityKind.MODIFY_STRUCTURE
        if self.domain.ndim > 1:
            kind |= common.ConnectivityKind.MODIFY_RANK
            kind |= common.ConnectivityKind.MODIFY_DIMS
        if self.domain.dim_index(self.codomain) is None:
            kind |= common.ConnectivityKind.MODIFY_DIMS

        return kind

    @classmethod
    def from_array(  # type: ignore[override]
        cls,
        data: npt.ArrayLike | core_defs.NDArrayObject,
        /,
        codomain: common.DimT,
        *,
        domain: common.DomainLike,
        dtype: Optional[core_defs.DTypeLike] = None,
        skip_value: Optional[core_defs.IntegralScalar] = None,
    ) -> NdArrayConnectivityField:
        domain = common.domain(domain)
        xp = cls.array_ns

        xp_dtype = None if dtype is None else xp.dtype(core_defs.dtype(dtype).scalar_type)
        array = xp.asarray(data, dtype=xp_dtype)

        if dtype is not None:
            assert array.dtype.type == core_defs.dtype(dtype).scalar_type

        assert issubclass(array.dtype.type, core_defs.INTEGRAL_TYPES)

        assert all(isinstance(d, common.Dimension) for d in domain.dims), domain
        assert len(domain) == array.ndim
        assert all(len(r) == s or s == 1 for r, s in zip(domain.ranges, array.shape))

        assert isinstance(codomain, common.Dimension)

        return cls(domain, array, codomain, _skip_value=skip_value)

    def inverse_image(
        self, image_range: common.UnitRange | common.NamedRange
    ) -> Sequence[common.NamedRange]:
        cache_key = hash((id(self.ndarray), self.domain, image_range))

        if (new_dims := self._cache.get(cache_key, None)) is None:
            xp = self.array_ns

            if not isinstance(
                image_range, common.UnitRange
            ):  # TODO(havogt): cleanup duplication with CartesianConnectivity
                if image_range.dim != self.codomain:
                    raise ValueError(
                        f"Dimension '{image_range.dim}' does not match the codomain dimension '{self.codomain}'."
                    )

                image_range = image_range.unit_range

            assert isinstance(image_range, common.UnitRange)

            assert common.UnitRange.is_finite(image_range)

            relative_ranges = _hypercube(self._ndarray, image_range, xp, self.skip_value)

            if relative_ranges is None:
                raise ValueError("Restriction generates non-contiguous dimensions.")

            new_dims = _relative_ranges_to_domain(relative_ranges, self.domain)

            self._cache[cache_key] = new_dims

        return new_dims

    def restrict(self, index: common.AnyIndexSpec) -> NdArrayConnectivityField:
        cache_key = (id(self.ndarray), self.domain, index)

        if (restricted_connectivity := self._cache.get(cache_key, None)) is None:
            cls = self.__class__
            xp = cls.array_ns
            new_domain, buffer_slice = self._slice(index)
            new_buffer = xp.asarray(self.ndarray[buffer_slice])
            restricted_connectivity = cls(new_domain, new_buffer, self.codomain, self.skip_value)
            self._cache[cache_key] = restricted_connectivity

        return restricted_connectivity

    __getitem__ = restrict


def _relative_ranges_to_domain(
    relative_ranges: Sequence[common.UnitRange], domain: common.Domain
) -> common.Domain:
    return common.Domain(
        dims=domain.dims, ranges=[rr + ar.start for ar, rr in zip(domain.ranges, relative_ranges)]
    )


def _hypercube(
    index_array: core_defs.NDArrayObject,
    image_range: common.UnitRange,
    xp: ModuleType,
    skip_value: Optional[core_defs.IntegralScalar] = None,
) -> Optional[list[common.UnitRange]]:
    """
    Return the hypercube that contains all indices in `index_array` that are within `image_range`, or `None` if no such hypercube exists.

    If `skip_value` is given, the selected values are ignored. It returns the smallest hypercube.
    A bigger hypercube could be constructed by adding lines that contain only `skip_value`s.
    Example:
    index_array =  0  1 -1
                   3  4 -1
                  -1 -1 -1
    skip_value = -1
    would currently select the 2x2 range [0,2], [0,2], but could also select the 3x3 range [0,3], [0,3].
    """
    select_mask = (index_array >= image_range.start) & (index_array < image_range.stop)

    nnz: tuple[core_defs.NDArrayObject, ...] = xp.nonzero(select_mask)

    slices = tuple(
        slice(xp.min(dim_nnz_indices), xp.max(dim_nnz_indices) + 1) for dim_nnz_indices in nnz
    )
    hcube = select_mask[tuple(slices)]
    if skip_value is not None:
        ignore_mask = index_array == skip_value
        hcube |= ignore_mask[tuple(slices)]
    if not xp.all(hcube):
        return None

    return [common.UnitRange(s.start, s.stop) for s in slices]


# -- Specialized implementations for builtin operations on array fields --

NdArrayField.register_builtin_func(
    fbuiltins.abs,  # type: ignore[attr-defined]
    NdArrayField.__abs__,
)
NdArrayField.register_builtin_func(
    fbuiltins.power,  # type: ignore[attr-defined]
    NdArrayField.__pow__,
)
# TODO gamma

for name in (
    fbuiltins.UNARY_MATH_FP_BUILTIN_NAMES
    + fbuiltins.UNARY_MATH_FP_PREDICATE_BUILTIN_NAMES
    + fbuiltins.UNARY_MATH_NUMBER_BUILTIN_NAMES
):
    if name in ["abs", "power", "gamma"]:
        continue
    NdArrayField.register_builtin_func(getattr(fbuiltins, name), _make_builtin(name, name))

NdArrayField.register_builtin_func(
    fbuiltins.minimum,  # type: ignore[attr-defined]
    _make_builtin("minimum", "minimum"),
)
NdArrayField.register_builtin_func(
    fbuiltins.maximum,  # type: ignore[attr-defined]
    _make_builtin("maximum", "maximum"),
)
NdArrayField.register_builtin_func(
    fbuiltins.fmod,  # type: ignore[attr-defined]
    _make_builtin("fmod", "fmod"),
)
NdArrayField.register_builtin_func(fbuiltins.where, _make_builtin("where", "where"))


def _compute_mask_ranges(mask: core_defs.NDArrayObject) -> list[tuple[bool, common.UnitRange]]:
    """Take a 1-dimensional mask and return a sequence of mappings from boolean values to ranges."""
    # TODO: does it make sense to upgrade this naive algorithm to numpy?
    assert mask.ndim == 1
    cur = bool(mask[0].item())
    ind = 0
    res = []
    for i in range(1, mask.shape[0]):
        if (
            mask_i := bool(mask[i].item())
        ) != cur:  # `.item()` to extract the scalar from a 0-d array in case of e.g. cupy
            res.append((cur, common.UnitRange(ind, i)))
            cur = mask_i
            ind = i
    res.append((cur, common.UnitRange(ind, mask.shape[0])))
    return res


def _trim_empty_domains(
    lst: Iterable[tuple[bool, common.Domain]],
) -> list[tuple[bool, common.Domain]]:
    """Remove empty domains from beginning and end of the list."""
    lst = list(lst)
    if not lst:
        return lst
    if lst[0][1].is_empty():
        return _trim_empty_domains(lst[1:])
    if lst[-1][1].is_empty():
        return _trim_empty_domains(lst[:-1])
    return lst


def _to_field(
    value: common.Field | core_defs.Scalar, nd_array_field_type: type[NdArrayField]
) -> common.Field:
    # TODO(havogt): this function is only to workaround broadcasting of scalars, once we have a ConstantField, we can broadcast to that directly
    return (
        value
        if isinstance(value, common.Field)
        else nd_array_field_type.from_array(
            nd_array_field_type.array_ns.asarray(value), domain=common.Domain()
        )
    )


def _intersect_fields(
    *fields: common.Field | core_defs.Scalar,
    ignore_dims: Optional[common.Dimension | tuple[common.Dimension, ...]] = None,
) -> tuple[common.Field, ...]:
    # TODO(havogt): this function could be moved to common, but then requires a broadcast implementation for all field implementations;
    # currently blocked, because requiring the `_to_field` function, see comment there.
    nd_array_class = _get_nd_array_class(*fields)
    promoted_dims = common.promote_dims(
        *(f.domain.dims for f in fields if isinstance(f, common.Field))
    )
    broadcasted_fields = [_broadcast(_to_field(f, nd_array_class), promoted_dims) for f in fields]

    intersected_domains = embedded_common.restrict_to_intersection(
        *[f.domain for f in broadcasted_fields], ignore_dims=ignore_dims
    )

    return tuple(
        nd_array_class.from_array(
            f.ndarray[_get_slices_from_domain_slice(f.domain, intersected_domain)],
            domain=intersected_domain,
        )
        for f, intersected_domain in zip(broadcasted_fields, intersected_domains, strict=True)
    )


def _stack_domains(*domains: common.Domain, dim: common.Dimension) -> Optional[common.Domain]:
    if not domains:
        return common.Domain()
    dim_start = domains[0][dim].unit_range.start
    dim_stop = dim_start
    for domain in domains:
        if not domain[dim].unit_range.start == dim_stop:
            return None
        else:
            dim_stop = domain[dim].unit_range.stop
    return domains[0].replace(dim, common.NamedRange(dim, common.UnitRange(dim_start, dim_stop)))


def _concat(*fields: common.Field, dim: common.Dimension) -> common.Field:
    # TODO(havogt): this function could be extended to a general concat
    # currently only concatenate along the given dimension and requires the fields to be ordered

    if (
        len(fields) > 1
        and not embedded_common.domain_intersection(*[f.domain for f in fields]).is_empty()
    ):
        raise ValueError("Fields to concatenate must not overlap.")
    new_domain = _stack_domains(*[f.domain for f in fields], dim=dim)
    if new_domain is None:
        raise embedded_exceptions.NonContiguousDomain(f"Cannot concatenate fields along {dim}.")
    nd_array_class = _get_nd_array_class(*fields)
    return nd_array_class.from_array(
        nd_array_class.array_ns.concatenate(
            [nd_array_class.array_ns.broadcast_to(f.ndarray, f.domain.shape) for f in fields],
            axis=new_domain.dim_index(dim),
        ),
        domain=new_domain,
    )


def _concat_where(
    mask_field: common.Field, true_field: common.Field, false_field: common.Field
) -> common.Field:
    cls_ = _get_nd_array_class(mask_field, true_field, false_field)
    xp = cls_.array_ns
    if mask_field.domain.ndim != 1:
        raise NotImplementedError(
            "'concat_where': Can only concatenate fields with a 1-dimensional mask."
        )
    mask_dim = mask_field.domain.dims[0]

    # intersect the field in dimensions orthogonal to the mask, then all slices in the mask field have same domain
    t_broadcasted, f_broadcasted = _intersect_fields(true_field, false_field, ignore_dims=mask_dim)

    # TODO(havogt): for clarity, most of it could be implemented on named_range in the masked dimension, but we currently lack the utils
    # compute the consecutive ranges (first relative, then domain) of true and false values
    mask_values_to_relative_range_mapping: Iterable[tuple[bool, common.UnitRange]] = (
        _compute_mask_ranges(mask_field.ndarray)
    )
    mask_values_to_domain_mapping: Iterable[tuple[bool, common.Domain]] = (
        (mask, _relative_ranges_to_domain((relative_range,), mask_field.domain))
        for mask, relative_range in mask_values_to_relative_range_mapping
    )
    # mask domains intersected with the respective fields
    mask_values_to_intersected_domains_mapping: Iterable[tuple[bool, common.Domain]] = (
        (
            mask_value,
            embedded_common.domain_intersection(
                t_broadcasted.domain if mask_value else f_broadcasted.domain, mask_domain
            ),
        )
        for mask_value, mask_domain in mask_values_to_domain_mapping
    )

    # remove the empty domains from the beginning and end
    mask_values_to_intersected_domains_mapping = _trim_empty_domains(
        mask_values_to_intersected_domains_mapping
    )
    if any(d.is_empty() for _, d in mask_values_to_intersected_domains_mapping):
        raise embedded_exceptions.NonContiguousDomain(
            f"In 'concat_where', cannot concatenate the following 'Domain's: {[d for _, d in mask_values_to_intersected_domains_mapping]}."
        )

    # slice the fields with the domain ranges
    transformed = [
        t_broadcasted[d] if v else f_broadcasted[d]
        for v, d in mask_values_to_intersected_domains_mapping
    ]

    # stack the fields together
    if transformed:
        return _concat(*transformed, dim=mask_dim)
    else:
        result_domain = common.Domain(common.NamedRange(mask_dim, common.UnitRange(0, 0)))
        result_array = xp.empty(result_domain.shape)
    return cls_.from_array(result_array, domain=result_domain)


NdArrayField.register_builtin_func(experimental.concat_where, _concat_where)  # type: ignore[has-type]


def _make_reduction(
    builtin_name: str, array_builtin_name: str, initial_value_op: Callable
) -> Callable[..., NdArrayField[common.DimsT, core_defs.ScalarT]]:
    def _builtin_op(
        field: NdArrayField[common.DimsT, core_defs.ScalarT], axis: common.Dimension
    ) -> NdArrayField[common.DimsT, core_defs.ScalarT]:
        xp = field.array_ns

        if not axis.kind == common.DimensionKind.LOCAL:
            raise ValueError("Can only reduce local dimensions.")
        if axis not in field.domain.dims:
            raise ValueError(f"Field can not be reduced as it doesn't have dimension '{axis}'.")
        if len([d for d in field.domain.dims if d.kind is common.DimensionKind.LOCAL]) > 1:
            raise NotImplementedError(
                "Reducing a field with more than one local dimension is not supported."
            )
        reduce_dim_index = field.domain.dims.index(axis)
        current_offset_provider = embedded_context.offset_provider.get(None)
        assert current_offset_provider is not None
        offset_definition = current_offset_provider[
            axis.value
        ]  # assumes offset and local dimension have same name
        assert isinstance(offset_definition, itir_embedded.NeighborTableOffsetProvider)
        new_domain = common.Domain(*[nr for nr in field.domain if nr.dim != axis])

        broadcast_slice = tuple(
            slice(None) if d in [axis, offset_definition.origin_axis] else xp.newaxis
            for d in field.domain.dims
        )
        masked_array = xp.where(
            xp.asarray(offset_definition.table[broadcast_slice]) != common._DEFAULT_SKIP_VALUE,
            field.ndarray,
            initial_value_op(field),
        )

        return field.__class__.from_array(
            getattr(xp, array_builtin_name)(masked_array, axis=reduce_dim_index), domain=new_domain
        )

    _builtin_op.__name__ = builtin_name
    return _builtin_op


NdArrayField.register_builtin_func(
    fbuiltins.neighbor_sum, _make_reduction("neighbor_sum", "sum", lambda x: x.dtype.scalar_type(0))
)
NdArrayField.register_builtin_func(
    fbuiltins.max_over, _make_reduction("max_over", "max", lambda x: x.array_ns.min(x._ndarray))
)
NdArrayField.register_builtin_func(
    fbuiltins.min_over, _make_reduction("min_over", "min", lambda x: x.array_ns.max(x._ndarray))
)


# -- Concrete array implementations --
# NumPy
_nd_array_implementations = [np]


@dataclasses.dataclass(frozen=True, eq=False)
class NumPyArrayField(NdArrayField):
    array_ns: ClassVar[ModuleType] = np


common._field.register(np.ndarray, NumPyArrayField.from_array)


@dataclasses.dataclass(frozen=True, eq=False)
class NumPyArrayConnectivityField(NdArrayConnectivityField):
    array_ns: ClassVar[ModuleType] = np


common._connectivity.register(np.ndarray, NumPyArrayConnectivityField.from_array)

# CuPy
if cp:
    _nd_array_implementations.append(cp)

    @dataclasses.dataclass(frozen=True, eq=False)
    class CuPyArrayField(NdArrayField):
        array_ns: ClassVar[ModuleType] = cp

    common._field.register(cp.ndarray, CuPyArrayField.from_array)

    @dataclasses.dataclass(frozen=True, eq=False)
    class CuPyArrayConnectivityField(NdArrayConnectivityField):
        array_ns: ClassVar[ModuleType] = cp

    common._connectivity.register(cp.ndarray, CuPyArrayConnectivityField.from_array)

# JAX
if jnp:
    _nd_array_implementations.append(jnp)

    @dataclasses.dataclass(frozen=True, eq=False)
    class JaxArrayField(NdArrayField):
        array_ns: ClassVar[ModuleType] = jnp

        def __setitem__(
            self,
            index: common.AnyIndexSpec,
            value: common.Field | core_defs.NDArrayObject | core_defs.ScalarT,
        ) -> None:
            # TODO(havogt): use something like `self.ndarray = self.ndarray.at(index).set(value)`
            raise NotImplementedError("'__setitem__' for JaxArrayField not yet implemented.")

    common._field.register(jnp.ndarray, JaxArrayField.from_array)


def _broadcast(field: common.Field, new_dimensions: Sequence[common.Dimension]) -> common.Field:
    if field.domain.dims == new_dimensions:
        return field
    domain_slice: list[slice | None] = []
    named_ranges = []
    for dim in new_dimensions:
        if (pos := embedded_common._find_index_of_dim(dim, field.domain)) is not None:
            domain_slice.append(slice(None))
            named_ranges.append(common.NamedRange(dim, field.domain[pos].unit_range))
        else:
            domain_slice.append(None)  # np.newaxis
            named_ranges.append(common.NamedRange(dim, common.UnitRange.infinite()))
    return common._field(field.ndarray[tuple(domain_slice)], domain=common.Domain(*named_ranges))


def _builtins_broadcast(
    field: common.Field | core_defs.Scalar, new_dimensions: tuple[common.Dimension, ...]
) -> common.Field:  # separated for typing reasons
    if isinstance(field, common.Field):
        return _broadcast(field, new_dimensions)
    raise AssertionError("Scalar case not reachable from 'fbuiltins.broadcast'.")


NdArrayField.register_builtin_func(fbuiltins.broadcast, _builtins_broadcast)


def _astype(field: common.Field | core_defs.ScalarT | tuple, type_: type) -> NdArrayField:
    if isinstance(field, NdArrayField):
        return field.__class__.from_array(field.ndarray.astype(type_), domain=field.domain)
    raise AssertionError("This is the NdArrayField implementation of 'fbuiltins.astype'.")


NdArrayField.register_builtin_func(fbuiltins.astype, _astype)


def _get_slices_from_domain_slice(
    domain: common.Domain,
    domain_slice: common.Domain | Sequence[common.NamedRange | common.NamedIndex],
) -> common.RelativeIndexSequence:
    """Generate slices for sub-array extraction based on named ranges or named indices within a Domain.

    This function generates a tuple of slices that can be used to extract sub-arrays from a field. The provided
    named ranges or indices specify the dimensions and ranges of the sub-arrays to be extracted.

    Args:
        domain (common.Domain): The Domain object representing the original field.
        domain_slice (DomainSlice): A sequence of dimension names and associated ranges.

    Returns:
        tuple[slice | int | None, ...]: A tuple of slices representing the sub-array extraction along each dimension
                                       specified in the Domain. If a dimension is not included in the named indices
                                       or ranges, a None is used to indicate expansion along that axis.
    """
    slice_indices: list[slice | common.IntIndex] = []

    for pos_old, (dim, _) in enumerate(domain):
        if (pos := embedded_common._find_index_of_dim(dim, domain_slice)) is not None:
            _, index_or_range = domain_slice[pos]
            slice_indices.append(_compute_slice(index_or_range, domain, pos_old))
        else:
            slice_indices.append(slice(None))
    return tuple(slice_indices)


def _compute_slice(
    rng: common.UnitRange | common.IntIndex, domain: common.Domain, pos: int
) -> slice | common.IntIndex:
    """Compute a slice or integer based on the provided range, domain, and position.

    Args:
        rng (DomainRange): The range to be computed as a slice or integer.
        domain (common.Domain): The domain containing dimension information.
        pos (int): The position of the dimension in the domain.

    Returns:
        slice | int: Slice if `new_rng` is a UnitRange, otherwise an integer.

    Raises:
        ValueError: If `new_rng` is not an integer or a UnitRange.
    """
    if isinstance(rng, common.UnitRange):
        start = (
            rng.start - domain.ranges[pos].start
            if common.UnitRange.is_left_finite(domain.ranges[pos])
            else None
        )
        stop = (
            rng.stop - domain.ranges[pos].start
            if common.UnitRange.is_right_finite(domain.ranges[pos])
            else None
        )
        return slice(start, stop)
    elif common.is_int_index(rng):
        assert common.Domain.is_finite(domain)
        return rng - domain.ranges[pos].start
    else:
        raise ValueError(f"Can only use integer or UnitRange ranges, provided type: '{type(rng)}'.")
