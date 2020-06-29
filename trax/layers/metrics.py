# coding=utf-8
# Copyright 2020 The Trax Authors.
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

# Lint as: python3
"""Trax layers for computing metrics (loss functions and evaluation metrics).

A metric layer takes three inputs:

  - model output: Batch of predicted values (typically vectors).
  - targets: Batch of target values (e.g., categories or vectors).
  - weights: Tensor that can assign different weights to different positions in.
    the model output. One common use of weights is for masking -- assigning
    weight 0 to positions that correspond to padding in the input so that they
    don't affect metrics.

and returns a single (per-batch) scalar.

The `L2Loss` layer treats a batch as an unanalyzed tensor and computes an
elementwise-weighted loss (Frobenius norm).

Other metric layers take into account the items that make up a batch. For each
item in a batch, a raw metric value is computed by comparing (item-wise) the
model output to the target value. These item-wise values are then combined into
a single scalar for the batch by a weighted reduction function, typically
weighted mean. For example:

  - Accuracy: Treat model output as giving different strength/votes to the
    possible categories; measure the category prediction as correct (value 1)
    if `argmax(output) == target_category`, else as incorrect (value 0). The
    accuracy for the batch is then the weighted mean of these 1's and 0's.

  - Cross Entropy: Treat model output and target values as two probability
    distributions; measure the cross entropy of the model output relative to
    the (assumed true) target distribution. The scalar value for the batch is
    then the weighted mean of the item-wise cross-entropy values.

In deriving a single scalar for the batch, there is flexibility to use reducing
functions other than mean, for instance sum or a specialized sequence mean.
"""

import jax

from trax import fastmath
from trax import shapes
from trax.fastmath import numpy as jnp
from trax.layers import combinators as cb
from trax.layers import core
from trax.layers.base import Fn


# TODO(jonni): Consider renaming to FrobeniusLoss.
def L2Loss():
  """Returns a layer that computes total L2 loss for one batch."""
  def f(model_output, targets, weights):  # pylint: disable=invalid-name
    """Returns elementwise-weighted Frobenius norm of `model_output - targets`.

    Args:
      model_output: Output from one batch, treated as an unanalyzed tensor.
      targets: Tensor of same shape as `model_output` containing element-wise
          target values.
      weights: Tensor of same shape as `model_output` and `targets`.
    """
    shapes.assert_same_shape(model_output, targets)
    shapes.assert_same_shape(targets, weights)
    l2 = weights * (model_output - targets)**2
    return jnp.sum(l2) / jnp.sum(weights)
  return Fn('L2Loss', f)


def AccuracyScalar():
  """Returns a layer that computes mean category prediction accuracy."""
  return _WeightedMeanOf(_Accuracy())


def SequenceAccuracyScalar():
  """Returns a layer that computes mean sequence prediction accuracy."""
  return _WeightedMeanOf(_Accuracy(),
                         final_layer_override=_WeightedSequenceMean())


def CrossEntropyLoss():
  """Returns a layer that computes mean prediction-target cross entropy."""
  return _WeightedMeanOf(_CrossEntropy())


def CrossEntropySum():
  """Returns a layer that computes sum of prediction-target cross entropies."""
  return _WeightedMeanOf(_CrossEntropy(),
                         final_layer_override=WeightedSum())


def SumOfWeights():
  """Returns a layer that computes sum of weights."""
  return cb.Serial(
      cb.Drop(),  # Drop inputs.
      cb.Drop(),  # Drop targets.
      core.Sum(axis=None)  # Sum weights.
  )
# pylint: enable=no-value-for-parameter


def _Accuracy(axis=-1):
  """Returns a layer that scores predicted versus target category."""
  def f(model_output, target_category):  # pylint: disable=invalid-name
    predicted_category = jnp.argmax(model_output, axis=axis)
    # TODO(pkozakowski): This assertion breaks some tests. Fix and uncomment.
    # shapes.assert_same_shape(predicted_category, target_category)
    return jnp.equal(predicted_category, target_category).astype(jnp.float32)
  return Fn('_Accuracy', f)


def _CrossEntropy():
  """Returns a layer that computes prediction-target cross entropies."""
  def f(model_output, target_category):  # pylint: disable=invalid-name
    # TODO(pkozakowski): This assertion breaks some tests. Fix and uncomment.
    # shapes.assert_shape_equals(target_category, model_output.shape[:-1])
    target_distribution = one_hot(target_category, model_output.shape[-1])
    return -1.0 * jnp.sum(model_output * target_distribution, axis=-1)
  return Fn('_CrossEntropy', f)


def _WeightedMean():
  """Returns a layer to compute weighted mean over all values in the input."""
  def f(values, weights):  # pylint: disable=invalid-name
    return jnp.sum(values * weights) / jnp.sum(weights)
  return Fn('_WeightedMean', f)


def WeightedSum():
  """Returns a layer to compute weighted sum over all values in the input."""
  def f(values, weights):  # pylint: disable=invalid-name
    return jnp.sum(values * weights)
  return Fn('WeightedSum', f)


def _WeightedSequenceMean():
  """Returns a layer to compute weighted seqeunce accuracy mean."""
  def f(values, weights):  # pylint: disable=invalid-name
    # This function assumes weights are 0 or 1.
    # Then compute 1: not-correct, 0: correct or masked
    not_correct = (1.0 - values) * weights
    axis_to_sum = list(range(1, len(not_correct.shape)))
    # Summing not-correct on all axes but batch. We're summing 0s and 1s,
    # so the sum is 0 if it's all 0 and >=1 in all other cases.
    not_correct_seq = jnp.sum(not_correct, axis=axis_to_sum)
    # Sequence is correct if not_correct_seq is 0, reverting here.
    correct_seq = 1.0 - jnp.minimum(1.0, not_correct_seq)
    return jnp.mean(correct_seq)  # Mean over batch.
  return Fn('_WeightedSequenceMean', f)


# pylint: disable=no-value-for-parameter
# TODO(jonni): Consider redesign where _WeightedMean is the default reducer.
def _WeightedMeanOf(raw_metric, final_layer_override=None):
  """Wraps a metric computation with a weighted mean or other reducing function.

  This layer depends on externally provided weights that match (or can be
  broadcasted to match) the output of the `raw_metric` layer.

  Args:
    raw_metric: Layer with two inputs -- predictions and targets -- that
        computes the raw logic of a metric prior to batch-level weighting or
        pooling.
    final_layer_override: Layer that reduces raw metric output to a single
        scalar, in place of the default weighted mean.

  Returns:
    A layer that computes a single scalar by combining values from the given
    raw metric function.
  """
  final_layer = final_layer_override or _WeightedMean()  # For sequence acc.
  return cb.Serial(
      raw_metric,
      final_layer
  )
# pylint: enable=no-value-for-parameter


# TODO(jonni): Figure out the right name and home for this function.
def one_hot(x, n_categories, dtype=jnp.float32):  # pylint: disable=invalid-name
  """Makes a one-hot array (n+1 dims) from an int-categorical array (n dims)."""
  indices_less_than_n = jnp.arange(n_categories)
  if fastmath.backend_name() == 'jax':
    # Work around a jax broadcasting issue.
    indices_less_than_n = jax.lax.tie_in(x, indices_less_than_n)
  return jnp.array(x[..., jnp.newaxis] == indices_less_than_n, dtype)
