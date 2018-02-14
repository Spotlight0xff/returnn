
# start: nosetests $this_file --nologcapture

import logging
logging.getLogger('tensorflow').disabled = True
import tensorflow as tf
import sys
sys.path += ["."]  # Python 3 hack
from nose.tools import assert_equal, assert_is_instance
import contextlib
import unittest
import numpy.testing
import better_exchook
better_exchook.replace_traceback_format_tb()

from Config import Config
from TFNetwork import *
from TFNetworkLayer import *
from Log import log

log.initialize(verbosity=[5])


@contextlib.contextmanager
def make_scope():
  with tf.Graph().as_default() as graph:
    with tf.Session(graph=graph) as session:
      yield session


def test_activation_layer_net_construct():
  with make_scope() as session:
    num_inputs = 2
    config = Config()
    config.update({
      "num_outputs": 3,
      "num_inputs": num_inputs,
      "network": {
        "output": {"class": "activation", "activation": "relu", "from": ["data"]}
      }})
    network = TFNetwork(config=config, train_flag=True)
    network.construct_from_dict(config.typed_value("network"))
    out = network.get_default_output_layer().output.placeholder
    n_batch = 1
    seq_len = 3
    feed = {network.extern_data.get_default_input_data().placeholder:
            numpy.array([[[0, 0], [-1, -1], [2, 2]]], dtype="float32")}
    assert_equal(feed[network.extern_data.get_default_input_data().placeholder].shape, (n_batch, seq_len, num_inputs))
    v = session.run(out, feed_dict=feed)
    assert_equal(v.shape, (n_batch, seq_len, num_inputs))
    assert_equal(v.tolist(), [[[0, 0], [0, 0], [2, 2]]])


def test_activation_layer_net_construct_two_out():
  with make_scope() as session:
    num_inputs = 2
    config = Config()
    config.update({
      "num_outputs": 3,
      "num_inputs": num_inputs,
      "network": {
        "0out": {"class": "linear", "n_out": 1, "activation": "relu", "from": ["data"], "is_output_layer": True},
        "output": {"class": "activation", "activation": "relu", "from": ["data"]}
      }})
    network = TFNetwork(config=config, train_flag=True)
    network.construct_from_dict(config.typed_value("network"))
    session.run(tf.global_variables_initializer())
    out = network.layers["output"].output.placeholder
    out2 = network.layers["0out"].output.placeholder
    n_batch = 1
    seq_len = 3
    feed = {network.extern_data.get_default_input_data().placeholder:
            numpy.array([[[0, 0], [-1, -1], [2, 2]]], dtype="float32")}
    assert_equal(feed[network.extern_data.get_default_input_data().placeholder].shape, (n_batch, seq_len, num_inputs))
    v, v2 = session.run([out, out2], feed_dict=feed)
    assert_equal(v.shape, (n_batch, seq_len, num_inputs))
    assert_equal(v.tolist(), [[[0, 0], [0, 0], [2, 2]]])


def test_combine_layer_net_construct():
  with make_scope() as session:
    net_dict = {
      "lstm0_fw": {"class": "rec", "unit": "lstmp", "n_out": 5, "dropout": 0.0, "L2": 0.01, "direction": 1},
      "lstm0_bw": {"class": "rec", "unit": "lstmp", "n_out": 5, "dropout": 0.0, "L2": 0.01, "direction": -1},
      "lstm0_avg": {"class": "combine", "kind": "average", "from": ["lstm0_fw", "lstm0_bw"], "trainable": False},
      "output": {"class": "softmax", "loss": "ce", "from": ["lstm0_avg"]}
    }
    config = Config()
    config.update(dict(num_inputs=4, num_outputs=9))
    network = TFNetwork(config=config, train_flag=True)
    network.construct_from_dict(net_dict)


def test_subnetwork_layer_net_construct():
  with make_scope() as session:
    net_dict = {
      "ff0": {"class": "forward", "activation": "tanh", "n_out": 3},
      "sub": {"class": "subnetwork", "from": ["ff0"], "subnetwork": {
        "ff1": {"class": "forward", "activation": "relu", "n_out": 2},
        "output": {"class": "forward", "activation": "relu", "n_out": 2}
      }},
      "output": {"class": "softmax", "loss": "ce", "from": ["sub"]}
    }
    config = Config()
    config.update(dict(num_inputs=4, num_outputs=3))
    network = TFNetwork(config=config, train_flag=True)
    network.construct_from_dict(net_dict)
    assert_equal(network.layers["sub"].output.dim, 2)


