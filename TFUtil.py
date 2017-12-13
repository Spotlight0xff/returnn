
from __future__ import print_function

import tensorflow as tf
from tensorflow.python.client import device_lib
import contextlib
import os
import sys
from Util import NotSpecified, NativeCodeCompiler


def tf_version_tuple():
  """
  :return: version tuple, e.g. (1, 1, 0), parsed from tf.__version__
  :rtype: tuple[int]
  """
  import re
  return tuple([int(s) for s in re.sub('-rc[0-9]+', '', tf.__version__).split(".")])


def assert_min_tf_version(version, reason):
  """
  :param tuple[int] version: e.g. (1,2,0) or (1,2)
  :param str reason:
  """
  tf_version = tf_version_tuple()
  assert len(version) <= len(tf_version)
  assert tf_version >= version, "Your TF version %r is too old (older than %r). %s" % (tf_version, version, reason)


def have_min_tf_version(version):
  """
  :param tuple[int] version: e.g. (1,2,0) or (1,2)
  :return: True if we have at least that version, or newer
  :rtype: bool
  """
  tf_version = tf_version_tuple()
  assert len(version) <= len(tf_version)
  return tf_version >= version


class Data(object):
  """
  This class is to describe a tensor,
  i.e. it's shape and properties like
  whether we should consider it as sparse data (i.e. it represents indices).
  This is used in TFNetwork to describe the dataset external data
  as well as for every layer output.
  """

  size_dtype = "int32"

  def __init__(self, name,
               shape=None, dtype=None,
               placeholder=None,
               sparse=None,
               dim=None,
               size_placeholder=None,
               batch_dim_axis=0,
               time_dim_axis=NotSpecified,
               available_for_inference=True,
               auto_create_placeholders=False,
               beam_size=None):
    """
    :param str name:
    :param tuple[int|None]|list[int|None] shape: including time-dim (can be None). excluding batch-dim.
      e.g. (time,feat)=(None,128)
    :param str dtype: e.g. "float32" or "int64"
    :param tf.Tensor|None placeholder: with added batch-dim
    :param bool sparse: whether to treat the value as an index. do not confuse with tf.SparseTensor
    :param None|int dim: feature dimension, shape[-1] if not sparse, otherwise like num_classes
    :param int|None batch_dim_axis: where we add the batch-dim.
      e.g. shape=(time,...), 0 -> (batch,time,...), 1 -> (time,batch,...).
      This is normally always set, and a lot of code expects this. However, you can set it to None
      if this Data does not have a batch-dim.
    :param int|None time_dim_axis: where we have the time dim axis, after we added the batch-dim.
      this is often 1. however, can be None if there is no time-dim.
    :param dict[int,tf.Tensor] tf.Tensor size_placeholder: for every None in shape, this will describe the size.
      The size is always a tensor of shape (batch,), i.e. the size can be different for each sequence in a batch.
    :param bool available_for_inference: e.g. the extern data "classes" is usually not available for inference
    :param int|None beam_size: the batch-dim could be extended by a beam-size,
      such that it represents the merged dims [batch, beam_size].
    """
    self.name = name
    if sparse is None:
      if dtype and (dtype.startswith("int") or dtype.startswith("uint")):
        sparse = True
      else:
        sparse = False
    self.sparse = sparse
    if shape is None:
      assert dim, "no shape specified, need dim"
      if sparse:
        shape = (None,)  # assume common (time,)
      else:
        shape = (None, dim)  # assume common (time,feat)
    self.shape = tuple(shape)  # type: tuple[int|None]  # excluding batch-dim. see self.batch_shape
    if dtype is None:
      if sparse:
        dtype = "int32"
      else:
        dtype = "float32"
    if dim is None and len(shape):
      assert not sparse, "need dim"
      dim = shape[-1]
    self.dim = dim  # type: int
    self.batch_dim_axis = batch_dim_axis  # type: int|None  # usually not None
    if time_dim_axis is NotSpecified:
      if batch_dim_axis is None:
        time_dim_axis = None
      elif (sparse and len(shape) >= 1) or ((not sparse) and len(shape) >= 2):
        if batch_dim_axis >= 1:
          time_dim_axis = 0
        else:
          time_dim_axis = 1
      else:
        time_dim_axis = None
    self.time_dim_axis = time_dim_axis  # type: int|None  # counted with batch-dim
    self.dtype = dtype  # type: str
    if placeholder is None and auto_create_placeholders:
      with tf.name_scope("extern_data/placeholders/%s/" % name):
        placeholder = tf.placeholder(**self.get_placeholder_kwargs(with_batch=True))
    self.placeholder = placeholder  # type: tf.Tensor  # this will hold the data value itself
    # The size_placeholder is for each variable length dimension in shape, i.e. excluding the batch-dim.
    if size_placeholder is None and auto_create_placeholders:
      size_placeholder = {}  # type: dict[int,tf.Tensor]
      with tf.name_scope("extern_data/placeholders/%s/" % name):
        for axis in self.get_axes_with_size():
          size_placeholder[axis] = tf.placeholder(**self.get_size_placeholder_kwargs(axis))
    if not size_placeholder and self.ndim_dense <= 1:
      size_placeholder = {}
    self.size_placeholder = size_placeholder  # type: dict[int,tf.Tensor]  # axis w.o. batch -> size of shape (batch,)
    self.available_for_inference = available_for_inference
    self.beam_size = beam_size

  def get_placeholder_kwargs(self, with_batch=True):
    return dict(name=self.name, dtype=self.dtype, shape=self.batch_shape if with_batch else self.shape)

  def get_axes_with_size(self):
    """
    :return: list of axes which can vary in size for each entry of the batch-dim, e.g. the time-dim-axis.
      The axis index is counted without the batch-dim.
    :rtype: list[int]
    """
    return [i for (i, dim) in enumerate(self.shape) if dim is None]

  def get_size_placeholder_kwargs(self, axis, with_batch=True):
    # For each batch a separate size.
    return dict(name="%s_dim%i_size" % (self.name, axis), dtype=self.size_dtype,
                shape=(None,) if with_batch else ())

  def get_kwargs(self):
    keys = ["name", "shape", "dtype", "sparse", "dim", "batch_dim_axis", "time_dim_axis"]
    if not self.available_for_inference:
      keys += ["available_for_inference"]
    if self.beam_size is not None:
      keys += ["beam_size"]
    return {key: getattr(self, key) for key in keys}

  def get_description(self, with_name=True, with_placeholder=False):
    keys = ["shape", "dtype"]
    if self.sparse:
      keys.append("sparse")
      keys.append("dim")
    if self.batch_dim_axis != 0:
      keys.append("batch_dim_axis")
    if self.time_dim_axis is None or self.time_dim_axis >= 2:
      keys.append("time_dim_axis")
    if with_name:
      keys.insert(0, "name")
    if with_placeholder:
      keys.append("placeholder")
    if not self.available_for_inference:
      keys.append("available_for_inference")
    if self.beam_size is not None:
      keys.append("beam_size")
    return "Data(%s)" % ", ".join(["%s=%r" % (key, getattr(self, key)) for key in keys])

  def __repr__(self):
    return self.get_description()

  def copy(self, name=None):
    """
    :param str name: if given, will overwrite this name
    :return: copy of myself, using self.get_kwargs(), and with placeholder and size_placeholder
    :rtype: Data
    """
    data = Data(**self.get_kwargs())
    data.placeholder = self.placeholder
    if self.size_placeholder is not None:
      data.size_placeholder = self.size_placeholder.copy()
    if name:
      data.name = name
    return data

  def copy_as_batch_major(self):
    """
    :return: copy of myself with batch_dim_axis == 0
    :rtype: Data
    """
    return self.copy_with_batch_dim_axis(0)

  def copy_as_time_major(self):
    """
    :return: copy of myself with time_dim_axis == 0
    :rtype: Data
    """
    assert self.batch_dim_axis is not None
    assert self.time_dim_axis is not None
    data = self.copy()
    if data.time_dim_axis != 0:
      if data.placeholder is not None:
        with reuse_name_scope_of_tensor(data.placeholder):
          with tf.name_scope("%s_as_time_major" % (data.name,)):
            data.placeholder = swapaxes(data.placeholder, 0, data.time_dim_axis)
      if data.batch_dim_axis <= data.time_dim_axis:
        data.batch_dim_axis += 1
      data.time_dim_axis = 0
    return data

  def copy_with_batch_dim_axis(self, batch_dim_axis):
    """
    :param int batch_dim_axis:
    :return: copy of myself with specific batch_dim_axis
    :rtype: Data
    """
    assert self.batch_dim_axis is not None
    data = self.copy()
    if data.batch_dim_axis != batch_dim_axis:
      if data.placeholder is not None:
        with tf.name_scope("%s_with_batch_axis_%i" % (data.name, batch_dim_axis)):
          data.placeholder = swapaxes(data.placeholder, batch_dim_axis, data.batch_dim_axis)
      other_special_axes = data.get_special_axes_dict(counted_with_batch_dim=False, only_available=True)
      data.batch_dim_axis = batch_dim_axis
      for k, a in other_special_axes.items():
        setattr(data, k, data.get_batch_axis(a))
    return data

  def copy_add_batch_dim(self, batch_dim_axis):
    """
    :param int batch_dim_axis:
    :return: copy of myself with added batch-dim
    :rtype: Data
    """
    assert self.batch_dim_axis is None
    if not self.sparse:
      assert batch_dim_axis <= self.feature_dim_axis, "does not work yet otherwise, feature-dim-axis must be last"
    data = self.copy()
    if data.placeholder is not None:
      data.placeholder = tf.expand_dims(data.placeholder, batch_dim_axis, name="%s_add_batch_dim" % self.name)
    data.batch_dim_axis = batch_dim_axis
    other_special_axes = self.get_special_axes_dict(counted_with_batch_dim=True, only_available=True)
    for k, a in other_special_axes.items():
      setattr(data, k, a if (a < batch_dim_axis) else (a + 1))
    return data

  def copy_add_spatial_dim(self, spatial_dim_axis):
    """
    :param int spatial_dim_axis: counted with batch-dim. if there is no time-dim, this will be it
    :return: copy of myself with added spatial-dim
    :rtype: Data
    """
    data = self.copy()
    if data.placeholder is not None:
      data.placeholder = tf.expand_dims(data.placeholder, spatial_dim_axis, name="%s_add_spatial_dim" % self.name)
    axis_wo_batch = spatial_dim_axis if (spatial_dim_axis <= (self.batch_dim_axis or 0)) else (spatial_dim_axis - 1)
    data.shape = data.shape[:axis_wo_batch] + (1,) + data.shape[axis_wo_batch:]
    if data.time_dim_axis is None:
      data.time_dim_axis = spatial_dim_axis
    other_special_axes = self.get_special_axes_dict(
      counted_with_batch_dim=True, only_available=True, include_batch_dim_axis=True)
    for k, a in other_special_axes.items():
      setattr(data, k, a if (a < spatial_dim_axis) else (a + 1))
    return data

  def copy_compatible_to(self, data):
    """
    :param Data data: other data which the returned tensor should be compatible to
    :returns: Data, might add broadcast dimensions
    :rtype: Data
    """
    assert self.sparse == data.sparse
    assert self.dtype == data.dtype
    v = self.copy()
    # First add spatial dims, in case we miss any.
    for axis in data.get_spatial_batch_axes():
      if len(data.get_spatial_batch_axes()) > len(v.get_spatial_batch_axes()):
        axis_wo_batch = data.get_batch_axis_excluding_batch(axis)
        v = v.copy_add_spatial_dim(v.get_batch_axis(axis_wo_batch))
    assert data.get_spatial_axes() == v.get_spatial_axes()
    if v.batch_dim_axis != data.batch_dim_axis:
      if v.batch_dim_axis is not None:
        v = v.copy_with_batch_dim_axis(data.batch_dim_axis)
      else:
        # Note that it might be important here that we added any missing spatial dims before.
        v = v.copy_add_batch_dim(data.batch_dim_axis)
    assert v.batch_dim_axis == data.batch_dim_axis
    assert data.feature_dim_axis == v.feature_dim_axis
    return v

  def copy_time_flattened(self):
    """
    :return: copy of myself where the time-axis is flattened away into the batch-dim-axis.
      See :func:`get_placeholder_time_flattened` and :func:`flatten_with_seq_len_mask for more details.
    :rtype: Data
    """
    assert self.batch_dim_axis is not None
    assert self.time_dim_axis is not None
    data = self.copy()
    if data.placeholder is not None:
      data.placeholder = data.get_placeholder_time_flattened()
    data.shape = tuple([
      data.batch_shape[i] for i in range(data.batch_ndim)
      if i not in (data.batch_dim_axis, data.time_dim_axis)])
    if data.size_placeholder is not None:
      if data.time_dim_axis_excluding_batch in data.size_placeholder:
        del data.size_placeholder[data.time_dim_axis_excluding_batch]
    data.time_dim_axis = None
    return data

  def copy_extend_with_beam(self, beam_size, dyn_beam_size=None):
    """
    :param int beam_size:
    :param tf.Tensor|None dyn_beam_size: if the beam size is dynamic
    :return: copy of myself where the batch-dim is extended/multiplied by beam_size, using tile_transposed
    :rtype: Data
    """
    if dyn_beam_size is None:
      dyn_beam_size = beam_size
    with tf.name_scope("data_extend_with_beam"):
      data = self.copy()
      if data.beam_size and data.beam_size == beam_size:
        return data
      assert data.beam_size is None, "incompatible beam sizes (%r vs %r)" % (data.beam_size, beam_size)
      if data.placeholder is not None:
        data.placeholder = tile_transposed(data.placeholder, axis=data.batch_dim_axis, multiples=dyn_beam_size)
      if data.size_placeholder is not None:
        data.size_placeholder = {
          i: tile_transposed(v, axis=0, multiples=dyn_beam_size) for (i, v) in data.size_placeholder.items()}
      data.beam_size = beam_size * (data.beam_size or 1)
      return data

  def copy_template(self, name=None):
    """
    :return: copy of myself, using self.get_kwargs(), without placeholder
    :rtype: Data
    """
    kwargs = self.get_kwargs()
    if name:
      kwargs["name"] = name
    return Data(**kwargs)

  def copy_template_excluding_time_dim(self, name=None):
    """
    :param str|None name: if set, this will be the new name
    :return: copy of myself excluding the time-dimension without placeholder
    :rtype: Data
    """
    assert self.batch_dim_axis is not None
    assert self.time_dim_axis is not None
    new_shape = list(self.shape)
    del new_shape[self.time_dim_axis_excluding_batch]
    kwargs = self.get_kwargs()
    kwargs["batch_dim_axis"] = (
      self.batch_dim_axis
      if (self.batch_dim_axis < self.time_dim_axis)
      else (self.batch_dim_axis - 1))
    kwargs["time_dim_axis"] = None
    kwargs["shape"] = new_shape
    if name:
      kwargs["name"] = name
    return Data(**kwargs)

  def copy_template_adding_time_dim(self, name=None, time_dim_axis=0):
    """
    :param str|None name: if set, this will be the new name
    :param int time_dim_axis: the new time-dim-axis index
    :return: copy of myself adding the time-dimension without placeholder
    :rtype: Data
    """
    assert self.batch_dim_axis is not None
    assert self.time_dim_axis is None
    new_shape = list(self.shape)
    new_shape.insert(time_dim_axis, None)
    kwargs = self.get_kwargs()
    kwargs["batch_dim_axis"] = (
      self.batch_dim_axis
      if (self.batch_dim_axis < time_dim_axis)
      else (self.batch_dim_axis + 1))
    kwargs["time_dim_axis"] = time_dim_axis
    kwargs["shape"] = new_shape
    if name:
      kwargs["name"] = name
    return Data(**kwargs)

  def _get_variable_dim_pattern(self):
    """
    :return: tuple with bools specifying which dims of the shape (excluding batch-dim) are of variable length.
     e.g. (time,feature), shape=(None,128), this returns (True, False)
    :rtype: tuple[bool]
    """
    return tuple([dim is None for dim in self.shape])

  def _get_var_len_axes(self):
    return sorted([i for (i, d) in enumerate(self._get_variable_dim_pattern()) if d])

  def matches_var_dim_pattern(self, other):
    """
    :param Data other:
    :return: whether the variable-dims pattern matches,
      i.e. same variable dims (get_variable_dim_pattern), same time dim, excluding batch-dim.
      i.e. the size_placeholder should be compatible.
    :rtype: bool
    """
    if self.time_dim_axis_excluding_batch != other.time_dim_axis_excluding_batch:
      return False
    return self._get_var_len_axes() == other._get_var_len_axes()

  @property
  def batch_shape(self):
    """
    :return: shape with added batch-dim. e.g. (batch,time,feat) = (None,None,128)
    :rtype: tuple[int|None]
    """
    if self.batch_dim_axis is not None:
      return self.shape[:self.batch_dim_axis] + (None,) + self.shape[self.batch_dim_axis:]
    return self.shape

  @property
  def shape_dense(self):
    if self.sparse:
      return self.shape + (self.dim,)
    return self.shape

  @property
  def ndim(self):
    """
    :rtype: int
    :return: ndim counted without batch-dim
    """
    return len(self.shape)

  @property
  def ndim_dense(self):
    """
    :rtype: int
    :return: ndim counted without batch-dim, added by 1 if we are sparse
    """
    if self.sparse:
      return self.ndim + 1
    return self.ndim

  @property
  def batch_ndim(self):
    """
    :rtype: int
    :return: ndim counted with batch-dim
    """
    if self.batch_dim_axis is not None:
      return self.ndim + 1
    return self.ndim

  @property
  def is_time_major(self):
    """
    :return: whether this is in time-major format, i.e. (time,batch,...)
    :rtype: bool
    """
    return self.time_dim_axis == 0

  @property
  def is_batch_major(self):
    """
    :return: whether this is in batch-major format, i.e. (batch,...)
    :rtype: bool
    """
    return self.batch_dim_axis == 0

  @property
  def time_dim_axis_excluding_batch(self):
    if self.time_dim_axis is None:
      return None
    return self.get_batch_axis_excluding_batch(self.time_dim_axis)

  def time_dimension(self):
    """
    :return: shape(placeholder)[time_dim_axis], int scalar
    :rtype: tf.Tensor
    """
    assert self.time_dim_axis is not None
    with reuse_name_scope_of_tensor(self.placeholder):
      with tf.name_scope("time_dim"):
        return tf.shape(self.placeholder)[self.time_dim_axis]

  def get_placeholder_as_time_major(self):
    return self.copy_as_time_major().placeholder

  def get_placeholder_as_batch_major(self):
    return self.copy_as_batch_major().placeholder

  def get_placeholder_with_specific_batch_dim_axis(self, batch_dim_axis):
    if self.batch_dim_axis == batch_dim_axis:
      return self.placeholder
    return swapaxes(self.placeholder, batch_dim_axis, self.batch_dim_axis)

  def get_placeholder_time_flattened(self):
    assert self.have_time_axis()
    # flatten_with_seq_len_mask only works for these two cases at the moment:
    assert (self.time_dim_axis, self.batch_dim_axis) == (0, 1) or (self.time_dim_axis, self.batch_dim_axis) == (1, 0)
    seq_lens = self.size_placeholder[self.time_dim_axis_excluding_batch]
    return flatten_with_seq_len_mask(self.placeholder, seq_lens, time_major=self.is_time_major)

  def get_placeholder_flattened(self, keep_dims=False):
    """
    :param bool keep_dims: if set, it will add broadcast dimensions after the flattening behind the first axis
    :rtype: tf.Tensor
    :return: placeholder where all dynamic axes are flattened into a single axis.
      e.g. for the usual case (batch, time, dim), it becomes (batch'|time', dim),
      or (batch, time, height, dim) will also become (batch'|time', dim).
      with keep_dims, (batch, time, height, dim) will become (batch'|time', 1, 1, dim).
    """
    x = self.placeholder
    dyn_axes = self.get_spatial_batch_axes() + [self.batch_dim_axis]
    if dyn_axes == [self.batch_dim_axis]:
      return x
    assert 0 in dyn_axes, "would need some transpose, not supported at the moment"
    assert len(dyn_axes) > 1
    orig_num_dyn_axes = len(dyn_axes)
    ndim = len(self.batch_shape)
    if self.have_time_axis():
      x = self.get_placeholder_time_flattened()
      removed_axis = max(self.time_dim_axis, self.batch_dim_axis)
      dyn_axes.remove(removed_axis)
      dyn_axes = [(i if (i < removed_axis) else (i - 1))
                  for i in dyn_axes]
      ndim -= 1
    if len(dyn_axes) > 1:
      assert 0 in dyn_axes, "would need some transpose, not supported at the moment"
      for i in dyn_axes:
        if i > 0:
          assert i - 1 in dyn_axes, "would need some transpose, not supported at the moment"
      shape = tf.shape(x)
      x = tf.reshape(
        x,
        [tf.reduce_prod([shape[i] for i in dyn_axes])] +
        [shape[i] for i in range(ndim) if i not in dyn_axes])
      dyn_axes = [0]
    assert dyn_axes == [0]
    if keep_dims and orig_num_dyn_axes >= 2:
      for i in range(orig_num_dyn_axes - 1):
        x = tf.expand_dims(x, axis=1)
    return x

  @property
  def feature_dim_axis(self):
    if self.sparse:
      return None
    return self.batch_ndim - 1

  @feature_dim_axis.setter
  def feature_dim_axis(self, value):
    assert value == self.feature_dim_axis, "feature_dim_axis cannot be set at the moment"

  def get_axes(self, exclude_time=False, exclude_batch=False):
    """
    :param bool exclude_time: will filter out the time-axis
    :param bool exclude_batch: will filter out the batch-axis
    :return: list of axes, like `range(len(self.shape))`, calculated with batch dim.
    :rtype: list[int]
    """
    axes = list(range(len(self.batch_shape)))
    if exclude_time and self.time_dim_axis is not None:
      axes.pop(axes.index(self.time_dim_axis))
    if exclude_batch and self.batch_dim_axis is not None:
      axes.pop(axes.index(self.batch_dim_axis))
    return axes

  def get_axes_from_description(self, axes):
    """
    :param int|list[int]|str|list[str] axes: one axis or multiple axis.
      This is counted with batch-dim, which by default is axis 0 (see enforce_batch_dim_axis).
      It also accepts the special tokens "B"|"batch", "spatial", "spatial_except_time", or "F"|"feature",
      and more (see the code).
    :return: list of axes, counted with batch-dim
    :rtype: list[int]
    """
    if isinstance(axes, str):
      import re
      axes = axes.lower()
      if axes in ["b", "batch"]:
        assert self.batch_dim_axis is not None
        axes = self.batch_dim_axis
      elif axes == "spatial":
        axes = self.get_spatial_batch_axes()
      elif re.match("(s|spatial):-?\\d+$", axes):
        s = int(axes.split(":")[1])
        axes = self.get_spatial_batch_axes()
        assert s < len(axes)
        axes = axes[s]
      elif axes == "spatial_except_time":
        axes = self.get_spatial_batch_axes()
        assert self.time_dim_axis is not None
        axes.remove(self.time_dim_axis)
      elif axes in ["t", "time"]:
        assert self.time_dim_axis is not None
        axes = self.time_dim_axis
      elif axes == "except_time":
        axes = list(range(self.batch_ndim))
        axes.remove(self.batch_dim_axis)
        assert self.time_dim_axis is not None
        axes.remove(self.time_dim_axis)
      elif axes in ["f", "feature", "non_spatial"]:
        axes = self.get_feature_batch_axes()
      elif all([a in "btf" for a in axes]):
        return self.get_axes_from_description(list(axes))
      else:
        raise Exception("invalid axis mode %r" % axes)
    if isinstance(axes, int):
      axes = [axes]
    assert isinstance(axes, (tuple, list)), "invalid axis %r" % axes
    flat_axes = []
    for i in axes:
      if isinstance(i, int):
        flat_axes += [i]
      else:
        assert isinstance(i, (str, tuple, list))
        flat_axes += self.get_axes_from_description(i)
    flat_axes = [i % self.batch_ndim for i in flat_axes]
    res = []
    for i in flat_axes:
      if i not in res:
        res.append(i)
    return res

  def get_axis_from_description(self, axis):
    """
    :param int|str axis:
    :return: axis, counted with batch-dim
    :rtype: int
    """
    axes = self.get_axes_from_description(axis)
    assert len(axes) == 1, "%r is not a unique axis but %r" % (axis, axes)
    return axes[0]

  def get_batch_axis_excluding_batch(self, axis):
    """
    :param int axis: counted with batch-dim
    :return: axis counted without batch-dim
    :rtype: int
    """
    if self.batch_dim_axis is None:
      return axis
    if axis == self.batch_dim_axis:
      return None
    if axis < self.batch_dim_axis:
      return axis
    return axis - 1

  def get_batch_axis(self, axis):
    """
    :param int axis: counted without batch-dim
    :return: axis counted with batch-dim
    :rtype: int
    """
    if self.batch_dim_axis is None:
      return axis
    if axis >= self.batch_dim_axis:
      return axis + 1
    return axis

  def have_batch_axis(self):
    return self.batch_dim_axis is not None

  def have_time_axis(self):
    return self.time_dim_axis is not None

  def get_sequence_lengths(self):
    """
    :return: seq lens tensor of shape (batch,) of dtype int32
    :rtype: tf.Tensor
    """
    assert self.time_dim_axis is not None
    return self.size_placeholder[self.time_dim_axis_excluding_batch]

  def get_sequence_mask(self):
    """
    :return: seq mask of shape (batch,time) if we are batch-major, else (time,batch) if we are time-major
    :rtype: tf.Tensor
    """
    assert self.time_dim_axis is not None
    assert self.batch_dim_axis is not None
    if self.is_time_major:
      assert self.batch_dim_axis == 1
      return sequence_mask_time_major(self.get_sequence_lengths())
    else:
      assert self.batch_dim_axis == 0
      assert self.time_dim_axis == 1
      return sequence_mask(self.get_sequence_lengths())

  def get_sequence_mask_broadcast(self):
    """
    :return: seq mask of shape ((batch,time) or (time,batch)) + (1,)s for remaining dims
    :rtype: tf.Tensor
    """
    seq_mask = self.get_sequence_mask()
    assert seq_mask.get_shape().ndims == 2  # batch and time
    seq_mask = expand_multiple_dims(
      seq_mask, [i for i in range(self.batch_ndim) if i not in (self.batch_dim_axis, self.time_dim_axis)])
    assert seq_mask.get_shape().ndims == self.batch_ndim
    return seq_mask

  def get_spatial_batch_axes(self):
    """
    :rtype: list[int]
    :return: list of axes which are not feature and batch axes, counted with batch-dim.
    """
    return [axis
            for axis in range(self.batch_ndim)
            if (axis not in [self.batch_dim_axis, self.feature_dim_axis])]

  def get_spatial_axes(self):
    """
    :rtype: list[int]
    :return: list of axes which are not feature and batch axes, counted without batch-dim.
    """
    return [self.get_batch_axis_excluding_batch(axis) for axis in self.get_spatial_batch_axes()]

  def get_feature_batch_axes(self):
    """
    :rtype: list[int]
    :return: list of axes which are feature axes, counted with batch-dim. currently there is only one or zero such axis.
    """
    if self.feature_dim_axis is not None:
      return [self.feature_dim_axis]
    return []

  def get_feature_axes(self):
    """
    :rtype: list[int]
    :return: list of axes which are feature axes, counted without batch-dim.
    """
    return [self.get_batch_axis_excluding_batch(axis) for axis in self.get_feature_batch_axes()]

  SpecialAxesNames = ("batch_dim_axis", "time_dim_axis", "feature_dim_axis")

  def get_special_axes_dict(self, counted_with_batch_dim=True, include_batch_dim_axis=False, only_available=False):
    """
    :param bool counted_with_batch_dim:
    :param bool include_batch_dim_axis:
    :param bool only_available:
    :return: dict axis-name -> axis
    :rtype: dict[str,int]
    """
    axes = list(self.SpecialAxesNames)
    if include_batch_dim_axis:
      assert counted_with_batch_dim
    else:
      axes.remove("batch_dim_axis")
    d = {k: getattr(self, k) for k in axes}
    if not counted_with_batch_dim:
      d = {k: self.get_batch_axis_excluding_batch(v) if (v is not None) else None
           for (k, v) in d.items()}
    if only_available:
      d = {k: v for (k, v) in d.items() if v is not None}
    return d

  def get_bc_spatial_batch_shape(self):
    """
    :return: shape which will broadcast along all spatial dimensions and time/batch dim
    :rtype: tuple[int]
    """
    dyn_axes = self.get_spatial_batch_axes()
    if self.batch_dim_axis is not None:
      dyn_axes += [self.batch_dim_axis]
    return [1 if (axis in dyn_axes) else dim
            for axis, dim in enumerate(self.batch_shape)]


