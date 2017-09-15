#!/usr/bin/env python2
# -*- coding: utf-8 -*-
"""
Created on Thu Jul  6 20:31:47 2017

@author: zqwu
"""
import numpy as np
import scipy.optimize
import tensorflow as tf

import deepchem as dc

from deepchem.models.tensorgraph.layers import Dense, Concat, WeightedError, Stack, Layer, ANIFeat
from deepchem.models.tensorgraph.layers import L2Loss, Label, Weights, Feature
from deepchem.models.tensorgraph.tensor_graph import TensorGraph
from deepchem.models.tensorgraph.graph_layers import DTNNEmbedding
from deepchem.models.tensorgraph.symmetry_functions import DistanceMatrix, \
    DistanceCutoff, RadialSymmetry, AngularSymmetry, AngularSymmetryMod, \
    BPFeatureMerge, BPGather, AtomicDifferentiatedDense


class BPSymmetryFunctionRegression(TensorGraph):

  def __init__(self,
               n_tasks,
               max_atoms,
               n_feat=96,
               layer_structures=[128, 64],
               **kwargs):
    """
    Parameters
    ----------
    n_tasks: int
      Number of tasks
    max_atoms: int
      Maximum number of atoms in the dataset
    n_hidden: int, optional
      Number of hidden units in the readout function
    """
    self.n_tasks = n_tasks
    self.max_atoms = max_atoms
    self.n_feat = n_feat
    self.layer_structures = layer_structures
    super(BPSymmetryFunctionRegression, self).__init__(**kwargs)
    self.build_graph()

  def build_graph(self):
    self.atom_flags = Feature(shape=(None, self.max_atoms, self.max_atoms))
    self.atom_feats = Feature(shape=(None, self.max_atoms, self.n_feat))
    previous_layer = self.atom_feats

    Hiddens = []
    for n_hidden in self.layer_structures:
      Hidden = Dense(
          out_channels=n_hidden,
          activation_fn=tf.nn.tanh,
          in_layers=[previous_layer])
      Hiddens.append(Hidden)
      previous_layer = Hiddens[-1]

    costs = []
    self.labels_fd = []
    for task in range(self.n_tasks):
      regression = Dense(
          out_channels=1, activation_fn=None, in_layers=[Hiddens[-1]])
      output = BPGather(self.max_atoms, in_layers=[regression, self.atom_flags])
      self.add_output(output)

      label = Label(shape=(None, 1))
      self.labels_fd.append(label)
      cost = L2Loss(in_layers=[label, output])
      costs.append(cost)

    all_cost = Stack(in_layers=costs, axis=1)
    self.weights = Weights(shape=(None, self.n_tasks))
    loss = WeightedError(in_layers=[all_cost, self.weights])
    self.set_loss(loss)

  def default_generator(self,
                        dataset,
                        epochs=1,
                        predict=False,
                        pad_batches=True):
    for epoch in range(epochs):
      if not predict:
        print('Starting epoch %i' % epoch)
      for (X_b, y_b, w_b, ids_b) in dataset.iterbatches(
          batch_size=self.batch_size,
          deterministic=True,
          pad_batches=pad_batches):

        feed_dict = dict()
        if y_b is not None and not predict:
          for index, label in enumerate(self.labels_fd):
            feed_dict[label] = y_b[:, index:index + 1]
        if w_b is not None and not predict:
          feed_dict[self.weights] = w_b

        flags = np.sign(np.array(X_b[:, :, 0]))
        feed_dict[self.atom_flags] = np.stack([flags]*self.max_atoms, axis=2)*\
            np.stack([flags]*self.max_atoms, axis=1)
        feed_dict[self.atom_feats] = np.array(X_b[:, :, 1:], dtype=float)
        yield feed_dict