def test_constant_layer():
  with make_scope() as session:
    config = Config()
    config.update({
      "num_outputs": 3,
      "num_inputs": 2,
      "network": {
        "output": {"class": "constant", "value": 42, "from": []}
      }
    })
    network = TFNetwork(config=config, train_flag=True)
    network.construct_from_dict(config.typed_dict["network"])
    out = network.get_default_output_layer(must_exist=True)
    v = session.run(out.output.placeholder)
    assert_equal(v.shape, (1,))  # (batch,), where batch==1 for broadcasting
    assert_equal(v[0], 42)


def test_compare_layer():
  with make_scope() as session:
    config = Config()
    config.update({
      "model": "/tmp/model",
      "num_outputs": 3,
      "num_inputs": 2,
      "network": {
        "const": {"class": "constant", "value": 3, "from": []},
        "output": {"class": "compare", "from": ["const"], "value": 3}
      }
    })
    network = TFNetwork(config=config, train_flag=True)
    network.construct_from_dict(config.typed_dict["network"])
    out = network.get_default_output_layer(must_exist=True)
    v = session.run(out.output.placeholder)
    assert_equal(v.shape, (1,))  # (batch,), where batch==1 for broadcasting
    assert_equal(v.dtype, numpy.dtype("bool"))
    assert_equal(v[0], True)


def test_shift_layer():
  with make_scope() as session:
    import numpy as np
    batch_size = 8
    time_size = 20
    feat_size = 10
    shift_amount = 5  # right-shift of 5 elements
    config = Config()
    config.update({
      "num_outputs": feat_size,
      "num_inputs": feat_size,
      "network": {
        "output": {"class": "shift_axis", "from": ["data"], "amount": shift_amount, "pad": True, "axis": "T"}
      }
    })
    network = TFNetwork(config=config, train_flag=True)
    network.construct_from_dict(config.typed_dict["network"])
    out = network.get_default_output_layer(must_exist=True)
    input_data = np.ones(shape=(batch_size, time_size, feat_size))
    input_data[0, :, 0] = np.arange(time_size) # 0..time_size in time-axis
    feed_dict = {network.layers['data'].output.placeholder: input_data}
    v = session.run(out.output.placeholder, feed_dict)

    assert_equal(v.shape, (batch_size, time_size, feat_size))
    assert_equal(np.equal(v[0, shift_amount:, 0], np.arange(time_size-shift_amount)).all(), True)
    assert_equal((v[:,:shift_amount,:] == 0).all(), True)  # padding
    assert_equal((v[1:,shift_amount:,:] == 1).all(), True)


def test_layer_base_get_out_data_from_opts():
  with make_scope() as session:
    config = Config()
    config.update({
      "num_inputs": 4,
      "num_outputs": 3
    })
    network = TFNetwork(config=config)
    input_data = network.extern_data.data["data"]
    target_data = network.extern_data.data["classes"]
    assert input_data.dim == 4
    assert input_data.shape == (None, 4)
    assert not input_data.sparse
    assert input_data.dtype == "float32"
    assert target_data.dim == 3
    assert target_data.shape == (None,)
    assert target_data.sparse
    assert target_data.dtype == "int32"
    out = LayerBase._base_get_out_data_from_opts(network=network, name="output", target="classes")
    # Output data type is a non-sparse version of the targets by default.
    assert out.dim == target_data.dim
    assert out.shape == target_data.shape_dense
    assert not out.sparse
    assert out.dtype == "float32"