class CustomUpdate(object):
  def set_on_var(self, var):
    """
    :param tf.Variable var: variable to update. this will be recognized by :class:`TFUpdater.Updater`
    """
    # A bit ugly, but simple.
    setattr(var, "custom_update", self)

  def update_var(self, var):
    """
    :param tf.Variable var: variable to update
    :return: operation which updates the variable, e.g. tf.assign_add(var, something)
    :rtype: tf.Operation
    """
    raise NotImplementedError


class CustomUpdateExpAverage(CustomUpdate):
  """
  exponential moving average
  """

  def __init__(self, average, alpha):
    """
    :param tf.Tensor average:
    :param float alpha:
    """
    self.average = average
    self.alpha = alpha

  def update_var(self, var):
    return tf.assign_add(var, self.alpha * (self.average - var))  # ((alpha - 1) * old + alpha * new)


class OutputWithActivation(object):
  def __init__(self, x, act_func=None):
    """
    :param tf.Tensor x:
    :param None|(tf.Tensor)->tf.Tensor act_func:
    """
    self.x = x
    self.act_func = act_func
    if act_func:
      with tf.name_scope("activation"):
        self.y = act_func(x)
    else:
      self.y = x

  def is_softmax_act_func(self):
    return self.act_func is tf.nn.softmax

  def get_logits(self):
    """
    :rtype: tf.Tensor
    :return: logits. logits are (not necessarily normalized) log probabilities, i.e. the input of softmax.
    This call assumes that self.y is in probability space.
    """
    if self.is_softmax_act_func():
      return self.x
    return tf.log(self.y)


def variable_scalar_summaries_dict(x, name=None):
  """
  Collects all interesting information about `x`, such as min/max/mean, etc. (all scalars).
  This is used by :func:`variable_summaries`.

  :param tf.Tensor|tf.Variable x:
  :param str name:
  :return: dicth with key -> scalar info, e.g. with "%s_mean" % name -> tf.reduce_mean(x)
  :rtype: dict[str,tf.Tensor]
  """
  if x.dtype == tf.string:
    return {}
  if not name:
    name = get_base_name(x)
  mean = tf.reduce_mean(x)
  stddev = tf.sqrt(tf.reduce_mean(tf.square(x - mean)))
  return {
    '%s_mean' % name: mean,
    '%s_stddev' % name: stddev,
    '%s_rms' % name: tf.sqrt(tf.reduce_mean(tf.square(x))),
    '%s_max' % name: tf.reduce_max(x),
    '%s_min' % name: tf.reduce_min(x)}


def variable_summaries(var, name=None, with_histogram=False):
  """
  Attach a lot of summaries to a Tensor (for TensorBoard visualization).
  Also see :func:`variable_scalar_summaries_dict`.

  :param tf.Tensor|tf.Variable var:
  :param str name:
  :param bool with_histogram: adds histogram. note that this can add noticeable overhead
  :return: nothing, use :func:`tf.summary.merge_all()` to collect the summaries
  """
  if var.dtype == tf.string:
    return
  if not name:
    name = get_base_name(var)
  with tf.name_scope('summaries_%s' % name):
    for k, v in variable_scalar_summaries_dict(var, name=name).items():
      tf.summary.scalar(k, v)
    if with_histogram:
      tf.summary.histogram('%s_histogram' % name, var)


def get_current_var_scope_name():
  """
  :return: current absolute variable scope name, via tf.variable_scope
  :rtype: str
  """
  v = tf.get_variable_scope()
  return v.name


def get_current_name_scope():
  """
  :return: current absolute name scope, via tf.name_scope
  :rtype: str

  http://stackoverflow.com/questions/40907769/how-to-get-current-tensorflow-name-scope

  Note that this is a private member and might break at some point.
  Note also that this does not need to be the same as get_current_var_scope_name().
  """
  return tf.get_default_graph()._name_stack or ""


@contextlib.contextmanager
def reuse_name_scope(name, absolute=None, reuse_vars=None):
  """
  Context manager to reuse an already created scope.
  We try to both set the variable scope and the name scope.

  :param str|tf.VariableScope name: relative or absolute name scope (absolute if absolute=True or if tf.VariableScope).
    must not end with "/".
  :param bool absolute: if True it will be absolute
  :param bool reuse_vars: passed on as `reuse` arg for `tf.variable_scope`
  :return: yields the variable_scope
  """
  if isinstance(name, tf.VariableScope):
    name = name.name
    if absolute is not None:
      assert absolute is True
    absolute = True
  assert isinstance(name, str)
  if not absolute:
    assert name
    # First figure out the absolute name scope which we want to reuse/set.
    # The current name scope is more reliable because tf.variable_scope
    # will always also set the name scope.
    current_name_scope = get_current_name_scope()
    if current_name_scope:
      name = current_name_scope + "/" + name
  else:
    current_name_scope = None  # not needed
  assert name[-1:] != "/"
  abs_name = name + "/" if name else ""
  # tf.name_scope with a scope-name ending with "/" will interpret is as absolute name,
  # and use it as-is.
  # In all other cases, it would create a new name-scope with a new unique name,
  # which is not what we want.
  with tf.name_scope(abs_name):
    # tf.name_scope will not set the variable scope.
    # tf.variable_scope will also set the name scope, but the logic is broken
    # for absolute name scopes, thus we had to do the tf.name_scope manually above.
    # We create the dummy_var_scope to force it to reuse that name,
    # and the trailing "/" will work-around the broken tf.variable_scope() usage of tf.name_scope().
    # Afterwards we fix that name again.
    # Note that the reuse-argument might be miss-leading in this context:
    # It means that tf.get_variable() will search for existing variables and errors otherwise.
    var_scope = tf.VariableScope(reuse=reuse_vars, name=abs_name)
    with tf.variable_scope(var_scope) as scope:
      assert isinstance(scope, tf.VariableScope)
      # remove "/" from the end of the var-scope.
      # This is a work-around to fix up the variable scope behavior for nested variable scopes.
      # Warning: This might break at some future point.
      assert scope.name is scope._name
      assert scope.name[-1:] == "/" or scope.name == ""
      scope._name = scope._name[:-1]
      assert name == scope.name, "%r" % current_name_scope
      yield scope


def get_name_scope_of_tensor(x):
  """
  :param tf.Tensor x: has name e.g. "layer0/rec/W:0"
  :return: the name scope of x, e.g. "layer0/rec"
  :rtype: str
  """
  parts = str(x.name).split("/")
  return "/".join(parts[:-1])


def get_base_name(x):
  """
  :param tf.Tensor x: has name e.g. "layer0/rec/W:0"
  :return: return the base name, e.g. "W", without the output index
  """
  parts = str(x.name).split("/")
  return parts[-1].split(":")[0]


@contextlib.contextmanager
def reuse_name_scope_of_tensor(x, prefix="", postfix=""):
  """
  :param tf.Tensor x: has name e.g. "layer0/rec/W:0"
  :param str prefix:
  :param str postfix:
  :return: reuse the name scope of x, e.g. "layer0/rec", yields scope
  """
  with reuse_name_scope(prefix + get_name_scope_of_tensor(x) + postfix, absolute=True) as scope:
    yield scope


@contextlib.contextmanager
def var_creation_scope():
  """
  If you create a variable inside of a while-loop, you might get the following error:
    InvalidArgumentError: The node 'while/w/Assign' has inputs from different frames.
    The input 'while/j' is in frame 'while/while/'. The input 'while/w' is in frame ''.
  Also see tests/test_TFUtil.py:test_loop_var_creation().
  Related TF bugs:
    https://github.com/tensorflow/tensorflow/issues/3114
    https://github.com/tensorflow/tensorflow/issues/4478
    https://github.com/tensorflow/tensorflow/issues/8604
  The solution is to reset the current frame.
  Resetting all control dependencies has this effect.
  """
  with tf.control_dependencies(None) as dep:
    yield dep


class FlipGradientBuilder(object):
  """
  Gradient Reversal Layer.
  Discussion:
    https://github.com/fchollet/keras/issues/3119
    https://github.com/tensorflow/tensorflow/issues/4342
  Code from here:
    https://github.com/pumpikano/tf-dann/blob/master/flip_gradient.py

  Also see :class:`CustomGradient` which is more generic.
  """

  def __init__(self):
    self.num_calls = 0

  def __call__(self, x, l=1.0):
    grad_name = "FlipGradient%d" % self.num_calls

    from tensorflow.python.framework import ops
    @ops.RegisterGradient(grad_name)
    def _flip_gradients(op, grad):
      return [tf.negative(grad) * l]

    g = tf.get_default_graph()
    with g.gradient_override_map({"Identity": grad_name}):
      y = tf.identity(x, "flip_gradient_identity")

    self.num_calls += 1
    return y

flip_gradient = FlipGradientBuilder()


def lookup_grad_func_by_name(op_type):
  """
  :param str op_type:
  :return: function grad_func(op, grad), or raises LookupError
  """
  from tensorflow.python.framework import ops
  # Also see ops.RegisterGradient and ops.get_gradient_function.
  return ops._gradient_registry.lookup(op_type)


def identity_with_check_numerics(x, with_grad=True, name="identity_with_check_numerics"):
  """
  Returns identity(x), but with additional check_numerics control dependency,
  and optionally the same for its gradient.
  See also :func:`TFUpdater.add_check_numerics_ops`, which will add checks for the whole graph.

  :param tf.Tensor x:
  :param bool with_grad: whether the check will also be added for the gradient
  :param str name:
  :rtype: tf.Tensor
  """
  with tf.name_scope(name):
    with tf.control_dependencies([tf.check_numerics(x, message="%s %s" % (x.name, name))]):
      if with_grad:
        # An alternative to gradient_override_map would be :class:`CustomGradient` which is more generic.
        grad_name = "identity_with_check_numerics_grad"
        try:
          lookup_grad_func_by_name(grad_name)
        except LookupError:
          from tensorflow.python.framework import ops
          @ops.RegisterGradient(grad_name)
          def _identity_with_check_numerics_grad(op, grad):
            return identity_with_check_numerics(grad, with_grad=True, name="%s_grad" % name)

        g = tf.get_default_graph()
        with g.gradient_override_map({"Identity": grad_name}):
          y = tf.identity(x)

      else:
        y = tf.identity(x)

      return y


def check_input_ndim(x, ndim):
  """
  :param tf.Tensor x:
  :param int ndim:
  :return: x with check added
  :rtype: tf.Tensor
  """
  dyn_shape = x.get_shape()
  if dyn_shape.ndims is not None:
    assert dyn_shape.ndims == ndim
    return x
  # Need to fall-back to runtime check.
  with tf.name_scope("check_input_ndim"):
    with tf.control_dependencies(
      [tf.assert_equal(tf.rank(x), ndim, data=["ndim not %i" % ndim, tf.shape(x)])]):
      return tf.identity(x, "identity_with_ndim_check")


def check_input_ndim_equal_offset(x, y, y_ndim_offset=0):
  """
  :param tf.Tensor x:
  :param tf.Tensor y:
  :param int y_ndim_offset:
  :return: x with check added such that ndim(x) == ndim(y) + y_ndim_offset
  :rtype: tf.Tensor
  """
  x_dyn_shape = x.get_shape()
  y_dyn_shape = y.get_shape()
  if x_dyn_shape.ndims is not None and y_dyn_shape.ndims is not None:
    assert x_dyn_shape.ndims == y_dyn_shape.ndims + y_ndim_offset
    return x
  # Need to fall-back to runtime check.
  with tf.name_scope("check_input_ndim_equal_offset"):
    with tf.control_dependencies(
      [tf.assert_equal(tf.rank(x), tf.rank(y) + y_ndim_offset,
                       data=["ndim not equal with offset %i" % y_ndim_offset,
                             tf.shape(x), tf.shape(y)])]):
      return tf.identity(x, "identity_with_ndim_equal_check")


