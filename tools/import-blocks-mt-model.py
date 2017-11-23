#!/usr/bin/env python3

from __future__ import print_function

import os
import sys
import numpy
import re
from pprint import pprint
from nose.tools import assert_equal, assert_is_instance
from numpy.testing import assert_almost_equal, assert_allclose
import tensorflow as tf

my_dir = os.path.dirname(os.path.abspath(__file__))
returnn_dir = os.path.dirname(my_dir)
sys.path.insert(0, returnn_dir)

import better_exchook
import rnn
import Util
from TFNetwork import TFNetwork
from TFNetworkLayer import SourceLayer, LayerBase, LinearLayer


def get_network():
  """
  :rtype: TFNetwork
  """
  return rnn.engine.network


def get_input_layers():
  """
  :rtype: list[LayerBase]
  """
  ls = []
  for layer in get_network().layers.values():
    if len(layer.sources) != 1:
      continue
    if isinstance(layer.sources[0], SourceLayer):
      ls.append(layer)
  return ls


def find_our_input_embed_layer():
  """
  :rtype: LinearLayer
  """
  input_layers = get_input_layers()
  assert len(input_layers) == 1
  layer = input_layers[0]
  assert isinstance(layer, LinearLayer)
  return layer


def get_in_hierarchy(name, hierarchy):
  """
  :param str name: e.g. "decoder/sequencegenerator"
  :param dict[str,dict[str]] hierarchy: nested hierarchy
  :rtype: dict[str,dict[str]]
  """
  if "/" in name:
    name, rest = name.split("/", 1)
  else:
    rest = None
  if rest is None:
    return hierarchy[name]
  else:
    return get_in_hierarchy(rest, hierarchy[name])