def test_SplitDimsLayer_resolve_dims():
  assert_equal(SplitDimsLayer._resolve_dims(old_dim=3 * 5, new_dims=(3, -1)), (3, 5))
  assert_equal(SplitDimsLayer._resolve_dims(old_dim=3 * 5, new_dims=(3, 5)), (3, 5))
  assert_equal(SplitDimsLayer._resolve_dims(old_dim=3 * 5, new_dims=(5, -1)), (5, 3))
  assert_equal(SplitDimsLayer._resolve_dims(old_dim=2 * 3 * 5, new_dims=(-1, 3, 5)), (2, 3, 5))
  assert_equal(SplitDimsLayer._resolve_dims(old_dim=2 * 3 * 5, new_dims=(2, -1, 5)), (2, 3, 5))
  assert_equal(SplitDimsLayer._resolve_dims(old_dim=2 * 3 * 5, new_dims=(2, 3, -1)), (2, 3, 5))
  assert_equal(SplitDimsLayer._resolve_dims(old_dim=2 * 3 * 5, new_dims=(2, 3, -1, 1)), (2, 3, 5, 1))


def test_ConvLayer_get_valid_out_dim():
  assert_equal(ConvLayer.calc_out_dim(in_dim=10, stride=1, filter_size=2, padding="same"), 10)
  assert_equal(ConvLayer.calc_out_dim(in_dim=10, stride=1, filter_size=3, padding="same"), 10)
  assert_equal(ConvLayer.calc_out_dim(in_dim=10, stride=1, filter_size=2, padding="valid"), 9)
  assert_equal(ConvLayer.calc_out_dim(in_dim=10, stride=1, filter_size=3, padding="valid"), 8)
  assert_equal(ConvLayer.calc_out_dim(in_dim=10, stride=2, filter_size=2, padding="valid"), 5)
  assert_equal(ConvLayer.calc_out_dim(in_dim=10, stride=3, filter_size=2, padding="valid"), 3)
  assert_equal(ConvLayer.calc_out_dim(in_dim=10, stride=3, filter_size=1, padding="valid"), 4)
  assert_equal(ConvLayer.calc_out_dim(in_dim=10, stride=3, filter_size=2, padding="same"), 4)
  assert_equal(ConvLayer.calc_out_dim(in_dim=41, stride=1, filter_size=2, padding="valid"), 40)
  assert_equal(ConvLayer.calc_out_dim(in_dim=40, stride=2, filter_size=2, padding="valid"), 20)


def test_reuse_params():
  with make_scope() as session:
    config = Config()
    n_in, n_out = 2, 3
    config.update({
      "num_outputs": n_out,
      "num_inputs": n_in,
      "network": {
        "l1": {"class": "linear", "activation": None, "n_out": n_out},
        "output": {"class": "linear", "activation": None, "n_out": n_out, "reuse_params": "l1"}
      }
    })
    network = TFNetwork(config=config, train_flag=True)
    network.construct_from_dict(config.typed_dict["network"])
    l1 = network.layers["l1"]
    l2 = network.layers["output"]
    assert_equal(set(l1.params.keys()), {"W", "b"})
    assert_equal(set(l2.params.keys()), {"W", "b"})
    assert l1.params["W"] is l2.params["W"]
    assert l1.params["b"] is l2.params["b"]
    assert_equal(set(network.get_trainable_params()), {l1.params["W"], l1.params["b"]})


def test_ResizeLayer_fill_value():
  with make_scope() as session:
    net = TFNetwork(extern_data=ExternData())
    src = InternalLayer(name="src", network=net, out_type={"dim": 20, "sparse": True})
    src.output.placeholder = tf.constant([[1, 2, 3, 4, 5], [6, 7, 8, 9, 10]], dtype=tf.int32)
    src.output.size_placeholder = {0: tf.constant([5, 3], dtype=tf.int32)}
    layer = ResizeLayer(
      name="resize", network=net, factor=3, axis="T", kind="fill", fill_value=19, sources=[src],
      output=ResizeLayer.get_out_data_from_opts(
        name="resize", network=net, factor=3, axis="T", kind="fill", sources=[src]))
    out, seq_lens = session.run([layer.output.placeholder, layer.output.size_placeholder[0]])
    print(out)
    print(seq_lens)
    assert isinstance(out, numpy.ndarray)
    assert isinstance(seq_lens, numpy.ndarray)
    assert_equal(
      out.tolist(),
      [[1, 19, 19,  2, 19, 19,  3, 19, 19,  4, 19, 19,  5, 19, 19,],
       [6, 19, 19,  7, 19, 19,  8, 19, 19,  9, 19, 19, 10, 19, 19]])
    assert_equal(seq_lens.tolist(), [15, 9])