def check_input_dim(x, axis, dim):
  """
  :param tf.Tensor x:
  :param int axis: which axis to check
  :param int|tf.Tensor dim:
  :return: x with check added
  :rtype: tf.Tensor
  """
  dyn_shape = x.get_shape()
  if dyn_shape.ndims is not None and isinstance(dim, int):
    if dyn_shape.dims[axis].value is not None:
      assert dyn_shape.dims[axis].value == dim
      return x
  # Need to fall-back to runtime check.
  with tf.name_scope("check_input_dim"):
    with tf.control_dependencies(
      [tf.assert_equal(tf.shape(x)[axis], dim, data=["shape[%i]:" % (axis,), tf.shape(x), "!=", "dim:", dim])]):
      return tf.identity(x, "identity_with_dim_check")


def check_dim_equal(x, x_axis, y, y_axis, extra_msg=()):
  """
  :param tf.Tensor x:
  :param int x_axis: which axis to check
  :param tf.Tensor y:
  :param int y_axis: which axis to check
  :param list[str]|tuple[str] extra_msg: will be printed additionally if it fails
  :return: x with check added that shape(x)[x_axis] == shape(y)[y_axis]
  :rtype: tf.Tensor
  """
  x_dyn_shape = x.get_shape()
  y_dyn_shape = y.get_shape()
  if x_dyn_shape.ndims is not None and y_dyn_shape.ndims is not None:
    if x_dyn_shape.dims[x_axis].value is not None and y_dyn_shape.dims[y_axis].value is not None:
      assert x_dyn_shape.dims[x_axis].value == y_dyn_shape.dims[y_axis].value, extra_msg
      return x
  # Need to fall-back to runtime check.
  with tf.name_scope("check_dim_equal"):
    shape_x = tf.shape(x)
    shape_y = tf.shape(y)
    with tf.control_dependencies(
      [tf.assert_equal(
         shape_x[x_axis], shape_y[y_axis],
         data=["x.shape[%i] != y.shape[%i]" % (x_axis, y_axis), shape_x, shape_y] + list(extra_msg))]):
      return tf.identity(x, "identity_with_dim_equal_check")


def check_shape_equal(x, y):
  """
  :param tf.Tensor x:
  :param tf.Tensor y:
  :return: x with check added that shape(x) == shape(y)
  :rtype: tf.Tensor
  """
  x_dyn_shape = x.get_shape()
  y_dyn_shape = y.get_shape()
  if x_dyn_shape.ndims is not None and y_dyn_shape.ndims is not None:
    assert x_dyn_shape.ndims == y_dyn_shape.ndims
    have_unknown = False
    for axis in range(x_dyn_shape.ndims):
      if x_dyn_shape.dims[axis].value is not None and y_dyn_shape.dims[axis].value is not None:
        assert x_dyn_shape.dims[axis].value == y_dyn_shape.dims[axis].value
      else:
        have_unknown = True
    if not have_unknown:
      return x  # all dims are checked, we can return
  # We need to fall-back to runtime check.
  with tf.name_scope("check_shape_equal"):
    with tf.control_dependencies(
      [tf.assert_equal(
        tf.shape(x), tf.shape(y),
        data=["x.shape not y.shape",
              tf.shape(x), tf.shape(y)])]):
      return tf.identity(x, "identity_with_shape_equal_check")


def get_shape_dim(x, axis, name="shape_dim"):
  """
  :param tf.Tensor x:
  :param int axis: which axis
  :param str name:
  :return: x.shape[axis] either as a static int or otherwise as an expression
  :rtype: int|tf.Tensor
  """
  dyn_shape = x.get_shape()
  if dyn_shape.ndims is not None:
    if dyn_shape.dims[axis].value is not None:
      return dyn_shape.dims[axis].value
  # Need to fall-back to runtime.
  with tf.name_scope(name):
    return tf.shape(x)[axis]


def get_shape(x):
  """
  :param tf.Tensor x:
  :return: list of scalars, which are either int if known statically, or otherwise expressions
  :rtype: list[int|tf.Tensor]
  """
  with tf.name_scope("get_shape"):
    dyn_shape = tf.shape(x)
    static_shape = x.get_shape()
    assert static_shape.ndims is not None
    return [static_shape.dims[i].value
            if static_shape.dims[i].value is not None
            else dyn_shape[i]
            for i in range(static_shape.ndims)]


def get_ndim(x):
  """
  :param tf.Tensor x:
  :return: x.ndim either as a static int or otherwise as an expression
  :rtype: int|tf.Tensor
  """
  dyn_shape = x.get_shape()
  if dyn_shape.ndims is not None:
    return dyn_shape.ndims
  # Need to fall-back to runtime.
  return tf.rank(x)


def get_range(start, stop=NotSpecified):
  """
  :param int|tf.Tensor|None start:
  :param int|tf.Tensor|None stop:
  :return: either tuple(range(start, stop)) or the same as a symbolic expression
  :rtype: tuple[int]|tf.Tensor
  """
  if stop is NotSpecified:
    stop = start
    start = 0
  if isinstance(start, tf.Tensor) or isinstance(stop, tf.Tensor):
    return tf.range(start, stop)
  return tuple(range(start, stop))


def identity_with_ops(x, ops):
  """
  :param tf.Tensor x:
  :param () -> list[tf.Operation|tf.Tensor] ops:
  :return: x with all ops executed
  :rtype: tf.Tensor
  """
  with tf.name_scope("identity_with_ops"):
    with tf.control_dependencies(ops()):
      return tf.identity(x, name="identity_with_ops")


def _guess_requested_max_num_threads(log_file=None):
  from Util import read_sge_num_procs
  try:
    sge_num_procs = read_sge_num_procs()
  except Exception as exc:
    if log_file:
      print("Error while getting SGE num_proc: %r" % exc, file=log_file)
  else:
    if sge_num_procs:
      if log_file:
        print("Use num_threads=%i (but min 2) via SGE num_proc." % sge_num_procs, file=log_file)
      return max(sge_num_procs, 2)
  omp_num_threads = int(os.environ.get("OMP_NUM_THREADS") or 0)
  if omp_num_threads:
    # Minimum of 2 threads, should not hurt.
    if log_file:
      print("Use num_threads=%i (but min 2) via OMP_NUM_THREADS." % omp_num_threads, file=log_file)
    return max(omp_num_threads, 2)
  return None


_setup_tf_thread_pools_called_once = False

def setup_tf_thread_pools(num_threads=None, log_file=None):
  """
  See here for documentation of intra_op_parallelism_threads and inter_op_parallelism_threads:
  https://github.com/tensorflow/tensorflow/blob/master/tensorflow/core/protobuf/config.proto

  intra_op_parallelism_threads is used for the LocalDevice::EigenThreadPoolInfo, which is always global.
  https://github.com/tensorflow/tensorflow/blob/master/tensorflow/core/common_runtime/local_device.cc

  inter_op_parallelism_threads is used for the (global if not use_per_session_threads) session thread pool.
  https://github.com/tensorflow/tensorflow/blob/master/tensorflow/core/common_runtime/direct_session.cc

  TF will setup the thread pools on first usage. That can happen quite early, esp for intra_op_parallelism_threads.
  E.g. list_local_devices() will trigger this, i.e. any call to is_gpu_available() or print_available_devices().
  For debugging, you can set the env-var TF_CPP_MIN_VLOG_LEVEL=1 and then check for these message::

      Local device intra op parallelism threads: 4
      Direct session inter op parallelism threads: 4

  Thus, call this function as early as possible with your preferred number of threads,
  used for both thread pools.
  It will create a dummy session and directly close it again, but if you use the global thread pools,
  those settings will remain for further sessions.
  This function will only execute on the first call.

  :param int num_threads: used for both intra and inter parallelism thread pools
  :param stream|None log_file:
  """
  global _setup_tf_thread_pools_called_once
  if _setup_tf_thread_pools_called_once:
    return
  _setup_tf_thread_pools_called_once = True
  if not num_threads:
    num_threads = _guess_requested_max_num_threads(log_file=log_file)
  if log_file:
    print("Setup TF inter and intra global thread pools, num_threads=%r." % num_threads, file=log_file)
  opts = {}
  opts.setdefault("log_device_placement", False)
  opts.setdefault("device_count", {}).setdefault("GPU", 0)
  if num_threads:
    opts.setdefault("intra_op_parallelism_threads", num_threads)
    opts.setdefault("inter_op_parallelism_threads", num_threads)
  with tf.Session(config=tf.ConfigProto(**opts)) as session:
    session.close()


def check_initial_tf_thread_pool_init():
  if not _setup_tf_thread_pools_called_once:
    from Util import try_get_caller_name
    print("setup_tf_thread_pools() not yet called (via func %s), calling it now." %
          try_get_caller_name(fallback="<unknown>"))
    setup_tf_thread_pools(log_file=sys.stdout)


_list_local_devices = None

def get_tf_list_local_devices():
  """
  This uses tensorflow.device_lib.list_local_devices().
  Note that a call to this will trigger the internal TF thread pool inits,
  so you should call :func:`setup_tf_thread_pools` first.

  :rtype: list[tensorflow.core.framework.device_attributes_pb2.DeviceAttributes]
  """
  check_initial_tf_thread_pool_init()
  global _list_local_devices
  if _list_local_devices:
    return _list_local_devices
  print("Collecting TensorFlow device list...")
  _list_local_devices = list(device_lib.list_local_devices())
  return _list_local_devices


def _parse_physical_device_desc(s):
  """
  :param str s: string via dev.physical_device_desc. e.g. "device: 0, name: GeForce GTX 980, pci bus id: 0000:41:00.0"
  :return: dict key -> value
  :rtype: dict[str,str]
  """
  d = {}
  for part in s.split(","):
    part = part.strip()
    key, value = part.split(":", 1)
    key, value = key.strip(), value.strip()
    d[key] = value
  return d


def print_available_devices():
  """
  Prints the available TF devices on stdout.
  This uses tensorflow.device_lib.list_local_devices().
  Note that a call to this will trigger the internal TF thread pool inits,
  so you should call :func:`setup_tf_thread_pools` first.
  """
  cuda_visible_devs = None
  if "CUDA_VISIBLE_DEVICES" in os.environ:
    print("CUDA_VISIBLE_DEVICES is set to %r." % os.environ["CUDA_VISIBLE_DEVICES"])
    cuda_visible_devs = dict(enumerate([int(d) for d in os.environ["CUDA_VISIBLE_DEVICES"].split(",") if d]))
  else:
    print("CUDA_VISIBLE_DEVICES is not set.")
  devs = get_tf_list_local_devices()
  print("Local devices available to TensorFlow:")
  for i, dev in enumerate(devs):
    print("  %i/%i: %s" % (i + 1, len(devs), "\n       ".join(str(dev).splitlines())))

  # Theano prints sth like: Using gpu device 2: GeForce GTX 980 (...)
  # Print in a similar format so that some scripts which grep our stdout work just as before.
  for dev in devs:
    if dev.device_type == "GPU":
      d = _parse_physical_device_desc(dev.physical_device_desc)
      dev_id = int(d["device"])
      if cuda_visible_devs:
        dev_id = cuda_visible_devs[dev_id]
      dev_name = d["name"]
      print("Using gpu device %i: %s" % (dev_id, dev_name))


def is_gpu_available():
  """
  Returns whether TensorFlow can access a GPU.
  This uses tensorflow.device_lib.list_local_devices().
  Note that a call to this will trigger the internal TF thread pool inits,
  so you should call :func:`setup_tf_thread_pools` first.
  """
  return any(x.device_type == 'GPU' for x in get_tf_list_local_devices())


def dot(a, b):
  """
  :param tf.Tensor a: shape [...da...,d]
  :param tf.Tensor b: shape [d,...db...]
  :return: tensor of shape [...da...,d,...db...]
  :rtype: tf.Tensor
  """
  with tf.name_scope("dot"):
    a_ndim = a.get_shape().ndims
    b_ndim = b.get_shape().ndims
    assert a_ndim is not None
    if a_ndim == 0:
      return tf.scalar_mul(a, b)
    assert b_ndim is not None
    if b_ndim == 0:
      return tf.scalar_mul(b, a)
    a = check_dim_equal(a, -1, b, 0)
    if a_ndim == b_ndim == 1:
      return tf.reduce_sum(a * b)
    d = get_shape_dim(b, 0)
    assert a_ndim >= 2 and b_ndim >= 2
    res_shape = None
    if a_ndim > 2 or b_ndim > 2:
      res_shape = [get_shape_dim(a, i) for i in range(0, a_ndim - 1)] + [get_shape_dim(b, i) for i in range(1, b_ndim)]
    if a_ndim > 2:
      a = tf.reshape(a, (-1, d))
    if b_ndim > 2:
      b = tf.reshape(b, (d, -1))
    res = tf.matmul(a, b)
    if a_ndim > 2 or b_ndim > 2:
      res = tf.reshape(res, res_shape)
    return res


def identity(x):
  """
  :param tf.Tensor x:
  :rtype: tf.Tensor
  """
  return x


def _plus(a, b):
  return a + b


def _minus(a, b):
  return a - b


def _mul(a, b):
  return a * b


def _div(a, b):
  return a / b


_bin_ops = {"+": _plus, "-": _minus, "*": _mul, "/": _div}
_act_func_with_op_cache = {}  # type: dict[str,(tf.Tensor)->tf.Tensor]


def _get_act_func_with_op(s):
  """
  :param str s: e.g. "2 * sigmoid" or even "3 + 2 * sigmoid"
  :rtype: (tf.Tensor) -> tf.Tensor
  """
  if s in _act_func_with_op_cache:
    return _act_func_with_op_cache[s]
  def _conv(v):
    v = v.strip()
    from Util import str_is_number
    if str_is_number(v):
      try:
        v = int(v)
      except ValueError:
        v = float(v)
      return lambda x: v
    else:
      return get_activation_function(v)
  a, b = None, None
  for k in "+-*/":
    if k in s:
      a, b = s.split(k, 2)
      a, b = _conv(a), _conv(b)
      def combined_op(x):
        return _bin_ops[k](a(x), b(x))
      _act_func_with_op_cache[s] = combined_op
      return combined_op
  assert False


def get_activation_function(s):
  """
  :param str|None s:
  :rtype: (tf.Tensor) -> tf.Tensor
  """
  if not s or s in ["none", "identity"]:
    return identity
  if any(k in s for k in _bin_ops):
    return _get_act_func_with_op(s)
  if hasattr(tf.nn, s):
    return getattr(tf.nn, s)  # e.g. relu, elu, sigmoid, softmax, ...
  elif hasattr(tf, s):
    return getattr(tf, s)  # e.g. log, abs
  elif s in globals():
    return globals()[s]  # e.g. safe_log
  raise Exception("invalid activation function: %r" % s)


def random_uniform_abs_initializer(limit, **kwargs):
  return tf.random_uniform_initializer(minval=-limit, maxval=limit, **kwargs)


def xavier_initializer(uniform=True, seed=None, dtype=tf.float32):
  """
  Alias for tf.glorot_uniform_initializer or tf.glorot_normal_initializer.

  :param bool uniform: uniform or normal distribution
  :param int seed:
  :param tf.DType dtype:
  :return: ((tuple[int]) -> tf.Tensor) | tf.Initializer
  """
  from tensorflow.python.ops import init_ops
  return init_ops.variance_scaling_initializer(
    scale=1.0, mode='fan_avg', distribution="uniform" if uniform else "normal", seed=seed, dtype=dtype)


def get_initializer(s, seed=None, eval_local_ns=None):
  """
  :param str|dict[str]|float s: e.g. "glorot_uniform" or "truncated_normal" or "orthogonal", or config dict with "class",
    or string to be `eval`ed if it contains "(". constant if a float is given.
  :param int|tf.Tensor seed:
  :param dict[str]|None eval_local_ns:
  :return: (function (shape) -> tf.Tensor) | tf.Initializer
  :rtype: ((tuple[int]) -> tf.Tensor) | tf.Initializer
  """
  if isinstance(s, (float, int)):
    return tf.constant_initializer(s, dtype=tf.float32)
  import numpy
  import math
  from tensorflow.python.ops import init_ops
  ns = dict(globals())
  ns.update(vars(tf))
  ns.update(vars(init_ops))
  ns.update(vars(math))
  ns["numpy"] = numpy
  for k in sorted(list(ns.keys())):
    if k.endswith("_initializer"):
      k_short = k[:-len("_initializer")]
      if k_short not in ns:
        ns[k_short] = ns[k]
  f = None
  if isinstance(s, str):
    if "(" in s:
      f = eval(s, ns, eval_local_ns)
    elif s + "_initializer" in ns:
      f = ns[s + "_initializer"]()
    elif s in ns:
      f = ns[s]()
  elif isinstance(s, dict):
    s = s.copy()
    class_name = s.pop("class")
    class_ = ns[class_name]
    assert issubclass(class_, init_ops.Initializer)
    f = class_.from_config(s)
  else:
    raise ValueError("invalid get_initializer argument, expected string or dict, got: %r" % s)
  if isinstance(f, (float, int)):
    return tf.constant_initializer(f, dtype=tf.float32)
  if not f:
    raise Exception("invalid initializer: %r" % s)
  if seed is not None:
    assert isinstance(f, init_ops.Initializer)
    if hasattr(f, "seed"):
      f.seed = seed
  return f


def dropout(x, keep_prob, noise_shape=None, seed=None, name=None):
  """
  Computes dropout.
  Like :func:`tf.nn.dropout` but avoid :func:`tf.div` if possible.

  :param tf.Tensor x:
  :param float|tf.Tensor keep_prop:
  :param tf.Tensor|tuple[int] noise_shape:
  :param int seed:
  :param str name:
  """
  with tf.name_scope(name, "dropout", [x]):
    x = tf.convert_to_tensor(x, name="x")
    assert isinstance(x, tf.Tensor)
    if isinstance(keep_prob, (float, int)) and not 0 < keep_prob <= 1:
      raise ValueError("keep_prob must be a scalar tensor or a float in the "
                       "range (0, 1], got %g" % keep_prob)
    # Do nothing if we know keep_prob == 1
    if isinstance(keep_prob, (float, int)) and keep_prob == 1:
      return x
    inv_keep_prob = 1.0 / keep_prob

    noise_shape = noise_shape if noise_shape is not None else tf.shape(x)
    # uniform [keep_prob, 1.0 + keep_prob)
    random_tensor = keep_prob
    random_tensor += tf.random_uniform(noise_shape, seed=seed, dtype=x.dtype)
    # 0. if [keep_prob, 1.0) and 1. if [1.0, 1.0 + keep_prob)
    binary_tensor = tf.floor(random_tensor)
    ret = x * (binary_tensor * inv_keep_prob)
    assert isinstance(ret, tf.Tensor)
    ret.set_shape(x.get_shape())
    return ret


def swapaxes(x, axis1, axis2):
  """
  :param tf.Tensor x:
  :param tf.Tensor|int axis1:
  :param tf.Tensor|int axis2:
  :return: tensor with swapped axes, like numpy.swapaxes
  :rtype: tf.Tensor
  """
  with tf.name_scope("swapaxes"):
    ndim = x.get_shape().ndims
    if ndim is not None:
      if isinstance(axis1, tf.Tensor) or isinstance(axis2, tf.Tensor):
        perm = [tf.where(tf.equal(axis1, i), axis2,
                         tf.where(tf.equal(axis2, i), axis1,
                                  i))
                for i in range(ndim)]
      else:
        perm = list(range(ndim))
        perm[axis1] = axis2
        perm[axis2] = axis1
    else:
      # Just fall back to the very generic pure symbolic variant.
      rank = tf.rank(x)
      all_axes = tf.range(rank)
      assert all_axes.get_shape().ndims == 1
      axis1 = tf.convert_to_tensor(axis1)
      axis2 = tf.convert_to_tensor(axis2)
      assert axis1.get_shape().ndims == 0
      assert axis2.get_shape().ndims == 0
      axis1_bc = tf.expand_dims(axis1, 0)
      axis2_bc = tf.expand_dims(axis2, 0)
      perm = tf.where(tf.equal(axis1_bc, all_axes), axis2_bc,
                      tf.where(tf.equal(axis2_bc, all_axes), axis1_bc,
                               all_axes))
    return tf.transpose(x, perm=perm)