def main():
  rnn.init(
    commandLineOptions=sys.argv[1:],
    config_updates={
      "task": "nop", "log": None, "device": "cpu",
      "allow_random_model_init": True,
      "debug_add_check_numerics_on_output": False},
    extra_greeting="Import Blocks MT model.")
  assert Util.BackendEngine.is_tensorflow_selected()
  config = rnn.config

  # Load Blocks MT model params.
  if not config.has("blocks_mt_model"):
    print("Please provide the option blocks_mt_model.")
    sys.exit(1)
  blocks_mt_model_fn = config.value("blocks_mt_model", "")
  assert blocks_mt_model_fn
  assert os.path.exists(blocks_mt_model_fn)
  if os.path.isdir(blocks_mt_model_fn):
    blocks_mt_model_fn += "/params.npz"
    assert os.path.exists(blocks_mt_model_fn)
  blocks_mt_model = numpy.load(blocks_mt_model_fn)
  assert isinstance(blocks_mt_model, numpy.lib.npyio.NpzFile), "did not expect type %r in file %r" % (
    type(blocks_mt_model), blocks_mt_model_fn)
  print("Params found in Blocks model:")
  blocks_params = {}  # type: dict[str,numpy.ndarray]
  blocks_params_hierarchy = {}  # type: dict[str,dict[str]]
  blocks_total_num_params = 0
  for key in sorted(blocks_mt_model.keys()):
    value = blocks_mt_model[key]
    key = key.replace("-", "/")
    assert key[0] == "/"
    key = key[1:]
    blocks_params[key] = value
    print("  %s: %s, %s" % (key, value.shape, value.dtype))
    blocks_total_num_params += numpy.prod(value.shape)
    d = blocks_params_hierarchy
    for part in key.split("/"):
      d = d.setdefault(part, {})
  print("Blocks total num params: %i" % blocks_total_num_params)

  # Init our network structure.
  from TFNetworkRecLayer import _SubnetworkRecCell
  _SubnetworkRecCell._debug_out = []  # enable for debugging intermediate values below
  rnn.engine.use_search_flag = True  # construct the net as in search
  rnn.engine.init_network_from_config()
  print("Our network model params:")
  our_params = {}  # type: dict[str,tf.Variable]
  our_total_num_params = 0
  for v in rnn.engine.network.get_params_list():
    key = v.name[:-2]
    our_params[key] = v
    print("  %s: %s, %s" % (key, v.shape, v.dtype.base_dtype.name))
    our_total_num_params += numpy.prod(v.shape.as_list())
  print("Our total num params: %i" % our_total_num_params)

  # Now matching...
  blocks_used_params = set()  # type: set[str]
  our_loaded_params = set()  # type: set[str]

  def import_var(our_var, blocks_param):
    """
    :param tf.Variable our_var:
    :param str|numpy.ndarray blocks_param:
    """
    assert isinstance(our_var, tf.Variable)
    if isinstance(blocks_param, str):
      blocks_param = load_blocks_var(blocks_param)
    assert isinstance(blocks_param, numpy.ndarray)
    assert_equal(tuple(our_var.shape.as_list()), blocks_param.shape)
    our_loaded_params.add(our_var.name[:-2])
    our_var.load(blocks_param, session=rnn.engine.tf_session)

  def load_blocks_var(blocks_param_name):
    """
    :param str blocks_param_name:
    :rtype: numpy.ndarray
    """
    assert isinstance(blocks_param_name, str)
    assert blocks_param_name in blocks_params
    blocks_used_params.add(blocks_param_name)
    return blocks_params[blocks_param_name]

  enc_name = "bidirectionalencoder"
  enc_embed_name = "EncoderLookUp0.W"
  assert enc_name in blocks_params_hierarchy
  assert enc_embed_name in blocks_params_hierarchy[enc_name]  # input embedding
  num_encoder_layers = max([
    int(re.match(".*([0-9]+)", s).group(1))
    for s in blocks_params_hierarchy[enc_name]
    if s.startswith("EncoderBidirectionalLSTM")])
  blocks_input_dim, blocks_input_embed_dim = blocks_params["%s/%s" % (enc_name, enc_embed_name)].shape
  print("Blocks input dim: %i, embed dim: %i" % (blocks_input_dim, blocks_input_embed_dim))
  print("Blocks num encoder layers: %i" % num_encoder_layers)
  expected_enc_entries = (
    ["EncoderLookUp0.W"] +
    ["EncoderBidirectionalLSTM%i" % i for i in range(1, num_encoder_layers + 1)])
  assert_equal(set(expected_enc_entries), set(blocks_params_hierarchy[enc_name].keys()))

  our_input_layer = find_our_input_embed_layer()
  assert our_input_layer.input_data.dim == blocks_input_dim
  assert our_input_layer.output.dim == blocks_input_embed_dim
  assert not our_input_layer.with_bias
  import_var(our_input_layer.params["W"], "%s/%s" % (enc_name, enc_embed_name))

  dec_name = "decoder/sequencegenerator"
  dec_hierarchy_base = get_in_hierarchy(dec_name, blocks_params_hierarchy)
  assert_equal(set(dec_hierarchy_base.keys()), {"att_trans", "readout"})
  dec_embed_name = "readout/lookupfeedbackwmt15/lookuptable.W"
  get_in_hierarchy(dec_embed_name, dec_hierarchy_base)  # check

  for i in range(num_encoder_layers):
    # Assume standard LSTMCell.
    # i = input_gate, j = new_input, f = forget_gate, o = output_gate
    # lstm_matrix = self._linear1([inputs, m_prev])
    # i, j, f, o = array_ops.split(value=lstm_matrix, num_or_size_splits=4, axis=1)
    # bias (4*in), kernel (in+out,4*out), w_(f|i|o)_diag (out)
    # prefix: rec/rnn/lstm_cell
    # Blocks: gate-in, gate-forget, next-in, gate-out
    for direction in ("fwd", "bwd"):
      our_layer = get_network().layers["lstm%i_%s" % (i, direction[:2])]
      blocks_prefix = "bidirectionalencoder/EncoderBidirectionalLSTM%i" % (i + 1,)
      # (in,out*4), (out*4,)
      W_in, b = [load_blocks_var(
        "%s/%s_fork/fork_inputs.%s" % (blocks_prefix, {"bwd": "back", "fwd": "fwd"}[direction], p))
        for p in ("W", "b")]
      W_re = load_blocks_var(
        "%s/bidirectionalseparateparameters/%s.W_state" % (blocks_prefix, {"fwd": "forward", "bwd": "backward"}[direction]))
      W = numpy.concatenate([W_in, W_re], axis=0)
      b = lstm_vec_blocks_to_tf(b)
      W = lstm_vec_blocks_to_tf(W)
      import_var(our_layer.params["rnn/lstm_cell/bias"], b)
      import_var(our_layer.params["rnn/lstm_cell/kernel"], W)
      import_var(our_layer.params["initial_c"], "%s/bidirectionalseparateparameters/%s.initial_cells" % (blocks_prefix, {"fwd": "forward", "bwd": "backward"}[direction]))
      import_var(our_layer.params["initial_h"], "%s/bidirectionalseparateparameters/%s.initial_state" % (blocks_prefix, {"fwd": "forward", "bwd": "backward"}[direction]))
      for s1, s2 in [("W_cell_to_in", "w_i_diag"), ("W_cell_to_forget", "w_f_diag"), ("W_cell_to_out", "w_o_diag")]:
        import_var(our_layer.params["rnn/lstm_cell/%s" % s2], "%s/bidirectionalseparateparameters/%s.%s" % (blocks_prefix, {"fwd": "forward", "bwd": "backward"}[direction], s1))
  import_var(get_network().layers["enc_ctx"].params["W"], "decoder/sequencegenerator/att_trans/attention/encoder_state_transformer.W")
  import_var(get_network().layers["enc_ctx"].params["b"], "decoder/sequencegenerator/att_trans/attention/encoder_state_transformer.b")
  import_var(our_params["output/rec/s/initial_c"], "decoder/sequencegenerator/att_trans/lstm_decoder.initial_cells")
  import_var(our_params["output/rec/s/initial_h"], "decoder/sequencegenerator/att_trans/lstm_decoder.initial_state")
  import_var(our_params["output/rec/weight_feedback/W"], "decoder/sequencegenerator/att_trans/attention/sum_alignment_transformer.W")

  print("Not initialized own params:")
  for key, v in sorted(our_params.items()):
    if key in our_loaded_params:
      continue
    print("  %s: %s, %s" % (key, v.shape, v.dtype.base_dtype.name))
  print("Not used Blocks params:")
  for key, value in sorted(blocks_params.items()):
    if key in blocks_used_params:
      continue
    print("  %s: %s, %s" % (key, value.shape, value.dtype))
  print("Done.")

  blocks_debug_dump_output = config.value("blocks_debug_dump_output", None)
  if blocks_debug_dump_output:
    blocks_initial_outputs = numpy.load("%s/initial_states_data.0.npz" % blocks_debug_dump_output)
    input_seq = blocks_initial_outputs["input"]
    beam_size, seq_len = input_seq.shape
    input_seq = input_seq[0]  # all the same, select beam 0
    assert isinstance(input_seq, numpy.ndarray)
    print("Debug input seq: %s" % input_seq.tolist())
    from GeneratingDataset import StaticDataset
    dataset = StaticDataset(
      data=[{"data": input_seq}],
      output_dim={"data": get_network().extern_data.get_default_input_data().get_kwargs()})
    dataset.init_seq_order(epoch=0)
    extract_output_dict = {
      "enc_data_emb": get_network().layers["data_embed"].output.get_placeholder_as_batch_major(),
      "encoder": get_network().layers["encoder"].output.get_placeholder_as_batch_major(),
      "enc_ctx": get_network().layers["enc_ctx"].output.get_placeholder_as_batch_major(),
      "output": get_network().layers["output"].output.get_placeholder_as_batch_major()
    }
    from TFNetworkLayer import concat_sources
    for i in range(num_encoder_layers):
      extract_output_dict["enc_layer_%i" % i] = concat_sources(
        [get_network().layers["lstm%i_fw" % i], get_network().layers["lstm%i_bw" % i]]
      ).get_placeholder_as_batch_major()
    extract_output_dict["enc_layer_0_fwd"] = get_network().layers["lstm0_fw"].output.get_placeholder_as_batch_major()
    our_output = rnn.engine.run_single(
      dataset=dataset, seq_idx=0, output_dict=extract_output_dict)
    blocks_out = blocks_initial_outputs["bidirectionalencoder_EncoderLookUp0__EncoderLookUp0_apply_output"]
    our_out = our_output["enc_data_emb"]
    print("our enc emb shape:", our_out.shape)
    print("Blocks enc emb shape:", blocks_out.shape)
    assert our_out.shape[:2] == (1, seq_len)
    assert blocks_out.shape[:2] == (seq_len, beam_size)
    assert our_out.shape[2] == blocks_out.shape[2]
    assert_almost_equal(our_out[0], blocks_out[:, 0])
    blocks_lstm0_out_ref = calc_lstm(blocks_out[:, 0], blocks_params)
    blocks_lstm0_out = blocks_initial_outputs["bidirectionalencoder_EncoderBidirectionalLSTM1_bidirectionalseparateparameters_forward__forward_apply_states"]
    our_lstm0_out = our_output["enc_layer_0_fwd"]
    assert blocks_lstm0_out.shape == (seq_len, beam_size) + blocks_lstm0_out_ref.shape
    assert our_lstm0_out.shape == (1, seq_len) + blocks_lstm0_out_ref.shape
    assert_almost_equal(blocks_lstm0_out[0, 0], blocks_lstm0_out_ref)
    print("Blocks LSTM0 frame 0 matched to ref calc.")
    assert_almost_equal(our_lstm0_out[0, 0], blocks_lstm0_out_ref)
    print("Our LSTM0 frame 0 matched to ref calc.")
    for i in range(num_encoder_layers):
      blocks_out = blocks_initial_outputs[
        "bidirectionalencoder_EncoderBidirectionalLSTM%i_bidirectionalseparateparameters__bidirectionalseparateparameters_apply_output_0" % (i + 1,)]
      our_out = our_output["enc_layer_%i" % i]
      print("our enc layer %i shape:" % i, our_out.shape)
      print("Blocks enc layer %i shape:" % i, blocks_out.shape)
      assert our_out.shape[:2] == (1, seq_len)
      assert blocks_out.shape[:2] == (seq_len, beam_size)
      assert our_out.shape[2] == blocks_out.shape[2]
      assert_almost_equal(our_out[0], blocks_out[:, 0], decimal=6)
    print("our encoder shape:", our_output["encoder"].shape)
    blocks_encoder_out = blocks_initial_outputs["bidirectionalencoder__bidirectionalencoder_apply_representation"]
    print("Blocks encoder shape:", blocks_encoder_out.shape)
    assert our_output["encoder"].shape[:2] == (1, seq_len)
    assert blocks_encoder_out.shape[:2] == (seq_len, beam_size)
    assert our_output["encoder"].shape[2] == blocks_encoder_out.shape[2]
    assert_almost_equal(our_output["encoder"][0], blocks_encoder_out[:, 0], decimal=6)
    blocks_first_frame_outputs = numpy.load("%s/next_states.0.npz" % blocks_debug_dump_output)
    blocks_enc_ctx_out = blocks_first_frame_outputs["decoder_sequencegenerator_att_trans_attention__attention_preprocess_preprocessed_attended"]
    our_enc_ctx_out = our_output["enc_ctx"]
    print("Blocks enc ctx shape:", blocks_enc_ctx_out.shape)
    assert blocks_enc_ctx_out.shape[:2] == (seq_len, beam_size)
    assert our_enc_ctx_out.shape[:2] == (1, seq_len)
    assert blocks_enc_ctx_out.shape[2:] == our_enc_ctx_out.shape[2:]
    assert_almost_equal(blocks_enc_ctx_out[:, 0], our_enc_ctx_out[0], decimal=5)
    our_dec_outputs = {v["step"]: v for v in _SubnetworkRecCell._debug_out}
    assert our_dec_outputs
    print("our dec frame keys:", sorted(our_dec_outputs[0].keys()))
    last_lstm_state = blocks_params["decoder/sequencegenerator/att_trans/lstm_decoder.initial_state"]
    last_lstm_cells = blocks_params["decoder/sequencegenerator/att_trans/lstm_decoder.initial_cells"]
    accumulated_weights = numpy.zeros((seq_len,), dtype="float32")
    for dec_step in range(3):
      blocks_frame_outputs = numpy.load("%s/next_states.%i.npz" % (blocks_debug_dump_output, dec_step))
      our_dec_frame_outputs = our_dec_outputs[dec_step]
      blocks_accumulated_weights = blocks_frame_outputs["decoder_sequencegenerator__sequencegenerator_generate_accumulated_weights"]
      assert blocks_accumulated_weights.shape == (beam_size, seq_len)
      assert_almost_equal(blocks_accumulated_weights[0], accumulated_weights)
      energy_sum = blocks_enc_ctx_out[:, 0]  # (T,enc-ctx-dim)
      # weight_feedback = numpy.dot(accumulated_weights, blocks_params["decoder/sequencegenerator/att_trans/attention/sum_alignment_transformer.W"])  # TODO...
      # energy_sum += weight_feedback  # TODO
      transformed_states = 0  # TODO, based on states -> state_transformers (Linear?)
      energy_sum += transformed_states
      blocks_energy_sum_tanh = blocks_frame_outputs["decoder_sequencegenerator_att_trans_attention_energy_comp_tanh__tanh_apply_output"]
      assert blocks_energy_sum_tanh.shape == (seq_len, beam_size, energy_sum.shape[-1])
      # assert_almost_equal(blocks_energy_sum_tanh[1], numpy.tanh(energy_sum), decimal=6)  # TODO
      # pprint(our_dec_frame_outputs)
      blocks_last_lstm_state = blocks_frame_outputs["decoder_sequencegenerator__sequencegenerator_generate_states"]
      blocks_last_lstm_cells = blocks_frame_outputs["decoder_sequencegenerator__sequencegenerator_generate_cells"]
      assert blocks_last_lstm_state.shape == (beam_size, last_lstm_state.shape[0])
      assert_almost_equal(blocks_last_lstm_state[0], last_lstm_state)
      assert_almost_equal(blocks_last_lstm_cells[0], last_lstm_cells)
      if dec_step == 0: break  # TODO ...

  print("Finished importing.")