class ANIRegression(TensorGraph):

  def __init__(self,
               n_tasks,
               max_atoms,
               layer_structures=[128, 64],
               atom_number_cases=[1, 6, 7, 8, 16],
               **kwargs):
    """
    Parameters
    ----------
    n_tasks: int
      Number of tasks
    max_atoms: int
      Maximum number of atoms in the dataset
    n_hidden: int, optional
      Number of hidden units in the readout function
    """
    self.n_tasks = n_tasks
    self.max_atoms = max_atoms
    self.layer_structures = layer_structures
    self.atom_number_cases = atom_number_cases
    super(ANIRegression, self).__init__(**kwargs)
    self.build_graph()
    self.grad = None

  def save(self):
    self.grad = None # recompute grad on restore
    super(ANIRegression, self).save()

  def build_grad(self):
    self.grad = tf.gradients(self.outputs, self.atom_feats)

  def compute_grad(self, dataset, batch_size=1):
    with self._get_tf("Graph").as_default():
      if not self.built:
        self.build()
      if not self.grad:
        self.build_grad()

      feed_dict = dict()
      X = dataset.X
      flags = np.sign(np.array(X[:batch_size, :, 0]))
      feed_dict[self.atom_flags] = np.stack([flags]*self.max_atoms, axis=2)*\
          np.stack([flags]*self.max_atoms, axis=1)
      feed_dict[self.atom_numbers] = np.array(X[:batch_size, :, 0], dtype=int)
      feed_dict[self.atom_feats] = np.array(X[:batch_size, :, :], dtype=float)
      return self.session.run([self.grad], feed_dict=feed_dict)

  def pred_one(self, X, atomic_nums):
    num_atoms = atomic_nums.shape[0]
    X = X.reshape((num_atoms, 3))
    A = atomic_nums.reshape((atomic_nums.shape[0], 1))
    Z = np.zeros((23, 4))
    Z[:X.shape[0], 1:X.shape[1]+1] = X
    Z[:A.shape[0], :A.shape[1]] = A
    X = Z
    dd = dc.data.NumpyDataset(np.array(X).reshape((1, 23, 4)), np.array(0), np.array(1))
    return self.predict(dd)[0]

  def grad_one(self, X, atomic_nums):
    y = self.pred_one(X, atomic_nums)
    num_atoms = atomic_nums.shape[0]
    X = X.reshape((num_atoms, 3))
    A = atomic_nums.reshape((atomic_nums.shape[0], 1))
    Z = np.zeros((23, 4))
    Z[:X.shape[0], 1:X.shape[1]+1] = X
    Z[:A.shape[0], :A.shape[1]] = A
    X = Z
    inp = np.array(X).reshape((1, 23, 4))
    dd = dc.data.NumpyDataset(inp, np.array([y]), np.array([1]))
    res = self.compute_grad(dd)[0][0][0]
    res = res[:num_atoms, 1:]
    return res.reshape((num_atoms*3,))

  def minimize_structure(self, X, atomic_nums):
    num_atoms = atomic_nums.shape[0]
    res = scipy.optimize.minimize(
      self.pred_one,
      X,
      args=(atomic_nums,),
      jac=self.grad_one,
      method="BFGS",
      tol=1e-6,
      options={'disp': True})
    return res.x.reshape((num_atoms, 3))

  def build_graph(self):

    self.atom_numbers = Feature(shape=(None, self.max_atoms), dtype=tf.int32)
    self.atom_flags = Feature(shape=(None, self.max_atoms, self.max_atoms))
    self.atom_feats = Feature(shape=(None, self.max_atoms, 4))

    previous_layer = ANIFeat(self.atom_feats)

    self.featurized = previous_layer

    Hiddens = []
    for n_hidden in self.layer_structures:
      Hidden = AtomicDifferentiatedDense(
          self.max_atoms,
          n_hidden,
          self.atom_number_cases,
          activation='tanh',
          in_layers=[previous_layer, self.atom_numbers])
      Hiddens.append(Hidden)
      previous_layer = Hiddens[-1]

    costs = []
    self.labels_fd = []
    for task in range(self.n_tasks):
      regression = Dense(
          out_channels=1, activation_fn=None, in_layers=[Hiddens[-1]])
      output = BPGather(self.max_atoms, in_layers=[regression, self.atom_flags])
      self.add_output(output)

      label = Label(shape=(None, 1))
      self.labels_fd.append(label)
      cost = L2Loss(in_layers=[label, output])
      costs.append(cost)

    all_cost = Stack(in_layers=costs, axis=1)
    self.weights = Weights(shape=(None, self.n_tasks))
    loss = WeightedError(in_layers=[all_cost, self.weights])
    self.set_loss(loss)

  def default_generator(self,
                        dataset,
                        epochs=1,
                        predict=False,
                        deterministic=True,
                        pad_batches=True):
    for epoch in range(epochs):
      if not predict:
        print('Starting epoch %i' % epoch)
      for (X_b, y_b, w_b, ids_b) in dataset.iterbatches(
          batch_size=self.batch_size,
          deterministic=deterministic,
          pad_batches=pad_batches):

        feed_dict = dict()
        if y_b is not None and not predict:
          for index, label in enumerate(self.labels_fd):
            feed_dict[label] = y_b[:, index:index + 1]
        if w_b is not None and not predict:
          feed_dict[self.weights] = w_b

        flags = np.sign(np.array(X_b[:, :, 0]))
        feed_dict[self.atom_flags] = np.stack([flags]*self.max_atoms, axis=2)*\
            np.stack([flags]*self.max_atoms, axis=1)
        feed_dict[self.atom_numbers] = np.array(X_b[:, :, 0], dtype=int)
        feed_dict[self.atom_feats] = np.array(X_b[:, :, :], dtype=float)
        yield feed_dict