def move_axis(x, old_axis, new_axis, name="move_axis"):
  """
  :param tf.Tensor x:
  :param int old_axis: can also be negative
  :param int new_axis: can also be negative
  :param str name: name of the scope
  """
  with tf.name_scope(name):
    ndim = x.get_shape().ndims
    assert ndim is not None, "not supported currently: %r" % x
    if old_axis < 0:
      old_axis += ndim
      assert old_axis >= 0
    if new_axis < 0:
      new_axis += ndim
      assert new_axis >= 0
    if old_axis == new_axis:
      return x
    perm = list(range(ndim))
    old = perm.pop(old_axis)
    perm.insert(new_axis, old)
    return tf.transpose(x, perm)


def sequence_mask(lengths, **kwargs):
  """
  Wraps around tf.sequence_mask().
  It will cache the value inside the passed object so that we don't recompute it multiple times.

  :param tf.Tensor lengths: shape (batch,)
  :param kwargs: passed on to tf.sequence_mask
  :return: tensor mask of shape (batch,maxlen/time). default dtype is bool unless you specify something else
  :rtype: tf.Tensor
  """
  if hasattr(lengths, "_sequence_mask"):
    return lengths._sequence_mask
  with reuse_name_scope_of_tensor(lengths):
    mask = tf.sequence_mask(lengths, **kwargs)
  lengths._sequence_mask = mask
  return mask


def sequence_mask_time_major(lengths, **kwargs):
  """
  Wraps around tf.transpose(tf.sequence_mask(), (1,0)).
  It will cache the value inside the passed object so that we don't recompute it multiple times.

  :param tf.Tensor lengths: shape (batch,)
  :param kwargs: passed on to tf.sequence_mask
  :return: mask of shape (maxlen/time,batch)
  :rtype: tf.Tensor
  """
  if hasattr(lengths, "_sequence_mask_time_major"):
    return lengths._sequence_mask_time_major
  mask = sequence_mask(lengths=lengths, **kwargs)  # shape (time,batch)
  with reuse_name_scope_of_tensor(lengths):
    with tf.name_scope("sequence_mask_time_major"):
      mask = tf.transpose(mask, (1, 0))  # shape (batch,time)
  lengths._sequence_mask_time_major = mask
  return mask


def directed(x, direction):
  """
  If direction == 1 or direction is None, returns just x.
  If direction == -1, returns reversed(x).

  :param tf.Tensor x:
  :param int|None direction: -1 or 1 (or None)
  :rtype: tf.Tensor
  """
  if direction == 1 or direction is None:
    return x
  if direction == -1:
    return reversed(x)
  raise ValueError("invalid direction: %r" % direction)


def reversed(x):
  """
  Just returns x[::-1].
  It will cache the value inside the passed object so that we don't recompute it multiple times.

  :param tf.Tensor x:
  :rtype: tf.Tensor
  """
  if hasattr(x, "_reversed_dim0"):
    return x._reversed_dim0
  with reuse_name_scope_of_tensor(x):
    with tf.name_scope("reversed"):
      y = x[::-1]
  x._reversed_dim0 = y
  y._reversed_dim0 = x
  return y


def flatten_with_seq_len_mask(x, seq_lens, time_major=False):
  """
  :param tf.Tensor x: shape (batch,time,...s...) with time_major=False or otherwise shape (time,batch,...s....)
  :param tf.Tensor seq_lens: shape (batch,) of int32
  :param bool time_major: if the time-dim is the first dimension in x
  :return: tensor of shape (time', ...s...) where time' = sum(seq_len) <= batch*time
  :rtype: tf.Tensor
  """
  with tf.name_scope("flatten_with_seq_len_mask"):
    seq_lens = check_input_ndim(seq_lens, 1)
    if time_major:
      x = swapaxes(x, 0, 1)  # get (batch,time,...s...)
    x = check_dim_equal(x, 0, seq_lens, 0, ["batch-dim does not match"])  # batch dim
    # int64? -> https://github.com/tensorflow/tensorflow/issues/6518
    mask = sequence_mask(seq_lens, maxlen=tf.shape(x)[1])  # shape (batch,time)
    mask = check_input_ndim(mask, 2)
    mask = check_dim_equal(mask, 0, x, 0)
    mask = check_dim_equal(mask, 1, x, 1)
    res = tf.boolean_mask(x, mask)
    res = check_input_ndim_equal_offset(res, x, -1)
    return res


def expand_dims_unbroadcast(x, axis, dim, name="expand_dims_unbroadcast"):
  """
  :param tf.Tensor|float|int x:
  :param int|tf.Tensor axis: new axis
  :param int|tf.Tensor dim: dimension for axis
  :param str name: scope name
  :return: if x is of shape (a,b,c) and axis=0, then we return (dim,a,b,c)
  :rtype: tf.Tensor
  """
  with tf.name_scope(name):
    x = tf.convert_to_tensor(x)
    x = tf.expand_dims(x, axis)
    if dim is not 1:
      new_ndim = x.get_shape().ndims
      assert new_ndim is not None, "not implemented otherwise yet"
      assert isinstance(axis, int), "not implemented otherwise yet"
      x = tf.tile(x, [dim if (axis == i) else 1 for i in range(new_ndim)])
    return x


def expand_multiple_dims(x, axes, name="expand_multiple_dims"):
  """
  :param tf.Tensor x:
  :param list[int]|tuple[int] axes: after completion, tf.shape(y)[axis] == 1 for axis in axes
  :param str name: scope name
  :return: y where we have a new broadcast axis for each axis in axes
  :rtype: tf.Tensor
  """
  with tf.name_scope(name):
    for i in sorted(axes):
      x = tf.expand_dims(x, axis=i, name="expand_axis_%i" % i)
    return x


def tile_transposed(x, axis, multiples):
  """
  Example: x with shape (D,), tf.tile(x, [N]) can be reshaped into (N,D),
  while tile_transposed(x, axis=0, multiples=N) can be reshaped into (D,N).

  :param tf.Tensor x:
  :param int axis:
  :param int|tf.Tensor multiples:
  :return: tensor with shape[axis] == x.shape[axis] * multiples
  :rtype: tf.Tensor
  """
  with tf.name_scope("tile_transposed"):
    ndim = x.get_shape().ndims
    assert ndim is not None
    shape = tf.shape(x)
    x = expand_dims_unbroadcast(x, axis=axis + 1, dim=multiples)  # new axis after `axis`
    return tf.reshape(
      x,
      [shape[i] for i in range(axis)] +
      [shape[axis] * multiples] +
      [shape[i] for i in range(axis + 1, ndim)])


def constant_with_shape(x, shape, dtype=None, name="constant_with_shape"):
  """
  :param tf.Tensor|float|int|bool x: scalar
  :param list[tf.Tensor|int]|tuple[tf.Tensor|int]|tf.Tensor shape:
  :param tf.DType dtype:
  :param str name:
  :return: x of the specified shape
  :rtype: tf.Tensor
  """
  with tf.name_scope(name):
    x = tf.convert_to_tensor(x, dtype=dtype)
    ones = tf.ones(shape, dtype=x.dtype)
    if x.dtype == tf.bool:
      return tf.logical_and(x, ones)
    return tf.multiply(x, ones)


def dimshuffle(x, axes, name="dimshuffle"):
  """
  Like Theanos dimshuffle.
  Combines tf.transpose, tf.expand_dims and tf.squeeze.

  :param tf.Tensor x:
  :param list[int|str]|tuple[int|str] axes:
  :param str name: scope name
  :rtype: tf.Tensor
  """
  with tf.name_scope(name):
    assert all([i == "x" or isinstance(i, int) for i in axes])
    real_axes = [i for i in axes if isinstance(i, int)]
    bc_axes = [i for (i, j) in enumerate(axes) if j == "x"]
    if x.get_shape().ndims is None:
      x_shape = tf.shape(x)
      x = tf.reshape(x, [x_shape[i] for i in range(max(real_axes) + 1)])  # will have static ndims
    assert x.get_shape().ndims is not None

    # First squeeze missing axes.
    i = 0
    while i < x.get_shape().ndims:
      if i not in real_axes:
        x = tf.squeeze(x, axis=i)
        real_axes = [(j if (j < i) else (j - 1)) for j in real_axes]
      else:
        i += 1

    # Now permute.
    assert list(sorted(real_axes)) == list(range(x.get_shape().ndims))
    if real_axes != list(range(x.get_shape().ndims)):
      x = tf.transpose(x, real_axes)

    # Now add broadcast dimensions.
    if bc_axes:
      x = expand_multiple_dims(x, bc_axes)
    assert len(axes) == x.get_shape().ndims
    return x


def sparse_labels_with_seq_lens(x, seq_lens, dtype=tf.int32, collapse_repeated=False):
  """
  :param tf.Tensor x: shape (batch,time) -> index, some int type
  :param tf.Tensor|None seq_lens: shape (batch,) of int32|int64
  :param tf.DType|None dtype: if given, will cast the `x` values to this type. ctc_loss() wants int32
  :param bool collapse_repeated: like uniq() behavior
  :return: SparseTensor, e.g. input for tf.nn.ctc_loss(), and seq_lens of shape (batch,)
  :rtype: (tf.SparseTensor, tf.Tensor)
  """
  with tf.name_scope("sparse_labels"):
    x = check_input_ndim(x, ndim=2)
    if seq_lens is not None:
      x = check_dim_equal(x, 0, seq_lens, 0)
    if dtype:
      x = tf.cast(x, dtype)
    batch_size = tf.shape(x)[0]
    max_time = tf.shape(x)[1]
    if seq_lens is not None:
      mask = sequence_mask(seq_lens, maxlen=max_time)  # shape (batch,time)
    else:
      mask = tf.ones(dtype=tf.bool, shape=(batch_size, max_time))
    if collapse_repeated:
      with tf.name_scope("collapse_repeated"):
        diffs = tf.concat(
          1, [tf.ones_like(x[:, :1], dtype=tf.bool), tf.not_equal(x[:, 1:], x[:, :-1])])  # shape (batch,time)
        mask = tf.logical_and(diffs, mask)
    with tf.name_scope("flat_x"):
      flat_x = tf.boolean_mask(x, mask)  # (N, ...s...)
    with tf.name_scope("idxs"):
      if collapse_repeated:
        # Recalculate mask, so that we have them all behind each other.
        seq_lens = tf.reduce_sum(tf.cast(mask, tf.int32), axis=1)
        max_time = tf.reduce_max(seq_lens)
        mask = sequence_mask(seq_lens)
      time_idxs = expand_dims_unbroadcast(tf.range(max_time), 0, batch_size)  # shape (batch,time)
      flat_time_idxs = tf.boolean_mask(time_idxs, mask)  # (N,)
      batch_idxs = expand_dims_unbroadcast(tf.range(batch_size), 1, max_time)  # shape (batch,time)
      flat_batch_idxs = tf.boolean_mask(batch_idxs, mask)  # (N,)
      flat_idxs = tf.stack([flat_batch_idxs, flat_time_idxs], axis=1)  # shape (N, 2)
      # tf.SparseTensor requires int64 indices
      flat_idxs = tf.cast(flat_idxs, tf.int64)
    with tf.name_scope("shape"):
      shape = [batch_size, max_time]
      # tf.SparseTensor requires int64 shape
      shape = [tf.cast(d, tf.int64) for d in shape]
      shape = tf.convert_to_tensor(shape)
    # tf.SparseTensor args:
    #   indices: A 2-D int64 tensor of shape `[N, ndims]`.
    #   values: A 1-D tensor of any type and shape `[N]`.
    #   shape: A 1-D int64 tensor of shape `[ndims]`.
    return tf.SparseTensor(flat_idxs, flat_x, shape), seq_lens


def sparse_labels(x, seq_lens, dtype=tf.int32, collapse_repeated=False):
  """
  :param tf.Tensor x: shape (batch,time) -> index, some int type
  :param tf.Tensor|None seq_lens: shape (batch,) of int32|int64
  :param tf.DType|None dtype: if given, will cast the `x` values to this type. ctc_loss() wants int32
  :param bool collapse_repeated: like uniq() behavior
  :return: SparseTensor, e.g. input for tf.nn.ctc_loss()
  :rtype: tf.SparseTensor
  """
  y, _ = sparse_labels_with_seq_lens(x=x, seq_lens=seq_lens, dtype=dtype, collapse_repeated=collapse_repeated)
  return y


def uniq(x):
  """
  :param tf.Tensor x: 1D shape (time,) -> index, some int type
  :return: like numpy.uniq. unlike tf.unique which will never repeat entries.
  Example: uniq([0, 0, 1, 1, 0, 0]) == [0, 1, 0], tf.unique([0, 0, 1, 1, 0, 0]) == [0, 1].
  For a batched variant, see batched_uniq, or sparse_labels() with option collapse_repeated.
  """
  diffs = tf.concat(0, [tf.ones_like(x[:1]), x[1:] - x[:-1]])
  nonzero_idx = tf.where(diffs)
  x_uniq = tf.gather_nd(x, nonzero_idx)
  return x_uniq


def batched_uniq(x, seq_lens):
  """
  :param tf.Tensor x: shape (batch,time) -> index, some int type
  :param tf.Tensor|None seq_lens: shape (batch,) of int32|int64
  :return: tuple (z, new_seq_lens), where z is of shape (batch, max_new_time),
    max_new_time = max(new_seq_lens), seq_lens is of shape (batch,).
  :rtype: (tf.Tensor, tf.Tensor)
  """
  y, new_seq_lens = sparse_labels_with_seq_lens(x, seq_lens=seq_lens, collapse_repeated=True)
  z = tf.sparse_to_dense(sparse_indices=y.indices, sparse_values=y.values, output_shape=y.dense_shape)
  return z, new_seq_lens


class VariableAssigner(object):
  def __init__(self, var):
    """
    :param tf.Variable var:
    """
    self.var = var
    assert isinstance(self.var.initializer, tf.Operation)
    assert self.var.initializer.type in ["Assign", "AssignVariableOp"]
    self.assign_op = self.var.initializer

  def assign(self, value, session):
    """
    :param numpy.ndarray|int|float value:
    :param tf.Session session:
    """
    session.run(self.assign_op, feed_dict={self.assign_op.inputs[1]: value})


class CudaEnv(object):
  _instance = None
  verbose_find_cuda = False

  def __init__(self):
    self.cuda_path = self._find_cuda_path()
    if self.verbose_find_cuda:
      print("CUDA path:", self.cuda_path)

  @classmethod
  def _find_nvcc_in_path(cls):
    """
    :return: yields full path to nvcc
    :rtype: list[str]
    """
    for p in os.environ["PATH"].split(":"):
      pp = "%s/nvcc" % p
      if os.path.exists(pp):
        yield pp

  @classmethod
  def _find_lib_in_ld_path(cls):
    """
    :return: yields full path to libcudart.so
    :rtype: list[str]
    """
    if not os.environ.get("LD_LIBRARY_PATH"):
      return
    for p in os.environ["LD_LIBRARY_PATH"].split(":"):
      pp = "%s/libcudart.so" % p
      if os.path.exists(pp):
        yield pp

  @classmethod
  def _get_lib_dir_name(cls):
    from Util import is_64bit_platform
    if is_64bit_platform():
      return "lib64"
    return "lib"

  @classmethod
  def _cuda_path_candidates(cls):
    for p in cls._find_nvcc_in_path():
      # Expect p == "/usr/local/cuda-8.0/bin/nvcc" or so.
      postfix = "/bin/nvcc"
      if cls.verbose_find_cuda:
        print("found cuda nvcc (wanted postfix: %r): %s" % (postfix, p))
      if not p.endswith(postfix):
        continue
      yield p[:-len(postfix)]
    for p in cls._find_lib_in_ld_path():
      # Expect p == "/usr/local/cuda-8.0/lib64/libcudart.so" or so.
      postfix = "/%s/libcudart.so" % cls._get_lib_dir_name()
      if cls.verbose_find_cuda:
        print("found cuda lib (wanted postfix: %r): %s" % (postfix, p))
      if not p.endswith(postfix):
        continue
      yield p[:-len(postfix)]

  @classmethod
  def _check_valid_cuda_path(cls, p):
    """
    :param str p: path to CUDA, e.g. "/usr/local/cuda-8.0"
    :return: whether this is a valid CUDA path, i.e. we find all what we need
    :rtype: bool
    """
    if cls.verbose_find_cuda:
      print("check valid CUDA path: %s" % p)
    if not os.path.exists("%s/bin/nvcc" % p):
      return False
    if not os.path.exists("%s/include/cuda.h" % p):
      return False
    if not os.path.exists("%s/%s/libcudart.so" % (p, cls._get_lib_dir_name())):
      return False
    return True

  @classmethod
  def _find_cuda_path(cls):
    """
    :return: base CUDA path if we find one, otherwise None
    :rtype: str|None
    """
    for p in cls._cuda_path_candidates():
      if cls._check_valid_cuda_path(p):
        return p
    return None

  def is_available(self):
    return bool(self.cuda_path)

  def get_compiler_opts(self):
    return [
      "-I", "%s/include" % self.cuda_path, "-L", "%s/%s" % (self.cuda_path, self._get_lib_dir_name()),
      "-x", "cu"]

  def get_compiler_bin(self):
    assert self.cuda_path
    return "%s/bin/nvcc" % self.cuda_path

  @classmethod
  def get_instance(cls):
    """
    :rtype: CudaEnv
    """
    if cls._instance is not None:
      return cls._instance
    cls._instance = cls()
    return cls._instance


class OpCodeCompiler(NativeCodeCompiler):
  """
  Helper class to compile TF ops on-the-fly, similar as Theano.
  https://www.tensorflow.org/versions/master/how_tos/adding_an_op/
  https://github.com/tensorflow/tensorflow/blob/master/tensorflow/docs_src/extend/adding_an_op.md
  """

  CacheDirName = "returnn_tf_cache/ops"

  def __init__(self, use_cuda_if_available=True, include_paths=(), ld_flags=(), **kwargs):
    self._cuda_env = use_cuda_if_available and CudaEnv.get_instance()
    tf_include = tf.sysconfig.get_include()  # e.g. "...python2.7/site-packages/tensorflow/include"
    tf_include_nsync = tf_include + "/external/nsync/public"  # https://github.com/tensorflow/tensorflow/issues/2412
    include_paths = list(include_paths) + [tf_include, tf_include_nsync]
    ld_flags = list(ld_flags)
    if have_min_tf_version((1, 4)):
      # https://github.com/tensorflow/tensorflow/issues/13607
      ld_flags += ["-L%s" % tf.sysconfig.get_lib(), "-ltensorflow_framework"]
    super(OpCodeCompiler, self).__init__(include_paths=include_paths, ld_flags=ld_flags, **kwargs)
    self._tf_mod = None

  _relevant_info_keys = NativeCodeCompiler._relevant_info_keys + ("tf_version", "with_cuda")

  def _make_info_dict(self):
    d = super(OpCodeCompiler, self)._make_info_dict()
    d.update({
      "tf_version": tf.__version__,
      "with_cuda": bool(self._cuda_env and self._cuda_env.is_available())
    })
    return d

  def _get_compiler_bin(self):
    if self._cuda_env and self._cuda_env.is_available():
      return self._cuda_env.get_compiler_bin()
    return super(OpCodeCompiler, self)._get_compiler_bin()

  def _transform_compiler_opts(self, opts):
    if self._cuda_env and self._cuda_env.is_available():
      nvcc_opts = self._cuda_env.get_compiler_opts()
      nvcc_opts += ["-DGOOGLE_CUDA=1"]
      for opt in opts:
        nvcc_opts += ["-Xcompiler", opt]
      return nvcc_opts
    return super(OpCodeCompiler, self)._transform_compiler_opts(opts)

  def load_tf_module(self):
    if self._tf_mod:
      return self._tf_mod
    self._maybe_compile()
    self._tf_mod = tf.load_op_library(self._so_filename)
    return self._tf_mod


def make_var_tuple(v):
  """
  :param tf.Tensor|list[tf.Tensor]|tuple[tf.Tensor] v:
  :return: tuple of tensors
  :rtype: tuple[tf.Tensor]
  """
  if isinstance(v, (int, float, tf.Tensor, tf.Operation)):
    return (v,)
  if isinstance(v, list):
    return tuple(v)
  assert isinstance(v, tuple)
  return v