def lstm_vec_blocks_to_tf(x):
  """
  Blocks order: gate-in, gate-forget, next-in, gate-out
  TF order: i = input_gate, j = new_input, f = forget_gate, o = output_gate
  :param numpy.ndarray x: (..., dim*4)
  :rtype: numpy.ndarray
  """
  axis = x.ndim - 1
  i, f, j, o = numpy.split(x, 4, axis=axis)
  return numpy.concatenate([i, j, f, o], axis=axis)


def calc_lstm(x, blocks_params):
  """
  :param numpy.ndarray x:
  :param dict[str,numpy.ndarray] blocks_params:
  :rtype: numpy.ndarray
  """
  prefix = "bidirectionalencoder/EncoderBidirectionalLSTM1"
  prefix1 = "%s/bidirectionalseparateparameters/forward." % prefix
  prefix2 = "%s/fwd_fork/fork_inputs." % prefix
  W_in, b = blocks_params[prefix2 + "W"], blocks_params[prefix2 + "b"]
  assert b.ndim == 1
  assert b.shape[0] % 4 == 0
  out_dim = b.shape[0] // 4
  seq_len, in_dim = x.shape
  assert W_in.shape == (in_dim, out_dim * 4)
  W_re = blocks_params[prefix1 + "W_state"]
  assert W_re.shape == (out_dim, out_dim * 4)
  W_cell_to_in = blocks_params[prefix1 + "W_cell_to_in"]
  W_cell_to_forget = blocks_params[prefix1 + "W_cell_to_forget"]
  W_cell_to_out = blocks_params[prefix1 + "W_cell_to_out"]
  assert W_cell_to_in.shape == W_cell_to_forget.shape == W_cell_to_out.shape == (out_dim,)
  initial_state, initial_cell = blocks_params[prefix1 + "initial_state"], blocks_params[prefix1 + "initial_cells"]
  assert initial_state.shape == initial_cell.shape == (out_dim,)
  t = 0
  last_state = initial_state
  last_cell = initial_cell
  z = numpy.dot(x[t], W_in) + numpy.dot(last_state, W_re) + b
  assert z.shape == (out_dim * 4,)
  # Blocks order: gate-in, gate-forget, next-in, gate-out
  gate_in, gate_forget, next_in, gate_out = numpy.split(z, 4)
  gate_in += W_cell_to_in * last_cell
  gate_forget += W_cell_to_forget * last_cell
  gate_in = 1.0 / (1.0 + numpy.exp(-gate_in))
  gate_forget = 1.0 / (1.0 + numpy.exp(-gate_forget))
  next_in = numpy.tanh(next_in)
  cur_cell = last_cell * gate_forget + next_in * gate_in
  gate_out += W_cell_to_out * cur_cell
  gate_out = 1.0 / (1.0 + numpy.exp(-gate_out))
  cur_state = numpy.tanh(cur_cell) * gate_out
  last_cell = cur_cell
  last_state = cur_state
  return cur_state


if __name__ == "__main__":
  better_exchook.install()
  main()
