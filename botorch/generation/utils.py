#!/usr/bin/env python3
# Copyright (c) Meta Platforms, Inc. and affiliates.
#
# This source code is licensed under the MIT license found in the
# LICENSE file in the root directory of this source tree.

from __future__ import annotations

import warnings
from collections.abc import Callable
from dataclasses import dataclass

import torch

from botorch.acquisition import AcquisitionFunction, FixedFeatureAcquisitionFunction
from botorch.optim.parameter_constraints import (
    _generate_unfixed_lin_constraints,
    _generate_unfixed_nonlin_constraints,
)
from torch import Tensor


def _convert_nonlinear_inequality_constraints(
    nonlinear_inequality_constraints: list[Callable | tuple[Callable, bool]],
) -> list[tuple[Callable, bool]]:
    """Convert legacy defintions of nonlinear inequality constraints into the new
    format. Assumes intra-point constraints.
    """
    nlcs = []
    legacy = False
    # return nonlinear_inequality_constraints
    for nlc in nonlinear_inequality_constraints:
        if callable(nlc):
            # old style --> convert
            nlcs.append((nlc, True))
            legacy = True
        else:
            nlcs.append(nlc)
    if legacy:
        warnings.warn(
            "The `nonlinear_inequality_constraints` argument is expected "
            "take a list of tuples. Passing a list of callables "
            "will result in an error in future versions.",
            DeprecationWarning,
            stacklevel=3,
        )

    return nlcs


def _flip_sub_unique(x: Tensor, k: int) -> Tensor:
    """Get the first k unique elements of a single-dimensional tensor, traversing the
    tensor from the back.

    Args:
        x: A single-dimensional tensor
        k: the number of elements to return

    Returns:
        A tensor with min(k, |x|) elements.

    Example:
        >>> x = torch.tensor([1, 6, 4, 3, 6, 3])
        >>> y = _flip_sub_unique(x, 3)  # tensor([3, 6, 4])
        >>> y = _flip_sub_unique(x, 4)  # tensor([3, 6, 4, 1])
        >>> y = _flip_sub_unique(x, 10)  # tensor([3, 6, 4, 1])

    NOTE: This should really be done in C++ to speed up the loop. Also, we would like
    to make this work for arbitrary batch shapes, I'm sure this can be sped up.
    """
    n = len(x)
    i = 0
    out = set()
    idcs = torch.empty(k, dtype=torch.long)
    for j, xi in enumerate(x.flip(0).tolist()):
        if xi not in out:
            out.add(xi)
            idcs[i] = n - 1 - j
            i += 1
        if len(out) >= k:
            break
    return x[idcs[: len(out)]]


@dataclass(frozen=True, repr=False, eq=False)
class _NoFixedFeatures:
    """
    Dataclass to store the objects after removing fixed features.
    Objects here refer to the acquisition function, initial conditions,
    bounds and parameter constraints.
    """

    acquisition_function: FixedFeatureAcquisitionFunction
    initial_conditions: Tensor
    lower_bounds: float | Tensor | None
    upper_bounds: float | Tensor | None
    inequality_constraints: list[tuple[Tensor, Tensor, float]] | None
    equality_constraints: list[tuple[Tensor, Tensor, float]] | None
    nonlinear_inequality_constraints: list[Callable[[Tensor], Tensor]] | None


def _remove_fixed_features_from_optimization(
    fixed_features: dict[int, float | None],
    acquisition_function: AcquisitionFunction,
    initial_conditions: Tensor,
    lower_bounds: float | Tensor | None,
    upper_bounds: float | Tensor | None,
    inequality_constraints: list[tuple[Tensor, Tensor, float]] | None,
    equality_constraints: list[tuple[Tensor, Tensor, float]] | None,
    nonlinear_inequality_constraints: list[Callable[[Tensor], Tensor]] | None,
) -> _NoFixedFeatures:
    """
    Given a set of non-empty fixed features, this function effectively reduces the
    dimensionality of the domain that the acquisition function is being optimized
    over by removing the set of fixed features. Consequently, this function returns a
    new `FixedFeatureAcquisitionFunction`, new constraints, and bounds defined over
    unfixed features.

    Args:
        fixed_features: This is a dictionary of feature indices to values, where
            all generated candidates will have features fixed to these values.
            If the dictionary value is None, then that feature will just be
            fixed to the clamped value and not optimized. Assumes values to be
            compatible with lower_bounds and upper_bounds!
        acquisition_function: Acquisition function over the original domain being
            maximized.
        initial_conditions: Starting points for optimization w.r.t. the complete domain.
        lower_bounds: Minimum values for each column of initial_conditions.
        upper_bounds: Minimum values for each column of initial_conditions.
        inequality constraints: A list of tuples (indices, coefficients, rhs),
            with each tuple encoding an inequality constraint of the form
            `sum_i (X[indices[i]] * coefficients[i]) >= rhs`.
        equality constraints: A list of tuples (indices, coefficients, rhs),
            with each tuple encoding an inequality constraint of the form
            `sum_i (X[indices[i]] * coefficients[i]) = rhs`.
        nonlinear_inequality_constraints: A list of callables with that represent
            non-linear inequality constraints of the form `callable(x) >= 0`. Each
            callable is expected to take a `(num_restarts) x q x d`-dim tensor as
            an input and return a `(num_restarts) x q`-dim tensor with the
            constraint values.

    Returns:
        _NoFixedFeatures dataclass object.
    """
    # sort the keys for consistency
    sorted_keys = sorted(fixed_features)
    sorted_values = []
    for key in sorted_keys:
        if fixed_features[key] is None:
            val = initial_conditions[..., [key]]
        else:
            val = fixed_features[key]
        sorted_values.append(val)

    d = initial_conditions.shape[-1]
    acquisition_function = FixedFeatureAcquisitionFunction(
        acq_function=acquisition_function,
        d=d,
        columns=sorted_keys,
        values=sorted_values,
    )

    # extract initial_conditions, bounds at unfixed indices
    unfixed_indices = sorted(set(range(d)) - set(sorted_keys))
    initial_conditions = initial_conditions[..., unfixed_indices]
    if isinstance(lower_bounds, Tensor):
        lower_bounds = lower_bounds[..., unfixed_indices]
    if isinstance(upper_bounds, Tensor):
        upper_bounds = upper_bounds[..., unfixed_indices]

    inequality_constraints = _generate_unfixed_lin_constraints(
        constraints=inequality_constraints,
        fixed_features=fixed_features,
        dimension=d,
        eq=False,
    )
    equality_constraints = _generate_unfixed_lin_constraints(
        constraints=equality_constraints,
        fixed_features=fixed_features,
        dimension=d,
        eq=True,
    )
    nonlinear_inequality_constraints = _generate_unfixed_nonlin_constraints(
        constraints=nonlinear_inequality_constraints,
        fixed_features=fixed_features,
        dimension=d,
    )
    return _NoFixedFeatures(
        acquisition_function=acquisition_function,
        initial_conditions=initial_conditions,
        lower_bounds=lower_bounds,
        upper_bounds=upper_bounds,
        inequality_constraints=inequality_constraints,
        equality_constraints=equality_constraints,
        nonlinear_inequality_constraints=nonlinear_inequality_constraints,
    )