def add_scaled_noise_to_gradients(grads_and_vars, gradient_noise_scale, sparse_grads=False):
  """
  Adds scaled noise from a 0-mean normal distribution to gradients.
  Adapted from tf.contrib.layers.optimizers.

  :param list[(tf.Tensor|tf.IndexedSlices, tf.Variable)] grads_and_vars:
  :param float gradient_noise_scale: used as stddev for tf.truncated_normal().
  :param bool sparse_grads: for sparse gradients (tf.IndexedSlices), it will only add the noise to the indexed values.
    Seems broken in some cases? Needs debugging.
  :return: adapted grads_and_vars
  :rtype: list[(tf.Tensor|tf.IndexedSlices, tf.Variable)]
  """
  gradients, variables = zip(*grads_and_vars)
  noisy_gradients = []
  for gradient in gradients:
    if gradient is None:
      noisy_gradients.append(None)
      continue
    name = get_base_name(gradient)
    with reuse_name_scope_of_tensor(gradient):
      if isinstance(gradient, tf.IndexedSlices):
        if sparse_grads:
          gradient_values = gradient.values
          gradient_shape = gradient.values.get_shape()
        else:
          gradient_values = gradient
          gradient_shape = gradient.dense_shape
      else:
        assert isinstance(gradient, tf.Tensor)
        gradient_values = gradient
        gradient_shape = gradient_values.get_shape()
      if isinstance(gradient_shape, tf.TensorShape) and not gradient_shape.is_fully_defined():
        gradient_shape = tf.shape(gradient_values)
      noise = tf.truncated_normal(gradient_shape, stddev=gradient_noise_scale, name="%s_grad_noise" % name)
      gradient_values = tf.add(gradient_values, noise, name="%s_add_grad_noise" % name)
      if sparse_grads and isinstance(gradient, tf.IndexedSlices):
        gradient = tf.IndexedSlices(values=gradient_values, indices=gradient.indices, dense_shape=gradient.dense_shape)
      else:
        gradient = gradient_values
      noisy_gradients.append(gradient)
  return list(zip(noisy_gradients, variables))


class CustomGradient(object):
  """
  Utility functions to specify a custom gradient for a given function,
  which will be wrapped around via TF :func:`Defun`.

  Also see :class:`FlipGradientBuilder`.
  """

  def __init__(self):
    from Util import NotSpecified
    from weakref import ref
    self.num_calls = 0
    self.registered_ops_graph = ref(NotSpecified)
    self.registered_ops = {}  # func -> decorated func

  def Defun(self, *input_types, **kwargs):
    """
    :param (tf.Operation, tf.Tensor) -> tf.Tensor grad_op:
    :param list[tf.DType] input_types:
    :param dict[str] kwargs: passed to self.register()
    :return: function decorator
    :rtype: ((tf.Tensor) -> tf.Tensor) -> ((tf.Tensor) -> tf.Tensor)
    """

    def decorator(op):
      return self.register(input_types=input_types, op=op, **kwargs)

    return decorator

  def register(self, input_types, op, grad_op, name=None):
    """
    :param list[tf.DType] input_types:
    :param (tf.Tensor) -> tf.Tensor op:
    :param (tf.Operation, tf.Tensor) -> tf.Tensor grad_op: args are (op, out_grad) and it must return in_grad
    :param str name: optional func_name
    :return: op
    :rtype: (tf.Tensor) -> tf.Tensor
    """
    graph = tf.get_default_graph()
    assert isinstance(graph, tf.Graph)
    if graph is not self.registered_ops_graph():
      self.registered_ops.clear()
      from weakref import ref
      self.registered_ops_graph = ref(graph)
    if op in self.registered_ops:
      return self.registered_ops[op]
    from tensorflow.python.framework import function
    op_with_new_grad = function.Defun(*input_types, python_grad_func=grad_op, func_name=name)(op)
    # We need to add one instance of the new op to the graph now because of:
    # https://github.com/tensorflow/tensorflow/issues/6804
    # In case this is done too late, which is if there was already a previous session.run call,
    # you might get an exception like this:
    # NotFoundError: Op type not registered 'generic_loss_and_error_signal'
    call = op_with_new_grad(*[tf.placeholder(dtype) for dtype in input_types])
    assert isinstance(call, tf.Tensor)
    assert call.graph is graph
    self.registered_ops[op] = op_with_new_grad
    return op_with_new_grad

  @classmethod
  def _generic_loss_and_error_signal(cls, loss, x, grad_x):
    """
    :param tf.Tensor loss:
    :param tf.Tensor x:
    :param tf.Tensor grad_x:
    :return: just loss
    :rtype: tf.Tensor
    """
    return loss

  @classmethod
  def _generic_loss_and_error_signal_grad(cls, op, grad_loss):
    """
    :param tf.Operation op:
    :param tf.Tensor grad_loss: grad for loss
    :return: grad for op.outputs, only defined for op input x
    :rtype: (None, tf.Tensor, None)
    """
    loss, x, grad_x = op.inputs
    return None, grad_loss * grad_x, None

  def register_generic_loss_and_error_signal(self):
    """
    If you want to use :func:`generic_loss_and_error_signal` at some point,
    call this as early as possible, because of https://github.com/tensorflow/tensorflow/issues/6804.
    """
    return self.register(
      input_types=[tf.float32, tf.float32, tf.float32],
      op=self._generic_loss_and_error_signal,
      grad_op=self._generic_loss_and_error_signal_grad,
      name="generic_loss_and_error_signal")

  def generic_loss_and_error_signal(self, loss, x, grad_x):
    """
    Wrapper around self.register().
    Expects that loss = loss(x), and grad_x = \partial loss / \partial x.

    :param tf.Tensor loss:
    :param tf.Tensor x:
    :param tf.Tensor grad_x:
    :return: loss but with the gradient for x
    :rtype: tf.Tensor
    """
    loss = tf.convert_to_tensor(loss)
    x = tf.convert_to_tensor(x)
    grad_x = tf.convert_to_tensor(grad_x)
    x.set_shape(grad_x.get_shape())
    grad_x.set_shape(x.get_shape())
    generic_loss_and_error_signal = self.register_generic_loss_and_error_signal()
    loss_out = generic_loss_and_error_signal(loss, x, grad_x)
    loss_out.set_shape(loss.get_shape())
    return loss_out

custom_gradient = CustomGradient()


class SyntheticGradient(object):
  class Scope(object):
    def __init__(self):
      self.losses = []  # type: list[tf.Tensor]

    def register_loss(self, loss):
      """
      :param tf.Tenosr loss:
      """
      self.losses.append(loss)

    def exit(self):
      assert SyntheticGradient.scope is self
      SyntheticGradient.scope = None

    def as_fetch_dict(self):
      from collections import OrderedDict
      d = OrderedDict()
      for loss in self.losses:
        # Admittedly the way to get the layer name has some assumptions which might break in the future.
        # However, this is anyway mostly used for debugging purpose, so fix it, or just remove it.
        op_name_parts = loss.op.name.split("/")  # type: list[str]
        assert op_name_parts[-2] == "grad_prediction_loss"
        assert op_name_parts[-3].endswith("_grad")
        layer_name = op_name_parts[-4]
        d["cost:%s" % layer_name] = loss
      return d

  scope = None

  @classmethod
  def enter_gradient_scope(cls):
    """
    :rtype: SyntheticGradient.Scope
    """
    # Currently not multi-threading safe.
    assert not cls.scope
    cls.scope = cls.Scope()
    return cls.scope

  @classmethod
  def exit_gradient_scope(cls):
    cls.scope.exit()

  @classmethod
  @contextlib.contextmanager
  def gradient_scope(cls):
    """
    When gradients will be calculated (e.g. via some optimizer.minimize()),
    the synthetic gradient needs to collect the gradient prediction losses.
    This is done via this global scope.
    """
    yield cls.enter_gradient_scope()
    cls.exit_gradient_scope()

  @classmethod
  def _synthetic_gradient_fwd(cls, x, synthetic_grad_x):
    """
    :param tf.Tensor x:
    :param tf.Tensor synthetic_grad_x:
    :return: x
    :rtype: tf.Tensor
    """
    return x

  @classmethod
  def _synthetic_gradient_bwd(cls, op, grad_out):
    """
    :param tf.Operation op:
    :param tf.Tensor grad_out:
    :return: grad for x
    :rtype: (tf.Tensor,)
    """
    x, synthetic_grad_x = op.inputs
    if cls.scope:
      with tf.name_scope("grad_prediction_loss"):
        grad_prediction_loss = tf.reduce_mean(tf.square(synthetic_grad_x - tf.stop_gradient(grad_out)))
        tf.summary.scalar("loss", grad_prediction_loss)
      cls.scope.register_loss(grad_prediction_loss)
    return synthetic_grad_x, None

  @classmethod
  def synthetic_gradient(cls, x, synthetic_grad_x):
    """
    :param tf.Tensor x:
    :param tf.Tensor synthetic_grad_x:
    :return: x, where the gradient is overwritten by synthetic_grad_x, and when calculated,
      the gradient prediction loss will be added to ``cls.scope``.
    :rtype: tf.Tensor
    """
    op = custom_gradient.register(
      [tf.float32, tf.float32],
      op=cls._synthetic_gradient_fwd,
      grad_op=cls._synthetic_gradient_bwd,
      name="synthetic_gradient")
    y = op(x, synthetic_grad_x)
    y.set_shape(x.get_shape())
    return y


def filter_grad(x, threshold, axis):
  """
  :param tf.Tensor x:
  :param float threshold: all grads going through `x` which max(grad**2) is over the threshold are removed
  :param int|list[int] axis: max(grad**2) will be reduced over this axis
  :return: identity(x) with custom gradient
  :rtype: tf.Tensor
  """
  def grad_op(op, out_grad):
    with tf.name_scope("filter_grad__grad_op"):
      assert isinstance(op, tf.Operation)
      assert isinstance(out_grad, tf.Tensor)
      out_grad.set_shape(op.inputs[0].get_shape())
      keep_filter = tf.less(tf.reduce_max(out_grad ** 2, axis=axis, keep_dims=True), threshold)
      # keep_filter must be the same shape as out_grad.
      keep_filter = tf.logical_and(keep_filter, tf.ones_like(out_grad, dtype=tf.bool))
      out_grad = tf.where(keep_filter, out_grad, tf.zeros_like(out_grad))
      return out_grad

  with tf.name_scope("filter_grad"):
    op = custom_gradient.register([x.dtype], op=identity, grad_op=grad_op)
    y = op(x)
    y.set_shape(x.get_shape())
    return y


def debugRegisterBetterRepr():
  """
  Some types don't have good __repr__ implementations by default (for the current TF version).
  For debugging, it can be helpful to give some more info.
  This monkey-patches clazz.__repr__ of some TF classes if they are object.__repr__.
  """

  from tensorflow.python.framework import tensor_util

  def indexed_slices_repr(x):
    """
    :param tf.IndexedSlices x:
    :rtype: str
    """
    dense_shape = tensor_util.constant_value_as_shape(x.dense_shape)
    return "<tf.IndexedSlices %r dense_shape=%r dtype=%r>" % (x.name, dense_shape, x.dtype)

  def op_repr(x):
    """
    :param tf.Operation x:
    :rtype: str
    """
    return "<tf.Operation %r type=%r inputs=%r>" % (x.name, x.type, list(x.inputs))

  def var_repr(x):
    """
    :param tf.Variable x:
    :rtype: str
    """
    return "<tf.Variable %r initial_value=%r>" % (x.name, x.initial_value)

  def tensorarray_repr(x):
    """
    :param tf.TensorArray x:
    :rtype: str
    """
    op = x.handle.op
    assert isinstance(op, tf.Operation)
    return "<tf.TensorArray %r>" % op.name

  for cl, f in [
        (tf.IndexedSlices, indexed_slices_repr),
        (tf.Operation, op_repr),
        (tf.Variable, var_repr),
        (tf.TensorArray, tensorarray_repr)]:
    if getattr(cl, "__repr__") is object.__repr__:
      setattr(cl, "__repr__", f)


def cond(pred, fn1, fn2, name=None):
  """
  This is a wrapper around tf.control_flow_ops.cond().
  This will be a branched execution, i.e. either fn1() or fn2() will be executed,
  or at least the resulting graph will be evaluated.
  If pred can is constant at the call, only the corresponding fn will be called.
  This is similar as the TF internal _smart_cond().

  :param tf.Tensor|bool pred:
  :param ()->(tf.Tensor|list[tf.Tensor]) fn1:
  :param ()->(tf.Tensor|list[tf.Tensor]) fn2:
  :param str name:
  :return: fn1() if pred else fn2()
  :rtype: tf.Tensor|list[tf.Tensor]
  """
  if not callable(fn1):
    raise TypeError("fn1 must be callable.")
  if not callable(fn2):
    raise TypeError("fn2 must be callable.")
  if pred is True:
    return fn1()
  if pred is False:
    return fn2()
  from tensorflow.python.framework import tensor_util
  pred_const = tensor_util.constant_value(pred)
  if pred_const is not None:
    if pred_const:
      return fn1()
    else:
      return fn2()
  from tensorflow.python.ops import control_flow_ops
  return control_flow_ops.cond(pred, fn1, fn2, name=name)


def single_strided_slice(x, axis, begin=None, end=None, step=None):
  """
  :param tf.Tensor x:
  :param int|tf.Tensor axis:
  :param int|tf.Tensor|None begin:
  :param int|tf.Tensor|None end:
  :param int|tf.Tensor|None step:
  :return: e.g. if axis == 0, returns x[begin:end:step], if axis == 1, returns x[:, begin:end:step], etc.
  :rtype: tf.Tensor
  """
  with tf.name_scope("single_strided_slice"):
    if isinstance(axis, int):
      if axis < 0 and x.get_shape().ndims is not None:
        axis %= x.get_shape().ndims
        assert axis >= 0
      if axis >= 0:
        return x[(slice(None),) * axis + (slice(begin, end, step),)]
    else:
      assert isinstance(axis, tf.Tensor)
    axis = axis % tf.rank(x)
    shape = tf.shape(x)
    if begin is None:
      begin = 0
    if end is None:
      end = shape[axis]
    begins = tf.concat([tf.zeros((axis,), tf.int32), (begin,)], axis=0)
    ends = tf.concat([shape[:axis], (end,)], axis=0)
    if step is not None:
      strides = tf.concat([tf.ones((axis,), tf.int32), (step,)], axis=0)
    else:
      strides = None
    return tf.strided_slice(x, begin=begins, end=ends, strides=strides)


def circular_pad(x, paddings, axes=None):
  """
  :param tf.Tensor x: shape (..., height, width)
  :param int|((int,int), (int,int))|tf.Tensor paddings: how much to add ((top,bottom),(left,right))
  :return: tensor with shape (..., top + height + bottom, left + width + right)
  :rtype: tf.Tensor
  """
  with tf.name_scope("circular_pad"):
    ndim = x.get_shape().ndims
    assert ndim is not None
    shape = tf.shape(x)
    if axes is None:
      axis_height = ndim - 2
      axis_width = ndim - 1
    elif isinstance(axes, tf.Tensor):
      axes = check_input_ndim(axes, 1)
      axes = check_input_dim(axes, 0, 2)
      axis_height, axis_width = axes[0], axes[1]
    else:
      axis_height, axis_width = axes
    height, width = shape[axis_height], shape[axis_width]
    if isinstance(paddings, tf.Tensor):
      paddings = check_input_ndim(paddings, 2)
      paddings = check_input_dim(paddings, 0, 2)
      paddings = check_input_dim(paddings, 1, 2)
      top, bottom = paddings[0, 0], paddings[0, 1]
      left, right = paddings[1, 0], paddings[1, 1]
    elif isinstance(paddings, int):
      top = bottom = left = right = paddings
    else:
      assert isinstance(paddings, (list, tuple))
      (top, bottom), (left, right) = paddings
    left_x = single_strided_slice(x, begin=width - left, axis=axis_width)
    right_x = single_strided_slice(x, end=right, axis=axis_width)
    left_right_and_x = tf.concat([left_x, x, right_x], axis=axis_width)  # shape (..., height, left + width + right)
    top_x = single_strided_slice(left_right_and_x, begin=height - top, axis=axis_height)
    bottom_x = single_strided_slice(left_right_and_x, end=bottom, axis=axis_height)
    all_combined_x = tf.concat([top_x, left_right_and_x, bottom_x], axis=axis_height)  # final shape
    assert isinstance(all_combined_x, tf.Tensor)
    return all_combined_x


def spatial_smoothing_energy(x, dim, use_circular_conv=True):
  """
  :param tf.Tensor x: shape (..., dim)
  :param int dim: last dimension of x
  :param bool use_circular_conv: whether to use circular convolution, via circular_pad
  :rtype: tf.Tensor
  :return: energy of shape (...)

  Via Achieving Human Parity in Conversational Speech Recognition, Microsoft, 2017.
  Interpret the last dimension as 2D (w, h) and apply some high-pass filter on it.
  """
  import math
  with tf.name_scope("spatial_smoothing_energy"):
    x = check_input_dim(x, -1, dim)
    shape = get_shape(x)
    w = int(math.sqrt(dim))
    while dim % w > 0:
      w -= 1
      assert w > 0
    h = dim // w
    assert w * h == dim
    assert w >= 3 and h >= 3, "too small"
    # input shape: [batch, in_height=h, in_width=w, in_channels=1]
    x = tf.reshape(x, [-1, h, w, 1])
    if use_circular_conv:
      x = circular_pad(x, paddings=1, axes=(1, 2))  # [batch, h+2, w+2, in_channels=1]
    # filter shape: [filter_height, filter_width, in_channels=1, out_channels=1]
    filter = tf.reshape(tf.constant(
      [[-0.125, -0.125, -0.125],
       [-0.125, 1.0, -0.125],
       [-0.125, -0.125, -0.125]]), [3, 3, 1, 1])
    # out shape: [batch, out_height, out_width, out_channels=1]
    out = tf.nn.conv2d(x, filter=filter, strides=[1, 1, 1, 1], padding="VALID")
    out = tf.reshape(out, shape[:-1] + [-1])  # (..., out_height*out_width)
    # Note: Square all the filter values.
    return tf.reduce_sum(out ** 2, axis=-1)


def nan_to_num(x, nan_num=0, inf_num=1e30):
  """
  Like numpy.nan_to_num().

  :param tf.Tensor|tf.IndexedSlices x:
  :param float|tf.Tensor nan_num:
  :param float|tf.Tensor inf_num:
  :return: x with replaced nan and inf
  """
  if isinstance(x, tf.IndexedSlices):
    return tf.IndexedSlices(values=nan_to_num(x.values), indices=x.indices, dense_shape=x.dense_shape)
  with tf.name_scope("nan_to_num"):
    nan_num = tf.convert_to_tensor(nan_num, dtype=x.dtype)
    inf_num = tf.convert_to_tensor(inf_num, dtype=x.dtype)
    # Note that tf.where() does not support broadcasting at the moment,
    # so we need the same shape. The following will do that.
    # This should be removed once tf.where() supports broadcasting.
    # https://github.com/tensorflow/tensorflow/issues/3945
    nan_num = tf.ones_like(x) * nan_num
    inf_num = tf.ones_like(x) * inf_num
    x = tf.where(tf.is_nan(x), nan_num, x)
    x = tf.where(tf.logical_and(tf.is_inf(x), tf.greater(x, 0)), inf_num, x)
    x = tf.where(tf.logical_and(tf.is_inf(x), tf.less(x, 0)), -inf_num, x)
    return x


def identity_op_nested(x, name="identity"):
  """
  :param tf.Tensor|list[tf.Tensor]|dict[str,tf.Tensor] x:
  :param str name:
  :rtype tf.Tensor|list[tf.Tensor]|dict[str,tf.Tensor]
  """
  if isinstance(x, dict):
    return {k: identity_op_nested(x[k], name="%s_%s" % (name, k)) for k in x}
  if isinstance(x, (list, tuple)):
    return [identity_op_nested(x[i], name="%s_%i" % (name, i)) for i in range(len(x))]
  assert isinstance(x, tf.Tensor)
  return tf.identity(x, name=name)


