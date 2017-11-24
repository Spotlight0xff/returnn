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
  import_var(our_params["output/rec/target_embed/W"], "decoder/sequencegenerator/readout/lookupfeedbackwmt15/lookuptable.W")

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
      "enc_src_emb": get_network().layers["source_embed"].output.get_placeholder_as_batch_major(),
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
    our_out = our_output["enc_src_emb"]
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
    fertility = numpy.dot(blocks_encoder_out[:, 0], blocks_params["decoder/sequencegenerator/att_trans/attention/fertility_transformer.W"])
    fertility = 1.0 / (1.0 + numpy.exp(-fertility))  # sigmoid
    assert fertility.shape == (seq_len, 1)
    fertility = fertility[:, 0]
    assert fertility.shape == (seq_len,)
    our_dec_outputs = {v["step"]: v for v in _SubnetworkRecCell._debug_out}
    assert our_dec_outputs
    print("our dec frame keys:", sorted(our_dec_outputs[0].keys()))
    dec_lookup = blocks_params["decoder/sequencegenerator/readout/lookupfeedbackwmt15/lookuptable.W"]
    last_lstm_state = blocks_params["decoder/sequencegenerator/att_trans/lstm_decoder.initial_state"]
    last_lstm_cells = blocks_params["decoder/sequencegenerator/att_trans/lstm_decoder.initial_cells"]
    last_accumulated_weights = numpy.zeros((seq_len,), dtype="float32")
    last_output = 0
    for dec_step in range(100):
      blocks_frame_outputs_fn = "%s/next_states.%i.npz" % (blocks_debug_dump_output, dec_step)
      if dec_step > 3:
        if not os.path.exists(blocks_frame_outputs_fn):
          break
      blocks_frame_outputs = numpy.load(blocks_frame_outputs_fn)
      our_dec_frame_outputs = our_dec_outputs[dec_step]
      blocks_last_lstm_state = blocks_frame_outputs["decoder_sequencegenerator__sequencegenerator_generate_states"]
      blocks_last_lstm_cells = blocks_frame_outputs["decoder_sequencegenerator__sequencegenerator_generate_cells"]
      assert blocks_last_lstm_state.shape == (beam_size, last_lstm_state.shape[0])
      assert_almost_equal(blocks_last_lstm_state[0], last_lstm_state, decimal=5)
      assert_almost_equal(blocks_last_lstm_cells[0], last_lstm_cells, decimal=5)
      blocks_last_accum_weights = blocks_frame_outputs["decoder_sequencegenerator__sequencegenerator_generate_accumulated_weights"]
      assert blocks_last_accum_weights.shape == (beam_size, seq_len)
      assert_almost_equal(blocks_last_accum_weights[0], last_accumulated_weights, decimal=5)
      energy_sum = numpy.copy(blocks_enc_ctx_out[:, 0])  # (T,enc-ctx-dim)
      weight_feedback = numpy.dot(last_accumulated_weights[:, None], blocks_params["decoder/sequencegenerator/att_trans/attention/sum_alignment_transformer.W"])
      energy_sum += weight_feedback
      transformed_states = numpy.dot(last_lstm_state[None, :], blocks_params["decoder/sequencegenerator/att_trans/attention/state_trans/transform_states.W"])
      transformed_cells = numpy.dot(last_lstm_cells[None, :], blocks_params["decoder/sequencegenerator/att_trans/attention/state_trans/transform_cells.W"])
      energy_sum += transformed_states + transformed_cells
      assert energy_sum.shape == (seq_len, blocks_enc_ctx_out.shape[-1])
      blocks_energy_sum_tanh = blocks_frame_outputs["decoder_sequencegenerator_att_trans_attention_energy_comp_tanh__tanh_apply_output"]
      assert blocks_energy_sum_tanh.shape == (seq_len, beam_size, energy_sum.shape[-1])
      assert_almost_equal(blocks_energy_sum_tanh[:, 0], numpy.tanh(energy_sum), decimal=5)
      blocks_energy = blocks_frame_outputs["decoder_sequencegenerator_att_trans_attention_energy_comp__energy_comp_apply_output"]
      assert blocks_energy.shape == (seq_len, beam_size, 1)
      energy = numpy.dot(numpy.tanh(energy_sum), blocks_params["decoder/sequencegenerator/att_trans/attention/energy_comp/linear.W"])
      assert energy.shape == (seq_len, 1)
      assert_almost_equal(blocks_energy[:, 0], energy, decimal=5)
      weights = softmax(energy[:, 0])
      assert weights.shape == (seq_len,)
      accumulated_weights = last_accumulated_weights + weights / (2.0 * fertility)
      assert accumulated_weights.shape == (seq_len,)
      blocks_accumulated_weights = blocks_frame_outputs["decoder_sequencegenerator_att_trans_attention__attention_take_glimpses_accumulated_weights"]
      assert blocks_accumulated_weights.shape == (beam_size, seq_len)
      assert_almost_equal(blocks_accumulated_weights[0], accumulated_weights, decimal=5)
      blocks_weights = blocks_frame_outputs["decoder_sequencegenerator_att_trans_attention__attention_compute_weights_output_0"]
      assert blocks_weights.shape == (seq_len, beam_size)
      assert_almost_equal(weights, blocks_weights[:, 0], decimal=5)
      weighted_avg = (weights[:, None] * blocks_encoder_out[:, 0]).sum(axis=0)  # att in our
      assert weighted_avg.shape == (blocks_encoder_out.shape[-1],)
      blocks_weighted_avg = blocks_frame_outputs["decoder_sequencegenerator_att_trans_attention__attention_compute_weighted_averages_output_0"]
      assert blocks_weighted_avg.shape == (beam_size, blocks_encoder_out.shape[-1])
      assert_almost_equal(blocks_weighted_avg[0], weighted_avg, decimal=5)

      blocks_last_output = blocks_frame_outputs["decoder_sequencegenerator__sequencegenerator_generate_outputs"]
      assert blocks_last_output.shape == (beam_size,)
      assert max(blocks_last_output[0], 0) == last_output
      last_target_embed = dec_lookup[last_output]

      readout_in = \
        numpy.dot(last_lstm_state, blocks_params["decoder/sequencegenerator/readout/merge/transform_states.W"]) + \
        numpy.dot(last_target_embed, blocks_params["decoder/sequencegenerator/readout/merge/transform_feedback.W"]) + \
        numpy.dot(weighted_avg, blocks_params["decoder/sequencegenerator/readout/merge/transform_weighted_averages.W"])
      readout_in += blocks_params["decoder/sequencegenerator/readout/initializablefeedforwardsequence/maxout_bias.b"]
      assert readout_in.shape == (blocks_params["decoder/sequencegenerator/readout/initializablefeedforwardsequence/maxout_bias.b"].shape[0],)
      readout = readout_in.reshape((readout_in.shape[0] // 2, 2)).max(axis=1)
      prob_logits = numpy.dot(readout, blocks_params["decoder/sequencegenerator/readout/initializablefeedforwardsequence/softmax1.W"]) + \
        blocks_params["decoder/sequencegenerator/readout/initializablefeedforwardsequence/softmax1.b"]
      output_prob = softmax(prob_logits)
      ref_output = numpy.argmax(output_prob)
      blocks_dec_output = blocks_frame_outputs["decoder_sequencegenerator_readout__readout_emit_output_0"]
      assert blocks_dec_output.shape == (beam_size,)
      assert ref_output in blocks_dec_output
      print("Frame %i: Ref output symbol: %i, Blocks: %r" % (dec_step, int(ref_output), blocks_dec_output.tolist()))
      if dec_step == 0:
        assert blocks_dec_output[0] == ref_output
      ref_output = blocks_dec_output[0]

      blocks_target_emb = blocks_frame_outputs["decoder_sequencegenerator_fork__fork_apply_feedback_decoder_input"]
      assert blocks_target_emb.shape == (beam_size, dec_lookup.shape[1])
      target_embed = dec_lookup[ref_output]
      assert target_embed.shape == (dec_lookup.shape[1],)
      assert_almost_equal(blocks_target_emb[0], target_embed)

      feedback_to_decoder = numpy.dot(target_embed, blocks_params["decoder/sequencegenerator/att_trans/feedback_to_decoder/fork_inputs.W"])
      context_to_decoder = numpy.dot(weighted_avg, blocks_params["decoder/sequencegenerator/att_trans/context_to_decoder/fork_inputs.W"])
      lstm_z = feedback_to_decoder + context_to_decoder
      assert lstm_z.shape == feedback_to_decoder.shape == context_to_decoder.shape == (last_lstm_state.shape[-1] * 4,)
      blocks_feedback_to_decoder = blocks_frame_outputs["decoder_sequencegenerator_att_trans_feedback_to_decoder__feedback_to_decoder_apply_inputs"]
      blocks_context_to_decoder = blocks_frame_outputs["decoder_sequencegenerator_att_trans_context_to_decoder__context_to_decoder_apply_inputs"]
      assert blocks_feedback_to_decoder.shape == blocks_context_to_decoder.shape == (beam_size, last_lstm_state.shape[-1] * 4)
      assert_almost_equal(blocks_feedback_to_decoder[0], feedback_to_decoder, decimal=5)
      assert_almost_equal(blocks_context_to_decoder[0], context_to_decoder, decimal=5)
      lstm_state, lstm_cells = calc_raw_lstm(
        lstm_z, blocks_params=blocks_params,
        prefix="decoder/sequencegenerator/att_trans/lstm_decoder.",
        last_state=last_lstm_state, last_cell=last_lstm_cells)
      assert lstm_state.shape == last_lstm_state.shape == lstm_cells.shape == last_lstm_cells.shape
      blocks_lstm_state = blocks_frame_outputs["decoder_sequencegenerator_att_trans_lstm_decoder__lstm_decoder_apply_states"]
      blocks_lstm_cells = blocks_frame_outputs["decoder_sequencegenerator_att_trans_lstm_decoder__lstm_decoder_apply_cells"]
      assert blocks_lstm_state.shape == blocks_lstm_cells.shape == (beam_size, last_lstm_state.shape[-1])
      assert_almost_equal(blocks_lstm_state[0], lstm_state, decimal=5)
      assert_almost_equal(blocks_lstm_cells[0], lstm_cells, decimal=5)

      last_accumulated_weights = accumulated_weights
      last_lstm_state = lstm_state
      last_lstm_cells = lstm_cells
      last_output = ref_output
      if last_output == 0:
        print("Sequence finished, seq len %i." % dec_step)
        break

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


def calc_lstm(x, blocks_params, t=0):
  """
  :param numpy.ndarray x: (seq_len, in_dim)
  :param dict[str,numpy.ndarray] blocks_params:
  :param int t:
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
  z = numpy.dot(x[t], W_in) + b
  assert z.shape == (out_dim * 4,)
  cur_state, cur_cell = calc_raw_lstm(z, blocks_params=blocks_params, prefix=prefix1)
  return cur_state


def calc_raw_lstm(z, blocks_params, prefix, last_state=None, last_cell=None):
  """
  :param numpy.ndarray z: shape (out_dim * 4,)
  :param dict[str,numpy.ndarray] blocks_params:
  :param str prefix: e.g. "bidirectionalencoder/EncoderBidirectionalLSTM1/bidirectionalseparateparameters/forward."
  :param numpy.ndarray|None last_state: (out_dim,)
  :param numpy.ndarray|None last_cell: (out_dim,)
  :rtype: numpy.ndarray
  """
  assert z.ndim == 1
  assert z.shape[-1] % 4 == 0
  out_dim = z.shape[-1] // 4
  W_re = blocks_params[prefix + "W_state"]
  assert W_re.shape == (out_dim, out_dim * 4)
  W_cell_to_in = blocks_params[prefix + "W_cell_to_in"]
  W_cell_to_forget = blocks_params[prefix + "W_cell_to_forget"]
  W_cell_to_out = blocks_params[prefix + "W_cell_to_out"]
  assert W_cell_to_in.shape == W_cell_to_forget.shape == W_cell_to_out.shape == (out_dim,)
  if last_cell is None and last_state is None:
    initial_state, initial_cell = blocks_params[prefix + "initial_state"], blocks_params[prefix + "initial_cells"]
    assert initial_state.shape == initial_cell.shape == (out_dim,)
    last_state = initial_state
    last_cell = initial_cell
  z = z + numpy.dot(last_state, W_re)
  assert z.shape == (out_dim * 4,)
  # Blocks order: gate-in, gate-forget, next-in, gate-out
  gate_in, gate_forget, next_in, gate_out = numpy.split(z, 4)
  gate_in = gate_in + W_cell_to_in * last_cell
  gate_forget = gate_forget + W_cell_to_forget * last_cell
  gate_in = 1.0 / (1.0 + numpy.exp(-gate_in))
  gate_forget = 1.0 / (1.0 + numpy.exp(-gate_forget))
  next_in = numpy.tanh(next_in)
  cur_cell = last_cell * gate_forget + next_in * gate_in
  gate_out = gate_out + W_cell_to_out * cur_cell
  gate_out = 1.0 / (1.0 + numpy.exp(-gate_out))
  cur_state = numpy.tanh(cur_cell) * gate_out
  return cur_state, cur_cell


def softmax(x, axis=-1):
  e_x = numpy.exp(x - numpy.max(x, axis=axis))
  return e_x / e_x.sum(axis=axis)


if __name__ == "__main__":
  better_exchook.install()
  main()