def test_ResizeLayer_fill_dropout():
  with make_scope() as session:
    net = TFNetwork(extern_data=ExternData())
    src = InternalLayer(name="src", network=net, out_type={"dim": 20, "sparse": True})
    src_seqs = [[1, 2, 3, 4, 5], [6, 7, 8, 9, 10]]
    src_seq_lens = [5, 3]
    factor = 3
    fill_value = 19
    src.output.placeholder = tf.constant(src_seqs, dtype=tf.int32)
    src.output.size_placeholder = {0: tf.constant(src_seq_lens, dtype=tf.int32)}
    layer = ResizeLayer(
      name="resize", network=net,
      factor=factor, axis="T", kind="fill", fill_value=fill_value, fill_dropout=0.5, sources=[src],
      output=ResizeLayer.get_out_data_from_opts(
        name="resize", network=net, factor=factor, axis="T", kind="fill", sources=[src]))
    out, seq_lens = session.run([layer.output.placeholder, layer.output.size_placeholder[0]])
    print(out)
    print(seq_lens)
    assert isinstance(out, numpy.ndarray)
    assert isinstance(seq_lens, numpy.ndarray)
    # Non-deterministic output. But we can check some constraints.
    for i in range(len(src_seq_lens)):
      assert src_seq_lens[i] <= seq_lens[i] <= src_seq_lens[i] * factor
      assert_equal([s for s in out[i] if s != fill_value], src_seqs[i])


def test_DotLayer():
  with make_scope() as session:
    B = 2
    H = 3
    D = H * 5
    net = TFNetwork(extern_data=ExternData())
    a = InternalLayer(name="A", network=net, out_type={"shape": (None, H, D // H)})
    assert a.output.batch_dim_axis == 0
    assert a.output.time_dim_axis == 1
    assert a.output.shape == (None, H, D // H)
    assert a.output.dim == D // H
    a_seq_lens = [7, 3]
    assert len(a_seq_lens) == B
    a.output.placeholder = tf.reshape(
      tf.range(B * max(a_seq_lens) * D, dtype=tf.float32), (B, max(a_seq_lens), H, D // H))
    a.output.size_placeholder = {0: tf.constant(a_seq_lens, dtype=tf.int32)}
    b = InternalLayer(name="B", network=net, out_type={"shape": (H, D // H)})
    assert b.output.batch_dim_axis == 0
    assert b.output.shape == (H, D // H)
    assert b.output.dim == D // H
    b.output.placeholder = tf.reshape(tf.add(tf.range(B * D, dtype=tf.float32), 0.5), (B, H, D // H))
    kwargs = dict(
      name="dot", network=net, sources=[a, b], debug=True,
      red1=-1, red2=-1, var1="T", var2=None)
    layer = DotLayer(output=DotLayer.get_out_data_from_opts(**kwargs), **kwargs)
    print(layer, layer.output)
    assert layer.output.batch_dim_axis == 0
    assert layer.output.time_dim_axis == 2
    assert layer.output.shape == (H, None, 1)
    assert layer.output.dim == 1
    out, seq_lens = session.run([layer.output.placeholder, layer.output.size_placeholder[1]])
    print(out)
    print(seq_lens)
    assert isinstance(out, numpy.ndarray)
    assert isinstance(seq_lens, numpy.ndarray)
    assert_equal(seq_lens.tolist(), a_seq_lens)
    assert_equal(out.shape, (B, H, max(a_seq_lens), 1))


if __name__ == "__main__":
  try:
    better_exchook.install()
    if len(sys.argv) <= 1:
      for k, v in sorted(globals().items()):
        if k.startswith("test_"):
          print("-" * 40)
          print("Executing: %s" % k)
          try:
            v()
          except unittest.SkipTest as exc:
            print("SkipTest:", exc)
          print("-" * 40)
      print("Finished all tests.")
    else:
      assert len(sys.argv) >= 2
      for arg in sys.argv[1:]:
        print("Executing: %s" % arg)
        if arg in globals():
          globals()[arg]()  # assume function and execute
        else:
          eval(arg)  # assume Python code and execute
  finally:
    import threading
    #if len(list(threading.enumerate())) > 1:
    #  print("Warning, more than one thread at exit:")
    #  better_exchook.dump_all_thread_tracebacks()