def nd_indices(indices, batch_axis=0):
  """
  :param tf.Tensor indices: e.g. (batch, ...) -> index
  :return: extended indices with batch-idx which can be used for tf.gather_nd,
    i.e. in the example of shape (batch, ..., 2) where the 2-tuple represents (batch_idx, index).
  :rtype: tf.Tensor
  """
  assert indices.get_shape().ndims >= 1
  assert batch_axis < indices.get_shape().ndims
  with tf.name_scope("nd_indices"):
    batches_idxs = tf.range(tf.shape(indices)[batch_axis], name="batches_idxs")  # (batch,)
    batches_idxs = tf.cast(batches_idxs, dtype=indices.dtype)
    for axis in range(indices.get_shape().ndims):
      if axis == batch_axis:
        continue
      batches_idxs = expand_dims_unbroadcast(batches_idxs, axis=axis, dim=tf.shape(indices)[axis],
                                             name="batches_idxs_bc")  # (batch, ...)
    batches_idxs.set_shape(indices.get_shape())
    idxs_exp = tf.stack([batches_idxs, indices], axis=-1,
                        name="idxs_exp")  # (batch,...,2), where the 2 stands for (batch_idx, index)
    return idxs_exp


def stop_event_writer_thread(event_writer):
  """
  There is a bug in TensorFlow (at least 1.1.0) (https://github.com/tensorflow/tensorflow/issues/4820)
  that the event writer thread is never stopped.
  This will try to stop it. Only do it if you don't use the event writer anymore.

  :param tensorflow.python.summary.writer.event_file_writer.EventFileWriter event_writer:
  """
  from tensorflow.python.summary.writer.event_file_writer import EventFileWriter, _EventLoggerThread
  assert isinstance(event_writer, EventFileWriter)
  if not event_writer._worker:  # maybe fixed already?
    return

  # This solution is very ugly and dependent on TF internal code.
  class DummyStopThread:
    @classmethod
    def WriteEvent(cls, *args, **kwargs):
      raise SystemExit  # stop the thread

  assert isinstance(event_writer._worker, _EventLoggerThread)
  worker = event_writer._worker
  worker._ev_writer = DummyStopThread
  worker._queue.put(None)
  worker.join()


def optional_add(*args):
  """
  :param list[tf.Tensor|None]|tf.Tensor args:
  :rtype: tf.Tensor|None
  :return: sums all non-None values, or returns None if there are none
  """
  y = None
  for v in args:
    if v is not None:
      if y is None:
        y = v
      else:
        y = y + v
  return y


def windowed_nd(source, window, padding="same", time_axis=1, new_window_axis=2):
  """
  :param tf.Tensor source: N-D tensor of shape (..., n_time, ...)
  :param int|tf.Tensor window: window size
  :param str padding: "same" or "valid"
  :param int time_axis:
  :param int new_window_axis:
  :return: tensor of shape (..., n_time, ..., window, ...)
  :rtype: tf.Tensor
  """
  with tf.name_scope("windowed_batch"):
    if time_axis != 0:
      source = move_axis(source, time_axis, 0)  # (n_time,...)
    source_shape = tf.shape(source)
    n_time = source_shape[0]
    if padding == "same":
      n_out_time = n_time
      w_right = window // 2
      w_left = window - w_right - 1
      pad_left = tf.zeros(tf.concat([[w_left], source_shape[1:]], axis=0), dtype=source.dtype)
      pad_right = tf.zeros(tf.concat([[w_right], source_shape[1:]], axis=0), dtype=source.dtype)
      source = tf.concat([pad_left, source, pad_right], axis=0)  # shape[0] == n_time + window - 1
    elif padding == "valid":
      n_out_time = n_time - window + 1
    else:
      raise Exception("invalid padding %r" % padding)
    tiled_dimshuffle = expand_dims_unbroadcast(source, axis=0, dim=window)  # (window,n_time+window-1,...)
    # We want to shift every dim*time block by one to the left.
    # To do this, we interpret that we have one more time frame (i.e. n_time+window).
    # We have to do some dimshuffling so that we get the right layout, then we can flatten,
    # add some padding, and then dimshuffle it back.
    # Then we can take out the first n_time frames.
    tiled_flat = tf.reshape(tiled_dimshuffle, [-1])
    rem = window * tf.reduce_prod(source_shape[1:])
    tiled_flat_pad_right = tf.concat([tiled_flat, tf.zeros((rem,), dtype=source.dtype)], axis=0)
    tiled_reshape_shift = tf.reshape(
      tiled_flat_pad_right,
      tf.concat([(window, n_time + window),
                 source_shape[1:]], axis=0))  # add time frame, (window,n_time+window,...)
    final = tiled_reshape_shift
    if new_window_axis != 0:
      final = move_axis(final, 0, new_window_axis)  # (n_time+window,...,window,...)
      final = final[:n_out_time]  # (n_out_time,...,window,...)
    else:
      final = final[:, :n_out_time]  # (window,n_out_time,...)
    # Move time-axis back to its original place.
    if new_window_axis <= time_axis:
      time_axis += 1  # New window axis was inserted before.
    if time_axis != 0:
      if new_window_axis != 0:
        final = move_axis(final, 0, time_axis)
      else:
        final = move_axis(final, 1, time_axis)
    return final


def global_tensor(f, name):
  """
  This creates a global accessible tensor in the graph to be reused later,
  i.e. on the second call given a unique name, it will not create a new tensor
  but return the previously created tensor.
  This is for the current graph, i.e. if there is a new graph, it will recreate the tensor.

  :param () -> tf.Tensor f: callable which creates the tensor
  :param str name: global reference name for the tensor. should be a valid scope name
  :return: the tensor
  :rtype: tf.Tensor
  """
  graph = tf.get_default_graph()
  assert isinstance(graph, tf.Graph)
  abs_graph_name = "globals/%s:0" % name
  try:
    return graph.get_tensor_by_name(abs_graph_name)
  except KeyError:  # does not exist yet
    pass
  with tf.name_scope("global_tensor_%s" % name):  # relative to the current scope
    v = f()
  with tf.name_scope("globals/"):  # enter the absolute scope
    v = tf.identity(v, name=name)
  assert isinstance(v, tf.Tensor)
  assert v.name == abs_graph_name
  assert graph.get_tensor_by_name(abs_graph_name) is v
  return v


def get_global_train_flag_placeholder():
  """
  :return: bool scalar tensor
  :rtype: tf.Tensor
  """
  return global_tensor(
    lambda: tf.placeholder(tf.bool, shape=(), name="train_flag"),
    name="train_flag")


def encode_raw(x, axis=-1, seq_lens=None):
  """
  The inverse function of tf.decode_raw().
  Also see: https://stackoverflow.com/questions/43403147/how-to-create-a-encode-raw-tensorflow-function

  :param tf.Tensor x: of integer types [0,255], will get casted to uint8
  :param int axis: the axis to reduce-join the string. decode_raw has added it at the end
  :param tf.Tensor|None seq_lens: must have same shape as x after reduce-joining.
    Note that using seq_lens will make our output not compatible with tf.decode_raw() anymore
    because tf.decode_raw() requires all strings to be of the same length.
  :return: string tensor
  :rtype: tf.Tensor
  """
  with tf.name_scope("encode_raw"):
    character_lookup = global_tensor(
      lambda: tf.constant([chr(i) for i in range(256)]), name="character_lookup")
    raw_bytes = tf.bitcast(x, tf.uint8, name="raw_bytes")
    chars = tf.gather(character_lookup, indices=tf.cast(raw_bytes, tf.int32), name="chars")
    strings = tf.reduce_join(chars, axis=axis, name="strings")
    if seq_lens is not None:
      strings = tf.substr(strings, pos=tf.zeros_like(seq_lens), len=seq_lens)
    return strings


def map_labels(x, label_map, name="map_labels"):
  """
  :param tf.Tensor|tf.SparseTensor x: values of integer types
  :param dict[int,int|None] label_map: should be dense on input
  :param str name:
  :return: mapped values
  :rtype: tf.Tensor
  """
  if any([v is None for v in label_map.values()]):
    assert isinstance(x, tf.SparseTensor), "not supported otherwise currently"
    x = remove_labels(x, labels=[k for (k, v) in label_map.items() if v is None])
    label_map = {k: v if v is not None else -1 for (k, v) in label_map.items()}
  if isinstance(x, tf.SparseTensor):
    return tf.SparseTensor(
      indices=x.indices,
      values=map_labels(x.values, label_map=label_map, name=name),
      dense_shape=x.dense_shape)
  with tf.name_scope(name):
    assert label_map
    assert 0 in label_map
    assert len(label_map) - 1 in label_map
    lookup = global_tensor(
      lambda: tf.constant([label_map[i] for i in range(len(label_map))]),
      name="label_map_lookup_id%i" % id(label_map))
    y = tf.gather(lookup, indices=x, name="mapped")
    return y


def remove_labels(x, labels):
  """
  :param tf.SparseTensor x: sequences, i.e. the indices are interpret as (batch,time)
  :param set[int]|list[int] labels:
  :return: x where all provided labels are removed, and the indices are changed accordingly
  """
  if not labels:
    return x
  x.indices.set_shape((tf.TensorShape((None, 2))))
  x.values.set_shape((tf.TensorShape((None,))))
  x.dense_shape.set_shape(tf.TensorShape((2,)))
  import numpy

  # Much simpler for now to use tf.py_func.
  def py_remove_labels(indices, values, dense_shape):
    assert isinstance(indices, numpy.ndarray), "indices %r" % indices
    assert isinstance(values, numpy.ndarray), "values %r" % indices
    assert isinstance(dense_shape, numpy.ndarray), "dense_shape %r" % indices
    indices_dtype = indices.dtype
    values_dtype = values.dtype
    dense_shape_dtype = dense_shape.dtype
    assert dense_shape.shape == (2,), "dense_shape %r shape" % dense_shape  # (batch, time)
    assert len(indices) == len(values), "len mismatch. shapes %r vs %r" % (indices.shape, values.shape)
    indices = list(indices)
    values = list(values)
    i = 0
    while i < len(indices):
      if values[i] not in labels:
        i += 1
        continue
      batch, time = indices[i]
      # Remove it.
      indices = indices[:i] + indices[i + 1:]
      values = values[:i] + values[i + 1:]
      for j in range(len(indices)):
        if indices[j][0] != batch:
          continue
        if indices[j][1] < time:
          continue
        indices[j][1] -= 1
    indices = numpy.array(indices, dtype=indices_dtype)
    values = numpy.array(values, dtype=values_dtype)
    dense_shape = numpy.array(
      [dense_shape[0], max([idx[1] + 1 for idx in indices] or [0])], dtype=dense_shape_dtype)
    return indices, values, dense_shape

  with tf.name_scope("remove_labels"):
    indices, values, dense_shape = tf.py_func(
      py_remove_labels,
      [x.indices, x.values, x.dense_shape],
      [x.indices.dtype, x.values.dtype, x.dense_shape.dtype],
      name="py_remove_labels")
    assert isinstance(indices, tf.Tensor)
    assert isinstance(values, tf.Tensor)
    assert isinstance(dense_shape, tf.Tensor)
    indices.set_shape((tf.TensorShape((None, 2))))
    values.set_shape((tf.TensorShape((None,))))
    dense_shape.set_shape(tf.TensorShape((2,)))
    return tf.SparseTensor(indices=indices, values=values, dense_shape=dense_shape)


def pad_zeros_in_axis(x, before=0, after=0, axis=0):
  """
  :param tf.Tensor x:
  :param int|tf.Tensor before:
  :param int|tf.Tensor after:
  :param int axis:
  :return:
  """
  with tf.name_scope("pad_zeros_in_axis"):
    paddings = [[0, 0] for i in range(x.get_shape().ndims)]
    paddings[axis] = [before, after]
    return tf.pad(x, paddings=paddings)


def slice_pad_zeros(x, begin, end, axis=0):
  """
  :param tf.Tensor x: of shape (..., time, ...)
  :param int|tf.Tensor begin:
  :param int|tf.Tensor end:
  :param int axis:
  :return: basically x[begin:end] (with axis==0) but if begin < 0 or end > x.shape[0],
   it will not discard these frames but pad zeros, such that the resulting shape[0] == end - begin.
  :rtype: tf.Tensor
  """
  with tf.name_scope("slice_pad_zeros"):
    min_frame = tf.minimum(begin, end)
    left_rem = -min_frame
    x, begin, end = tf.cond(
      tf.less_equal(left_rem, 0),
      lambda: [x, begin, end],
      lambda: [pad_zeros_in_axis(x, before=left_rem, axis=axis), begin + left_rem, end + left_rem])
    max_frame = tf.maximum(begin, end)
    right_rem = max_frame - tf.shape(x)[axis]
    x = tf.cond(
      tf.less_equal(right_rem, 0),
      lambda: x,
      lambda: pad_zeros_in_axis(x, after=right_rem, axis=axis))
    return single_strided_slice(x, axis=axis, begin=begin, end=end)


def post_control_dependencies(x, updates):
  """
  :param tf.Tensor|list[tf.Tensor]|dict[str,tf.Tensor] x:
  :param list[tf.Operation] updates:
  :return: identity(x) with control_dependencies(updates)
  :rtype: tf.Tensor|list[tf.Tensor]|dict[str,tf.Tensor]
  """
  with tf.name_scope("post_control_dependencies"):
    with tf.control_dependencies(updates):
      if isinstance(x, tf.Tensor):
        return tf.identity(x)
      elif isinstance(x, (tuple, list)):
        return [tf.identity(v) for v in x]
      elif isinstance(x, dict):
        return {k: tf.identity(v) for (k, v) in x.items()}
      else:
        raise ValueError("type of %r not expected" % x)


@contextlib.contextmanager
def sequential_control_dependencies(l):
  """
  tf.control_dependencies but each operation will be created such that it is executed
  after the ones coming before in the list, i.e. l[0] is executed first, l[-1] is executed last.

  :param list[()->(tf.Operation|tf.Tensor)] l:
  """
  with tf.control_dependencies([l[0]()]) as dep:
    if len(l) > 1:
      with sequential_control_dependencies(l[1:]) as dep2:
        yield dep2
    else:
      yield dep


def global_queue(name, queue_type, capacity, dtypes, shapes=None, names=None):
  """
  :param (args)->tf.QueueBase queue_type: some function which creates a queue
  :param str name: global name
  :param list[tf.DType|str] dtypes:
  :param list[tf.TensorShape|tuple[int|None]]|None shapes:
  :param list[str]|None names:
  :rtype: tf.QueueBase
  """
  queue_ref = global_tensor(
    name=name,
    f=lambda: queue_type(capacity=capacity, dtypes=dtypes, shapes=shapes, names=names).queue_ref)
  queue = tf.QueueBase(dtypes=dtypes, shapes=shapes, names=names, queue_ref=queue_ref)
  return queue


def init_variable_if_needed(v):
  """
  :param tf.Variable v:
  :rtype: tf.Operation
  """
  def make_init():
    # Cannot use tf.variables_initializer(), see here: https://stackoverflow.com/questions/44354964/
    with tf.control_dependencies([tf.assign(v, v.initial_value)]):
      return tf.no_op()

  maybe_init = tf.cond(
    tf.is_variable_initialized(v),
    lambda: tf.no_op(),
    make_init,
    name="maybe_init")

  return maybe_init


def auto_init_var(v):
  """
  :param tf.Variable v:
  :return: a reference to the var via tf.identity
  :rtype: tf.Tensor
  """
  with tf.control_dependencies(init_variable_if_needed(v)):
    return tf.identity(v, name="auto_init_var")


def true_once():
  """
  :return: tensor which will be True once and then always False
    Internally, this creates a non-trainable variable as a helper.
  :rtype: tf.Tensor
  """
  with tf.variable_scope("true_once"):
    v = tf.Variable(initial_value=True, trainable=False, name="true_once_var")
    with tf.control_dependencies([init_variable_if_needed(v)]):
      # Cannot use tf.identity because that would give us a reference to the var but we want to copy it now.
      x = tf.where(v.read_value(), True, False)
      with tf.control_dependencies([x]):
        x = tf.identity(x)
        reset = tf.assign(v, False)
        with tf.control_dependencies([x, reset]):
          x = tf.identity(x)
  return x


def raise_OutOfRangeError():
  """
  :return: an op which raises an OutOfRangeError
  :rtype: tf.Operation
  """
  # Kind of hacky. We create some dummy queue, close it and every time we call dequeue on it,
  # it will raise the desired exception.
  with tf.name_scope("raise_OutOfRangeError"):
    queue = global_queue(name="raise_exception/queue", queue_type=tf.FIFOQueue, capacity=1, dtypes=[tf.bool])
    # We must only close it once, otherwise we could get a CancelledError.
    queue_open = global_tensor(f=true_once, name="raise_exception/queue_open")
    with tf.control_dependencies([tf.cond(queue_open, lambda: queue.close(), lambda: tf.no_op())]):
      return queue.dequeue()


def enforce_copy(x):
  """
  :param tf.Tensor|tf.Variable x:
  :return: copy of input, i.e. enforces that this is not a ref
  """
  with tf.name_scope("copy"):
    zero = x.dtype.as_numpy_dtype()
    return tf.add(x, zero)


def view_as(x, dtype):
  """
  Does the numpy.view equivalent.
  Note that the current implementation is inefficient (uses tf.py_func) and CPU-only.
  Also see `tf.tf.bitcast`.

  :param tf.Tensor x:
  :param tf.DType dtype:
  :return: x.view(dtype) equivalent (see numpy.view)
  """
  import numpy

  def py_wrap_numpy_view(x):
    assert isinstance(x, numpy.ndarray)
    return x.view(dtype.as_numpy_dtype)

  y, = tf.py_func(
    py_wrap_numpy_view,
    [x], [dtype],
    name="py_wrap_numpy_view")
  assert isinstance(y, tf.Tensor)
  y.set_shape(x.get_shape())
  return y


def safe_log(x, eps=1e-20):
  """
  Safe wrapper around :func:`tf.log` which avoids infs or nans in the gradient.

  :param tf.Tensor x:
  :param float|tf.Tensor eps:
  :return: log(max(x, eps))
  :rtype: tf.Tensor
  """
  return tf.log(tf.maximum(x, eps))


class Lock(object):
  """
  A pure TensorFlow implementation of a mutex / lock.
  """
  def __init__(self, name="Lock"):
    self._name = name
    with tf.name_scope(self._name):
      from tensorflow.python.ops.data_flow_ops import StagingArea
      self._queue = StagingArea(dtypes=[tf.bool])
      self._queue_init = self._queue.put([tf.constant(True)])

  def init(self):
    return self._queue_init

  def lock(self):
    """
    On first call, just returns. Any further call will block, unless there is an unlock() call.
    """
    with tf.name_scope("%s/lock" % self._name):
      v, = self._queue.get()
      return v

  def unlock(self):
    """
    Must be called after lock().
    """
    with tf.name_scope("%s/unlock" % self._name):
      return self._queue.put([tf.constant(True)])


class Condition(object):
  """
  A pure TensorFlow implementation of a condition.
  """
  def __init__(self, lock=None, name="Condition"):
    self._name = name
    with tf.variable_scope(name):
      self._init_ops = []
      if not lock:
        lock = Lock()
        self._init_ops += [lock.init()]
      self.lock = lock
      self._waiting_counter = tf.Variable(initial_value=0, trainable=False, name="waiting_counter")
      self._waiter_queue = tf.FIFOQueue(capacity=1, dtypes=[tf.bool], name="waiter_queue")
      self._init_ops += [self._waiting_counter.initializer]

  def init(self):
    return tf.group(*self._init_ops)

  def wait(self):
    """
    Must be called with the lock held, will unlock while waiting for a signal.
    """
    with tf.name_scope("%s/wait" % self._name):
      with sequential_control_dependencies([
        lambda: self._waiting_counter.assign_add(1, use_locking=True),
        lambda: self.lock.unlock(),
        lambda: self._waiter_queue.dequeue(),
        lambda: self.lock.lock(),
        lambda: self._waiting_counter.assign_sub(1, use_locking=True)
      ]):
        return tf.no_op()

  def wait_counter(self):
    return enforce_copy(self._waiting_counter.read_value())

  def signal(self):
    """
    Must be called with the lock held.
    Emits one signal.
    """
    with tf.name_scope("%s/signal" % self._name):
      def on_waiting_counter():
        return self._waiter_queue.enqueue(True)
      return tf.cond(tf.greater(self._waiting_counter.read_value(), 0), on_waiting_counter, lambda: tf.no_op())

  def signal_all(self):
    """
    Must be called with the lock held.
    Emits as many signals as they are waiters.
    """
    with tf.name_scope("%s/signal_all" % self._name):
      count = self.wait_counter()
      with sequential_control_dependencies([lambda: count, lambda: self.lock.unlock()]):
        # We must unlock because we could have to do multiple signals but the waiter-queue has only capacity 1,
        # i.e. we would (dead)lock otherwise.
        def body(i):
          with tf.control_dependencies([i]):
            with tf.control_dependencies([self._waiter_queue.enqueue(False)]):
              return i + 1
        loop = tf.while_loop(
          cond=lambda i: tf.less(i, count),
          body=body, parallel_iterations=1, back_prop=False, loop_vars=[0])
        with tf.control_dependencies([loop]):
          return self.lock.lock()


class GlobalTensorArrayOpMaker:
  """
  Creates a TensorArray which does not use the per-run ("per-step") resource manager container
  but uses the standard container which persists across runs.
  This TensorArray resource handle is then just a standard TensorArray resource handle which
  can be used with all TensorArray related functions/ops.

  Note: This whole implementation currently does not work because tensor_array.h is not available.
  See https://github.com/tensorflow/tensorflow/issues/10527
  and test_GlobalTensorArray().

  An alternative to this might be the MapStagingArea (https://github.com/tensorflow/tensorflow/pull/9686),
  which should get into TF 1.2.2.
  """

  code = """
    #include "tensorflow/core/framework/op_kernel.h"
    #include "tensorflow/core/framework/register_types.h"
    #include "tensorflow/core/framework/resource_mgr.h"
    #include "tensorflow/core/framework/tensor.h"
    #include "tensorflow/core/framework/tensor_shape.h"
    #include "tensorflow/core/framework/types.h"
    #include "tensorflow/core/kernels/bounds_check.h"
    #include "tensorflow/core/kernels/tensor_array.h"
    #include "tensorflow/core/lib/core/errors.h"
    #include "tensorflow/core/lib/core/refcount.h"
    #include "tensorflow/core/lib/strings/strcat.h"
    #include "tensorflow/core/platform/dynamic_annotations.h"
    #include "tensorflow/core/platform/logging.h"
    #include "tensorflow/core/platform/thread_annotations.h"
    #include "tensorflow/core/platform/types.h"

    using namespace tensorflow;
  
    // Adopted from https://github.com/tensorflow/tensorflow/blob/master/tensorflow/core/ops/data_flow_ops.cc.
    REGISTER_OP("GlobalTensorArray")
    .Input("size: int32")
    .Attr("container: string = ''")
    .Attr("shared_name: string = ''")
    .Attr("dtype: type")
    .Attr("element_shape: shape = { unknown_rank: true }")
    .Attr("dynamic_size: bool = false")
    .Attr("clear_after_read: bool = true")
    .Attr("tensor_array_name: string = ''")
    .Output("handle: resource")
    .Output("flow: float")
    .SetIsStateful()
    .SetShapeFn([](InferenceContext* c) {
      ShapeHandle unused;
      TF_RETURN_IF_ERROR(c->WithRank(c->input(0), 0, &unused));
      c->set_output(0, c->Vector(2));
      c->set_output(1, c->Scalar());
      return Status::OK();
    })
    .Doc("GlobalTensorArray, persistent across runs");
    
    // Copied from https://github.com/tensorflow/tensorflow/blob/master/tensorflow/core/kernels/tensor_array_ops.cc,
    // and https://github.com/tensorflow/tensorflow/blob/master/tensorflow/core/framework/resource_op_kernel.h.
    // The original TensorArrayOp used the per-run ("per-step") resource manager container
    // but we use the standard container which persists across runs.
    class GlobalTensorArrayOp : public OpKernel {
     public:
      explicit GlobalTensorArrayOp(OpKernelConstruction* context)
          : OpKernel(context), device_type_(context->device_type()) {
        OP_REQUIRES_OK(context, context->GetAttr("dtype", &dtype_));
        OP_REQUIRES_OK(context, context->GetAttr("element_shape", &element_shape_));
        OP_REQUIRES_OK(context, context->GetAttr("dynamic_size", &dynamic_size_));
        OP_REQUIRES_OK(context,
                       context->GetAttr("clear_after_read", &clear_after_read_));
        OP_REQUIRES_OK(context,
                       context->GetAttr("tensor_array_name", &tensor_array_name_));
        if (tensor_array_name_.empty()) tensor_array_name_ = name();

        AllocatorAttributes alloc_attr;
        alloc_attr.set_on_host(true);
        OP_REQUIRES_OK(context, context->allocate_persistent(
                                tensorflow::DT_STRING, tensorflow::TensorShape({2}),
                                &handle_, alloc_attr));
      }
    
      ~GlobalTensorArrayOp() {
        if (resource_ != nullptr) {
          resource_->Unref();
          if (cinfo_.resource_is_private_to_kernel()) {
            if (!cinfo_.resource_manager()
                     ->template Delete<T>(cinfo_.container(), cinfo_.name())
                     .ok()) {
              // Do nothing; the resource can have been deleted by session resets.
            }
          }
        }
      }
    
      void Compute(OpKernelContext* ctx) override {
        mutex_lock l(mu_);
        if (resource_ == nullptr) {
          ResourceMgr* mgr = ctx->resource_manager();
          OP_REQUIRES(ctx, mgr != nullptr, errors::Internal("No resource manager."));
          OP_REQUIRES_OK(ctx, cinfo_.Init(mgr, def()));
          auto h = handle_.AccessTensor(ctx)->template flat<string>();
          h(0) = cinfo_.container();
          h(1) = cinfo_.name();
          OP_REQUIRES_OK(ctx, CreateTensorArray(ctx, rm, &handle_, &resource_));
        }

        Tensor* handle;
        OP_REQUIRES_OK(ctx, ctx->allocate_output(0, TensorShape({}), &handle));
        handle->flat<ResourceHandle>()(0) =
            resource_->resource_handle(ctx);            
        if (ctx->num_outputs() == 2) {
          // Create the flow output.
          Tensor* flow;
          OP_REQUIRES_OK(ctx, ctx->allocate_output(1, TensorShape({}), &flow));
          if (device_type_ == DEVICE_CPU) {
            // Value doesn't matter, but this makes msan not complaint about
            // copying an uninitialized value. To do this on GPU would require
            // a kernel launch or a host->device memcpy, so we avoid that.
            flow->flat<float>()(0) = 0;
          }
        }
      }
    
     private:
      Status CreateTensorArray(OpKernelContext* ctx, ResourceMgr* rm,
                               Tensor* tensor_array_output_handle,
                               TensorArray** output_tensor_array) EXCLUSIVE_LOCKS_REQUIRED(mu_) {
        const Tensor* tensor_size;
        TF_RETURN_IF_ERROR(ctx->input("size", &tensor_size));
    
        if (!TensorShapeUtils::IsScalar(tensor_size->shape())) {
          return errors::InvalidArgument(
              "TensorArray size must be scalar, but had shape: ",
              tensor_size->shape().DebugString());
        }
        const int32 size = tensor_size->scalar<int32>()();
        if (size < 0) {
          return errors::InvalidArgument("Size should be >= 0.");
        }
    
        TensorArray* tensor_array = new TensorArray(
            cinfo_.name(), dtype_, *tensor_array_output_handle, size, element_shape_,
            dynamic_size_, false /* multiple_writes_aggregate */,
            false /* is_grad */, -1 /* marked_size */, clear_after_read_);
    
        // TODO: could use LookupOrCreate instead...
        TF_RETURN_IF_ERROR(
            rm->Create(cinfo_.container(), cinfo_.name(), tensor_array));
    
        *output_tensor_array = tensor_array;
    
        return Status::OK();
      }

      mutex mu_;
      ContainerInfo cinfo_ GUARDED_BY(mu_);
      PersistentTensor handle_ GUARDED_BY(mu_);
      TensorArray* resource_ GUARDED_BY(mu_) = nullptr;
      
      const DeviceType device_type_;
      DataType dtype_;
      PartialTensorShape element_shape_;
      bool dynamic_size_;
      bool clear_after_read_;
      string tensor_array_name_;  // The name used to create the TensorArray.
      
      TF_DISALLOW_COPY_AND_ASSIGN(GlobalTensorArrayOp);
    };
    
    REGISTER_KERNEL_BUILDER(Name("GlobalTensorArray").Device(DEVICE_CPU), GlobalTensorArrayOp);

  """

  def __init__(self):
    self._mod = None

  def _make_mod(self):
    if self._mod:
      return self._mod

    comp = OpCodeCompiler(
      base_name="GlobalTensorArray",
      code_version=1,  # code also ends up in hash, thus this doesn't always needs to be increased
      code=self.code,
      include_deps=[],
      ld_flags=[])

    mod = comp.load_tf_module()
    self._mod = mod
    return mod

  def get_op(self):
    mod = self._make_mod()
    from Util import camel_case_to_snake_case
    op = getattr(mod, camel_case_to_snake_case("GlobalTensorArray"))
    return op


class TFArrayContainer(object):
  """
  Array container, like std::vector, with random index access.

  Currently does not work.
  See https://github.com/tensorflow/tensorflow/issues/10950,
  and test_TFArrayContainer().
  Bug #10950 is fixed upstream, should be in TF 1.2.2.

  An alternative to this could be :class:`GlobalTensorArrayOpMaker`
  and `MapStagingArea <https://github.com/tensorflow/tensorflow/pull/9686>`_,
  which should get into TF 1.2.2.
  """

  code = """
    #include <vector>

    // For Eigen::ThreadPoolDevice.
    #define EIGEN_USE_THREADS 1

    #include "tensorflow/core/framework/op.h"
    #include "tensorflow/core/framework/shape_inference.h"
    #include "tensorflow/core/framework/op_kernel.h"
    #include "tensorflow/core/framework/resource_mgr.h"
    #include "tensorflow/core/framework/resource_op_kernel.h"
    #include "tensorflow/core/framework/tensor.h"
    #include "tensorflow/core/framework/tensor_shape.h"
    #include "tensorflow/core/framework/types.h"
    #include "tensorflow/core/platform/macros.h"
    #include "tensorflow/core/platform/mutex.h"
    #include "tensorflow/core/platform/types.h"

    using namespace tensorflow;

    REGISTER_OP("ArrayContainerCreate")
    .Attr("T: type")
    .Attr("container: string = ''")
    .Attr("shared_name: string = ''")
    .Output("resource: resource")
    .SetIsStateful()
    .SetShapeFn(shape_inference::ScalarShape)
    .Doc(R"doc(Array container, random index access)doc");

    REGISTER_OP("ArrayContainerGetSize")
    .Input("handle: resource")
    .Output("out: int32")
    .SetShapeFn(shape_inference::ScalarShape)
    ;

    REGISTER_OP("ArrayContainerSetSize")
    .Attr("T: type")
    .Input("handle: resource")
    .Input("size: int32")
    ;

    REGISTER_OP("ArrayContainerGet")
    .Attr("T: type")
    .Input("handle: resource")
    .Input("index: int32")
    .Output("out: T")
    ;

    REGISTER_OP("ArrayContainerSet")
    .Attr("T: type")
    .Input("handle: resource")
    .Input("index: int32")
    .Input("value: T")
    ;

    // https://github.com/tensorflow/tensorflow/blob/master/tensorflow/core/framework/resource_mgr.h
    struct ArrayContainer : public ResourceBase {
      ArrayContainer(const DataType& dtype) : dtype_(dtype) {}

      string DebugString() override { return "ArrayContainer"; }
      int64 MemoryUsed() const override { return 0; };

      mutex mu_;
      const DataType dtype_;
      std::vector<PersistentTensor> data_ GUARDED_BY(mu_);

      int32 get_size() {
        mutex_lock l(mu_);
        return (int32) data_.size();
      }

      Status set_size(int32 size) {
        if(size < 0)
          return errors::InvalidArgument("size ", size, " must be >= 0");
        mutex_lock l(mu_);
        data_.resize((size_t) size);
        return Status::OK();
      }

      Status get(OpKernelContext* ctx, int32 idx, PersistentTensor* v) {
        mutex_lock l(mu_);
        if(idx < 0)
          return errors::InvalidArgument("idx ", idx, " must be >= 0");
        if((size_t)idx >= data_.size())
          return errors::InvalidArgument("idx ", idx, " must be < size ", data_.size());
        PersistentTensor& t = data_[(size_t)idx];
        if(!t.IsInitialized())
          return errors::InvalidArgument("tensor at idx ", idx, " must have been set before");
        *v = t;
        return Status::OK();
      }

      Status set(OpKernelContext* ctx, int32 idx, const Tensor& v) {
        mutex_lock l(mu_);
        if(idx < 0)
          return errors::InvalidArgument("idx ", idx, " must be >= 0");
        if((size_t)idx >= data_.size())
          return errors::InvalidArgument("idx ", idx, " must be < size ", data_.size());
        data_[idx] = PersistentTensor(v);
        return Status::OK();
      }

    };

    ResourceHandle OwnMakeResourceHandle(OpKernelContext* ctx, const string& container,
                                         const string& name,
                                         const TypeIndex& type_index) {
      ResourceHandle result;
      result.set_device(ctx->device()->attributes().name());
      printf("make dev %s\\n", result.device().c_str());
      string actual_container;
      if (!container.empty()) {
        actual_container = container;
      } else {
        actual_container = ctx->resource_manager()->default_container();
      }
      result.set_container(actual_container);
      result.set_name(name);
      result.set_hash_code(type_index.hash_code());
      result.set_maybe_type_name(type_index.name());
      printf("make dev %s\\n", result.device().c_str());
      return result;
    }

    // https://github.com/tensorflow/tensorflow/blob/master/tensorflow/core/framework/resource_op_kernel.h
    class ArrayContainerCreateOp : public ResourceOpKernel<ArrayContainer> {
    public:
      explicit ArrayContainerCreateOp(OpKernelConstruction* context) : ResourceOpKernel(context) {
        OP_REQUIRES_OK(context, context->GetAttr("T", &dtype_));
      }

      void Compute(OpKernelContext* context) override {
        ResourceOpKernel<ArrayContainer>::Compute(context);
        mutex_lock l(mu_);
        ResourceHandle rhandle = OwnMakeResourceHandle(context, cinfo_.container(), cinfo_.name(), MakeTypeIndex<ArrayContainer>());
        printf("created. device: %s\\n", rhandle.device().c_str());
        printf("container: %s\\n", rhandle.container().c_str());
        printf("name: %s\\n", rhandle.name().c_str());
        printf("actual device: %s\\n", context->device()->attributes().name().c_str());
        printf("actual name: %s\\n", cinfo_.name().c_str());
        rhandle.set_device("foo");
        printf("now device: %s\\n", rhandle.device().c_str());
        ResourceHandle cpy = rhandle;
        printf("cpy device: %s\\n", cpy.device().c_str());
      }
      
    private:
      virtual bool IsCancellable() const { return false; }
      virtual void Cancel() {}

      Status CreateResource(ArrayContainer** ret) override EXCLUSIVE_LOCKS_REQUIRED(mu_) {
        *ret = new ArrayContainer(dtype_);
        if(*ret == nullptr)
          return errors::ResourceExhausted("Failed to allocate");
        return Status::OK();
      }

      Status VerifyResource(ArrayContainer* ar) override {
        if(ar->dtype_ != dtype_)
          return errors::InvalidArgument("Data type mismatch: expected ", DataTypeString(dtype_),
                                         " but got ", DataTypeString(ar->dtype_), ".");
        return Status::OK();
      }
  
      DataType dtype_;
    };
    REGISTER_KERNEL_BUILDER(Name("ArrayContainerCreate").Device(DEVICE_CPU), ArrayContainerCreateOp);

    class ArrayContainerGetSizeOp : public OpKernel {
    public:
      using OpKernel::OpKernel;

      void Compute(OpKernelContext* context) override {
        ArrayContainer* ar;
        
        const Tensor* handle;
        OP_REQUIRES_OK(context, context->input("handle", &handle));
        const ResourceHandle& rhandle = handle->scalar<ResourceHandle>()();
        printf("device: %s\\n", rhandle.device().c_str());
        printf("container: %s\\n", rhandle.container().c_str());
        printf("name: %s\\n", rhandle.name().c_str());
        
        OP_REQUIRES_OK(context, GetResourceFromContext(context, "handle", &ar));
        core::ScopedUnref unref(ar);

        int32 size = ar->get_size();
        Tensor* tensor_size = nullptr;
        OP_REQUIRES_OK(context, context->allocate_output(0, TensorShape({}), &tensor_size));
        tensor_size->flat<int32>().setConstant(size);
      }
    };
    REGISTER_KERNEL_BUILDER(Name("ArrayContainerGetSize").Device(DEVICE_CPU), ArrayContainerGetSizeOp);

    class ArrayContainerSetSizeOp : public OpKernel {
    public:
      using OpKernel::OpKernel;

      void Compute(OpKernelContext* context) override {
        ArrayContainer* ar;
        OP_REQUIRES_OK(context, GetResourceFromContext(context, "handle", &ar));
        core::ScopedUnref unref(ar);

        const Tensor* tensor_size;
        OP_REQUIRES_OK(context, context->input("size", &tensor_size));
        OP_REQUIRES(context, TensorShapeUtils::IsScalar(tensor_size->shape()),
                    errors::InvalidArgument(
                        "TensorArray index must be scalar, but had shape: ",
                        tensor_size->shape().DebugString()));
        const int32 size = tensor_size->scalar<int32>()();
        OP_REQUIRES_OK(context, ar->set_size(size));
      }
    };
    REGISTER_KERNEL_BUILDER(Name("ArrayContainerSetSize").Device(DEVICE_CPU), ArrayContainerSetSizeOp);

    class ArrayContainerGetOp : public OpKernel {
    public:
      explicit ArrayContainerGetOp(OpKernelConstruction* context) : OpKernel(context) {
        OP_REQUIRES_OK(context, context->GetAttr("T", &dtype_));
      }

      void Compute(OpKernelContext* context) override {
        ArrayContainer* ar;
        OP_REQUIRES_OK(context, GetResourceFromContext(context, "handle", &ar));
        core::ScopedUnref unref(ar);

        const Tensor* tensor_index;
        OP_REQUIRES_OK(context, context->input("index", &tensor_index));
        OP_REQUIRES(context, TensorShapeUtils::IsScalar(tensor_index->shape()),
                    errors::InvalidArgument(
                        "TensorArray index must be scalar, but had shape: ",
                        tensor_index->shape().DebugString()));
        const int32 index = tensor_index->scalar<int32>()();

        PersistentTensor value;
        OP_REQUIRES_OK(context, ar->get(context, index, &value));
        context->set_output(0, *value.AccessTensor(context));
      }

    private:
      DataType dtype_;
    };
    REGISTER_KERNEL_BUILDER(Name("ArrayContainerGet").Device(DEVICE_CPU), ArrayContainerGetOp);

    class ArrayContainerSetOp : public OpKernel {
    public:
      explicit ArrayContainerSetOp(OpKernelConstruction* context) : OpKernel(context) {
        OP_REQUIRES_OK(context, context->GetAttr("T", &dtype_));
      }

      void Compute(OpKernelContext* context) override {
        ArrayContainer* ar;
        OP_REQUIRES_OK(context, GetResourceFromContext(context, "handle", &ar));
        core::ScopedUnref unref(ar);

        const Tensor* tensor_index;
        const Tensor* tensor_value;
        OP_REQUIRES_OK(context, context->input("index", &tensor_index));
        OP_REQUIRES_OK(context, context->input("value", &tensor_value));
    
        OP_REQUIRES(context, TensorShapeUtils::IsScalar(tensor_index->shape()),
                    errors::InvalidArgument(
                        "index must be scalar, but had shape: ",
                        tensor_index->shape().DebugString()));
        const int32 index = tensor_index->scalar<int32>()();
        OP_REQUIRES(context, tensor_value->IsInitialized(), errors::InvalidArgument("value must be initialized"));

        OP_REQUIRES_OK(context, ar->set(context, index, *tensor_value));
      }

    private:
      DataType dtype_;
    };
    REGISTER_KERNEL_BUILDER(Name("ArrayContainerSet").Device(DEVICE_CPU), ArrayContainerSetOp);
  """

  _mod = None

  def __init__(self, dtype, handle=None, container=None, shared_name=None, name="array_container"):
    """
    :param tf.DType dtype:
    :param str container:
    :param str shared_name:
    :param str name:
    :param tf.resource handle: existing handle to reuse. otherwise we will create a new one
    """
    self.dtype = dtype
    if handle is not None:
      self.handle = handle
    else:
      self.handle = self._create(dtype=dtype, container=container, shared_name=shared_name, name=name)

  def __repr__(self):
    return "<%s %r %r>" % (self.__class__.__name__, self.dtype, self.handle)

  @classmethod
  def _make_mod(cls):
    if cls._mod:
      return cls._mod

    # Fix for undefined symbol: _ZN6google8protobuf8internal26fixed_address_empty_stringE.
    # https://github.com/tensorflow/tensorflow/issues/1419
    # noinspection PyUnresolvedReferences
    from google.protobuf.pyext import _message as msg
    lib = msg.__file__
    #lib = "/u/zeyer/.local/lib/python2.7/site-packages/tensorflow/python/_pywrap_tensorflow_internal.so"
    #lib = "/u/zeyer/.local/lib/python2.7/site-packages/tensorflow/contrib/tfprof/python/tools/tfprof/_pywrap_tensorflow_print_model_analysis_lib.so"
    #lib = "/u/zeyer/.local/lib/python2.7/site-packages/google/protobuf/pyext/_message.so"
    #lib = "/u/zeyer/.local/lib/python2.7/site-packages/external/protobuf/python/google/protobuf/pyext/_message.so"

    comp = OpCodeCompiler(
      base_name="TFArrayContainer",
      code_version=1,  # code also ends up in hash, thus this doesn't always needs to be increased
      code=cls.code,
      include_deps=[],
      use_cuda_if_available=False,
      ld_flags=[
        "-Xlinker", "-rpath", "-Xlinker", os.path.dirname(lib),
        "-L", os.path.dirname(lib), "-l", ":" + os.path.basename(lib)])

    mod = comp.load_tf_module()
    cls._mod = mod
    return mod

  def _get_op(self, k):
    mod = self._make_mod()
    from Util import camel_case_to_snake_case
    return getattr(mod, camel_case_to_snake_case(k))

  def _create(self, dtype, container=None, shared_name=None, name="array_container"):
    """
    :param tf.DType dtype:
    :param str container:
    :param str shared_name:
    :param str name:
    :return: handle to ArrayContainer
    :rtype: tf.resource
    """
    op = self._get_op("ArrayContainerCreate")
    return op(T=dtype, container=container, shared_name=shared_name, name=name)

  def get_size(self):
    """
    :return: size int32 scalar
    :rtype: tf.Tensor
    """
    op = self._get_op("ArrayContainerGetSize")
    return op(handle=self.handle)

  def set_size(self, size):
    """
    :param tf.Tensor size:
    :return: operation
    :rtype: tf.Operation
    """
    op = self._get_op("ArrayContainerSetSize")
    return op(handle=self.handle, size=size)

  def get(self, index):
    """
    :param tf.Tensor index: >= 0 and < size
    :return: tensor at that index
    :rtype: tf.Tensor
    """
    op = self._get_op("ArrayContainerGet")
    return op(T=self.dtype, handle=self.handle, index=index)

  def set(self, index, value):
    """
    :param tf.Tensor index: >= 0 and < size
    :param tf.Tensor value:
    :return: operation
    :rtype: tf.Operation
    """
    op = self._get_op("ArrayContainerSet")
    return op(T=self.dtype, handle=self.handle, index=index, value=value)


class ExplicitRandomShuffleQueue(object):
  """
  This is intended to behave very much like tf.RandomShuffleQueue,
  except that it's implemented by other TF native ops / data structures,
  and you can change min_after_dequeue at runtime.
  This means that if you have your own logic about when to end,
  you can set min_after_dequeue=0 and dequeue all the remaining entries from the queue,
  and then later increase min_after_dequeue again.
  You can also start with a small min_after_dequeue and increase the number steadily.
  The original tf.RandomShuffleQueue had the effect of a reset min_after_dequeue=0
  after you closed the queue. However, there was no way to reopen the queue.
  That is the whole reason this implementation exists.

  One difference of this implementation is that you must call the init() op once before usage.

  One way to implement this is in pure TF.
  We need some TF container type which supports having entries of different shapes
  (where the shape can differ where-ever we specified None).
  We also need some TF container which we can access by index.
  tf.TensorArray can handle that.

  Another way to implement this is by multiple stateful tf.py_func which all reference this instance.
  """

  def __init__(self, capacity, min_after_dequeue=0, dtypes=None, shapes=None,
               names=None, seed=None, shared_name=None,
               name="explicit_random_shuffle_queue"):
    """
    :param int capacity:
    :param int|tf.Tensor min_after_dequeue:
    :param list[str|tf.DType] dtypes:
    :param list[tuple[int|tf.Tensor|None]] shapes:
    :param list[str]|None names:
    :param int seed:
    :param str|None shared_name:
    :param str name:
    """
    assert dtypes
    assert not shared_name, "not supported yet"
    assert isinstance(dtypes, list)
    self.dtypes = dtypes
    if shapes is None:
      shapes = [None] * len(dtypes)
    assert isinstance(shapes, list)
    self.shapes = shapes
    assert len(shapes) == len(dtypes)
    if names is not None:
      assert isinstance(names, list)
      assert len(names) == len(dtypes)
    self.names = names
    self._name = name
    self._seed = seed

    with tf.name_scope(self._name):
      self._lock = Lock()
      self._is_full_cond = Condition(lock=self._lock)
      self._min_after_dequeue_cond = Condition(lock=self._lock)

      self.capacity = capacity
      self._min_after_dequeue = tf.Variable(
        initial_value=min_after_dequeue, dtype=tf.int32, trainable=False, name="min_after_dequeue")

      self._is_written = tf.Variable(
        initial_value=tf.zeros(shape=(self.capacity,), dtype=tf.int8), trainable=False, name="free_mask")

      with tf.control_dependencies([self._min_after_dequeue.initializer]):
        self._init_ops = tf.group(self._is_written.initializer)
      self._init_ops = tf.group(
        self._init_ops, self._lock.init(), self._is_full_cond.init(), self._min_after_dequeue_cond.init())

      # TODO Seems like we cannot use tf.TensorArray for what we need here...
      # see test_TensorArray() and https://stackoverflow.com/questions/44418036/
      # Solutions are GlobalTensorArrayOpMaker or TFArrayContainer which also both currently do not work.
      # Thus at the moment, I don't see any good way to make this work...
      # TODO Another option might be MapStagingArea (https://github.com/tensorflow/tensorflow/pull/9686).
      # This should get into TF 1.2.2.
      self._tas = [
        tf.TensorArray(
          dtype=dtype, size=capacity, clear_after_read=True,
          element_shape=shape, name="%s_TensorArray" % name)
        for (dtype, shape, name) in zip(self.dtypes, self.shapes, self.names or ["unk"] * len(self.dtypes))]
      self._flows = [tf.Variable(initial_value=ta.flow) for ta in self._tas]
      self._init_ops = tf.group(self._init_ops, *[flow.initializer for flow in self._flows])
      assert len(self._tas) == len(self.dtypes)
      self._tas_dict = {name: ta for (name, ta) in zip(self.names, self._tas)} if self.names else None

  def init(self):
    """
    :rtype: tf.Operation
    """
    return self._init_ops

  def size(self):
    """
    :rtype: tf.Tensor
    """
    with reuse_name_scope("%s/size" % self._name):
      return tf.count_nonzero(self._is_written, dtype=tf.int32)

  def min_after_dequeue_read(self):
    return enforce_copy(self._min_after_dequeue.read_value())

  def min_after_dequeue_assign(self, min_after_dequeue):
    """
    :param tf.Tensor min_after_dequeue:
    :rtype: tf.Operation
    """
    with sequential_control_dependencies([
      lambda: self._lock.lock(),
      lambda: self._min_after_dequeue.assign(min_after_dequeue, use_locking=True),
      lambda: self._min_after_dequeue_cond.signal_all(),
      lambda: self._lock.unlock()
    ]):
      return tf.no_op()

  def _get_cur_tensor_array(self, idx):
    ta = self._tas[idx]
    return tf.TensorArray(dtype=ta.dtype, handle=ta.handle, flow=enforce_copy(self._flows[idx].read_value()))

  def _get_cur_tas(self):
    return [self._get_cur_tensor_array(i) for i in range(len(self._tas))]

  def _tas_write(self, index, vs):
    tas = self._get_cur_tas()
    assert len(vs) == len(tas)
    tas_flows = [ta.write(index, v).flow for (ta, v) in zip(tas, vs)]
    return [tf.assign(flow_var, flow) for (flow_var, flow) in zip(self._flows, tas_flows)]

  def _tas_read(self, index):
    tas = self._get_cur_tas()
    return [ta.read(index) for ta in tas]

  def enqueue(self, v):
    """
    :param list[tf.Tensor]|dict[str,tf.Tensor]|tf.Tensor v:
    :rtype: tf.Operation
    """
    if self.names:
      assert isinstance(v, dict)
      v = [v[name] for name in self.names]
    elif not isinstance(v, list) and len(self.dtypes) == 1:
      v = [v]
    assert isinstance(v, list)
    assert len(v) == len(self.dtypes)
    with reuse_name_scope("%s/enqueue" % self._name):
      with tf.control_dependencies([self._lock.lock()]):
        with tf.control_dependencies([self._loop_while_full()]):
          index = tf.cast(tf.arg_min(self._is_written, dimension=0), tf.int32)
          with tf.control_dependencies([tf.scatter_update(self._is_written, index, 1)]):
            with tf.control_dependencies(self._tas_write(index=index, vs=v)):
              with tf.control_dependencies([self._maybe_signal_min_after_dequeue()]):
                return self._lock.unlock()

  def _is_full(self):
    return tf.greater_equal(self.size(), self.capacity, name="is_full")

  def _loop_while_full(self):
    """
    Called with lock held.
    """
    def loop_cond(last):
      with tf.control_dependencies([last]):
        return self._is_full()

    def body(last):
      # This gets only executed if the queue is full. We still have the lock.
      with tf.control_dependencies([last]):
        with tf.control_dependencies([self._is_full_cond.wait()]):
          return tf.identity(last)

    return tf.while_loop(
      name="loop_while_full", cond=loop_cond, body=body, loop_vars=[0], parallel_iterations=1, back_prop=False)

  def _have_min_after_dequeue(self):
    return tf.greater_equal(self.size(), self._min_after_dequeue, name="have_min_after_dequeue")

  def _maybe_signal_min_after_dequeue(self):
    return tf.cond(self._have_min_after_dequeue(), lambda: self._min_after_dequeue_cond.signal(), lambda: tf.no_op(), name="maybe_signal_min_after_dequeue")

  def _loop_while_not_min_after_dequeue(self):
    """
    Called with lock held.
    """
    def loop_cond(last):
      with tf.control_dependencies([last]):
        return tf.logical_not(self._have_min_after_dequeue())

    def body(last):
      # This gets only executed if we not have min-after-dequeue. We still have the lock.
      with tf.control_dependencies([last]):
        with tf.control_dependencies([self._min_after_dequeue_cond.wait()]):
          return tf.identity(last)

    return tf.while_loop(
      name="loop_while_not_min_after_dequeue",
      cond=loop_cond, body=body, loop_vars=[0], parallel_iterations=1, back_prop=False)

  def dequeue(self):
    with reuse_name_scope("%s/dequeue" % self._name):
      with tf.control_dependencies([self._lock.lock()]):
        with tf.control_dependencies([self._loop_while_not_min_after_dequeue()]):
          free_idxs = tf.cast(tf.where(tf.equal(self._is_written, 1)), tf.int32)  # (num_true, 1)
          free_idxs = tf.random_shuffle(free_idxs, seed=self._seed)
          index = free_idxs[0][0]
          vs = self._tas_read(index)
          with tf.control_dependencies(vs):
            with tf.control_dependencies([tf.scatter_update(self._is_written, index, 0)]):
              with tf.control_dependencies([self._is_full_cond.signal()]):
                with tf.control_dependencies([self._lock.unlock()]):
                  vs = [tf.identity(v) for v in vs]
                  if self.names:
                    return {name: v for (name, v) in zip(self.names, vs)}
                  elif len(vs) == 1:
                    return vs[0]
                  else:
                    return vs


def mem_usage_for_dev(dev_name):
  """
  :param str dev_name: e.g. "/cpu:0" or "/gpu:0"
  :return: int scalar, which is the peak memory usage in bytes of the given device
  :rtype: tf.Tensor

  This function will not create multiple nodes in the graph for multiple calls.
  Currently only works for GPU devices.
  """
  def get():
    from tensorflow.contrib import memory_stats
    try:
      bytes_in_use = memory_stats.BytesInUse  # since TF 1.4.0
    except AttributeError:
      bytes_in_use = memory_stats.MaxBytesInUse
    with tf.device(dev_name):
      return bytes_in_use()

  assert dev_name.startswith("/")  # e.g. "/cpu:0" or "/gpu:0"
  scope_name = dev_name[1:].replace(":", "")  # e.g. "cpu0" or "gpu0"
  return global_tensor(get, "mem_usage_%s" % scope_name)


def identity_with_debug_log(x, args, out, name="DebugLogOp"):
  """
  :param tf.Tensor x:
  :param dict[str,tf.Tensor|None] args:
  :param list[dict[str,numpy.ndarray]] out:
  :param str name:
  :return: x
  :rtype: tf.Tensor
  """
  from Util import dict_joined
  none_args = {k: None for (k, v) in args.items() if v is None}
  arg_keys = sorted([k for k in args.keys() if k not in none_args])

  def py_func(x, *arg_values):
    out.append(dict_joined(dict(zip(arg_keys, arg_values)), none_args))
    return x

  with tf.name_scope(name):
    y, = tf.py_func(
      py_func, [x] + [args[k] for k in arg_keys], [x.dtype], stateful=True)
    with tf.control_dependencies([y]):
      return tf.identity(x)


def nested_get_shapes(x):
  """
  :param tf.Tensor|dict[str,tf.Tensor]|list[tf.Tensor]|object x: anything that nest supports
  :return: same structure as x, but tf.TensorShape for each tensor
  """
  if isinstance(x, tf.Tensor):
    return x.get_shape()
  if isinstance(x, list):
    return [nested_get_shapes(v) for v in x]
  if isinstance(x, tuple):
    if type(x) is not tuple:  # assume namedtuple
      return type(x)(*[nested_get_shapes(v) for v in x])
    return tuple([nested_get_shapes(v) for v in x])
  if isinstance(x, dict):
    return {k: nested_get_shapes(v) for (k, v) in x.items()}
  raise TypeError("invalid type %r of %r" % (type(x), x))


@contextlib.contextmanager
def same_context(x):
  """
  Will use the same context as `x`.
  E.g. if `x` is a constant, it can be outside the loop,
  so we will yield a context which is not inside the loop.

  :param tf.Tensor|int|float|None|list[tf.Tensor|int|float] x:
  :return: yields context (via tf.control_dependencies)
  """
  def get_tensors(v):
    if isinstance(v, (list, tuple)):
      for elem in v:
        for t in get_tensors(elem):
          yield t
      return
    if isinstance(v, (int, float, type(None))):
      return
    assert isinstance(v, tf.Tensor), "unexpected type %r" % type(v)
    yield v
  tensors = list(get_tensors(x))
  if not tensors:
    # No TF tensor given, so we can consider the input as constant
    # and can use a clean context (e.g. not within the current loop or so).
    with tf.control_dependencies(None) as dep:  # this will reset the context
      yield dep
    return
  # There is currently no good way to get into the same context.
  # Just keep using the current context.
  yield None


def select_src_beams(x, src_beams):
  """
  :param tf.Tensor x: (batch * src-beam, ...)
  :param tf.Tensor src_beams: (batch, beam) -> src-beam-idx
  :return: (batch * beam, ...)
  :rtype: tf.Tensor
  """
  assert isinstance(x, tf.Tensor)
  assert isinstance(src_beams, tf.Tensor)
  with tf.name_scope("select_src_beams"):
    src_beams.set_shape(tf.TensorShape([None, None]))
    src_beams_shape = tf.shape(src_beams)
    batch_dim, beam_dim = src_beams_shape[0], src_beams_shape[1]
    x_ndim = x.get_shape().ndims
    assert x_ndim is not None
    x_shape = tf.shape(x)
    x_shape_rem = [x_shape[i] for i in range(1, x_ndim)]
    src_beam_dim = x_shape[0] // batch_dim
    x = tf.reshape(x, [batch_dim, src_beam_dim] + x_shape_rem)  # (batch, src-beam, ...)
    indices = nd_indices(src_beams)  # (batch, beam, 2)
    x = tf.gather_nd(x, indices=indices)  # K=2, (batch, beam, ...)
    x = tf.reshape(x, [batch_dim * beam_dim] + x_shape_rem)
    return x


def filter_ended_scores(x, end_flags, batch_dim=None, dim=None, score_zero=0.0, score_rem=-1.e30):
  """
  This can e.g. used before tf.nn.top_k to let only one beam through for an ended hypothesis.
  Then, batch would also include the beam size, which does not matter here.

  :param tf.Tensor x: (batch, dim)
  :param tf.Tensor end_flags: (batch,)
  :param tf.Tensor|int|None batch_dim:
  :param tf.Tensor|int|None dim:
  :param float score_zero: x[..., 0] will have this score where end_flag is True
  :param float score_rem: x[..., 1:] will have this score where end_flag is False
  :return: filtered x, (batch, dim)
  :rtype: tf.Tensor
  """
  with tf.name_scope("filter_ended_scores"):
    end_flags = check_input_ndim(end_flags, 1)
    x = check_dim_equal(x, 0, end_flags, 0)
    x.set_shape(tf.TensorShape([
      batch_dim if isinstance(batch_dim, int) else None,
      dim if isinstance(dim, int) else None]))
    if batch_dim is None:
      batch_dim = tf.shape(end_flags)[0]
    if dim is None:
      dim = tf.shape(x)[-1]
    with same_context(dim):  # force calculation outside loop if possible
      filter_score = tf.one_hot(
        0, dim, dtype=tf.float32, on_value=score_zero, off_value=score_rem)  # (dim,)
    with same_context([dim, batch_dim]):  # force calculation outside loop if possible
      filter_score = expand_dims_unbroadcast(filter_score, axis=0, dim=batch_dim)  # (batch,dim)
    x = tf.where(end_flags, filter_score, x)
    return x


def to_int32_64(x):
  """
  :param tf.Tensor x: dtype int8, int16, int32, int64
  :rtype: tf.Tensor
  :return: dtype int32 or int64
  """
  if x.dtype in [tf.int32, tf.int64]:
    return x
  assert x.dtype in [tf.int8, tf.int16]
  return tf.cast(x, tf.int32)


# -------------------- BEGIN Segment model related helpers --------------------

def batch_sizes_after_windowing(sizes, window):
  """
  :param tf.Tensor sizes: (batch_sizes)
  :param int window: size of the applied window
  :return: sizes for each batch after applying a window on each batch
  :rtype: tf.Tensor
  """
  def fold_times(acc, x):
    r1 = tf.tile([window], [tf.maximum(x - window + 1, 0)])
    r2 = tf.range(tf.minimum(x, window - 1), 0, -1)
    return tf.concat([acc, r1, r2], 0)
  return tf.foldl(fold_times, sizes, tf.placeholder_with_default(tf.zeros([0], dtype='int32'), [None]), name='fold_sizes')

def batch_indices_after_windowing(sizes, window):
  """
  here we compute the start and end times for each of the new batches when applying a window
  :param tf.Tensor sizes: (batch_sizes)
  :param int window: size of the applied window
  :return: tensor of shape (?, 3), contains batch index, start-frame and end-frame for each batch after applying a window
  :rtype: tf.Tensor
  """
  def fold_batches(acc, x):
    b = x[0]
    l = x[1]
    batch = tf.tile([b], [l])
    start = tf.range(l)
    end   = tf.minimum(tf.range(window, l + window), l)
    return tf.concat([acc, tf.transpose(tf.stack([batch, start, end]))], axis=0)

  return tf.foldl(fold_batches, tf.stack([tf.range(tf.shape(sizes)[0]), sizes], axis=1),
                  tf.placeholder_with_default(tf.zeros([0, 3], dtype='int32'), [None, 3]), name="fold_batches")



# --------------------- END Segment model related helpers ---------------------

